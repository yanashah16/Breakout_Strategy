"""
Backtest parity replay.

    python tests/replay.py sp-spike-flag 2026-06-01 2026-10-14        # one strategy
    python tests/replay.py all 2026-08-01 2026-10-14                   # every card

1. Reference: the research simulate() (run_one) on the research 1-minute data (unadjusted).
2. Live: a real Deployment (practice mode, fake hub) fed the same minutes one by one, exactly as the
   engine feeds it: on_minute for every minute, on_bar_close at every chart-bar close.
   Signals come from the same arrays (the no-look-ahead check of the rolling window is `--window`).
3. Every trade is compared: entry bar, side, entry price, stop, and each leg's exit bar and reason.

--window N: also checks N random bar closes with the LIVE rolling 90-day window (warm-up effects).
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
from desk.logic.base import BarContext  # noqa: E402
from desk.logic.patterns import adapter as A  # noqa: E402
from desk.logic.patterns import lines as LN  # noqa: E402
from desk.trade import Trade  # noqa: E402

DATA = ROOT.parent / "nq_wae_backtest" / "data"
CODE = {1: "stop", 2: "tp1", 3: "be", 4: "time", 5: "flat", 6: "open", 7: "trail", 8: "tp2"}


def research(logic):
    """(research module, LiveMarket class) of the card's engine"""
    return (LN.R2, LN.LinesMarket) if isinstance(logic, LN.LinesLogic) else (A.R, A.LiveMarket)


def load_m1(inst: str, start: pd.Timestamp, end: pd.Timestamp, warm_days: int) -> pd.DataFrame:
    import box_backtest as B
    B.BIG = DATA
    m1 = B.load_big(inst, False)                       # unadjusted, like the live feed
    lo = start - pd.Timedelta(days=warm_days)
    m1 = m1[(m1.t >= lo) & (m1.t < end + pd.Timedelta(days=1))].reset_index(drop=True)
    return m1


class FakeHub:
    def order_key(self, ref, account=None):
        return f"replay_{ref}"


class Settings:
    dry_run = True
    entry_wait_seconds = 60


class ReplayLogic:
    """The card's PatternLogic, with plans read from arrays computed once on the whole window."""

    def __init__(self, logic, arrays):
        self.lg, self.a = logic, arrays
        self.index = {pd.Timestamp(t).value: k for k, t in enumerate(arrays["t"])}

    def __getattr__(self, k):
        return getattr(self.lg, k)

    def on_bar_close(self, ctx):
        j = self.index.get(pd.Timestamp(ctx.shared["bar_open"]).value)
        return [] if j is None else self.lg.plans_at(self.a, j)


def reference(logic, mk, start):
    p = logic.p
    cfg = {"data": {"htf_anchor": logic.anchor_cfg}, "backtest": {"start": str(start.date())}, "costs": {"commission": 0}}
    _, T, _ = research(logic)[0].run_one(mk, p, cfg, True)
    t = mk.bars(logic.timeframe)[0]
    trades = {}
    for r in T:
        sig, ebar, xbar, side, entry, stop, _, _, _, why, _, leg = r[:12]
        k = (int(ebar), int(side))
        tr = trades.setdefault(k, {"bar": t[int(ebar)], "side": int(side), "entry": float(entry), "stop": float(stop), "legs": {}})
        tr["legs"][int(leg)] = (t[int(xbar)], CODE[int(why)])
    return sorted(trades.values(), key=lambda x: x["bar"])


def live(card, logic, mk, m1, start, tmp):
    logic.cfg["backtest"] = {"start": str(start.date())}      # entries from `start`, as the reference
    arrays = logic.arrays(mk)
    rl = ReplayLogic(logic, arrays)
    DEP.STATE = tmp
    DEP.JOURNAL = tmp / "journal.jsonl"
    mkt = markets.get(card["markets"][0])
    cfg = {"id": "replay", "strategy": card["id"], "market": mkt.symbol, "qty": card["default_qty"], "enabled": True,
           "risk_usd": 0}   # trade path only: risk sizing would skip / resize trades
    d = DEP.Deployment(cfg, card, rl, mkt, FakeHub(), Settings())
    d.path = tmp / f"{card['id']}.json"
    d.save = lambda: None                                      # no disk writes in the replay
    clock = {"t": None}
    d.now = lambda: clock["t"]
    done = []

    def entry_bar(m):                       # market entries (trendline) open at the signal bar's CLOSE
        t = pd.Timestamp(m["opened"]) - pd.Timedelta(minutes=logic.timeframe if logic.kind == 2 else 0)
        return bucket(t, logic.timeframe)

    def exit_bar(e):                        # bar-close exits (time / flat) carry the bar's CLOSE time
        t = pd.Timestamp(e["time"])
        if e["reason"] in ("time", "flat"):
            t -= pd.Timedelta(minutes=1)
        return bucket(t, logic.timeframe)

    def capture():
        m, tr = d.meta, d.trade
        legs = {e["leg"]: (exit_bar(e), e["reason"]) for e in m.get("exits") or []}
        done.append({"bar": entry_bar(m), "side": tr.side,
                     "entry": m["trigger"], "stop": tr.stop0, "legs": legs})
    d._journal_close = capture
    tf = logic.timeframe
    contract = (1, "209912")
    rows = m1[m1.t >= start - pd.Timedelta(days=2)].reset_index(drop=True)
    ob, closed = None, None
    for row in rows.itertuples(index=False):
        b = bucket(row.t, tf)
        if ob is not None and b > ob and closed != ob:          # a bar without its last minute
            clock["t"] = b
            d.on_bar_close(ob, float(prev_close), BarContext("X", None, None, {"bar_open": ob}), contract)
            closed = ob
        clock["t"] = row.t + pd.Timedelta(minutes=1)
        d.on_minute(row, contract)
        ob, prev_close = b, row.close
        if is_last_minute(row.t, tf):
            d.on_bar_close(b, float(row.close), BarContext("X", None, None, {"bar_open": b}), contract)
            closed = b
    if d.trade:                                                # still open at the end
        tr, m = d.trade, d.meta
        legs = {e["leg"]: (exit_bar(e), e["reason"]) for e in m.get("exits") or []}
        for k in (1, 2):
            if tr.open[k - 1]:
                legs[k] = (bucket(rows.t.iloc[-1], tf), "open")
        done.append({"bar": entry_bar(m), "side": tr.side, "entry": m["trigger"],
                     "stop": tr.stop0, "legs": legs})
    return done, arrays


def compare(ref, got, tick):
    n_ok, bad = 0, []
    R = {(pd.Timestamp(x["bar"]).value, x["side"]): x for x in ref}
    G = {(pd.Timestamp(x["bar"]).value, x["side"]): x for x in got}
    for k in sorted(set(R) | set(G)):
        r, g = R.get(k), G.get(k)
        if r is None or g is None:
            bad.append(("only in " + ("live" if r is None else "backtest"), r or g))
            continue
        why = []
        if abs(r["entry"] - g["entry"]) > tick / 2:
            why.append(f"entry {r['entry']} vs {g['entry']}")
        if abs(r["stop"] - g["stop"]) > tick / 2:
            why.append(f"stop {r['stop']:.4f} vs {g['stop']:.4f}")
        for leg in sorted(set(r["legs"]) | set(g["legs"])):
            a, b = r["legs"].get(leg), g["legs"].get(leg)
            if a is None or b is None or pd.Timestamp(a[0]) != pd.Timestamp(b[0]) or a[1] != b[1]:
                why.append(f"leg {leg}: {a} vs {b}")
        if why:
            bad.append(("; ".join(why), r))
        else:
            n_ok += 1
    return n_ok, bad


def window_check(logic, m1, arrays, n, inst, days=90, seed=1):
    """Plans with the live rolling window vs the full-history arrays at sampled bar closes."""
    t = arrays["t"]
    has = [j for j in range(len(t)) if not (np.isnan(arrays["LL"][j]) and np.isnan(arrays["SL"][j]))]
    rnd = random.Random(seed)
    pick = rnd.sample(has, min(n, len(has)))
    same = 0
    diffs = []
    for j in pick:
        end = t[j].tz_convert("UTC") + pd.Timedelta(minutes=logic.timeframe)
        w = m1[(m1.t < end) & (m1.t >= end - pd.Timedelta(days=days))]
        got = logic.on_bar_close(BarContext(inst, w.reset_index(drop=True), None, {}))
        want = logic.plans_at(arrays, j)
        key = lambda ps: sorted((p.side, round(p.level, 6), round(p.stop_ref, 6), p.anchor) for p in ps)  # noqa: E731
        if key(got) == key(want):
            same += 1
        else:
            diffs.append((str(t[j]), key(want), key(got)))
    return same, len(pick), diffs


def run(card, start, end, window_n=0, verbose=True):
    logic = L.make(card)
    inst = card["signal"]
    m1 = load_m1(inst, start, end, 45)
    mk = research(logic)[1](inst, m1, logic.anchor_cfg)
    t0 = time.time()
    ref = reference(logic, mk, start)
    Trade.STRICT_ENTRY_MINUTE = True
    with tempfile.TemporaryDirectory() as td:
        got, arrays = live(card, logic, mk, m1, start, Path(td))
    n_ok, bad = compare(ref, got, markets.get(inst).tick)
    res = {"id": card["id"], "backtest": len(ref), "live": len(got), "match": n_ok, "diff": len(bad), "secs": round(time.time() - t0, 1)}
    if window_n:
        s, n, diffs = window_check(logic, m1, arrays, window_n, inst)
        res["window"] = f"{s}/{n}"
        if verbose and diffs:
            for d in diffs[:3]:
                print("   window diff", d)
    if verbose:
        print(res)
        for why, x in bad[:8]:
            print(f"   {pd.Timestamp(x['bar']).tz_convert('America/New_York'):%Y-%m-%d %H:%M} "
                  f"{'L' if x['side'] > 0 else 'S'}: {why}")
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("card")
    ap.add_argument("start")
    ap.add_argument("end")
    ap.add_argument("--window", type=int, default=0)
    a = ap.parse_args()
    cards, _ = catalog.load()
    ids = sorted(k for k, c in cards.items() if c.get("engine", "patterns") in ("patterns", "lines")) if a.card == "all" else [a.card]
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
