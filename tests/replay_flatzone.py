"""
Flat zone parity replay: the live Deployment + FlatTrade vs research/flatzone_backtest.py simulate().

    python tests/replay_flatzone.py nq-flatzone-15m 2025-06-01 2026-10-14
    python tests/replay_flatzone.py all 2026-01-01 2026-10-14 [--window 20]

Both sides use the same unadjusted 1-minute data, no slippage / commission. The live side is a real Deployment in
practice mode, fed minute by minute like the engine (on_minute every minute, on_bar_close at each bar close); its
entry (plan kind "open") fills at the open of the first minute after the signal bar, as the backtest's. Every trade is compared: entry minute, side, stop,
and each leg's exit minute, reason and price. --window N also checks N signals with the live rolling window.
"""
from __future__ import annotations

import argparse
import random
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desk import catalog, markets  # noqa: E402
from desk import deployment as DEP  # noqa: E402
from desk import logic as L  # noqa: E402
from desk.bars import bucket, is_last_minute  # noqa: E402
from desk.logic import flatzone as FZ  # noqa: E402
from desk.logic.base import BarContext  # noqa: E402

DATA = ROOT.parent / "nq_wae_backtest" / "data"
CODE = {0: "tp1", 1: "stop", 2: "be", 3: "trail", 4: "open", 5: "tp2", 6: "lock", 7: "time"}


class FakeHub:
    def order_key(self, ref, account=None):
        return f"replay_{ref}"


class Settings:
    dry_run = True
    entry_wait_seconds = 60
    entry_max_age_minutes = 0
    entry_max_chase_r = 0


def load_m1(inst, start, end, warm_days):
    import box_backtest as B
    B.BIG = DATA
    m1 = B.load_big(inst, False)
    lo = start - pd.Timedelta(days=warm_days)
    return m1[(m1.t >= lo) & (m1.t < end + pd.Timedelta(days=1))].reset_index(drop=True)


def reference(logic, m1, start):
    cfg = {"data": {"instrument": logic.card["signal"]}, "backtest": {"start": str(start.tz_convert('America/New_York').date())},
           "costs": {"commission": 0, "point_value": 1.0, "slippage_ticks": 0, "tick": 0.25}}
    p = dict(logic.p, qty_tiers=None)
    bars = FZ.F.Bars(m1, cfg)
    _, T, _ = FZ.F.run(bars, p, cfg, True)
    t = m1.t
    out = {}
    for r in T:
        k = (pd.Timestamp(t.iloc[int(r[0])]).value, int(r[2]))
        tr = out.setdefault(k, {"t": t.iloc[int(r[0])], "side": int(r[2]), "entry": float(r[3]), "rev": bool(r[9]), "legs": {}})
        tr["legs"][int(r[7])] = (t.iloc[int(r[1])], CODE[int(r[6])], float(r[4]))
    return out, bars, cfg


class ReplayLogic:
    def __init__(self, logic, arrays):
        self.lg, self.a = logic, arrays
        self.index = {pd.Timestamp(x).value: k for k, x in enumerate(arrays["t"])}

    def __getattr__(self, k):
        return getattr(self.lg, k)

    def on_bar_close(self, ctx):
        j = self.index.get(pd.Timestamp(ctx.shared["bar_open"]).value)
        return [] if j is None else self.lg.plans_at(self.a, j)


def live(card, logic, arrays, m1, start, tmp):
    DEP.STATE = tmp
    DEP.JOURNAL = tmp / "journal.jsonl"
    mkt = markets.get(card["markets"][-1])
    cfg = {"id": "replay", "strategy": card["id"], "market": mkt.symbol, "qty": card["default_qty"], "enabled": True}
    d = DEP.Deployment(cfg, card, ReplayLogic(logic, arrays), mkt, FakeHub(), Settings())
    d.save = lambda: None
    clock = {"t": None}
    d.now = lambda: clock["t"]
    done = []

    def rec():
        tr, m = d.trade, d.meta
        k = tr.skip_minute or tr.entry_minute
        legs = {e["leg"]: (pd.Timestamp(e["time"]), e["reason"], e["level"]) for e in tr.exits}
        done.append({"t": pd.Timestamp(k), "side": tr.side, "entry": m.get("entry_fill"), "rev": tr.is_rev, "legs": legs})
    d._journal_close = rec
    tf = logic.timeframe
    contract = (1, "209912")
    rows = m1[m1.t >= start - pd.Timedelta(days=1)].reset_index(drop=True)
    opens = rows.open.to_numpy()
    ob, closed, prev_close = None, None, None
    for i, row in enumerate(rows.itertuples(index=False)):
        b = bucket(row.t, tf)
        if ob is not None and b > ob and closed != ob:
            clock["t"] = b
            d.on_bar_close(ob, float(prev_close), BarContext("X", None, None, {"bar_open": ob}), contract)
            closed = ob
        clock["t"] = row.t + pd.Timedelta(minutes=1)
        d.on_minute(row, contract)
        ob, prev_close = b, row.close
        if is_last_minute(row.t, tf):
            d.on_bar_close(b, float(row.close), BarContext("X", None, None, {"bar_open": b}), contract)
            closed = b
    if d.trade:
        tr = d.trade
        legs = {e["leg"]: (pd.Timestamp(e["time"]), e["reason"], e["level"]) for e in tr.exits}
        for k in (1, 2):
            if tr.open[k - 1]:
                legs[k] = (rows.t.iloc[-1], "open", NAN)
        done.append({"t": pd.Timestamp(tr.skip_minute or tr.entry_minute), "side": tr.side,
                     "entry": d.meta.get("entry_fill"), "rev": tr.is_rev, "legs": legs})
    return {(x["t"].value, x["side"]): x for x in done}


NAN = float("nan")


def compare(ref, got, tick):
    ok, bad = 0, []
    for k in sorted(set(ref) | set(got)):
        r, g = ref.get(k), got.get(k)
        if r is None or g is None:
            bad.append(("only in " + ("live" if r is None else "backtest"), r or g))
            continue
        why = []
        if g["entry"] is not None and abs(r["entry"] - g["entry"]) > tick / 2 and not r["rev"]:
            why.append(f"entry {r['entry']} vs {g['entry']}")
        for leg in sorted(set(r["legs"]) | set(g["legs"])):
            a, b = r["legs"].get(leg), g["legs"].get(leg)
            if a is None or b is None:
                why.append(f"leg {leg}: {a} vs {b}")
                continue
            if a[1] == "open" and b[1] == "open":
                continue
            if pd.Timestamp(a[0]) != pd.Timestamp(b[0]) or a[1] != b[1] or (a[1] != "open" and abs(a[2] - b[2]) > tick / 2):
                why.append(f"leg {leg}: {a[0]:%m-%d %H:%M} {a[1]} {a[2]:g} vs {pd.Timestamp(b[0]):%m-%d %H:%M} {b[1]} {b[2]:g}")
        if why:
            bad.append(("; ".join(why), r))
        else:
            ok += 1
    return ok, bad


def window_check(logic, m1, arrays, n, days=90, seed=1):
    t = arrays["t"]
    has = [j for j in range(len(t)) if logic.plans_at(arrays, j)]
    pick = random.Random(seed).sample(has, min(n, len(has)))
    same, diffs = 0, []
    for j in pick:
        end = t[j].tz_convert("UTC") + pd.Timedelta(minutes=logic.timeframe)
        w = m1[(m1.t < end) & (m1.t >= end - pd.Timedelta(days=days))]
        got = logic.on_bar_close(BarContext("X", w.reset_index(drop=True), None, {}))
        want = logic.plans_at(arrays, j)
        key = lambda ps: sorted((p.side, round(p.level, 6), round(p.stop, 6), p.boost) for p in ps)  # noqa: E731
        if key(got) == key(want):
            same += 1
        else:
            diffs.append((str(t[j]), key(want), key(got)))
    return same, len(pick), diffs


def run(card, start, end, window_n=0):
    logic = L.make(card)
    m1 = load_m1(card["signal"], start, end, 60)
    t0 = time.time()
    ref, bars, cfg = reference(logic, m1, start)
    arrays = logic.arrays(bars)
    with tempfile.TemporaryDirectory() as td:
        got = live(card, logic, arrays, m1, start, Path(td))
    n_ok, bad = compare(ref, got, markets.get(card["signal"]).tick)
    res = {"id": card["id"], "backtest": len(ref), "live": len(got), "match": n_ok, "diff": len(bad),
           "secs": round(time.time() - t0, 1)}
    if window_n:
        s, n, diffs = window_check(logic, m1, arrays, window_n)
        res["window"] = f"{s}/{n}"
        for d in diffs[:3]:
            print("   window diff", d)
    print(res)
    for why, x in bad[:8]:
        print(f"   {pd.Timestamp(x['t']).tz_convert('America/New_York'):%Y-%m-%d %H:%M} {'L' if x['side'] > 0 else 'S'}"
              f"{' rev' if x.get('rev') else ''}: {why}")
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("card")
    ap.add_argument("start")
    ap.add_argument("end")
    ap.add_argument("--window", type=int, default=0)
    a = ap.parse_args()
    cards, _ = catalog.load()
    ids = sorted(k for k, c in cards.items() if c.get("engine") == "flatzone") if a.card == "all" else [a.card]
    start = pd.Timestamp(a.start, tz="America/New_York").tz_convert("UTC")
    end = pd.Timestamp(a.end, tz="America/New_York").tz_convert("UTC")
    out = [run(cards[i], start, end, a.window) for i in ids]
    if len(out) > 1:
        print(pd.DataFrame(out).to_string(index=False))
    bad = sum(r["diff"] for r in out)
    print("PARITY OK" if bad == 0 else f"PARITY: {bad} trade(s) differ")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
