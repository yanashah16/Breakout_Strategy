"""
Bars of one stock (regular hours 09:30-16:00 New York only, like the backtest's rth_only data).

Warm-up (hub /history, sec_type STK):
  * 5-minute bars, last 30 days      the indicators (ATR 150 / EMA 200 of 10m bars settle in ~20 sessions)
  * 1-minute bars, last 10 days      exact minutes (the price path inside a bar; restart catch-up)
  * daily bars, last 3 years         daily ATR(100) % and the trend ER (regular-hours days)
Live: GET /bars/{symbol} (1-minute stream); only completed minutes are used.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import List, Optional

import pandas as pd

from desk.bars import frame
from desk.stocks.copies import ib_symbol

log = logging.getLogger("desk.stocks")
NY = "America/New_York"


def rth(df: pd.DataFrame) -> pd.DataFrame:
    """Rows of 09:30-16:00 New York (bar open)."""
    if df.empty:
        return df
    t = df.t.dt.tz_convert(NY)
    m = t.dt.hour * 60 + t.dt.minute
    return df[(m >= 570) & (m < 960)].reset_index(drop=True)


def ny(t: pd.Timestamp) -> dt.datetime:
    return t.tz_convert(NY).tz_localize(None).to_pydatetime()


class StockFeed:
    SETTLE = 3.0                  # seconds into a minute before the previous one is read
    DELAY_WARN_MIN = 5.0          # newest minute this far behind the clock = delayed data
    RESUB_SECS = 60.0             # wait between re-subscribes of a stream the hub has lost

    def __init__(self, sym: str):
        self.sym = sym
        self.ib = ib_symbol(sym)
        self.m1 = frame([])       # completed minutes (5m rows for the older part), UTC, oldest first
        self.exact_from: Optional[pd.Timestamp] = None
        self.daily = pd.DataFrame()
        self.ready = False
        self.error = ""
        self.last_fetch: Optional[dt.datetime] = None
        self.current_price: Optional[float] = None
        self.con_id: Optional[int] = None
        self._next_sub = 0.0

    def contract(self) -> dict:
        c = {"symbol": self.ib, "sec_type": "STK", "exchange": "SMART", "currency": "USD"}
        if self.con_id:
            c["con_id"] = int(self.con_id)
        return c

    def _hist(self, hub, bar: str, dur: str) -> pd.DataFrame:
        for k in range(3):
            rows = hub.history(self.ib, "SMART", bar, dur, sec_type="STK", use_rth=True)
            if rows:
                return frame(rows)
            time.sleep(10 * (k + 1))
        return frame([])

    def warmup(self, hub) -> bool:
        m5 = rth(self._hist(hub, "5 mins", "30 D"))
        m1 = rth(self._hist(hub, "1 min", "10 D"))
        d1 = self._hist(hub, "1 day", "3 Y")
        if m5.empty or d1.empty:
            self.error = "no history from the hub"
            log.error(f"[{self.sym}] warm-up failed: 5m {len(m5)} rows, daily {len(d1)} rows")
            return False
        first1 = m1.t.min() if len(m1) else None
        older = m5[m5.t < first1] if first1 is not None else m5
        self.m1 = pd.concat([older, m1]).sort_values("t").drop_duplicates("t", keep="last").reset_index(drop=True)
        self.exact_from = first1
        # daily bars: IBKR stamps them with the date; keep completed days only (before today, New York)
        d1 = d1.copy()
        d1["date"] = d1.t.dt.tz_convert("UTC").dt.date     # midnight stamps: the UTC date is the trading day
        today = pd.Timestamp.now(tz=NY).date()
        self.daily = d1[d1.date < today][["date", "high", "low", "close"]].reset_index(drop=True)
        self.ready = True
        self.error = ""
        log.info(f"[{self.sym}] warm-up: {len(self.m1):,} rows ({len(m1):,} exact 1-minute), {len(self.daily)} days")
        return True

    def due(self, now_et: dt.datetime) -> bool:
        m = now_et.replace(second=0, microsecond=0)
        return self.last_fetch != m and now_et.second >= self.SETTLE

    def update(self, hub, now_utc: pd.Timestamp) -> List[pd.Series]:
        """New completed regular-hours minutes since the last call (oldest first)."""
        data = hub.bars(self.ib)
        if not data or not data.get("bars"):
            if time.monotonic() >= self._next_sub:          # at most once a minute, not on every 2 s tick
                self._next_sub = time.monotonic() + self.RESUB_SECS
                hub.subscribe_bars(self.ib, "SMART", sec_type="STK")
            return []
        self.last_fetch = now_utc.tz_convert(NY).tz_localize(None).to_pydatetime().replace(second=0, microsecond=0)
        if data.get("current_price"):
            self.current_price = float(data["current_price"])
        live = rth(frame(data["bars"]))
        live = live[live.t <= now_utc - pd.Timedelta(seconds=60 + self.SETTLE)]
        self._check_delay(live, now_utc)
        if live.empty:
            return []
        last = self.m1.t.iloc[-1] if len(self.m1) else None
        new = live if last is None else live[live.t > last]
        if len(new):
            self.m1 = pd.concat([self.m1, new]).sort_values("t").reset_index(drop=True)
            cut = self.m1.t.iloc[-1] - pd.Timedelta(days=40)
            self.m1 = self.m1[self.m1.t >= cut].reset_index(drop=True)
            if self.exact_from is None:
                self.exact_from = new.t.iloc[0]
        return [r for _, r in new.iterrows()]


    def _check_delay(self, live: pd.DataFrame, now_utc: pd.Timestamp):
        """Regular hours only: the newest completed minute should be the one that just ended. IBKR sends stock
        data 15 minutes late to an account without live US-stock data - say so instead of trading on it."""
        ny_now = now_utc.tz_convert(NY)
        m = ny_now.hour * 60 + ny_now.minute
        if ny_now.weekday() >= 5 or not 570 + self.DELAY_WARN_MIN <= m < 960:
            return
        if len(live):
            lag = (now_utc - live.t.iloc[-1]) / pd.Timedelta(minutes=1) - 1
        else:                                                    # no regular-hours minute at all yet today
            lag = m - 570 if self.m1.empty or self.m1.t.iloc[-1].tz_convert(NY).date() < ny_now.date() else None
        if lag is not None and lag >= self.DELAY_WARN_MIN:
            msg = (f"data {lag:.0f} min behind - IBKR is sending delayed prices (no live US-stock data for the API "
                   f"on this account?). No new trades until it is live.")
            if not self.error.startswith("data "):
                log.warning(f"[{self.sym}] {msg}")
            self.error = msg
        elif self.error.startswith("data "):
            log.info(f"[{self.sym}] data is live again")
            self.error = ""


def bucket(t: pd.Timestamp, tf: int) -> pd.Timestamp:
    """Clock-aligned chart-bar open (UTC minute grid, as research chart_bars): 09:30, 09:40 ... / 09:30, 09:45 ..."""
    minute = int((t - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(minutes=1))
    return pd.Timestamp((minute - minute % tf) * 60_000_000_000, tz="UTC")


def is_last_minute(t: pd.Timestamp, tf: int) -> bool:
    minute = int((t - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(minutes=1))
    return minute % tf == tf - 1
