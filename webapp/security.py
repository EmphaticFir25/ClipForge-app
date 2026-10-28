"""HTTP hardening as plain ASGI middleware, so it works with or without any framework layer.

- Content-Security-Policy: scripts only from this origin, no inline scripts or handlers.
- HTTPS only (redirect) and HSTS, when CLIPFORGE_HTTPS_ONLY=1.
- No caching of API responses, which carry private clips and account data.
- Request body cap, checked both from Content-Length and as bytes stream in, so an
  upload can't fill the disk before the plan check runs.
"""
from __future__ import annotations

import json
import os

CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' https://fonts.googleapis.com",
    "style-src-attr 'unsafe-inline'",  # generated markup uses style="" for bars and custom properties
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: blob:",
    "media-src 'self' blob:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])

STATIC_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
]
HSTS = (b"strict-transport-security", b"max-age=31536000; includeSubDomains")

DEFAULT_MAX_BODY = 8 * 1024**3 + 1024**2  # largest plan upload plus form overhead


class BodyTooLarge(Exception):
    pass


def _header(scope, name: bytes) -> str | None:
    for k, v in scope.get("headers", []):
        if k == name:
            return v.decode("latin-1")
    return None


class SecurityMiddleware:
    def __init__(self, app, *, https_only: bool | None = None, max_body_bytes: int | None = None,
                 trust_forwarded_proto: bool | None = None):
        self.app = app
        env = os.environ
        self.https_only = env.get("CLIPFORGE_HTTPS_ONLY", "0") == "1" if https_only is None else https_only
        self.trust_forwarded = (env.get("CLIPFORGE_TRUST_PROXY", "0") == "1"
                                if trust_forwarded_proto is None else trust_forwarded_proto)
        self.max_body = int(env.get("CLIPFORGE_MAX_BODY_BYTES", DEFAULT_MAX_BODY)) if max_body_bytes is None else max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        scheme = scope.get("scheme", "http")
        if self.trust_forwarded:
            scheme = _header(scope, b"x-forwarded-proto") or scheme
        if self.https_only and scheme != "https":
            host = _header(scope, b"host") or "localhost"
            raw = scope.get("raw_path")
            path = raw.decode("latin-1") if isinstance(raw, bytes) else (raw or scope["path"])
            target = f"https://{host}{path}"
            if scope.get("query_string"):
                target += "?" + scope["query_string"].decode("latin-1")
            return await self._respond(send, 308, b"", [(b"location", target.encode("latin-1"))], scope)

        declared = _header(scope, b"content-length")
        if declared and declared.isdigit() and int(declared) > self.max_body:
            return await self._too_large(scope, send)

        started = False

        async def counting_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body:
                    raise BodyTooLarge()
            return message

        async def guarded_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = list(message.get("headers", []))
                headers += STATIC_HEADERS
                if self.https_only:
                    headers.append(HSTS)
                if scope["path"].startswith("/v1/"):
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        received = 0
        try:
            await self.app(scope, counting_receive, guarded_send)
        except BodyTooLarge:
            if started:
                raise
            await self._too_large(scope, send)

    async def _too_large(self, scope, send):
        return await self._respond(send, 413, json.dumps({"detail": "upload is too large"}).encode(),
                                   [(b"content-type", b"application/json")], scope)

    async def _respond(self, send, status: int, body: bytes, headers: list, scope):
        headers = list(headers) + STATIC_HEADERS + [(b"content-length", str(len(body)).encode())]
        if self.https_only and scope.get("scheme") == "https":
            headers.append(HSTS)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})
