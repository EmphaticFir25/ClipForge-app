"""Find viral-worthy moments: windowing, LLM scoring, validation and de-duplication."""
from __future__ import annotations

import bisect
from typing import Callable

from .config import Settings
from .signals import window_mean

AskFn = Callable[[str, Settings], list[dict]]


def _to_score(z: float) -> float:
    """Map a z-score to 0..100 with 50 as the stream's average."""
    return max(0.0, min(100.0, 50.0 + 15.0 * z))


def apply_signals(
    candidates: list[dict],
    audio_z: list[float] | None,
    chat_z: list[float] | None,
) -> list[dict]:
    """Blend the LLM score with audio loudness and chat activity.

    Weights: LLM 0.75, audio 0.15, chat 0.10. A missing signal is dropped and
    the remaining weights are renormalised, so with no signals the LLM score is unchanged.
    """
    for c in candidates:
        c["llm_score"] = c["score"]
        parts = [(c["score"], 0.75)]
        a = window_mean(audio_z, c["start"], c["end"]) if audio_z else None
        ch = window_mean(chat_z, c["start"], c["end"]) if chat_z else None
        if a is not None:
            parts.append((_to_score(a), 0.15))
        if ch is not None:
            parts.append((_to_score(ch), 0.10))
        total = sum(w for _, w in parts)
        c["score"] = round(sum(v * w for v, w in parts) / total, 1)
        c["audio_z"] = None if a is None else round(a, 2)
        c["chat_z"] = None if ch is None else round(ch, 2)
    return candidates


def make_windows(segments: list[dict], s: Settings) -> list[tuple[float, float, list[dict]]]:
    """Slide a window over the segments. Returns (start, end, segments_in_window)."""
    if not segments:
        return []
    starts = [seg["start"] for seg in segments]
    total_end = segments[-1]["end"]
    windows = []
    t = 0.0
    while t < total_end:
        lo = bisect.bisect_left(starts, t)
        hi = bisect.bisect_left(starts, t + s.window_s)
        chunk = segments[lo:hi]
        if chunk:
            windows.append((t, t + s.window_s, chunk))
        t += s.stride_s
    return windows


def format_window(chunk: list[dict]) -> str:
    lines = [f"[{seg['start']:.1f}-{seg['end']:.1f}] {seg['text']}" for seg in chunk]
    return "Transcript excerpt:\n" + "\n".join(lines)


def validate(candidate: dict, s: Settings, segments: list[dict]) -> dict | None:
    """Clean up one model suggestion. Returns None if it can't be used."""
    try:
        start = float(candidate["start"])
        end = float(candidate["end"])
    except (KeyError, TypeError, ValueError):
        return None
    if end <= start:
        return None

    # Clamp duration into the allowed range instead of throwing the clip away
    duration = min(max(end - start, s.min_clip_s), s.max_clip_s)
    end = start + duration

    # Snap the start and end to segment boundaries so cuts don't land mid-word
    start = _snap_start(start, segments)
    end = _snap_end(end, segments)
    if end - start < s.min_clip_s:
        end = start + s.min_clip_s

    try:
        score = max(0.0, min(100.0, float(candidate.get("score", 0))))
    except (TypeError, ValueError):
        score = 0.0

    hashtags = [h for h in candidate.get("hashtags", []) if isinstance(h, str)]
    return {
        "start": round(start, 2),
        "end": round(end, 2),
        "score": round(score, 1),
        "title": str(candidate.get("title", "")).strip()[:100],
        "hook": str(candidate.get("hook", "")).strip(),
        "caption": str(candidate.get("caption", "")).strip(),
        "hashtags": hashtags[:8],
        "reason": str(candidate.get("reason", "")).strip(),
    }


def _snap_start(t: float, segments: list[dict]) -> float:
    # The segment that contains t, or the next one after it
    for seg in segments:
        if seg["start"] <= t < seg["end"]:
            return seg["start"]
        if seg["start"] > t:
            return seg["start"]
    return t


def _snap_end(t: float, segments: list[dict]) -> float:
    best = t
    for seg in segments:
        if seg["end"] >= t:
            return seg["end"] if seg["end"] - t < 3 else t
        best = seg["end"]
    return best


def overlap_ratio(a: dict, b: dict) -> float:
    """Intersection over union of two clips' time ranges."""
    inter = min(a["end"], b["end"]) - max(a["start"], b["start"])
    if inter <= 0:
        return 0.0
    union = max(a["end"], b["end"]) - min(a["start"], b["start"])
    return inter / union


def select(candidates: list[dict], s: Settings) -> list[dict]:
    """Highest score first; drop anything that overlaps an already-kept clip."""
    kept: list[dict] = []
    for c in sorted(candidates, key=lambda c: c["score"], reverse=True):
        if all(overlap_ratio(c, k) <= s.overlap_iou for k in kept):
            kept.append(c)
    return kept


def find_moments(
    transcript: dict,
    s: Settings,
    ask: AskFn | None = None,
    on_window: Callable[[int, int], None] | None = None,
    audio_z: list[float] | None = None,
    chat_z: list[float] | None = None,
) -> list[dict]:
    """Full detection pass. `ask` is injectable so tests can run without a model."""
    if ask is None:
        from .llm import ask_for_clips
        ask = ask_for_clips

    segments = transcript["segments"]
    windows = make_windows(segments, s)
    candidates: list[dict] = []
    failures = 0

    for i, (w_start, w_end, chunk) in enumerate(windows, 1):
        if on_window:
            on_window(i, len(windows))
        try:
            raw = ask(format_window(chunk), s)
        except Exception:
            # A bad JSON reply or a dropped connection shouldn't kill a long VOD
            failures += 1
            continue
        for item in raw:
            # Windows overlap, so a clip can start before its window does.
            # select() de-duplicates across windows, so no per-window filter here.
            clean = validate(item, s, segments)
            if clean:
                candidates.append(clean)

    if failures and failures == len(windows):
        raise RuntimeError("every window failed. Is LM Studio's server running?")
    return select(apply_signals(candidates, audio_z, chat_z), s)
