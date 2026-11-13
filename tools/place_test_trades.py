"""
PAPER ONLY: open one trade per switched-on strategy, at market, through the desk's own code, into the
desk's REAL state - so the desk (started afterwards) restores and manages them like any other trade.

    1. stop Pattern Desk      2. python tools/place_test_trades.py      3. start Pattern Desk

Each trade: the strategy's own quantities (YAML / row) and exit rules (target R, runner, trail,
break-even); the stop sits a fixed distance away (STOP below). Sides alternate long / short.
Refuses to run unless the account is a paper account (DU...) and the desk is not running.
"""
from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desk import deployment as DEP  # noqa: E402
from desk import engine as E  # noqa: E402
from desk import settings as S  # noqa: E402
from desk.logic.base import Plan  # noqa: E402

STOP = {"ES": 12.0, "NQ": 50.0, "GC": 10.0, "CL": 0.40}      # stop distance, signal-market points


def main():
    s = S.load()
    if not s.account_id.startswith("DU"):
        sys.exit(f"{s.account_id} is not a paper account - refusing")
    with socket.socket() as k:
        if k.connect_ex((s.host, s.port)) == 0:
            sys.exit("Pattern Desk is running - stop it first")
    s.dry_run = False
    eng = E.Engine(s)
    deps = [d for d in eng.deps.values() if d.enabled and not (d.trade or d.held or d.order)]
    eng.start()
    t0 = time.time()
    while time.time() - t0 < 60:
        if all(eng.px.get((eng._contract_for(d)[0] or 0)) for d in deps):
            break
        time.sleep(1)
    side = 1
    for d in deps:
        with eng.lock:
            cid, exp = eng._contract_for(d)
            px = eng.px.get(cid or 0)
            if not px:
                print(f"SKIP {d.name}: no price for {d.market.symbol}")
                continue
            lg = d.logic.p
            stop = px - side * STOP[d.market.signal_symbol]
            p = Plan(side=side, kind="market", level=px, anchor=f"test-{DEP.now_iso()}", signal_time=DEP.now_iso(),
                     after=DEP.now_iso(), final=True, session=-9, stop=stop, tp_r=float(lg.get("tp_r") or 2.0),
                     trail_r=float(lg.get("trail_r") or 1.0), tp2_r=float(lg.get("tp2_r") or 0.0))
            d._enter(p, px, DEP.now_iso(), (cid, exp))
            if d.trade:
                print(f"ENTER {'LONG ' if side > 0 else 'SHORT'} {sum(d.trade.q)} {d.meta['contract']} {d.name}: "
                      f"stop {d.trade.stop0:g}, target {d.trade.tp1:g}")
            else:
                print(f"SKIP {d.name}: {d.error}")
        side = -side
    t0 = time.time()
    while time.time() - t0 < 120 and any(d.order or (d.trade and d.held == 0) for d in deps):
        time.sleep(1)
    eng.running = False
    time.sleep(3)
    for d in deps:
        if d.trade:
            print(f"  {d.name}: holding {d.held:+d} {d.meta.get('contract')} @ {d.meta.get('entry_fill')}")
        d.save()


if __name__ == "__main__":
    main()
