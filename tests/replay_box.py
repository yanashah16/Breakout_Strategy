"""
Box (box2) parity replay: the live Deployment + BoxTrade vs research/box2_backtest.py simulate().

    python tests/replay_box.py dow-box-breakout 2026-01-01 2026-10-14
    python tests/replay_box.py all 2026-01-01 2026-10-14 [--window 15]

Same unadjusted 1-minute data on both sides, no slippage / commission, the backtest's IV files. The live side is a
real Deployment in practice mode fed minute by minute like the engine. Compared per trade: signal bar (a reverse:
its stop minute), side, entry, and each leg's exit minute, reason and price.
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
from desk.logic import box as BX  # noqa: E402
from desk.logic.base import BarContext  # noqa: E402

B = BX.B
BOX2 = ROOT.parent / "nq_wae_backtest" / "box2" / "data"
B.BIG = BOX2
B.IVDIR = BOX2 / "iv"
BX.iv_file = lambda name: None            # the backtest's own IV files, no download
BX.IV_DIR = BOX2 / "iv"
CODE = {0: "tp1", 1: "tp2", 2: "tp3", 3: "stop", 4: "be", 5: "trail", 6: "flat", 7: "open", 8: "ema", 9: "hold"}
NAN = float("nan")
BAR_EXITS = ("hold", "ema", "flat")
TF = [5]


class FakeHub:
    def order_key(self, ref, account=None):
        return f"replay_{ref}"


class Settings:
    dry_run = True
    entry_wait_seconds = 60
    entry_max_age_minutes = 0
    entry_max_chase_r = 0


def load_m1(inst, start, end, warm_days):
    m1 = B.load_big(inst, False)
    lo = start - pd.Timedelta(days=warm_days)
    return m1[(m1.t >= lo) & (m1.t < end + pd.Timedelta(days=1))].reset_index(drop=True)


def reference(logic, m1, start):
    p = logic.p
    inst = logic.inst
    cfg = {"data": {"instrument": inst}, "backtest": {"start": str(start.tz_convert("America/New_York").date())},
           "costs": {"commission": 0, "micro_commission": 0, "point_value": B.SPECS[inst][0], "tick": B.SPECS[inst][1],
                     "slippage_ticks": 0}}
    tf = logic.timeframe
    t, o, h, l, c, bstart = B.chart_bars(m1, tf)
    v = np.add.reduceat(m1.volume.to_numpy(float), bstart[:-1])
    ok, flat = B.bar_flags(t, tf, p["flat_time"], cfg)
    tg = pd.DataFrame([p])
    tables, arrays, vr, iv, vmin, extra = B.build(tg, h, l, c, v, p["box_edges"])
    eok, er = B.hours_ok(t, p["skip_hours"]), B.trend_er(t, c, int(p["er_days"]))
    ivl, ivr = B.implied_vol(t, inst, int(p["ivr_days"]))
    FL, FS, fx = B.filter_arrays(tg, t, o, h, l, c, v, tf, ivl)
    extra = extra + fx
    qty = tg[B.QCOLS].to_numpy(float)
    mo, mh, ml, mc = (m1[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    st, T, E = B.simulate(c, bstart, ok, flat, mo, mh, ml, mc, B.bar_path(bstart, mh, ml, mc), *tables,
                          *[a[0] for a in arrays], vr, iv[0], vmin[0], eok, er, ivl, ivr, FL, FS, *[a[0] for a in extra],
                          qty, 0.0, 0.0, float(cfg["costs"]["point_value"]), float(cfg["costs"]["tick"]), 0.0, True)
    rev_rows = {int(e[0]) for e in E if e[4] > 0}
    out = {}
    mt = m1.t
    for r in T:
        er_ = int(r[0])
        rev = er_ in rev_rows
        key_t = mt.iloc[er_] if rev else bucket(mt.iloc[er_], tf)
        k = (pd.Timestamp(key_t).value, int(r[2]))
        tr = out.setdefault(k, {"t": key_t, "side": int(r[2]), "entry": float(r[3]), "rev": rev, "legs": {}})
        tr["legs"][int(r[7]) + 1] = (mt.iloc[int(r[1])], CODE[int(r[6])], float(r[4]))
    cfg["backtest"]["start"] = str(start.tz_convert("America/New_York").date())
    return out, cfg


class ReplayLogic:
    def __init__(self, logic, arrays):
        self.lg, self.a = logic, arrays
        self.index = {pd.Timestamp(x).value: k for k, x in enumerate(arrays["t"])}

    def __getattr__(self, k):
        return getattr(self.lg, k)

    def _j(self, ctx):
        return self.index.get(pd.Timestamp(ctx.shared["bar_open"]).value)

    def on_bar_close(self, ctx):
        j = self._j(ctx)
        return [] if j is None or j == 0 else self.lg.plans_at(self.a, j)

    def bar_values(self, ctx):
        j = self._j(ctx)
        if j is None:
            return {}
        k = self.a["k"]
        il2, s_il2 = int(k["il2"]), int(k["s_il2"])
        return {"ema_long": float(self.a["EMAS"][il2, j]), "ema_short": float(self.a["EMAS"][il2 if s_il2 < 0 else s_il2, j])}


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
    tf = logic.timeframe

    def item(tr, m):
        k = pd.Timestamp(tr.skip_minute) if tr.is_rev else pd.Timestamp(m["signal_time"]).tz_convert("UTC")
        legs = {e["leg"]: (pd.Timestamp(e["time"]), e["reason"], e["level"]) for e in tr.exits}
        return {"t": k, "side": tr.side, "entry": m.get("entry_fill"), "rev": tr.is_rev, "legs": legs}

    d._journal_close = lambda: done.append(item(d.trade, d.meta))
    contract = (1, "209912")
    rows = m1[m1.t >= start - pd.Timedelta(days=1)].reset_index(drop=True)
    ob, closed, prev_close = None, None, None
    for row in rows.itertuples(index=False):
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
        x = item(d.trade, d.meta)
        for k in range(3):
            if d.trade.open[k] and d.trade.q[k] > 0:
                x["legs"][k + 1] = (rows.t.iloc[-1], "open", NAN)
        done.append(x)
    return {(pd.Timestamp(x["t"]).value, x["side"]): x for x in done}


def compare(ref, got, tick, qty):
    ok, bad = 0, []
    for k in sorted(set(ref) | set(got)):
        r, g = ref.get(k), got.get(k)
        if r is None or g is None:
            bad.append(("only in " + ("live" if r is None else "backtest"), r or g))
            continue
        why = []
        if g["entry"] is not None and abs(r["entry"] - g["entry"]) > tick / 2:
            why.append(f"entry {r['entry']} vs {g['entry']}")
        for leg in (1, 2, 3):
            if qty[leg - 1] <= 0:
                continue
            a, b = r["legs"].get(leg), g["legs"].get(leg)
            if a is None or b is None:
                why.append(f"leg {leg}: {a} vs {b}")
                continue
            if a[1] == "open" and b[1] == "open":
                continue
            ta, tb = pd.Timestamp(a[0]), pd.Timestamp(b[0])
            if a[1] in BAR_EXITS:              # bar-close exits: backtest = the bar's last minute, live = the bar's close
                ta, tb = bucket(ta, TF[0]), bucket(tb - pd.Timedelta(minutes=1), TF[0])
            if ta != tb or a[1] != b[1] or abs(a[2] - b[2]) > tick / 2:
                why.append(f"leg {leg}: {a[0]:%m-%d %H:%M} {a[1]} {a[2]:g} vs {pd.Timestamp(b[0]):%m-%d %H:%M} {b[1]} {b[2]:g}")
        if why:
            bad.append(("; ".join(why), r))
        else:
            ok += 1
    return ok, bad


def window_check(logic, m1, arrays, n, days=90, seed=1):
    t = arrays["t"]
    has = [j for j in range(1, len(t)) if logic.plans_at(arrays, j)]
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
    TF[0] = logic.timeframe
    m1 = load_m1(card["signal"], start, end, 90)
    t0 = time.time()
    ref, cfg = reference(logic, m1, start)
    arrays = logic.arrays(m1, {"backtest": {"start": cfg["backtest"]["start"]}, "data": {"instrument": logic.inst}})
    with tempfile.TemporaryDirectory() as td:
        got = live(card, logic, arrays, m1, start, Path(td))
    n_ok, bad = compare(ref, got, markets.get(card["signal"]).tick, card["default_qty"])
    res = {"id": card["id"], "backtest": len(ref), "live": len(got), "match": n_ok, "diff": len(bad),
           "secs": round(time.time() - t0, 1)}
    if window_n:
        s, n, diffs = window_check(logic, m1, logic.arrays(m1), window_n)
        res["window"] = f"{s}/{n}"
        for x in diffs[:3]:
            print("   window diff", x)
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
    ids = sorted(k for k, c in cards.items() if c.get("engine") == "box") if a.card == "all" else [a.card]
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
