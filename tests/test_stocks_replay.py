"""
Parity: desk/stocks/core.py (live rules) vs the backtest's simulate() on the same bars.

    python tests/test_stocks_replay.py [SYMBOL ...]     (default TSLA JPM XOM, both copies)

Needs the research repo next to this one (..\\nq_wae_backtest: box/box_backtest.py + data/<SYM>_5min.parquet).
Costs are set to 0 in both, so every leg must match exactly: side, leg, entry / exit price, reason, times.
Leg sizes are not compared (the backtest keeps fractional volume-tier shares, live rounds to whole shares).
"""
from __future__ import annotations

import copy
import io
import contextlib
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT.parent / "nq_wae_backtest"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(RESEARCH / "box"))

from desk.stocks import core as C            # noqa: E402
from desk.stocks.copies import COPIES        # noqa: E402

REASON_BT = {"tp1": "TP1", "tp3": "TP3", "stop": "Stop loss", "be": "BE stop", "tp1_be": "BE stop",
             "trail": "Trail stop", "ema": "EMA exit", "stall": "Stall exit"}


def backtest(sym: str, params: dict) -> pd.DataFrame:
    import box_backtest as bb
    cfg = bb.load_cfg(RESEARCH / "box" / "box_stocks_params.yaml")
    cfg["data"]["instrument"] = sym
    cfg["costs"] = {"slippage_ticks": 0, "commission": 0, "micro_commission": 0, "point_value": 1, "tick": 0.01}
    bb.SPECS.setdefault(sym, (1.0, 0.01))
    m1 = rth(bb.load_big(sym, False))
    p = {k: bb.values(cfg["strategy"].get(k, v))[0] for k, v in bb.DEFAULTS.items()}
    p.update({k: v for k, v in params.items() if k in bb.DEFAULTS})
    p["risk_trade_usd"], p["size_step"], p["max_mult"] = params.get("risk_trade_usd", 1000), 1.0, 1e9
    out = Path(__file__).parent / "_replay_out"
    out.mkdir(exist_ok=True)
    bb.RESULTS = out
    with contextlib.redirect_stdout(io.StringIO()):
        bb.run_single(m1, *(m1[k].to_numpy() for k in ("open", "high", "low", "close")), p, cfg, f"{sym}_replay")
    t = pd.read_csv(out / f"box_{sym}_replay_trades.csv")
    return t[t.exit != "Open"].reset_index(drop=True), m1


def rth(m1: pd.DataFrame) -> pd.DataFrame:
    mny = m1.t.dt.tz_convert("America/New_York")
    mm = (mny.dt.hour * 60 + mny.dt.minute).to_numpy()
    return m1[(mm >= 570) & (mm < 960)].reset_index(drop=True)


def live(m1: pd.DataFrame, params: dict) -> pd.DataFrame:
    p = C.Params({k: v for k, v in params.items() if k in C.DEFAULTS})
    core = C.StockBoxCore(p)
    tf = p.tf
    tny = m1.t.dt.tz_convert("America/New_York").dt.tz_localize(None)
    minute = ((m1.t - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(minutes=1)).to_numpy(np.int64)
    bucket = minute - minute % tf
    o, h, l, c, v = (m1[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume"))
    times = tny.dt.strftime("%Y-%m-%d %H:%M").to_numpy()
    tlist = tny.to_list()
    start, end = pd.Timestamp("2016-11-01"), pd.Timestamp("2026-10-30")     # backtest start / end (+1 day)
    n = len(m1)
    i = 0
    day, dh, dl, dc = None, -np.inf, np.inf, np.nan
    while i < n:
        j = i
        while j + 1 < n and bucket[j + 1] == bucket[i]:
            j += 1
        bt = pd.Timestamp(bucket[i], unit="m", tz="UTC").tz_convert("America/New_York").tz_localize(None).to_pydatetime()
        d = bt.date()
        if day is not None and d != day:
            core.add_day(day, dh, dl, dc)
            dh, dl = -np.inf, np.inf
        day = d
        for k in range(i, j + 1):
            core.on_minute(C.Bar(tlist[k].to_pydatetime(), o[k], h[k], l[k], c[k], v[k]), now=times[k])
        bar = C.Bar(bt, o[i], h[i:j + 1].max(), l[i:j + 1].min(), c[j], v[i:j + 1].sum())
        dh, dl, dc = max(dh, bar.h), min(dl, bar.l), bar.c
        core.on_bar_close(bar, now=times[j], allow_entry=start <= bt < end and j + 1 < n)
        i = j + 1
    return pd.DataFrame(core.closed)


def compare(sym: str, name: str, params: dict) -> bool:
    bt, m1 = backtest(sym, params)
    lv = live(m1, params)
    a = pd.DataFrame({"entry_time": bt.entry_time, "exit_time": bt.exit_time, "side": np.where(bt.side == "Long", 1, -1),
                      "leg": bt.leg, "entry": bt.entry_price, "exit": bt.exit_price, "why": bt.exit})
    b = pd.DataFrame({"entry_time": lv.entry_time, "exit_time": lv.exit_time, "side": lv.side, "leg": lv.leg,
                      "entry": lv.entry_price, "exit": lv.exit_price, "why": lv.exit.map(REASON_BT)})
    key = ["entry_time", "side", "leg"]
    a, b = a.sort_values(key).reset_index(drop=True), b.sort_values(key).reset_index(drop=True)
    cols = ["entry_time", "exit_time", "side", "leg", "why"]
    same = (len(a) == len(b) and (a[cols].values == b[cols].values).all()
            and np.allclose(a[["entry", "exit"]].to_numpy(float), b[["entry", "exit"]].to_numpy(float), atol=1e-6))
    print(f"{sym} {name}: backtest {len(a)} legs, live {len(b)} legs -> {'IDENTICAL' if same else 'DIFFERENT'}")
    if not same:
        m = a.merge(b, on=key, how="outer", suffixes=("_bt", "_live"), indicator=True)
        bad = m[(m._merge != "both") | ((m.exit_bt - m.exit_live).abs() > 1e-6) | (m.why_bt != m.why_live) | (m.exit_time_bt != m.exit_time_live)]
        print(bad.head(12).to_string())
    return same


if __name__ == "__main__":
    syms = sys.argv[1:] or ["TSLA", "JPM", "XOM"]
    ok = all([compare(s, name, cp["params"]) for s in syms for name, cp in COPIES.items()])
    print("ALL IDENTICAL" if ok else "MISMATCH")
    sys.exit(0 if ok else 1)
