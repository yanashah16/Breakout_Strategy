"""
Live data path without a hub: a fake hub serves research 1-minute bars shifted to end 'now'.
Checks the BarBook warm-up merge (30-min + 5-min + 1-min layers), the live minute stream and a
full engine cycle with a strategy switched on (practice mode).
"""
import sys
import time
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = ROOT.parent / "nq_wae_backtest" / "data" / "ES_1min.parquet"
pytestmark = pytest.mark.skipif(not DATA.exists(), reason="research data not available")


class FakeHub:
    """Serves ES minutes; `now` = the real clock, the data shifted so its last minute is 'now'."""

    def __init__(self):
        df = pd.read_parquet(DATA, columns=["ts_utc", "open", "high", "low", "close", "volume"])
        df = df.rename(columns={"ts_utc": "t"}).sort_values("t").tail(140 * 1440).reset_index(drop=True)
        now = pd.Timestamp.now(tz="UTC").floor("min")
        self.shift = now - df.t.iloc[-1] - pd.Timedelta(minutes=1)
        self.shift = pd.Timedelta(weeks=round(self.shift / pd.Timedelta(weeks=1)))   # keep weekdays
        df["t"] = df.t + self.shift
        self.df = df
        self.algo_id, self.account_id, self.last_error = "test", "DU0", ""
        self.orders = []

    def rows(self, df):
        return [{"time": t.tz_convert("America/New_York").isoformat(), "open": o, "high": h, "low": l,
                 "close": c, "volume": v} for t, o, h, l, c, v in df.itertuples(index=False)]

    def _upto(self):
        return self.df[self.df.t <= pd.Timestamp.now(tz="UTC")]

    def health(self):
        return {"oms_connected": True, "mds_connected": True, "data_connected": True}

    def register(self):
        return True

    def subscribe_bars(self, *a):
        return True

    def bars(self, symbol):
        d = self._upto().tail(1400)
        return {"bars": self.rows(d), "current_price": float(d.close.iloc[-1])}

    def history(self, symbol, exchange, bar_size, duration):
        days = int(duration.split()[0])
        d = self._upto()
        d = d[d.t >= d.t.iloc[-1] - pd.Timedelta(days=days)]
        n = {"1 min": 1, "5 mins": 5, "30 mins": 30}[bar_size]
        if n > 1:
            g = d.groupby(d.t.dt.floor(f"{n}min"))
            d = pd.DataFrame({"t": list(g.groups), "open": g.open.first().values, "high": g.high.max().values,
                              "low": g.low.min().values, "close": g.close.last().values, "volume": g.volume.sum().values})
        return self.rows(d)

    def resolve(self, contract):
        return {"ES": 1, "MES": 2}.get(contract["symbol"], 9)

    def prices(self, contracts):
        px = float(self._upto().close.iloc[-1])
        return {int(c["con_id"]): {"last": px} for c in contracts}

    def order_key(self, ref, account=None):
        return f"test_{account or self.account_id}_{ref}"

    def positions(self, symbol=None, account=None):
        return []

    def accounts(self):
        from desk import settings as S
        acct = S.load().account_id
        return {"managed": [acct, "DU1"], "accounts": [{"account": acct}, {"account": "DU1"}]}


def test_barbook_layers_and_stream(tmp_path, monkeypatch):
    from desk import bars as B
    monkeypatch.setattr(B, "DATA", tmp_path)
    hub = FakeHub()
    book = B.BarBook("ES", "CME", 90, 3)
    assert book.warmup(hub)
    df = book.df
    span = (df.t.iloc[-1] - df.t.iloc[0]).days
    assert 85 <= span <= 95, span
    gaps = df.t.diff().dt.total_seconds().div(60)
    recent = df.t >= df.t.iloc[-1] - pd.Timedelta(days=9)
    assert (gaps[recent].dropna() >= 1).all() and gaps[recent].median() == 1       # exact 1-minute part
    assert df.t.is_monotonic_increasing and not df.t.duplicated().any()
    # the stream adds nothing new right after the warm-up (no duplicates, no forming minute)
    new = book.update(hub, pd.Timestamp.now(tz="UTC"))
    assert len(new) <= 2
    assert not book.df.t.duplicated().any()


def test_engine_cycle_practice_mode(tmp_path, monkeypatch):
    from desk import bars as B
    from desk import deployment as D
    from desk import engine as E
    from desk import settings as S
    for mod in (D, E):
        monkeypatch.setattr(mod, "STATE", tmp_path)
    monkeypatch.setattr(D, "JOURNAL", tmp_path / "journal.jsonl")
    monkeypatch.setattr(E, "DEPLOYMENTS", tmp_path / "deployments.json")
    monkeypatch.setattr(E, "EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr(B, "DATA", tmp_path)
    s = S.load()
    s.dry_run = True
    eng = E.Engine(s)
    eng.hub = FakeHub()
    for d in eng.deps.values():
        d.hub = eng.hub
    did = "sp-spike-flag@mes"
    eng.set_enabled(did, True)
    eng.cycle()                                   # starts the ES warm-up thread
    for _ in range(120):
        if "ES" in eng.books:
            break
        time.sleep(0.5)
    assert "ES" in eng.books and eng.books["ES"].ready
    eng.cycle()
    d = eng.deps[did]
    assert eng.contracts["MES"][0] == 2 and d.signal_px is not None and d.error == ""
    from desk import describe
    snap = describe.snapshot(eng)
    row = next(r for r in snap["strategies"] if r["id"] == did)
    assert row["enabled"] and row["status"]["tone"] in ("idle", "armed", "trade")
    # a chart-bar close through the real logic on the live window
    book = eng.books["ES"]
    last = book.df.t.iloc[-1]
    eng._bar_close("ES", book, 5, B.bucket(last, 5), upto=last + pd.Timedelta(minutes=1), deps=[d])
    assert d.error == ""
