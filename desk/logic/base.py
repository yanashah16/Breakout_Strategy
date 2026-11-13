"""
The contract between the platform and a strategy's logic.

A logic engine turns closed candles into entry PLANS and tells the platform how a trade is
managed (ExitRules). Everything else - orders, positions, persistence, the UI - is shared,
so a new kind of strategy is one new class registered with @register("name").
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Type

import pandas as pd


@dataclass
class ExitRules:
    """How a 2-leg trade is managed (the backtest's manage())."""
    be_r: float = 0.0              # whole position to break-even at x R (0 = off)
    be_after_tp1: bool = False     # runner stop to entry once TP1 is reached
    runner_be_r: float = 0.0       # runner stop to entry at x R (0 = off)
    trail: bool = False            # runner trails
    trail_start_r: float = -1.0    # < 0 = from TP1; >= 0 = once x R in profit
    trail_pct: float = 0.0         # > 0 = give back x % of the best profit instead of trail_r
    max_bars: int = 0              # close at the close of the n-th chart bar (0 = off)
    flat_time: Optional[str] = None


@dataclass
class Plan:
    """An entry the strategy wants.

    kind 'stop'   = enter when price trades through `level` during the entry bar: the first chart bar
                    that trades after the signal bar (after a session break that is the 18:00 bar).
                    Until that bar is known the plan is 'pending'; Logic.finalize() then sets the
                    stop / targets, which may depend on the entry bar (day vs night settings).
    kind 'market' = enter now, at the signal bar's close (finalized already).
    """
    side: int                      # +1 long, -1 short
    kind: str                      # "stop" | "market"
    level: float                   # trigger (stop) / reference price (market)
    anchor: str                    # pattern id: one trade per pattern
    signal_time: str               # chart bar that produced the plan (its open time, ISO, New York)
    after: str                     # entry bar starts at or after this time (ISO)
    stop_ref: float = float("nan")     # pattern stop level before the ATR buffer
    atr: float = float("nan")          # signal bar ATR
    target_ref: float = float("nan")   # pattern target (distance or price, engine specific)
    boost: bool = False            # volume boost -> boosted quantities
    # ---- set by finalize()
    final: bool = False
    entry_bar: str = ""            # open time of the entry bar (ISO)
    valid_until: str = ""          # end of the entry bar (ISO)
    session: int = 0               # CME session id of the entry bar (max trades per day)
    eth: bool = False              # entry bar outside the regular session
    stop: float = float("nan")     # initial stop of both legs
    tp_mode: str = "r"             # "r": tp1 = tp_r x R from the fill | "dist": tp_dist from the fill | "price": tp_price
    tp_r: float = 0.0
    tp_dist: float = 0.0
    tp_price: float = float("nan")
    tp2_r: float = 0.0             # 0 = no fixed runner target
    trail_r: float = 0.0           # runner trail distance in R
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Plan":
        return Plan(**{k: d[k] for k in Plan.__dataclass_fields__ if k in d})


@dataclass
class BarContext:
    """What a logic engine receives at a chart-bar close."""
    symbol: str                    # signal market (ES)
    m1: pd.DataFrame               # completed 1-minute bars: t (UTC, tz-aware), open, high, low, close, volume
    now_et: dt.datetime
    shared: Dict = field(default_factory=dict)   # per-minute scratch space shared by every strategy on `symbol`


class Logic:
    """Base class. Subclasses: timeframe, warmup_days, exit_rules, max_day_trades, on_bar_close."""
    timeframe: int = 5
    warmup_days: int = 30
    max_day_trades: int = 0

    def __init__(self, card: dict):
        self.card = card

    def exit_rules(self) -> ExitRules:
        raise NotImplementedError

    def on_bar_close(self, ctx: BarContext) -> List[Plan]:
        """Plans for the next bar (stop, pending) or for right now (market, final)."""
        raise NotImplementedError

    def finalize(self, plan: Plan, entry_bar: pd.Timestamp) -> bool:
        """Fix a pending stop plan to its entry bar (New York time). False = no entry in that bar."""
        raise NotImplementedError


_REGISTRY: Dict[str, Type[Logic]] = {}


def register(name: str):
    def deco(cls):
        _REGISTRY[name] = cls
        return cls
    return deco


def make(card: dict) -> Logic:
    name = card.get("engine", "patterns")
    if name not in _REGISTRY:
        raise KeyError(f"no logic engine {name!r} (known: {sorted(_REGISTRY)})")
    return _REGISTRY[name](card)
