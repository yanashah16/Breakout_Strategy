"""
Offline check of the live stock bot (desk/stocks/bot.py) in practice mode: warm-up from history, then real
minutes one by one with an order sync after each, exactly as the desk loop does. Checks:
  * the bot's own position always follows the rules' target (orders fill at once in practice mode)
  * every trade the rules made appears once in the journal, with the right side / shares / result
  * a restart in the middle of a trade restores it and carries on

    python tests/test_stocks_bot.py            (needs ..\\nq_wae_backtest\\data\\NVDA_1min.parquet)
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desk.stocks import bot as B           # noqa: E402
from desk.stocks.feed import rth            # noqa: E402
from desk.store import read_jsonl           # noqa: E402

DATA = ROOT.parent / "nq_wae_backtest" / "data"
COPY = sys.argv[1] if len(sys.argv) > 1 else "box10"


class FakeHub:
    def order_key(self, ref, account=None):
        return f"test_{ref}"


def load(sym="NVDA"):
    df = pd.read_parquet(DATA / f"{sym}_1min.parquet", columns=["ts_utc", "open", "high", "low", "close", "volume"])
    df = df.rename(columns={"ts_utc": "t"})
    df["t"] = pd.to_datetime(df.t, utc=True)
    return rth(df.sort_values("t").drop_duplicates("t").reset_index(drop=True))


def daily(m1: pd.DataFrame) -> pd.DataFrame:
    d = m1.assign(date=m1.t.dt.tz_convert("America/New_York").dt.date).groupby("date").agg(
        high=("high", "max"), low=("low", "min"), close=("close", "last")).reset_index()
    return d


def run():
    tmp = Path(tempfile.mkdtemp())
    B.DIR, B.JOURNAL = tmp, tmp / "journal.jsonl"
    settings = SimpleNamespace(dry_run=True, entry_wait_seconds=60)
    m1 = load()
    start, mid, end = pd.Timestamp("2025-01-01", tz="UTC"), pd.Timestamp("2025-04-01", tz="UTC"), pd.Timestamp("2025-07-01", tz="UTC")
    hist = m1[(m1.t >= start - pd.Timedelta(days=45)) & (m1.t < start)]
    days = daily(m1[m1.t < start])
    bot = B.StockBot(COPY, "NVDA", None, FakeHub(), settings, room=lambda n: True, risk_usd=1000)
    bot.path = tmp / f"{COPY}_NVDA.json"
    bot.warmup(hist, days, hist.t.iloc[0])
    live = m1[(m1.t >= start) & (m1.t < end)]
    bad = 0
    restarted = False
    closed_before = []
    trades_rules = 0
    for r in live.itertuples():
        if not restarted and r.t >= mid and bot.held != 0 and not bot.order:
            # restart: a new bot from the saved state, warmed up on the history up to now
            restarted = True
            was = (bot.core.pos.side, bot.held)
            bot.save()
            hist2 = m1[(m1.t >= r.t - pd.Timedelta(days=45)) & (m1.t < r.t)]
            bot2 = B.StockBot(COPY, "NVDA", None, FakeHub(), settings, room=lambda n: True, risk_usd=1000)
            bot2.path = bot.path
            bot2.restore()
            bot2.warmup(hist2, daily(m1[m1.t < r.t.normalize()]), hist2.t.iloc[0])
            print(f"restart at {r.t}: before {was}, after {(bot2.core.pos.side, bot2.held)}")
            assert (bot2.core.pos.side, bot2.held) == was, "restart lost the trade"
            closed_before = list(bot.core.closed)
            bot = bot2
        row = pd.Series({"t": r.t, "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume})
        bot.px = r.close
        bot.on_minute(row, enabled=True)
        bot.sync("DU0000000", {"symbol": "NVDA", "sec_type": "STK", "exchange": "SMART"})
        bot.sync("DU0000000", {"symbol": "NVDA", "sec_type": "STK", "exchange": "SMART"})   # reverse = 2 orders
        if bot.held != bot.target():
            bad += 1
    allc = closed_before + bot.core.closed
    trades_rules = len(allc)
    j = read_jsonl(B.JOURNAL)
    entries = {(c["entry_time"], c["side"]) for c in allc}
    print(f"rule trades {len(entries)}, journal trades {len(j)}")
    assert restarted, "never restarted inside a trade"
    assert len(entries) == len(j) + (1 if bot.core.pos.side else 0) - sum(1 for e in entries if False), "journal != rule trades"
    print(f"rule legs {trades_rules}, journal trades {len(j)}, minutes where held != target: {bad}")
    print(pd.DataFrame(j)[["opened", "side", "setup", "shares", "entry", "exit", "pnl", "exits"]].tail(8).to_string())
    shutil.rmtree(tmp, ignore_errors=True)
    assert bad == 0, "the bot's position did not follow the rules"
    assert len(j) > 0, "no trades"
    print("OK")


if __name__ == "__main__":
    run()
