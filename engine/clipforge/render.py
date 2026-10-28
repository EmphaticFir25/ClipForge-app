"""Cut a clip, reframe it to 9:16, burn in captions and optionally add a logo watermark."""
from __future__ import annotations

import subprocess
from pathlib import Path

from .config import Settings

WATERMARK_WIDTH_RATIO = 0.18   # logo width as a share of the frame
WATERMARK_OPACITY = 0.6
WATERMARK_MARGIN = 40          # pixels from the top-right corner


def render_clip(
    source: Path,
    start: float,
    end: float,
    out_path: Path,
    s: Settings,
    ass_path: Path | None = None,
    crop_x: str | None = None,
    watermark: Path | None = None,
) -> Path:
    """Crop to 9:16, scale, burn captions and (optionally) overlay a logo.

    crop_x is an ffmpeg expression for the crop's left edge (from tracking.crop_x_expr).
    When it is None the crop is centred. The watermark goes in the top-right corner,
    above the caption area.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    x_expr = crop_x if crop_x is not None else "(iw-ih*9/16)/2"
    chain = [
        f"crop=ih*9/16:ih:{x_expr}:0",
        f"scale={s.out_w}:{s.out_h}",
        "setsar=1",
    ]
    cwd = None
    if ass_path is not None:
        # Run from the ASS file's folder so the subtitles filter doesn't need
        # Windows-style path escaping (C:\... colons break filter syntax).
        cwd = ass_path.parent
        chain.append(f"ass={ass_path.name}")

    cmd = [s.ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", str(source.resolve())]
    if watermark is not None:
        cmd += ["-i", str(watermark.resolve())]
        logo_w = int(s.out_w * WATERMARK_WIDTH_RATIO)
        graph = (
            f"[0:v]{','.join(chain)}[base];"
            f"[1:v]format=rgba,scale={logo_w}:-1,colorchannelmixer=aa={WATERMARK_OPACITY}[logo];"
            f"[base][logo]overlay=W-w-{WATERMARK_MARGIN}:{WATERMARK_MARGIN}[out]"
        )
        cmd += ["-filter_complex", graph, "-map", "[out]", "-map", "0:a:0?"]
    else:
        cmd += ["-vf", ",".join(chain)]

    cmd += ["-t", f"{end - start:.3f}", "-c:v", s.video_encoder]
    if s.video_encoder == "libx264":
        cmd += ["-crf", "20"]  # AMF encoders use their own quality flags
    cmd += [
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        str(out_path.resolve()),
    ]
    subprocess.run(cmd, check=True, cwd=cwd)
    return out_path
