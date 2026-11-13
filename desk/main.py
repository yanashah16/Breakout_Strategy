"""
Start Pattern Desk:   python -m desk.main      (or double-click start.bat)
Then open http://127.0.0.1:8002
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys

import uvicorn

from desk import settings as S


def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.TimedRotatingFileHandler(S.LOGS / "desk.log", when="midnight", backupCount=30,
                                                   encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.handlers = [fh, sh]
    for noisy in ("uvicorn.access", "urllib3", "numba"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main():
    os.chdir(S.ROOT)
    s = S.load()
    S.LOGS.mkdir(parents=True, exist_ok=True)
    setup_logging()
    log = logging.getLogger("desk")
    log.info(f"Pattern Desk starting - account {s.account_id}, hub {s.hub_url}"
             + (" - DRY RUN (no orders are sent)" if s.dry_run else ""))
    from desk.engine import Engine
    from desk.web.api import build
    engine = Engine(s)
    if engine.problems:
        for p in engine.problems:
            log.warning(f"catalog: {p}")
    log.info(f"{len(engine.cards)} strategies in the catalog, {len(engine.deps)} rows, "
             f"{sum(1 for d in engine.deps.values() if d.enabled)} switched on")
    engine.start()
    app = build(engine)
    try:
        uvicorn.run(app, host=s.host, port=s.port, log_level="warning")
    finally:
        engine.stop()
        log.info("Pattern Desk stopped")


if __name__ == "__main__":
    main()
