"""Tests for accounts, plans, uploads, ownership and the worker. No FastAPI or GPU needed."""
import io
import shutil
import subprocess
from pathlib import Path

import pytest

from webapp import service as svc
from webapp.service import QuotaExceeded, Service, ServiceError, Worker
from webapp.store import Store


def _minutes(_path):
    return 10.0  # every test video is 10 minutes long


@pytest.fixture
def app_service(tmp_path):
    store = Store(tmp_path / "app.sqlite3")
    return Service(store, tmp_path, probe_minutes=_minutes)


def _user(service, email="a@example.com", plan="free"):
    key = service.create_user(email, plan=plan)
    return service.authenticate(key), key


def _upload(service, user, name="stream.mp4", data=b"x" * 1000, top=3, rights=True):
    return service.create_job(user, name, io.BytesIO(data), top=top, rights_confirmed=rights)


def test_api_key_authenticates_and_is_hashed(app_service):
    _, key = _user(app_service)
    assert app_service.authenticate(key)["email"] == "a@example.com"
    assert app_service.authenticate("cf_wrong") is None
    assert app_service.authenticate(None) is None
    assert key not in app_service.store.user_by_email("a@example.com")["key_hash"]


def test_duplicate_email_bad_plan_and_bad_email_rejected(app_service):
    _user(app_service)
    with pytest.raises(ServiceError):
        app_service.create_user("A@example.com")
    with pytest.raises(ServiceError):
        app_service.create_user("b@example.com", plan="platinum")
    with pytest.raises(ServiceError):
        app_service.create_user("not-an-email")


def test_upload_requires_rights_confirmation(app_service):
    user, _ = _user(app_service)
    with pytest.raises(ServiceError, match="rights"):
        _upload(app_service, user, rights=False)


def test_upload_rejects_unknown_types_and_empty_files(app_service):
    user, _ = _user(app_service)
    with pytest.raises(ServiceError, match="unsupported"):
        _upload(app_service, user, name="payload.exe")
    with pytest.raises(ServiceError, match="empty"):
        _upload(app_service, user, data=b"")


def test_unreadable_video_is_rejected_and_cleaned_up(app_service):
    user, _ = _user(app_service)

    def broken(_):
        raise RuntimeError("ffprobe failed")

    app_service.probe_minutes = broken
    with pytest.raises(ServiceError, match="could not read"):
        _upload(app_service, user)
    assert list((app_service.data_dir / "jobs").glob("*")) == []


def test_upload_size_limit_removes_partial_file(app_service, monkeypatch):
    user, _ = _user(app_service)
    monkeypatch.setitem(svc.PLANS["free"], "max_upload_bytes", 500)
    with pytest.raises(ServiceError, match="larger"):
        _upload(app_service, user, data=b"x" * 2000)
    assert list((app_service.data_dir / "jobs").glob("*")) == []


def test_job_is_queued_with_minutes_recorded(app_service):
    user, _ = _user(app_service)
    job = _upload(app_service, user, top=3)
    assert job["status"] == "queued"
    assert job["source_minutes"] == 10.0
    stored = app_service.store.get_job(job["id"])
    assert Path(stored["source_path"]).read_bytes() == b"x" * 1000


def test_top_is_capped_by_plan(app_service):
    user, _ = _user(app_service, plan="free")
    job = _upload(app_service, user, top=999)
    assert app_service.store.get_job(job["id"])["top"] == svc.PLANS["free"]["max_clips"]


def test_free_plan_limit_is_in_minutes_not_jobs(app_service):
    user, _ = _user(app_service, plan="free")  # 60 minutes a month
    for _ in range(6):                          # six 10-minute videos = 60 minutes
        _upload(app_service, user)
    with pytest.raises(QuotaExceeded, match="minutes left"):
        _upload(app_service, user)


def test_quota_rejection_does_not_leave_files_behind(app_service):
    user, _ = _user(app_service, plan="free")
    for _ in range(6):
        _upload(app_service, user)
    before = len(list((app_service.data_dir / "jobs").glob("*")))
    with pytest.raises(QuotaExceeded):
        _upload(app_service, user)
    assert len(list((app_service.data_dir / "jobs").glob("*"))) == before


def test_longer_video_than_remaining_quota_is_rejected(app_service):
    user, _ = _user(app_service, plan="free")
    app_service.probe_minutes = lambda p: 90.0
    with pytest.raises(QuotaExceeded):
        _upload(app_service, user)


def test_usage_reports_plan_and_remaining_minutes(app_service):
    user, _ = _user(app_service, plan="free")
    _upload(app_service, user)
    usage = app_service.usage(user)
    assert usage["plan"] == "free"
    assert usage["minutes_used"] == 10.0
    assert usage["minutes_limit"] == 60
    assert usage["watermark"] is True
    assert usage["publishing"] is False


def test_paid_plans_have_no_watermark_and_publishing_only_on_pro(app_service):
    assert svc.PLANS["creator"]["watermark"] is False
    assert svc.PLANS["pro"]["publishing"] is True
    assert svc.PLANS["creator"]["publishing"] is False


def test_users_cannot_see_each_others_jobs(app_service):
    alice, _ = _user(app_service, "alice@example.com")
    bob, _ = _user(app_service, "bob@example.com")
    job = _upload(app_service, alice)
    assert app_service.get_job(alice, job["id"]) is not None
    assert app_service.get_job(bob, job["id"]) is None
    assert app_service.list_jobs(bob) == []


def test_worker_passes_watermark_flag_from_plan(app_service):
    free_user, _ = _user(app_service, "free@example.com", plan="free")
    pro_user, _ = _user(app_service, "pro@example.com", plan="pro")
    _upload(app_service, free_user)
    _upload(app_service, pro_user)
    seen = []

    def pipeline(source, job_dir, top, watermark):
        seen.append(watermark)
        return 0

    worker = Worker(app_service, pipeline)
    worker.run_once()
    worker.run_once()
    assert seen == [True, False]  # the free job is watermarked, the pro job is not


def test_worker_marks_job_done_and_exposes_clips(app_service):
    user, _ = _user(app_service)
    job = _upload(app_service, user, top=2)

    def fake_pipeline(source, job_dir, top, watermark):
        assert source.exists()
        (job_dir / "clips").mkdir(parents=True, exist_ok=True)
        for i in range(1, top + 1):
            (job_dir / "clips" / f"clip_{i:02d}.mp4").write_bytes(b"mp4")
        return top

    assert Worker(app_service, fake_pipeline).run_once() is True
    done = app_service.get_job(user, job["id"])
    assert done["status"] == "done"
    assert done["clips"] == [1, 2]
    assert app_service.clip_file(user, job["id"], 1).read_bytes() == b"mp4"
    assert app_service.clip_file(user, job["id"], 3) is None


def test_worker_records_failures_and_keeps_going(app_service):
    user, _ = _user(app_service)
    bad = _upload(app_service, user)
    good = _upload(app_service, user)

    def pipeline(source, job_dir, top, watermark):
        if job_dir.name == bad["id"]:
            raise RuntimeError("whisper.cpp crashed")
        (job_dir / "clips").mkdir(parents=True, exist_ok=True)
        (job_dir / "clips" / "clip_01.mp4").write_bytes(b"mp4")
        return 1

    worker = Worker(app_service, pipeline)
    assert worker.run_once() and worker.run_once()
    assert app_service.get_job(user, bad["id"])["status"] == "failed"
    assert "whisper.cpp crashed" in app_service.get_job(user, bad["id"])["error"]
    assert app_service.get_job(user, good["id"])["status"] == "done"


def test_worker_idle_returns_false(app_service):
    assert Worker(app_service, lambda *a: 0).run_once() is False


def test_claim_is_exclusive(app_service):
    user, _ = _user(app_service)
    _upload(app_service, user)
    first = app_service.store.claim_next_job("2026-10-28T10:00:00+00:00")
    second = app_service.store.claim_next_job("2026-10-28T10:00:00+00:00")
    assert first is not None and second is None


def test_admin_set_plan(app_service):
    _user(app_service)
    app_service.set_plan("a@example.com", "pro")
    assert app_service.store.user_by_email("a@example.com")["plan"] == "pro"
    with pytest.raises(ServiceError):
        app_service.set_plan("nobody@example.com", "pro")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_watermarked_render_produces_portrait_clip(tmp_path):
    from clipforge.config import Settings
    from clipforge.render import render_clip

    src = tmp_path / "src.mp4"
    logo = tmp_path / "logo.png"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30",
                    "-t", "5", "-pix_fmt", "yuv420p", str(src)], check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=200x80",
                    "-frames:v", "1", str(logo)], check=True, capture_output=True)
    out = tmp_path / "clip.mp4"
    render_clip(src, 0.5, 4.5, out, Settings(), watermark=logo)
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert probe == "1080,1920"
