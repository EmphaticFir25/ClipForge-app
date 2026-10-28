"""Runtime settings. Everything can be overridden with environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    # Where job folders are written
    work_dir: Path = field(default_factory=lambda: Path(_env("CLIPFORGE_WORK", "work")))

    # LM Studio exposes an OpenAI-compatible server (Developer tab, or `lms server start`)
    lmstudio_base_url: str = _env("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
    lmstudio_model: str = _env("LMSTUDIO_MODEL", "gemma-4-12b-it")

    # whisper.cpp (Vulkan build works on the RX 9070 XT)
    whisper_cli: str = _env("WHISPER_CLI", "whisper-cli")
    whisper_model: str = _env("WHISPER_MODEL", "models/ggml-large-v3.bin")

    ffmpeg: str = _env("FFMPEG", "ffmpeg")
    ffprobe: str = _env("FFPROBE", "ffprobe")

    # Platform API credentials (see credentials.example.json). Keep this file private.
    credentials_file: Path = field(default_factory=lambda: Path(_env("CLIPFORGE_CREDENTIALS", "credentials.json")))
    ytdlp: str = _env("YTDLP", "yt-dlp")
    video_encoder: str = _env("VIDEO_ENCODER", "libx264")  # or h264_amf on AMD
    face_tracking: bool = _env("FACE_TRACKING", "1") == "1"  # needs mediapipe + numpy

    # Moment detection
    window_s: float = 90.0     # transcript window sent to the LLM
    stride_s: float = 60.0     # step between windows (overlap = window - stride)
    min_clip_s: float = 15.0
    max_clip_s: float = 75.0
    overlap_iou: float = 0.3   # clips overlapping more than this are merged away
    top_n: int = 10

    # Output format
    out_w: int = 1080
    out_h: int = 1920
