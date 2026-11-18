"""
Late entries are skipped: a stop entry whose trigger minute is old (desk was off / data feed down and the
desk is catching up on old candles), or whose live price is already far past the trigger, must not send a
market order at today's price.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desk import deployment as DEP          # noqa: E402
from desk.logic.base import ExitRules, Plan  # noqa: E402

NOW = pd.Timestamp("2026-11-18T04:51:00Z")


class Logic:
    timeframe = 3
    max_day_trades = 0

    def exit_rules(self):
        return ExitRules()


def make(tmp_path, signal_px=None, **settings):
    DEP.STATE = tmp_path
    DEP.JOURNAL = tmp_path / "journal.jsonl"
    mkt = SimpleNamespace(symbol="MGC", point_value=10.0)
    st = SimpleNamespace(dry_run=True, entry_wait_seconds=60, account_id="DU0", **settings)
    cfg = {"id": "t@mgc", "strategy": "t", "market": "MGC", "qty": [1, 2], "enabled": True, "risk_usd": 0}
    d = DEP.Deployment(cfg, {"name": "T"}, Logic(), mkt, hub=None, settings=st)
    d.path = tmp_path / "t.json"
    d.now = lambda: NOW
    d.signal_px = signal_px
    return d


def plan(level=3794.6, stop=3811.42):
    return Plan(side=-1, kind="stop", level=level, anchor="a1", signal_time="x", after="x", final=True,
                stop=stop, tp_mode="dist", tp_dist=10.64, session=1)


def test_stale_entry_is_skipped(tmp_path):
    d = make(tmp_path)
    p = plan()
    d.plans = [p]
    d._enter(p, 3794.6, "2026-11-18T03:15:00+00:00", (1, "202612"))    # 96 minutes old
    assert d.trade is None and d.plans == []
    assert d.last_anchor["-1"] == "a1"                                   # pattern spent: no later chase


def test_entry_chased_too_far_is_skipped(tmp_path):
    d = make(tmp_path, signal_px=3785.5)                                 # 9.1 pts = 0.54R past the trigger
    p = plan()
    d._enter(p, 3794.6, "2026-11-18T04:50:00+00:00", (1, "202612"))
    assert d.trade is None


def test_fresh_entry_near_the_trigger_is_taken(tmp_path):
    d = make(tmp_path, signal_px=3793.0)                                 # 1.6 pts past: fine
    p = plan()
    d._enter(p, 3794.6, "2026-11-18T04:50:00+00:00", (1, "202612"))
    assert d.trade is not None and d.meta["trigger"] == 3794.6


def test_limits_can_be_switched_off(tmp_path):
    d = make(tmp_path, signal_px=3700.0, entry_max_age_minutes=0, entry_max_chase_r=0)
    p = plan()
    d._enter(p, 3794.6, "2026-11-18T03:15:00+00:00", (1, "202612"))
    assert d.trade is not None
