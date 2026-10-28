"""End-to-end job: VOD -> audio -> transcript -> moments -> rendered clips."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .captions import build_ass
from .config import Settings
from .ingest import download_vod, extract_audio
from .moments import find_moments
from .publish import PLATFORMS, QueueItem, ReviewQueue
from .render import render_clip
from .signals import audio_loudness_per_second, chat_activity_per_second
from .tracking import crop_keyframes, crop_x_expr, detect_face_centers, probe_size
from .transcribe import transcribe


def new_job_dir(s: Settings) -> Path:
    job = s.work_dir / time.strftime("%Y%m%d-%H%M%S")
    job.mkdir(parents=True, exist_ok=True)
    return job


def run_job(source: str, s: Settings, top: int | None = None,
            chat_file: Path | None = None, log=print, job_dir: Path | None = None,
            watermark: Path | None = None) -> Path:
    """Run the full pipeline on one local video.

    job_dir lets the web app choose the folder. The CLI-style default creates a
    timestamped folder under the work directory.
    """
    if job_dir is None:
        job = new_job_dir(s)
    else:
        job = Path(job_dir)
        job.mkdir(parents=True, exist_ok=True)
    log(f"Job folder: {job}")

    # 1. Get the video
    if source.startswith(("http://", "https://")):
        log("Downloading VOD...")
        video = download_vod(source, job, s)
    else:
        video = Path(source).resolve()

    # 2. Transcribe, and measure audio loudness per second
    log("Extracting audio and transcribing...")
    wav = extract_audio(video, job / "audio.wav", s)
    transcript = transcribe(wav, job / "transcript.json", s)
    log(f"{len(transcript['words'])} words, {len(transcript['segments'])} segments")
    audio_z = audio_loudness_per_second(wav)

    # 3. Chat activity, if a chat export was given
    chat_z = None
    if chat_file:
        chat_z = chat_activity_per_second(chat_file, len(audio_z))
        log(f"Chat signal loaded from {chat_file.name}")

    # 4. Find moments with the local model, blended with the signals
    log("Scoring moments with the local model...")
    moments = find_moments(
        transcript, s,
        audio_z=audio_z,
        chat_z=chat_z,
        on_window=lambda i, n: log(f"  window {i}/{n}") if i % 10 == 0 else None,
    )
    (job / "moments.json").write_text(json.dumps(moments, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"{len(moments)} moments after de-duplication")

    # 5. Render the top clips with captions and face-tracked crops
    picks = moments[: (top or s.top_n)]
    queue = ReviewQueue(job / "queue.json")
    src_w = src_h = None
    if s.face_tracking:
        src_w, src_h = probe_size(video, s)

    for i, m in enumerate(picks, 1):
        clip_name = f"clip_{i:02d}"
        ass = job / f"{clip_name}.ass"
        ass.write_text(build_ass(transcript["words"], m["start"], m["end"], s), encoding="utf-8")

        crop_x = None
        if s.face_tracking:
            centers = detect_face_centers(video, m["start"], m["end"] - m["start"], s)
            crop_x = crop_x_expr(crop_keyframes(centers, src_w, src_h))

        out = job / "clips" / f"{clip_name}.mp4"
        log(f"Rendering {clip_name} ({m['end'] - m['start']:.0f}s, score {m['score']:.0f}, "
            f"{'face-tracked' if crop_x else 'centred'} crop)")
        render_clip(video, m["start"], m["end"], out, s, ass_path=ass, crop_x=crop_x,
                    watermark=watermark)
        queue.add(QueueItem(
            clip_path=str(out),
            title=m["title"],
            caption=m["caption"],
            hashtags=m["hashtags"],
            platforms=list(PLATFORMS),
        ))

    log(f"Done. Review the clips in {job / 'clips'}, or run: clipforge serve")
    return job
