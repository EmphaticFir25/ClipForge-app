"""Download a VOD and extract mono 16 kHz audio for transcription."""
from __future__ import annotations

import subprocess
from pathlib import Path

from .config import Settings


def download_vod(url: str, job_dir: Path, s: Settings) -> Path:
    """Download the VOD with yt-dlp and return the path to source.mp4.

    Only use this for streams you have rights to or permission to clip.
    """
    job_dir.mkdir(parents=True, exist_ok=True)
    target = job_dir / "source.mp4"
    cmd = [
        s.ytdlp,
        "--no-playlist",
        "-f", "bv*[height<=1080]+ba/b",
        "--merge-output-format", "mp4",
        "-o", str(job_dir / "source.%(ext)s"),
        url,
    ]
    subprocess.run(cmd, check=True)
    if not target.exists():
        raise FileNotFoundError(f"yt-dlp finished but {target} is missing")
    return target


def extract_audio(video: Path, wav: Path, s: Settings) -> Path:
    """Mono 16 kHz PCM, the format whisper.cpp expects."""
    subprocess.run(
        [
            s.ffmpeg, "-y", "-i", str(video),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            str(wav),
        ],
        check=True,
    )
    return wav
