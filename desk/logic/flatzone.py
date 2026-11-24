"""
"flatzone" logic engine: Flat Zone Breakout (nq_wae_backtest/flatzone/flatzone_backtest.py).

Signals: at every chart-bar close the rolling window of 1-minute bars goes through the research code's own
zones() / ATR / ADX / same-time-of-day volume (research/flatzone_backtest.py, vendored verbatim). The last bar's
values are the live signal. Entry = plan kind "open": a market order at the first price after the breakout bar closes (the backtest: the
next bar's open; after a session break that is the reopen).

Exits: FlatTrade below is a line-by-line port of simulate()'s position loop - two legs (T1 contracts and the
trailing contracts), stop by ATR / box / box middle, T1 and an optional T2 cap measured from the breakout close,
break-even after T1 (from the next chart bar), a profit lock (be_atr -> stop at entry + be_lock ATR, from the next
minute), a trailing stop active once trail_act ATR in profit, a time exit, and the stop-and-reverse: when the
INITIAL stop is hit within rev_bars chart bars the desk opens the same size the other way (Deployment, once).

Not used live: qty_tiers (price-level multipliers; at today's prices every tier card is at its x1 level - the
quantity boxes are the contracts) and risk_usd / qty_step.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd

from desk.logic.base import BarContext, ExitRules, Logic, Plan, register
from desk.logic.patterns.adapter import _RESEARCH  # noqa: F401  (puts research/ on sys.path)

import flatzone_backtest as F  # noqa: E402  (verbatim research code, see SOURCE_HASHES.txt)

NAN = float("nan")
INF = float("inf")
LIVE_OFF = ("qty_step", "risk_usd")          # not supported live
IGNORED = ("qty_tiers",)                     # see the module doc


def _clean(x):
    return None if isinstance(x, str) and x.strip().lower() in ("null", "none", "") else x


def full_params(logic: dict) -> dict:
    bad = [k for k in logic if k not in F.DEFAULTS]
    if bad:
        raise ValueError(f"unknown setting(s) {bad}")
    p = {k: _clean(logic.get(k, v)) for k, v in F.DEFAULTS.items()}
    for k in LIVE_OFF:
        if p.get(k):
            raise ValueError(f"{k} is not supported live")
    try:
        F.check(p)
    except SystemExit as e:
        raise ValueError(str(e))
    return p


def _iso(t) -> str:
    return pd.Timestamp(t).isoformat()


def _minute_no(iso: str) -> int:
    return int((pd.Timestamp(iso) - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(minutes=1))


class LiveBars(F.Bars):
    """flatzone Bars on the live window (live prices are real prices: raw close = close)."""

    def __init__(self, m1: pd.DataFrame):
        super().__init__(m1.reset_index(drop=True), {"backtest": {}, "data": {}})

    def raw_close(self, tf):
        return self.get(tf)["c"]


# =====================================================================================================  the trade
REASON_OF = {1: "stop", 2: "be", 3: "trail", 6: "lock"}


@dataclass
class FlatTrade:
    side: int
    entry: float
    stop0: float
    q: List[int]
    anchor: float = NAN                  # breakout close (levels are measured from it; a reverse: from its fill)
    ta: float = NAN                      # ATR(t_atr_len) of the breakout bar
    t1m: float = 0.0
    t2m: float = 0.0
    tram: float = 0.0
    trm: float = 0.0
    be_a: float = 0.0
    be_l: float = 0.0
    be_t1: bool = False
    rev_n: int = 0
    rev_ok: bool = True
    is_rev: bool = False
    risk_x: float = NAN                  # reverse: the reversed trade's entry -> stop distance
    hold_m: int = 0
    entry_minute: str = ""               # ISO UTC of the entry minute
    skip_minute: str = ""                # a reverse is managed from the minute AFTER the stop
    e_min: int = 0
    # ---- state, x space (x = side * price: every check is the long one)
    x_e: float = NAN
    x_sl: float = NAN
    x_lim: float = NAN
    x_lim2: float = INF
    x_act: float = NAN
    x_off: float = INF
    x_bea: float = 0.0
    x_bel: float = 0.0
    x_best: float = -INF
    tr_on: bool = False
    be_on: bool = False
    be_pend: bool = False
    lk_on: bool = False
    lk_next: bool = False
    rv_go: bool = False
    rv_px: float = NAN
    reverse_ready: Optional[dict] = None
    # ---- the Trade interface (real prices, for the desk / page / journal)
    open: List[bool] = field(default_factory=lambda: [True, True])
    stop: List[float] = field(default_factory=lambda: [NAN, NAN])
    kind: List[int] = field(default_factory=lambda: [0, 0])
    tp1: float = NAN
    tp2: float = NAN
    best: float = NAN
    risk: float = 0.0
    tp1_done: bool = False
    bars_closed: int = 0
    exits: List[dict] = field(default_factory=list)

    def __post_init__(self):
        if math.isnan(self.x_e):
            s = self.side
            self.open = [self.q[0] > 0, self.q[1] > 0]
            self.x_sl = s * self.stop0
            self._anchor(s * self.anchor)
            self.set_entry(self.entry)

    def _anchor(self, xa: float):
        ta = self.ta
        self.x_lim = xa + self.t1m * ta
        self.x_act = xa + self.tram * ta
        self.x_off = self.trm * ta if self.trm > 0 else INF
        self.x_bea = self.be_a * ta if self.be_a > 0 else 0.0
        self.x_bel = self.be_l * ta
        self.x_lim2 = xa + self.t2m * ta if self.t2m > 0 else INF

    def set_entry(self, px: float):
        """The entry price (the real fill). A first trade keeps its levels on the breakout close; a reverse
        measures everything from its own fill, like the backtest's reverse at the stop fill."""
        s = self.side
        self.entry = float(px)
        self.x_e = s * self.entry
        if self.is_rev:
            self.x_sl = self.x_e - self.risk_x
            self.stop0 = s * self.x_sl
            self._anchor(self.x_e)
        self.risk = self.x_e - self.x_sl
        self._ui()

    def shift(self, d: float):
        """Every price level moves by d (the bar series changed contract month)."""
        s = self.side
        self.entry += d
        self.stop0 += d
        self.anchor += d
        if not math.isnan(self.rv_px):
            self.rv_px += d
        for k in ("x_e", "x_sl", "x_lim", "x_lim2", "x_act", "x_best"):
            setattr(self, k, getattr(self, k) + s * d)
        self._ui()

    # ------------------------------------------------------------------ levels
    def _levels(self):
        lk = self.x_e + self.x_bel if self.lk_on else -INF
        base2 = max(max(self.x_sl, self.x_e) if self.be_on else self.x_sl, lk)
        st2 = max(base2, self.x_best - self.x_off) if self.tr_on else base2
        return lk, base2, max(self.x_sl, lk), st2

    def _ui(self):
        s = self.side
        lk, base2, l1, l2 = self._levels()
        self.stop = [s * l1, s * l2]
        self.kind = [1 if l1 > self.x_sl else 0,
                     2 if (self.tr_on and self.x_best - self.x_off > base2) else (1 if l2 > self.x_sl else 0)]
        self.tp1 = s * self.x_lim
        self.tp2 = s * self.x_lim2 if math.isfinite(self.x_lim2) else NAN
        self.best = s * self.x_best if math.isfinite(self.x_best) else self.entry

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

    def _out(self, leg: int, reason: str, x_level: float, when: str):
        from desk.trade import Exit
        self.open[leg - 1] = False
        e = Exit(leg, reason, float(self.side * x_level), when)
        self.exits.append(asdict(e))
        return e

    def close_all(self, reason: str, level: float, when: str = ""):
        self.rv_go = False
        out = [self._out(k, reason, self.side * level, when) for k in (1, 2) if self.open[k - 1]]
        self._ui()
        return out

    # ------------------------------------------------------------------ one stop check (simulate(), lo points)
    def _stops(self, px: float, gap: bool, when: str, out: list):
        lk, base2, l1, st2 = self._levels()
        for leg in (1, 2):
            if not self.open[leg - 1]:
                continue
            lvl = l1 if leg == 1 else st2
            if px <= lvl:
                fill = px if gap else lvl
                why = 1
                if leg == 2:
                    if self.tr_on and self.x_best - self.x_off > base2:
                        why = 3
                    elif self.be_on and self.x_e > self.x_sl:
                        why = 2
                if self.lk_on and lvl == lk and lk > self.x_sl and why != 3:
                    why = 6
                out.append(self._out(leg, REASON_OF[why], fill, when))
                if (self.rev_n > 0 and not self.is_rev and not self.rv_go and lvl == self.x_sl and why == 1
                        and self.bars_closed < self.rev_n and self.rev_ok):
                    self.rv_go = True
                    self.rv_px = self.side * fill

    def _targets(self, px: float, gap: bool, when: str, out: list, update: bool):
        if update and self.x_bea > 0 and not self.lk_on and px >= self.x_e + self.x_bea:
            self.lk_next = True
        if self.open[0] and px >= self.x_lim:
            out.append(self._out(1, "tp1", px if gap else self.x_lim, when))
            self.tp1_done = True
            if self.be_t1 and self.open[1]:
                self.be_pend = True
        if self.open[1] and px >= self.x_lim2:
            out.append(self._out(2, "tp2", px if gap else self.x_lim2, when))
        if update and self.open[1]:
            if not self.tr_on and px >= self.x_act:
                self.tr_on = True
            if self.tr_on and px > self.x_best:
                self.x_best = px

    def _ended(self, minute: str):
        if not self.is_open and self.rv_go:
            self.reverse_ready = {"px": self.rv_px, "minute": minute}
            self.rv_go = False

    # ------------------------------------------------------------------ completed minute
    def on_minute(self, o: float, h: float, l: float, c: float, minute: str, rules=None):
        if not self.is_open or minute == self.skip_minute:
            return []
        out = []
        if self.lk_next:
            self.lk_on, self.lk_next = True, False
        s = self.side
        if self.hold_m > 0 and _minute_no(minute) - self.e_min >= self.hold_m:
            self.rv_go = False
            out = [self._out(k, "time", s * o, minute) for k in (1, 2) if self.open[k - 1]]
            self._ui()
            return out
        if s > 0:
            xo, xh, xl = o, h, l
        else:
            xo, xh, xl = -o, -l, -h
        up_first = (xh - xo) <= (xo - xl)
        for step in range(3):
            if step == 0:
                px, gap = xo, True
            elif (step == 1) == up_first:
                px, gap = xh, False
            else:
                px, gap = xl, False
            hi_pt = step == 0 or px == xh
            lo_pt = step == 0 or px == xl
            if lo_pt:
                self._stops(px, gap, minute, out)
            if hi_pt:
                self._targets(px, gap, minute, out, update=True)
            if not self.is_open:
                break
        self._ended(minute)
        self._ui()
        return out

    # ------------------------------------------------------------------ live price (exits only, nothing moves)
    def on_price(self, px: float, when: str = ""):
        if not self.is_open or px is None or math.isnan(px):
            return []
        s = self.side
        minute = pd.Timestamp(when).floor("min").isoformat() if when else ""
        if self.hold_m > 0 and minute and _minute_no(minute) - self.e_min >= self.hold_m:
            self.rv_go = False
            out = [self._out(k, "time", s * px, when) for k in (1, 2) if self.open[k - 1]]
            self._ui()
            return out
        out = []
        x = s * px
        self._stops(x, False, when, out)
        if self.is_open:
            self._targets(x, False, when, out, update=False)
        self._ended(minute)
        self._ui()
        return out

    # ------------------------------------------------------------------ chart bar close
    def on_bar_close(self, close: float, flat: bool, rules=None, when: str = ""):
        if not self.is_open:
            return []
        self.bars_closed += 1                 # the entry bar's own close = 1
        if self.be_pend:                      # break-even after T1 counts from the next chart bar
            self.be_on, self.be_pend = True, False
            self._ui()
        return []

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "FlatTrade":
        return FlatTrade(**{k: d[k] for k in FlatTrade.__dataclass_fields__ if k in d})


# =====================================================================================================  the logic
@register("flatzone")
class FlatLogic(Logic):
    reenter_same_bar = True                   # the backtest enters at the close of a bar whose minutes closed a trade
    trade_cls = FlatTrade

    def __init__(self, card: dict):
        super().__init__(card)
        self.p = full_params(card["logic"])
        self.timeframe = int(self.p["timeframe"])
        self.warmup_days = 30
        self.max_day_trades = 0
        self.risk_usd = 0.0

    def exit_rules(self) -> ExitRules:
        p = self.p
        return ExitRules(be_after_tp1=bool(p["be_after_t1"]), trail=bool(p["trail_atr"]))

    def leg_qty(self, boost: bool):
        m = float(self.p["vol_mult"]) if boost else 1.0
        return (float(self.p["qty_t1"]) * m, float(self.p["qty_t2"]) * m)

    # ------------------------------------------------------------------ signals
    def bars(self, ctx: BarContext) -> LiveBars:
        b = ctx.shared.get("fz_bars")
        if b is None:
            b = ctx.shared["fz_bars"] = LiveBars(ctx.m1)
        return b

    def arrays(self, bars: F.Bars) -> dict:
        """run()'s arrays before simulate() (same code, same order)."""
        p = self.p
        tf = self.timeframe
        b = bars.get(tf)
        o, c = b["o"], b["c"]
        body = p["box_src"] == "body"
        hi_v = np.maximum(o, c) if body else c
        lo_v = np.minimum(o, c) if body else c
        zk = F.zone_key(p)
        zc = bars.__dict__.setdefault("zcache", {})
        if zk not in zc:
            zc[zk] = F.zones(o, c, hi_v, lo_v, bars.atr(tf, int(p["atr_len"])), int(p["min_bars"]), int(p["max_bars"]),
                             float(p["max_h_atr"]), float(p["max_slope"]), p["break_mode"] == "body", float(p["break_pct"]))
        sig, ztop, zbot, zlen, zfirst, started = zc[zk]
        ok = b["ok"] & ~F.skip_mask(b["t"], p["skip_hours"])
        vk = zk + (p["vol_ref"], int(p["vol_days"]))
        vc = bars.__dict__.setdefault("vcache", {})
        if vk not in vc:
            vc[vk] = F.vol_ratio(bars, tf, p, sig, zfirst)
        vr = vc[vk]
        if p["vol_min"] is not None:
            ok = ok & (vr >= float(p["vol_min"]))
        boost = np.zeros(len(c), np.bool_)
        if p["vol_boost"] is not None:
            boost = vr >= float(p["vol_boost"])
        rev_ok = np.ones(len(c), np.bool_)
        if p["adx_min"] is not None:
            ok = ok & (bars.adx(tf, int(p["adx_len"])) >= float(p["adx_min"]))
        if p["rev_vol_min"] is not None:
            rev_ok &= vr >= float(p["rev_vol_min"])
        if p["rev_vol_max"] is not None:
            rev_ok &= vr < float(p["rev_vol_max"])
        return dict(t=b["t"], c=c, sig=sig, ztop=ztop, zbot=zbot, ok=ok, boost=boost, rev_ok=rev_ok,
                    ta=bars.atr(tf, int(p["t_atr_len"])))

    def on_bar_close(self, ctx: BarContext) -> List[Plan]:
        a = self.arrays(self.bars(ctx))
        if not len(a["t"]):
            return []
        return self.plans_at(a, len(a["t"]) - 1)

    def plans_at(self, a: dict, j: int) -> List[Plan]:
        p = self.p
        s = int(a["sig"][j])
        if s == 0 or not a["ok"][j]:
            return []
        if (s > 0 and p["direction"] == "Short only") or (s < 0 and p["direction"] == "Long only"):
            return []
        c, ta = float(a["c"][j]), float(a["ta"][j])
        if p["sl_mode"] == "box":
            lvl = float(a["zbot"][j] if s > 0 else a["ztop"][j])
        elif p["sl_mode"] == "mid":
            lvl = float((a["ztop"][j] + a["zbot"][j]) / 2)
        else:
            lvl = c - s * float(p["sl_atr"]) * ta
        if not (math.isfinite(lvl) and math.isfinite(ta)):
            return []
        tj = a["t"][j]
        nxt = tj + pd.Timedelta(minutes=self.timeframe)
        return [Plan(side=s, kind="open", level=c, anchor=_iso(tj), signal_time=_iso(tj), after=_iso(nxt),
                     stop_ref=lvl, atr=ta, target_ref=c, boost=bool(a["boost"][j]), final=True,
                     entry_bar=_iso(nxt), valid_until=_iso(nxt + pd.Timedelta(minutes=self.timeframe)),
                     stop=lvl, note=json.dumps({"rev_ok": bool(a["rev_ok"][j])}))]

    def finalize(self, plan: Plan, entry_bar: pd.Timestamp) -> bool:
        return True                            # "open" plans are final already

    # ------------------------------------------------------------------ trades
    def _common(self) -> dict:
        p = self.p
        return dict(t1m=float(p["t1_atr"]), t2m=float(p["t2_atr"] or 0), tram=float(p["trail_act"] or 0),
                    trm=float(p["trail_atr"] or 0), be_a=float(p["be_atr"] or 0), be_l=float(p["be_lock"] or 0),
                    be_t1=bool(p["be_after_t1"]), rev_n=int(p["rev_bars"] or 0),
                    hold_m=int(round(float(p["max_hold_days"] or 0) * 1440)))

    def make_trade(self, plan: Plan, px: float, q: List[int], when: str) -> FlatTrade:
        em = pd.Timestamp(when).tz_convert("UTC").floor("min")
        try:
            rev_ok = bool(json.loads(plan.note or "{}").get("rev_ok", True))
        except ValueError:
            rev_ok = True
        return FlatTrade(side=plan.side, entry=float(px), stop0=float(plan.stop), q=list(q), anchor=float(plan.level),
                         ta=float(plan.atr), rev_ok=rev_ok, entry_minute=em.isoformat(),
                         e_min=_minute_no(em.isoformat()), **self._common())

    def make_reverse(self, old: FlatTrade) -> Optional[FlatTrade]:
        r = old.reverse_ready
        if not r:
            return None
        minute = r["minute"]
        nxt = (pd.Timestamp(minute) + pd.Timedelta(minutes=1)).isoformat()
        tr = FlatTrade(side=-old.side, entry=float(r["px"]), stop0=NAN, q=list(old.q), anchor=float(r["px"]),
                       ta=old.ta, is_rev=True, risk_x=old.x_e - old.x_sl, entry_minute=nxt, skip_minute=minute,
                       e_min=_minute_no(minute), **self._common())
        return tr
