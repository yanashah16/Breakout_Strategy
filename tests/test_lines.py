"""lines engine: cards build, runner lock after TP1, risk sizing in whole contracts."""
import types

from desk import catalog, markets
from desk import deployment as DEP
from desk.logic.base import ExitRules
from desk.trade import Trade


def test_cards_build_on_the_lines_engine():
    cards, problems = catalog.load()
    assert not problems
    lines = {k for k, c in cards.items() if c.get("engine") == "lines"}
    assert lines == {"es-multi-touch-break", "nq-multi-touch-break", "nq-zone-bounce-reverse", "es-zone-retest-reverse"}
    for k in lines:
        assert cards[k]["experimental"] is True


def test_runner_lock_after_tp1():
    rules = ExitRules(be_after_tp1=True, tp1_lock_r=0.25)
    t = Trade(side=1, entry=100.0, stop0=90.0, q=[1, 2], tp_r=1.0, entry_minute="m0")
    t.on_minute(100, 111, 99, 110, "m1", rules)              # TP1 at 110
    assert not t.open[0] and t.stop[1] == 102.5               # entry + 0.25 R
    s = Trade(side=-1, entry=100.0, stop0=110.0, q=[1, 2], tp_r=1.0, entry_minute="m0", lock_px=0.5)
    s.on_minute(100, 101, 89, 90, "m1", rules)
    assert s.stop[1] == 100.0 - 2.5 - 0.5


def test_lock_zero_is_plain_break_even():
    t = Trade(side=1, entry=100.0, stop0=90.0, q=[1, 2], tp_r=1.0, entry_minute="m0")
    t.on_minute(100, 111, 99, 110, "m1", ExitRules(be_after_tp1=True))
    assert t.stop[1] == 100.0


def _dep(risk, q=(1, 2), market="MES", cap=None):
    d = DEP.Deployment.__new__(DEP.Deployment)
    d.cfg = {"risk_usd": risk, **({"max_contracts": cap} if cap else {})}
    d.card = {"data": {}}
    d.logic = types.SimpleNamespace(risk_usd=1000.0)
    d.market = markets.get(market)
    d.id = "t"
    return d, list(q)


def test_risk_sizing_whole_contracts_rounded_down():
    d, q = _dep(1000)
    assert d.risk_qty(q, 40.0) == [2, 3]          # 1 MES at 40 pt = $200 -> 5 contracts, split 1 : 2
    assert d.risk_qty([1, 0], 40.0) == [5, 0]
    assert d.risk_qty(q, 70.0) == [1, 1]          # $350 each -> 2 contracts (2.86 rounded down)
    assert d.risk_qty(q, 150.0) == [0, 1]         # 1 contract: the larger leg (runner 2 of 3)
    assert d.risk_qty(q, 250.0) == [0, 0]         # 1 MES risks $1,250 > $1,000 -> skip
    e, _ = _dep(1000, market="ES")
    assert e.risk_qty([1, 0], 40.0) == [0, 0]     # 1 ES at 40 pt = $2,000 -> skip


def test_risk_sizing_cap_and_card_default():
    d, q = _dep(1000, cap=4)
    assert d.risk_qty([1, 0], 2.0) == [4, 0]      # 100 MES capped at 4
    d.cfg = {}
    assert d.risk_usd == 1000.0                   # the card's value when the deployment has none
    d.cfg = {"risk_usd": 0}
    assert d.risk_usd == 0.0                      # 0 = quantity boxes
