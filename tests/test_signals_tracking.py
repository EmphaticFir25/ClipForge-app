"""Signals, crop expressions and the render path. Runs without a model or GPU."""
import importlib.util
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from clipforge.config import Settings
from clipforge.moments import apply_signals
from clipforge.render import render_clip
from clipforge.signals import (
    audio_loudness_per_second,
    chat_activity_per_second,
    window_mean,
    zscore,
)
from clipforge.tracking import crop_keyframes, crop_x_expr, detect_face_centers


def _write_wav(path: Path, seconds_loud: list[int], total: int, rate: int = 16000) -> None:
    import array
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        for sec in range(total):
            amp = 12000 if sec in seconds_loud else 50
            samples = array.array("h", [amp if (i // 8) % 2 else -amp for i in range(rate)])
            w.writeframes(samples.tobytes())


def test_zscore_basic():
    z = zscore([1.0, 2.0, 3.0])
    assert abs(sum(z)) < 1e-9
    assert zscore([5.0, 5.0]) == [0.0, 0.0]
    assert zscore([]) == []


def test_window_mean_bounds():
    series = [0.0, 1.0, 2.0, 3.0]
    assert window_mean(series, 1, 3) == 1.5
    assert window_mean(series, 10, 20) == 0.0 or window_mean(series, 10, 20) == 0.0
    assert window_mean([], 0, 5) == 0.0


def test_audio_loudness_finds_the_loud_second(tmp_path):
    wav = tmp_path / "audio.wav"
    _write_wav(wav, seconds_loud=[2], total=5)
    z = audio_loudness_per_second(wav)
    assert len(z) == 5
    assert z[2] == max(z)
    assert (tmp_path / "audio_energy.json").exists()  # cached


def test_chat_activity_spikes(tmp_path):
    import json
    chat = tmp_path / "chat.json"
    items = [{"t": 30.2}] * 20 + [{"t": 5.0}]
    chat.write_text(json.dumps(items))
    z = chat_activity_per_second(chat, duration_s=60)
    assert len(z) == 60
    assert z[30] == max(z)


def test_apply_signals_blends_and_renormalises():
    base = [{"start": 0, "end": 20, "score": 60}]
    none = apply_signals([dict(c) for c in base], None, None)
    assert none[0]["score"] == 60  # no signals: unchanged

    loud = apply_signals([dict(c) for c in base], [3.0] * 30, None)[0]
    quiet = apply_signals([dict(c) for c in base], [-3.0] * 30, None)[0]
    assert loud["score"] > 60 > quiet["score"]
    assert loud["llm_score"] == 60
    assert loud["audio_z"] == 3.0 and loud["chat_z"] is None


def test_crop_keyframes_smooth_and_clamp():
    # Face at the far right of a 1920x1080 source
    centers = [(0.0, 0.95), (0.5, 0.95), (1.0, 0.95)]
    keys = crop_keyframes(centers, 1920, 1080)
    crop_w = 1080 * 9 / 16
    assert all(0 <= x <= 1920 - crop_w for _, x in keys)
    assert keys[0][1] <= 1920 - crop_w + 1e-6


def test_crop_keyframes_empty_means_centred():
    assert crop_keyframes([], 1920, 1080) == []
    assert crop_x_expr([]) is None


def test_crop_x_expr_structure():
    expr = crop_x_expr([(0.0, 100.0), (2.0, 400.0), (4.0, 400.0)])
    assert expr.count("if(") == 2
    assert "\\," in expr  # commas escaped for the filtergraph
    assert crop_x_expr([(3.0, 250.0)]) == "250.0"  # single key holds


def test_detect_face_centers_without_mediapipe_returns_empty():
    if importlib.util.find_spec("mediapipe") is not None:
        pytest.skip("mediapipe installed; this test checks the fallback path")
    assert detect_face_centers(Path("missing.mp4"), 0, 10, Settings()) == []


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_render_with_animated_crop(tmp_path):
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30",
         "-t", "6", "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True,
    )
    keys = [(0.0, 0.0), (3.0, 500.0)]
    out = tmp_path / "out" / "clip.mp4"
    render_clip(src, 0.5, 4.5, out, Settings(), crop_x=crop_x_expr(keys))
    assert out.exists() and out.stat().st_size > 0
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert probe == "1080,1920"
