"""
The engine loop (one background thread). Every `tick_seconds`:

  1. bars     once a minute per signal market in use: new completed minutes -> every deployment
              on that market (exits, entries in the current bar); chart-bar closes -> signals
  2. prices   one /price call for every contract in use -> live exits / stop-entry triggers
  3. orders   each deployment works its own order (target-position model)
  4. checks   every few minutes: our contracts still at the broker? (own contracts only)

One deployment failing never stops the others: every call into a deployment is guarded.
"""
from __future__ import annotations

import datetime as dt
import glob
import logging
import threading
import time
import traceback
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from desk import catalog as cat
from desk import logic as L
from desk import markets as mkts
from desk.bars import BarBook, bucket, is_last_minute
from desk.deployment import Deployment, now_iso
from desk.hub import Hub
from desk.logic.base import BarContext
from desk.rollrule import RollBook
from desk.settings import ROOT, STATE, Settings
from desk.store import append_jsonl, read_json, write_json

log = logging.getLogger("desk.engine")
NY = "America/New_York"


def now_ny() -> dt.datetime:
    """Current New York wall-clock time (naive), independent of the computer's time zone."""
    return pd.Timestamp.now(tz=NY).tz_localize(None).to_pydatetime()
DEPLOYMENTS = STATE / "deployments.json"
ACCOUNT = STATE / "account.json"          # default account picked on the web page (beats settings.yaml)
EVENTS = STATE / "events.jsonl"
# state files of the Strategy Manager's futures strategies: contracts they hold are theirs, not "outside"
OTHER_STATES = str(ROOT.parent / "strategy_manager" / "strategies" / "*" / "state" / "*.json")
# one market warms up at a time: IBKR cancels history requests that arrive together (code 162)
_WARM_LOCK = threading.Lock()


def slug(s: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in s.lower()).strip("-")


class Engine:
    def __init__(self, settings: Settings):
        self.s = settings
        picked = str((read_json(ACCOUNT) or {}).get("default") or "").strip()
        if picked:
            settings.account_id = picked
        self.hub = Hub(settings.hub_url, settings.hub_api_key, settings.algo_id, settings.account_id)
        self.lock = threading.RLock()
        self.cards, self.problems = cat.load()
        self.cfgs: List[dict] = self._load_cfgs()
        self.deps: Dict[str, Deployment] = {}
        self.books: Dict[str, BarBook] = {}
        self._warming: set = set()
        self.contracts: Dict[str, tuple] = {}         # market -> (con_id, expiry) of today's front month
        self._contract_day: Dict[str, dt.date] = {}
        self.month_ids: Dict[tuple, Optional[int]] = {}   # (market, YYYYMM) -> con_id
        self._month_tried: Dict[tuple, float] = {}
        self._accounts: Optional[dict] = None
        self._accounts_at = 0.0
        self.px: Dict[int, float] = {}                # con_id -> last price
        self.px_time = 0.0
        self.book_basis: Dict[str, float] = {}        # signal market: front contract - bar series
        self.open_bucket: Dict[tuple, pd.Timestamp] = {}
        self.closed_bucket: Dict[tuple, pd.Timestamp] = {}
        self._shared: Dict[tuple, dict] = {}
        self.hub_state = {"ok": False, "ibkr": False, "message": "Starting…", "checked": 0.0}
        self.registered = False
        self._outside: Dict[tuple, int] = {}          # (account, con_id) -> contracts that are not ours
        self._mismatch: Dict[tuple, int] = {}
        self._next_reconcile = 0.0
        self.started = time.time()
        self.running = False
        self.rolls = RollBook(self.hub)
        self._roll_checking = False
        for c in self.cfgs:
            self._build(c)

    # ================================================================== deployments config
    def _load_cfgs(self) -> List[dict]:
        cfgs = read_json(DEPLOYMENTS)
        if cfgs is None:                                # first start: every strategy on its micro market, OFF
            cfgs = []
            for c in sorted(self.cards.values(), key=lambda c: (c["family"], c["name"])):
                m = c["markets"][0]
                cfgs.append({"id": f"{c['id']}@{m.lower()}", "strategy": c["id"], "market": m,
                             "qty": list(c.get("default_qty") or [0, 1]), "enabled": False,
                             "stop_after_trade": False, "added": now_iso()})
            write_json(DEPLOYMENTS, cfgs)
        return cfgs

    def save_cfgs(self):
        for c in self.cfgs:
            c.pop("_changed", None)
        write_json(DEPLOYMENTS, self.cfgs)

    def _build(self, c: dict) -> Optional[Deployment]:
        card = self.cards.get(c["strategy"])
        if card is None:
            log.error(f"deployment {c['id']}: strategy {c['strategy']!r} not in the catalog")
            return None
        try:
            d = Deployment(c, card, L.make(card), mkts.get(c["market"]), self.hub, self.s)
        except Exception as exc:
            log.error(f"deployment {c['id']} not built: {exc}")
            return None
        d.price_of = lambda cid: self.px.get(int(cid)) if cid else None
        self.deps[c["id"]] = d
        return d

    def add(self, strategy: str, market: str, qty: List[int], account: Optional[str] = None,
            expiry: Optional[str] = None) -> str:
        with self.lock:
            card = self.cards[strategy]
            if market not in card["markets"]:
                raise ValueError(f"{card['name']} trades {card['markets']}, not {market}")
            account = self._check_account(account) if account else None
            expiry = self._check_expiry(market, expiry) if expiry else "auto"
            base = f"{strategy}@{market.lower()}"
            did, n = base, 2
            while did in self.deps:
                did, n = f"{base}-{n}", n + 1
            c = {"id": did, "strategy": strategy, "market": market, "qty": [int(x) for x in qty],
                 "enabled": False, "stop_after_trade": False, "added": now_iso(), "expiry": expiry}
            if account:
                c["account"] = account
            self.cfgs.append(c)
            self._build(c)
            self.save_cfgs()
            self.event(f"Added {card['name']} on {market}")
            return did

    def remove(self, did: str):
        with self.lock:
            d = self.deps[did]
            if d.held or d.trade or d.order:
                raise ValueError("This strategy still has an open trade. Close it first.")
            self.cfgs = [c for c in self.cfgs if c["id"] != did]
            del self.deps[did]
            self.save_cfgs()
            self.event(f"Removed {d.name} on {d.market.symbol}")

    def set_qty(self, did: str, qty: List[int]):
        with self.lock:
            q = [max(0, min(100, int(x))) for x in qty]
            if sum(q) <= 0:
                raise ValueError("At least one contract is needed.")
            d = self.deps[did]
            d.cfg["qty"] = q
            self.save_cfgs()
            self.event(f"{d.name} on {d.market.symbol}: quantity set to {'+'.join(map(str, q))}")

    # ------------------------------------------------------------------ account and contract month
    def accounts(self, max_age: float = 60) -> Optional[dict]:
        """The hub's account list, cached (None = the hub could not say)."""
        if self._accounts is None or time.monotonic() - self._accounts_at > max_age:
            a = self.hub.accounts()
            if a is not None:
                self._accounts, self._accounts_at = a, time.monotonic()
        return self._accounts

    def _check_account(self, account: str) -> str:
        account = str(account or "").strip()
        if not account:
            raise ValueError("Pick an account.")
        a = self.accounts(max_age=0)
        if a is None:
            raise ValueError("The Trading Hub cannot list the IBKR accounts right now. Try again in a moment.")
        managed = [str(x) for x in (a.get("managed") or [r.get("account") for r in a.get("accounts") or []])]
        if account not in managed:
            raise ValueError(f"{account} is not an account of this IBKR login ({', '.join(managed) or 'none'}).")
        return account

    def set_account(self, did: str, account: str):
        with self.lock:
            d = self.deps[did]
            account = self._check_account(account)
            if account == d.account:
                return
            if d.trade or d.held or d.order or d.roll:
                raise ValueError("This strategy has an open trade. Its account can be changed once it is flat.")
            d.cfg["account"] = account
            self.save_cfgs()
            self.event(f"{d.name} on {d.market.symbol}: orders now go to account {account}")

    def set_all_accounts(self, account: str) -> dict:
        """One account for every strategy: the new default, and every strategy that is flat moves to it now.
        Strategies with an open trade keep their account (change them once they are flat)."""
        with self.lock:
            account = self._check_account(account)
            changed, busy = 0, []
            for d in self.deps.values():
                if d.account == account and d.cfg.get("account") == account:
                    continue
                if d.trade or d.held or d.order or d.roll:
                    if d.account != account:
                        d.cfg["account"] = d.account      # pin it: it must not follow the new default mid-trade
                        busy.append(f"{d.name} on {d.market.symbol}")
                    continue
                d.cfg["account"] = account
                changed += 1
            self.s.account_id = account
            self.hub.account_id = account
            write_json(ACCOUNT, {"default": account, "set": now_iso()})
            self.save_cfgs()
            self.event(f"Account for all strategies set to {account} ({changed} changed"
                       + (f", {len(busy)} kept their account until their open trade is closed" if busy else "") + ")")
            return {"account": account, "changed": changed, "busy": busy}

    def month_choices(self, symbol: str) -> List[dict]:
        today = now_ny().date()
        spec = mkts.get(symbol)
        front = self.rolls.month(symbol, today)
        return [{"expiry": e, "label": mkts.label(symbol, e), "front": e == front}
                for e in mkts.expiries(spec, today, 3)]

    def _check_expiry(self, symbol: str, expiry: str) -> str:
        expiry = str(expiry or "auto").strip().lower()
        if expiry == "auto":
            return expiry
        ok = [m["expiry"] for m in self.month_choices(symbol)]
        if expiry not in ok:
            raise ValueError(f"{expiry} cannot be traded on {symbol} now (choices: {', '.join(ok)}).")
        return expiry

    def set_expiry(self, did: str, expiry: str, move_trade: bool = True) -> str:
        """Contract month of a strategy. With an open trade and move_trade, the position moves to that
        month right away (the trade - stop, targets, legs - carries on). Returns what happened, in words."""
        with self.lock:
            d = self.deps[did]
            expiry = self._check_expiry(d.market.symbol, expiry)
            d.cfg["expiry"] = expiry
            d.cfg["roll_open"] = bool(move_trade)
            self.save_cfgs()
            want = "the front month (automatic)" if expiry == "auto" else mkts.label(d.market.symbol, expiry)
            self.event(f"{d.name} on {d.market.symbol}: contract month set to {want}")
            if not (d.held and d.trade):
                return f"New trades use {want}."
            if not move_trade:
                return (f"The open trade stays in {mkts.label(d.market.symbol, d.pos_expiry)}; "
                        f"new trades use {want}.")
            if expiry != "auto":
                self._month_id(d.market.symbol, expiry)
            cid, exp = self._contract_for(d)
            if not cid:
                return f"IBKR does not know {want} yet; the trade stays where it is."
            if int(cid) == int(d.pos_con_id):
                return "The open trade is already in that month."
            return self.roll(did, cid, exp, "contract month changed")

    def roll(self, did: str, cid: int, expiry: str, why: str) -> str:
        d = self.deps[did]
        old = mkts.label(d.market.symbol, d.pos_expiry)
        new = mkts.label(d.market.symbol, expiry)
        if not self.s.dry_run and (self.px.get(int(cid)) is None or self.px.get(int(d.pos_con_id)) is None):
            return f"Moving {old} to {new} as soon as both months have a live price."
        res = d.start_roll(cid, expiry, why)
        if res:
            raise ValueError(res)
        self.event(f"{d.name}: moving {abs(d.held)} {old} to {new} ({why}) - the trade carries on")
        return f"Moving {abs(d.held)} {old} to {new}. The trade, its stop and its targets stay as they are."

    def roll_next(self, did: str) -> str:
        """Live page button: move the open trade to the month after the one it is in."""
        with self.lock:
            d = self.deps[did]
            if not d.held or not d.pos_expiry:
                raise ValueError("There is no open position to move.")
            later = [m["expiry"] for m in self.month_choices(d.market.symbol) if m["expiry"] > d.pos_expiry]
            if not later:
                raise ValueError("No later contract month is available.")
            return self.set_expiry(did, later[0], move_trade=True)

    def _check_accounts(self, act: List[Deployment]):
        """New entries only into accounts this IBKR login manages. (On a one-account login the hub would
        route an unknown account to the only account there is - so the desk checks it itself.)"""
        a = self.accounts(max_age=120)
        if a is None:
            return                                        # cannot say: keep the last verdict
        managed = {str(x) for x in (a.get("managed") or [r.get("account") for r in a.get("accounts") or []])}
        for d in self.deps.values():
            acct = d.trade_account if (d.held or d.trade or d.order or d.roll) else d.account
            bad = "" if not managed or acct in managed else (
                f"Account {acct} is not in this IBKR login ({', '.join(sorted(managed))}). No orders at all "
                f"for this strategy until that account is logged in again" +
                (" - its open position is NOT managed." if d.held else " (menu: Account and contract month)."))
            if bad and not d.account_bad:
                self.event(f"{d.name} on {d.market.symbol}: {bad}", "warn")
            d.account_bad = bad

    def _auto_roll(self, act: List[Deployment]):
        """An open trade follows the strategy's contract month: on the roll date (auto) or after the
        operator picked another month. Needs a live price in both months (= the market is open)."""
        if not getattr(self.s, "auto_roll", True):
            return
        for d in act:
            if not (d.held and d.trade and d.trade.is_open and d.pos_con_id) or d.roll or d.order or d.attention:
                continue
            if d.expiry_choice != "auto" and not d.cfg.get("roll_open", True):
                continue                                  # the operator chose: keep the trade where it is
            cid, exp = self._contract_for(d)
            if not cid or int(cid) == int(d.pos_con_id):
                continue
            if d.expiry_choice == "auto" and exp < (d.pos_expiry or ""):
                continue                                  # auto never moves a trade back to an earlier month
            if self.px.get(int(cid)) is None or self.px.get(int(d.pos_con_id)) is None:
                continue
            if d.can_roll():
                continue
            why = ("next month has more volume" if d.expiry_choice == "auto" else "contract month changed")
            try:
                self.roll(d.id, cid, exp, why)
            except ValueError as exc:
                log.warning(f"[{d.id}] roll not started: {exc}")

    def set_enabled(self, did: str, on: bool, mode: str = "after_trade"):
        """mode when switching off with an open trade: 'close' = close it now, 'after_trade' = let it finish."""
        with self.lock:
            d = self.deps[did]
            if on:
                if d.attention:
                    raise ValueError("This strategy needs attention first (see the Live page).")
                d.cfg["enabled"] = True
                d.cfg["stop_after_trade"] = False
                self.event(f"Switched ON: {d.name} on {d.market.symbol}")
            else:
                d.plans = []
                if d.trade or d.held:
                    if mode == "close":
                        d.cfg["enabled"] = False
                        d.close_now("shutdown")
                        self.event(f"Switched OFF and closing the trade: {d.name} on {d.market.symbol}")
                    else:
                        d.cfg["stop_after_trade"] = True
                        self.event(f"Will switch OFF after the open trade: {d.name} on {d.market.symbol}")
                else:
                    d.cfg["enabled"] = False
                    self.event(f"Switched OFF: {d.name} on {d.market.symbol}")
                d.save()
            self.save_cfgs()

    def close_trade(self, did: str):
        with self.lock:
            d = self.deps[did]
            d.close_now("manual")
            self.event(f"Closing by hand: {d.name} on {d.market.symbol}")

    def close_everything(self):
        with self.lock:
            for d in self.deps.values():
                d.cfg["enabled"] = False
                d.cfg["stop_after_trade"] = False
                if d.trade or d.held:
                    d.close_now("manual")
                d.plans = []
                d.save()
            self.save_cfgs()
            self.event("EMERGENCY: every strategy switched off, all Pattern Desk trades closing")

    def resolve_attention(self, did: str, how: str):
        """how: 'correct' = positions are right, carry on | 'closed_by_hand' = forget this trade."""
        with self.lock:
            d = self.deps[did]
            if d.roll and d.roll.get("failed"):
                if how == "correct":
                    d.finish_failed_roll()                 # the operator moved the rest by hand
                else:
                    d.roll = None
            if how == "closed_by_hand":
                if d.trade:
                    d.trade.close_all("manual", d.signal_px or float("nan"), now_iso())
                d.held = 0
                d.order = None
                d.pending_exits = []
                d._finish_if_done()
                d.trade, d.meta = None, {}
            d.attention = ""
            for key in list(self._outside) + list(self._mismatch):
                if d.pos_con_id is None or key[1] == d.pos_con_id:
                    self._outside.pop(key, None)
                    self._mismatch.pop(key, None)
            d.save()
            self.event(f"Attention cleared ({how.replace('_', ' ')}): {d.name} on {d.market.symbol}")

    def reload_catalog(self) -> List[str]:
        with self.lock:
            cards, problems = cat.load()
            self.cards, self.problems = cards, problems
            for c in self.cfgs:
                d = self.deps.get(c["id"])
                if c["strategy"] in cards and d is not None and not (d.trade or d.held or d.order):
                    self._build(c)
            return problems

    def event(self, text: str, level: str = "info"):
        append_jsonl(EVENTS, {"time": now_iso(), "level": level, "text": text})
        getattr(log, "warning" if level == "warn" else "info")(text)

    # ================================================================== thread
    def start(self):
        self.running = True
        threading.Thread(target=self._run, name="engine", daemon=True).start()

    def stop(self):
        self.running = False
        with self.lock:
            for b in self.books.values():
                b.close()

    def _run(self):
        while self.running:
            t0 = time.monotonic()
            try:
                with self.lock:
                    self.cycle()
            except Exception:
                log.error("engine cycle failed:\n" + traceback.format_exc())
            time.sleep(max(0.2, self.s.tick_seconds - (time.monotonic() - t0)))

    # ================================================================== one cycle
    def active(self) -> List[Deployment]:
        """Deployments that need data: switched on, or still holding / working something."""
        return [d for d in self.deps.values() if d.enabled or d.trade or d.held or d.order or d.roll]

    def cycle(self):
        now = pd.Timestamp.now(tz="UTC")
        self._health()
        if not self.hub_state["ok"]:
            return
        if not self.registered:
            self.registered = self.hub.register()
        act = self.active()
        for sym in sorted({d.market.signal_symbol for d in act}):
            self._book(sym)
        self._contracts(act)
        # ---- 1. bars
        for sym, book in self.books.items():
            if not book.ready or not book.due(now.tz_convert(NY).tz_localize(None)):
                continue
            deps = [d for d in act if d.market.signal_symbol == sym]
            rows = book.update(self.hub, now)
            if rows:
                rows = self._series(sym, book, deps, rows)
            for row in rows:
                self._minute(sym, book, row, deps)
            self._basis(sym, book)
        # ---- 2. prices
        self._prices(act, now)
        self._check_accounts(act)
        self._auto_roll(act)
        # ---- 3. orders (deployments whose events did not already sync)
        for d in act:
            self._guard(d, d.sync)
            if d.cfg.get("_changed"):
                self.save_cfgs()
        # ---- 4. broker check
        if time.monotonic() >= self._next_reconcile:
            self._reconcile(act)

    def _guard(self, d: Deployment, fn, *a):
        try:
            fn(*a)
        except Exception as exc:
            d.error = f"{type(exc).__name__}: {exc}"
            log.error(f"[{d.id}] {fn.__name__} failed:\n" + traceback.format_exc())

    # ------------------------------------------------------------------ hub health
    def _health(self):
        if time.monotonic() - self.hub_state["checked"] < 10:
            return
        h = self.hub.health()
        self.hub_state["checked"] = time.monotonic()
        if h is None:
            self.hub_state.update(ok=False, ibkr=False, message="Trading Hub is not running (port 8000).")
            return
        ibkr = bool(h.get("oms_connected") and h.get("data_connected", True) and h.get("mds_connected"))
        msg = "Connected" if ibkr else "Trading Hub is up, but IBKR (TWS / Gateway) is not connected."
        if h.get("blocker"):
            b = h["blocker"]
            msg = str(b.get("message") or b.get("reason") or b)
        self.hub_state.update(ok=True, ibkr=ibkr, message=msg)

    # ------------------------------------------------------------------ bars
    def _book(self, sym: str):
        if sym in self.books or sym in self._warming:
            return
        spec = mkts.get(sym)
        book = BarBook(sym, spec.exchange, self.s.warmup_days, self.s.bar_settle_seconds)
        self._warming.add(sym)

        def warm():
            ok = False
            try:
                with _WARM_LOCK:
                    ok = book.warmup(self.hub)
            except Exception:
                log.error(f"[{sym}] warm-up failed:\n" + traceback.format_exc())
            if ok:
                self.hub.subscribe_bars(sym, spec.exchange)
                with self.lock:
                    self.books[sym] = book
                    last = book.df.t.iloc[-1]
                    for tf in {d.logic.timeframe for d in self.deps.values() if d.market.signal_symbol == sym}:
                        self.open_bucket[(sym, tf)] = bucket(last, tf)
                    self.event(f"{sym} market data ready")
            else:
                time.sleep(30)                            # then the next cycle starts a new warm-up
            self._warming.discard(sym)
        threading.Thread(target=warm, name=f"warm-{sym}", daemon=True).start()

    def _minute(self, sym: str, book: BarBook, row, deps: List[Deployment]):
        tfs = sorted({d.logic.timeframe for d in deps})
        # a bar whose last minute never printed closes when the next bar's first minute arrives
        for tf in tfs:
            ob = self.open_bucket.get((sym, tf))
            b = bucket(row.t, tf)
            if ob is not None and b > ob and self.closed_bucket.get((sym, tf)) != ob:
                self._bar_close(sym, book, tf, ob, upto=b, deps=deps)
        for d in deps:
            self._guard(d, d.on_minute, row, self._contract_for(d))
        for tf in tfs:
            b = bucket(row.t, tf)
            self.open_bucket[(sym, tf)] = b
            if is_last_minute(row.t, tf):
                self._bar_close(sym, book, tf, b, upto=row.t + pd.Timedelta(minutes=1), deps=deps)

    def _bar_close(self, sym, book, tf, bar_open, upto, deps):
        self.closed_bucket[(sym, tf)] = bar_open
        mine = [d for d in deps if d.logic.timeframe == tf]
        if not mine:
            return
        m1 = book.window(upto)
        if m1.empty:
            return
        key = (sym, upto)
        if key not in self._shared:
            self._shared = {key: {}}                      # one LiveMarket per market and minute, shared
        shared = self._shared[key]
        ctx = BarContext(symbol=sym, m1=m1, now_et=now_ny(), shared=shared)
        close = float(m1.close.iloc[-1])
        t0 = time.monotonic()
        for d in mine:
            self._guard(d, d.on_bar_close, bar_open, close, ctx, self._contract_for(d))
        took = time.monotonic() - t0
        if took > 5:
            log.warning(f"[{sym}] {tf}-minute bar close took {took:.1f}s for {len(mine)} strategies")

    def _basis(self, sym: str, book: BarBook):
        """Front contract minus the bar series (0 while both are the same month)."""
        cid = (self.contracts.get(sym) or (None,))[0]
        px = self.px.get(cid) if cid else None
        if px and book.current_price:
            spec = mkts.get(sym)
            b = spec.round_tick(px - book.current_price)
            self.book_basis[sym] = 0.0 if abs(b) <= 2 * spec.tick + 1e-9 else b

    # ------------------------------------------------------------------ contracts and prices
    def _contracts(self, act: List[Deployment]):
        today = now_ny().date()
        need = {d.market.symbol for d in act} | {d.market.signal_symbol for d in act}
        self._volume_checks(need)
        for d in act:                                     # explicit months chosen by the operator
            if d.expiry_choice != "auto" and mkts.holdable(d.market, d.expiry_choice, today):
                self._month_id(d.market.symbol, d.expiry_choice)
        for sym in need:
            if self._contract_day.get(sym) == today and self.contracts.get(sym, (None,))[0]:
                continue
            spec = mkts.get(sym)
            expiry = self.rolls.month(sym, today)
            cid = self._month_id(sym, expiry) if expiry != (self.contracts.get(sym) or (None, None))[1] \
                else self.contracts[sym][0]
            cid = cid or self.hub.resolve(spec.contract(expiry))
            if cid:
                self.contracts[sym] = (cid, expiry)
                self._contract_day[sym] = today
                log.info(f"[{sym}] trading {mkts.label(sym, expiry)} (con_id {cid})")
            else:
                self._contract_day[sym] = today       # retried tomorrow or at restart; logged by the hub
                self.contracts.setdefault(sym, (None, expiry))

    def _month_id(self, sym: str, expiry: str) -> Optional[int]:
        """con_id of one contract month (resolved once; a miss is retried every 10 minutes)."""
        key = (sym, expiry)
        front = self.contracts.get(sym)
        if front and front[1] == expiry and front[0]:
            return front[0]
        if self.month_ids.get(key):
            return self.month_ids[key]
        if time.monotonic() - self._month_tried.get(key, -1e9) < 600:
            return None
        self._month_tried[key] = time.monotonic()
        cid = self.hub.resolve(mkts.get(sym).contract(expiry))
        self.month_ids[key] = cid
        if cid:
            log.info(f"[{sym}] {mkts.label(sym, expiry)} is con_id {cid}")
        else:
            log.warning(f"[{sym}] {mkts.label(sym, expiry)}: IBKR does not know this contract")
        return cid

    # ------------------------------------------------------------------ monthly volume rule
    def _volume_checks(self, syms):
        """Once per trading day per instrument, in the background (2 history calls each)."""
        due = sorted(s for s in syms if self.rolls.due(s))
        if not due or self._roll_checking:
            return
        self._roll_checking = True

        def run():
            try:
                for sym in due:
                    before = self.rolls.month(sym)
                    try:
                        st = self.rolls.check(sym)
                    except Exception:
                        log.error(f"[{sym}] volume check failed:\n" + traceback.format_exc())
                        continue
                    after = self.rolls.month(sym)
                    if st.get("volumes"):
                        log.info(f"[{sym}] volume check {st.get('session')}: " + ", ".join(
                            f"{mkts.label(sym, m)} {v:,}" for m, v in st["volumes"].items())
                            + f" -> trading {mkts.label(sym, after)}")
                    if after != before:
                        with self.lock:
                            self._contract_day.pop(sym, None)       # picked up by the next cycle
                            self.event(f"{sym}: now trading {mkts.label(sym, after)} - {st.get('reason', '')}")
            finally:
                self._roll_checking = False
        threading.Thread(target=run, name="volume-check", daemon=True).start()

    def roll_status(self) -> List[dict]:
        syms = sorted({d.market.symbol for d in self.deps.values() if d.enabled or d.held}
                      | {d.market.signal_symbol for d in self.deps.values() if d.enabled or d.held})
        return [self.rolls.status(s) for s in syms]

    def check_volumes_now(self) -> List[dict]:
        for s in [r["symbol"] for r in self.roll_status()]:
            st = self.rolls.state.get(s)
            if st:
                st.pop("checked_on", None)
            self.rolls._retry_at.pop(s, None)
        return self.roll_status()

    def set_roll_ratio(self, r: float) -> float:
        r = self.rolls.set_ratio(r)
        self.event(f"Contract roll: switch to the next month when it trades {r:g}x the volume of the current one")
        return r

    # ------------------------------------------------------------------ the bar series' month
    def _series(self, sym: str, book: BarBook, deps: List[Deployment], rows: list) -> list:
        """Which month is the hub's continuous stream on? If it moved since the last minute, shift the
        older minutes and every open trade on `sym` by the jump, BEFORE the new minutes are used."""
        spec = mkts.get(sym)
        today = now_ny().date()
        pts = {}
        for m in mkts.expiries(spec, today, 2):
            cid = self._month_id(sym, m)
            if cid and self.px.get(int(cid)):
                pts[m] = self.px[int(cid)]
        cur = book.current_price
        if len(pts) < 2 or not cur:
            return rows
        (m1, p1), (m2, p2) = sorted(pts.items(), key=lambda kv: abs(kv[1] - cur))[:2]
        spread = abs(p1 - p2)
        if spread < 8 * spec.tick or abs(p1 - cur) > 0.25 * spread:
            return rows                                    # cannot tell the months apart right now
        old = book.series_month
        if old is None or old == m1:
            book.series_month = m1
            return rows
        book.series_month = m1
        if old not in pts:
            return rows
        jump = spec.round_tick(pts[m1] - pts[old])
        tail = book.df.tail(40)
        t_b = rows[0].t
        if len(tail) > 1:
            gaps = tail.open.to_numpy()[1:] - tail.close.to_numpy()[:-1]
            k = int(abs(gaps - jump).argmin())
            if abs(gaps[k] - jump) <= 0.35 * abs(jump):
                t_b = tail.t.iloc[k + 1]
        book.adjust.append((t_b, jump))
        for d in self.deps.values():
            if d.market.signal_symbol == sym:
                self._guard(d, d.series_shift, jump)
        self._shared = {}
        self.event(f"{sym} price data moved from {mkts.label(sym, old)} to {mkts.label(sym, m1)} "
                   f"(jump {jump:+g}): older candles and open trades adjusted, entries re-armed at the next candle",
                   "warn")
        return [book.adjust_row(r) for r in rows]

    def _contract_for(self, d: Deployment) -> tuple:
        """(con_id, expiry) new trades of d use: its chosen month, or the front month (auto, or once the
        chosen month is too close to expiry)."""
        choice = d.expiry_choice
        if choice != "auto" and mkts.holdable(d.market, choice, now_ny().date()):
            cid = self.month_ids.get((d.market.symbol, choice))
            front = self.contracts.get(d.market.symbol)
            if not cid and front and front[1] == choice:
                cid = front[0]
            return (cid, choice) if cid else (None, choice)
        return self.contracts.get(d.market.symbol) or (None, None)

    def _prices(self, act: List[Deployment], now: pd.Timestamp):
        want = {}
        today = now_ny().date()
        for sym in {d.market.signal_symbol for d in act}:
            for m in mkts.expiries(mkts.get(sym), today, 2):   # to tell which month the bar stream is on
                cid = self.month_ids.get((sym, m)) or (
                    self.contracts.get(sym, (None, None))[0] if self.contracts.get(sym, (None, None))[1] == m else None)
                if cid:
                    want[int(cid)] = mkts.get(sym).contract(m, cid)
        for d in act:
            for sym in (d.market.symbol, d.market.signal_symbol):
                cid, exp = self.contracts.get(sym) or (None, None)
                if cid:
                    want[cid] = mkts.get(sym).contract(exp, cid)
            if d.pos_con_id and d.pos_expiry:
                want[int(d.pos_con_id)] = d.market.contract(d.pos_expiry, d.pos_con_id)
            cid, exp = self._contract_for(d)
            if cid and exp:
                want[int(cid)] = d.market.contract(exp, cid)
            for lg in (d.roll or {}).get("legs") or []:
                want[int(lg["con_id"])] = d.market.contract(lg["expiry"], lg["con_id"])
        if not want:
            return
        q = self.hub.prices(list(want.values()))
        for cid, p in q.items():
            if p.get("stale"):
                self.px.pop(cid, None)
                continue
            v = p.get("last")
            if not v or v <= 0:
                b, a = p.get("bid") or 0, p.get("ask") or 0
                v = (b + a) / 2 if b > 0 and a > 0 else None
            if v:
                self.px[cid] = float(v)
        self.px_time = time.monotonic()
        for d in act:
            scid = (self.contracts.get(d.market.signal_symbol) or (None,))[0]
            sp = self.px.get(scid) if scid else None
            sig = sp - self.book_basis.get(d.market.signal_symbol, 0.0) if sp else None
            tcid = d.pos_con_id or (self.contracts.get(d.market.symbol) or (None,))[0]
            d.trade_px = self.px.get(int(tcid)) if tcid else None
            book = self.books.get(d.market.signal_symbol)
            if book is None or not book.ready:
                continue                               # no entries / exits before the warm-up is done
            self._guard(d, d.on_price, sig, now, self._contract_for(d))

    # ------------------------------------------------------------------ broker check
    @staticmethod
    def _sm_account(state_file: str) -> Optional[str]:
        """Account of a Strategy Manager strategy (its config.yaml algo.account_id), None = unknown."""
        cfg = Path(state_file).parent.parent / "config.yaml"
        try:
            import yaml
            d = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            a = (d.get("algo") or {}).get("account_id") or d.get("account_id")
            return str(a) if a else None
        except Exception:
            return None

    def _others_on(self, con_id: int, account: Optional[str] = None) -> int:
        """Contracts the Strategy Manager's futures strategies hold on con_id in `account` (state files)."""
        n = 0
        for f in glob.glob(OTHER_STATES):
            snap = read_json(Path(f)) or {}
            if isinstance(snap, dict) and snap.get("pos_con_id") and int(snap["pos_con_id"]) == int(con_id):
                acct = snap.get("account_id") or snap.get("account") or self._sm_account(f)
                if account and acct and str(acct) != str(account):
                    continue                              # held in another account
                n += int(snap.get("held") or 0)
        return n

    def _reconcile(self, act: List[Deployment]):
        self._next_reconcile = time.monotonic() + 60 * self.s.reconcile_minutes
        if self.s.dry_run:
            return
        by_key: Dict[tuple, List[Deployment]] = {}
        for d in self.deps.values():
            if d.pos_con_id and (d.held or d.trade):
                by_key.setdefault((d.trade_account, int(d.pos_con_id)), []).append(d)
        for key, deps in by_key.items():
            account, cid = key
            if any(d.order or d.roll for d in deps):
                self._next_reconcile = time.monotonic() + 15     # an order is working: look again soon
                continue
            spec = deps[0].market
            pos = self.hub.positions(spec.broker_symbol, account=account)
            if pos is None:
                continue                                          # hub could not see positions: never "flat"
            broker = 0
            for p in pos:
                if int(p.get("con_id") or 0) == cid:
                    acct = [a for a in p.get("accounts") or [] if a.get("account") == account]
                    broker = int(round(float(acct[0]["qty"]))) if acct else 0
            ours = sum(d.held for d in deps)
            rest = broker - self._others_on(cid, account)  # without the Strategy Manager's contracts
            ok = ours == 0 or (rest * ours > 0 and abs(rest) >= abs(ours))
            before = self._outside.get(key)
            if not ok and before is not None and rest - before == ours:
                ok = True                                  # an opposite outside position nets ours away
            if ok:
                out = rest - ours
                if out and before != out:
                    self.event(f"{mkts.label(spec.symbol, deps[0].pos_expiry)}: account {account} also holds "
                               f"{out:+d} contract(s) Pattern Desk did not open - left alone")
                self._outside[key] = out
                self._mismatch.pop(key, None)
                continue
            self._mismatch[key] = self._mismatch.get(key, 0) + 1
            if self._mismatch[key] < 2:                     # check once more before raising the alarm
                self._next_reconcile = time.monotonic() + 30
                continue
            names = ", ".join(f"{d.name} {d.held:+d}" for d in deps)
            msg = (f"IBKR shows {broker:+d} {spec.symbol} in {account} but Pattern Desk's own trades add up to {ours:+d} "
                   f"({names}). Orders are paused for this contract until you check it.")
            for d in deps:
                if not d.attention:
                    d.attention = msg
                    d.save()
            self.event(msg, "warn")
