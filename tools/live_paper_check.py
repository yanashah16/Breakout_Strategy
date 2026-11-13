"""
Real-order check on the PAPER account, through the real engine + hub (no fakes).

    python tools/live_paper_check.py                 # MES, 3 contracts (1 target + 2 runner)
    python tools/live_paper_check.py --market MNQ --qty 1 2

What it does, verifying the broker position after every step:
  1. entry          market entry of a test trade (the engine's own Deployment / order code)
  2. take profit    leg 1 closes (as when TP1 is reached)
  3. roll           the strategy's contract month is set to the next month -> the open position moves
                    there in one hub batch, the trade (stop, target, runner) carries on
  4. restart        a fresh engine restores the trade from disk (still in the new month)
  5. reconcile      the engine's broker check (per account) finds no mismatch
  6. close          the runner closes; the journal shows the trade with its roll
  7. bad account    the desk refuses an account this login does not manage, and blocks new entries
                    for a strategy whose account disappeared (no order is sent: on a one-account login
                    the hub would route it to the only account)

Runs with its own state folder (state/_paper_check) and order tag (pattern_desk_test), so it never
touches the desk's trades. It refuses to run unless the account is a paper account (DU...).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desk import deployment as DEP  # noqa: E402
from desk import engine as E  # noqa: E402
from desk import markets as mkts  # noqa: E402
from desk import settings as S  # noqa: E402
from desk.logic.base import Plan  # noqa: E402
from desk.store import read_jsonl  # noqa: E402

TEST_STATE = S.STATE / "_paper_check"


def say(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def fail(msg):
    say("FAIL: " + msg)
    sys.exit(1)


def broker_qty(hub, symbol, con_id, account):
    pos = hub.positions(symbol, account=account)
    if pos is None:
        return None
    for p in pos:
        if int(p.get("con_id") or 0) == int(con_id):
            a = [x for x in p.get("accounts") or [] if x.get("account") == account]
            return int(round(float(a[0]["qty"]))) if a else 0
    return 0


def wait(cond, secs, what):
    t0 = time.time()
    while time.time() - t0 < secs:
        if cond():
            return True
        time.sleep(1)
    fail(f"timed out waiting for {what}")


def make_engine(s, cfg):
    E.DEPLOYMENTS = TEST_STATE / "deployments.json"
    E.EVENTS = TEST_STATE / "events.jsonl"
    DEP.STATE = TEST_STATE
    DEP.JOURNAL = TEST_STATE / "journal.jsonl"
    from desk import rollrule
    rollrule.MONTHS_FILE = TEST_STATE / "contract_months.json"     # never the desk's own roll state
    rollrule.SETTINGS_FILE = TEST_STATE / "desk_settings.json"
    from desk.store import write_json
    write_json(E.DEPLOYMENTS, [cfg])
    eng = E.Engine(s)
    eng.start()
    return eng


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="MES")
    ap.add_argument("--strategy", default="sp-quick-bank-flag")
    ap.add_argument("--qty", nargs=2, type=int, default=[1, 2])
    ap.add_argument("--account", default=None)
    a = ap.parse_args()

    s = S.load()
    s.dry_run = False
    s.algo_id = "pattern_desk_test"
    s.warmup_days = 30
    account = a.account or s.account_id
    if not account.startswith("DU"):
        fail(f"{account} is not a paper account - this check only runs on paper (DU...)")
    TEST_STATE.mkdir(parents=True, exist_ok=True)
    for f in list(TEST_STATE.rglob("*.json")) + list(TEST_STATE.rglob("*.jsonl")):
        f.unlink()

    spec = mkts.get(a.market)
    cfg = {"id": f"paper-check@{a.market.lower()}", "strategy": a.strategy, "market": a.market,
           "qty": list(a.qty), "enabled": True, "stop_after_trade": False, "added": DEP.now_iso(),
           "expiry": "auto", "account": account}
    eng = make_engine(s, cfg)
    hub = eng.hub
    did = cfg["id"]

    # ---- 0. accounts + months
    acc = eng.accounts(max_age=0)
    if acc is None:
        fail("hub /accounts did not answer")
    say(f"accounts of this login: {acc.get('managed')}")
    if account not in acc.get("managed", []):
        fail(f"{account} is not managed by this login")
    months = eng.month_choices(a.market)
    say("contract months offered: " + ", ".join(m["label"] + (" (front)" if m["front"] else "") for m in months))
    wait(lambda: eng.contracts.get(a.market, (None,))[0], 60, "front-month contract")
    front_cid, front_exp = eng.contracts[a.market]
    nxt = [m["expiry"] for m in months if m["expiry"] > front_exp][0]
    say(f"front {mkts.label(a.market, front_exp)} = {front_cid}, next {mkts.label(a.market, nxt)}")
    wait(lambda: eng.px.get(front_cid), 60, "a live price")
    base_front = broker_qty(hub, spec.broker_symbol, front_cid, account)
    say(f"account {account} holds {base_front:+d} {mkts.label(a.market, front_exp)} before the test (not ours)")

    d = eng.deps[did]
    q = sum(a.qty)

    # ---- 1. entry
    with eng.lock:
        px = eng.px[front_cid]
        sig = d.signal_px or px
        p = Plan(side=1, kind="market", level=sig, anchor="paper-check", signal_time=DEP.now_iso(),
                 after=DEP.now_iso(), final=True, session=-1, stop=sig - 40 * spec.tick * 4, tp_r=3.0,
                 trail_r=1.0, tp2_r=0.0)
        d._enter(p, sig, DEP.now_iso(), (front_cid, front_exp))
        if not d.trade:
            fail(f"entry refused: {d.error}")
    say(f"1. ENTRY long {q} sent (stop {d.trade.stop0:g}, target {d.trade.tp1:g})")
    wait(lambda: d.held == q and not d.order, 90, "entry fill")
    b = broker_qty(hub, spec.broker_symbol, front_cid, account)
    say(f"   filled @ {d.meta['entry_fill']} - IBKR {b:+d} (= {base_front:+d} outside + {q} ours)")
    if b != base_front + q:
        fail("broker position does not match")

    # ---- 2. take profit on leg 1
    if a.qty[0]:
        with eng.lock:
            d.trade.tp1_done = True
            d._exits([d.trade._out(1, "tp1", d.trade.tp1, DEP.now_iso())])
        wait(lambda: d.held == a.qty[1] and not d.order, 90, "leg 1 exit")
        b = broker_qty(hub, spec.broker_symbol, front_cid, account)
        say(f"2. TAKE PROFIT leg 1 ({a.qty[0]}) filled - IBKR {b:+d}")
        if b != base_front + a.qty[1]:
            fail("broker position does not match after leg 1")
    left = a.qty[1] if a.qty[0] else q
    stop_before = list(d.trade.stop)

    # ---- 3. volume roll: the daily check reads both months' volume; then the market moves to the next
    #         month and the engine moves the open trade by itself (auto mode)
    from desk import describe
    st = eng.rolls.check(a.market, force=True)
    vols = st.get("volumes") or {}
    say(f"3. VOLUME CHECK {st.get('session')}: " + ", ".join(f"{mkts.label(a.market, m)} {v:,}" for m, v in vols.items())
        + f" -> {st.get('share')}x (ratio {eng.rolls.ratio:g}x) {st.get('error') or ''}")
    if not vols:
        fail("no per-month volume from the hub")
    before = describe.live_row(d, eng)
    with eng.lock:
        eng.rolls._set(a.market, nxt, "paper check: forced as if the next month had the volume", eng.rolls.now_et().date())
        eng._contract_day.pop(a.market, None)
    say(f"   market switched to {mkts.label(a.market, nxt)} - waiting for the engine to move the trade")
    wait(lambda: d.pos_expiry == nxt and not d.roll, 180, "the automatic roll")
    after = describe.live_row(d, eng)
    fl = lambda x: float(str(x).replace(",", ""))                   # noqa: E731
    sp = d.meta["rolls"][-1]["spread"]
    say(f"   Live page before: entry {before['entry']} stop {before['stop']} target {before['target']} ({before['contract']})")
    say(f"   Live page after : entry {after['entry']} stop {after['stop']} target {after['target']} ({after['contract']})")
    moved = [abs((fl(after[k]) - fl(before[k])) - sp) <= 1e-6 for k in ("stop", "target", "entry") if before.get(k) and after.get(k)]
    if len(moved) < 2 or not all(moved):
        fail("stop / target shown were not moved by the spread")
    say(f"   stop and target moved by the spread {sp:+g} (distances unchanged)")
    new_cid = d.pos_con_id
    b_old = broker_qty(hub, spec.broker_symbol, front_cid, account)
    b_new = broker_qty(hub, spec.broker_symbol, new_cid, account)
    r = d.meta["rolls"][-1]
    say(f"   rolled {r['qty']} {r['from']} @ {r['close_px']} -> {r['to']} @ {r['open_px']} (spread {r['spread']:+g})")
    say(f"   IBKR {mkts.label(a.market, front_exp)} {b_old:+d}, {mkts.label(a.market, nxt)} {b_new:+d}")
    if b_old != base_front or b_new < left:
        fail("broker positions after the roll do not match")
    if d.trade.stop != stop_before or not d.trade.is_open or d.held != left:
        fail("the trade changed during the roll")
    say(f"   trade unchanged: stop {d.trade.stop}, held {d.held:+d}, still open")

    # ---- 4. restart: a new engine restores from disk
    eng.stop()
    time.sleep(3)
    eng2 = E.Engine(s)
    d2 = eng2.deps[did]
    if not (d2.trade and d2.held == left and d2.pos_expiry == nxt and d2.pos_account == account):
        fail(f"restore: held {d2.held}, month {d2.pos_expiry}, account {d2.pos_account}")
    say(f"4. RESTART: restored {d2.held:+d} {d2.meta.get('contract')} in {d2.pos_account}, roll history kept")
    eng2.start()
    eng = eng2
    d = d2

    # ---- 5. broker check (per account)
    wait(lambda: eng.px.get(int(d.pos_con_id)), 60, "a price for the new month")
    with eng.lock:
        eng._mismatch.clear()
        eng._reconcile(eng.active())
        eng._reconcile(eng.active())
    if d.attention:
        fail("reconcile raised: " + d.attention)
    say(f"5. RECONCILE: no mismatch (outside contracts seen: {eng._outside})")

    # ---- 6. close
    with eng.lock:
        d.cfg["enabled"] = False                     # no real signal may open a trade in the test state
        d.close_now("manual")
    wait(lambda: d.held == 0 and d.trade is None and not d.order, 90, "the close")
    b_new = broker_qty(hub, spec.broker_symbol, new_cid, account)
    row = read_jsonl(DEP.JOURNAL)[-1]
    say(f"6. CLOSED - IBKR {mkts.label(a.market, nxt)} {b_new:+d}; journal: {row['result']}, "
        f"P&L ${row['pnl']:,.2f}, contract {row['contract']}, account {row['account']}, rolls {len(row['rolls'])}")
    if b_new != 0:
        fail("the new month is not flat")

    # ---- 7. an account this login does not manage
    bad = "DU0000000"
    with eng.lock:
        try:
            eng.set_account(did, bad)
            fail("set_account accepted an unknown account")
        except ValueError as exc:
            say(f"7. BAD ACCOUNT: desk refuses it: {exc}")
        d.cfg["account"], d.cfg["enabled"] = bad, True    # as if the account vanished from the login
        eng._check_accounts([d])
        p = Plan(side=1, kind="market", level=eng.px[int(d.pos_con_id or new_cid)], anchor="bad", signal_time=DEP.now_iso(),
                 after=DEP.now_iso(), final=True, session=-2, stop=eng.px[int(new_cid)] - 50, tp_r=3.0)
        d._enter(p, p.level, DEP.now_iso(), (new_cid, nxt))
        d.cfg["enabled"] = False
        if d.trade:
            fail("an entry was opened for an unknown account")
        say(f"   entry blocked: {d.account_bad}")
    eng.stop()
    say("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
