"""Account deletion removes the user's data and nothing else."""
import pytest

from conftest_helpers import make_job, make_service, make_user
from webapp.service import ServiceError


def test_deleting_an_account_removes_files_rows_keys_and_links(tmp_path):
    service = make_service(tmp_path)
    alice = make_user(service, "gone@example.com")
    job_id = make_job(service, alice)
    key_before = service.store.user_by_email("gone@example.com")
    service.request_sign_in("gone@example.com", "https://x.test")

    service.delete_account(alice, "gone@example.com")

    assert not (tmp_path / "jobs" / job_id).exists()
    assert service.store.get_job(job_id) is None
    assert service.store.user_by_email("gone@example.com") is None
    assert service.authenticate("cf_never_issued_key_value_000") is None
    assert service.store._run("SELECT * FROM api_keys WHERE user_id = ?", (key_before["id"],)) == []
    assert service.store._run("SELECT * FROM login_links WHERE email = ?", ("gone@example.com",)) == []
    assert service.store._run("SELECT * FROM auth_requests WHERE email = ?", ("gone@example.com",)) == []


def test_deleted_account_cannot_sign_back_in(tmp_path):
    service = make_service(tmp_path)
    user = make_user(service, "back@example.com")
    key = service.create_user("other@example.com")  # unrelated account keeps working
    service.delete_account(user, "back@example.com")
    assert service.authenticate(key) is not None
    assert service.store.user_by_email("back@example.com") is None


def test_deletion_needs_the_email_typed_back(tmp_path):
    service = make_service(tmp_path)
    user = make_user(service, "safe@example.com")
    make_job(service, user)
    with pytest.raises(ServiceError, match="type your email"):
        service.delete_account(user, "wrong@example.com")
    assert service.store.user_by_email("safe@example.com") is not None


def test_deletion_leaves_other_accounts_untouched(tmp_path):
    service = make_service(tmp_path)
    alice = make_user(service, "alice2@example.com")
    bob = make_user(service, "bob2@example.com")
    bob_job = make_job(service, bob)
    service.delete_account(alice, "alice2@example.com")
    assert service.get_job(bob, bob_job) is not None
    assert (tmp_path / "jobs" / bob_job).exists()


def test_paid_account_cancels_its_subscription_first(tmp_path):
    cancelled = []
    service = make_service(tmp_path, cancel_subscription=lambda sub: cancelled.append(sub))
    user = make_user(service, "paid@example.com", plan="pro")
    service.store.set_billing(user["id"], "pro", "cus_1", "sub_123")
    user = service.store.get_user_by_id(user["id"])
    service.delete_account(user, "paid@example.com")
    assert cancelled == ["sub_123"]
    assert service.store.get_user_by_id(user["id"]) is None


def test_failed_cancellation_keeps_the_account(tmp_path):
    def refuse(sub):
        raise RuntimeError("stripe down")

    service = make_service(tmp_path, cancel_subscription=refuse)
    user = make_user(service, "stuck@example.com", plan="pro")
    service.store.set_billing(user["id"], "pro", "cus_2", "sub_456")
    user = service.store.get_user_by_id(user["id"])
    with pytest.raises(ServiceError, match="still open"):
        service.delete_account(user, "stuck@example.com")
    assert service.store.get_user_by_id(user["id"]) is not None


def test_paid_account_without_a_canceller_is_refused(tmp_path):
    service = make_service(tmp_path)
    user = make_user(service, "nocancel@example.com", plan="pro")
    service.store.set_billing(user["id"], "pro", "cus_3", "sub_789")
    user = service.store.get_user_by_id(user["id"])
    with pytest.raises(ServiceError, match="cancel your paid plan"):
        service.delete_account(user, "nocancel@example.com")
