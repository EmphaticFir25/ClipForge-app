"""Headers, HTTPS-only, no-store, and body limits, tested on the raw ASGI interface."""
import asyncio
import json
import re
from pathlib import Path

import pytest

from webapp.security import SecurityMiddleware

STATIC = Path(__file__).resolve().parent.parent / "webapp" / "static"


async def _app(scope, receive, send):
    if scope["path"] == "/upload":
        total = 0
        while True:
            msg = await receive()
            total += len(msg.get("body", b""))
            if not msg.get("more_body"):
                break
        body = json.dumps({"received": total}).encode()
    else:
        body = b"ok"
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": body})


def call(app, method="GET", path="/", headers=None, chunks=(b"",), scheme="http"):
    scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(),
             "scheme": scheme, "query_string": b"", "headers": [(b"host", b"clip.test")] + (headers or [])}
    sent = []
    queue = list(chunks)

    async def receive():
        if queue:
            chunk = queue.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(queue)}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    headers = {k.decode(): v.decode() for k, v in start["headers"]}
    return start["status"], headers, body


def test_every_response_has_the_security_headers():
    status, h, _ = call(SecurityMiddleware(_app, https_only=False, max_body_bytes=1000))
    assert status == 200
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "no-referrer"
    assert "camera=()" in h["permissions-policy"]


def test_csp_blocks_inline_script_and_third_party_scripts():
    _, h, _ = call(SecurityMiddleware(_app, https_only=False, max_body_bytes=1000))
    csp = h["content-security-policy"]
    script = re.search(r"script-src ([^;]+)", csp).group(1)
    assert script == "'self'"  # no unsafe-inline, no eval, no CDNs
    assert "object-src 'none'" in csp and "frame-ancestors 'none'" in csp and "base-uri 'none'" in csp


def test_api_responses_are_never_cached():
    _, h, _ = call(SecurityMiddleware(_app, https_only=False, max_body_bytes=1000), path="/v1/jobs")
    assert h["cache-control"] == "no-store"
    _, h, _ = call(SecurityMiddleware(_app, https_only=False, max_body_bytes=1000), path="/pricing")
    assert "cache-control" not in h


def test_plain_http_is_redirected_to_https_when_required():
    status, h, _ = call(SecurityMiddleware(_app, https_only=True, max_body_bytes=1000),
                        path="/app", scheme="http")
    assert status == 308
    assert h["location"] == "https://clip.test/app"
    assert "strict-transport-security" not in h  # HSTS is only sent over HTTPS


def test_https_responses_carry_hsts_when_required():
    _, h, _ = call(SecurityMiddleware(_app, https_only=True, max_body_bytes=1000), scheme="https")
    assert h["strict-transport-security"].startswith("max-age=31536000")


def test_declared_oversize_body_is_refused_before_it_is_read():
    status, _, body = call(SecurityMiddleware(_app, https_only=False, max_body_bytes=100), method="POST",
                           path="/upload", headers=[(b"content-length", b"5000")], chunks=(b"x" * 10,))
    assert status == 413
    assert json.loads(body)["detail"] == "upload is too large"


def test_streamed_oversize_body_is_cut_off_without_content_length():
    chunks = [b"x" * 60, b"x" * 60, b"x" * 60]  # 180 bytes against a 100 byte cap
    status, _, _ = call(SecurityMiddleware(_app, https_only=False, max_body_bytes=100), method="POST",
                        path="/upload", chunks=chunks)
    assert status == 413


def test_body_within_the_cap_is_delivered_intact():
    status, _, body = call(SecurityMiddleware(_app, https_only=False, max_body_bytes=1000), method="POST",
                           path="/upload", chunks=[b"a" * 300, b"b" * 300])
    assert status == 200 and json.loads(body)["received"] == 600


def test_static_pages_have_no_inline_scripts_or_handlers():
    """A strict CSP only works if no page depends on inline JavaScript."""
    offenders = []
    for page in list(STATIC.rglob("*.html")):
        text = page.read_text(encoding="utf-8")
        for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>", text):
            offenders.append(f"{page.name}: inline script")
        if re.search(r"\son[a-z]+\s*=", text, re.I):
            offenders.append(f"{page.name}: inline event handler")
        if re.search(r"<style[\s>]", text, re.I):
            offenders.append(f"{page.name}: inline <style> element")
    assert offenders == []
