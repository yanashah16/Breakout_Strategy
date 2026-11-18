"""
Minute candles are fetched at :00 (bar_settle_seconds 0). If the minute that just ended is not in the
hub's stream yet, the desk asks again on the next tick instead of waiting a whole minute.
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desk import bars as B          # noqa: E402


class Hub:
    def __init__(self):
        self.rows = []
        self.calls = 0

    def add(self, t):
        self.rows.append({"time": pd.Timestamp(t).tz_convert("America/New_York").isoformat(),
                          "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10})

    def bars(self, symbol):
        self.calls += 1
        return {"bars": list(self.rows), "current_price": 1.5}

    def subscribe_bars(self, *a):
        return True


def book(tmp_path, settle=0.0):
    B.DATA = tmp_path
    b = B.BarBook("GC", "COMEX", 90, settle)
    b.path = tmp_path / "GC.parquet"
    b._save_cache = lambda force=False: None
    b.ready = True
    return b


def et(t):
    return t.tz_convert("America/New_York").tz_localize(None)


def test_fetch_at_zero_seconds_takes_the_just_ended_minute(tmp_path):
    hub, b = Hub(), book(tmp_path)
    hub.add("2026-11-18T14:04:00Z")
    hub.add("2026-11-18T14:05:00Z")                       # forming minute: never used
    now = pd.Timestamp("2026-11-18T14:05:00.400Z")
    assert b.due(et(now))
    rows = b.update(hub, now)
    assert [r.t for r in rows] == [pd.Timestamp("2026-11-18T14:04:00Z")]
    assert not b.due(et(now + pd.Timedelta(seconds=2)))   # got it: no more fetches this minute


def test_missing_minute_is_retried_on_the_next_tick(tmp_path):
    hub, b = Hub(), book(tmp_path)
    hub.add("2026-11-18T14:03:00Z")
    now = pd.Timestamp("2026-11-18T14:05:00.400Z")
    assert b.update(hub, now)[0].t == pd.Timestamp("2026-11-18T14:03:00Z")    # 14:04 not there yet
    later = now + pd.Timedelta(seconds=2)
    assert b.due(et(later))                               # asks again 2 s later, not at 14:06
    hub.add("2026-11-18T14:04:00Z")
    assert [r.t for r in b.update(hub, later)] == [pd.Timestamp("2026-11-18T14:04:00Z")]
    assert not b.due(et(later + pd.Timedelta(seconds=2)))


def test_gives_up_retrying_after_15_seconds(tmp_path):
    hub, b = Hub(), book(tmp_path)                        # market closed: nothing new
    hub.add("2026-11-18T14:03:00Z")
    b.update(hub, pd.Timestamp("2026-11-18T14:05:16Z"))
    assert not b.due(et(pd.Timestamp("2026-11-18T14:05:18Z")))
