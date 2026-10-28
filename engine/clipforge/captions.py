"""Word-highlighted captions as an ASS subtitle file (burned in by ffmpeg)."""
from __future__ import annotations

from .config import Settings

MAX_WORDS_PER_LINE = 4
MAX_LINE_GAP_S = 0.6

# ASS colours are &HAABBGGRR. Unspoken words are white, the sung word turns yellow.
UNSPOKEN = "&H00FFFFFF"
SPOKEN = "&H0000FFFF"


def ass_time(t: float) -> str:
    """Seconds to H:MM:SS.cc"""
    if t < 0:
        t = 0.0
    cs = int(round(t * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    sec, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{sec:02d}.{cs:02d}"


def _clean(word: str) -> str:
    # Braces start override tags in ASS, so strip them from the text
    return word.replace("{", "").replace("}", "").replace("\\", "")


def group_lines(words: list[dict]) -> list[list[dict]]:
    lines: list[list[dict]] = []
    current: list[dict] = []
    for w in words:
        if current:
            gap = w["start"] - current[-1]["end"]
            if gap > MAX_LINE_GAP_S or len(current) >= MAX_WORDS_PER_LINE:
                lines.append(current)
                current = []
        current.append(w)
    if current:
        lines.append(current)
    return lines


def build_ass(words: list[dict], clip_start: float, clip_end: float, s: Settings) -> str:
    """Build an ASS file for one clip. Times are shifted so the clip starts at 0."""
    in_clip = [w for w in words if w["end"] > clip_start and w["start"] < clip_end]

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {s.out_w}
PlayResY: {s.out_h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,96,{SPOKEN},{UNSPOKEN},&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,0,2,80,80,520,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events = []
    for line in group_lines(in_clip):
        line_start = max(line[0]["start"], clip_start) - clip_start
        line_end = min(line[-1]["end"], clip_end) - clip_start
        parts = []
        for w in line:
            # \k is the karaoke duration in centiseconds
            cs = max(1, int(round((w["end"] - w["start"]) * 100)))
            parts.append(f"{{\\k{cs}}}{_clean(w['word'])}")
        text = " ".join(parts)
        events.append(
            f"Dialogue: 0,{ass_time(line_start)},{ass_time(line_end)},Default,,0,0,0,,{text}"
        )

    return header + "\n".join(events) + "\n"
