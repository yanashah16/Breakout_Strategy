"""
Trading Hub client (HTTP). The desk never talks to IBKR directly.

Every call returns None on a transport / server failure, so callers can tell
"the hub did not answer" apart from "the answer is empty" (positions!).
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import requests

log = logging.getLogger("desk.hub")


class Hub:
    def __init__(self, url: str, api_key: str, algo_id: str, account_id: str):
        self.url = url.rstrip("/")
        self.algo_id = algo_id
        self.account_id = account_id
        self.s = requests.Session()
        if api_key:
            self.s.headers["X-API-Key"] = api_key
        self.last_ok = 0.0          # monotonic time of the last good answer
        self.last_error = ""

    # ------------------------------------------------------------------ plumbing
    def _call(self, method: str, path: str, timeout: float = 10, quiet404: bool = False, **kw):
        try:
            r = self.s.request(method, f"{self.url}{path}", timeout=timeout, **kw)
        except requests.RequestException as exc:
            self.last_error = f"hub not reachable ({type(exc).__name__})"
            log.debug(f"{method} {path}: {exc}")
            return None
        if r.status_code == 404 and quiet404:
            self.last_ok = time.monotonic()
            return {"_status": 404}
        if r.status_code != 200:
            self.last_error = f"{path}: {r.status_code} {r.text[:160]}"
            log.warning(f"{method} {path}: {r.status_code} {r.text[:300]}")
            return {"_status": r.status_code, "_text": r.text[:500]}
        self.last_ok = time.monotonic()
        try:
            return r.json()
        except ValueError:
            return {}

    @staticmethod
    def ok(res) -> bool:
        return isinstance(res, dict) and "_status" not in res

    # ------------------------------------------------------------------ health
    def health(self) -> Optional[dict]:
        res = self._call("GET", "/health", timeout=5)
        return res if self.ok(res) else None

    def register(self) -> bool:
        res = self._call("POST", "/algo/register",
                         json={"algo_id": self.algo_id, "description": "Pattern Desk"})
        return self.ok(res)

    # ------------------------------------------------------------------ bars
    def subscribe_bars(self, symbol: str, exchange: str, sec_type: str = "FUT") -> bool:
        res = self._call("POST", "/bars/subscribe", timeout=30, json={
            "algo_id": self.algo_id, "symbol": symbol, "exchange": exchange, "sec_type": sec_type,
            "currency": "USD", "duration": "1 D", "bar_size": "1 min", "what_to_show": "TRADES",
            "use_rth": False})
        return self.ok(res) and res.get("status") == "subscribed"

    def bars(self, symbol: str) -> Optional[dict]:
        res = self._call("GET", f"/bars/{requests.utils.quote(symbol)}", timeout=10, quiet404=True)
        return res if self.ok(res) else None

    def history(self, symbol: str, exchange: str, bar_size: str, duration: str, sec_type: str = "FUT",
                use_rth: bool = False) -> Optional[list]:
        res = self._call("POST", "/history", timeout=150, json={
            "symbol": symbol, "exchange": exchange, "sec_type": sec_type, "currency": "USD",
            "bar_size": bar_size, "duration": duration, "what_to_show": "TRADES", "use_rth": use_rth})
        return res.get("bars") if self.ok(res) else None

    def month_history(self, symbol: str, exchange: str, bar_size: str, duration: str,
                      expiry: str) -> Optional[dict]:
        """History of ONE contract month (hub /history with expiry). The answer echoes `expiry`; an older
        hub ignores the field and answers with the continuous contract - the caller must check the echo."""
        res = self._call("POST", "/history", timeout=90, json={
            "symbol": symbol, "exchange": exchange, "sec_type": "FUT", "currency": "USD", "expiry": expiry,
            "bar_size": bar_size, "duration": duration, "what_to_show": "TRADES", "use_rth": False})
        return res if self.ok(res) else None

    # ------------------------------------------------------------------ prices
    def prices(self, contracts: List[dict]) -> Dict[int, dict]:
        """{con_id: price dict} for contracts given with their con_id."""
        if not contracts:
            return {}
        body = {"algo_id": self.algo_id, "con_ids": [int(c["con_id"]) for c in contracts if c.get("con_id")],
                "contracts": contracts}
        res = self._call("POST", "/price", timeout=10, json=body)
        if not self.ok(res):
            return {}
        return {int(k): v for k, v in (res.get("prices") or {}).items()}

    def resolve(self, contract: dict) -> Optional[int]:
        """con_id of a contract (symbol + expiry) - the hub qualifies it on first sight."""
        want = str(contract.get("symbol", "")).upper()
        for attempt in range(5):
            res = self._call("POST", "/price", timeout=15,
                             json={"algo_id": self.algo_id, "contracts": [contract]})
            if self.ok(res):
                for cid, d in (res.get("prices") or {}).items():
                    if not d.get("symbol") or str(d.get("symbol")).upper() == want:
                        return int(cid)
            time.sleep(1.0 + attempt)
        return None

    # ------------------------------------------------------------------ orders
    def order_key(self, ref: str, account: Optional[str] = None) -> str:
        return f"{self.algo_id}_{account or self.account_id}_{ref}"

    def order(self, key: str, contract: dict, action: str, qty: int, exit_: bool,
              account: Optional[str] = None) -> dict:
        return {"order_key": key, "algo_id": self.algo_id, "account_id": account or self.account_id,
                "contract": contract, "action": action, "quantity": int(qty),
                "chase_bid_secs": 0, "chase_mid_secs": 0, "is_exit": bool(exit_)}

    def submit(self, key: str, contract: dict, action: str, qty: int, exit_: bool,
               account: Optional[str] = None) -> Optional[dict]:
        return self.submit_batch(key, [self.order(key, contract, action, qty, exit_, account)])

    def submit_batch(self, batch_key: str, orders: List[dict]) -> Optional[dict]:
        """Several orders in one call; the hub places them all at once (a contract roll's two legs)."""
        return self._call("POST", "/orders", timeout=15,
                          json={"batch_key": batch_key, "algo_id": self.algo_id, "orders": orders})

    def order_status(self, key: str) -> Optional[dict]:
        res = self._call("GET", f"/orders/{key}", timeout=10, quiet404=True)
        if res is None:
            return None
        if res.get("_status") == 404:
            return {"status": "NOT_FOUND"}
        return res if self.ok(res) else None

    def cancel(self, key: str) -> bool:
        res = self._call("DELETE", f"/orders/{key}", timeout=10, json={"algo_id": self.algo_id})
        return self.ok(res)

    # ------------------------------------------------------------------ account
    def positions(self, symbol: Optional[str] = None, account: Optional[str] = None,
                  sec_type: str = "FUT") -> Optional[list]:
        params = {"sec_type": sec_type, "account": account or self.account_id}
        if symbol:
            params["symbol"] = symbol
        res = self._call("GET", "/positions", timeout=15, params=params)
        return res.get("positions") if self.ok(res) else None

    def accounts(self) -> Optional[dict]:
        """{"managed": [ids], "accounts": [{account, net_liquidation, ...}]} - the accounts this IBKR login
        can trade. None = the hub could not say (never read that as "no accounts")."""
        res = self._call("GET", "/accounts", timeout=20)
        return res if self.ok(res) else None
