"""Paths and settings (config/settings.yaml)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "settings.yaml"
STRATEGIES = ROOT / "strategies"
STATE = ROOT / "state"
DATA = ROOT / "data"
LOGS = ROOT / "logs"


@dataclass
class Settings:
    hub_url: str
    hub_api_key: str
    account_id: str
    algo_id: str
    host: str
    port: int
    tick_seconds: float
    warmup_days: int
    bar_settle_seconds: float
    entry_wait_seconds: float
    reconcile_minutes: float
    dry_run: bool
    auto_roll: bool = True
    entry_max_age_minutes: float = 3.0
    entry_max_chase_r: float = 0.5


def load(path: Path = CONFIG) -> Settings:
    d = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    h, a, w, e = (d.get(k) or {} for k in ("hub", "account", "web", "engine"))
    for p in (STATE, DATA, LOGS):
        p.mkdir(parents=True, exist_ok=True)
    return Settings(
        hub_url=str(h.get("url") or "http://127.0.0.1:8000").rstrip("/"),
        hub_api_key=str(h.get("api_key") or ""),
        account_id=str(a.get("id") or ""),
        algo_id=str(a.get("algo_id") or "pattern_desk"),
        host=str(w.get("host") or "127.0.0.1"),
        port=int(w.get("port") or 8002),
        tick_seconds=float(e.get("tick_seconds") or 2),
        warmup_days=min(120, int(e.get("warmup_days") or 90)),
        bar_settle_seconds=float(e.get("bar_settle_seconds") or 3),
        entry_wait_seconds=float(e.get("entry_wait_seconds") or 60),
        reconcile_minutes=float(e.get("reconcile_minutes") or 5),
        dry_run=bool(e.get("dry_run")),
        auto_roll=bool(e.get("auto_roll", True)),
        entry_max_age_minutes=float(e.get("entry_max_age_minutes", 3)),
        entry_max_chase_r=float(e.get("entry_max_chase_r", 0.5)),
    )
