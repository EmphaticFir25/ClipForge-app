"""Extra scoring signals beyond the LLM: audio loudness and chat activity.

Both are turned into per-second series, z-scored over the whole VOD, so a
value of +2 means "two standard deviations louder (or busier) than this stream's usual".
"""
from __future__ import annotations

import array
import json
import math
import sys
import wave
from pathlib import Path


def zscore(values: list[float]) -> list[float]:
    if not values:
        return []
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    std = math.sqrt(var)
    if std < 1e-9:
        return [0.0] * len(values)
    return [(v - mean) / std for v in values]


def audio_loudness_per_second(wav: Path) -> list[float]:
    """RMS loudness for each second of 16-bit mono PCM, z-scored.

    The result is cached next to the wav (audio_energy.json) because a
    multi-hour VOD takes a while to scan.
    """
    cache = wav.with_name("audio_energy.json")
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))

    rms: list[float] = []
    with wave.open(str(wav), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("expected 16-bit PCM audio")
        rate = w.getframerate()
        while True:
            raw = w.readframes(rate)  # one second of mono frames
            if not raw:
                break
            samples = array.array("h")
            samples.frombytes(raw[: len(raw) - len(raw) % 2])
            if sys.byteorder == "big":
                samples.byteswap()
            if not samples:
                break
            rms.append(math.sqrt(sum(x * x for x in samples) / len(samples)))

    z = zscore(rms)
    cache.write_text(json.dumps(z), encoding="utf-8")
    return z


def load_chat_times(chat_file: Path) -> list[float]:
    """Read chat messages from a JSON list like [{"t": 812.4, "text": "..."}].

    `t` is seconds from the start of the VOD. Export your chat to this shape
    (a short script over a chat-replay downloader's output is enough).
    """
    items = json.loads(chat_file.read_text(encoding="utf-8"))
    return [float(item["t"]) for item in items if "t" in item]


def chat_activity_per_second(chat_file: Path, duration_s: int) -> list[float]:
    """Messages per second, smoothed over 5 s and z-scored."""
    counts = [0.0] * max(duration_s, 1)
    for t in load_chat_times(chat_file):
        i = int(t)
        if 0 <= i < len(counts):
            counts[i] += 1
    smoothed = []
    for i in range(len(counts)):
        lo, hi = max(0, i - 2), min(len(counts), i + 3)
        smoothed.append(sum(counts[lo:hi]) / (hi - lo))
    return zscore(smoothed)


def window_mean(series: list[float], start: float, end: float) -> float:
    if not series:
        return 0.0
    lo = max(0, int(start))
    hi = min(len(series), max(lo + 1, int(math.ceil(end))))
    vals = series[lo:hi]
    return sum(vals) / len(vals) if vals else 0.0
