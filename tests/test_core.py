"""Tests that run without ffmpeg, whisper.cpp or LM Studio."""
from pathlib import Path

from clipforge.captions import ass_time, build_ass, group_lines
from clipforge.config import Settings
from clipforge.llm import parse_json_reply
from clipforge.moments import find_moments, make_windows, overlap_ratio, select, validate
from clipforge.transcribe import group_segments, normalize_whisper_json


def _settings(**kw) -> Settings:
    s = Settings(work_dir=Path("unused"))
    for k, v in kw.items():
        setattr(s, k, v)
    return s


SEGMENTS = [
    {"start": 0.0, "end": 10.0, "text": "chat look at this"},
    {"start": 10.0, "end": 25.0, "text": "I just lost the round to a one tap"},
    {"start": 25.0, "end": 40.0, "text": "and I am not okay"},
    {"start": 40.0, "end": 200.0, "text": "okay so back to the build"},
]


def test_ass_time_formats_centiseconds():
    assert ass_time(0) == "0:00:00.00"
    assert ass_time(65.5) == "0:01:05.50"
    assert ass_time(3723.04) == "1:02:03.04"


def test_group_lines_breaks_on_gap_and_count():
    words = [
        {"start": 0.0, "end": 0.3, "word": "a"},
        {"start": 0.3, "end": 0.6, "word": "b"},
        {"start": 2.0, "end": 2.3, "word": "c"},  # 1.4s gap -> new line
    ]
    lines = group_lines(words)
    assert [len(l) for l in lines] == [2, 1]


def test_build_ass_shifts_times_and_escapes_text():
    words = [
        {"start": 100.0, "end": 100.5, "word": "hello"},
        {"start": 100.5, "end": 101.0, "word": "{evil}"},
        {"start": 300.0, "end": 300.5, "word": "outside"},
    ]
    ass = build_ass(words, 100.0, 110.0, _settings())
    assert "Dialogue: 0,0:00:00.00,0:00:01.00" in ass
    assert "outside" not in ass
    assert "{evil}" not in ass
    assert "evil" in ass
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass


def test_validate_clamps_duration_and_snaps_to_segments():
    s = _settings()
    c = validate({"start": 11.0, "end": 12.0, "score": 150, "title": "x"}, s, SEGMENTS)
    assert c is not None
    assert c["end"] - c["start"] >= s.min_clip_s
    assert c["score"] == 100.0
    assert c["start"] == 10.0  # snapped to the segment containing 11.0


def test_validate_rejects_bad_input():
    s = _settings()
    assert validate({"start": 50, "end": 40}, s, SEGMENTS) is None
    assert validate({"score": 80}, s, SEGMENTS) is None
    assert validate({"start": "abc", "end": 40}, s, SEGMENTS) is None


def test_select_drops_overlaps_keeps_best():
    s = _settings()
    a = {"start": 0, "end": 30, "score": 90}
    b = {"start": 2, "end": 32, "score": 80}   # overlaps a heavily
    c = {"start": 100, "end": 120, "score": 70}
    kept = select([b, a, c], s)
    assert kept == [a, c]
    assert overlap_ratio(a, b) > s.overlap_iou


def test_windows_cover_transcript():
    s = _settings(window_s=60, stride_s=30)
    windows = make_windows(SEGMENTS, s)
    assert windows[0][0] == 0.0
    assert windows[-1][2][-1] is SEGMENTS[-1]


def test_find_moments_with_fake_model():
    s = _settings(window_s=60, stride_s=30)
    calls = []

    def fake_ask(text, _s):
        calls.append(text)
        return [{"start": 10, "end": 30, "score": 85, "title": "One tap", "hook": "I lost", "hashtags": ["#fps"]}]

    moments = find_moments({"segments": SEGMENTS}, s, ask=fake_ask)
    assert calls and "Transcript excerpt" in calls[0]
    assert len(moments) == 1
    assert moments[0]["title"] == "One tap"


def test_find_moments_survives_one_bad_reply():
    s = _settings(window_s=60, stride_s=30)
    state = {"n": 0}

    def flaky(text, _s):
        state["n"] += 1
        if state["n"] == 1:
            raise ValueError("bad json")
        return [{"start": 10, "end": 30, "score": 50, "title": "ok"}]

    assert len(find_moments({"segments": SEGMENTS}, s, ask=flaky)) == 1


def test_parse_json_reply_handles_fences_and_prose():
    reply = 'Sure!\n```json\n{"clips": [{"start": 1, "end": 2}]}\n```'
    assert parse_json_reply(reply)["clips"][0]["end"] == 2


def test_normalize_whisper_json_drops_special_tokens():
    raw = {"transcription": [
        {"text": "[_BEG_]", "offsets": {"from": 0, "to": 0}},
        {"text": " hello", "offsets": {"from": 500, "to": 900}},
        {"text": " world", "offsets": {"from": 900, "to": 1300}},
        {"text": " later", "offsets": {"from": 5000, "to": 5400}},
    ]}
    out = normalize_whisper_json(raw)
    assert [w["word"] for w in out["words"]] == ["hello", "world", "later"]
    assert out["words"][0]["start"] == 0.5
    assert len(out["segments"]) == 2  # 4s pause splits the segments
