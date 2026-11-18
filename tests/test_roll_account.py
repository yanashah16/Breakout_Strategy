"""Contract roll (move an open trade to another month) and per-strategy accounts, against a fake hub."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desk import deployment as DEP  # noqa: E402
from desk import markets  # noqa: E402
from desk.logic.base import ExitRules, Plan  # noqa: E402

OLD, NEW = 815824257, 900000001          # MESZ6, MESH7 (fake con_ids)


class Hub:
    """Records orders; fills them at the configured price unless told to reject."""

    def __init__(self):
        self.algo_id, self.account_id = "t", "DU_DEFAULT"
        self.sent, self.status, self.reject = [], {}, set()
        self.fill_px = {OLD: 7000.0, NEW: 7050.0}
        self.partial = {}

    def order_key(self, ref, account=None):
        return f"t_{account or self.account_id}_{ref}"

    def order(self, key, contract, action, qty, exit_, account=None):
        return {"order_key": key, "account_id": account or self.account_id, "contract": contract,
                "action": action, "quantity": qty, "is_exit": exit_}

    def submit(self, key, contract, action, qty, exit_, account=None):
        return self.submit_batch(key, [self.order(key, contract, action, qty, exit_, account)])

    def submit_batch(self, bk, orders):
        self.sent.append(orders)
        errs = {}
        for o in orders:
            cid = int(o["contract"].get("con_id") or 0)
            if cid in self.reject:
                errs[o["order_key"]] = "IBKR_REJECTED"
                continue
            f = self.partial.pop(cid, o["quantity"])
            self.status[o["order_key"]] = {"status": "FILLED" if f == o["quantity"] else "CANCELLED",
                                           "filled_qty": f, "avg_fill_price": self.fill_px.get(cid, 7000.0)}
        return {"accepted": len(orders) - len(errs), "rejected": len(errs), "errors": errs}

    def order_status(self, key):
        return self.status.get(key, {"status": "PLACED"})

    def cancel(self, key):
        return True


class Logic:
    timeframe, max_day_trades = 5, 0

    def exit_rules(self):
        return ExitRules(be_after_tp1=True, trail=True)


class S:
    dry_run = False
    entry_wait_seconds = 60
    account_id = "DU_DEFAULT"
    entry_max_age_minutes = 0      # entries here use a fixed past timestamp: no late-entry check


@pytest.fixture
def dep(tmp_path, monkeypatch):
    monkeypatch.setattr(DEP, "STATE", tmp_path)
    monkeypatch.setattr(DEP, "JOURNAL", tmp_path / "journal.jsonl")
    monkeypatch.setattr(DEP, "_POLL_SECS", 0.0)
    hub = Hub()
    card = {"id": "x", "name": "Test", "default_qty": [1, 2], "family": "S&P 500"}
    cfg = {"id": "x@mes", "strategy": "x", "market": "MES", "qty": [1, 2], "enabled": True, "account": "DU_B"}
    d = DEP.Deployment(cfg, card, Logic(), markets.get("MES"), hub, S())
    return d, hub


def open_trade(d, hub, px=7000.0):
    p = Plan(side=1, kind="market", level=px, anchor="a", signal_time="2026-11-13T09:00:00-05:00",
             after="2026-11-13T09:05:00-05:00", final=True, session=1, stop=px - 10, tp_r=2.0, trail_r=1.0)
    d._enter(p, px, pd.Timestamp("2026-11-13T14:05:00Z").isoformat(), (OLD, "202612"))
    d.sync()           # entry order
    d.sync()           # poll -> filled
    assert d.held == 3 and d.order is None


def test_orders_go_to_the_strategy_account(dep):
    d, hub = dep
    open_trade(d, hub)
    o = hub.sent[0][0]
    assert o["account_id"] == "DU_B" and "_DU_B_" in o["order_key"]
    assert d.pos_account == "DU_B" and d.meta["account"] == "DU_B"
    d.cfg["account"] = "DU_C"                         # a later change never moves the open trade's orders
    d._exits(d.trade.close_all("manual", 7001.0))
    d.sync(); d.sync()
    assert hub.sent[-1][0]["account_id"] == "DU_B"
    assert d.held == 0 and d.trade is None and d.pos_account is None


def test_roll_keeps_the_trade_and_books_pnl_across_months(dep):
    d, hub = dep
    open_trade(d, hub, 7000.0)
    stops, tp1 = list(d.trade.stop), d.trade.tp1
    assert d.start_roll(NEW, "202703", "test") == ""
    batch = hub.sent[-1]
    assert [(o["action"], o["quantity"], o["contract"]["con_id"], o["is_exit"]) for o in batch] == \
        [("SELL", 3, OLD, True), ("BUY", 3, NEW, False)]
    assert all(o["account_id"] == "DU_B" for o in batch)
    d.sync()                                           # poll both legs
    assert d.roll is None and d.held == 3
    assert d.pos_con_id == NEW and d.pos_expiry == "202703" and d.meta["contract"] == "MESH7"
    assert d.trade.stop == stops and d.trade.tp1 == tp1     # the trade itself is untouched
    assert d.meta["roll_adj"] == 50.0 and d.meta["rolls"][0]["spread"] == 50.0
    # exit everything in the new month at 7060 = +10 points over the (rolled) entry
    hub.fill_px[NEW] = 7060.0
    d._exits(d.trade.close_all("manual", 7010.0))
    d.sync(); d.sync()
    assert hub.sent[-1][0]["contract"]["con_id"] == NEW
    from desk.store import read_jsonl
    row = read_jsonl(DEP.JOURNAL)[-1]
    # (7000 sold at the roll - 7000 entry) + (7060 - 7050 bought at the roll) = +10 pts x 3 x $5
    assert row["pnl"] == 150.0 and row["rolls"][0]["to"] == "MESH7" and row["account"] == "DU_B"


def test_exit_during_roll_waits_then_goes_to_new_month(dep):
    d, hub = dep
    open_trade(d, hub)
    hub.status_hold = True
    orig = hub.submit_batch

    def slow(bk, orders):                             # the roll's orders are working, not filled yet
        res = orig(bk, orders)
        for o in orders:
            hub.status[o["order_key"]] = {"status": "PLACED"}
        return res
    hub.submit_batch = slow
    d.start_roll(NEW, "202703", "test")
    d._exits(d.trade.close_all("stop", 6990.0))        # stop hit while moving
    n = len(hub.sent)
    d.sync()
    assert len(hub.sent) == n                          # no exit order while the position is split
    for o in hub.sent[-1]:
        hub.status[o["order_key"]] = {"status": "FILLED", "filled_qty": 3,
                                      "avg_fill_price": hub.fill_px[o["contract"]["con_id"]]}
    hub.submit_batch = orig
    d.sync()                                           # roll done
    d.sync(); d.sync()                                 # exit sent and filled
    assert hub.sent[-1][0]["contract"]["con_id"] == NEW and hub.sent[-1][0]["action"] == "SELL"
    assert d.held == 0 and d.trade is None


def test_roll_rejected_nothing_filled_stays_put(dep):
    d, hub = dep
    open_trade(d, hub)
    hub.reject = {OLD, NEW}
    d.start_roll(NEW, "202703", "test")
    for lg in d.roll["legs"]:
        lg["tries"] = 3                                # pretend the retries are used up
    d._roll_check()
    assert d.roll is None and d.pos_con_id == OLD and d.held == 3 and not d.attention


def test_roll_half_done_needs_attention_then_finishes(dep):
    d, hub = dep
    open_trade(d, hub)
    hub.reject = {NEW}                                 # old month sells, new month is rejected
    d.start_roll(NEW, "202703", "test")
    d.sync()
    for lg in d.roll["legs"]:
        if lg["role"] == "open":
            lg["tries"] = 3
            lg["key"] = None
    d._roll_check()
    assert d.attention and "MESH7" in d.attention and d.roll["failed"]
    n = len(hub.sent)
    d._exits(d.trade.close_all("stop", 6990.0))
    d.sync()
    assert len(hub.sent) == n                          # no orders while it needs a human
    d.finish_failed_roll()                             # operator bought the new month by hand
    d.attention = ""
    assert d.roll is None and d.pos_con_id == NEW and d.held == 3


def test_engine_account_and_month_checks(monkeypatch, tmp_path):
    from desk import engine as E
    eng = E.Engine.__new__(E.Engine)
    eng.hub = type("H", (), {"accounts": lambda self: {"managed": ["DU1", "DU2"], "accounts": []}})()
    eng._accounts, eng._accounts_at = None, 0.0
    from desk import rollrule
    monkeypatch.setattr(rollrule, "MONTHS_FILE", tmp_path / "m.json")
    monkeypatch.setattr(rollrule, "SETTINGS_FILE", tmp_path / "s.json")
    eng.rolls = rollrule.RollBook(eng.hub)
    assert eng._check_account("DU2") == "DU2"
    with pytest.raises(ValueError):
        eng._check_account("U999")
    assert eng._check_expiry("MES", "auto") == "auto"
    ok = [m["expiry"] for m in eng.month_choices("MES")]
    assert eng._check_expiry("MES", ok[1]) == ok[1]
    with pytest.raises(ValueError):
        eng._check_expiry("MES", "201912")


def test_no_order_at_all_for_an_unknown_account(dep):
    d, hub = dep
    open_trade(d, hub)
    n = len(hub.sent)
    d.account_bad = "not in this login"
    d._exits(d.trade.close_all("stop", 6990.0))
    d.sync(); d.sync()
    assert len(hub.sent) == n and d.held == 3          # exits too: nothing goes to another account
    d.account_bad = ""
    d.sync(); d.sync()
    assert len(hub.sent) == n + 1 and d.held == 0
