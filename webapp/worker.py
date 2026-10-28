"""Background worker: runs the ClipForge engine on queued jobs.

Run one worker per GPU:
    python -m webapp.worker
"""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import json
from dataclasses import replace

from clipforge.captions import build_ass
from clipforge.config import Settings
from clipforge.pipeline import run_job
from clipforge.render import output_size, render_clip
from clipforge.tracking import crop_keyframes, crop_x_expr, detect_face_centers, probe_size

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


def engine_render(data_dir: Path):
    """Re-render one clip with the user's trim, caption edits, style and aspect ratio."""
    def run(job_dir: Path, source: Path, clip_n: int, aspect: str, spec: dict, watermark: bool) -> None:
        base = Settings(work_dir=data_dir / "work")
        out_w, out_h = output_size(aspect, base)
        s = replace(base, out_w=out_w, out_h=out_h)
        words = [dict(w) for w in json.loads((job_dir / "transcript.json").read_text(encoding="utf-8"))["words"]]
        for idx, text in spec["word_edits"].items():
            if 0 <= idx < len(words):
                words[idx]["word"] = text
        start, end = spec["start"], spec["end"]
        tag = aspect.replace(":", "x")
        ass = job_dir / f"clip_{clip_n:02d}__{tag}.ass"
        ass.write_text(build_ass(words, start, end, s, style=spec["caption_style"]), encoding="utf-8")

        crop_x = None
        if aspect in ("9:16", "1:1") and base.face_tracking:
            src_w, src_h = probe_size(source, base)
            centers = detect_face_centers(source, start, end - start, base)
            crop_w = src_h * 9 / 16 if aspect == "9:16" else src_h
            crop_x = crop_x_expr(crop_keyframes(centers, src_w, src_h, crop_w=crop_w))
        logo = watermark_path(data_dir) if watermark else None
        out = job_dir / "clips" / f"clip_{clip_n:02d}__{tag}.mp4"
        render_clip(source, start, end, out, base, ass_path=ass, crop_x=crop_x, watermark=logo, aspect=aspect)
    return run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clipforge-worker")
    parser.add_argument("--once", action="store_true", help="process at most one job, then exit")
    parser.add_argument("--poll", type=float, default=5.0, help="seconds to wait when the queue is empty")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    service, data_dir = build_service()
    worker = Worker(service, engine_pipeline(data_dir), render_pipeline=engine_render(data_dir))
    while True:
        did_work = worker.run_once()
        if args.once:
            return 0
        if not did_work:
            time.sleep(args.poll)


if __name__ == "__main__":
    raise SystemExit(main())
