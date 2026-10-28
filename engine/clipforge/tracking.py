"""Keep the 9:16 crop on the speaker's face.

Pipeline for one clip:
  1. ffmpeg decodes the clip at 2 fps, scaled to 320 px wide, as raw RGB frames.
  2. MediaPipe face detection finds the largest face in each frame.
  3. Face centres are smoothed and turned into crop x-positions (source pixels).
  4. A piecewise-linear expression of t is passed to ffmpeg's crop filter, so the
     crop pans with the speaker.

If mediapipe or numpy isn't installed, or no face is found, detect_face_centers
returns nothing and the caller falls back to the centred crop.
"""
from __future__ import annotations

import math
import subprocess

from .config import Settings

SAMPLE_FPS = 2
PROBE_W = 320
SMOOTHING = 0.25   # exponential smoothing factor (lower = steadier, slower)


def probe_size(source, s: Settings) -> tuple[int, int]:
    out = subprocess.run(
        [s.ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(source)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


def detect_face_centers(source, start: float, duration: float, s: Settings) -> list[tuple[float, float]]:
    """Returns (seconds_into_clip, face_centre_x as a fraction of frame width)."""
    try:
        import mediapipe as mp
        import numpy as np
    except ImportError:
        return []
    if not hasattr(mp, "solutions"):  # newer mediapipe builds dropped the legacy API
        return []

    src_w, src_h = probe_size(source, s)
    out_w = PROBE_W
    out_h = int(round(src_h * PROBE_W / src_w / 2)) * 2  # even height for the scaler
    frame_bytes = out_w * out_h * 3

    cmd = [
        s.ffmpeg, "-v", "error",
        "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{duration:.3f}",
        "-vf", f"fps={SAMPLE_FPS},scale={out_w}:{out_h}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]
    detector = mp.solutions.face_detection.FaceDetection(model_selection=1, min_detection_confidence=0.5)
    centers: list[tuple[float, float]] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    try:
        i = 0
        while True:
            raw = proc.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            frame = np.frombuffer(raw, dtype=np.uint8).reshape(out_h, out_w, 3)
            result = detector.process(frame)
            if result.detections:
                best = max(result.detections, key=lambda d: d.location_data.relative_bounding_box.width)
                box = best.location_data.relative_bounding_box
                centers.append((i / SAMPLE_FPS, box.xmin + box.width / 2))
            i += 1
    finally:
        detector.close()
        proc.stdout.close()
        proc.wait()
    return centers


def crop_keyframes(centers: list[tuple[float, float]], src_w: int, src_h: int,
                   max_keys: int = 40, crop_w: float | None = None) -> list[tuple[float, float]]:
    """Face centres to (t, crop_x_pixels), smoothed and thinned to max_keys.

    crop_w is the crop's width in source pixels: 9:16 by default, src_h for a square.
    """
    if not centers:
        return []
    crop_w = src_h * 9 / 16 if crop_w is None else crop_w
    max_x = max(src_w - crop_w, 0.0)
    keys: list[tuple[float, float]] = []
    current: float | None = None
    for t, cx in centers:
        target = min(max(cx * src_w - crop_w / 2, 0.0), max_x)
        current = target if current is None else current + SMOOTHING * (target - current)
        keys.append((t, current))
    step = max(1, math.ceil(len(keys) / max_keys))
    return keys[::step]


def crop_x_expr(keys: list[tuple[float, float]]) -> str | None:
    """Build an ffmpeg expression that linearly interpolates the crop x over time t.

    Commas are escaped because they separate arguments inside a filtergraph.
    Returns None when there are no keys (use the centred crop).
    """
    if not keys:
        return None
    if len({round(x, 1) for _, x in keys}) == 1:
        return f"{keys[0][1]:.1f}"  # the face never moves: a constant is enough
    if keys[0][0] > 0:
        keys = [(0.0, keys[0][1])] + keys  # hold the first position before the first face
    if len(keys) == 1:
        return f"{keys[0][1]:.1f}"

    expr = f"{keys[-1][1]:.1f}"  # hold the last position after the final key
    for (t0, x0), (t1, x1) in reversed(list(zip(keys, keys[1:]))):
        seg = f"{x0:.1f}+({x1 - x0:.1f})*(t-{t0:.3f})/{max(t1 - t0, 0.001):.3f}"
        expr = f"if(lt(t,{t1:.3f}),{seg},{expr})"
    return expr.replace(",", "\\,")
