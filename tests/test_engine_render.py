"""The worker's re-render path, with real ffmpeg: trim, word edit and aspect are all applied."""
import json
import shutil
import subprocess

import pytest

from webapp.worker import engine_render


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_rerender_applies_trim_word_edit_and_square_aspect(tmp_path):
    source = tmp_path / "source.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30",
                    "-t", "8", "-pix_fmt", "yuv420p", str(source)], check=True, capture_output=True)
    job_dir = tmp_path / "jobs" / "j1"
    job_dir.mkdir(parents=True)
    (job_dir / "clips").mkdir()
    words = [{"word": w, "start": 1.0 + i, "end": 1.5 + i} for i, w in enumerate(["so", "there", "we", "go"])]
    (job_dir / "transcript.json").write_text(json.dumps({"words": words, "segments": []}), encoding="utf-8")

    spec = {"start": 1.0, "end": 5.0, "caption_style": "signal", "word_edits": {1: "their"}}
    engine_render(tmp_path)(job_dir, source, 1, "1:1", spec, False)

    out = job_dir / "clips" / "clip_01__1x1.mp4"
    assert out.exists()
    ass = (job_dir / "clip_01__1x1.ass").read_text(encoding="utf-8")
    assert "PlayResX: 1080" in ass and "PlayResY: 1080" in ass
    assert "their" in ass and "there" not in ass
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert probe == "1080,1080"
