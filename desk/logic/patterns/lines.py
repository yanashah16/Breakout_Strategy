"""
"lines" logic engine: multi-touch line break and zone line (bounce / break + retest).

Same idea as the "patterns" engine (adapter.py): at every chart-bar close the rolling window of
1-minute bars is wrapped in a LiveMarket and passed through the research code's own prepared()
(research/lines_backtest.py = the 2026-11-10 patterns_backtest.py that has these two setups,
vendored verbatim). The last bar's values are the live signal.

Both setups are simulate() kind 2: a market order at the close of the signal bar.
prepared() already applies reverse / max_stop_atr / risk_min_pts, exactly as run_one() does.

Sizing: risk_usd (the card's, or the deployment's own "risk_usd") makes the platform size each trade
in whole contracts of the market it trades (see Deployment._enter); null = the quantity boxes.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from desk.logic.base import ExitRules, register
from desk.logic.patterns.adapter import PatternLogic, _RESEARCH, _clean  # noqa: F401  (_RESEARCH on sys.path)

import lines_backtest as R2  # noqa: E402  (verbatim research code, see SOURCE_HASHES.txt)

SETUPS = ("multitouch", "zoneline")
UNSUPPORTED = {"fail_reverse_bars": "fail-reverse trades", "tp_sl_bars": "the TP1 time limit",
               "vol_min": "volume filters", "vol_max": "volume filters", "tvol_min": "volume filters",
               "tvol_max": "volume filters", "trail_start_r": "trail start in R"}


def full_params(logic: dict) -> dict:
    setup = logic.get("setup")
    if setup not in SETUPS:
        raise ValueError(f"engine lines runs {SETUPS}, not {setup!r}")
    allowed = {**R2.COMMON, **R2.SETUP_KEYS[setup]}
    bad = [k for k in logic if k not in allowed]
    if bad:
        raise ValueError(f"unknown setting(s) {bad} for setup {setup}")
    p = {k: _clean(logic.get(k, v)) for k, v in allowed.items()}
    for k, what in UNSUPPORTED.items():
        if p.get(k):
            raise ValueError(f"{what} ({k}) is not supported live")
    if isinstance(p.get("tp_r"), list) or any(isinstance(v, (list, dict)) for v in p.values()):
        raise ValueError("a card needs single values, not sweep lists")
    return p


class LinesMarket(R2.Market):
    """lines_backtest Market built from live 1-minute bars instead of the parquet files."""

    def __init__(self, inst: str, m1: pd.DataFrame, htf_anchor):
        R2._PREP.clear()                         # prepared() is keyed by id(Market): a new one can reuse a freed id
        self.inst = inst
        self.m1 = m1.reset_index(drop=True)
        self.m1.attrs = {"htf_anchor": htf_anchor}
        self.mo, self.mh, self.ml, self.mc = (self.m1[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        self.pv, self.tick = R2.SPECS[inst]
        self.cache = {}


@register("lines")
class LinesLogic(PatternLogic):
    R = R2

    def __init__(self, card: dict):
        self.card = card
        self.p = full_params(card["logic"])
        self.anchor_cfg = (card.get("data") or {}).get("htf_anchor")
        self.cfg = {"data": {"htf_anchor": self.anchor_cfg}, "backtest": {}, "costs": {}}
        self.timeframe = int(self.p["timeframe"])
        self.kind = 2                            # market order at the signal bar's close
        self.max_day_trades = int(self.p["max_day_trades"] or 0)
        self.warmup_days = 30
        self.tpm = "r"
        self.risk_usd = float(self.p.get("risk_usd") or 0.0)

    def exit_rules(self) -> ExitRules:
        p = self.p
        return ExitRules(
            be_r=float(p["be_r"] or 0), be_after_tp1=bool(p["be_after_tp1"]), trail=bool(p["trail"]),
            trail_start_r=-1.0, max_bars=int(p["max_bars"] or 0), flat_time=p["flat_time"],
            tp1_lock_r=float(p.get("tp1_lock_r") or 0))

    def leg_qty(self, boost: bool):
        return (float(self.p["qty1"] or 0), float(self.p["qty2"] or 0))

    def market(self, ctx) -> LinesMarket:
        key = ("lines_mk", self.anchor_cfg)
        mk = ctx.shared.get(key)
        if mk is None:
            mk = ctx.shared[key] = LinesMarket(ctx.symbol, ctx.m1, self.anchor_cfg)
        return mk

    def arrays(self, mk):
        p = self.p
        t, o, h, l, c, bstart = mk.bars(self.timeframe)
        LL, LS, LT, LA, SL_, SS, ST, SA, trend, A, ok, flat, day, ETH, sig = R2.prepared(mk, p, self.cfg)
        z = np.zeros(len(c))
        return dict(t=t, c=c, LL=LL, LS=LS, LT=LT, LA=LA, SL=SL_, SS=SS, ST=ST, SA=SA, trend=trend, A=A,
                    ok=ok, flat=flat, day=day, ETH=ETH, LB=z, SB=z)

    def _finish(self, plan, bar, eth, session):
        super()._finish(plan, bar, eth, session)
        plan.lock_px = float(self.p.get("tp1_lock_atr") or 0.0) * plan.atr

    # entry-bar helpers on this engine's research module
    def bar_ok(self, bar: pd.Timestamp) -> bool:
        ok, _ = R2.entry_flags(pd.DatetimeIndex([bar]), self.timeframe, self.p, self.cfg)
        return bool(ok[0])

    def bar_flat(self, bar: pd.Timestamp) -> bool:
        _, flat = R2.entry_flags(pd.DatetimeIndex([bar]), self.timeframe, self.p, self.cfg)
        return bool(flat[0])

    def bar_eth(self, bar: pd.Timestamp) -> bool:
        return bool(R2.eth_bars(pd.DatetimeIndex([bar]))[0])
