"""
Parity: desk/stocks/flag_core.py (live rules) vs the research backtest (patterns_backtest.py, setup flag) on the same bars.

    python tests/test_stocks_flag_replay.py [SYMBOL ...]     (default TSLA JPM XOM)

Needs the research repo next to this one (..\\nq_wae_backtest: patterns/patterns_backtest.py,
patterns/flag_stocks_10m/flag_stocks_10m_params.yaml, data/<SYM>_5min.parquet).
Costs are set to 0 in both, so every leg must match: entry bar, side, leg, entry / exit price, reason, exit bar.
Leg sizes are not compared (the backtest sizes fractional shares, live whole shares).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT.parent / "nq_wae_backtest"
YAML = RESEARCH / "patterns" / "flag_stocks_10m" / "flag_stocks_10m_params.yaml"
sys.path.insert(0, str(ROOT))

from desk.stocks import flag_core as F        # noqa: E402
from desk.stocks.copies import COPIES         # noqa: E402

REASON_BT = {"tp1": "TP1", "stop": "Stop", "tp1_be": "BE stop", "trail": "Trail stop"}


def _research():
    sys.path.insert(0, str(RESEARCH / "patterns"))
    import patterns_backtest as pb
    return pb


def backtest(sym: str):
    pb = _research()
    cfg = pb.load_cfg(YAML)
    cfg["data"]["instrument"] = sym
    cfg["costs"] = {"commission": 0, "slippage_ticks": 0}
    s = cfg["strategy"]
    allowed = {**pb.COMMON, **pb.SETUP_KEYS["flag"], **pb.FLAG_EXTRA}
    p = {k: s.get(k, v) for k, v in allowed.items()}
    mk = pb.Market(sym, cfg)
    st, T, sig = pb.run_one(mk, p, cfg, True)
    df = pb.trades_frame(mk, p, T)
    df = df[df.reason != "Open"].reset_index(drop=True)
    t = mk.bars(int(p["timeframe"]))[0].tz_localize(None)
    df["entry_bar"] = t[df.ebar]
    return df, mk.m1, cfg


def live(m1: pd.DataFrame, params: dict, cfg: dict) -> pd.DataFrame:
    p = F.Params({k: v for k, v in params.items() if k in F.DEFAULTS})
    core = F.StockFlagCore(p)
    tf = p.tf
    tny = m1.t.dt.tz_convert("America/New_York").dt.tz_localize(None)
    minute = ((m1.t - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(minutes=1)).to_numpy(np.int64)
    bucket = minute - minute % tf
    o, h, l, c, v = (m1[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume"))
    times = tny.dt.strftime("%Y-%m-%d %H:%M").to_numpy()
    tlist = tny.to_list()
    bt_ = cfg.get("backtest") or {}
    start = pd.Timestamp(bt_.get("start", "2016-11-01"))
    end = pd.Timestamp(bt_.get("end", "2026-10-14")) + pd.Timedelta(days=1)
    n = len(m1)
    i = 0
    day, dh, dl, dc = None, -np.inf, np.inf, np.nan
    rows = []
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
            core.on_minute(F.Bar(tlist[k].to_pydatetime(), o[k], h[k], l[k], c[k], v[k]), now=times[k])
        bar = F.Bar(bt, o[i], h[i:j + 1].max(), l[i:j + 1].min(), c[j], v[i:j + 1].sum())
        dh, dl, dc = max(dh, bar.h), min(dl, bar.l), bar.c
        nxt = bt + pd.Timedelta(minutes=tf)
        core.on_bar_close(bar, now=times[j], allow_entry=start <= nxt < end and j + 1 < n)
        i = j + 1
    out = pd.DataFrame(core.closed)
    if len(out):
        et = pd.to_datetime(out.entry_time)
        out["entry_bar"] = et.dt.floor(f"{tf}min")
        out["exit_bar"] = pd.to_datetime(out.exit_time).dt.floor(f"{tf}min")
    return out


def compare(sym: str, params: dict) -> bool:
    bt, m1, cfg = backtest(sym)
    lv = live(m1, params, cfg)
    a = pd.DataFrame({"entry_bar": bt.entry_bar, "exit_bar": bt.exit_time, "side": np.where(bt.side == "Long", 1, -1),
                      "leg": bt.leg, "entry": bt.entry, "exit": bt.exit, "why": bt.reason})
    b = pd.DataFrame({"entry_bar": lv.entry_bar, "exit_bar": lv.exit_bar, "side": lv.side, "leg": lv.leg,
                      "entry": lv.entry_price, "exit": lv.exit_price, "why": lv.exit.map(REASON_BT)})
    key = ["entry_bar", "side", "leg"]
    a, b = a.sort_values(key).reset_index(drop=True), b.sort_values(key).reset_index(drop=True)
    cols = ["entry_bar", "exit_bar", "side", "leg", "why"]
    same = (len(a) == len(b) and (a[cols].values == b[cols].values).all()
            and np.allclose(a[["entry", "exit"]].to_numpy(float), b[["entry", "exit"]].to_numpy(float), atol=1e-6))
    print(f"{sym}: backtest {len(a)} legs, live {len(b)} legs -> {'IDENTICAL' if same else 'DIFFERENT'}", flush=True)
    if not same:
        m = a.merge(b, on=key, how="outer", suffixes=("_bt", "_live"), indicator=True)
        bad = m[(m._merge != "both") | ((m.exit_bt - m.exit_live).abs() > 1e-6) | ((m.entry_bt - m.entry_live).abs() > 1e-6)
                | (m.why_bt != m.why_live) | (m.exit_bar_bt != m.exit_bar_live)]
        print(f"  {len(bad)} rows differ; first ones:")
        print(bad.head(12).to_string())
    return same


if __name__ == "__main__":
    syms = sys.argv[1:] or ["TSLA", "JPM", "XOM"]
    params = COPIES["flag10"]["params"]
    ok = all([compare(s, params) for s in syms])
    print("ALL IDENTICAL" if ok else "MISMATCH")
    sys.exit(0 if ok else 1)
