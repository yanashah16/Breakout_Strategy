"""
StockDesk: runs the stock box strategy (two copies x the stock list) next to the futures engine, in the same
process and with the same hub, account and practice-mode settings. Its own thread, its own lock.

Config: state/stocks/config.json   {"copies": {"box10": {"enabled", "capital_usd"}, ...}, "stocks": [...],
                                     "max_exposure_usd": 0}
Ownership: each bot counts only the shares its own orders filled. The broker check compares the sum of both
copies with IBKR's position per stock; shares that were already there (manual trades) are remembered as
"outside" and never touched. A mismatch pauses that stock's orders until the operator checks it.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Dict, List, Optional

import pandas as pd
import requests.adapters

from desk.hub import Hub
from desk.stocks.bot import DIR, JOURNAL, StockBot
from desk.stocks.copies import COPIES, HOW_IT_ENTERS, HOW_IT_EXITS, STOCKS
from desk.stocks.feed import NY, StockFeed
from desk.store import read_json, read_jsonl, write_json

log = logging.getLogger("desk.stocks")
CONFIG = DIR / "config.json"
_WARM_LOCK = threading.BoundedSemaphore(3)   # stocks warming at once (one at a time took ~25 min for 20 stocks)


def now_ny() -> dt.datetime:
    return pd.Timestamp.now(tz=NY).tz_localize(None).to_pydatetime()


class StockDesk:
    def __init__(self, engine):
        self.engine = engine
        s = engine.s
        self.s = s
        self.hub = Hub(s.hub_url, s.hub_api_key, s.algo_id, s.account_id)     # own client: own thread
        # stock bar fetches run in parallel on this (stocks-only) client: room for their connections
        for scheme in ("http://", "https://"):
            self.hub.s.mount(scheme, requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=32))
        self.lock = threading.RLock()
        self.cfg = self._load_cfg()
        self.feeds: Dict[str, StockFeed] = {}
        self.bots: Dict[str, StockBot] = {}
        self._warming: set = set()
        self._failed: Dict[str, str] = {}      # stock -> why its last warm-up failed (shown on the page)
        self._outside: Dict[str, int] = {}
        self._mismatch: Dict[str, int] = {}
        self._next_reconcile = 0.0
        self.running = False
        # Hub calls for bars and prices run outside self.lock: a slow hub (an IBKR data reconnect makes it rebuild
        # every bar stream, ~30 s each) used to hold the lock for many minutes, freezing stops, orders and the page.
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="stock-bars")
        self._fetch: Dict[str, Future] = {}      # stock -> its bar fetch in flight (one at a time per stock)
        self._quotes: Optional[tuple] = None     # (con_id -> feed, hub prices) fetched for the next cycle
        for cid in COPIES:
            for sym in self.cfg["stocks"]:
                self._bot(cid, sym)

    # ------------------------------------------------------------------ config
    def _load_cfg(self) -> dict:
        c = read_json(CONFIG) or {}
        copies = c.get("copies") or {}
        def cap(k):
            v = (copies.get(k) or {}).get("capital_usd")
            return float(v) if v else None
        out = {"copies": {k: {"enabled": bool((copies.get(k) or {}).get("enabled", False)) and cap(k) is not None,
                              "capital_usd": cap(k)} for k in COPIES},
               "stocks": [x for x in (c.get("stocks") or STOCKS)],
               "max_exposure_usd": float(c.get("max_exposure_usd") or 0)}
        write_json(CONFIG, out)
        return out

    def save_cfg(self):
        write_json(CONFIG, self.cfg)

    def _bot(self, cid: str, sym: str) -> StockBot:
        key = f"{cid}_{sym}"
        if key not in self.bots:
            self.bots[key] = StockBot(cid, sym, self.cfg["copies"][cid]["capital_usd"], self.hub, self.s, self._room)
        return self.bots[key]

    def enabled(self, cid: str) -> bool:
        return self.cfg["copies"][cid]["enabled"]

    @property
    def account(self) -> str:
        return self.engine.s.account_id

    # ------------------------------------------------------------------ controls (API)
    def set_enabled(self, cid: str, on: bool, mode: str = "after_trade"):
        with self.lock:
            if cid not in COPIES:
                raise KeyError(cid)
            if on and not self.cfg["copies"][cid]["capital_usd"]:
                raise ValueError("Enter the amount per trade first")
            self.cfg["copies"][cid]["enabled"] = bool(on)
            self.save_cfg()
            if not on and mode == "close_now":
                for b in self._copy_bots(cid):
                    b.close_now("shutdown")
            self.engine.event(f"{COPIES[cid]['name']} switched {'on' if on else 'off'}"
                              + (" - its trades are being closed" if not on and mode == "close_now" else ""))

    def set_capital(self, cid: str, usd: float):
        """The $ amount every trade of this copy buys (all stocks the same; volume-tier trades up to ~1.8x)."""
        if cid not in COPIES:
            raise KeyError(cid)
        if not 1_000 <= float(usd) <= 10_000_000:
            raise ValueError("the amount per trade must be between $1,000 and $10,000,000")
        with self.lock:
            self.cfg["copies"][cid]["capital_usd"] = float(usd)
            self.save_cfg()
            for b in self._copy_bots(cid):
                b.set_capital(usd)

    def set_exposure(self, usd: float):
        if float(usd) < 0:
            raise ValueError("the limit cannot be negative (0 = no limit)")
        with self.lock:
            self.cfg["max_exposure_usd"] = float(usd)
            self.save_cfg()

    def close_trade(self, bid: str):
        with self.lock:
            self.bots[bid].close_now("manual")

    def close_all(self):
        with self.lock:
            for cid in COPIES:
                self.cfg["copies"][cid]["enabled"] = False
            self.save_cfg()
            for b in self.bots.values():
                b.close_now("manual")
            self.engine.event("Stocks: everything switched off and every stock position is being closed", "warn")

    def resolve_attention(self, bid: str, how: str):
        with self.lock:
            b = self.bots[bid]
            if how == "closed_by_hand":
                b.forget()
            b.attention = ""
            b.save()
            self._mismatch.pop(b.sym, None)
            self._outside.pop(b.sym, None)

    def _copy_bots(self, cid: str) -> List[StockBot]:
        return [b for b in self.bots.values() if b.copy == cid]

    def _room(self, notional: float) -> bool:
        cap = self.cfg.get("max_exposure_usd") or 0
        if cap <= 0:
            return True
        open_ = sum(abs(b.held) * (b.px or 0) for b in self.bots.values())
        return open_ + notional <= cap

    # ------------------------------------------------------------------ loop
    def start(self):
        self.running = True
        threading.Thread(target=self._run, name="stocks", daemon=True).start()

    def stop(self):
        self.running = False
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _run(self):
        while self.running:
            t0 = time.monotonic()
            try:
                self._io()
                with self.lock:
                    self.cycle()
            except Exception:
                log.error("stocks cycle failed:\n" + traceback.format_exc())
            time.sleep(max(0.5, self.s.tick_seconds - (time.monotonic() - t0)))

    def _io(self):
        """Network part of a cycle, without the lock: start bar fetches that are due, read the prices."""
        if not self.engine.hub_state.get("ok"):
            return
        now = pd.Timestamp.now(tz="UTC")
        now_et = now.tz_convert(NY).tz_localize(None).to_pydatetime()
        with self.lock:
            syms = sorted({b.sym for b in self.active()})
            feeds = {s: self.feeds[s] for s in syms if s in self.feeds}
        for sym, f in feeds.items():
            if f.ready and sym not in self._fetch and f.due(now_et):
                self._fetch[sym] = self._pool.submit(f.update, self.hub, now)
        want = {f.con_id: f for f in feeds.values() if f.con_id}
        self._quotes = (want, self.hub.prices([f.contract() for f in want.values()])) if want else None

    def active(self) -> List[StockBot]:
        return [b for b in self.bots.values() if self.enabled(b.copy) or b.busy]

    def cycle(self):
        if not self.engine.hub_state.get("ok"):
            return
        self.hub.account_id = self.account
        act = self.active()
        syms = sorted({b.sym for b in act})
        for sym in syms:
            self._feed(sym)
        now = pd.Timestamp.now(tz="UTC")
        now_et = now.tz_convert(NY).tz_localize(None).to_pydatetime()
        # 1. bars (fetched in the background by _io; only the finished ones are used, nothing waits here)
        for sym, fut in list(self._fetch.items()):
            if not fut.done():
                continue
            del self._fetch[sym]
            try:
                rows = fut.result()
            except Exception:
                log.error(f"[{sym}] bar update failed:\n" + traceback.format_exc())
                continue
            bots = [b for b in act if b.sym == sym and b.ready]
            for row in rows:
                for b in bots:
                    self._guard(b, b.on_minute, row, self.enabled(b.copy))
        # 2. prices (stocks with a position or a working order)
        want, q = self._quotes or ({}, {})
        self._quotes = None
        if want:
            for cid, p in q.items():
                f = want.get(int(cid))
                if f is None:
                    continue
                v = None if p.get("stale") else p.get("last")
                if not v or v <= 0:
                    b_, a_ = p.get("bid") or 0, p.get("ask") or 0
                    v = (b_ + a_) / 2 if b_ > 0 and a_ > 0 else None
                f.current_price = float(v) if v else f.current_price
            m = now_et.hour * 60 + now_et.minute
            in_rth = now_et.weekday() < 5 and 570 <= m < 960
            for b in act:
                f = self.feeds.get(b.sym)
                if f and f.ready and b.ready:
                    self._guard(b, b.on_price, f.current_price if in_rth else None, now_et)
                    if not in_rth:
                        b.px = f.current_price                      # display only: rules act in regular hours
        # 3. orders: regular hours (+1 minute, so an exit decided at the 15:59 candle close is still sent at once)
        m = now_et.hour * 60 + now_et.minute
        if now_et.weekday() < 5 and 570 <= m < 961:
            for b in act:
                f = self.feeds.get(b.sym)
                if f and f.con_id and b.ready:
                    self._guard(b, b.sync, self.account, f.contract())
        # 4. broker check
        if time.monotonic() >= self._next_reconcile:
            self._reconcile()

    def _guard(self, b: StockBot, fn, *a):
        try:
            fn(*a)
        except Exception as exc:
            b.error = f"{type(exc).__name__}: {exc}"
            log.error(f"[{b.id}] {fn.__name__} failed:\n" + traceback.format_exc())

    def _feed(self, sym: str):
        if sym in self.feeds or sym in self._warming:
            return
        f = StockFeed(sym)
        self._warming.add(sym)

        def warm():
            ok = False
            try:
                with _WARM_LOCK:
                    spec = {"symbol": f.ib, "sec_type": "STK", "exchange": "SMART", "currency": "USD"}
                    cid = self.hub.qualify(spec) or self.hub.resolve(spec)
                    f.con_id = cid
                    if not cid:
                        f.error = self.hub.last_error or "contract not found"
                        log.warning(f"[{sym}] no con_id from the hub: {f.error}")
                    ok = bool(cid) and f.warmup(self.hub)
                    if ok:
                        self.hub.subscribe_bars(f.ib, "SMART", sec_type="STK")
            except Exception:
                log.error(f"[{sym}] warm-up failed:\n" + traceback.format_exc())
            if ok:
                # The bots' warm-up (~10 s per stock) runs outside the desk lock: holding it froze the stock loop
                # (prices, stops, orders of the stocks already trading) and the Stocks page meanwhile. Safe, because
                # the loop leaves a stock's bots alone until its feed is in self.feeds.
                for b in [b for b in list(self.bots.values()) if b.sym == sym]:
                    self._guard(b, b.warmup, f.m1, f.daily, f.exact_from)
                with self.lock:
                    self.feeds[sym] = f
                    self._failed.pop(sym, None)
                self.engine.event(f"Stocks: {sym} market data ready")
            else:
                f.error = f.error or "contract not found"
                self._failed[sym] = f.error
                log.warning(f"[{sym}] not ready ({f.error}) - trying again in a minute")
                time.sleep(60)
            self._warming.discard(sym)
        threading.Thread(target=warm, name=f"warm-{sym}", daemon=True).start()

    def _reconcile(self):
        self._next_reconcile = time.monotonic() + 60 * self.s.reconcile_minutes
        if self.s.dry_run:
            return
        by_sym: Dict[str, List[StockBot]] = {}
        for b in self.bots.values():
            if b.held or b.core.pos.side:
                by_sym.setdefault(b.sym, []).append(b)
        if not by_sym:
            return
        pos = self.hub.positions(account=self.account, sec_type="STK")
        if pos is None:
            return
        for sym, bots in by_sym.items():
            if any(b.order for b in bots):
                self._next_reconcile = time.monotonic() + 15
                continue
            f = self.feeds.get(sym)
            cid = f.con_id if f else None
            broker = 0
            for p in pos:
                if (cid and int(p.get("con_id") or 0) == int(cid)) or str(p.get("symbol", "")).upper() == (f.ib if f else sym):
                    acct = [a for a in p.get("accounts") or [] if a.get("account") == self.account]
                    broker = int(round(float(acct[0]["qty"]))) if acct else int(round(float(p.get("total_qty") or p.get("qty") or 0)))
            ours = sum(b.held for b in bots)
            before = self._outside.get(sym)
            out = broker - ours
            if before is None or out == before:
                if before is None and out:
                    self.engine.event(f"Stocks: {sym}: account {self.account} also holds {out:+d} share(s) "
                                      f"Pattern Desk did not buy - left alone")
                self._outside[sym] = out
                self._mismatch.pop(sym, None)
                continue
            self._mismatch[sym] = self._mismatch.get(sym, 0) + 1
            if self._mismatch[sym] < 2:
                self._next_reconcile = time.monotonic() + 30
                continue
            msg = (f"IBKR shows {broker:+d} {sym} in {self.account}, Pattern Desk's stock trades add up to {ours:+d} "
                   f"(other shares seen before: {before:+d}). Orders for {sym} are paused until you check it.")
            for b in bots:
                if not b.attention:
                    b.attention = msg
                    b.save()
            self.engine.event(msg, "warn")

    # ------------------------------------------------------------------ views
    def snapshot(self) -> dict:
        with self.lock:
            copies = []
            for cid, cp in COPIES.items():
                bots = self._copy_bots(cid)
                open_ = [b for b in bots if b.held or b.core.pos.side]
                copies.append({"id": cid, "name": cp["name"], "summary": cp["summary"], "backtest": cp["backtest"],
                               "enabled": self.enabled(cid), "capital_usd": self.cfg["copies"][cid]["capital_usd"],
                               "open": len(open_), "stocks": len(bots),
                               "ready": sum(1 for b in bots if b.ready),
                               "upnl": sum((b.view()["upnl"] or 0) for b in open_)})
            pos = [b.view() for b in self.bots.values() if b.held or b.core.pos.side or b.order or b.attention]
            feeds = {s: {"ready": f.ready, "price": f.current_price, "error": f.error} for s, f in self.feeds.items()}
            waiting = [s for s in self.cfg["stocks"] if s not in self.feeds]
            return {"copies": copies, "positions": sorted(pos, key=lambda x: (x["stock"], x["copy"])),
                    "stocks": self.cfg["stocks"], "feeds": feeds, "warming": waiting if any(self.enabled(c) for c in COPIES) else [],
                    "failed": {**self._failed, **{s: f.error for s, f in self.feeds.items() if f.error}}
                              if any(self.enabled(c) for c in COPIES) else {},
                    "exposure": sum(p["notional"] for p in pos), "max_exposure_usd": self.cfg["max_exposure_usd"],
                    "account": self.account, "dry_run": self.s.dry_run,
                    "how_it_enters": HOW_IT_ENTERS, "how_it_exits": HOW_IT_EXITS,
                    "watch": self._watch()}

    def _watch(self) -> list:
        """Per stock and copy: what the last candle said (inside the box, too calm, ...)."""
        out = []
        for b in self.bots.values():
            if b.ready and not (b.held or b.core.pos.side) and self.enabled(b.copy):
                out.append({"id": b.id, "stock": b.sym, "copy": b.copy, **(b.core.last or {})})
        return sorted(out, key=lambda x: (x["stock"], x["copy"]))

    @staticmethod
    def history() -> list:
        return list(reversed(read_jsonl(JOURNAL, 500)))
