"""Every lookup by ID must be scoped to the signed-in user.

A foreign ID and a missing ID must get the same answer, so IDs can't be probed.
"""
import io
from datetime import datetime, timedelta, timezone

import pytest

from conftest_helpers import make_job, make_service, make_user
from webapp import service as svc


@pytest.fixture
def two_users(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "APPROVED_PLATFORMS", {"youtube"})
    service = make_service(tmp_path)
    alice = make_user(service, "alice@example.com")
    bob = make_user(service, "bob@example.com")
    job_id = make_job(service, alice)
    return service, alice, bob, job_id


def test_foreign_job_looks_exactly_like_a_missing_one(two_users):
    service, alice, bob, job_id = two_users
    import uuid
    missing = uuid.uuid4().hex
    assert service.get_job(bob, job_id) is None
    assert service.get_job(bob, missing) is None
    assert service.clip_list(bob, job_id) is None
    assert service.clip_list(bob, missing) is None


@pytest.mark.parametrize("call", [
    lambda s, u, j: s.clip_list(u, j),
    lambda s, u, j: s.clip_words(u, j, 1),
    lambda s, u, j: s.clip_file(u, j, 1),
    lambda s, u, j: s.variant_file(u, j, 1, "1:1"),
    lambda s, u, j: s.export_zip(u, j, "9:16"),
    lambda s, u, j: s.update_clip(u, j, 1, {"caption_style": "plain"}),
])
def test_other_user_cannot_read_or_change_a_clip(two_users, call):
    service, alice, bob, job_id = two_users
    from webapp.service import ServiceError
    try:
        result = call(service, bob, job_id)
    except ServiceError:
        result = None
    assert result is None
    assert service.store.get_clip_edit(job_id, 1) is None  # nothing was saved


def test_other_user_cannot_schedule_someone_elses_clip(two_users):
    service, alice, bob, job_id = two_users
    from webapp.service import ServiceError
    when = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    with pytest.raises(ServiceError, match="not found"):
        service.create_schedule(bob, job_id, 1, "youtube", when)
    assert service.list_schedules(bob) == []


def test_other_user_cannot_delete_a_schedule_by_id(two_users):
    service, alice, bob, job_id = two_users
    when = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    row = service.create_schedule(alice, job_id, 1, "youtube", when)
    assert service.delete_schedule(bob, row["id"]) is False
    assert [r["id"] for r in service.list_schedules(alice)] == [row["id"]]
    assert [r["id"] for r in service.list_schedules(bob)] == []


def test_other_user_cannot_list_jobs_or_see_usage_of_others(two_users):
    service, alice, bob, job_id = two_users
    assert service.list_jobs(bob) == []
    assert service.usage(bob)["jobs_this_month"] == 0


def test_api_keys_only_authenticate_their_owner(two_users):
    service, alice, bob, _ = two_users
    alice_key = service.create_user("carol@example.com", plan="free")
    assert service.authenticate(alice_key)["email"] == "carol@example.com"
    assert service.authenticate(None) is None
    assert service.authenticate("cf_not_a_real_key") is None


def test_revoked_key_stops_working_only_for_that_key(two_users):
    service, alice, bob, _ = two_users
    first = service.create_user("dan@example.com", plan="free")
    second = service._store_key(service.store.user_by_email("dan@example.com")["id"], "cf_second_key_value_123456")
    service.sign_out(first)
    assert service.authenticate(first) is None
    assert service.authenticate("cf_second_key_value_123456") is not None
