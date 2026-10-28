"""YouTube and TikTok accounts, with the platforms faked. Nothing here calls a real platform."""
import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet

from conftest_helpers import make_job, make_service, make_user
from webapp import platforms
from webapp import service as svc
from webapp.platforms import PlatformError, ReconnectRequired
from webapp.secretbox import RedactingFilter, SecretBox
from webapp.service import ServiceError

SITE = "https://clip.test"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
YT_TOKEN_URL = "https://oauth2.googleapis.com/token"
YT_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
YT_CHANNELS = "https://www.googleapis.com/youtube/v3/channels"
YT_INIT = "https://www.googleapis.com/upload/youtube/v3/videos"
YT_UPLOAD_SESSION = "https://upload.youtube.test/session/1"
TT_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TT_REVOKE_URL = "https://open.tiktokapis.com/v2/oauth/revoke/"
TT_USERINFO = "https://open.tiktokapis.com/v2/user/info/"
TT_INIT = "https://open.tiktokapis.com/v2/post/publish/video/init/"
TT_UPLOAD = "https://upload.tiktok.test/chunk"
TT_STATUS = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"


def j(obj, status=200, headers=None):
    return status, headers or {}, json.dumps(obj).encode("utf-8")


class FakePlatform:
    """Records every request. Routes are (method, url) -> handler; unknown requests fail the test."""

    def __init__(self):
        self.calls = []
        self.routes = {}
        self.tt_status = ["PROCESSING_UPLOAD", "PUBLISH_COMPLETE"]
        self.refresh_fails = False
        self.revoke_fails = False
        self.reset_defaults()

    def reset_defaults(self):
        self.routes = {
            ("POST", YT_TOKEN_URL): self._yt_token,
            ("POST", YT_REVOKE_URL): self._revoke_yt,
            ("GET", YT_CHANNELS): lambda m, u, h, b: j({"items": [{"id": "UC999", "snippet": {"title": "Pat's Streams"}}]}),
            ("POST", TT_TOKEN_URL): self._tt_token,
            ("POST", TT_REVOKE_URL): lambda m, u, h, b: j({}) if not self.revoke_fails else j({}, 500),
            ("GET", TT_USERINFO): lambda m, u, h, b: j({"data": {"user": {"open_id": "tt-open-1", "display_name": "patstreams"}}}),
            ("POST", YT_INIT): lambda m, u, h, b: (200, {"location": YT_UPLOAD_SESSION}, b""),
            ("PUT", YT_UPLOAD_SESSION): lambda m, u, h, b: j({"id": "yt-video-42"}),
            ("POST", TT_INIT): lambda m, u, h, b: j({"data": {"publish_id": "pub-7", "upload_url": TT_UPLOAD}}),
            ("PUT", TT_UPLOAD): lambda m, u, h, b: (206, {}, b""),
            ("POST", TT_STATUS): lambda m, u, h, b: j({"data": {"status": self.tt_status.pop(0) if len(self.tt_status) > 1 else self.tt_status[0]}}),
        }

    def _yt_token(self, m, u, h, b):
        form = parse_qs(b.decode())
        if form.get("grant_type") == ["refresh_token"]:
            if self.refresh_fails:
                return j({"error": "invalid_grant", "error_description": "Token has been expired or revoked."}, 400)
            return j({"access_token": "ya29.refreshed-access", "expires_in": 3600})
        return j({"access_token": "ya29.first-access", "refresh_token": "1//first-refresh", "expires_in": 3600,
                  "scope": "youtube.upload"})

    def _tt_token(self, m, u, h, b):
        form = parse_qs(b.decode())
        if form.get("grant_type") == ["refresh_token"]:
            if self.refresh_fails:
                return j({"error": "invalid_token"}, 400)
            return j({"access_token": "act.fake-refreshed-tiktok-token", "refresh_token": "rt-rotated",
                      "expires_in": 86400})
        return j({"access_token": "act.fake-first-tiktok-token", "refresh_token": "rt-first",
                  "expires_in": 86400, "open_id": "tt-open-1"})

    def _revoke_yt(self, m, u, h, b):
        return j({}) if not self.revoke_fails else j({"error": "invalid_token"}, 400)

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        handler = self.routes.get((method, url.split("?")[0] if "upload/youtube" not in url else url))
        if handler is None and "upload.youtube.test" in url and method == "PUT":
            handler = self.routes[("PUT", YT_UPLOAD_SESSION)]
        if handler is None and "upload.tiktok.test" in url and method == "PUT":
            handler = self.routes[("PUT", TT_UPLOAD)]
        if handler is None and url.startswith(YT_CHANNELS):
            handler = self.routes[("GET", YT_CHANNELS)]
        if handler is None and url.startswith(YT_INIT):
            handler = self.routes[("POST", YT_INIT)]
        if handler is None and url.startswith(TT_USERINFO):
            handler = self.routes[("GET", TT_USERINFO)]
        if handler is None:
            raise AssertionError(f"unexpected request {method} {url}")
        return handler(method, url, headers, body)

    def uploads(self):
        return [c for c in self.calls if c[0] in ("POST", "PUT") and ("upload" in c[1] or "publish" in c[1])]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "yt-client.apps")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "yt-secret-value-XYZ")
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "tt-client-key")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "tt-secret-value-XYZ")
    monkeypatch.setattr(svc, "APPROVED_PLATFORMS", {"youtube", "tiktok"})


@pytest.fixture
def fake():
    return FakePlatform()


@pytest.fixture
def service(tmp_path, env, fake):
    return make_service(tmp_path, platform_http=fake, platform_sleep=lambda s: None,
                        secret_box=SecretBox(Fernet.generate_key()))


def connect(service, user, platform, now=NOW):
    """Runs the real start and complete steps, as the browser would."""
    url, nonce = service.connect_start(user, platform, SITE, now=now)
    state = parse_qs(urlparse(url).query)["state"][0]
    result = service.connect_complete(platform, "auth-code", state, nonce, SITE, now=now)
    return result, url, state, nonce


# ---------- connect flow and binding ----------

def test_authorize_urls_request_only_the_scopes_needed(service, env):
    alice = make_user(service, "a@example.com", plan="pro")
    yt, _ = service.connect_start(alice, "youtube", SITE, now=NOW)
    q = parse_qs(urlparse(yt).query)
    assert q["scope"] == ["https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"]
    assert q["code_challenge_method"] == ["S256"] and q["client_id"] == ["yt-client.apps"]
    assert "yt-secret-value" not in yt
    tt, _ = service.connect_start(alice, "tiktok", SITE, now=NOW)
    q = parse_qs(urlparse(tt).query)
    assert q["scope"] == ["user.info.basic,video.publish"] and q["client_key"] == ["tt-client-key"]
    assert "tt-secret-value" not in tt


def test_connect_stores_the_account_under_the_user_who_started_it(service, fake):
    alice = make_user(service, "alice@example.com", plan="pro")
    bob = make_user(service, "bob@example.com", plan="pro")
    result, *_ = connect(service, alice, "youtube")
    assert result["display_name"] == "Pat's Streams"
    assert service.store.get_platform_account(alice["id"], "youtube") is not None
    assert service.store.get_platform_account(bob["id"], "youtube") is None


def test_link_sent_to_someone_else_cannot_be_completed_by_them(service):
    """The attack: Mallory starts a connection and sends the link to Alice. Alice's browser has no nonce
    from Mallory's start, so nothing is linked. Mallory's account stays clean."""
    mallory = make_user(service, "mallory@example.com", plan="pro")
    _, mallory_nonce = service.connect_start(mallory, "youtube", SITE, now=NOW)
    url = service.connect_start(mallory, "youtube", SITE, now=NOW)[0]
    state = parse_qs(urlparse(url).query)["state"][0]
    with pytest.raises(PlatformError, match="same browser"):
        service.connect_complete("youtube", "alice-code", state, None, SITE, now=NOW)
    with pytest.raises(PlatformError, match="same browser"):
        service.connect_complete("youtube", "alice-code", state, "some-other-browser", SITE, now=NOW)
    assert service.store.get_platform_account(mallory["id"], "youtube") is None


def test_wrong_browser_does_not_use_up_the_state(service):
    """A stray or forged callback must not burn the real user's link."""
    alice = make_user(service, "alice2@example.com", plan="pro")
    url, nonce = service.connect_start(alice, "youtube", SITE, now=NOW)
    state = parse_qs(urlparse(url).query)["state"][0]
    with pytest.raises(PlatformError):
        service.connect_complete("youtube", "c", state, "forged", SITE, now=NOW)
    result = service.connect_complete("youtube", "c", state, nonce, SITE, now=NOW)
    assert result["platform"] == "youtube"


def test_state_works_once_and_expires(service):
    alice = make_user(service, "once@example.com", plan="pro")
    _, url, state, nonce = connect(service, alice, "youtube")
    with pytest.raises(PlatformError, match="expired or was already used"):
        service.connect_complete("youtube", "c", state, nonce, SITE, now=NOW)
    url2, nonce2 = service.connect_start(alice, "tiktok", SITE, now=NOW)
    state2 = parse_qs(urlparse(url2).query)["state"][0]
    late = NOW + platforms.STATE_TTL + timedelta(seconds=1)
    with pytest.raises(PlatformError, match="expired"):
        service.connect_complete("tiktok", "c", state2, nonce2, SITE, now=late)


def test_state_from_one_platform_cannot_finish_another(service):
    alice = make_user(service, "cross@example.com", plan="pro")
    url, nonce = service.connect_start(alice, "youtube", SITE, now=NOW)
    state = parse_qs(urlparse(url).query)["state"][0]
    with pytest.raises(PlatformError):
        service.connect_complete("tiktok", "c", state, nonce, SITE, now=NOW)


def test_free_plan_cannot_connect(tmp_path, env, fake):
    service = make_service(tmp_path, platform_http=fake, secret_box=SecretBox(Fernet.generate_key()))
    free = make_user(service, "free@example.com", plan="free")
    from webapp.service import QuotaExceeded
    with pytest.raises(QuotaExceeded):
        service.connect_start(free, "youtube", SITE, now=NOW)


def test_connecting_needs_the_encryption_key(tmp_path, env, fake, monkeypatch):
    monkeypatch.delenv("CLIPFORGE_DATA_KEY", raising=False)
    service = make_service(tmp_path, platform_http=fake)
    pro = make_user(service, "nokey@example.com", plan="pro")
    with pytest.raises(PlatformError, match="aren't set up"):
        service.connect_start(pro, "youtube", SITE, now=NOW)


def test_unconfigured_platform_is_refused(tmp_path, fake, monkeypatch):
    monkeypatch.delenv("TIKTOK_CLIENT_KEY", raising=False)
    service = make_service(tmp_path, platform_http=fake, secret_box=SecretBox(Fernet.generate_key()))
    pro = make_user(service, "unset@example.com", plan="pro")
    with pytest.raises(ServiceError, match="aren't set up"):
        service.connect_start(pro, "tiktok", SITE, now=NOW)


# ---------- encryption at rest and what the API shows ----------

def test_tokens_are_encrypted_in_the_database(service):
    alice = make_user(service, "enc@example.com", plan="pro")
    connect(service, alice, "youtube")
    row = service.store._run("SELECT * FROM platform_accounts WHERE user_id = ?", (alice["id"],))[0]
    dump = json.dumps(row)
    assert "ya29.first-access" not in dump and "1//first-refresh" not in dump
    box = service.secret_box
    assert box.open(row["access_token_enc"]) == "ya29.first-access"
    assert box.open(row["refresh_token_enc"]) == "1//first-refresh"


def test_account_list_never_contains_tokens(service):
    alice = make_user(service, "list@example.com", plan="pro")
    connect(service, alice, "youtube")
    listing = json.dumps(service.platform_accounts(alice))
    assert "ya29" not in listing and "1//" not in listing and "refresh" not in listing
    yt = next(a for a in service.platform_accounts(alice) if a["platform"] == "youtube")
    assert yt["connected"] and yt["display_name"] == "Pat's Streams" and yt["status"] == "connected"


# ---------- refresh and reconnect ----------

def test_token_near_expiry_is_refreshed_and_the_new_one_is_stored(service, fake):
    alice = make_user(service, "refresh@example.com", plan="pro")
    connect(service, alice, "youtube")
    later = NOW + timedelta(minutes=58)  # within five minutes of the hour-long token expiry
    token = service.fresh_access_token(alice, "youtube", now=later)
    assert token == "ya29.refreshed-access"
    assert any("refresh_token" in (c[3] or b"").decode(errors="ignore") for c in fake.calls)
    assert service.secret_box.open(service.store.get_platform_account(alice["id"], "youtube")["access_token_enc"]) == "ya29.refreshed-access"


def test_fresh_token_is_reused_when_not_near_expiry(service, fake):
    alice = make_user(service, "fresh@example.com", plan="pro")
    connect(service, alice, "youtube")
    before = len(fake.calls)
    assert service.fresh_access_token(alice, "youtube", now=NOW) == "ya29.first-access"
    assert len(fake.calls) == before  # no refresh call


def test_refresh_failure_marks_the_account_and_tells_the_user(service, fake):
    alice = make_user(service, "stale@example.com", plan="pro")
    connect(service, alice, "youtube")
    fake.refresh_fails = True
    with pytest.raises(ReconnectRequired, match="reconnect"):
        service.fresh_access_token(alice, "youtube", now=NOW + timedelta(minutes=58))
    row = service.store.get_platform_account(alice["id"], "youtube")
    assert row["status"] == "needs_reconnect"
    assert next(a for a in service.platform_accounts(alice) if a["platform"] == "youtube")["status"] == "needs_reconnect"


def test_scheduled_post_fails_visibly_when_the_account_needs_reconnecting(service, fake):
    alice = make_user(service, "sched@example.com", plan="pro")
    job_id = make_job(service, alice)
    connect(service, alice, "youtube")
    when = (NOW + timedelta(hours=2)).isoformat()
    row = service.create_schedule(alice, job_id, 1, "youtube", when)
    service.store.set_platform_status(alice["id"], "youtube", "needs_reconnect")
    uploads_before = len(fake.uploads())
    counts = service.dispatch_due_schedules(NOW + timedelta(hours=3))
    assert counts["failed"] == 1
    stored = service.store.get_schedule(row["id"])
    assert stored["status"] == "failed" and "reconnect" in stored["error"].lower()
    assert len(fake.uploads()) == uploads_before  # nothing was uploaded


def test_disconnected_account_cannot_post(service):
    alice = make_user(service, "gone@example.com", plan="pro")
    job_id = make_job(service, alice)
    connect(service, alice, "tiktok")
    row = service.create_schedule(alice, job_id, 1, "tiktok", (NOW + timedelta(hours=2)).isoformat())
    service.disconnect(alice, "tiktok")
    service.dispatch_due_schedules(NOW + timedelta(hours=3))
    stored = service.store.get_schedule(row["id"])
    assert stored["status"] == "failed" and "connect" in stored["error"].lower()


# ---------- uploads ----------

def test_youtube_upload_is_resumable_and_private_by_default(service, fake):
    alice = make_user(service, "yt@example.com", plan="pro")
    job_id = make_job(service, alice)
    connect(service, alice, "youtube")
    row = service.create_schedule(alice, job_id, 1, "youtube", (NOW + timedelta(hours=2)).isoformat())
    service.dispatch_due_schedules(NOW + timedelta(hours=3))
    stored = service.store.get_schedule(row["id"])
    assert stored["status"] == "posted" and stored["external_id"] == "yt-video-42"
    init = next(c for c in fake.calls if c[1].startswith(YT_INIT))
    meta = json.loads(init[3])
    assert meta["status"]["privacyStatus"] == "private"
    assert init[2]["X-Upload-Content-Length"] == "3"  # the clip's byte count
    put = next(c for c in fake.calls if c[0] == "PUT")
    assert put[3] == b"mp4" and put[2]["Authorization"] == "Bearer ya29.first-access"


def test_tiktok_upload_is_self_only_chunked_and_waits_for_completion(service, fake):
    alice = make_user(service, "tt@example.com", plan="pro")
    job_id = make_job(service, alice)
    connect(service, alice, "tiktok")
    row = service.create_schedule(alice, job_id, 1, "tiktok", (NOW + timedelta(hours=2)).isoformat())
    fake.tt_status = ["PROCESSING_UPLOAD", "PUBLISH_COMPLETE"]
    service.dispatch_due_schedules(NOW + timedelta(hours=3))
    stored = service.store.get_schedule(row["id"])
    assert stored["status"] == "posted" and stored["external_id"] == "pub-7"
    init = json.loads(next(c for c in fake.calls if c[1] == TT_INIT)[3])
    assert init["post_info"]["privacy_level"] == "SELF_ONLY"
    assert init["source_info"]["total_chunk_count"] == 1 and init["source_info"]["video_size"] == 3
    chunk = next(c for c in fake.calls if c[0] == "PUT" and c[1] == TT_UPLOAD)
    assert chunk[2]["Content-Range"] == "bytes 0-2/3"


def test_tiktok_failed_processing_is_reported_without_details(service, fake):
    alice = make_user(service, "ttfail@example.com", plan="pro")
    job_id = make_job(service, alice)
    connect(service, alice, "tiktok")
    row = service.create_schedule(alice, job_id, 1, "tiktok", (NOW + timedelta(hours=2)).isoformat())
    fake.tt_status = ["FAILED"]
    service.dispatch_due_schedules(NOW + timedelta(hours=3))
    stored = service.store.get_schedule(row["id"])
    assert stored["status"] == "failed" and "act." not in stored["error"]


def test_upload_is_split_into_chunks_when_the_clip_is_large(service, fake, monkeypatch):
    monkeypatch.setattr(platforms, "TIKTOK_CHUNK", 2)
    alice = make_user(service, "chunks@example.com", plan="pro")
    connect(service, alice, "tiktok")
    fake.tt_status = ["PUBLISH_COMPLETE"]
    path = service.data_dir / "clip.mp4"
    path.write_bytes(b"abcde")  # 5 bytes, 2 byte chunks -> 3 chunks
    platforms.upload("tiktok", "act.fake-token-value", path, "t", "d", fake, lambda s: None)
    ranges = [c[2]["Content-Range"] for c in fake.calls if c[0] == "PUT"]
    assert ranges == ["bytes 0-1/5", "bytes 2-3/5", "bytes 4-4/5"]


def test_platform_rejecting_the_upload_gives_a_plain_message(service, fake):
    alice = make_user(service, "reject@example.com", plan="pro")
    job_id = make_job(service, alice)
    connect(service, alice, "youtube")
    fake.routes[("POST", YT_INIT)] = lambda m, u, h, b: j({"error": {"message": "quota"}}, 403)
    row = service.create_schedule(alice, job_id, 1, "youtube", (NOW + timedelta(hours=2)).isoformat())
    service.dispatch_due_schedules(NOW + timedelta(hours=3))
    stored = service.store.get_schedule(row["id"])
    assert stored["status"] == "failed" and stored["error"] == "YouTube didn't accept the upload. Try again later."


# ---------- cross-user use ----------

def test_user_b_cannot_post_with_user_a_account(service, fake):
    alice = make_user(service, "owner@example.com", plan="pro")
    bob = make_user(service, "other@example.com", plan="pro")
    connect(service, alice, "youtube")
    with pytest.raises(PlatformError, match="connect your YouTube"):
        service.fresh_access_token(bob, "youtube", now=NOW)
    bob_job = make_job(service, bob)
    with pytest.raises(ServiceError, match="connect your YouTube"):
        service.create_schedule(bob, bob_job, 1, "youtube", (NOW + timedelta(hours=2)).isoformat())


def test_user_b_cannot_see_or_remove_user_a_account(service):
    alice = make_user(service, "see@example.com", plan="pro")
    bob = make_user(service, "nosee@example.com", plan="pro")
    connect(service, alice, "youtube")
    assert all(not a["connected"] for a in service.platform_accounts(bob))
    with pytest.raises(ServiceError, match="isn't connected"):
        service.disconnect(bob, "youtube")
    assert service.store.get_platform_account(alice["id"], "youtube") is not None


def test_a_schedule_posts_with_its_owners_token(service, fake):
    alice = make_user(service, "ownerpost@example.com", plan="pro")
    bob = make_user(service, "ownerpost2@example.com", plan="pro")
    job_id = make_job(service, alice)
    connect(service, alice, "youtube")
    connect(service, bob, "youtube")
    service.store.upsert_platform_account({**service.store.get_platform_account(bob["id"], "youtube"),
                                           "access_token_enc": service.secret_box.seal("ya29.BOB-ONLY"),
                                           "refresh_token_enc": service.secret_box.seal("1//bob-refresh")})
    row = service.create_schedule(alice, job_id, 1, "youtube", (NOW + timedelta(hours=2)).isoformat())
    service.dispatch_due_schedules(NOW + timedelta(hours=3))
    used = [c[2]["Authorization"] for c in fake.calls if c[0] == "PUT"]
    assert used == ["Bearer ya29.first-access"]  # alice's token, never bob's


# ---------- disconnect and deletion ----------

def test_disconnect_revokes_at_the_platform_and_deletes_the_row(service, fake):
    alice = make_user(service, "dc@example.com", plan="pro")
    connect(service, alice, "youtube")
    assert service.disconnect(alice, "youtube") is True
    revoke = next(c for c in fake.calls if c[1] == YT_REVOKE_URL)
    assert parse_qs(revoke[3].decode())["token"] == ["1//first-refresh"]
    assert service.store.get_platform_account(alice["id"], "youtube") is None


def test_failed_revoke_still_removes_the_stored_tokens(service, fake):
    alice = make_user(service, "dc2@example.com", plan="pro")
    connect(service, alice, "tiktok")
    fake.revoke_fails = True
    assert service.disconnect(alice, "tiktok") is False
    assert service.store.get_platform_account(alice["id"], "tiktok") is None


def test_deleting_the_account_revokes_platform_tokens(service, fake):
    alice = make_user(service, "erase@example.com", plan="pro")
    connect(service, alice, "youtube")
    connect(service, alice, "tiktok")
    service.delete_account(alice, "erase@example.com")
    assert any(c[1] == YT_REVOKE_URL for c in fake.calls)
    assert any(c[1] == TT_REVOKE_URL for c in fake.calls)
    assert service.store._run("SELECT * FROM platform_accounts WHERE user_id = ?", (alice["id"],)) == []


# ---------- logs and errors never show tokens ----------

def test_provider_error_body_with_a_token_never_reaches_the_message(service, fake):
    alice = make_user(service, "leak@example.com", plan="pro")
    url, nonce = service.connect_start(alice, "youtube", SITE, now=NOW)
    state = parse_qs(urlparse(url).query)["state"][0]
    fake.routes[("POST", YT_TOKEN_URL)] = lambda m, u, h, b: (
        500, {}, b'{"error":"server_error","access_token":"ya29.LEAKED-IN-BODY"}')
    with pytest.raises(PlatformError) as err:
        service.connect_complete("youtube", "c", state, nonce, SITE, now=NOW)
    assert "LEAKED" not in str(err.value)


def test_tiktok_token_in_a_log_line_is_masked(caplog):
    logger = logging.getLogger("clipforge.platform.test")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO, logger="clipforge.platform.test"):
        logger.info("posting with %s", "act.fake-tiktok-token-value-0001")
        logger.info("refresh_token=1//0gSecretRefreshValue&state=ok")
    assert "fake-tiktok-token" not in caplog.text and "SecretRefreshValue" not in caplog.text
