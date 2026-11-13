"""Exit rules of desk.trade.Trade (fast, no market data)."""
import math

from desk.logic.base import ExitRules
from desk.trade import Trade

RULES = ExitRules(be_after_tp1=True, trail=True)


def long_trade(**kw):
    d = dict(side=1, entry=100.0, stop0=90.0, q=[1, 2], tp_r=2.0, trail_r=1.0, entry_minute="m0")
    d.update(kw)
    return Trade(**d)


def test_levels_from_entry():
    t = long_trade()
    assert t.risk == 10 and t.tp1 == 120 and math.isnan(t.tp2)
    t.set_entry(101.0)                       # real fill moves every level
    assert t.risk == 11 and t.tp1 == 123


def test_stop_first_then_targets():
    t = long_trade()
    out = t.on_minute(100, 125, 89, 110, "m1", RULES)       # range hits stop AND tp1: stop first
    assert [e.reason for e in out] == ["stop", "stop"] and not t.is_open


def test_tp1_then_runner_break_even_next_minute():
    t = long_trade()
    out = t.on_minute(100, 121, 99, 120, "m1", RULES)
    assert [(e.leg, e.reason) for e in out] == [(1, "tp1")]
    assert t.stop[1] == 111 and t.kind[1] == 2               # break-even, then trail 1R behind best 121
    t2 = long_trade()
    t2.on_minute(100, 121, 99, 120, "m1", ExitRules(be_after_tp1=True, trail=False))
    assert t2.stop[1] == 100 and t2.kind[1] == 1             # break-even only
    out = t2.on_minute(120, 121, 99.5, 100, "m2", RULES)
    assert [(e.leg, e.reason) for e in out] == [(2, "be")]


def test_trailing_moves_only_in_favour():
    t = long_trade()
    t.on_minute(100, 121, 100, 120, "m1", RULES)              # tp1, trail starts: best 121 -> stop 111
    assert t.stop[1] == 111 and t.kind[1] == 2
    t.on_minute(120, 120, 115, 116, "m2", RULES)
    assert t.stop[1] == 111                                   # no new high -> unchanged
    t.on_minute(116, 130, 116, 129, "m3", RULES)
    assert t.stop[1] == 120


def test_live_price_exits_but_never_moves_stops():
    t = long_trade()
    out = t.on_price(120.5, "p")
    assert [(e.leg, e.reason) for e in out] == [(1, "tp1")]
    assert t.stop[1] == 90                                    # the move waits for the minute's end
    t.on_minute(118, 121, 117, 120, "m1", RULES)
    assert t.stop[1] >= 100


def test_fill_minute_is_not_judged_on_its_range():
    t = long_trade(entry_minute="m1")
    assert t.on_minute(95, 101, 85, 100, "m1", RULES) == []  # low before the fill: not a stop (live)
    assert t.is_open


def test_short_mirror():
    t = Trade(side=-1, entry=100.0, stop0=110.0, q=[0, 3], tp_r=3.0, tp2_r=5.0, trail_r=2.0)
    assert t.tp1 == 70 and t.tp2 == 50 and t.open == [False, True]
    out = t.on_minute(100, 101, 69, 70, "m1", RULES)
    assert out == [] and t.tp1_done and t.stop[1] == 89       # runner-only: no leg 1 exit; trail 2R above 69
    out = t.on_minute(70, 71, 49, 50, "m2", RULES)
    assert [(e.leg, e.reason) for e in out] == [(2, "tp2")]


def test_roundtrip_dict():
    t = long_trade()
    t.on_minute(100, 121, 100, 120, "m1", RULES)
    u = Trade.from_dict(t.to_dict())
    assert u.to_dict() == t.to_dict()
