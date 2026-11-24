"""
"box" logic engine: Box Breakout 3 Targets, box2 versions (nq_wae_backtest/box2/box/box_backtest.py).

Signals: at every chart-bar close the rolling window of 1-minute bars goes through the research code's own
indicator tables (research/box2_backtest.py build() / trend_er() / implied_vol() and research/filters.py, vendored
verbatim). plans_at() is simulate()'s entry block line by line for the bar that just closed. Entry = market order
at the breakout bar's close (the backtest fills at that close).

Exits: BoxTrade below ports simulate()'s position loop: 3 legs - leg 1 at TP1, leg 2 at the leg 2 EMA close (or at
TP2 when tp2_mult is set), leg 3 trailing (giving back trail_pct % of the best open profit) up to TP3; one stop for
every open leg (break-even after TP1); the stop-and-reverse when the ORIGINAL stop is hit within reverse_bars bars;
max_hold_hours. Levels are rounded to the tick and measured from the breakout close, like the backtest.

IV (iv_min / iv_max / f_iv_min): the CBOE index csv (VIX for YM / RTY / NKD, GVZ for SI), downloaded to data/iv/
and refreshed when a newer trading day is due - the same files the backtest reads.

Not supported live (refused when set): be_mult, flat_time, leg3_guard_r, short_sl_box_pct, short_tp2_mult, every risk
sizing (risk_usd / risk_trade_usd / risk_pct): the quantity boxes are the contracts (x size_up_mult on high-score entries).
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd

from desk.logic.base import BarContext, ExitRules, Logic, Plan, register
from desk.logic.patterns.adapter import _RESEARCH  # noqa: F401  (puts research/ on sys.path)
from desk.settings import DATA

import box2_backtest as B  # noqa: E402  (verbatim research code, see SOURCE_HASHES.txt)
from orb_backtest import bar_flags, chart_bars  # noqa: E402

log = logging.getLogger("desk.box")
NAN = float("nan")
NY = "America/New_York"
OFF_LIVE = ("be_mult", "flat_time", "leg3_guard_r", "short_sl_box_pct", "short_tp2_mult", "risk_usd",
            "risk_trade_usd", "risk_pct", "vol_pct", "min_box_atr", "ivr_min", "ivr_max")
REASON = {0: "tp1", 1: "tp2", 2: "tp3", 3: "stop", 4: "be", 5: "trail", 6: "flat", 8: "ema", 9: "hold"}
CBOE_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{}_History.csv"
IV_DIR = DATA / "iv"


def _clean(x):
    return None if isinstance(x, str) and x.strip().lower() in ("null", "none", "") else x


def full_params(logic: dict) -> dict:
    bad = [k for k in logic if k not in B.DEFAULTS]
    if bad:
        raise ValueError(f"unknown setting(s) {bad}")
    p = {k: _clean(logic.get(k, v)) for k, v in B.DEFAULTS.items()}
    for k in OFF_LIVE:
        if not B.is_off(p.get(k)):
            raise ValueError(f"{k} is not supported live")
    if not float(p["tp1_mult"]) < float(p["tp3_mult"]):
        raise ValueError("targets must rise: tp1_mult < tp3_mult")
    if p["box_edges"] not in ("close", "wick"):
        raise ValueError("box_edges must be close | wick")
    return p


def _iso(t) -> str:
    return pd.Timestamp(t).isoformat()


# =====================================================================================================  IV files
_IV_CHECKED: dict = {}


def iv_file(name: str) -> None:
    """Keep data/iv/<name>.csv (CBOE daily history) current: download when the last weekday before today is missing.
    At most once an hour; a failed download keeps the old file (the IV filter then reads the last close it has)."""
    path = IV_DIR / f"{name}.csv"
    need = dt.date.today() - dt.timedelta(days=1)
    while need.weekday() >= 5:
        need -= dt.timedelta(days=1)
    last = None
    if path.exists():
        try:
            tail = path.read_text(encoding="utf-8").strip().splitlines()[-1]
            last = dt.datetime.strptime(tail.split(",")[0].strip(), "%m/%d/%Y").date()
        except (ValueError, IndexError):
            last = None
    if last is not None and last >= need:
        return
    if time.time() - _IV_CHECKED.get(name, 0) < 3600:
        return
    _IV_CHECKED[name] = time.time()
    try:
        req = urllib.request.Request(CBOE_URL.format(name), headers={"User-Agent": "Mozilla/5.0"})
        text = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
        if text.startswith("DATE") and len(text) > 1000:
            IV_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            log.info(f"[IV] {name}: updated, last row {text.strip().splitlines()[-1][:10]}")
    except Exception as exc:
        log.warning(f"[IV] {name}: CBOE download failed ({exc}) - using the file on disk")


# =====================================================================================================  the trade
@dataclass
class BoxTrade:
    side: int
    entry: float                         # the real fill (break-even / trail measure from it, like the backtest)
    stop0: float
    q: List[int]
    anchor: float = NAN                  # breakout close (stop / targets measured from it); a reverse: its fill
    unit: float = 0.0                    # 1 R, rounded to the tick
    tick: float = 0.25
    x_tp1: float = 3.0
    x_tp2: float = NAN                   # nan = leg 2 leaves on the EMA close
    x_tp3: float = 30.0
    x_trail: float = 60.0
    x_il2: int = 200                     # leg 2 EMA length of this side
    rev: bool = False
    rev_bars: int = 10
    hold_bars: float = NAN
    tf: int = 5
    is_rev: bool = False
    entry_minute: str = ""
    skip_minute: str = ""
    # ---- state
    tps: List[float] = field(default_factory=lambda: [NAN, NAN, NAN])
    stop_px: float = NAN
    stage: int = 0                       # 0 initial stop, 1 TP1 reached (stop at entry), 2 leg 3 trailing
    best: float = NAN
    ext: float = NAN
    bars_closed: int = 0                 # chart bars since the entry bar (simulate: i - i_entry / i - i_open)
    reverse_ready: Optional[dict] = None
    # ---- the Trade interface
    open: List[bool] = field(default_factory=lambda: [True, True, True])
    stop: List[float] = field(default_factory=lambda: [NAN, NAN, NAN])
    kind: List[int] = field(default_factory=lambda: [0, 0, 0])
    tp1: float = NAN
    tp2: float = NAN
    risk: float = 0.0
    tp1_done: bool = False
    exits: List[dict] = field(default_factory=list)

    def __post_init__(self):
        if math.isnan(self.tps[0]):
            self.open = [q > 0 for q in self.q]
            self._levels(self.anchor)
            self.set_entry(self.entry)

    def _r(self, mult: float) -> float:
        tk = self.tick
        return max(1.0, math.floor(mult * self.unit / tk + 0.5)) * tk

    def _levels(self, a: float):
        d = self.side
        self.tps = [a + d * self._r(self.x_tp1), a + d * (1e9 if math.isnan(self.x_tp2) else self._r(self.x_tp2)),
                    a + d * self._r(self.x_tp3)]
        self.stop_px = a - d * self._r(1.0)
        self.stop0 = self.stop_px

    def set_entry(self, px: float):
        self.entry = float(px)
        if self.is_rev:                      # a reverse: every level from its own fill (the backtest's stop fill)
            self._levels(self.entry)
        if math.isnan(self.ext):
            self.ext = self.entry                # the backtest: ext = entry, then the best price reached
        elif self.stage == 0:
            self.ext = max(self.ext, self.entry) if self.side > 0 else min(self.ext, self.entry)
        self.risk = self.side * (self.entry - self.stop0)
        self._ui()

    def shift(self, d: float):
        self.entry += d
        self.stop0 += d
        self.anchor += d
        self.stop_px += d
        self.tps = [x + d for x in self.tps]
        for k in ("best", "ext"):
            v = getattr(self, k)
            if not math.isnan(v):
                setattr(self, k, v + d)
        self._ui()

    def _ui(self):
        self.stop = [self.stop_px] * 3
        self.kind = [self.stage] * 3
        self.tp1 = self.tps[0]
        self.tp2 = self.tps[2]

    @property
    def is_open(self) -> bool:
        return any(self.open)

    def open_qty(self) -> int:
        return sum(q for q, o in zip(self.q, self.open) if o)

    def target_position(self) -> int:
        return self.side * self.open_qty()

    def r_now(self, px: Optional[float]) -> float:
        if px is None or self.risk <= 0:
            return NAN
        return self.side * (px - self.entry) / self.risk

    def _out(self, k: int, why: int, px: float, when: str):
        from desk.trade import Exit
        self.open[k] = False
        e = Exit(k + 1, REASON[why], float(px), when)
        self.exits.append(asdict(e))
        return e

    def close_all(self, reason: str, level: float, when: str = ""):
        from desk.trade import Exit
        out = []
        for k in range(3):
            if self.open[k]:
                self.open[k] = False
                e = Exit(k + 1, reason, float(level), when)
                self.exits.append(asdict(e))
                out.append(e)
        return out

    def _bar_ok(self, minute: str) -> bool:
        """ok[i] and not flat[i] of the chart bar holding this minute (the reverse may only open there)."""
        bar = pd.DatetimeIndex([pd.Timestamp(minute).floor(f"{self.tf}min")]).tz_convert(NY)
        ok, flat = bar_flags(bar, self.tf, None, {"backtest": {}})
        return bool(ok[0]) and not bool(flat[0])

    def _stop_moves(self, reached, fav):
        d = self.side
        if reached[0] and self.stage == 0:
            self.stage = 1
            self.stop_px = self.entry
        if reached[1] and self.stage == 1 and not math.isnan(self.x_tp2):
            self.stage = 2
            self.best = self.ext
        if self.stage == 2:
            self.best = max(self.best, fav) if d > 0 else min(self.best, fav)
            ts = self.best - d * self.x_trail / 100.0 * abs(self.best - self.entry)
            self.stop_px = max(self.stop_px, ts) if d > 0 else min(self.stop_px, ts)

    # ------------------------------------------------------------------ completed minute (simulate's j loop)
    def on_minute(self, o: float, h: float, l: float, c: float, minute: str, rules=None):
        if not self.is_open or minute == self.skip_minute:
            return []
        d = self.side
        fav = h if d > 0 else l
        adv = l if d > 0 else h
        high_first = h - o <= o - l
        fav_first = high_first if d > 0 else not high_first
        reached = [False, False, False]
        stopped = False
        stop_stage = self.stage
        fill = NAN
        out = []
        self.ext = max(self.ext, fav) if d > 0 else min(self.ext, fav)
        for step in range(2):
            if (step == 0) == fav_first:
                for k in range(3):
                    if d * (fav - self.tps[k]) >= 0:
                        reached[k] = True
                        if k == 0:
                            self.tp1_done = True
                        if self.open[k]:
                            px = o if d * (o - self.tps[k]) >= 0 else self.tps[k]
                            out.append(self._out(k, k, px, minute))
            elif d * (adv - self.stop_px) <= 0:
                px = o if d * (o - self.stop_px) <= 0 else self.stop_px
                for k in range(3):
                    if self.open[k]:
                        out.append(self._out(k, 3 + self.stage, px, minute))
                        stopped = True
                fill = px
                stop_stage = self.stage
        if not self.is_open:
            if (self.rev and stopped and stop_stage == 0 and not reached[0] and not self.is_rev
                    and self.bars_closed + 1 <= self.rev_bars and self._bar_ok(minute)):
                self.reverse_ready = {"px": fill, "minute": minute}
            self._ui()
            return out
        self._stop_moves(reached, fav)
        self._ui()
        return out

    # ------------------------------------------------------------------ live price (exits only, nothing moves)
    def on_price(self, px: float, when: str = ""):
        if not self.is_open or px is None or math.isnan(px):
            return []
        d = self.side
        out = []
        minute = pd.Timestamp(when).floor("min").isoformat() if when else ""
        if d * (px - self.stop_px) <= 0:
            st = self.stage
            for k in range(3):
                if self.open[k]:
                    out.append(self._out(k, 3 + st, self.stop_px, when))
            if (self.rev and st == 0 and not self.tp1_done and not self.is_rev and self.bars_closed + 1 <= self.rev_bars
                    and minute and self._bar_ok(minute)):
                self.reverse_ready = {"px": self.stop_px, "minute": minute}
        else:
            for k in range(3):
                if d * (px - self.tps[k]) >= 0:
                    if k == 0:
                        self.tp1_done = True
                    if self.open[k]:
                        out.append(self._out(k, k, self.tps[k], when))
        self._ui()
        return out

    # ------------------------------------------------------------------ chart bar close
    def on_bar_close(self, close: float, flat: bool, rules=None, when: str = "", ema2: float = NAN):
        if not self.is_open:
            return []
        d = self.side
        out = []
        self.bars_closed += 1
        if self.stage == 1 and math.isnan(self.x_tp2) and not math.isnan(ema2) and d * (close - ema2) < 0:
            if self.open[1]:
                out.append(self._out(1, 8, close, when))
            self.stage = 2
            self.best = self.ext
            ts = self.best - d * self.x_trail / 100.0 * abs(self.best - self.entry)
            self.stop_px = max(self.stop_px, ts) if d > 0 else min(self.stop_px, ts)
        if self.is_open and not math.isnan(self.hold_bars) and self.bars_closed >= self.hold_bars:
            for k in range(3):
                if self.open[k]:
                    out.append(self._out(k, 9, close, when))
        self._ui()
        return out

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "BoxTrade":
        return BoxTrade(**{k: d[k] for k in BoxTrade.__dataclass_fields__ if k in d})


# =====================================================================================================  the logic
@register("box")
class BoxLogic(Logic):
    reenter_same_bar = True
    trade_cls = BoxTrade

    def __init__(self, card: dict):
        super().__init__(card)
        self.p = full_params(card["logic"])
        self.inst = card["signal"]
        self.timeframe = int(self.p["timeframe"])
        self.warmup_days = 30
        self.max_day_trades = 0
        self.risk_usd = 0.0
        self.tick = B.SPECS[self.inst][1]
        p = self.p
        self.uses_iv = any(not B.is_off(p.get(k)) for k in ("iv_min", "iv_max", "f_iv_min"))

    def exit_rules(self) -> ExitRules:
        return ExitRules(be_after_tp1=True, trail=True)

    def leg_qty(self, boost: bool):
        m = float(self.p["size_up_mult"] or 1) if boost else 1.0
        return tuple(float(self.p[k] or 0) * m for k in ("qty1", "qty2", "qty3"))

    # ------------------------------------------------------------------ arrays (run_single, before simulate)
    def arrays(self, m1: pd.DataFrame, cfg: Optional[dict] = None) -> dict:
        p = self.p
        tf = self.timeframe
        cfg = cfg or {"backtest": {}, "data": {"instrument": self.inst}}
        if self.uses_iv:
            iv_file(B.IV_INDEX[self.inst])
            B.IVDIR = IV_DIR
        t, o, h, l, c, bstart = chart_bars(m1, tf)
        v = np.add.reduceat(m1.volume.to_numpy(float), bstart[:-1])
        ok, flat = bar_flags(t, tf, p["flat_time"], cfg)
        tg = pd.DataFrame([p])
        tables, arrs, vr, iv, vmin, extra = B.build(tg, h, l, c, v, p["box_edges"])
        eok, er = B.hours_ok(t, p["skip_hours"]), B.trend_er(t, c, int(p["er_days"]))
        ivl, ivr = B.implied_vol(t, self.inst, int(p["ivr_days"]))
        FL, FS, fx = B.filter_arrays(tg, t, o, h, l, c, v, tf, ivl)
        ATRS, BATRS, TOPS, BOTS, EMAS = tables
        a = [x[0] for x in arrs]
        ex = [x[0] for x in extra] + [x[0] for x in fx]
        names = ["ia", "ib", "ix", "ie", "il2", "max_box", "buf", "allow_long", "allow_short", "sl_pct", "tp1", "tp3",
                 "trail", "rev", "rev_bars"]
        xn = ["max_er", "min_box", "min_sq", "be", "risk", "max_mult", "iv_lo", "iv_hi", "ivr_lo", "ivr_hi", "s_tp1",
              "s_tp2", "s_tp3", "s_trail", "s_il2", "g_r", "g_sl", "r_tr", "r_pc", "cap0", "s_step", "s_floor", "s_slp",
              "hold", "cmask", "min_sc", "up_sc", "up_m", "bx_min"]
        k = dict(zip(names, a)) | dict(zip(xn, ex))
        emas = list(dict.fromkeys(int(x) for col in ("ema_len", "leg2_ema_len", "short_leg2_ema_len")
                                  for x in tg[col] if not B.is_off(x)))
        return dict(t=t, c=c, ok=ok, flat=flat, eok=eok, ER=er, IVL=ivl, IVR=ivr, FL=FL, FS=FS, ATRS=ATRS,
                    BATRS=BATRS, TOPS=TOPS, BOTS=BOTS, EMAS=EMAS, VR=vr, iv=int(iv[0]), vmin=float(vmin[0]), k=k,
                    emas=emas, bstart=bstart)

    def _ctx_arrays(self, ctx: BarContext) -> dict:
        key = ("box_arrays", self.card["id"])
        a = ctx.shared.get(key)
        if a is None:
            a = ctx.shared[key] = self.arrays(ctx.m1)
        return a

    def bar_values(self, ctx: BarContext) -> dict:
        """What an open trade needs at this bar close: the leg 2 EMA of both sides at the bar."""
        a = self._ctx_arrays(ctx)
        k, j = a["k"], len(a["t"]) - 1
        if j < 0:
            return {}
        il2 = int(k["il2"])
        s_il2 = int(k["s_il2"])
        return {"ema_long": float(a["EMAS"][il2, j]),
                "ema_short": float(a["EMAS"][il2 if s_il2 < 0 else s_il2, j])}

    def on_bar_close(self, ctx: BarContext) -> List[Plan]:
        a = self._ctx_arrays(ctx)
        if len(a["t"]) < 2:
            return []
        return self.plans_at(a, len(a["t"]) - 1)

    # ------------------------------------------------------------------ simulate()'s entry block for bar i
    def plans_at(self, a: dict, i: int) -> List[Plan]:
        k = a["k"]
        c = a["c"]
        if not a["ok"][i] or not a["eok"][i] or i == 0:
            return []
        ix, ia, ib = int(k["ix"]), int(k["ia"]), int(k["ib"])
        hi, lo = a["TOPS"][ix, i], a["BOTS"][ix, i]
        xa, xa1 = a["ATRS"][ia, i], a["ATRS"][ia, i - 1]
        if np.isnan(hi) or np.isnan(xa) or np.isnan(xa1):
            return []
        height = hi - lo
        ba1 = a["BATRS"][ib, i - 1]
        nan = np.isnan
        if not nan(k["max_box"]) and (nan(ba1) or height > k["max_box"] * ba1):
            return []
        if not nan(k["min_box"]) and not height >= k["min_box"] * ba1:
            return []
        if not nan(k["min_sq"]) and not xa >= k["min_sq"] * a["BATRS"][ib, i]:
            return []
        if not nan(k["max_er"]) and not a["ER"][i] <= k["max_er"]:
            return []
        if not nan(k["iv_lo"]) and not a["IVL"][i] >= k["iv_lo"]:
            return []
        if not nan(k["iv_hi"]) and not a["IVL"][i] <= k["iv_hi"]:
            return []
        b, bp = k["buf"] * xa, k["buf"] * xa1
        d = 0
        if c[i] > hi + b and not c[i - 1] > a["TOPS"][ix, i - 1] + bp:
            d = 1
        elif c[i] < lo - b and not c[i - 1] < a["BOTS"][ix, i - 1] - bp:
            d = -1
        if d == 0 or (d > 0 and not k["allow_long"]) or (d < 0 and not k["allow_short"]):
            return []
        ie = int(k["ie"])
        if ie >= 0 and not (d * (c[i] - a["EMAS"][ie, i]) > 0):
            return []
        if not nan(a["vmin"]) and not a["VR"][a["iv"], i] > a["vmin"]:
            return []
        sc = 0
        cmask = int(k["cmask"])
        if cmask != 0 or not nan(k["bx_min"]):
            nen = 0
            FLS = a["FL"] if d > 0 else a["FS"]
            for r in range(FLS.shape[0]):
                if (cmask >> r) & 1:
                    nen += 1
                    if FLS[r, i]:
                        sc += 1
            if not nan(k["bx_min"]):
                nen += 1
                if height >= k["bx_min"] * xa:
                    sc += 1
            if sc < (nen if nan(k["min_sc"]) else k["min_sc"]):
                return []
        boost = bool(not nan(k["up_sc"]) and sc >= k["up_sc"])
        tk = self.tick
        unit = max(1.0, math.floor(k["sl_pct"] / 100.0 * height / tk + 0.5)) * tk
        stop = c[i] - d * max(1.0, math.floor(unit / tk + 0.5)) * tk
        t = a["t"]
        nxt = t[i] + pd.Timedelta(minutes=self.timeframe)
        return [Plan(side=d, kind="market", level=float(c[i]), anchor=_iso(t[i]), signal_time=_iso(t[i]),
                     after=_iso(nxt), stop_ref=float(stop), atr=float(unit), target_ref=float(height), boost=boost,
                     final=True, entry_bar=_iso(t[i]), valid_until=_iso(nxt), stop=float(stop),
                     note=f"score {sc}")]

    def finalize(self, plan: Plan, entry_bar: pd.Timestamp) -> bool:
        return True

    # ------------------------------------------------------------------ trades
    def _side_exits(self, d: int) -> dict:
        k = self.p
        s_tp2 = -float(k["tp2_mult"]) if not B.is_off(k["tp2_mult"]) else NAN
        if d > 0:
            x_tp2 = -s_tp2 if (not math.isnan(s_tp2) and s_tp2 < 0) else NAN
            return dict(x_tp1=float(k["tp1_mult"]), x_tp2=x_tp2, x_tp3=float(k["tp3_mult"]),
                        x_trail=float(k["trail_pct"]), x_il2=int(k["leg2_ema_len"]))
        num = lambda key, alt: float(alt) if B.is_off(k[key]) else float(k[key])  # noqa: E731
        return dict(x_tp1=num("short_tp1_mult", k["tp1_mult"]), x_tp2=abs(s_tp2),
                    x_tp3=num("short_tp3_mult", k["tp3_mult"]), x_trail=num("short_trail_pct", k["trail_pct"]),
                    x_il2=int(k["leg2_ema_len"] if B.is_off(k["short_leg2_ema_len"]) else k["short_leg2_ema_len"]))

    def _common(self) -> dict:
        k = self.p
        hold = NAN if B.is_off(k["max_hold_hours"]) else float(k["max_hold_hours"]) * 60.0 / self.timeframe
        return dict(tick=self.tick, rev=bool(k["reverse"]), rev_bars=int(k["reverse_bars"]), hold_bars=hold,
                    tf=self.timeframe)

    def make_trade(self, plan: Plan, px: float, q: List[int], when: str) -> BoxTrade:
        em = pd.Timestamp(when).tz_convert("UTC").floor("min")
        return BoxTrade(side=plan.side, entry=float(px), stop0=float(plan.stop), q=list(q) + [0] * (3 - len(q)),
                        anchor=float(plan.level), unit=float(plan.atr), entry_minute=em.isoformat(),
                        **self._side_exits(plan.side), **self._common())

    def make_reverse(self, old: BoxTrade) -> Optional[BoxTrade]:
        r = old.reverse_ready
        if not r:
            return None
        d = -old.side
        nxt = (pd.Timestamp(r["minute"]) + pd.Timedelta(minutes=1)).isoformat()
        tr = BoxTrade(side=d, entry=float(r["px"]), stop0=NAN, q=list(old.q), anchor=float(r["px"]), unit=old.unit,
                      is_rev=True, entry_minute=nxt, skip_minute=r["minute"], **self._side_exits(d), **self._common())
        tr.bars_closed = -1                   # the reverse's own bar closes at i - i_open = 0
        return tr

    def trade_bar_args(self, trade: BoxTrade, vals: dict) -> dict:
        return {"ema2": vals.get("ema_long" if trade.side > 0 else "ema_short", NAN)}
