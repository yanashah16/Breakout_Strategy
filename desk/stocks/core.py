"""
Box breakout for US stocks - the rules with no I/O: bars and prices go in, a target position comes out.

A port of nq_wae_backtest/box/box_backtest.py simulate() for the settings the stock strategy uses
(ATR stop, 3 legs, leg 2 EMA exit, leg 3 trail, break-even lock, reverse, volume tiers, daily ATR %
and trend ER filters, skip window, stall exit, $ risk sizing). Its structure follows the futures port
strategy_manager/strategies/box_break/core.py. tests/test_stocks_replay.py feeds the backtest's own
bars through this class and compares every leg with the backtest - run it after any change here.

Feed, per stock and copy:
    warmup(bar)            completed chart bars before the strategy trades (indicators only)
    add_day(date, h, l, c) completed daily bars (regular hours) for the daily filters
    on_tick(price, ...)    live prices inside a minute: targets / stop / reverse fire at once
    on_minute(bar, ...)    a completed 1-minute bar: the O -> nearer extreme -> other -> C path, then the
                           stop moves (TP1 -> break-even, BE lock, trailing), which count from the next minute
    on_bar_close(bar, ...) the completed chart bar: leg 2 EMA exit, stall exit, new breakout
Levels are anchored like the backtest: a breakout's stop / targets from the signal bar's close, a
reverse's from the stop level that turned it. A different real fill changes the P&L, never the levels.
"""
from __future__ import annotations

import bisect
import datetime as dt
import math
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

NAN = float("nan")

DEFAULTS = {
    "timeframe": 10, "box_bars": 20, "max_box_atr": 10, "box_atr_len": 150, "break_buffer": 1.4, "atr_len": 14,
    "ema_len": 200, "direction": "Both", "reverse": True, "reverse_bars": 10,
    "sl_box_pct": -1.5, "short_sl_box_pct": None,            # < 0 = x ATR(atr_len); > 0 = % of the box height
    "tp1_mult": 7, "leg2_ema_len": 200, "tp3_mult": 80, "trail_pct": 75,
    "short_tp1_mult": 3, "short_leg2_ema_len": 30, "short_tp3_mult": 10, "short_trail_pct": 50,
    "qty1": 1, "qty2": 1, "qty3": 4,
    "skip_hours": "15:30-16:00", "min_squeeze": 0.9, "max_trend_er": 0.37, "er_days": 20,
    "atrp_min": 1.95, "atrp_len": 100,
    "be_mult": 2.5, "be_lock_r": 0.1,
    "risk_trade_usd": 1000, "size_atr_floor": 1, "max_shares": 1_000_000,
    "capital_usd": None, "capital_over_pct": 5,       # set = fixed $ amount per trade instead of risk sizing (live)
    "tvol_days": 20, "vq_t1": 2, "vq_m1": 1.5, "vq_t2": 3, "vq_m2": 2,
    "stall_age_h": 120, "stall_hours": 6, "stall_pct": 1.0, "stall_max_r": 1,
}
REASON = {"tp1": "Profit target 1", "tp3": "Final target", "stop": "Stop loss", "be": "Break-even stop",
          "tp1_be": "Stop at entry (after target 1)", "trail": "Trailing stop", "ema": "Trend turned (EMA close)",
          "stall": "Stall exit (quiet for 6 h)", "manual": "Closed by you", "shutdown": "Strategy stopped"}
STOP_REASON = {0: "stop", 1: "tp1_be", 2: "trail"}
RTH = (9 * 60 + 30, 16 * 60)


def _off(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or (isinstance(v, str) and v.strip().lower() in ("", "null", "none"))


def _num(v) -> float:
    return NAN if _off(v) else float(v)


def _hhmm(s: str) -> int:
    h, m = str(s).strip().split(":")
    return int(h) * 60 + int(m)


class Params:
    """One copy's settings (the backtest names); nan = off."""

    def __init__(self, raw: dict):
        unknown = set(raw) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"unknown setting(s): {sorted(unknown)}")
        p = {**DEFAULTS, **raw}
        self.raw = p
        self.tf = int(p["timeframe"])
        self.box_bars, self.box_atr_len, self.atr_len = int(p["box_bars"]), int(p["box_atr_len"]), int(p["atr_len"])
        self.max_box, self.buf = _num(p["max_box_atr"]), float(p["break_buffer"])
        self.ema_len = None if _off(p["ema_len"]) else int(p["ema_len"])
        d = str(p["direction"]).lower()
        self.allow_long, self.allow_short = d in ("both", "long only"), d in ("both", "short only")
        self.rev, self.rev_bars = bool(p["reverse"]), int(p["reverse_bars"])
        self.sl, self.s_sl = float(p["sl_box_pct"]), _num(p["short_sl_box_pct"])
        self.tp1, self.tp3, self.trail, self.il2 = float(p["tp1_mult"]), float(p["tp3_mult"]), float(p["trail_pct"]), int(p["leg2_ema_len"])
        self.s_tp1, self.s_tp3, self.s_trail = _num(p["short_tp1_mult"]), _num(p["short_tp3_mult"]), _num(p["short_trail_pct"])
        self.s_il2 = None if _off(p["short_leg2_ema_len"]) else int(p["short_leg2_ema_len"])
        self.qty = (int(p["qty1"]), int(p["qty2"]), int(p["qty3"]))
        self.skip = None if _off(p["skip_hours"]) else tuple(_hhmm(x) for x in str(p["skip_hours"]).split("-"))
        self.min_sq, self.max_er, self.er_days = _num(p["min_squeeze"]), _num(p["max_trend_er"]), int(p["er_days"])
        self.atrp_min, self.atrp_len = _num(p["atrp_min"]), int(p["atrp_len"])
        self.be, self.be_lock = _num(p["be_mult"]), _num(p["be_lock_r"])
        self.risk, self.floor, self.max_shares = float(p["risk_trade_usd"]), _num(p["size_atr_floor"]), int(p["max_shares"])
        self.capital, self.capital_over = _num(p["capital_usd"]), float(p["capital_over_pct"])
        self.tvol_days = int(p["tvol_days"])
        self.vq_t1, self.vq_m1, self.vq_t2, self.vq_m2 = _num(p["vq_t1"]), float(p["vq_m1"]), _num(p["vq_t2"]), float(p["vq_m2"])
        self.st_age, self.st_hours, self.st_pct, self.st_mr = (_num(p["stall_age_h"]), float(p["stall_hours"]),
                                                               float(p["stall_pct"]), _num(p["stall_max_r"]))
        self.st_n = max(1, int(round(self.st_hours * 60 / self.tf)))
        if not self.tp1 < self.tp3:
            raise ValueError("targets must rise: tp1_mult < tp3_mult")

    def emas(self) -> set:
        s = {self.il2, self.s_il2 if self.s_il2 else self.il2}
        if self.ema_len:
            s.add(self.ema_len)
        return s

    def exits(self, d: int):
        """TP1, TP3, trail %, leg 2 EMA length of side d."""
        if d > 0:
            return self.tp1, self.tp3, self.trail, self.il2
        return (self.tp1 if math.isnan(self.s_tp1) else self.s_tp1, self.tp3 if math.isnan(self.s_tp3) else self.s_tp3,
                self.trail if math.isnan(self.s_trail) else self.s_trail, self.s_il2 if self.s_il2 else self.il2)

    def sl_for(self, d: int) -> float:
        return self.s_sl if d < 0 and not math.isnan(self.s_sl) else self.sl


# ---------------------------------------------------------------------- indicators (= the backtest's)
class Atr:
    """ta.atr: RMA of the true range seeded with the SMA of the first n values (backtest atr())."""

    def __init__(self, n: int):
        self.n, self.k, self.s, self.value, self.pc = n, 0, 0.0, NAN, NAN

    def update(self, h: float, l: float, c: float) -> float:
        tr = h - l if math.isnan(self.pc) else max(h - l, abs(h - self.pc), abs(l - self.pc))
        self.pc = c
        self.k += 1
        if self.k < self.n:
            self.s += tr
        elif self.k == self.n:
            self.value = (self.s + tr) / self.n
        else:
            self.value = (self.value * (self.n - 1) + tr) / self.n
        return self.value


class Ema:
    """ta.ema seeded with the SMA of the first n closes (backtest ema())."""

    def __init__(self, n: int):
        self.n, self.k, self.s, self.value, self.a = n, 0, 0.0, NAN, 2.0 / (n + 1)

    def update(self, c: float) -> float:
        self.k += 1
        if self.k < self.n:
            self.s += c
        elif self.k == self.n:
            self.value = (self.s + c) / self.n
        else:
            self.value = self.a * c + (1 - self.a) * self.value
        return self.value


class Daily:
    """Completed regular-hours days: ATR(n) % of the close and the trend ER of the closes (backtest Frames "D" /
    trend_er: the value of the last completed day BEFORE the bar's day)."""

    def __init__(self, atr_len: int, er_days: int):
        self.atr = Atr(atr_len)
        self.er_days = er_days
        self.dates: List[dt.date] = []
        self.closes: deque = deque(maxlen=er_days + 1)
        self.atrp: Dict[dt.date, float] = {}
        self.er: Dict[dt.date, float] = {}

    def add(self, d: dt.date, h: float, l: float, c: float):
        if self.dates and d <= self.dates[-1]:
            return
        self.dates.append(d)
        a = self.atr.update(h, l, c)
        self.atrp[d] = a / c * 100 if not math.isnan(a) and c > 0 else NAN
        self.closes.append(c)
        er = NAN
        if len(self.closes) == self.er_days + 1:
            cl = list(self.closes)
            ch = sum(abs(cl[i] - cl[i - 1]) for i in range(1, len(cl)))
            er = abs(cl[-1] - cl[0]) / ch if ch > 0 else NAN
        self.er[d] = er

    def before(self, d: dt.date):
        """(ATR %, ER) of the last completed day before d (nan when unknown)."""
        i = bisect.bisect_left(self.dates, d) - 1
        if i < 0:
            return NAN, NAN
        k = self.dates[i]
        return self.atrp.get(k, NAN), self.er.get(k, NAN)


@dataclass
class Bar:
    t: dt.datetime            # bar OPEN, New York time (naive)
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


@dataclass
class Position:
    side: int = 0
    entry: float = 0.0             # rule entry (signal close / reverse stop level)
    entry_time: str = ""
    signal_bar: str = ""           # open time of the breakout bar (ISO) - stall age counts from it
    unit: float = 0.0              # 1 R in $ per share
    atr: float = 0.0
    height: float = 0.0            # box height of the breakout
    stop: float = 0.0
    tps: list = field(default_factory=lambda: [0.0, 0.0])      # TP1, TP3
    stage: int = 0                 # 0 initial stop, 1 TP1 reached, 2 leg 3 trailing
    best: float = 0.0
    ext: float = 0.0
    legs: list = field(default_factory=lambda: [0, 0, 0])      # open shares per leg
    sizes: list = field(default_factory=lambda: [0, 0, 0])     # shares per leg at the entry (reverse keeps them)
    is_rev: bool = False
    be_on: bool = False
    bars_since: int = 0
    boost: str = ""

    def open_qty(self) -> int:
        return sum(self.legs)


@dataclass
class Event:
    """Something that changes the position (for the order layer and the trade log)."""
    kind: str                 # "entry" | "exit" | "flip"
    side: int                 # side of the position it applies to (flip: the NEW side)
    qty: int
    px: float                 # rule price (what the backtest fills at)
    legs: tuple = ()
    reason: str = ""


class StockBoxCore:
    def __init__(self, params: Params, tick: float = 0.01):
        p = self.p = params
        self.tk = tick
        self.atr, self.batr = Atr(p.atr_len), Atr(p.box_atr_len)
        self.emas = {n: Ema(n) for n in p.emas()}
        self.daily = Daily(p.atrp_len, p.er_days)
        self.hist_h: deque = deque(maxlen=p.box_bars)
        self.hist_l: deque = deque(maxlen=p.box_bars)
        self.rng_h: deque = deque(maxlen=p.st_n)
        self.rng_l: deque = deque(maxlen=p.st_n)
        self.vol: Dict[int, deque] = {}           # session slot -> volumes of the previous sessions
        self.prev_top = self.prev_bot = self.prev_c = NAN
        self.prev_xa = NAN
        self.nbars = 0
        self.pos = Position()
        self._reached = [False, False]
        self._fav = None
        self._minute = None
        self._flip_minute = None
        self.closed: list = []                    # rule exits per leg (replay / log)
        self.last: dict = {}                      # the last bar's numbers and why it did / did not trade

    # ------------------------------------------------------------------ helpers
    def _rt(self, x: float) -> float:
        return max(1.0, math.floor(x / self.tk + 0.5)) * self.tk

    def _levels(self, d: int, anchor: float):
        ps = self.pos
        t1, t3, _, _ = self.p.exits(d)
        ps.tps = [anchor + d * self._rt(t1 * ps.unit), anchor + d * self._rt(t3 * ps.unit)]
        ps.stop = anchor - d * self._rt(ps.unit)

    def hours_ok(self, t: dt.datetime) -> bool:
        if not self.p.skip:
            return True
        a, b = self.p.skip
        m = t.hour * 60 + t.minute
        return not ((a <= m < b) if a < b else (m >= a or m < b))

    def _book(self, k: int, px: float, why: str, now: str):
        ps = self.pos
        self.closed.append({"entry_time": ps.entry_time, "exit_time": now, "side": ps.side, "leg": k + 1,
                            "entry_price": ps.entry, "exit_price": px, "shares": ps.legs[k], "exit": why,
                            "setup": "Reverse" if ps.is_rev else "Breakout"})
        ps.legs[k] = 0

    # ------------------------------------------------------------------ price path inside a minute
    def _path(self, o: float, h: float, l: float, now: str) -> List[Event]:
        """Targets / stop on o -> nearer extreme -> other extreme (backtest order). No stop moves here."""
        ps, out = self.pos, []
        d = ps.side
        fav, adv = (h, l) if d > 0 else (l, h)
        fav_first = (h - o <= o - l) if d > 0 else not (h - o <= o - l)
        ps.ext = max(ps.ext, fav) if d > 0 else min(ps.ext, fav)
        self._fav = fav if self._fav is None else (max(self._fav, fav) if d > 0 else min(self._fav, fav))
        stopped, stop_px, stop_stage = False, 0.0, 0
        for step in range(2):
            if (step == 0) == fav_first:                       # favourable part: TP1 (leg 1), TP3 (leg 3)
                for j, k in ((0, 0), (1, 2)):
                    if d * (fav - ps.tps[j]) >= 0:
                        if j == 0:
                            self._reached[0] = True
                        if ps.legs[k]:
                            px = o if d * (o - ps.tps[j]) >= 0 else ps.tps[j]
                            why = "tp1" if j == 0 else "tp3"
                            out.append(Event("exit", d, ps.legs[k], px, (k,), why))
                            self._book(k, px, why, now)
            elif d * (adv - ps.stop) <= 0 and ps.open_qty():   # adverse part: the stop for every open leg
                px = o if d * (o - ps.stop) <= 0 else ps.stop
                why = "be" if ps.be_on and ps.stage == 0 else STOP_REASON[ps.stage]
                legs = tuple(k for k in range(3) if ps.legs[k])
                qty = ps.open_qty()
                for k in legs:
                    self._book(k, px, why, now)
                stopped, stop_px = True, px
                stop_stage = 1 if ps.be_on and ps.stage == 0 else ps.stage
                out.append(Event("exit", d, qty, px, legs, why))
        if ps.open_qty() == 0:
            # reverse: the ORIGINAL stop hit within reverse_bars bars -> turn around at the same price, once
            if (self.p.rev and stopped and stop_stage == 0 and not self._reached[0] and not ps.is_rev
                    and ps.bars_since + 1 <= self.p.rev_bars):
                closing = out.pop()
                self._flip(-d, stop_px, now)
                out.append(Event("flip", -d, closing.qty + self.pos.open_qty(), stop_px, closing.legs, closing.reason))
                return out
            self.pos = Position()
        return out

    def _flip(self, d: int, px: float, now: str):
        old = self.pos
        unit = old.unit
        if not math.isnan(self.p.s_sl):                        # own stop per side (not used by the stock copies)
            pct = self.p.sl_for(d)
            unit = self._rt(-pct * old.atr if pct < 0 else pct / 100.0 * old.height)
        ps = Position(side=d, entry=px, entry_time=now, signal_bar=old.signal_bar, unit=unit, atr=old.atr, height=old.height,
                      legs=list(old.sizes), sizes=list(old.sizes), is_rev=True, bars_since=old.bars_since,
                      boost=old.boost)
        ps.ext = px
        self.pos = ps
        self._levels(d, px)
        self._reached = [False, False]
        self._fav = None
        self._flip_minute = self._minute

    # ------------------------------------------------------------------ public feed
    def on_tick(self, price: float, minute: Optional[dt.datetime], prev: Optional[float], now: str = "") -> List[Event]:
        """A live price; prev -> price is handled as a tiny bar (a level crossed on the way fills AT the level)."""
        if self.pos.side == 0 or price is None or not price > 0:
            return []
        self._minute = minute
        o = prev if prev else price
        return self._path(o, max(o, price), min(o, price), now)

    def on_minute(self, m: Bar, now: str = "") -> List[Event]:
        out: List[Event] = []
        fresh = self._flip_minute is not None and m.t <= self._flip_minute
        if self.pos.side != 0 and not fresh:
            self._minute = m.t
            out = self._path(m.o, m.h, m.l, now)
            if self.pos.side != 0 and self._flip_minute != m.t:
                self._move_stop()
        self._reached = [False, False]
        self._fav = None
        return out

    def _move_stop(self):
        """Stop moves after a minute, effective from the next one (backtest order)."""
        ps, p = self.pos, self.p
        d, fav = ps.side, self._fav
        _, _, x_trail, _ = p.exits(d)
        if self._reached[0] and ps.stage == 0:
            ps.stage = 1
            ps.stop = max(ps.stop, ps.entry) if d > 0 else min(ps.stop, ps.entry)
        if fav is None:
            return
        if ps.stage == 0 and not ps.be_on and not math.isnan(p.be) and d * (fav - ps.entry) >= p.be * ps.unit:
            ps.be_on = True
            ps.stop = ps.entry + d * (0.0 if math.isnan(p.be_lock) else p.be_lock) * ps.unit
        if ps.stage == 2:
            ps.best = max(ps.best, fav) if d > 0 else min(ps.best, fav)
            trail = ps.best - d * x_trail / 100.0 * abs(ps.best - ps.entry)
            ps.stop = max(ps.stop, trail) if d > 0 else min(ps.stop, trail)

    # ------------------------------------------------------------------ chart bars
    def add_day(self, d: dt.date, h: float, l: float, c: float):
        self.daily.add(d, h, l, c)

    @staticmethod
    def slot(t: dt.datetime, tf: int) -> int:
        """Session slot of a bar (backtest tod_volume: minutes since 18:00 // tf)."""
        return ((t.hour * 60 + t.minute + 360) % 1440) // tf

    def _indicators(self, bar: Bar) -> dict:
        p = self.p
        full = len(self.hist_h) == p.box_bars
        top = max(self.hist_h) if full else NAN
        bot = min(self.hist_l) if full else NAN
        ba1, xa1 = self.batr.value, self.atr.value
        xa = self.atr.update(bar.h, bar.l, bar.c)
        ba = self.batr.update(bar.h, bar.l, bar.c)
        for e in self.emas.values():
            e.update(bar.c)
        q = self.vol.setdefault(self.slot(bar.t, p.tf), deque(maxlen=p.tvol_days))
        need = max(2, p.tvol_days // 2)
        tvol = bar.v / (sum(q) / len(q)) if len(q) >= need and sum(q) > 0 else NAN
        q.append(bar.v)
        self.rng_h.append(bar.h)
        self.rng_l.append(bar.l)
        rng = max(self.rng_h) - min(self.rng_l) if len(self.rng_h) == p.st_n else NAN
        s = {"top": top, "bot": bot, "prev_top": self.prev_top, "prev_bot": self.prev_bot, "prev_c": self.prev_c,
             "xa": xa, "xa1": xa1, "ba": ba, "ba1": ba1, "first": self.nbars == 0, "tvol": tvol, "rng": rng}
        self.prev_top, self.prev_bot, self.prev_c = top, bot, bar.c
        self.hist_h.append(bar.h)
        self.hist_l.append(bar.l)
        self.nbars += 1
        return s

    def warmup(self, bar: Bar):
        self._indicators(bar)

    def on_bar_close(self, bar: Bar, now: str = "", allow_entry: bool = True) -> List[Event]:
        """The completed chart bar (bar.t = its open): leg 2 EMA exit, stall exit, then a new breakout."""
        p, out = self.p, []
        s = self._indicators(bar)
        ps = self.pos
        c = bar.c
        if ps.side != 0:
            ps.bars_since += 1
            d = ps.side
            _, _, x_trail, x_il2 = p.exits(d)
            if ps.stage == 1 and d * (c - self.emas[x_il2].value) < 0:
                if ps.legs[1]:
                    out.append(Event("exit", d, ps.legs[1], c, (1,), "ema"))
                    self._book(1, c, "ema", now)
                ps.stage, ps.best = 2, ps.ext
                trail = ps.best - d * x_trail / 100.0 * abs(ps.best - ps.entry)
                ps.stop = max(ps.stop, trail) if d > 0 else min(ps.stop, trail)
                if ps.open_qty() == 0:
                    self.pos = Position()
        ps = self.pos
        if ps.side != 0 and not math.isnan(p.st_age) and not math.isnan(s["rng"]) and ps.signal_bar:
            age = (bar.t - dt.datetime.fromisoformat(ps.signal_bar)).total_seconds() / 3600
            if (age >= p.st_age and s["rng"] <= p.st_pct / 100.0 * c
                    and (math.isnan(p.st_mr) or ps.side * (c - ps.entry) < p.st_mr * ps.unit)):
                legs = tuple(k for k in range(3) if ps.legs[k])
                out.append(Event("exit", ps.side, ps.open_qty(), c, legs, "stall"))
                for k in legs:
                    self._book(k, c, "stall", now)
                self.pos = Position()
        if self.pos.side != 0:
            self.last = {"state": "in a trade", "at": bar.t.strftime("%H:%M")}
            return out
        why = self._entry(bar, s, now) if allow_entry else "switched off"
        if isinstance(why, Event):
            out.append(why)
        else:
            self.last = {"state": why, **self._view(bar, s)}
        return out

    def _view(self, bar: Bar, s: dict) -> dict:
        r = lambda v: None if v is None or (isinstance(v, float) and math.isnan(v)) else round(v, 4)
        hi, lo, xa = s["top"], s["bot"], s["xa"]
        out = {"at": bar.t.strftime("%H:%M"), "close": r(bar.c)}
        if not (math.isnan(hi) or math.isnan(xa)):
            out.update(top=r(hi), bottom=r(lo), long_above=r(hi + self.p.buf * xa), short_below=r(lo - self.p.buf * xa),
                       ema=r(self.emas[self.p.ema_len].value) if self.p.ema_len else None)
        return out

    def _entry(self, bar: Bar, s: dict, now: str):
        p = self.p
        if not self.hours_ok(bar.t):
            return "no new trades in the last half hour"
        if s["first"]:
            return "warming up"
        hi, lo, xa, xa1 = s["top"], s["bot"], s["xa"], s["xa1"]
        if math.isnan(hi) or math.isnan(xa) or math.isnan(xa1):
            return "warming up"
        height = hi - lo
        ba1 = s["ba1"]
        if not math.isnan(p.max_box) and (math.isnan(ba1) or height > p.max_box * ba1):
            return "no box (range too wide)"
        if not math.isnan(p.min_sq) and not xa >= p.min_sq * s["ba"]:
            return "quiet (no volatility expansion)"
        atrp, er = self.daily.before(bar.t.date())
        if not math.isnan(p.max_er) and not er <= p.max_er:
            return "daily trend too strong" if not math.isnan(er) else "no daily data yet"
        c = bar.c
        b, bp = p.buf * xa, p.buf * xa1
        d = 0
        if c > hi + b and not s["prev_c"] > s["prev_top"] + bp:
            d = 1
        elif c < lo - b and not s["prev_c"] < s["prev_bot"] - bp:
            d = -1
        if d == 0:
            return "inside the box"
        if (d > 0 and not p.allow_long) or (d < 0 and not p.allow_short):
            return "direction off"
        if not math.isnan(p.atrp_min) and not atrp >= p.atrp_min:
            return "stock too calm (daily ATR % below the minimum)" if not math.isnan(atrp) else "no daily data yet"
        if p.ema_len and not d * (c - self.emas[p.ema_len].value) > 0:
            return "against the EMA trend"
        pct = p.sl_for(d)
        unit = self._rt(-pct * xa if pct < 0 else pct / 100.0 * height)
        sumq = sum(p.qty)
        if not math.isnan(p.capital):
            # fixed amount per trade (live): shares per qty unit = amount / (price x units), rounded up when that
            # stays within amount + capital_over %, else down (legs stay in the 1 : 1 : 4 ratio)
            per = c * sumq
            down, up = int(math.floor(p.capital / per + 1e-9)), int(math.ceil(p.capital / per - 1e-9))
            mult = up if up * per <= p.capital * (1 + p.capital_over / 100.0) + 1e-9 else down
            if mult < 1:
                return f"amount too small (one share per unit costs ${per:,.0f})"
        else:
            sz = max(unit, p.floor * xa) if not math.isnan(p.floor) else unit
            mult = max(1, int(math.floor(p.risk / (sz * sumq) + 1e-9)))
        tv = s["tvol"]
        f23 = 1.0
        if not math.isnan(p.vq_t2) and not math.isnan(tv) and tv >= p.vq_t2:
            f23 = p.vq_m2
        elif not math.isnan(p.vq_t1) and not math.isnan(tv) and tv >= p.vq_t1:
            f23 = p.vq_m1
        sizes = [p.qty[0] * mult, int(round(p.qty[1] * mult * f23)), int(round(p.qty[2] * mult * f23))]
        cap = p.max_shares
        if sum(sizes) > cap:
            f = cap / sum(sizes)
            sizes = [int(math.floor(x * f)) for x in sizes]
        if sum(sizes) <= 0:
            return "size 0"
        ps = Position(side=d, entry=c, entry_time=now, signal_bar=bar.t.isoformat(), unit=unit, atr=xa, height=height,
                      legs=list(sizes), sizes=list(sizes), boost=f"volume {tv:.1f}x -> legs 2+3 x{f23:g}" if f23 != 1 else "")
        ps.ext = c
        self.pos = ps
        self._levels(d, c)
        self._reached = [False, False]
        self._fav = None
        self.last = {"state": "entered", **self._view(bar, s)}
        return Event("entry", d, ps.open_qty(), c)

    # ------------------------------------------------------------------ persistence / manual
    def target(self) -> int:
        return self.pos.side * self.pos.open_qty()

    def state(self) -> dict:
        return asdict(self.pos)

    def restore(self, d: Optional[dict]):
        self.pos = Position(**d) if d else Position()

    def close_all(self, why: str, px: float, now: str = "") -> List[Event]:
        ps = self.pos
        if ps.side == 0:
            return []
        legs = tuple(k for k in range(3) if ps.legs[k])
        ev = Event("exit", ps.side, ps.open_qty(), px, legs, why)
        for k in legs:
            self._book(k, px, why, now)
        self.pos = Position()
        return [ev]

    def scale_entry(self, filled: int):
        """Only part of the entry filled: keep what filled, leg 1 first."""
        ps = self.pos
        left = max(0, int(filled))
        new = []
        for k in range(3):
            q = min(ps.legs[k], left)
            new.append(q)
            left -= q
        ps.legs = new
        ps.sizes = list(new)
        if sum(new) == 0:
            self.pos = Position()
