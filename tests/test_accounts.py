"""Sign-in by email link, one key per sign-in, revocation. Mail goes to an outbox folder in tests."""
import json
import re
import sqlite3
from datetime import timedelta

import pytest

from webapp import service as svc
from webapp.service import SIGN_IN_MESSAGE, Service, ServiceError
from webapp.store import Store


@pytest.fixture
def app_service(tmp_path):
    return Service(Store(tmp_path / "app.sqlite3"), tmp_path, probe_minutes=lambda p: 1.0)


def _sent(service):
    return [json.loads(p.read_text()) for p in sorted((service.data_dir / "outbox").glob("*.json"))]


def _token_from_mail(service):
    body = _sent(service)[-1]["body"]
    return re.search(r"/app#login=([A-Za-z0-9_\-]+)", body).group(1)


def test_first_request_creates_account_and_mails_a_link(app_service):
    msg = app_service.request_sign_in("New.Person@Example.com", "https://clipforge.test")
    assert msg == SIGN_IN_MESSAGE
    user = app_service.store.user_by_email("new.person@example.com")
    assert user is not None and user["plan"] == "free"
    mail = _sent(app_service)[-1]
    assert mail["to"] == "new.person@example.com"
    assert "https://clipforge.test/app#login=" in mail["body"]


def test_unknown_and_known_addresses_get_the_same_answer(app_service):
    assert app_service.request_sign_in("a@example.com", "https://x.test") == SIGN_IN_MESSAGE
    assert app_service.request_sign_in("a@example.com", "https://x.test") == SIGN_IN_MESSAGE
    assert app_service.request_sign_in("not-an-email", "https://x.test") == SIGN_IN_MESSAGE


def test_link_redeems_once_for_a_working_key(app_service):
    app_service.request_sign_in("a@example.com", "https://x.test")
    token = _token_from_mail(app_service)
    result = app_service.redeem_sign_in(token)
    assert result["api_key"].startswith("cf_") and result["email"] == "a@example.com"
    assert app_service.authenticate(result["api_key"])["email"] == "a@example.com"
    with pytest.raises(ServiceError, match="expired or was already used"):
        app_service.redeem_sign_in(token)


def test_expired_link_is_refused(app_service, monkeypatch):
    monkeypatch.setattr(svc, "LOGIN_TTL", timedelta(seconds=-1))
    app_service.request_sign_in("a@example.com", "https://x.test")
    with pytest.raises(ServiceError):
        app_service.redeem_sign_in(_token_from_mail(app_service))


def test_made_up_or_empty_tokens_are_refused(app_service):
    with pytest.raises(ServiceError):
        app_service.redeem_sign_in("")
    with pytest.raises(ServiceError):
        app_service.redeem_sign_in("guessed-token")


def test_each_sign_in_issues_its_own_key_and_sign_out_revokes_only_that_one(app_service):
    app_service.request_sign_in("a@example.com", "https://x.test")
    token1 = _token_from_mail(app_service)
    app_service.request_sign_in("a@example.com", "https://x.test")
    token2 = _token_from_mail(app_service)
    key1 = app_service.redeem_sign_in(token1)["api_key"]
    key2 = app_service.redeem_sign_in(token2)["api_key"]
    app_service.sign_out(key1)
    assert app_service.authenticate(key1) is None
    assert app_service.authenticate(key2) is not None


def test_keys_and_links_are_stored_only_as_hashes(app_service):
    app_service.request_sign_in("a@example.com", "https://x.test")
    token = _token_from_mail(app_service)
    key = app_service.redeem_sign_in(token)["api_key"]
    store = app_service.store
    stored = [str(v) for row in store._run("SELECT * FROM api_keys") for v in row.values()]
    stored += [str(v) for row in store._run("SELECT * FROM login_links") for v in row.values()]
    assert stored and key not in " ".join(stored) and token not in " ".join(stored)


def test_sign_in_requests_are_rate_limited_per_address(app_service):
    for _ in range(svc.LOGIN_REQUESTS_PER_HOUR + 3):
        assert app_service.request_sign_in("busy@example.com", "https://x.test") == SIGN_IN_MESSAGE
    assert len(_sent(app_service)) == svc.LOGIN_REQUESTS_PER_HOUR


def test_key_auth_still_works_for_admin_created_accounts(app_service):
    key = app_service.create_user("admin@example.com", plan="pro")
    assert app_service.authenticate(key)["plan"] == "pro"
    assert app_service.authenticate("cf_wrong") is None
