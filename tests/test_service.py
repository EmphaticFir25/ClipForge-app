"""Tests for accounts, quotas, uploads, ownership and the worker. No FastAPI or GPU needed."""
import io
from pathlib import Path

import pytest

from webapp import service as svc
from webapp.service import QuotaExceeded, ServiceError, Service, Worker
from webapp.store import Store


@pytest.fixture
def app_service(tmp_path):
    store = Store(tmp_path / "app.sqlite3")
    return Service(store, tmp_path)


def _upload(service, user, name="stream.mp4", data=b"x" * 1000, top=3, rights=True):
    return service.create_job(user, name, io.BytesIO(data), top=top, rights_confirmed=rights)


def _user(service, email="a@example.com", plan="free"):
    key = service.create_user(email, plan=plan)
    return service.authenticate(key), key


def test_api_key_authenticates_and_is_hashed(app_service):
    user, key = _user(app_service)
    assert app_service.authenticate(key)["email"] == "a@example.com"
    assert app_service.authenticate("cf_wrong") is None
    assert app_service.authenticate(None) is None
    # The database never holds the raw key
    stored = app_service.store.user_by_email("a@example.com")
    assert key not in stored["key_hash"]


def test_duplicate_email_and_bad_plan_rejected(app_service):
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


def test_upload_rejects_unknown_file_types(app_service):
    user, _ = _user(app_service)
    with pytest.raises(ServiceError, match="unsupported"):
        _upload(app_service, user, name="payload.exe")
    with pytest.raises(ServiceError, match="empty"):
        _upload(app_service, user, data=b"")


def test_upload_size_limit_removes_partial_file(app_service, monkeypatch):
    user, _ = _user(app_service)
    monkeypatch.setitem(svc.PLANS["free"], "max_upload_bytes", 500)
    with pytest.raises(ServiceError, match="larger"):
        _upload(app_service, user, data=b"x" * 2000)
    assert list((app_service.data_dir / "jobs").glob("*")) == []


def test_job_is_queued_with_source_saved(app_service):
    user, _ = _user(app_service)
    job = _upload(app_service, user, top=3)
    assert job["status"] == "queued"
    assert job["clips"] == []
    stored = app_service.store.get_job(job["id"])
    assert Path(stored["source_path"]).read_bytes() == b"x" * 1000
    assert stored["top"] == 3


def test_top_is_capped_by_plan(app_service):
    user, _ = _user(app_service, plan="free")
    job = _upload(app_service, user, top=999)
    assert app_service.store.get_job(job["id"])["top"] == svc.PLANS["free"]["max_clips"]


def test_monthly_quota_enforced(app_service):
    user, _ = _user(app_service, plan="free")
    for _ in range(svc.PLANS["free"]["jobs_per_month"]):
        _upload(app_service, user)
    with pytest.raises(QuotaExceeded):
        _upload(app_service, user)


def test_users_cannot_see_each_others_jobs(app_service):
    alice, _ = _user(app_service, "alice@example.com")
    bob, _ = _user(app_service, "bob@example.com")
    job = _upload(app_service, alice)
    assert app_service.get_job(alice, job["id"]) is not None
    assert app_service.get_job(bob, job["id"]) is None  # same answer as "missing"
    assert app_service.list_jobs(bob) == []


def test_worker_marks_job_done_and_exposes_clips(app_service):
    user, _ = _user(app_service)
    job = _upload(app_service, user, top=2)

    def fake_pipeline(source, job_dir, top):
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
    assert app_service.clip_file(user, job["id"], 3) is None  # out of range


def test_worker_records_failures_and_keeps_going(app_service):
    user, _ = _user(app_service)
    bad = _upload(app_service, user)
    good = _upload(app_service, user)

    def pipeline(source, job_dir, top):
        if source.parent.name == bad["id"]:
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
