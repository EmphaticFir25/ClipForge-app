"""Cut a clip, reframe it to 9:16 and burn in captions with ffmpeg."""
from __future__ import annotations

import subprocess
from pathlib import Path

from .config import Settings


def render_clip(
    source: Path,
    start: float,
    end: float,
    out_path: Path,
    s: Settings,
    ass_path: Path | None = None,
    crop_x: str | None = None,
) -> Path:
    """Crop to 9:16 and scale to out_w x out_h.

    crop_x is an ffmpeg expression for the crop's left edge (from tracking.crop_x_expr).
    When it is None the crop is centred.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    x_expr = crop_x if crop_x is not None else "(iw-ih*9/16)/2"
    filters = [
        f"crop=ih*9/16:ih:{x_expr}:0",
        f"scale={s.out_w}:{s.out_h}",
        "setsar=1",
    ]
    cwd = None
    if ass_path is not None:
        # Run from the ASS file's folder so the subtitles filter doesn't need
        # Windows-style path escaping (C:\... colons break filter syntax).
        cwd = ass_path.parent
        filters.append(f"ass={ass_path.name}")

    cmd = [
        s.ffmpeg, "-y",
        "-ss", f"{start:.3f}",
        "-i", str(source.resolve()),
        "-t", f"{end - start:.3f}",
        "-vf", ",".join(filters),
        "-c:v", s.video_encoder,
    ]
    if s.video_encoder == "libx264":
        cmd += ["-crf", "20"]  # AMF encoders use their own quality flags
    cmd += [
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        str(out_path.resolve()),
    ]
    subprocess.run(cmd, check=True, cwd=cwd)
    return out_path
