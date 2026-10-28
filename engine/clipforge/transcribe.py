"""Transcription with whisper.cpp, producing word-level timestamps.

Output format (transcript.json):
{
  "words":    [{"start": 12.31, "end": 12.62, "word": "hello"}, ...],
  "segments": [{"start": 12.31, "end": 15.04, "text": "hello there chat"}, ...]
}
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .config import Settings

PAUSE_BREAK_S = 0.8     # a pause longer than this starts a new segment
MAX_WORDS_PER_SEGMENT = 30


def transcribe(wav: Path, out_json: Path, s: Settings) -> dict:
    """Run whisper.cpp once per job. Re-uses the existing transcript if present."""
    if out_json.exists():
        return load_transcript(out_json)

    # whisper.cpp writes <prefix>.json when -ojf (full JSON) and -of <prefix> are given.
    prefix = out_json.with_suffix("")
    subprocess.run(
        [
            s.whisper_cli,
            "-m", s.whisper_model,
            "-f", str(wav),
            "-ojf",               # full JSON, includes per-token offsets
            "-of", str(prefix),
            "-ml", "1",           # one token group per output segment...
            "-sow",               # ...split on word boundaries
        ],
        check=True,
    )
    raw = json.loads(out_json.read_text(encoding="utf-8"))
    transcript = normalize_whisper_json(raw)
    out_json.write_text(json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
    return transcript


def normalize_whisper_json(raw: dict) -> dict:
    """Convert whisper.cpp's JSON into the words/segments format.

    The offsets are in milliseconds. Special tokens such as [_BEG_] are dropped.
    Check this against the JSON your whisper.cpp build produces; field names
    have changed between releases.
    """
    words = []
    for item in raw.get("transcription", []):
        text = item.get("text", "").strip()
        if not text or text.startswith("[") or text.startswith("<"):
            continue
        off = item.get("offsets", {})
        words.append({
            "start": round(off["from"] / 1000.0, 3),
            "end": round(off["to"] / 1000.0, 3),
            "word": text,
        })
    return {"words": words, "segments": group_segments(words)}


def group_segments(words: list[dict]) -> list[dict]:
    """Group words into readable segments, splitting on pauses or length."""
    segments: list[dict] = []
    current: list[dict] = []
    for w in words:
        if current:
            gap = w["start"] - current[-1]["end"]
            if gap > PAUSE_BREAK_S or len(current) >= MAX_WORDS_PER_SEGMENT:
                segments.append(_segment(current))
                current = []
        current.append(w)
    if current:
        segments.append(_segment(current))
    return segments


def _segment(words: list[dict]) -> dict:
    return {
        "start": words[0]["start"],
        "end": words[-1]["end"],
        "text": " ".join(w["word"] for w in words),
    }


def load_transcript(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
