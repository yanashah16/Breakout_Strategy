"""Volume-based contract month (desk.rollrule) and the bar-series month switch (engine._series)."""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desk import rollrule  # noqa: E402
from desk.trade import Trade  # noqa: E402


class Hub:
    def __init__(self, vols):
        self.vols, self.calls = vols, 0            # {expiry: {date: volume}}

    def month_history(self, symbol, exchange, bar_size, duration, expiry):
        self.calls += 1
        if expiry not in self.vols:
            return None
        return {"expiry": expiry, "bars": [{"time": f"{d}T00:00:00", "volume": v} for d, v in self.vols[expiry].items()]}


@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.setattr(rollrule, "MONTHS_FILE", tmp_path / "months.json")
    monkeypatch.setattr(rollrule, "SETTINGS_FILE", tmp_path / "settings.json")
    clock = {"now": dt.datetime(2026, 12, 8, 10, 0)}           # Tue, a session in progress

    def make(vols):
        b = rollrule.RollBook(Hub(vols), now_et=lambda: clock["now"])
        return b
    return make, clock


def test_nearest_month_until_next_has_ratio_x_volume(book):
    make, clock = book
    b = make({"202612": {"2026-12-07": 1_000_000, "2026-12-08": 10}, "202703": {"2026-12-07": 1_150_000, "2026-12-08": 999}})
    assert b.month("MES") == "202612"
    st = b.check("MES")
    assert st["session"] == "2026-12-07"                          # today's partial session is not used
    assert b.month("MES") == "202612" and st["share"] == 1.15        # 1.15x < 1.2x: stay
    assert not b.due("MES")                                          # once per trade date
    b.hub.vols["202703"]["2026-12-08"] = 1_300_000
    b.hub.vols["202612"]["2026-12-08"] = 1_000_000
    clock["now"] = dt.datetime(2026, 12, 8, 17, 30)                  # session closed -> new trade date
    assert b.due("MES")
    b.check("MES")
    assert b.month("MES") == "202703"
    # never back, even if the old month trades more again
    b.hub.vols["202612"]["2026-12-09"] = 9_000_000
    b.hub.vols["202703"]["2026-12-09"] = 1
    clock["now"] = dt.datetime(2026, 12, 9, 17, 30)
    b.check("MES")
    assert b.month("MES") == "202703"


def test_ratio_setting_and_limits(book):
    make, clock = book
    b = make({})
    assert b.ratio == 1.2
    assert b.set_ratio(1.4) == 1.4 and rollrule.RollBook(b.hub).ratio == 1.4     # saved
    with pytest.raises(ValueError):
        b.set_ratio(0.9)


def test_no_volume_keeps_month_and_safety_net_by_date(book):
    make, clock = book
    b = make({})                                                     # old hub / no data
    st = b.check("MES")
    assert st["error"] and b.month("MES") == "202612"
    assert not b.due("MES")                                          # retried after 10 minutes, not every cycle
    assert b.month("MES", dt.date(2026, 12, 18)) == "202703"         # expiry day: never the old month
    assert b.month("MGC", dt.date(2026, 11, 25)) == "202702"         # before gold first notice


def test_trade_shift_keeps_distances():
    t = Trade(side=1, entry=100.0, stop0=90.0, q=[1, 2], tp_r=2.0, tp2_r=3.0)
    d1, d2 = t.tp1 - t.entry, t.entry - t.stop[0]
    t.shift(76.25)
    assert t.entry == 176.25 and t.stop == [166.25, 166.25] and t.tp1 - t.entry == d1 and t.entry - t.stop[0] == d2
    assert t.risk == 10.0 and t.tp2 == 206.25


def test_series_switch_shifts_history_and_open_trade(monkeypatch, tmp_path):
    from desk import engine as E
    from desk.bars import BarBook

    class D:
        def __init__(self):
            self.market = type("M", (), {"signal_symbol": "ES"})()
            self.shifted = []

        def series_shift(self, j):
            self.shifted.append(j)
    eng = E.Engine.__new__(E.Engine)
    eng.px, eng.month_ids, eng.contracts, eng._month_tried, eng._shared = {}, {}, {}, {}, {}
    d = D()
    eng.deps = {"x": d}
    eng.event = lambda *a, **k: None
    eng._guard = lambda dep, fn, *a: fn(*a)
    eng._month_id = lambda sym, m: {"202612": 1, "202703": 2}.get(m)
    monkeypatch.setattr(E.dt, "datetime", type("F", (dt.datetime,), {"now": staticmethod(lambda: dt.datetime(2026, 12, 9, 10))}))
    b = BarBook("ES", "CME", 30, 3)
    t0 = pd.Timestamp("2026-12-09T14:00Z")
    old = [{"t": t0 + pd.Timedelta(minutes=k), "open": 7000.0, "high": 7001.0, "low": 6999.0, "close": 7000.0, "volume": 1}
           for k in range(10)]
    new = [{"t": t0 + pd.Timedelta(minutes=10 + k), "open": 7076.0, "high": 7077.0, "low": 7075.0, "close": 7076.0, "volume": 1}
           for k in range(2)]
    b.df = pd.DataFrame(old + new)
    eng.px = {1: 7000.25, 2: 7076.25}
    b.current_price = 7000.0
    rows = [r for _, r in b.df.tail(1).iterrows()]
    b.current_price = 7000.0
    eng._series("ES", b, [], rows)                                  # first look: on Z6, nothing moves
    assert b.series_month == "202612" and not b.adjust
    b.current_price = 7076.0                                        # the stream moved to H7
    rows = [r for _, r in b.df.tail(2).iterrows()]
    out = eng._series("ES", b, [], rows)
    assert b.series_month == "202703" and d.shifted == [76.0]
    assert b.adjust[0][0] == t0 + pd.Timedelta(minutes=10)          # boundary found at the gap
    w = b.window()
    assert (w.close == 7076.0).all() or (w.close.iloc[:10] == 7076.0).all()
    assert [r.close for r in out] == [7076.0, 7076.0]
    assert (b.df.close.iloc[:10] == 7000.0).all()                    # raw data / cache untouched
