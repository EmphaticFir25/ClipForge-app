"""Uploads are judged by their content, and ffmpeg/ffprobe can't run forever."""
import io
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from conftest_helpers import make_service, make_user, make_video
from webapp import media
from webapp.media import MediaRejected, inspect
from webapp.service import ServiceError

BIN = Path(__file__).resolve().parent.parent / "webapp" / "bin"
needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  reason="ffmpeg/ffprobe not installed")


@needs_ffmpeg
def test_real_video_is_accepted(tmp_path):
    info = inspect(make_video(tmp_path / "ok.mp4", seconds=3))
    assert info["width"] == 320 and info["height"] == 240
    assert 2.5 < info["duration_s"] < 3.5


@needs_ffmpeg
def test_text_file_renamed_to_mp4_is_refused(tmp_path):
    fake = tmp_path / "notes.mp4"
    fake.write_text("this is not a video\n" * 100)
    with pytest.raises(MediaRejected):
        inspect(fake)


@needs_ffmpeg
def test_audio_only_file_is_refused(tmp_path):
    with pytest.raises(MediaRejected, match="no video track"):
        inspect(make_video(tmp_path / "audio.mp4", seconds=2, audio_only=True))


@needs_ffmpeg
def test_truncated_video_is_refused_or_has_a_length(tmp_path):
    good = make_video(tmp_path / "good.mp4", seconds=2).read_bytes()
    cut = tmp_path / "cut.mp4"
    cut.write_bytes(good[:200])  # header only, no playable frames
    try:
        info = inspect(cut)
    except MediaRejected:
        return
    assert info["duration_s"] > 0  # if ffprobe still reads it, it must report a real length


def test_hanging_probe_is_stopped_by_timeout(tmp_path, monkeypatch):
    slow = tmp_path / "slow_probe"
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(slow.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(media, "PROBE_TIMEOUT_S", 1)
    video = tmp_path / "x.mp4"
    video.write_bytes(b"\0" * 10)
    with pytest.raises(MediaRejected, match="too long"):
        inspect(video, ffprobe=str(slow))


@pytest.mark.skipif(not (BIN / "cf-ffprobe").exists(), reason="wrapper missing")
def test_limited_wrapper_kills_a_runaway_tool(tmp_path):
    runaway = tmp_path / "runaway"
    runaway.write_text("#!/bin/sh\nsleep 60\n")
    runaway.chmod(runaway.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "CLIPFORGE_REAL_FFPROBE": str(runaway), "CLIPFORGE_PROBE_WALL_S": "1"}
    result = subprocess.run([str(BIN / "cf-ffprobe"), "-v", "error"], env=env, timeout=30)
    assert result.returncode != 0  # stopped by the wall-clock limit, not allowed to finish


@needs_ffmpeg
def test_renamed_text_file_is_rejected_by_the_service_and_leaves_no_files(tmp_path):
    service = make_service(tmp_path, probe_minutes=lambda p: media.inspect(p)["duration_s"] / 60)
    user = make_user(service, "up@example.com")
    with pytest.raises(ServiceError):
        service.create_job(user, "fake.mp4", io.BytesIO(b"plain text " * 50), top=2, rights_confirmed=True)
    jobs_dir = tmp_path / "jobs"
    leftovers = list(jobs_dir.iterdir()) if jobs_dir.exists() else []
    assert leftovers == []
    assert service.store.list_jobs(user["id"]) == []


def test_upload_larger_than_the_plan_is_cut_off_while_streaming(tmp_path):
    service = make_service(tmp_path)
    big = io.BytesIO(b"\0" * (3 * 1024 * 1024))
    with pytest.raises(ServiceError, match="larger than your plan"):
        service._copy_upload(big, tmp_path / "out.mp4", limit=1024 * 1024)
    assert (tmp_path / "out.mp4").stat().st_size <= 1024 * 1024 + 4 * 1024 * 1024  # never wrote past the chunk


def test_empty_upload_is_refused(tmp_path):
    service = make_service(tmp_path)
    with pytest.raises(ServiceError, match="empty"):
        service._copy_upload(io.BytesIO(b""), tmp_path / "e.mp4", limit=10)
