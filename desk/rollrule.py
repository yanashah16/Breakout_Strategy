"""
Which contract month each instrument trades: the nearest month, until the next month trades more.

Rule (checked once per completed trading session, per instrument, on its OWN volume):
  * start / default: the nearest month that can still be held (markets.expiries()[0])
  * switch to the next month when its volume in the last completed session is at least
    `ratio` x the volume of the month traded now (ratio is one desk setting, default 1.2)
  * never switch back (a one-off volume blip after the switch cannot flip it again)
  * safety net: a month is never traded past its last hold day (markets.last_hold_day: expiry for
    equity index, before first notice for metals / grains, before last trade for energy), even if the
    volume data never arrives

State: state/contract_months.json (survives restarts). Settings: state/desk_settings.json.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Callable, Dict, Optional

import pandas as pd

from desk import markets as mkts
from desk.settings import STATE
from desk.store import read_json, write_json

log = logging.getLogger("desk.roll")
NY = "America/New_York"
MONTHS_FILE = STATE / "contract_months.json"
SETTINGS_FILE = STATE / "desk_settings.json"
DEFAULT_RATIO = 1.2
RETRY_SECS = 600


def trade_date(now_et: dt.datetime) -> dt.date:
    """CME trade date: the session that opens at 18:00 New York belongs to the next day."""
    return (now_et + dt.timedelta(hours=7)).date()


def load_settings() -> dict:
    s = read_json(SETTINGS_FILE) or {}
    s.setdefault("roll_volume_ratio", DEFAULT_RATIO)
    return s


def save_settings(s: dict):
    write_json(SETTINGS_FILE, s)


class RollBook:
    def __init__(self, hub, now_et: Callable[[], dt.datetime] = None):
        self.hub = hub
        self.now_et = now_et or (lambda: pd.Timestamp.now(tz=NY).tz_localize(None).to_pydatetime())
        self.state: Dict[str, dict] = read_json(MONTHS_FILE) or {}
        self.settings = load_settings()
        self._retry_at: Dict[str, float] = {}

    # ------------------------------------------------------------------ settings
    @property
    def ratio(self) -> float:
        return float(self.settings.get("roll_volume_ratio") or DEFAULT_RATIO)

    def set_ratio(self, r: float) -> float:
        r = round(float(r), 3)
        if not 1.0 <= r <= 5.0:
            raise ValueError("The volume ratio must be between 1.0 and 5.0 (1.2 = 20% more volume).")
        self.settings["roll_volume_ratio"] = r
        save_settings(self.settings)
        return r

    # ------------------------------------------------------------------ the month
    def month(self, sym: str, today: Optional[dt.date] = None) -> str:
        """The month `sym` trades now (never a month past its last hold day)."""
        spec = mkts.get(sym)
        today = today or self.now_et().date()
        nearest = mkts.expiries(spec, today, 1)[0]
        st = self.state.get(sym) or {}
        m = st.get("month")
        if not m or m < nearest or not mkts.holdable(spec, m, today):
            m = nearest
            if st.get("month") and st.get("month") != m:
                self._set(sym, m, f"{mkts.label(sym, st['month'])} too close to expiry", today)
        return m

    def _set(self, sym: str, month: str, why: str, today: dt.date):
        st = self.state.setdefault(sym, {})
        old = st.get("month")
        st.update(month=month, since=str(today), reason=why)
        st.setdefault("history", []).append({"date": str(today), "from": old, "to": month, "why": why})
        st["history"] = st["history"][-12:]
        write_json(MONTHS_FILE, self.state)
        if old and old != month:
            log.info(f"[{sym}] contract month {mkts.label(sym, old)} -> {mkts.label(sym, month)}: {why}")

    # ------------------------------------------------------------------ daily volume check
    def due(self, sym: str) -> bool:
        """Once per CME trade date (a new one starts at 17:00 New York), plus retries after a failure."""
        st = self.state.get(sym) or {}
        return (st.get("checked_on") != str(trade_date(self.now_et()))
                and time.monotonic() >= self._retry_at.get(sym, 0.0))

    def _volume(self, sym: str, expiry: str) -> Optional[Dict[str, int]]:
        """{session date: volume} of one contract month (completed sessions only)."""
        spec = mkts.get(sym)
        res = self.hub.month_history(spec.broker_symbol, spec.exchange, "1 day", "5 D", expiry)
        if res is None or res.get("expiry") != expiry:
            return None                                   # no answer, or a hub without per-month history
        done = trade_date(self.now_et())
        out = {}
        for b in res.get("bars") or []:
            d = str(b.get("time", ""))[:10]
            if d and dt.date.fromisoformat(d) < done:
                out[d] = int(b.get("volume") or 0)
        return out

    def check(self, sym: str, force: bool = False) -> dict:
        """Once per completed session: does the next month trade `ratio` x the current one?"""
        if not force and not self.due(sym):
            return self.state.get(sym) or {}
        spec = mkts.get(sym)
        today = self.now_et().date()
        cur = self.month(sym, today)
        later = [m for m in mkts.expiries(spec, today, 3) if m > cur]
        st = self.state.setdefault(sym, {"month": cur})
        st.setdefault("month", cur)
        if not later:
            return st
        nxt = later[0]
        vc, vn = self._volume(sym, cur), self._volume(sym, nxt)
        if vc is None or vn is None:
            st["error"] = "volume not available from the hub - trying again in 10 minutes"
            self._retry_at[sym] = time.monotonic() + RETRY_SECS
            write_json(MONTHS_FILE, self.state)
            return st
        sess = max(set(vc) & set(vn)) if set(vc) & set(vn) else None
        if sess is None:
            st["error"] = "no completed session with volume for both months yet"
            self._retry_at[sym] = time.monotonic() + RETRY_SECS
            write_json(MONTHS_FILE, self.state)
            return st
        a, b = vc[sess], vn[sess]
        st.update(checked=dt.datetime.now().isoformat(timespec="seconds"), checked_on=str(trade_date(self.now_et())),
                  session=sess, volumes={cur: a, nxt: b}, next=nxt, ratio=self.ratio, error="",
                  share=round(b / a, 3) if a else None)
        if a > 0 and b >= self.ratio * a:
            self._set(sym, nxt, f"{mkts.label(sym, nxt)} traded {b:,} vs {a:,} on {sess} "
                                f"({b / a:.2f}x >= {self.ratio:g}x)", today)
        else:
            write_json(MONTHS_FILE, self.state)
        return st

    def status(self, sym: str) -> dict:
        today = self.now_et().date()
        st = dict(self.state.get(sym) or {})
        m = self.month(sym, today)
        spec = mkts.get(sym)
        st.update(symbol=sym, month=m, label=mkts.label(sym, m),
                  hold_until=str(mkts.last_hold_day(spec, int(m[:4]), int(m[4:6]))), ratio=self.ratio)
        if st.get("next"):
            st["next_label"] = mkts.label(sym, st["next"])
        return st
