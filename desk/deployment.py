"""
Deployment: one strategy running on one market with the operator's quantity.

It owns:
  * plans    - armed entries from the last chart bar (stop entries live for one bar)
  * trade    - the open trade's exit rules (desk.trade.Trade)
  * held     - contracts THIS deployment holds at the broker = the sum of its own filled orders.
               Nothing else in the account is ever counted, closed or added to.
  * order    - at most one working hub order; the rules say what to hold, the order fixes the difference
  * roll     - moving the open position to another contract month (two orders in one hub batch:
               close the old month, open the new one). The trade itself - stop, targets, legs - is kept.
Every change is saved to state/deployments/<id>.json, so a restart continues where it stopped.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import time
import uuid
from typing import List, Optional

import pandas as pd

from desk import markets as mkts
from desk.bars import bucket
from desk.logic.base import Logic, Plan
from desk.settings import STATE
from desk.store import append_jsonl, read_json, write_json
from desk.trade import REASON, Exit, Trade

log = logging.getLogger("desk.deploy")
NAN = float("nan")
JOURNAL = STATE / "journal.jsonl"
_POLL_SECS = 1.5
_RETRY_SECS = 20


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Deployment:
    def __init__(self, cfg: dict, card: dict, logic: Logic, market: mkts.Market, hub, settings):
        self.id: str = cfg["id"]
        self.cfg = cfg
        self.card = card
        self.logic = logic
        self.market = market
        self.hub = hub
        self.settings = settings
        self.rules = logic.exit_rules()
        self.path = STATE / "deployments" / f"{self.id}.json"
        # ---- runtime state (saved)
        self.plans: List[Plan] = []
        self.trade: Optional[Trade] = None
        self.meta: dict = {}                 # open trade: id, times, fills, basis, contract
        self.last_anchor = {"1": None, "-1": None}
        self.day = {"session": None, "count": 0}
        self.held = 0
        self.pos_con_id: Optional[int] = None
        self.pos_expiry: Optional[str] = None
        self.pos_account: Optional[str] = None   # account the open position is in
        self.order: Optional[dict] = None
        self.roll: Optional[dict] = None
        self.pending_exits: List[dict] = []  # rule exits not yet sent in an order
        self.closed_bar: Optional[str] = None
        self.attention = ""
        self.last_signal: Optional[str] = None
        self.error = ""
        # ---- not saved
        self.signal_px: Optional[float] = None
        self.trade_px: Optional[float] = None
        self._retry_at = 0.0
        self._rejects = 0
        self._unanswered = None
        self.now = lambda: pd.Timestamp.now(tz="UTC")     # replaced by the replay test's clock
        self.price_of = lambda con_id: None                # live price of any contract (set by the engine)
        self.account_bad = ""                              # set by the engine: account not in the IBKR login
        self.restore()

    # ================================================================== config
    @property
    def name(self) -> str:
        return self.card["name"]

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled"))

    @property
    def account(self) -> str:
        """IBKR account new trades go to (the row's choice, else the desk default)."""
        return str(self.cfg.get("account") or getattr(self.settings, "account_id", "") or "")

    @property
    def trade_account(self) -> str:
        """Account of the position being worked (an open trade stays in the account it was opened in)."""
        return self.pos_account or self.account

    @property
    def expiry_choice(self) -> str:
        """'auto' = the front month (rolls by itself) | 'YYYYMM' = that contract month."""
        return str(self.cfg.get("expiry") or "auto")

    @property
    def qty(self) -> List[int]:
        q = self.cfg.get("qty") or self.card.get("default_qty") or [0, 1]
        return [max(0, int(q[0] or 0)), max(0, int(q[1] or 0))]

    def leg_qty(self, boost: bool) -> List[int]:
        """Operator quantities; on a volume-boost signal scaled like the research boost (double = x2)."""
        q = self.qty
        if not boost or not hasattr(self.logic, "leg_qty"):
            return q
        base, big = self.logic.leg_qty(False), self.logic.leg_qty(True)
        return [int(round(qk * (b / a))) if a else qk for qk, a, b in zip(q, base, big)]

    @property
    def risk_usd(self) -> float:
        """$ risk per trade (this deployment's own "risk_usd", else the card's); 0 = trade the quantity boxes."""
        v = self.cfg.get("risk_usd")
        if v is None:
            v = getattr(self.logic, "risk_usd", 0.0)
        return max(0.0, float(v or 0.0))

    @property
    def max_contracts(self) -> int:
        """Safety cap for risk sizing (card data.max_contracts, deployment "max_contracts")."""
        v = self.cfg.get("max_contracts") or (self.card.get("data") or {}).get("max_contracts") or 20
        return max(1, int(v))

    def risk_qty(self, q: List[int], risk_pts: float) -> List[int]:
        """Whole contracts so the trade risks at most risk_usd at its stop: N = floor(risk_usd / (risk x $ per point)),
        capped at max_contracts, split in the quantity boxes' ratio (largest remainder, ties to leg 1).
        The research sizes in fractions; live rounds DOWN, and N < 1 skips the trade (never risks more)."""
        per = risk_pts * self.market.point_value
        if per <= 0:
            return [0, 0]
        n = int(math.floor(self.risk_usd / per + 1e-9))
        if n > self.max_contracts:
            log.info(f"[{self.id}] risk sizing {n} contracts capped at {self.max_contracts}")
            n = self.max_contracts
        if n < 1:
            log.info(f"[{self.id}] skipped: 1 {self.market.symbol} risks ${per:,.0f} at the stop, "
                     f"more than the ${self.risk_usd:,.0f} limit")
            return [0, 0]
        w = [max(0, int(x)) for x in q]
        if w[0] and w[1]:
            raw = [n * w[0] / sum(w), n * w[1] / sum(w)]
            out = [int(math.floor(x)) for x in raw]
            left = n - sum(out)
            order = sorted((0, 1), key=lambda k: (-(raw[k] - out[k]), k))
            for k in order[:left]:
                out[k] += 1
            return out
        return [n, 0] if w[0] else [0, n]

    @property
    def can_open(self) -> bool:
        return (self.enabled and not self.cfg.get("stop_after_trade") and not self.attention
                and not self.account_bad)

    # ================================================================== persistence
    def save(self):
        write_json(self.path, {
            "plans": [p.to_dict() for p in self.plans],
            "trade": self.trade.to_dict() if self.trade else None,
            "meta": self.meta, "last_anchor": self.last_anchor, "day": self.day, "held": self.held,
            "pos_con_id": self.pos_con_id, "pos_expiry": self.pos_expiry, "pos_account": self.pos_account,
            "order": self.order, "roll": self.roll,
            "pending_exits": self.pending_exits, "closed_bar": self.closed_bar, "attention": self.attention,
            "last_signal": self.last_signal, "saved": now_iso()})

    def restore(self):
        d = read_json(self.path) or {}
        if not d:
            return
        self.plans = [Plan.from_dict(p) for p in d.get("plans") or []]
        self.trade = Trade.from_dict(d["trade"]) if d.get("trade") else None
        self.meta = d.get("meta") or {}
        self.last_anchor = d.get("last_anchor") or self.last_anchor
        self.day = d.get("day") or self.day
        self.held = int(d.get("held") or 0)
        self.pos_con_id = d.get("pos_con_id")
        self.pos_expiry = d.get("pos_expiry")
        self.pos_account = d.get("pos_account")
        self.order = d.get("order")
        self.roll = d.get("roll")
        self.pending_exits = d.get("pending_exits") or []
        self.closed_bar = d.get("closed_bar")
        self.attention = d.get("attention") or ""
        self.last_signal = d.get("last_signal")
        if self.held or self.trade:
            log.info(f"[{self.id}] restored: holding {self.held:+d}, trade {'open' if self.trade else 'none'}")

    # ================================================================== events from the engine
    def on_minute(self, row, contract: tuple):
        """A completed 1-minute candle of the signal market (row.t = open, UTC)."""
        t = row.t
        when = t.isoformat()
        if self.trade and self.trade.is_open:
            ex = self.trade.on_minute(row.open, row.high, row.low, row.close, when, self.rules)
            self._exits(ex)
        for p in list(self.plans):
            if p not in self.plans or p.kind != "stop":
                continue
            if not p.final and t >= pd.Timestamp(p.after):
                if not self._fix(p, bucket(t, self.logic.timeframe)):
                    continue
            if not p.final:
                continue
            if t >= pd.Timestamp(p.valid_until):
                self._drop(p)
                continue
            if t >= pd.Timestamp(p.entry_bar) and self._flat_for_entry(p.entry_bar):
                hit = row.high >= p.level if p.side > 0 else row.low <= p.level
                if hit:
                    px = max(p.level, row.open) if p.side > 0 else min(p.level, row.open)
                    self._enter(p, px, when, contract)
                    if self.trade and Trade.STRICT_ENTRY_MINUTE:   # replay: backtest fill-minute stop
                        self.sync()                                # the entry order goes first
                        self._exits(self.trade.on_minute(row.open, row.high, row.low, row.close, when, self.rules))
        self.sync()

    def on_price(self, px: Optional[float], now: pd.Timestamp, contract: tuple):
        """A live price of the signal market (every tick_seconds)."""
        self.signal_px = px
        if px is None:
            return
        when = now.isoformat()
        if self.trade and self.trade.is_open:
            self._exits(self.trade.on_price(px, when))
        for p in list(self.plans):
            if p not in self.plans or p.kind != "stop":
                continue
            if not p.final and now >= pd.Timestamp(p.after):
                if not self._fix(p, bucket(now, self.logic.timeframe)):
                    continue
            if not p.final:
                continue
            if now >= pd.Timestamp(p.valid_until):
                self._drop(p)
                self.save()
                continue
            if now >= pd.Timestamp(p.entry_bar) and self._flat_for_entry(p.entry_bar):
                if (p.side > 0 and px >= p.level) or (p.side < 0 and px <= p.level):
                    self._enter(p, px, when, contract)
        self.sync()

    def on_bar_close(self, bar_open: pd.Timestamp, close: float, ctx, contract: tuple):
        """A chart bar of this strategy's timeframe closed; ctx = BarContext for the logic."""
        when = (bar_open + pd.Timedelta(minutes=self.logic.timeframe)).isoformat()
        if self.trade and self.trade.is_open:
            flat = self._bar_flat(bar_open)
            self._exits(self.trade.on_bar_close(close, flat, self.rules, when))
        if not self.enabled:
            self.plans = []
            self.sync()
            return
        try:
            plans = self.logic.on_bar_close(ctx)
            self.error = ""
        except Exception as exc:                       # one strategy's failure never stops the others
            self.error = f"signal error: {type(exc).__name__}: {exc}"
            log.exception(f"[{self.id}] on_bar_close failed")
            plans = []
        self.plans = [p for p in plans if p.kind == "stop"]   # a new bar replaces the armed entries
        for p in plans:
            self.last_signal = p.signal_time
            if p.kind == "market" and self._flat_for_entry(bar=bar_open.isoformat()):
                self._enter(p, p.level, when, contract)
        if plans:
            log.info(f"[{self.id}] signal: " + "; ".join(
                f"{'BUY' if p.side > 0 else 'SELL'} {'stop ' if p.kind == 'stop' else ''}{p.level:g}" for p in plans))
        self.save()
        self.sync()

    # ================================================================== entries
    def _bar_flat(self, bar_open: pd.Timestamp) -> bool:
        """Does this chart bar's close reach the strategy's flat time (close everything)?"""
        if self.rules.flat_time is None or not hasattr(self.logic, "bar_flat"):
            return False
        return bool(self.logic.bar_flat(bar_open.tz_convert("America/New_York")))

    def _flat_for_entry(self, bar: Optional[str] = None) -> bool:
        """The backtest enters only when flat at the start of the bar, never in a bar that closed a trade."""
        if self.trade is not None or self.held != 0 or self.order or self.roll:
            return False
        if bar and self.closed_bar and pd.Timestamp(self.closed_bar) == pd.Timestamp(bar):
            return False
        return True

    def _fix(self, p: Plan, entry_bar: pd.Timestamp) -> bool:
        """Pending stop plan -> its entry bar (the first bar that trades after the signal)."""
        ok = False
        try:
            ok = self.logic.finalize(p, entry_bar.tz_convert("America/New_York"))
        except Exception as exc:
            self.error = f"signal error: {exc}"
            log.exception(f"[{self.id}] finalize failed")
        if not ok:
            self._drop(p)
            return False
        if self.trade is not None or self.held:           # in a trade at the start of the entry bar
            self._drop(p)
            return False
        return True

    def _drop(self, p: Plan):
        if p in self.plans:                  # an entry on the other side may have cleared the plans already
            self.plans.remove(p)

    def _room(self, session: int) -> bool:
        if self.day.get("session") != session:
            self.day = {"session": session, "count": 0}
        mx = self.logic.max_day_trades
        return mx == 0 or self.day["count"] < mx

    def _late(self, p: Plan, px: float, when: str) -> bool:
        """True = skip this entry: it is being acted on too late to be the trade the backtest took.
        * stale: the entry minute is older than entry_max_age_minutes (the desk was off / the data feed was
          down and is catching up on old candles - the order would go out now, at today's price)
        * chased: the live price is already more than entry_max_chase_r x the trade's risk past the trigger
          (the market order would fill far from the level the stop and targets are measured from)."""
        max_age = float(getattr(self.settings, "entry_max_age_minutes", 3) or 0)
        max_chase = float(getattr(self.settings, "entry_max_chase_r", 0.5) or 0)
        try:
            age = (self.now() - pd.Timestamp(when)).total_seconds() / 60.0
        except (TypeError, ValueError):
            age = 0.0
        if max_age > 0 and age > max_age:
            log.warning(f"[{self.id}] entry SKIPPED: {'BUY' if p.side > 0 else 'SELL'} trigger {px:g} was hit "
                        f"{age:.0f} min ago ({when}) - too late to enter (limit {max_age:g} min)")
            return True
        live, risk = self.signal_px, abs(float(px) - float(p.stop))
        if max_chase > 0 and live is not None and not math.isnan(live) and risk > 0:
            past = (float(px) - live) if p.side < 0 else (live - float(px))   # > 0 = price beyond the trigger
            if past > max_chase * risk:
                log.warning(f"[{self.id}] entry SKIPPED: {'BUY' if p.side > 0 else 'SELL'} trigger {px:g} but price "
                            f"is already {live:g} ({past:.2f} pts = {past / risk:.2f}R past it, "
                            f"limit {max_chase:g}R)")
                return True
        return False

    def _enter(self, p: Plan, px: float, when: str, contract: tuple):
        if not self.can_open:
            return
        if self.last_anchor.get(str(p.side)) == p.anchor:
            return                                        # one trade per pattern
        if not p.final or not self._room(p.session):
            return
        con_id, expiry = contract
        if not con_id:
            self.error = "no contract for this market yet"
            return
        if self._late(p, px, when):
            self.last_anchor[str(p.side)] = p.anchor      # this pattern is spent: never chase it later
            self._drop(p)
            self.save()
            return
        q = self.leg_qty(p.boost)
        if sum(q) <= 0:
            return
        if self.risk_usd > 0:
            q = self.risk_qty(q, abs(float(px) - float(p.stop)))
            if sum(q) <= 0:
                return
        tr = Trade(side=p.side, entry=float(px), stop0=float(p.stop), q=q, tp_mode=p.tp_mode, tp_r=p.tp_r,
                   tp_dist=p.tp_dist, tp_price=p.tp_price, tp2_r=p.tp2_r, trail_r=p.trail_r,
                   entry_minute=bucket(pd.Timestamp(when), 1).isoformat(), lock_px=float(p.lock_px or 0.0))
        if tr.risk <= 0:
            log.info(f"[{self.id}] entry at {px:g} would be past the stop {p.stop:g} - skipped")
            return
        if p.kind == "market":
            tr.bars_closed = 1                # entered AT the signal bar's close: that bar is the entry bar
        self.trade = tr
        self.plans = []
        self.day["count"] += 1
        self.last_anchor[str(p.side)] = p.anchor
        self.pos_con_id, self.pos_expiry, self.pos_account = int(con_id), expiry, self.account
        self.meta = {"trade_id": uuid.uuid4().hex[:10], "opened": when, "signal_time": p.signal_time,
                     "trigger": float(px), "boost": p.boost, "eth": p.eth, "basis": 0.0, "fills": [],
                     "entry_fill": None, "contract": mkts.label(self.market.symbol, expiry),
                     "qty": list(q), "exits": [], "account": self.account, "roll_adj": 0.0, "rolls": []}
        log.info(f"[{self.id}] ENTER {'LONG' if p.side > 0 else 'SHORT'} {sum(q)} {self.meta['contract']} "
                 f"trigger {px:g} stop {p.stop:g} tp1 {tr.tp1:g}")
        self.save()

    # ================================================================== exits
    def _exits(self, ex: List[Exit]):
        if not ex:
            return
        for e in ex:
            self.pending_exits.append({"leg": e.leg, "reason": e.reason, "level": e.level, "time": e.time,
                                       "qty": self.trade.q[e.leg - 1] if self.trade else 0})
            log.info(f"[{self.id}] exit leg {e.leg}: {REASON.get(e.reason, e.reason)} at {e.level:g}")
        self.save()

    def close_now(self, reason: str = "manual"):
        """Close this deployment's own position at market (button / switch off)."""
        self.plans = []
        if self.trade and self.trade.is_open:
            self._exits(self.trade.close_all(reason, self.signal_px or NAN, self.now().isoformat()))
        if self.order and self.order.get("opening"):
            self._cancel()
        self.save()
        self.sync()

    # ================================================================== orders
    def target(self) -> int:
        return self.trade.target_position() if self.trade else 0

    @property
    def busy(self) -> bool:
        return bool(self.order) or bool(self.roll) or self.held != self.target()

    def sync(self):
        if self.account_bad:
            # never send anything for an account this login does not manage: on a one-account login the
            # hub would route it to the only account there is (another account's money)
            return
        if self.roll:
            self._roll_poll()                            # exits wait until the position is in one month again
            return
        if self.order:
            self._poll()
            return
        if self.attention:
            return                                       # needs a human: no orders at all
        if time.monotonic() < self._retry_at:
            return
        delta = self.target() - self.held
        if delta == 0:
            self._finish_if_done()
            return
        if not self.pos_con_id:
            self.error = "no contract to trade"
            return
        reducing = abs(self.target()) < abs(self.held) or self.target() * self.held < 0
        word = "close" if reducing else "open"
        acct = self.trade_account
        key = self.hub.order_key(f"{self.id}_{word}_{uuid.uuid4().hex[:8]}", acct)
        same = (delta, int(self.pos_con_id))
        if self._unanswered and self._unanswered[0] == same:
            key = self._unanswered[1]                    # resend under the same key: the hub dedupes it
        self._unanswered = None
        action = "BUY" if delta > 0 else "SELL"
        legs = [e for e in self.pending_exits] if reducing else []
        contract = self.market.contract(self.pos_expiry, self.pos_con_id)
        if self.settings.dry_run:
            res = {"accepted": 1, "dry": True}
        else:
            res = self.hub.submit(key, contract, action, abs(delta), exit_=reducing, account=acct)
        if res is None:
            self._unanswered = (same, key)
            self._retry_at = time.monotonic() + 5
            return
        if "already submitted" in str((res.get("errors") or {}).get(key, "")):
            res = {"accepted": 1}
        if res.get("_status") or res.get("rejected") or not res.get("accepted"):
            self._rejects += 1
            self._retry_at = time.monotonic() + _RETRY_SECS
            self.error = f"order rejected: {(res.get('errors') or {}).get(key) or res.get('_text') or res}"
            log.error(f"[{self.id}] {action} {abs(delta)} rejected ({self.error}) - retry {self._rejects}")
            if not reducing and self.held == 0 and self._rejects >= 3:
                log.error(f"[{self.id}] entry rejected 3 times - trade dropped")
                self.trade = None
                self.meta = {}
                self._rejects = 0
                self.save()
            return
        self._rejects = 0
        self.error = ""
        self.order = {"key": key, "qty": delta, "sent": time.time(), "opening": not reducing,
                      "action": action, "exits": legs, "last_poll": 0.0, "cancelled": False,
                      "dry": bool(res.get("dry"))}
        self.pending_exits = [] if reducing else self.pending_exits
        log.info(f"[{self.id}] {action} {abs(delta)} {mkts.label(self.market.symbol, self.pos_expiry)} ({word}) "
                 f"in {acct}")
        self.save()
        if self.order["dry"]:
            self._poll()

    def _poll(self):
        o = self.order
        if o.get("dry"):                                   # practice mode: filled at once
            px = self.trade_px or self.signal_px
            if px is None:                                 # no live price (replay): the rule price
                px = (o["exits"][0]["level"] if o.get("exits") else (self.trade.entry if self.trade else None))
            self._filled(abs(o["qty"]), px, "FILLED")
            return
        if time.time() - o.get("last_poll", 0.0) < _POLL_SECS:
            return
        o["last_poll"] = time.time()
        st = self.hub.order_status(o["key"])
        if not st:
            return
        status = str(st.get("status", "")).upper()
        filled = int(st.get("filled_qty") or 0)
        if status in ("FILLED", "CANCELLED"):
            self._filled(filled, st.get("avg_fill_price"), status)
            return
        if status == "NOT_FOUND":
            # the hub forgot the order (hub restart): the reconciler works out what happened
            self.attention = ("An order was lost by the hub. Check the position in TWS, then press "
                              "'Positions are correct' on the Live page.")
            log.error(f"[{self.id}] hub lost order {o['key']} - needs attention")
            self.order = None
            self.save()
            return
        if o["opening"] and not o["cancelled"] and time.time() - o["sent"] > self.settings.entry_wait_seconds:
            log.warning(f"[{self.id}] entry not filled after {self.settings.entry_wait_seconds:.0f}s - cancelling")
            self._cancel()

    def _cancel(self):
        if self.order and not self.order.get("cancelled"):
            self.order["cancelled"] = True
            if not self.order.get("dry"):
                self.hub.cancel(self.order["key"])

    def _filled(self, filled: int, avg, status: str):
        o, self.order = self.order, None
        sign = 1 if o["qty"] > 0 else -1
        self.held += sign * filled
        px = float(avg) if avg else None
        if filled and px is not None:
            self.meta.setdefault("fills", []).append({"time": self.now().isoformat(), "action": o["action"], "qty": filled,
                                                      "price": px, "opening": o["opening"]})
        if o["opening"]:
            if filled and px is not None and self.trade:
                if self.meta.get("entry_fill") is None:
                    self.meta["entry_fill"] = px
                    self.meta["basis"] = self._basis(px)
                    self.trade.set_entry(px - self.meta["basis"])     # levels from the real fill
                    if self.trade.risk <= 0:
                        self._exits(self.trade.close_all("past_stop", px, self.now().isoformat()))
            if filled < abs(o["qty"]) and self.trade:
                # entry not (fully) filled: keep what filled, leg 1 first
                f = abs(self.held)
                q1 = min(self.trade.q[0], f)
                self.trade.q = [q1, f - q1]
                self.trade.open = [self.trade.open[0] and q1 > 0, self.trade.open[1] and f - q1 > 0]
                self.meta["qty"] = list(self.trade.q)
                log.warning(f"[{self.id}] entry {status}: {filled}/{abs(o['qty'])} filled - trading {f}")
                if f == 0:
                    self.trade = None
                    self.meta = {}
        else:
            if filled:
                for e in o.get("exits") or []:
                    e["fill"] = px
                    e["adj"] = float(self.meta.get("roll_adj") or 0.0)   # filled in a later month: roll cost
                    self.meta.setdefault("exits", []).append(e)
            if filled < abs(o["qty"]):
                self._retry_at = time.monotonic() + 5
                self.pending_exits = (o.get("exits") or []) + self.pending_exits
        if filled:
            log.info(f"[{self.id}] {o['action']} {filled} filled @ {px} -> holding {self.held:+d}")
        self._finish_if_done()
        self.save()

    def series_shift(self, jump: float):
        """The signal bars moved to another month: trade levels move with them (the traded contract did
        not change, so its stop / target in its own prices stay where they were); armed entries are
        re-armed at the next bar close on the adjusted bars."""
        if self.trade:
            self.trade.shift(jump)
            self.meta["basis"] = float(self.meta.get("basis") or 0.0) - jump
        self.plans = []
        self.save()

    # ================================================================== contract roll
    def can_roll(self) -> str:
        """'' when the open position can move to another contract month now, else why not (plain words)."""
        if not self.held or not self.pos_con_id:
            return "There is no open position to move."
        if self.roll:
            return "The position is already being moved."
        if self.order or self.pending_exits or self.held != self.target():
            return "An order is working. Try again in a moment."
        if self.attention:
            return "This strategy needs attention first."
        if not self.trade or not self.trade.is_open:
            return "The trade is closing."
        return ""

    def start_roll(self, con_id: int, expiry: str, why: str = "manual") -> str:
        """Move the whole open position to (con_id, expiry): close the old month and open the new one in
        ONE hub batch (both orders are placed at the same moment). Returns '' or the reason it cannot."""
        why_not = self.can_roll()
        if why_not:
            return why_not
        if int(con_id) == int(self.pos_con_id):
            return "The position is already in that month."
        q, long_ = abs(self.held), self.held > 0
        legs = [{"role": "close", "con_id": int(self.pos_con_id), "expiry": self.pos_expiry,
                 "action": "SELL" if long_ else "BUY"},
                {"role": "open", "con_id": int(con_id), "expiry": expiry, "action": "BUY" if long_ else "SELL"}]
        for leg in legs:
            leg.update(qty=q, filled=0, cost=0.0, key=None, tries=0)
        self.roll = {"id": uuid.uuid4().hex[:8], "from": self.pos_expiry, "to": expiry, "qty": q, "why": why,
                     "started": time.time(), "legs": legs, "account": self.trade_account, "last_poll": 0.0,
                     "retry_at": 0.0, "dry": bool(self.settings.dry_run), "failed": ""}
        log.info(f"[{self.id}] ROLL {q} {mkts.label(self.market.symbol, self.pos_expiry)} -> "
                 f"{mkts.label(self.market.symbol, expiry)} ({why}) in {self.trade_account}")
        self._roll_send()
        self.save()
        return ""

    def _roll_send(self):
        r = self.roll
        todo = [lg for lg in r["legs"] if lg["filled"] < lg["qty"] and not lg["key"]]
        if not todo:
            return
        if r["dry"]:
            for lg in todo:
                px = self.price_of(lg["con_id"]) or self.trade_px or self.signal_px
                lg["filled"], lg["cost"] = lg["qty"], float(px or 0.0) * lg["qty"]
            self._roll_check()
            return
        orders = []
        for lg in todo:
            lg["key"] = self.hub.order_key(f"{self.id}_roll{lg['role']}_{r['id']}_{lg['tries']}", r["account"])
            contract = self.market.contract(lg["expiry"], lg["con_id"])
            orders.append(self.hub.order(lg["key"], contract, lg["action"], lg["qty"] - lg["filled"],
                                         lg["role"] == "close", r["account"]))
        res = self.hub.submit_batch(f"{orders[0]['order_key']}_batch", orders)
        if res is None:                                   # no answer: poll the keys - the hub may have them
            r["retry_at"] = time.monotonic() + 5
            return
        errs = res.get("errors") or {}
        for lg in todo:
            e = str(errs.get(lg["key"], ""))
            if res.get("_status") or (e and "already submitted" not in e):
                log.error(f"[{self.id}] roll {lg['role']} order rejected: {e or res.get('_text')}")
                self.error = f"moving to the next month: {e or res.get('_text')}"
                lg["key"], lg["tries"] = None, lg["tries"] + 1
                r["retry_at"] = time.monotonic() + _RETRY_SECS
        self._roll_check()

    def _roll_poll(self):
        r = self.roll
        if r["failed"]:
            return                                        # waits for the operator (attention)
        if not r["dry"] and time.time() - r.get("last_poll", 0.0) >= _POLL_SECS:
            r["last_poll"] = time.time()
            for lg in r["legs"]:
                if not lg["key"]:
                    continue
                st = self.hub.order_status(lg["key"])
                if not st:
                    continue
                status = str(st.get("status", "")).upper()
                if status == "NOT_FOUND" and time.time() - r["started"] > 30:
                    lg["key"], lg["tries"] = None, lg["tries"] + 1
                    continue
                if status in ("FILLED", "CANCELLED"):
                    f = int(st.get("filled_qty") or 0)
                    lg["filled"] += f
                    lg["cost"] += f * float(st.get("avg_fill_price") or 0.0)
                    if lg["filled"] < lg["qty"]:
                        lg["tries"] += 1
                        log.warning(f"[{self.id}] roll {lg['role']} {status}: {lg['filled']}/{lg['qty']} filled")
                    lg["key"] = None
                    self.save()
        self._roll_check()
        if self.roll and not self.roll["failed"] and time.monotonic() >= r.get("retry_at", 0.0):
            self._roll_send()

    def _roll_check(self):
        r = self.roll
        legs = r["legs"]
        if all(lg["filled"] >= lg["qty"] for lg in legs):
            return self._roll_done()
        if any(lg["tries"] >= 3 and not lg["key"] and lg["filled"] < lg["qty"] for lg in legs):
            close, open_ = legs
            if close["filled"] == 0 and open_["filled"] == 0:     # nothing happened: stay where we are
                self.error = "could not move to the next month (orders rejected) - still in the old month"
                log.error(f"[{self.id}] roll abandoned, nothing filled - position unchanged")
                self.roll = None
                self.save()
                return
            old_name = mkts.label(self.market.symbol, r["from"])
            new_name = mkts.label(self.market.symbol, r["to"])
            sd = 1 if self.held > 0 else -1
            r["failed"] = (f"Moving to {new_name} did not complete. IBKR should now hold "
                           f"{sd * (r['qty'] - close['filled']):+d} {old_name} and {sd * open_['filled']:+d} "
                           f"{new_name} for this strategy. Move the rest to {new_name} in TWS, then press "
                           f"'Positions are correct'.")
            self.attention = r["failed"]
            log.error(f"[{self.id}] {r['failed']}")
            self.save()

    def _roll_done(self, assumed: bool = False):
        r, self.roll = self.roll, None
        close, open_ = r["legs"]
        c_px = close["cost"] / close["filled"] if close["filled"] else None
        o_px = open_["cost"] / open_["filled"] if open_["filled"] else None
        spread = (o_px - c_px) if (c_px and o_px) else 0.0
        self.meta["roll_adj"] = float(self.meta.get("roll_adj") or 0.0) + spread
        self.meta["basis"] = float(self.meta.get("basis") or 0.0) + spread
        old_name = mkts.label(self.market.symbol, r["from"])
        new_name = mkts.label(self.market.symbol, r["to"])
        self.meta.setdefault("rolls", []).append({"time": now_iso(), "from": old_name, "to": new_name,
                                                  "qty": r["qty"], "close_px": c_px, "open_px": o_px,
                                                  "spread": spread, "why": r["why"], "by_hand": assumed})
        for lg, px in ((close, c_px), (open_, o_px)):
            if px:
                self.meta.setdefault("fills", []).append({"time": now_iso(), "action": lg["action"],
                                                          "qty": lg["filled"], "price": px, "opening": False,
                                                          "roll": lg["role"]})
        self.pos_con_id, self.pos_expiry = int(open_["con_id"]), open_["expiry"]
        self.meta["contract"] = new_name
        self.error = ""
        log.info(f"[{self.id}] ROLLED {r['qty']} {old_name} @ {c_px} -> {new_name} @ {o_px} "
                 f"(spread {spread:+g}); the trade continues in {new_name}")
        self.save()

    def finish_failed_roll(self):
        """The operator moved the rest by hand: the whole position is now in the new month."""
        if self.roll and self.roll.get("failed"):
            for lg in self.roll["legs"]:
                lg["filled"] = lg["qty"] if not lg["filled"] else lg["filled"]
            self._roll_done(assumed=True)

    def _basis(self, fill: float) -> float:
        """Traded contract minus the signal series (0 unless the bars are on another month, near a roll)."""
        if self.signal_px is None or self.trade_px is None:
            return 0.0
        b = self.market.round_tick(self.trade_px - self.signal_px)
        return 0.0 if abs(b) <= 2 * self.market.tick + 1e-9 else b

    def _finish_if_done(self):
        if self.trade and not self.trade.is_open and self.held == 0 and not self.order:
            if self.meta.get("entry_fill") is not None:          # a trade that never filled is not a trade
                self._journal_close()
            else:
                log.info(f"[{self.id}] trade ended before its entry filled - nothing traded")
            # the bar of the EXIT itself (a stop in a bar's last minute is processed after the bar ended)
            ex = next((e for e in reversed(self.trade.exits) if e.get("time")), None)
            try:
                t_exit = pd.Timestamp(ex["time"]) if ex else self.now()
            except ValueError:
                t_exit = self.now()
            t_exit = t_exit.tz_convert("UTC") if t_exit.tzinfo else t_exit.tz_localize("UTC")
            if ex and ex.get("reason") in ("time", "flat"):
                # a bar-close exit carries the bar's CLOSE time = the next bar's open: it belongs to the bar
                # that just closed, so the next bar may enter again (backtest: no entry only in the exit's bar)
                t_exit -= pd.Timedelta(minutes=1)
            self.closed_bar = bucket(t_exit, self.logic.timeframe).isoformat()
            self.trade = None
            self.meta = {}
            self.pending_exits = []
            self.pos_con_id = self.pos_expiry = self.pos_account = None
            if self.cfg.get("stop_after_trade"):
                self.cfg["enabled"] = False
                self.cfg["stop_after_trade"] = False
                self.cfg["_changed"] = True
            self.save()

    def _journal_close(self):
        m, tr = self.meta, self.trade
        pv = self.market.point_value
        entry = m.get("entry_fill")
        pnl = 0.0
        legs = []
        for e in m.get("exits") or []:
            fill = e.get("fill")
            if entry is not None and fill is not None:
                # after a roll the exit fills in the new month: entry + (new - old month at the roll)
                pnl += (fill - entry - float(e.get("adj") or 0.0)) * tr.side * e["qty"] * pv
            legs.append({"leg": e["leg"], "qty": e["qty"], "reason": e["reason"], "level": e["level"],
                         "fill": fill, "time": e.get("time")})
        main = max(legs, key=lambda x: x["leg"])["reason"] if legs else "manual"
        append_jsonl(JOURNAL, {
            "trade_id": m.get("trade_id"), "deployment": self.id, "strategy": self.card["id"],
            "name": self.name, "market": self.market.symbol, "contract": m.get("contract"),
            "account": m.get("account") or self.trade_account, "rolls": m.get("rolls") or [],
            "side": "Long" if tr.side > 0 else "Short", "qty": sum(m.get("qty") or tr.q),
            "opened": m.get("opened"), "closed": self.now().isoformat(), "entry": entry, "stop": tr.stop0,
            "target": tr.tp1, "legs": legs, "result": REASON.get(main, main),
            "pnl": round(pnl, 2), "r": round(pnl / (tr.risk * pv * max(1, sum(tr.q))), 2) if tr.risk > 0 else None,
            "dry_run": self.settings.dry_run})
        log.info(f"[{self.id}] trade closed: {REASON.get(main, main)}, P&L ${pnl:,.2f}")
