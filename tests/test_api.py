"""HTTP routes. Needs FastAPI and httpx; skipped where they aren't installed.

These exist because a route can fail at startup in ways the service tests never see.
"""
import io

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from conftest_helpers import make_job, make_service  # noqa: E402
from webapp.api import create_app  # noqa: E402
from webapp.security import SecurityMiddleware  # noqa: E402


@pytest.fixture
def client(tmp_path):
    service = make_service(tmp_path)
    app = create_app(service)
    return TestClient(SecurityMiddleware(app, https_only=False)), service


def _key(service, email):
    return service.create_user(email, plan="pro")


def test_app_builds_and_serves_the_pages(client):
    c, _ = client
    assert c.get("/pricing").status_code == 200
    assert c.get("/app").status_code == 200
    assert c.get("/terms").status_code == 200


def test_every_data_route_refuses_requests_without_a_key(client):
    c, _ = client
    for method, path in [("get", "/v1/me"), ("get", "/v1/jobs"), ("get", "/v1/jobs/abc"),
                         ("get", "/v1/jobs/abc/clips"), ("get", "/v1/schedules"),
                         ("delete", "/v1/me"), ("get", "/v1/jobs/abc/clips.zip")]:
        r = getattr(c, method)(path, json={"confirm_email": "x"} if method == "delete" and path == "/v1/me" else None)
        assert r.status_code == 401, path


def test_one_user_gets_404_for_another_users_job(client):
    c, service = client
    alice = service.authenticate(_key(service, "a@example.com"))
    bob_key = _key(service, "b@example.com")
    job_id = make_job(service, alice)
    headers = {"X-API-Key": bob_key}
    assert c.get(f"/v1/jobs/{job_id}", headers=headers).status_code == 404
    assert c.get(f"/v1/jobs/{job_id}/clips", headers=headers).status_code == 404
    assert c.get(f"/v1/jobs/{job_id}/clips/1.mp4", headers=headers).status_code == 404
    assert c.patch(f"/v1/jobs/{job_id}/clips/1", json={"caption_style": "plain"},
                   headers=headers).status_code == 404


def test_api_responses_are_not_cached(client):
    c, service = client
    key = _key(service, "cache@example.com")
    r = c.get("/v1/me", headers={"X-API-Key": key})
    assert r.headers["cache-control"] == "no-store"


def test_sign_in_always_answers_the_same_way(client):
    c, _ = client
    a = c.post("/v1/auth/start", json={"email": "exists@example.com"})
    b = c.post("/v1/auth/start", json={"email": "nobody@example.com"})
    assert a.status_code == b.status_code == 202
    assert a.json() == b.json()


def test_account_deletion_over_http(client):
    c, service = client
    key = _key(service, "bye@example.com")
    assert c.delete("/v1/me", json={"confirm_email": "wrong@example.com"},
                    headers={"X-API-Key": key}).status_code == 400
    assert c.delete("/v1/me", json={"confirm_email": "bye@example.com"},
                    headers={"X-API-Key": key}).status_code == 204
    assert c.get("/v1/me", headers={"X-API-Key": key}).status_code == 401


def test_oversize_upload_is_refused_before_reaching_the_service(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPFORGE_MAX_BODY_BYTES", "1000")
    service = make_service(tmp_path)
    c = TestClient(SecurityMiddleware(create_app(service), https_only=False))
    key = _key(service, "big@example.com")
    r = c.post("/v1/jobs", headers={"X-API-Key": key},
               files={"file": ("a.mp4", io.BytesIO(b"x" * 5000), "video/mp4")},
               data={"rights_confirmed": "true"})
    assert r.status_code == 413
    assert service.store.list_jobs(service.authenticate(key)["id"]) == []
