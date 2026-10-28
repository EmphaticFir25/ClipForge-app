"""Scheduled housekeeping: clip retention and due posts.

    python -m webapp.maintenance          run once
    python -m webapp.maintenance --loop   run every 15 minutes

Run it beside the worker. Both steps are safe to repeat.
"""
from __future__ import annotations

import argparse
import logging
import time

from .main import build_service, configure_logging

log = logging.getLogger("clipforge.maintenance")


def run_once() -> dict:
    service, _ = build_service()
    purged = service.purge_expired()
    dispatched = service.dispatch_due_schedules()
    log.info("purged %s jobs; schedules: %s", purged, dispatched)
    return {"purged": purged, **dispatched}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clipforge-maintenance")
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args(argv)
    configure_logging()
    while True:
        run_once()
        if not args.loop:
            return 0
        time.sleep(15 * 60)


if __name__ == "__main__":
    raise SystemExit(main())
