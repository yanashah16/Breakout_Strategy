"""
Futures specs (desk/futures.json) and the front-month roll rule.

Every tradable product is one JSON entry: exchange, $ per point, tick, months, roll rule
and the `signal` market whose bars drive it (MES -> ES). Adding a product is a JSON edit.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

_PATH = Path(__file__).resolve().parent / "futures.json"
MONTH_CODES = "FGHJKMNQUVXZ"


@dataclass(frozen=True)
class Market:
    symbol: str
    exchange: str
    point_value: float
    tick: float
    months: str
    roll: str
    roll_days: int = 0
    signal: Optional[str] = None
    currency: str = "USD"
    name: str = ""
    ib_symbol: Optional[str] = None
    trading_class: Optional[str] = None

    @property
    def signal_symbol(self) -> str:
        return self.signal or self.symbol

    @property
    def broker_symbol(self) -> str:
        return self.ib_symbol or self.symbol

    @property
    def is_micro(self) -> bool:
        return bool(self.signal) and self.signal != self.symbol

    def round_tick(self, px: float) -> float:
        return round(math.floor(px / self.tick + 0.5) * self.tick, 10)

    def contract(self, expiry: str, con_id: Optional[int] = None) -> dict:
        c = {"symbol": self.broker_symbol, "sec_type": "FUT", "exchange": self.exchange,
             "currency": self.currency, "expiry": expiry}
        if self.trading_class:
            c["trading_class"] = self.trading_class
        if con_id:
            c["con_id"] = int(con_id)
        return c


_cache: Optional[Dict[str, Market]] = None


def all_markets() -> Dict[str, Market]:
    global _cache
    if _cache is None:
        raw = json.loads(_PATH.read_text(encoding="utf-8"))
        known = set(Market.__dataclass_fields__) - {"symbol"}
        _cache = {s.upper(): Market(symbol=s.upper(), **{k: v for k, v in d.items() if k in known})
                  for s, d in raw.items() if not s.startswith("_")}
    return _cache


def get(symbol: str) -> Market:
    m = all_markets().get(symbol.upper())
    if m is None:
        raise KeyError(f"{symbol}: not in desk/futures.json")
    return m


def third_friday(year: int, month: int) -> dt.date:
    d = dt.date(year, month, 15)
    return d + dt.timedelta(days=(4 - d.weekday()) % 7)


def roll_date(m: Market, year: int, month: int) -> dt.date:
    """First day the NEXT contract is traded instead of (year, month)."""
    if m.roll == "equity_index":
        return third_friday(year, month) - dt.timedelta(days=8)
    if m.roll == "month_before":
        return dt.date(year, month, 1) - dt.timedelta(days=int(m.roll_days))
    if m.roll == "energy":
        prev = dt.date(year, month, 1) - dt.timedelta(days=1)
        return dt.date(prev.year, prev.month, 25) - dt.timedelta(days=int(m.roll_days))
    raise ValueError(f"{m.symbol}: unknown roll rule {m.roll!r}")


def front_month(m: Market, today: dt.date) -> str:
    months = sorted(MONTH_CODES.index(c) + 1 for c in m.months)
    for year in (today.year, today.year + 1, today.year + 2):
        for mo in months:
            if today < roll_date(m, year, mo):
                return f"{year}{mo:02d}"
    raise ValueError(f"{m.symbol}: no contract month after {today}")


def last_hold_day(m: Market, year: int, month: int) -> dt.date:
    """Last day a position in (year, month) may still be held / opened: the expiry for equity index futures
    (they trade until the 3rd Friday), the day before first notice for metals / grains, the 3rd business
    day before the 25th of the prior month (last trade, approximately) for energy. After this day the
    month is not offered any more and an explicit choice of it falls back to the front month."""
    if m.roll == "equity_index":
        return third_friday(year, month) - dt.timedelta(days=1)       # expiry morning is not tradable all day
    if m.roll == "month_before":
        # physical delivery: IBKR closes positions out a few business days before first notice
        return dt.date(year, month, 1) - dt.timedelta(days=7)
    if m.roll == "energy":
        # last trade is ~3 business days before the 25th of the prior month; IBKR closes out earlier
        prev = dt.date(year, month, 1) - dt.timedelta(days=1)
        return dt.date(prev.year, prev.month, 25) - dt.timedelta(days=8)
    raise ValueError(f"{m.symbol}: unknown roll rule {m.roll!r}")


def expiries(m: Market, today: dt.date, n: int = 3) -> list:
    """The next n contract months that can still be held on `today` (YYYYMM, nearest first). Between the
    roll date and the expiry this includes the old front month, so a trade can stay in it on purpose."""
    months = sorted(MONTH_CODES.index(c) + 1 for c in m.months)
    out = []
    for year in (today.year, today.year + 1, today.year + 2, today.year + 3):
        for mo in months:
            if today <= last_hold_day(m, year, mo):
                out.append(f"{year}{mo:02d}")
                if len(out) >= n:
                    return out
    return out


def holdable(m: Market, expiry: str, today: dt.date) -> bool:
    return today <= last_hold_day(m, int(expiry[:4]), int(expiry[4:6]))


def label(symbol: str, expiry: str) -> str:
    """'MES', '202612' -> 'MESZ6'."""
    return f"{symbol}{MONTH_CODES[int(expiry[4:6]) - 1]}{expiry[3]}"
