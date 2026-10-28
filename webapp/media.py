"""Checks what an upload really is before any engine work starts.

The extension is only a hint. ffprobe reads the container, so a renamed text file,
an audio-only file or a file with no video stream is refused here.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

# Container names ffprobe reports for the formats we accept (see ALLOWED_EXTENSIONS).
ALLOWED_FORMATS = {"mov", "mp4", "matroska", "webm"}
PROBE_TIMEOUT_S = 60
MAX_DURATION_S = 24 * 3600  # no single source longer than a day


class MediaRejected(ValueError):
    """The file is not a video we can process. The message is safe to show the user."""


def _ffprobe() -> str:
    """The resource-limited wrapper unless another path is configured."""
    default = str(Path(__file__).parent / "bin" / "cf-ffprobe")
    return os.environ.get("CLIPFORGE_FFPROBE") or default


def inspect(path: Path, ffprobe: str | None = None) -> dict:
    """Returns {'duration_s', 'width', 'height', 'format'} or raises MediaRejected."""
    cmd = [ffprobe or _ffprobe(), "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        raise MediaRejected("this file took too long to read; try exporting it again as an mp4")
    except OSError:
        raise MediaRejected("video inspection is not available on this server")
    if out.returncode != 0:
        raise MediaRejected("this file isn't a video we can read; try exporting it again as an mp4")
    try:
        data = json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        raise MediaRejected("this file isn't a video we can read")

    fmt = data.get("format") or {}
    names = {n.strip() for n in str(fmt.get("format_name", "")).split(",") if n.strip()}
    if not names & ALLOWED_FORMATS:
        raise MediaRejected("unsupported video format; use an mp4, mov, mkv or webm file")

    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None or not int(video.get("width") or 0) or not int(video.get("height") or 0):
        raise MediaRejected("this file has no video track")

    try:
        duration = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise MediaRejected("this video has no length")
    if duration > MAX_DURATION_S:
        raise MediaRejected("this video is longer than 24 hours")

    return {"duration_s": duration, "width": int(video["width"]), "height": int(video["height"]),
            "format": sorted(names)[0]}
