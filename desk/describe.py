"""
Plain-English views of the engine for the UI. No parameters, no jargon: what is it doing,
what happens next.
"""
from __future__ import annotations

import datetime as dt
import math
import time
from typing import Optional

import pandas as pd

from desk import markets as mkts
from desk.deployment import JOURNAL, Deployment
from desk.store import read_jsonl

NY = "America/New_York"


def decimals(tick: float) -> int:
    s = f"{tick:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def fmt(px: Optional[float], tick: float) -> Optional[str]:
    if px is None or (isinstance(px, float) and math.isnan(px)):
        return None
    return f"{px:,.{decimals(tick)}f}"


def ny_time(iso: Optional[str], with_day: bool = False, seconds: bool = False) -> str:
    if not iso:
        return ""
    t = pd.Timestamp(iso)
    t = (t.tz_localize("UTC") if t.tzinfo is None else t).tz_convert(NY)
    today = pd.Timestamp.now(tz=NY).date()
    clock = "%H:%M:%S" if seconds else "%H:%M"
    if with_day or t.date() != today:
        return t.strftime("%a %d %b, " + clock)
    return t.strftime(clock)


def side_word(side: int, verb: bool = False) -> str:
    if verb:
        return "buy" if side > 0 else "sell"
    return "Long" if side > 0 else "Short"


def status(d: Deployment, data_ready: bool) -> dict:
    """{tone, text} for the strategy list. tone: off | idle | armed | trade | warn"""
    tick = d.market.tick
    if d.attention:
        return {"tone": "warn", "text": "Needs attention"}
    if d.account_bad and (d.held or d.trade):
        return {"tone": "warn", "text": "Account not logged in - trade not managed"}
    if not d.enabled and not (d.trade or d.held or d.order):
        return {"tone": "off", "text": "Off"}
    if d.trade or d.held or d.order or d.roll:
        tr = d.trade
        if d.roll:
            return {"tone": "trade", "text": f"Moving the trade to {mkts.label(d.market.symbol, d.roll['to'])}…"}
        if d.order and d.order.get("opening"):
            return {"tone": "trade", "text": "Entering a trade…"}
        if tr and not tr.is_open:
            return {"tone": "trade", "text": "Closing the trade…"}
        t = f"In a {side_word(tr.side).lower()} trade" if tr else "Closing…"
        if d.cfg.get("stop_after_trade"):
            t += " · switches off after it"
        return {"tone": "trade", "text": t}
    if d.account_bad:
        return {"tone": "warn", "text": "Account not found in IBKR"}
    if not data_ready:
        return {"tone": "idle", "text": "Loading market data…"}
    if d.error:
        return {"tone": "warn", "text": "Problem: " + d.error[:80]}
    final = [p for p in d.plans if p.final]
    if final:
        p = final[0]
        return {"tone": "armed", "text": f"Ready to {side_word(p.side, True)} at {fmt(p.level, tick)}"}
    if d.plans:
        return {"tone": "armed", "text": "Setup found · ready at the next candle"}
    return {"tone": "idle", "text": "Watching for a setup"}


def strategy_row(d: Deployment, data_ready: bool, engine=None) -> dict:
    c = d.card
    q = c.get("default_qty") or [0, 1]
    cid, exp = engine._contract_for(d) if engine is not None else (None, None)
    choice = d.expiry_choice
    return {
        "account": d.account, "trade_account": d.trade_account if (d.held or d.trade) else None,
        "expiry": choice, "contract": mkts.label(d.market.symbol, exp) if exp else None,
        "pos_contract": mkts.label(d.market.symbol, d.pos_expiry) if d.pos_expiry else None,
        "id": d.id, "strategy": c["id"], "name": d.name, "family": c["family"], "market": d.market.symbol,
        "market_name": d.market.name, "micro": d.market.is_micro, "qty": d.qty,
        "legs": [k for k, v in enumerate(q) if v] or [1], "enabled": d.enabled,
        "stop_after_trade": bool(d.cfg.get("stop_after_trade")), "experimental": bool(c.get("experimental")),
        "pattern": c.get("pattern"), "summary": c.get("summary"), "how_it_enters": c.get("how_it_enters"),
        "how_it_exits": c.get("how_it_exits"), "status": status(d, data_ready),
        "has_trade": bool(d.trade or d.held or d.roll), "attention": d.attention or d.account_bad,
        "boost": (c.get("logic") or {}).get("boost_qty1") is not None,
    }


def trade_story(d: Deployment) -> dict:
    """State and 'what happens next' of an open trade, in words (prices of the contract it is in)."""
    tr, tick = d.trade, d.market.tick
    b = float((d.meta or {}).get("basis") or 0.0)
    f = lambda x: fmt(None if x is None else x + b, tick)   # noqa: E731
    out_qty = tr.open_qty()
    runner_only = tr.q[0] == 0
    if not tr.is_open:
        return {"state": "Closing the trade at market.", "next": "Waiting for the closing order to fill."}
    lines = []
    if not tr.tp1_done:
        if runner_only:
            state = f"Riding the move. Stop loss at {f(tr.stop[1])}."
            nxt = f"At {f(tr.tp1)} the trailing stop starts and follows the price."
        else:
            state = f"Waiting for the profit target. Stop loss at {f(tr.stop[0])}."
            nxt = f"At {f(tr.tp1)} it takes profit on {tr.q[0]} contract{'s' if tr.q[0] != 1 else ''}"
            nxt += "; the rest then runs" + (" with its stop at break-even." if d.rules.be_after_tp1 else ".")
    else:
        k = tr.kind[1]
        if k == 1:
            state = f"Target reached. The remaining {out_qty} {'is' if out_qty == 1 else 'are'} protected at break-even ({f(tr.stop[1])})."
        elif k == 2:
            state = f"Target reached. A trailing stop follows the price, now at {f(tr.stop[1])}."
        else:
            state = f"Target reached. The rest runs with its stop at {f(tr.stop[1])}."
        nxt = "The trailing stop moves up as the price moves in our favour." if d.rules.trail else ""
        if not math.isnan(tr.tp2):
            nxt = (nxt + " " if nxt else "") + f"Final target {f(tr.tp2)}."
    lines.append(state)
    return {"state": state, "next": nxt}


def live_row(d: Deployment, engine=None) -> Optional[dict]:
    tr = d.trade
    if not tr and not d.held:
        return None
    tick, pv = d.market.tick, d.market.point_value
    m = d.meta or {}
    entry = m.get("entry_fill")
    adj = float(m.get("roll_adj") or 0.0)              # after a roll: new month - old month at the roll
    px = d.trade_px
    held = abs(d.held)
    side = tr.side if tr else (1 if d.held > 0 else -1)
    pnl = None
    if entry is not None and px is not None and held and not d.roll:
        pnl = round((px - entry - adj) * side * held * pv, 2)
    # realised part (legs already closed)
    real = 0.0
    for e in m.get("exits") or []:
        if entry is not None and e.get("fill") is not None:
            real += (e["fill"] - entry - float(e.get("adj") or 0.0)) * side * e["qty"] * pv
    # contract month: the one the trade is in, and the next one it can move to
    nxt = None
    if engine is not None and d.pos_expiry and not d.roll:
        later = [x for x in engine.month_choices(d.market.symbol) if x["expiry"] > d.pos_expiry]
        nxt = later[0]["label"] if later else None
    want = engine._contract_for(d)[1] if engine is not None else None
    rolls = m.get("rolls") or []
    stop = target = None
    basis = float(m.get("basis") or 0.0)              # traded contract - signal bars: levels in contract prices
    if tr:
        stop = (tr.stop[1] if (tr.open[1] or not tr.open[0]) else tr.stop[0]) + basis
        target = tr.tp1 if not tr.tp1_done else (None if math.isnan(tr.tp2) else tr.tp2)
        target = None if target is None else target + basis
    story = trade_story(d) if tr else {"state": "Closing.", "next": ""}
    if d.roll:
        story = {"state": f"Moving the position from {mkts.label(d.market.symbol, d.roll['from'])} to "
                          f"{mkts.label(d.market.symbol, d.roll['to'])}.",
                 "next": "The trade, its stop and its targets stay the same. Exits wait until the move is done."}
    elif rolls:
        r = rolls[-1]
        story = dict(story, next=(story["next"] + " " if story["next"] else "") +
                     f"Moved from {r['from']} to {r['to']} at a spread of {fmt(r.get('spread'), tick)}.")
    return {
        "id": d.id, "name": d.name, "family": d.card["family"], "market": d.market.symbol,
        "contract": m.get("contract"), "side": side_word(side), "qty": held,
        "account": d.trade_account, "next_month": nxt, "rolling": bool(d.roll),
        "month_differs": bool(want and d.pos_expiry and want != d.pos_expiry and not d.roll),
        "wanted_contract": mkts.label(d.market.symbol, want) if want else None,
        "entry": fmt(entry + adj if entry is not None else (tr.entry + basis if tr else None), tick),
        "entry_note": (f"{fmt(entry, tick)} in {rolls[0]['from']}, moved to {m.get('contract')}" if rolls and entry is not None else ""),
        "stop": fmt(stop, tick), "target": fmt(target, tick),
        "target_label": ("Final target" if tr and tr.tp1_done else "Profit target"),
        "price": fmt(px, tick), "pnl": pnl, "realised": round(real, 2) if real else 0.0,
        "opened": ny_time(m.get("opened")), "state": story["state"], "next": story["next"],
        "attention": d.attention, "working": bool(d.order or d.roll), "r": None if not tr or px is None else
        round(tr.r_now(px - (m.get("basis") or 0.0)), 2),
    }


def armed_row(d: Deployment) -> Optional[dict]:
    final = [p for p in d.plans if p.final]
    if not d.enabled or not d.plans or d.trade:
        return None
    tick = d.market.tick
    rows = []
    for p in (final or d.plans):
        q = sum(d.leg_qty(p.boost))
        rows.append({
            "side": side_word(p.side), "level": fmt(p.level, tick), "qty": q,
            "until": ny_time(p.valid_until) if p.final else "next candle",
            "stop": fmt(p.stop, tick) if p.final else None, "boost": p.boost})
    return {"id": d.id, "name": d.name, "market": d.market.symbol, "plans": rows}


def history(limit: int = 300) -> list:
    rows = read_jsonl(JOURNAL, limit)
    out = []
    for r in reversed(rows):
        out.append({**r, "opened_txt": ny_time(r.get("opened"), True, True), "closed_txt": ny_time(r.get("closed"), True, True)})
    return out


def today_pnl() -> float:
    start = pd.Timestamp.now(tz=NY).normalize()
    tot = 0.0
    for r in read_jsonl(JOURNAL, 2000):
        try:
            if pd.Timestamp(r["closed"]).tz_convert(NY) >= start:
                tot += float(r.get("pnl") or 0)
        except Exception:
            continue
    return round(tot, 2)


def snapshot(engine) -> dict:
    with engine.lock:
        deps = list(engine.deps.values())
        ready = {s for s, b in engine.books.items() if b.ready}
        rows = [strategy_row(d, d.market.signal_symbol in ready, engine) for d in deps]
        live = [r for r in (live_row(d, engine) for d in deps) if r]
        armed = [r for r in (armed_row(d) for d in deps) if r]
        hub = dict(engine.hub_state)
        attention = [{"id": d.id, "name": d.name, "market": d.market.symbol, "text": d.attention}
                     for d in deps if d.attention]
        data = {s: {"ready": b.ready, "price": b.current_price,
                    "age": None if not b.last_live else round(time.monotonic() - b.last_live)}
                for s, b in engine.books.items()}
        warming = sorted(engine._warming)
    open_pnl = sum(r["pnl"] or 0 for r in live)
    return {
        "time": pd.Timestamp.now(tz=NY).strftime("%H:%M:%S"),
        "hub": hub, "dry_run": engine.s.dry_run, "account": engine.s.account_id,
        "accounts_in_use": sorted({r["account"] for r in rows if r["enabled"] or r["has_trade"]}),
        "strategies": rows, "live": live, "armed": armed, "attention": attention,
        "data": data, "warming": warming, "problems": engine.problems,
        "summary": {"on": sum(1 for r in rows if r["enabled"]), "total": len(rows), "open": len(live),
                    "open_pnl": round(open_pnl, 2), "today": today_pnl()},
    }
