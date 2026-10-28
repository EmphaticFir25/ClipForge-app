"""Sign in with Google or GitHub, with the provider faked. Nothing here calls a real provider."""
import base64
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from conftest_helpers import make_service
from webapp import oauth

SITE = "https://clip.test"
NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "google-client-id.apps")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "fake-google-secret-value")
    monkeypatch.setenv("GITHUB_CLIENT_ID", "gh-client-id")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "ghsecret-value-123")


class FakeProvider:
    """Records each call. Responses are set per test."""

    def __init__(self, token="tok-abc", userinfo=None, emails=None, fail_token=False):
        self.calls = []
        self.token = token
        self.userinfo = userinfo or {"email": "Pat@Example.com", "email_verified": True}
        self.emails = emails if emails is not None else [
            {"email": "pat@example.com", "primary": True, "verified": True}]
        self.fail_token = fail_token

    def __call__(self, method, url, form, headers):
        self.calls.append((method, url, form, headers))
        if url.endswith("/token"):
            if self.fail_token:
                raise oauth.OAuthError("provider returned status 400")
            return {"access_token": self.token} if self.token else {}
        if "openidconnect" in url:
            return self.userinfo
        if url.endswith("/emails"):
            return self.emails
        if url.endswith("access_token"):  # GitHub token endpoint
            return {"access_token": self.token} if self.token else {}
        raise AssertionError(url)


def _start(service, provider):
    url = oauth.start_url(provider, SITE, service.store, now=NOW)
    qs = parse_qs(urlparse(url).query)
    return url, qs["state"][0]


def test_authorize_url_has_state_and_pkce_but_never_the_secret(tmp_path, creds):
    service = make_service(tmp_path)
    url, state = _start("google", None) if False else (None, None)
    url = oauth.start_url("google", SITE, service.store, now=NOW)
    qs = parse_qs(urlparse(url).query)
    assert qs["client_id"] == ["google-client-id.apps"]
    assert qs["redirect_uri"] == [f"{SITE}/v1/auth/oauth/google/callback"]
    assert qs["code_challenge_method"] == ["S256"]
    assert "fake-google" not in url and "supersecret" not in url
    # the challenge is the SHA-256 of the verifier stored server side
    row = service.store._run("SELECT * FROM oauth_states")[0]
    expected = base64.urlsafe_b64encode(hashlib.sha256(row["verifier"].encode()).digest()).rstrip(b"=").decode()
    assert qs["code_challenge"] == [expected]


def test_github_flow_has_no_pkce_fields(tmp_path, creds):
    service = make_service(tmp_path)
    qs = parse_qs(urlparse(oauth.start_url("github", SITE, service.store, now=NOW)).query)
    assert "code_challenge" not in qs and qs["scope"] == ["user:email"]


def test_google_sign_in_returns_the_verified_email(tmp_path, creds):
    service = make_service(tmp_path)
    url = oauth.start_url("google", SITE, service.store, now=NOW)
    state = parse_qs(urlparse(url).query)["state"][0]
    provider = FakeProvider()
    email = oauth.complete("google", "code-1", state, SITE, service.store, http=provider, now=NOW)
    assert email == "pat@example.com"
    token_call = provider.calls[0]
    assert token_call[2]["code_verifier"]  # PKCE verifier sent on the back channel
    assert token_call[2]["client_secret"] == "fake-google-secret-value"  # sent server to server only


def test_state_works_once(tmp_path, creds):
    service = make_service(tmp_path)
    state = parse_qs(urlparse(oauth.start_url("google", SITE, service.store, now=NOW)).query)["state"][0]
    oauth.complete("google", "c", state, SITE, service.store, http=FakeProvider(), now=NOW)
    with pytest.raises(oauth.OAuthError, match="expired or was already used"):
        oauth.complete("google", "c", state, SITE, service.store, http=FakeProvider(), now=NOW)


def test_expired_state_is_refused(tmp_path, creds):
    service = make_service(tmp_path)
    state = parse_qs(urlparse(oauth.start_url("google", SITE, service.store, now=NOW)).query)["state"][0]
    later = NOW + oauth.STATE_TTL + timedelta(seconds=1)
    with pytest.raises(oauth.OAuthError, match="expired"):
        oauth.complete("google", "c", state, SITE, service.store, http=FakeProvider(), now=later)


def test_state_from_one_provider_cannot_finish_another(tmp_path, creds):
    service = make_service(tmp_path)
    state = parse_qs(urlparse(oauth.start_url("google", SITE, service.store, now=NOW)).query)["state"][0]
    with pytest.raises(oauth.OAuthError):
        oauth.complete("github", "c", state, SITE, service.store, http=FakeProvider(), now=NOW)


def test_forged_state_is_refused(tmp_path, creds):
    service = make_service(tmp_path)
    with pytest.raises(oauth.OAuthError):
        oauth.complete("google", "c", "not-a-real-state", SITE, service.store, http=FakeProvider(), now=NOW)


def test_cancelled_or_missing_code_is_refused(tmp_path, creds):
    service = make_service(tmp_path)
    with pytest.raises(oauth.OAuthError, match="cancelled"):
        oauth.complete("google", "", "", SITE, service.store, http=FakeProvider(), now=NOW)


def test_unverified_google_email_is_refused(tmp_path, creds):
    service = make_service(tmp_path)
    state = parse_qs(urlparse(oauth.start_url("google", SITE, service.store, now=NOW)).query)["state"][0]
    provider = FakeProvider(userinfo={"email": "x@example.com", "email_verified": False})
    with pytest.raises(oauth.OAuthError, match="isn't verified"):
        oauth.complete("google", "c", state, SITE, service.store, http=provider, now=NOW)


def test_github_uses_the_primary_verified_address(tmp_path, creds):
    service = make_service(tmp_path)
    state = parse_qs(urlparse(oauth.start_url("github", SITE, service.store, now=NOW)).query)["state"][0]
    provider = FakeProvider(emails=[
        {"email": "old@example.com", "primary": False, "verified": True},
        {"email": "Main@Example.com", "primary": True, "verified": True},
    ])
    assert oauth.complete("github", "c", state, SITE, service.store, http=provider, now=NOW) == "main@example.com"


def test_github_with_no_verified_primary_is_refused(tmp_path, creds):
    service = make_service(tmp_path)
    state = parse_qs(urlparse(oauth.start_url("github", SITE, service.store, now=NOW)).query)["state"][0]
    provider = FakeProvider(emails=[{"email": "x@example.com", "primary": True, "verified": False}])
    with pytest.raises(oauth.OAuthError, match="isn't verified"):
        oauth.complete("github", "c", state, SITE, service.store, http=provider, now=NOW)


def test_failed_token_exchange_does_not_leak_the_secret(tmp_path, creds):
    service = make_service(tmp_path)
    state = parse_qs(urlparse(oauth.start_url("google", SITE, service.store, now=NOW)).query)["state"][0]
    provider = FakeProvider(fail_token=True)
    with pytest.raises(oauth.OAuthError) as err:
        oauth.complete("google", "c", state, SITE, service.store, http=provider, now=NOW)
    assert "supersecret" not in str(err.value) and "fake-google" not in str(err.value)


def test_missing_credentials_disable_the_option(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    assert oauth.enabled_providers()["google"] is False
    with pytest.raises(oauth.OAuthError, match="isn't set up"):
        oauth.start_url("google", SITE, make_service(tmp_path), now=NOW)


def test_oauth_sign_in_creates_the_account_and_the_token_gives_one_key(tmp_path, creds):
    service = make_service(tmp_path)
    token = service.sign_in_with_verified_email("New.Person@Example.com")
    user = service.store.user_by_email("new.person@example.com")
    assert user is not None and user["plan"] == "free"
    result = service.redeem_sign_in(token)
    assert result["api_key"].startswith("cf_") and result["email"] == "new.person@example.com"
    with pytest.raises(Exception):
        service.redeem_sign_in(token)  # single use


def test_oauth_and_email_sign_in_share_one_account(tmp_path, creds):
    service = make_service(tmp_path)
    service.request_sign_in("same@example.com", SITE)
    before = service.store.user_by_email("same@example.com")["id"]
    service.sign_in_with_verified_email("same@example.com")
    assert service.store.user_by_email("same@example.com")["id"] == before
