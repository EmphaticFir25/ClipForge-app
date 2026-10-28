"""Background worker: runs the ClipForge engine on queued jobs.

Run one worker per GPU:
    python -m webapp.worker
"""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from clipforge.config import Settings
from clipforge.pipeline import run_job

from .main import build_service, watermark_path
from .service import Worker

log = logging.getLogger("clipforge.worker")


def engine_pipeline(data_dir: Path):
    def run(source: Path, job_dir: Path, top: int, watermark: bool) -> int:
        settings = Settings(work_dir=data_dir / "work")
        logo = watermark_path(data_dir) if watermark else None
        run_job(str(source), settings, top=top, log=lambda m: log.info(m), job_dir=job_dir,
                watermark=logo)
        clips_dir = job_dir / "clips"
        return len(list(clips_dir.glob("clip_*.mp4"))) if clips_dir.exists() else 0
    return run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clipforge-worker")
    parser.add_argument("--once", action="store_true", help="process at most one job, then exit")
    parser.add_argument("--poll", type=float, default=5.0, help="seconds to wait when the queue is empty")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    service, data_dir = build_service()
    worker = Worker(service, engine_pipeline(data_dir))
    while True:
        did_work = worker.run_once()
        if args.once:
            return 0
        if not did_work:
            time.sleep(args.poll)


if __name__ == "__main__":
    raise SystemExit(main())
