"""Aspect-ratio variants and caption styles for re-rendered clips."""
import json
import shutil
import subprocess

import pytest

from clipforge.captions import CAPTION_STYLES, build_ass
from clipforge.config import Settings
from clipforge.render import ASPECTS, output_size, render_clip


def test_output_sizes_for_each_aspect():
    s = Settings()
    assert output_size("9:16", s) == (1080, 1920)
    assert output_size("1:1", s) == (1080, 1080)
    assert output_size("16:9", s) == (1920, 1080)
    with pytest.raises(ValueError):
        output_size("4:3", s)


def test_caption_styles_change_the_spoken_colour_only():
    words = [{"word": "hello", "start": 0.0, "end": 0.5}, {"word": "there", "start": 0.5, "end": 1.0}]
    s = Settings()
    highlight = build_ass(words, 0.0, 2.0, s, style="highlight")
    plain = build_ass(words, 0.0, 2.0, s, style="plain")
    assert CAPTION_STYLES["highlight"] in highlight
    assert CAPTION_STYLES["plain"] in plain and CAPTION_STYLES["highlight"] not in plain
    assert build_ass(words, 0.0, 2.0, s, style="unknown") == highlight  # unknown falls back to default


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
@pytest.mark.parametrize("aspect,expected", [("1:1", "1080,1080"), ("16:9", "1920,1080"), ("9:16", "1080,1920")])
def test_rendered_variant_has_requested_shape(tmp_path, aspect, expected):
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30",
                    "-t", "4", "-pix_fmt", "yuv420p", str(src)], check=True, capture_output=True)
    out = tmp_path / f"clip_{aspect.replace(':', 'x')}.mp4"
    render_clip(src, 0.5, 3.5, out, Settings(), aspect=aspect)
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert probe == expected
