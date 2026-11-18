"""HTTP API + the static pages."""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from desk import catalog as cat
from desk import describe
from desk.engine import EVENTS
from desk.store import read_jsonl

STATIC = Path(__file__).resolve().parent / "static"


class AddReq(BaseModel):
    strategy: str
    market: str
    qty: List[int]
    account: Optional[str] = None
    expiry: Optional[str] = None


class AccountReq(BaseModel):
    account: str


class RatioReq(BaseModel):
    ratio: float


class ExpiryReq(BaseModel):
    expiry: str
    move_trade: bool = True


class QtyReq(BaseModel):
    qty: List[int]


class EnableReq(BaseModel):
    on: bool
    mode: Optional[str] = "after_trade"


class AttentionReq(BaseModel):
    how: str


class RiskReq(BaseModel):
    usd: float


def build(engine, stocks=None) -> FastAPI:
    app = FastAPI(title="Pattern Desk", docs_url=None, redoc_url=None)

    def run(fn, *a):
        try:
            return fn(*a)
        except KeyError as exc:
            raise HTTPException(404, f"not found: {exc}")
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/state")
    def state():
        return describe.snapshot(engine)

    @app.get("/api/catalog")
    def catalog():
        cards = sorted(engine.cards.values(), key=lambda c: (c["family"], c.get("experimental", False), c["name"]))
        return {"strategies": [cat.public(c) for c in cards], "problems": engine.problems}

    @app.get("/api/history")
    def history():
        return {"trades": describe.history()}

    @app.get("/api/events")
    def events():
        return {"events": list(reversed(read_jsonl(EVENTS, 100)))}

    @app.post("/api/deployments")
    def add(req: AddReq):
        return {"id": run(engine.add, req.strategy, req.market, req.qty, req.account, req.expiry)}

    @app.get("/api/accounts")
    def accounts():
        a = engine.accounts()
        if a is None:
            return {"accounts": [], "managed": [], "default": engine.s.account_id,
                    "error": "The Trading Hub cannot list the accounts right now."}
        rows = [{"account": r.get("account"), "net_liquidation": r.get("net_liquidation"),
                 "available_funds": r.get("available_funds")} for r in a.get("accounts") or []]
        return {"accounts": rows, "managed": a.get("managed") or [r["account"] for r in rows],
                "default": engine.s.account_id}

    @app.get("/api/roll")
    def roll_status():
        with engine.lock:
            return {"ratio": engine.rolls.ratio, "instruments": engine.roll_status()}

    @app.post("/api/roll/ratio")
    def roll_ratio(req: RatioReq):
        return {"ratio": run(engine.set_roll_ratio, req.ratio)}

    @app.post("/api/roll/check")
    def roll_check():
        with engine.lock:
            return {"instruments": engine.check_volumes_now()}

    @app.get("/api/months/{market}")
    def months(market: str):
        return {"months": run(engine.month_choices, market.upper())}

    @app.post("/api/accounts/all")
    def account_all(req: AccountReq):
        return run(engine.set_all_accounts, req.account)

    @app.post("/api/deployments/{did}/account")
    def account(did: str, req: AccountReq):
        run(engine.set_account, did, req.account)
        return {"ok": True}

    @app.post("/api/deployments/{did}/expiry")
    def expiry(did: str, req: ExpiryReq):
        return {"ok": True, "message": run(engine.set_expiry, did, req.expiry, req.move_trade)}

    @app.post("/api/deployments/{did}/roll")
    def roll(did: str):
        return {"ok": True, "message": run(engine.roll_next, did)}

    @app.delete("/api/deployments/{did}")
    def remove(did: str):
        run(engine.remove, did)
        return {"ok": True}

    @app.post("/api/deployments/{did}/qty")
    def qty(did: str, req: QtyReq):
        run(engine.set_qty, did, req.qty)
        return {"ok": True}

    @app.post("/api/deployments/{did}/enable")
    def enable(did: str, req: EnableReq):
        run(engine.set_enabled, did, req.on, req.mode or "after_trade")
        return {"ok": True}

    @app.post("/api/deployments/{did}/close")
    def close(did: str):
        run(engine.close_trade, did)
        return {"ok": True}

    @app.post("/api/deployments/{did}/attention")
    def attention(did: str, req: AttentionReq):
        if req.how not in ("correct", "closed_by_hand"):
            raise HTTPException(400, "how must be correct | closed_by_hand")
        run(engine.resolve_attention, did, req.how)
        return {"ok": True}

    @app.post("/api/close-everything")
    def close_everything():
        engine.close_everything()
        if stocks is not None:
            stocks.close_all()
        return {"ok": True}

    # ---------------------------------------------------------------- US stocks (desk/stocks)
    def _stocks():
        if stocks is None:
            raise HTTPException(503, "the stock desk is not running")
        return stocks

    @app.get("/api/stocks/state")
    def stocks_state():
        return _stocks().snapshot()

    @app.get("/api/stocks/history")
    def stocks_history():
        return {"trades": _stocks().history()}

    @app.post("/api/stocks/copies/{cid}/enable")
    def stocks_enable(cid: str, req: EnableReq):
        run(_stocks().set_enabled, cid, req.on, req.mode or "after_trade")
        return {"ok": True}

    @app.post("/api/stocks/copies/{cid}/capital")
    def stocks_capital(cid: str, req: RiskReq):
        run(_stocks().set_capital, cid, req.usd)
        return {"ok": True}

    @app.post("/api/stocks/exposure")
    def stocks_exposure(req: RiskReq):
        run(_stocks().set_exposure, req.usd)
        return {"ok": True}

    @app.post("/api/stocks/positions/{bid}/close")
    def stocks_close(bid: str):
        run(_stocks().close_trade, bid)
        return {"ok": True}

    @app.post("/api/stocks/positions/{bid}/attention")
    def stocks_attention(bid: str, req: AttentionReq):
        if req.how not in ("correct", "closed_by_hand"):
            raise HTTPException(400, "how must be correct | closed_by_hand")
        run(_stocks().resolve_attention, bid, req.how)
        return {"ok": True}

    @app.post("/api/stocks/close-all")
    def stocks_close_all():
        _stocks().close_all()
        return {"ok": True}

    @app.post("/api/reload")
    def reload():
        return {"problems": engine.reload_catalog()}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def page_strategies():
        return FileResponse(STATIC / "index.html")

    @app.get("/live")
    def page_live():
        return FileResponse(STATIC / "live.html")

    @app.get("/history")
    def page_history():
        return FileResponse(STATIC / "history.html")

    @app.get("/stocks")
    def page_stocks():
        return FileResponse(STATIC / "stocks.html")

    return app
