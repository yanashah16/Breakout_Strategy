"""
StockBot: one stock in one copy (e.g. NVDA in the 10m copy). Runs the rules (core.StockBoxCore for the box copies,
flag_core.StockFlagCore for the pole & flag copy - same feed / target interface) and keeps the
broker position of ITS OWN fills equal to the rules' target, with the hub's orders.

Orders follow the Deployment model: one order at a time for the difference between the target and what this bot
holds (own fills only); a reverse is sent as close-then-open. Opening orders that do not fill in time are cancelled
and the rules forget the trade (or keep the part that filled). State is saved after every change.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import time
import uuid
from typing import Callable, List, Optional

import pandas as pd

from desk.settings import STATE
from desk.stocks import core as C
from desk.stocks import flag_core as F
from desk.stocks.copies import COPIES
from desk.stocks.feed import NY, bucket, is_last_minute, ny
from desk.store import append_jsonl, read_json, write_json

log = logging.getLogger("desk.stocks")
DIR = STATE / "stocks"
JOURNAL = DIR / "journal.jsonl"
_POLL_SECS = 2.0
_RETRY_SECS = 20.0


def _num(v):
    """nan -> None (the page's JSON cannot carry nan)."""
    return None if isinstance(v, float) and math.isnan(v) else v


def _clean(d: dict) -> dict:
    return {k: _num(v) for k, v in (d or {}).items()}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(0))).isoformat()


class StockBot:
    def __init__(self, copy_id: str, sym: str, capital_usd: Optional[float], hub, settings, room: Callable[[float], bool],
                 risk_usd: Optional[float] = None):
        """capital_usd = the fixed $ amount every trade buys (live); risk_usd = backtest risk sizing (tests)."""
        self.copy, self.sym = copy_id, sym
        self.id = f"{copy_id}_{sym}"
        cp = COPIES[copy_id]
        params = dict(cp["params"], capital_usd=capital_usd)
        if risk_usd is not None:
            params["risk_trade_usd"] = float(risk_usd)
        self.kind = cp.get("kind", "box")
        if self.kind == "flag":
            self.core = F.StockFlagCore(F.Params(params))
            self.reasons = F.REASON
        else:
            self.core = C.StockBoxCore(C.Params(params))
            self.reasons = C.REASON
        self.tf = self.core.p.tf
        self.hub, self.settings, self.room = hub, settings, room
        self.path = DIR / f"{self.id}.json"
        self.held = 0                    # shares this bot's own orders filled (signed)
        self.order: Optional[dict] = None
        self.meta: dict = {}             # the open trade: fills, rule exits
        self.attention = ""
        self.error = ""
        self.last_bar = ""               # open time (ISO, New York) of the last chart bar processed
        self.ready = False
        self.px: Optional[float] = None
        self._prev_px: Optional[float] = None
        self._bar: Optional[dict] = None  # the forming chart bar {t (UTC), o, h, l, c, v}
        self._retry_at = 0.0
        self._rejects = 0
        self._day, self._dh, self._dl, self._dc = None, -math.inf, math.inf, math.nan
        self._unanswered = None
        self._late_logged = None
        self.restore()

    # ------------------------------------------------------------------ persistence
    def save(self):
        write_json(self.path, {"pos": self.core.state(), "held": self.held, "order": self.order, "meta": self.meta,
                               "attention": self.attention, "last_bar": self.last_bar, "saved": now_iso()})

    def restore(self):
        d = read_json(self.path) or {}
        self.core.restore(d.get("pos"))
        self.held = int(d.get("held") or 0)
        self.order = d.get("order")
        self.meta = d.get("meta") or {}
        self.attention = d.get("attention") or ""
        self.last_bar = d.get("last_bar") or ""

    @property
    def busy(self) -> bool:
        return bool(self.order) or self.held != 0 or self.core.pos.side != 0

    def set_capital(self, usd: Optional[float]):
        self.core.p.capital = float("nan") if usd is None else float(usd)

    # ------------------------------------------------------------------ warm-up
    def warmup(self, m1: pd.DataFrame, daily: pd.DataFrame, exact_from: Optional[pd.Timestamp]):
        """Indicators from history; bars after the last one this bot processed (desk was off) are replayed with
        their exits but without new entries."""
        for r in daily.itertuples():
            self.core.add_day(r.date, float(r.high), float(r.low), float(r.close))
        if m1.empty:
            return
        b = m1.t.map(lambda t: bucket(t, self.tf))
        last_done = dt.datetime.fromisoformat(self.last_bar) if self.last_bar else None
        catch_up = self.core.pos.side != 0 and last_done is not None
        groups = list(m1.groupby(b, sort=True))
        day, dh, dl, dc = None, -math.inf, math.inf, math.nan
        for k, (bt, g) in enumerate(groups):
            if k == len(groups) - 1 and not self._complete(bt, g, exact_from):
                if catch_up and exact_from is not None and bt >= exact_from:
                    for r in g.itertuples():
                        self._events(self.core.on_minute(C.Bar(ny(r.t), r.open, r.high, r.low, r.close, r.volume),
                                                         ny(r.t).isoformat()), catch_up=True)
                self._bar = {"t": bt, "o": float(g.open.iloc[0]), "h": float(g.high.max()), "l": float(g.low.min()),
                             "c": float(g.close.iloc[-1]), "v": float(g.volume.sum())}
                break                                    # the forming bar continues live
            t = ny(bt)
            if day is not None and t.date() != day:
                self.core.add_day(day, dh, dl, dc)
                dh, dl = -math.inf, math.inf
            day = t.date()
            bar = C.Bar(t, float(g.open.iloc[0]), float(g.high.max()), float(g.low.min()), float(g.close.iloc[-1]),
                        float(g.volume.sum()))
            dh, dl, dc = max(dh, bar.h), min(dl, bar.l), bar.c
            if catch_up and t > last_done and exact_from is not None and bt >= exact_from:
                for r in g.itertuples():
                    self._events(self.core.on_minute(C.Bar(ny(r.t), r.open, r.high, r.low, r.close, r.volume),
                                                     ny(r.t).isoformat()), catch_up=True)
                ev = self.core.on_bar_close(bar, (ny(g.t.iloc[-1])).isoformat(), allow_entry=False)
                self._events(ev, catch_up=True)
            else:
                self.core.warmup(bar)
            self.last_bar = t.isoformat()
        self._day = day
        self._dh, self._dl, self._dc = dh, dl, dc
        self.ready = True
        self.save()

    def _complete(self, bt: pd.Timestamp, g: pd.DataFrame, exact_from) -> bool:
        """The chart bar's last minute is in the history (older rows are 5-minute bars)."""
        last = g.t.iloc[-1]
        step = 1 if exact_from is not None and last >= exact_from else 5
        return last + pd.Timedelta(minutes=step) >= bt + pd.Timedelta(minutes=self.tf)

    # ------------------------------------------------------------------ live feed
    def on_minute(self, row, enabled: bool):
        """A completed 1-minute bar (UTC row) of this stock."""
        t = ny(row.t)
        stamp = t.isoformat()
        b = bucket(row.t, self.tf)
        if self._bar is not None and b > self._bar["t"]:
            self._close_bar(enabled)                    # its last minute never printed (halt / gap)
        if self._bar is None:
            self._bar = {"t": b, "o": float(row.open), "h": float(row.high), "l": float(row.low), "c": float(row.close),
                         "v": float(row.volume)}
        else:
            br = self._bar
            br["h"], br["l"], br["c"] = max(br["h"], float(row.high)), min(br["l"], float(row.low)), float(row.close)
            br["v"] += float(row.volume)
        ev = self.core.on_minute(C.Bar(t, float(row.open), float(row.high), float(row.low), float(row.close),
                                       float(row.volume)), stamp)
        self._events(ev)
        if is_last_minute(row.t, self.tf):
            self._close_bar(enabled, upto=stamp)

    def _close_bar(self, enabled: bool, upto: str = ""):
        br, self._bar = self._bar, None
        if br is None:
            return
        t = ny(br["t"])
        if self.last_bar and t <= dt.datetime.fromisoformat(self.last_bar):
            return                                      # already processed in the warm-up
        if self._day is not None and t.date() != self._day:
            self.core.add_day(self._day, self._dh, self._dl, self._dc)
            self._dh, self._dl = -math.inf, math.inf
        self._day = t.date()
        self._dh, self._dl, self._dc = max(self._dh, br["h"]), min(self._dl, br["l"]), br["c"]
        bar = C.Bar(t, br["o"], br["h"], br["l"], br["c"], br["v"])
        ok = enabled and not self.attention and not self.error.startswith("account")
        late = self._late_minutes(br["t"])
        if ok and late is not None:
            ok = False                                  # a stale candle (delayed data / feed gap): exits only
            if self._late_logged != br["t"]:
                self._late_logged = br["t"]
                log.warning(f"[{self.id}] candle {t:%H:%M} closed {late:.0f} min ago - no new entry from it "
                            f"(limit {self.settings.entry_max_age_minutes:g} min)")
        ev = self.core.on_bar_close(bar, upto or t.isoformat(), allow_entry=ok)
        self.last_bar = t.isoformat()
        self._events(ev)
        self.save()

    def _late_minutes(self, bar_open_utc: pd.Timestamp) -> Optional[float]:
        """Minutes since this chart bar closed, when that is more than entry_max_age_minutes (else None).
        IBKR serves stock data 15 minutes late to an account without live US-stock data (message 10089) - the
        rules would then enter breakouts that happened a quarter of an hour ago at today's price."""
        limit = float(getattr(self.settings, "entry_max_age_minutes", 0) or 0)
        if limit <= 0:
            return None
        age = (pd.Timestamp.now(tz="UTC") - (bar_open_utc + pd.Timedelta(minutes=self.tf))) / pd.Timedelta(minutes=1)
        return age if age > limit else None

    def on_price(self, px: Optional[float], now_ny: dt.datetime):
        self.px = px
        armed = bool(getattr(self.core, "pend", None))        # flag: a stop entry is waiting for this candle
        if px is None or (self.core.pos.side == 0 and not armed) or not self.ready:
            self._prev_px = px
            return
        minute = now_ny.replace(second=0, microsecond=0)
        ev = self.core.on_tick(px, minute, self._prev_px, now_ny.isoformat())
        self._prev_px = px
        self._events(ev)

    # ------------------------------------------------------------------ rule events -> trade record
    def _events(self, ev: List[C.Event], catch_up: bool = False):
        if not ev:
            return
        for e in ev:
            if e.kind == "entry":
                notional = e.qty * e.px
                if not self.room(notional):
                    log.warning(f"[{self.id}] breakout {'LONG' if e.side > 0 else 'SHORT'} {e.qty} skipped: "
                                f"open exposure limit reached")
                    self.core.restore(None)
                    continue
                ps = self.core.pos
                setup = "Flag" if self.kind == "flag" else "Breakout"
                self.meta = {"side": e.side, "rule_entry": e.px, "opened": now_iso(), "fills": [], "exits": [],
                             "setup": setup, "boost": ps.boost}
                log.info(f"[{self.id}] {setup.upper()} {'LONG' if e.side > 0 else 'SHORT'} {e.qty} @ {e.px:.2f} | stop "
                         f"{ps.stop:.2f} TP1 {ps.tps[0]:.2f} | 1R {ps.unit:.2f} legs {ps.legs}" + (f" | {ps.boost}" if ps.boost else ""))
            elif e.kind == "flip":
                self.meta.setdefault("exits", []).append({"reason": e.reason, "level": e.px, "time": now_iso(),
                                                          "qty": e.qty - self.core.pos.open_qty()})
                self.meta["flip_to"] = {"side": e.side, "rule_entry": e.px, "time": now_iso()}
                log.info(f"[{self.id}] REVERSE -> {'LONG' if e.side > 0 else 'SHORT'} @ {e.px:.2f} ({self.reasons.get(e.reason, e.reason)})")
            else:
                self.meta.setdefault("exits", []).append({"reason": e.reason, "level": e.px, "time": now_iso(), "qty": e.qty})
                log.info(f"[{self.id}] EXIT {e.qty} @ {e.px:.2f} ({self.reasons.get(e.reason, e.reason)})"
                         + (" [restart catch-up]" if catch_up else ""))
        self.save()

    # ------------------------------------------------------------------ orders
    def target(self) -> int:
        return self.core.target()

    def sync(self, account: str, contract: dict):
        if self.order:
            self._poll()
            return
        if self.attention or time.monotonic() < self._retry_at:
            return
        tgt = self.target()
        if tgt == self.held:
            self._finish_if_done()
            return
        step = -self.held if tgt * self.held < 0 else tgt - self.held      # reverse: close first, then open
        reducing = abs(self.held + step) < abs(self.held)
        word = "close" if reducing else "open"
        key = self.hub.order_key(f"stk_{self.id}_{word}_{uuid.uuid4().hex[:8]}", account)
        same = (step, word)
        if self._unanswered and self._unanswered[0] == same:
            key = self._unanswered[1]
        self._unanswered = None
        action = "BUY" if step > 0 else "SELL"
        if self.settings.dry_run:
            res = {"accepted": 1, "dry": True}
        else:
            res = self.hub.submit(key, contract, action, abs(step), exit_=reducing, account=account)
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
            log.error(f"[{self.id}] {action} {abs(step)} rejected ({self.error}) - retry {self._rejects}")
            if not reducing and self.held == 0 and self._rejects >= 3:
                log.error(f"[{self.id}] entry rejected 3 times - trade dropped")
                self.core.restore(None)
                self.meta = {}
                self._rejects = 0
                self.save()
            return
        self._rejects = 0
        self.error = ""
        self.order = {"key": key, "qty": step, "sent": time.time(), "opening": not reducing, "action": action,
                      "last_poll": 0.0, "cancelled": False, "dry": bool(res.get("dry"))}
        log.info(f"[{self.id}] {action} {abs(step)} {self.sym} ({word}) in {account}"
                 + (" [practice]" if self.order["dry"] else ""))
        self.save()
        if self.order["dry"]:
            self._poll()

    def _poll(self):
        o = self.order
        if o.get("dry"):
            px = self.px or (self.core.pos.entry if self.core.pos.side else None)
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
            self.attention = "An order was lost by the hub. Check the position in TWS, then press 'Positions are correct'."
            log.error(f"[{self.id}] hub lost order {o['key']} - needs attention")
            self.order = None
            self.save()
            return
        if o["opening"] and not o["cancelled"] and time.time() - o["sent"] > self.settings.entry_wait_seconds:
            log.warning(f"[{self.id}] entry not filled after {self.settings.entry_wait_seconds:.0f}s - cancelling")
            o["cancelled"] = True
            self.hub.cancel(o["key"])

    def _filled(self, filled: int, avg, status: str):
        o, self.order = self.order, None
        sign = 1 if o["qty"] > 0 else -1
        self.held += sign * filled
        px = float(avg) if avg else None
        if filled and px is not None:
            self.meta.setdefault("fills", []).append({"time": now_iso(), "action": o["action"], "qty": filled,
                                                      "price": px, "opening": o["opening"]})
        if o["opening"] and filled < abs(o["qty"]):
            log.warning(f"[{self.id}] entry {status}: {filled}/{abs(o['qty'])} filled - trading {abs(self.held)}")
            self.core.scale_entry(abs(self.held))
        if not o["opening"] and filled < abs(o["qty"]):
            self._retry_at = time.monotonic() + 5
        if filled:
            log.info(f"[{self.id}] {o['action']} {filled} filled @ {px} -> holding {self.held:+d}")
        self._finish_if_done()
        self.save()

    def _finish_if_done(self):
        """The trade is over when this bot holds nothing and the rules hold nothing on that side any more."""
        if self.held != 0 or self.order or not self.meta.get("fills"):
            if self.held == 0 and not self.order and self.meta and not self.meta.get("fills") and self.core.pos.side == 0:
                self.meta = {}                       # an entry that never filled
            return
        flip = self.meta.get("flip_to")
        if self.core.pos.side != 0 and not flip:
            return                                   # rules still want a position (an entry is being sent)
        self._journal()
        if flip and self.core.pos.side == flip["side"]:
            self.meta = {"side": flip["side"], "rule_entry": flip["rule_entry"], "opened": flip["time"], "fills": [],
                         "exits": [], "setup": "Reverse"}
        else:
            self.meta = {}

    def _journal(self):
        m = self.meta
        fills = m.get("fills") or []
        ins = [f for f in fills if f["opening"]]
        outs = [f for f in fills if not f["opening"]]
        q_in = sum(f["qty"] for f in ins)
        if not q_in:
            return
        avg_in = sum(f["qty"] * f["price"] for f in ins) / q_in
        q_out = sum(f["qty"] for f in outs)
        avg_out = sum(f["qty"] * f["price"] for f in outs) / q_out if q_out else avg_in
        side = int(m.get("side") or 0)
        pnl = side * (avg_out - avg_in) * q_out
        reasons = [self.reasons.get(x["reason"], x["reason"]) for x in m.get("exits") or []]
        row = {"copy": self.copy, "strategy": COPIES[self.copy]["name"], "stock": self.sym, "side": "Long" if side > 0 else "Short",
               "setup": m.get("setup", "Breakout"), "opened": m.get("opened"), "closed": now_iso(), "shares": q_in,
               "entry": round(avg_in, 4), "exit": round(avg_out, 4), "pnl": round(pnl, 2),
               "exits": " · ".join(dict.fromkeys(reasons)) or "Closed", "practice": bool(self.settings.dry_run)}
        append_jsonl(JOURNAL, row)
        log.info(f"[{self.id}] trade closed: {row['side']} {q_in} @ {row['entry']} -> {row['exit']}, P&L ${pnl:,.2f}")

    # ------------------------------------------------------------------ manual
    def close_now(self, why: str = "manual"):
        if self.core.pos.side != 0:
            self._events(self.core.close_all(why, self.px or self.core.pos.entry, now_iso()))
        self.meta.pop("flip_to", None)
        if self.order and self.order.get("opening") and not self.order.get("cancelled"):
            self.order["cancelled"] = True
            if not self.order.get("dry"):
                self.hub.cancel(self.order["key"])
        self.save()

    def forget(self):
        """'I closed it by hand': drop the position from the books without an order."""
        self.core.restore(None)
        self.held, self.order, self.meta, self.attention = 0, None, {}, ""
        self.save()

    # ------------------------------------------------------------------ display
    def view(self) -> dict:
        ps, p = self.core.pos, self.core.p
        fills = [f for f in self.meta.get("fills") or [] if f["opening"]]
        q = sum(f["qty"] for f in fills)
        avg = sum(f["qty"] * f["price"] for f in fills) / q if q else None
        upnl = (self.px - avg) * self.held if (avg and self.px and self.held) else None
        d = ps.side
        stage = {0: "waiting for target 1", 1: "target 1 done - leg 2 leaves on an EMA close",
                 2: "trailing the runner"}.get(ps.stage, "")
        if d and ps.stage == 0 and ps.be_on:
            stage = "stop locked above the entry (break-even +0.1 R)"
        nxt = ""
        if self.kind == "flag":
            stage, nxt = self.core.story()
        elif d:
            t1, t3, trail, _ = p.exits(d)
            if ps.stage == 0:
                nxt = (f"Target 1 at {ps.tps[0]:.2f} closes {ps.legs[0]} shares; stop {ps.stop:.2f} closes all"
                       + (" (a stop in the first 10 candles turns the trade around)" if not ps.is_rev and ps.bars_since < p.rev_bars and not ps.be_on else ""))
            elif ps.stage == 1:
                nxt = f"Leg 2 ({ps.legs[1]}) leaves when a candle closes through the trend EMA; stop at {ps.stop:.2f}"
            else:
                nxt = f"Runner ({ps.legs[2]}) trails at {ps.stop:.2f} (gives back {trail:.0f} % of its best profit); final target {ps.tps[1]:.2f}"
        return {"id": self.id, "copy": self.copy, "strategy": COPIES[self.copy]["name"], "stock": self.sym,
                "side": "Long" if d > 0 else "Short" if d < 0 else "", "held": self.held, "legs": list(ps.legs),
                "rule_entry": ps.entry if d else None, "fill": avg, "stop": ps.stop if d else None,
                "tp1": ps.tps[0] if d else None, "tp3": _num(ps.tps[1]) if d else None, "r_size": ps.unit if d else None,
                "price": self.px, "upnl": upnl, "r_now": (d * (self.px - ps.entry) / ps.unit) if d and self.px and ps.unit else None,
                "stage": stage, "next": nxt,
                "setup": "Reverse" if ps.is_rev else (("Flag" if self.kind == "flag" else "Breakout") if d else ""),
                "opened": self.meta.get("opened"), "boost": ps.boost, "working": bool(self.order),
                "attention": self.attention, "error": self.error, "last": _clean(self.core.last),
                "notional": abs(self.held) * (self.px or 0)}
