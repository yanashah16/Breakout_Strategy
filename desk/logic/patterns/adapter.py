"""
"patterns" logic engine: flag, double top / bottom, pullback and trendline.

The signals come from the research code itself (research/patterns_backtest.py, vendored
verbatim): at every chart-bar close the rolling window of 1-minute bars is wrapped in a
LiveMarket and passed through the backtest's own _prepare(). The last bar's values are the
live signal - so a live signal is, by construction, the backtest's signal.

What the backtest's simulate() does at the entry is mirrored in on_bar_close() / finalize():
  kind 1 (flag, double, pullback): stop order from bar j works during the next bar i;
  kind 2 (trendline): market order at the close of bar j.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import List

import numpy as np
import pandas as pd

from desk.logic.base import BarContext, ExitRules, Logic, Plan, register

_RESEARCH = Path(__file__).resolve().parent / "research"
if str(_RESEARCH) not in sys.path:
    sys.path.insert(0, str(_RESEARCH))
import patterns_backtest as R  # noqa: E402  (verbatim research code)

NAN = float("nan")
UNSUPPORTED = {"rev_on": "opposite-break trades", "risk_usd": "risk sizing"}


def _clean(x):
    return None if isinstance(x, str) and x.strip().lower() in ("null", "none", "") else x


def full_params(logic: dict) -> dict:
    """The card's settings on top of the research defaults, normalised like research main()."""
    setup = logic.get("setup")
    if setup not in R.SETUPS:
        raise ValueError(f"unknown setup {setup!r}")
    allowed = {**R.COMMON, **R.SETUP_KEYS.get(setup, {}), **(R.FLAG_EXTRA if setup == "flag" else {})}
    bad = [k for k in logic if k not in allowed]
    if bad:
        raise ValueError(f"unknown setting(s) {bad} for setup {setup}")
    p = {k: _clean(logic.get(k, v)) for k, v in allowed.items()}
    for k, what in UNSUPPORTED.items():
        if p.get(k):
            raise ValueError(f"{what} ({k}) is not supported live")
    return p


class LiveMarket(R.Market):
    """research Market built from a DataFrame of live 1-minute bars instead of the parquet files."""

    def __init__(self, inst: str, m1: pd.DataFrame, htf_anchor):
        self.inst = inst
        self.m1 = m1.reset_index(drop=True)
        self.m1.attrs = {"htf_anchor": htf_anchor}
        self.mo, self.mh, self.ml, self.mc = (self.m1[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        self.pv, self.tick = R.SPECS[inst]
        self.cache = {}

    def raw_close(self, tf):
        return self.bars(tf)[4]                  # live bars are not back-adjusted: raw = close


def _iso(t) -> str:
    return pd.Timestamp(t).isoformat()


@register("patterns")
class PatternLogic(Logic):
    def __init__(self, card: dict):
        super().__init__(card)
        self.p = full_params(card["logic"])
        self.anchor_cfg = (card.get("data") or {}).get("htf_anchor")
        self.cfg = {"data": {"htf_anchor": self.anchor_cfg}, "backtest": {}, "costs": {}}
        self.timeframe = int(self.p["timeframe"])
        self.kind = 2 if self.p["setup"] == "trendline" else 1
        self.max_day_trades = int(self.p["max_day_trades"] or 0)
        self.warmup_days = 30
        tpm = str(self.p["tp_mode"]).lower()
        if self.p["setup"] == "double" and self.p.get("tp_mm"):
            tpm = "pole"
        if tpm not in ("r", "pole", "top"):
            raise ValueError(f"tp_mode {self.p['tp_mode']!r}")
        if tpm == "top" and self.p["setup"] != "flag":
            raise ValueError("tp_mode top works with flag only")
        self.tpm = tpm

    # ------------------------------------------------------------------ exits
    def exit_rules(self) -> ExitRules:
        p = self.p
        return ExitRules(
            be_r=float(p["be_r"] or 0), be_after_tp1=bool(p["be_after_tp1"]),
            runner_be_r=float(p.get("runner_be_r") or 0), trail=bool(p["trail"]),
            trail_start_r=-1.0 if p["trail_start_r"] is None else float(p["trail_start_r"]),
            trail_pct=float(p.get("trail_pct") or 0), max_bars=int(p["max_bars"] or 0),
            flat_time=p["flat_time"])

    def leg_qty(self, boost: bool):
        """(qty1, qty2) of the research setup, and the boosted pair (volume boost)."""
        q = (float(self.p["qty1"] or 0), float(self.p["qty2"] or 0))
        if not boost:
            return q
        return (float(self.p["qty1"] if self.p.get("boost_qty1") is None else self.p["boost_qty1"]),
                float(self.p["qty2"] if self.p.get("boost_qty2") is None else self.p["boost_qty2"]))

    # ------------------------------------------------------------------ signals
    def market(self, ctx: BarContext) -> LiveMarket:
        key = ("mk", self.anchor_cfg)
        mk = ctx.shared.get(key)
        if mk is None:
            mk = ctx.shared[key] = LiveMarket(ctx.symbol, ctx.m1, self.anchor_cfg)
        return mk

    def arrays(self, mk: LiveMarket):
        p = self.p
        tf = self.timeframe
        t, o, h, l, c, bstart = mk.bars(tf)
        LL, LS, LT, LA, SL_, SS, ST, SA, trend, A, ok, flat, day, ETH, sig, LR, SR = R._prepare(mk, p, self.cfg)
        if p["setup"] == "double" and p.get("tp_mm"):
            LT = LT * float(p["tp_mm"]); ST = ST * float(p["tp_mm"])
        if self.tpm == "top":
            with np.errstate(invalid="ignore"):
                LT = np.where((LA >= 0) & ~LR, h[np.clip(LA, 0, None)], np.nan)
                ST = np.where((SA >= 0) & ~SR, l[np.clip(SA, 0, None)], np.nan)
        LB, SB, _, _ = R.boost_arrays(mk, p, tf, sig, len(c))
        return dict(t=t, c=c, LL=LL, LS=LS, LT=LT, LA=LA, SL=SL_, SS=SS, ST=ST, SA=SA, trend=trend, A=A,
                    ok=ok, flat=flat, day=day, ETH=ETH, LB=LB, SB=SB)

    def on_bar_close(self, ctx: BarContext) -> List[Plan]:
        a = self.arrays(self.market(ctx))
        if not len(a["t"]):
            return []
        return self.plans_at(a, len(a["t"]) - 1)      # the bar that just closed = the window's last

    def plans_at(self, a: dict, j: int) -> List[Plan]:
        """simulate()'s entry logic for the signal bar j of the arrays `a`."""
        t = a["t"]
        tj = t[j]
        out = []
        for sd in (1, -1):
            L = sd == 1
            lv = (a["LL"] if L else a["SL"])[j]
            if np.isnan(lv):
                continue
            tr = int(a["trend"][j])
            if not (tr == 2 or tr == sd):
                continue
            anc = int((a["LA"] if L else a["SA"])[j])
            anchor = _iso(t[anc]) if 0 <= anc < len(t) else str(anc)
            plan = Plan(side=sd, kind="stop" if self.kind == 1 else "market", level=float(lv), anchor=anchor,
                        signal_time=_iso(tj), after=_iso(tj + pd.Timedelta(minutes=self.timeframe)),
                        stop_ref=float((a["LS"] if L else a["SS"])[j]), atr=float(a["A"][j]),
                        target_ref=float((a["LT"] if L else a["ST"])[j]),
                        boost=bool((a["LB"] if L else a["SB"])[j] > 0))
            if self.kind == 2:
                if not a["ok"][j] or a["flat"][j]:
                    continue
                plan.level = float(a["c"][j])
                self._finish(plan, tj, bool(a["ETH"][j]), int(a["day"][j]))
                plan.tp_mode = "r"               # simulate() kind 2: TP1 is always tp x R
            out.append(plan)
        return out

    # ------------------------------------------------------------------ entry bar
    def bar_ok(self, bar: pd.Timestamp) -> bool:
        ok, _ = R.entry_flags(pd.DatetimeIndex([bar]), self.timeframe, self.p, self.cfg)
        return bool(ok[0])

    def bar_flat(self, bar: pd.Timestamp) -> bool:
        _, flat = R.entry_flags(pd.DatetimeIndex([bar]), self.timeframe, self.p, self.cfg)
        return bool(flat[0])

    def bar_eth(self, bar: pd.Timestamp) -> bool:
        idx = pd.DatetimeIndex([bar])
        if self.p["setup"] == "flag":
            return bool(~R.rth_mask(idx, self.p["rth_hours"])[0])
        return bool(R.eth_bars(idx)[0])

    def finalize(self, plan: Plan, entry_bar: pd.Timestamp) -> bool:
        if not self.bar_ok(entry_bar):
            return False
        sess = int(R.session_ids(pd.DatetimeIndex([entry_bar]))[0])
        self._finish(plan, entry_bar, self.bar_eth(entry_bar), sess)
        return True

    def _finish(self, plan: Plan, bar: pd.Timestamp, eth: bool, session: int):
        p = self.p
        sd = plan.side
        x_sl = float(p["sl_atr"] if not eth or p["eth_sl_atr"] is None else p["eth_sl_atr"])
        x_tp = float(p["tp_r"] if not eth or p["eth_tp_r"] is None else p["eth_tp_r"])
        x_tr = float(p["trail_r"] if not eth or p["eth_trail_r"] is None else p["eth_trail_r"])
        if sd == -1:
            if p.get("short_sl_atr") is not None: x_sl = float(p["short_sl_atr"])
            if p.get("short_tp_r") is not None: x_tp = float(p["short_tp_r"])
            if p.get("short_trail_r") is not None: x_tr = float(p["short_trail_r"])
        plan.final = True
        plan.entry_bar = _iso(bar)
        plan.valid_until = _iso(bar + pd.Timedelta(minutes=self.timeframe))
        plan.session = session
        plan.eth = eth
        plan.stop = plan.stop_ref - sd * x_sl * plan.atr
        plan.tp_r = x_tp
        plan.trail_r = x_tr
        plan.tp2_r = float(p["tp2_r"] or 0)
        pt = plan.target_ref
        if self.tpm == "pole" and not math.isnan(pt):
            plan.tp_mode, plan.tp_dist = "dist", pt
        elif self.tpm == "top" and not math.isnan(pt):
            plan.tp_mode, plan.tp_price = "price", pt
        else:
            plan.tp_mode = "r"
