"""Retention purges, the approval-gated dispatcher, and face-tracked square crops."""
import io
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from clipforge.tracking import crop_keyframes, crop_x_expr
from webapp import service as svc
from webapp.service import Service
from webapp.store import Store


@pytest.fixture
def service(tmp_path):
    return Service(Store(tmp_path / "app.sqlite3"), tmp_path, probe_minutes=lambda p: 5.0)


_counter = iter(range(10_000))


def _finished_job(service, plan, finished_at):
    key = service.create_user(f"{plan}{next(_counter)}@example.com", plan=plan)
    user = service.authenticate(key)
    job = service.create_job(user, "s.mp4", io.BytesIO(b"x" * 50), top=1, rights_confirmed=True)
    job_dir = service.job_dir(job["id"])
    (job_dir / "clips").mkdir(exist_ok=True)
    (job_dir / "clips" / "clip_01.mp4").write_bytes(b"mp4")
    service.store.finish_job(job["id"], 1, finished_at.isoformat(timespec="seconds"))
    return user, job["id"], job_dir


def test_purge_removes_clips_after_the_plans_retention(service):
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    old_user, old_id, old_dir = _finished_job(service, "free", now - timedelta(days=8))   # free keeps 7 days
    new_user, new_id, new_dir = _finished_job(service, "free", now - timedelta(days=2))
    assert service.purge_expired(now) == 1
    assert not old_dir.exists() and new_dir.exists()
    assert service.get_job(old_user, old_id)["purged_at"] is not None
    assert service.clip_list(old_user, old_id) == []
    assert service.variant_file(old_user, old_id, 1) is None
    assert service.clip_file(new_user, new_id, 1) is not None


def test_pro_keeps_clips_longer_than_free(service):
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    _, _, pro_dir = _finished_job(service, "pro", now - timedelta(days=30))  # pro keeps 90 days
    assert service.purge_expired(now) == 0
    assert pro_dir.exists()


def _scheduled(service, platform, when):
    key = service.create_user(f"p{platform}@example.com", plan="pro")
    user = service.authenticate(key)
    job = service.create_job(user, "s.mp4", io.BytesIO(b"x" * 50), top=1, rights_confirmed=True)
    service.store.finish_job(job["id"], 1, "2026-10-28T09:00:00+00:00")
    row = {"id": f"s-{platform}", "user_id": user["id"], "job_id": job["id"], "clip_n": 1,
           "platform": platform, "scheduled_at": when.isoformat(timespec="minutes"),
           "status": "scheduled", "created_at": "2026-10-28T09:00:00+00:00", "error": None}
    service.store.insert_schedule(row)
    return row["id"]


def _status(service, sid):
    return next(r for r in service.store._run("SELECT * FROM schedules") if r["id"] == sid)


def test_unapproved_platform_never_posts(service, monkeypatch):
    monkeypatch.setattr(svc, "APPROVED_PLATFORMS", set())
    now = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    sid = _scheduled(service, "youtube", now - timedelta(minutes=5))
    sent = []
    counts = service.dispatch_due_schedules(now, publish=lambda row: sent.append(row))
    assert counts["waiting_approval"] == 1 and sent == []
    assert _status(service, sid)["status"] == "waiting_approval"


def test_waiting_post_goes_out_once_the_platform_is_approved(service, monkeypatch):
    monkeypatch.setattr(svc, "APPROVED_PLATFORMS", set())
    now = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    sid = _scheduled(service, "tiktok", now - timedelta(minutes=5))
    service.dispatch_due_schedules(now)
    monkeypatch.setattr(svc, "APPROVED_PLATFORMS", {"tiktok"})
    sent = []
    service.dispatch_due_schedules(now, publish=lambda row: sent.append(row["id"]))
    assert sent == [sid] and _status(service, sid)["status"] == "posted"


def test_approved_platform_without_connected_account_says_what_to_do(service, monkeypatch):
    monkeypatch.setattr(svc, "APPROVED_PLATFORMS", {"youtube"})
    now = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    sid = _scheduled(service, "youtube", now - timedelta(minutes=1))
    service.dispatch_due_schedules(now)
    row = _status(service, sid)
    assert row["status"] == "failed" and "connect" in row["error"].lower()


def test_future_posts_are_left_alone(service, monkeypatch):
    monkeypatch.setattr(svc, "APPROVED_PLATFORMS", {"youtube"})
    now = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    sid = _scheduled(service, "youtube", now + timedelta(hours=2))
    service.dispatch_due_schedules(now)
    assert _status(service, sid)["status"] == "scheduled"


def test_square_crop_keyframes_stay_inside_the_frame():
    # A 1920x1080 source: the square crop is 1080 wide, so the left edge ranges over 0..840
    centers = [(0.0, 0.05), (1.0, 0.5), (2.0, 0.98)]
    keys = crop_keyframes(centers, 1920, 1080, crop_w=1080)
    xs = [x for _, x in keys]
    assert min(xs) >= 0 and max(xs) <= 1920 - 1080
    assert xs[-1] > xs[0]  # it pans toward the speaker
    assert crop_x_expr(keys) is not None


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_square_render_accepts_a_tracked_crop(tmp_path):
    from clipforge.config import Settings
    from clipforge.render import render_clip
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30",
                    "-t", "3", "-pix_fmt", "yuv420p", str(src)], check=True, capture_output=True)
    out = tmp_path / "sq.mp4"
    render_clip(src, 0.2, 2.2, out, Settings(), crop_x="100", aspect="1:1")
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", str(out)],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert probe == "1080,1080"
