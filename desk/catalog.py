"""
Strategy catalog: every strategies/**/*.yaml card. A new strategy = a new card file
(see strategies/README.md); the desk picks it up at the next start or "Reload".
"""
from __future__ import annotations

import logging
from typing import Dict, List, Tuple

import yaml

from desk import logic as L
from desk import markets
from desk.settings import STRATEGIES

log = logging.getLogger("desk.catalog")
REQUIRED = ("id", "name", "family", "signal", "markets", "logic")


def load() -> Tuple[Dict[str, dict], List[str]]:
    """{id: card} and a list of problems (bad cards are skipped, never fatal)."""
    cards, problems = {}, []
    for path in sorted(STRATEGIES.rglob("*.yaml")):
        try:
            c = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            miss = [k for k in REQUIRED if k not in c]
            if miss:
                raise ValueError(f"missing {miss}")
            if c["id"] in cards:
                raise ValueError(f"id {c['id']!r} is used twice")
            for m in c["markets"]:
                if markets.get(m).signal_symbol != c["signal"]:
                    raise ValueError(f"{m} does not trade on {c['signal']} signals")
            L.make(c)                                      # the logic must build
            c["_file"] = str(path.relative_to(STRATEGIES))
            cards[c["id"]] = c
        except Exception as exc:
            problems.append(f"{path.name}: {exc}")
            log.error(f"strategy card {path.name} skipped: {exc}")
    return cards, problems


def public(card: dict) -> dict:
    """What the UI shows about a strategy (no parameters)."""
    keys = ("id", "name", "family", "signal", "markets", "pattern", "summary", "how_it_enters",
            "how_it_exits", "experimental", "default_qty", "source")
    out = {k: card.get(k) for k in keys}
    q = card.get("default_qty") or [0, 1]
    out["legs"] = [k for k, v in enumerate(q) if v]       # which quantity boxes to show
    out["boost"] = (card.get("logic") or {}).get("boost_qty1") is not None
    return out
