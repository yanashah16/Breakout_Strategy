"""
Trade: the exit rules of one open trade, a line-by-line port of research manage().

Two legs: leg 1 takes profit at TP1, leg 2 is the runner (break-even after TP1, trailing stop,
fixed TP2). Pure state machine - no orders, no I/O - so it can be replayed against the backtest.

Timing, as in the backtest:
  * every completed 1-minute candle: stops first, then TP1, then TP2; THEN break-even / trailing
    updates, which count from the next minute;
  * every live price in between: the same stop / target checks on the price (so a level is acted
    on at once, not a minute later). A live price never moves a stop - only completed minutes do.
  * the minute the entry filled is checked on live prices only: its range may come from before the
    fill (the backtest assumes the worst case there).
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from desk.logic.base import ExitRules

NAN = float("nan")
REASON = {"stop": "Stop loss", "be": "Break-even stop", "trail": "Trailing stop", "tp1": "Profit target",
          "tp2": "Final target", "time": "Time limit", "flat": "End-of-day close", "manual": "Closed by you",
          "past_stop": "Entry filled past the stop",
          "lock": "Profit-lock stop", "tp3": "Final target", "ema": "Trend turned (EMA close)",
          "hold": "Max hold time", "shutdown": "Strategy stopped"}
STOP_REASON = {0: "stop", 1: "be", 2: "trail"}


@dataclass
class Exit:
    leg: int                 # 1 or 2
    reason: str              # key of REASON
    level: float             # rule price (stop / target level, or the price for time exits)
    time: str = ""


@dataclass
class Trade:
    # replay only: check the stop on the fill minute's whole range, as the backtest does (live = prices)
    STRICT_ENTRY_MINUTE = False

    side: int
    entry: float
    stop0: float
    q: List[int]                          # contracts per leg [leg1, leg2]
    tp_mode: str = "r"
    tp_r: float = 0.0
    tp_dist: float = 0.0
    tp_price: float = NAN
    tp2_r: float = 0.0
    trail_r: float = 0.0
    entry_minute: str = ""                # ISO UTC of the minute the entry triggered
    lock_px: float = 0.0                  # extra runner lock after TP1, in price (see _lock)
    risk: float = 0.0
    open: List[bool] = field(default_factory=lambda: [True, True])
    stop: List[float] = field(default_factory=lambda: [NAN, NAN])
    kind: List[int] = field(default_factory=lambda: [0, 0])     # 0 initial, 1 break-even, 2 trailing
    tp1: float = NAN
    tp2: float = NAN
    best: float = NAN
    tp1_done: bool = False
    bars_closed: int = 0
    exits: List[dict] = field(default_factory=list)

    # ------------------------------------------------------------------ setup
    def __post_init__(self):
        if not self.exits and math.isnan(self.tp1):
            self.open = [self.q[0] > 0, self.q[1] > 0]
            self.stop = [self.stop0, self.stop0]
            self.set_entry(self.entry)

    def set_entry(self, px: float):
        """Levels measured from the entry price (called again with the real fill)."""
        sd = self.side
        self.entry = float(px)
        self.risk = (self.entry - self.stop0) * sd
        r = self.risk
        if self.tp_mode == "dist" and not math.isnan(self.tp_dist):
            d1 = self.tp_dist
        elif self.tp_mode == "price" and not math.isnan(self.tp_price):
            d1 = max(sd * (self.tp_price - self.entry), 0.0)
        else:
            d1 = self.tp_r * r
        self.tp1 = self.entry + sd * d1
        self.tp2 = self.entry + sd * self.tp2_r * r if self.tp2_r > 0 else NAN
        self.best = self.entry

    def shift(self, d: float):
        """Every price level moves by d (the bar series changed contract month); distances stay."""
        self.entry += d
        self.stop0 += d
        self.stop = [s + d for s in self.stop]
        self.tp1 += d
        if not math.isnan(self.tp2):
            self.tp2 += d
        if not math.isnan(self.best):
            self.best += d
        if not math.isnan(self.tp_price):
            self.tp_price += d

    # ------------------------------------------------------------------ state
    @property
    def is_open(self) -> bool:
        return any(self.open)

    def open_qty(self) -> int:
        return sum(q for q, o in zip(self.q, self.open) if o)

    def target_position(self) -> int:
        return self.side * self.open_qty()

    def _out(self, leg: int, reason: str, level: float, when: str) -> Exit:
        self.open[leg - 1] = False
        e = Exit(leg, reason, float(level), when)
        self.exits.append(asdict(e))
        return e

    def close_all(self, reason: str, level: float, when: str = "") -> List[Exit]:
        return [self._out(k, reason, level, when) for k in (1, 2) if self.open[k - 1]]

    # ------------------------------------------------------------------ live price
    def on_price(self, px: float, when: str = "") -> List[Exit]:
        if not self.is_open or px is None or math.isnan(px):
            return []
        sd = self.side
        out = []
        if self.risk <= 0:
            return self.close_all("past_stop", px, when)
        for k in (1, 2):
            if self.open[k - 1] and sd * (px - self.stop[k - 1]) <= 0:
                out.append(self._out(k, STOP_REASON[self.kind[k - 1]], self.stop[k - 1], when))
        if not self.tp1_done and sd * (px - self.tp1) >= 0:
            if self.open[0]:
                out.append(self._out(1, "tp1", self.tp1, when))
            self.tp1_done = True                 # the runner's break-even waits for the minute's end
            self._tp1_this_minute = True
        if self.open[1] and not math.isnan(self.tp2) and sd * (px - self.tp2) >= 0:
            out.append(self._out(2, "tp2", self.tp2, when))
        return out

    def _lock(self, rules: ExitRules) -> None:
        """be_after_tp1: runner stop to entry + tp1_lock_r x R + lock_px (research manage(); 0 + 0 = break-even)."""
        sd = self.side
        lk = self.entry + sd * (float(getattr(rules, "tp1_lock_r", 0.0) or 0.0) * self.risk + self.lock_px)
        if rules.be_after_tp1 and self.open[1] and sd * (lk - self.stop[1]) > 0:
            self.stop[1] = lk; self.kind[1] = 1

    # ------------------------------------------------------------------ completed minute
    def on_minute(self, o: float, h: float, l: float, c: float, minute: str, rules: ExitRules) -> List[Exit]:
        """research manage() for one 1-minute row."""
        tp1_by_price = getattr(self, "_tp1_this_minute", False)
        self._tp1_this_minute = False
        if not self.is_open:
            return []
        sd = self.side
        entry, risk = self.entry, self.risk
        out = []
        entry_min = minute == self.entry_minute
        if not entry_min:
            for k in (1, 2):
                if not self.open[k - 1]:
                    continue
                st = self.stop[k - 1]
                if (sd > 0 and l <= st) or (sd < 0 and h >= st):
                    out.append(self._out(k, STOP_REASON[self.kind[k - 1]], st, minute))
            hit = (sd > 0 and h >= self.tp1) or (sd < 0 and l <= self.tp1)
            if (not self.tp1_done and hit) or tp1_by_price:
                if self.open[0] and hit:
                    out.append(self._out(1, "tp1", self.tp1, minute))
                self.tp1_done = True
                self._lock(rules)
            if self.open[1] and not math.isnan(self.tp2) and ((sd > 0 and h >= self.tp2) or (sd < 0 and l <= self.tp2)):
                out.append(self._out(2, "tp2", self.tp2, minute))
        else:
            if Trade.STRICT_ENTRY_MINUTE and ((sd > 0 and l <= self.stop0) or (sd < 0 and h >= self.stop0)):
                return out + self.close_all("stop", self.stop0, minute)
            if tp1_by_price:
                self._lock(rules)
        if not self.is_open or entry_min:
            return out                            # the fill minute moves nothing (backtest: manage from m + 1)
        fav = h if sd > 0 else l
        if sd * (fav - self.best) > 0:
            self.best = fav
        move = sd * (self.best - entry)
        if rules.be_r > 0 and move >= rules.be_r * risk:
            for k in (0, 1):
                if sd * (entry - self.stop[k]) > 0:
                    self.stop[k] = entry; self.kind[k] = 1
        if rules.runner_be_r > 0 and self.open[1] and move >= rules.runner_be_r * risk and sd * (entry - self.stop[1]) > 0:
            self.stop[1] = entry; self.kind[1] = 1
        ts = rules.trail_start_r
        if rules.trail and self.open[1] and ((ts < 0 and self.tp1_done) or (ts >= 0 and move >= ts * risk)):
            if rules.trail_pct > 0:
                new = entry + sd * move * (1.0 - rules.trail_pct / 100.0)
            else:
                new = self.best - sd * self.trail_r * risk
            if sd * (new - self.stop[1]) > 0:
                self.stop[1] = new; self.kind[1] = 2
        return out

    # ------------------------------------------------------------------ chart bar close
    def on_bar_close(self, close: float, flat: bool, rules: ExitRules, when: str = "") -> List[Exit]:
        if not self.is_open:
            return []
        self.bars_closed += 1                     # the entry bar's own close = 1
        if flat:
            return self.close_all("flat", close, when)
        if rules.max_bars > 0 and self.bars_closed - 1 >= rules.max_bars:
            return self.close_all("time", close, when)
        return []

    # ------------------------------------------------------------------ persistence
    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @staticmethod
    def from_dict(d: dict) -> "Trade":
        return Trade(**{k: d[k] for k in Trade.__dataclass_fields__ if k in d})

    def r_now(self, px: Optional[float]) -> float:
        if px is None or self.risk <= 0:
            return NAN
        return self.side * (px - self.entry) / self.risk
