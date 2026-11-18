"""
BarBook: the 1-minute history of one signal market (ES, NQ, GC, CL), shared by every
strategy that trades on it (MES and ES strategies read the same ES book).

Warm-up (the higher-timeframe filters need ~30 days):
  * 1-minute bars, last 10 days    POST /history "1 min" "10 D"     (exact)
  * 5-minute bars, last 30 days    POST /history "5 mins" "30 D"    (older part only: each 5-minute
                                   bar becomes one row at its open minute - every 5/60/120/240-minute
                                   candle built from it is exact)
  * 30-minute bars, last 90 days   POST /history "30 mins" "90 D"   (oldest part: settles the 4-hour
                                   EMAs; the patterns themselves only look at the last few days)
  * the disk cache                 data/bars/<SYM>.parquet - exact 1-minute rows saved while running,
                                   so after a few weeks the whole window is exact 1-minute data
Live: GET /bars/{symbol} once per minute; only completed (settled) minutes are used.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from pathlib import Path
from typing import List, Optional

import pandas as pd

from desk.settings import DATA

log = logging.getLogger("desk.bars")
NY = "America/New_York"
COLS = ["t", "open", "high", "low", "close", "volume"]


def frame(rows: list) -> pd.DataFrame:
    """hub bar dicts -> DataFrame with t = bar open, tz-aware UTC."""
    if not rows:
        return pd.DataFrame({c: pd.Series(dtype="float64") for c in COLS}).assign(
            t=pd.Series(dtype="datetime64[ns, UTC]"))
    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["time"].astype(str), utc=True, format="ISO8601")
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0).astype(float)
    return df[COLS].sort_values("t").drop_duplicates("t", keep="last").reset_index(drop=True)


class BarBook:
    RETRY_SECS = 15                         # re-ask for a missing just-ended minute until this second

    def __init__(self, symbol: str, exchange: str, days: int, settle: float):
        self.symbol = symbol
        self.exchange = exchange
        self.days = days
        self.settle = settle
        self.df = frame([])                 # completed minutes, oldest first
        self.exact_from: Optional[pd.Timestamp] = None   # rows from here on are real 1-minute bars
        self.current_price: Optional[float] = None
        self.last_fetch_minute = None
        self.last_live = 0.0                # monotonic time of the last live update
        self._dirty = 0
        self.path = DATA / "bars" / f"{symbol}.parquet"
        self.ready = False
        # the hub's stream is IBKR's continuous contract; when it moves to the next month the prices jump
        # by the calendar spread. Older minutes are then shifted by that jump (like TradingView's
        # back-adjusted ES1!), so no pattern is built on the jump. Kept in memory; the cache stays raw.
        self.series_month: Optional[str] = None
        self.adjust: List[tuple] = []         # (first minute of the new month, jump)

    # ------------------------------------------------------------------ warm-up
    MIN_DAYS = 28            # the higher-timeframe filters need this much history (calendar days)
    RETRY_WAIT = 15.0

    def _history(self, hub, bar_size: str, duration: str, tries: int = 3) -> pd.DataFrame:
        """One history layer; IBKR cancels requests when it is busy (code 162), so try again."""
        for k in range(tries):
            rows = hub.history(self.symbol, self.exchange, bar_size, duration)
            if rows:
                return frame(rows)
            if k < tries - 1:
                log.warning(f"[{self.symbol}] {bar_size} history not received - trying again")
                time.sleep(self.RETRY_WAIT * (k + 1))
        return frame([])

    def warmup(self, hub) -> bool:
        cache = self._load_cache()
        m1 = self._history(hub, "1 min", "10 D")
        m5 = self._history(hub, "5 mins", f"{min(self.days, 30)} D")
        m30 = self._history(hub, "30 mins", f"{min(self.days, 120)} D") if self.days > 30 else frame([])
        if m1.empty and cache.empty:
            log.error(f"[{self.symbol}] no 1-minute history from the hub")
            return False
        exact = pd.concat([cache, m1]).drop_duplicates("t", keep="last").sort_values("t")
        first_exact = exact.t.min() if len(exact) else None
        older = m5[m5.t < first_exact] if first_exact is not None and len(m5) else m5.iloc[0:0]
        first5 = older.t.min() if len(older) else first_exact
        oldest = m30[m30.t < first5] if first5 is not None and len(m30) else m30.iloc[0:0]
        older = pd.concat([oldest, older])
        self.df = pd.concat([older, exact]).sort_values("t").reset_index(drop=True)
        self.exact_from = first_exact
        self._trim()
        span = (self.df.t.iloc[-1] - self.df.t.iloc[0]) if len(self.df) else pd.Timedelta(0)
        need = pd.Timedelta(days=min(self.days, self.MIN_DAYS))
        if span < need:
            # never trade on a short window: the EMAs / ATRs would not be the TradingView values yet
            log.error(f"[{self.symbol}] only {span.days} days of history (need {need.days}) - not ready, "
                      f"retrying the warm-up")
            return False
        self.ready = len(self.df) > 0
        log.info(f"[{self.symbol}] warm-up: {len(self.df):,} rows, exact 1-minute from "
                 f"{first_exact.tz_convert(NY):%Y-%m-%d %H:%M} ({len(older):,} older 5-minute rows)")
        self._save_cache(force=True)
        return self.ready

    # ------------------------------------------------------------------ live
    def due(self, now_et: dt.datetime) -> bool:
        """Fetch once per wall-clock minute, `settle` seconds after it starts."""
        m = now_et.replace(second=0, microsecond=0)
        return self.last_fetch_minute != m and now_et.second >= self.settle

    def update(self, hub, now_utc: pd.Timestamp) -> List[pd.Series]:
        """Read the live stream; return the NEW completed minutes (oldest first)."""
        self.last_fetch_minute = now_utc.tz_convert(NY).tz_localize(None).replace(second=0, microsecond=0)
        rows = self._update(hub, now_utc)
        # the minute that just ended is not in the stream yet: try again on the next tick (a few seconds),
        # not a whole minute later - until RETRY_SECS into the minute (market closed: give up, next minute)
        want = now_utc.floor("min") - pd.Timedelta(minutes=1)
        have = self.df.t.iloc[-1] if len(self.df) else None
        if (have is None or have < want) and now_utc.second < self.RETRY_SECS:
            self.last_fetch_minute = None
        return rows

    def _update(self, hub, now_utc: pd.Timestamp) -> List[pd.Series]:
        data = hub.bars(self.symbol)
        if not data or not data.get("bars"):
            hub.subscribe_bars(self.symbol, self.exchange)    # never subscribed / dropped: (re)start it
            return []
        if data.get("current_price"):
            self.current_price = float(data["current_price"])
        live = frame(data["bars"])
        cut = now_utc - pd.Timedelta(seconds=60 + self.settle)
        live = live[live.t <= cut]                         # the forming minute is not used
        if live.empty:
            return []
        last = self.df.t.iloc[-1] if len(self.df) else None
        new = live if last is None else live[live.t > last]
        # a minute already stored can still be corrected by the stream (late trades): take the stream's
        if last is not None:
            upd = live[(live.t >= last - pd.Timedelta(minutes=3)) & (live.t <= last)]
            if len(upd):
                keep = self.df[~self.df.t.isin(upd.t)]
                self.df = pd.concat([keep, upd]).sort_values("t").reset_index(drop=True)
        if len(new):
            self.df = pd.concat([self.df, new]).sort_values("t").reset_index(drop=True)
            self._dirty += len(new)
            self._trim()
            self.last_live = time.monotonic()
            if self.exact_from is None:
                self.exact_from = new.t.iloc[0]
            if self._dirty >= 10:
                self._save_cache()
        return [r for _, r in new.iterrows()]

    def adjusted(self, df: pd.DataFrame) -> pd.DataFrame:
        if not self.adjust or df.empty:
            return df
        df = df.copy()
        for t_b, j in self.adjust:
            m = df.t < t_b
            if m.any():
                df.loc[m, ["open", "high", "low", "close"]] += j
        return df

    def adjust_row(self, row):
        j = sum(jj for t_b, jj in self.adjust if row.t < t_b)
        if not j:
            return row
        row = row.copy()
        for c in ("open", "high", "low", "close"):
            row[c] += j
        return row

    def window(self, upto: Optional[pd.Timestamp] = None) -> pd.DataFrame:
        """Completed minutes (all, or those with t < upto), back-adjusted for live month switches."""
        df = self.df if upto is None else self.df[self.df.t < upto]
        return self.adjusted(df).reset_index(drop=True)

    # ------------------------------------------------------------------ cache
    def _trim(self):
        if len(self.df):
            start = self.df.t.iloc[-1] - pd.Timedelta(days=self.days + 5)
            self.df = self.df[self.df.t >= start].reset_index(drop=True)

    def _load_cache(self) -> pd.DataFrame:
        try:
            if self.path.exists():
                df = pd.read_parquet(self.path)
                df["t"] = pd.to_datetime(df["t"], utc=True)
                return df[COLS]
        except Exception as exc:
            log.warning(f"[{self.symbol}] bar cache unreadable ({exc}) - ignored")
        return frame([])

    def _save_cache(self, force: bool = False):
        if not force and self._dirty < 10:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            exact = self.df if self.exact_from is None else self.df[self.df.t >= self.exact_from]
            exact.to_parquet(self.path, index=False)
            self._dirty = 0
        except Exception as exc:
            log.warning(f"[{self.symbol}] bar cache not saved: {exc}")

    def close(self):
        self._save_cache(force=True)


def bucket(t: pd.Timestamp, tf: int) -> pd.Timestamp:
    """Clock-aligned chart-bar open (UTC minute grid, as research chart_bars)."""
    minute = int((t - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(minutes=1))
    return pd.Timestamp((minute - minute % tf) * 60_000_000_000, tz="UTC")


def is_last_minute(t: pd.Timestamp, tf: int) -> bool:
    minute = int((t - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(minutes=1))
    return minute % tf == tf - 1
