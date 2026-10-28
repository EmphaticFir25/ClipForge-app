"""YouTube and TikTok: connecting accounts, refreshing tokens, revoking them, and uploading.

Each platform has its own OAuth app and only the scopes it needs:
  YouTube  youtube.upload (upload videos), youtube.readonly (read the channel name)
  TikTok   user.info.basic (read the display name), video.publish (post a video)

Uploads are private (YouTube) or self-only (TikTok) unless the user changes that on the platform.
Every request is made server to server, and tokens are never put in a URL, a log line or an error.
The HTTP function is injected so the tests can fake the platforms.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

STATE_TTL = timedelta(minutes=10)
REFRESH_BEFORE = timedelta(minutes=5)
TIMEOUT_S = 60
TIKTOK_CHUNK = 32 * 1024 * 1024  # TikTok accepts 5 MB to 64 MB chunks

CONFIG = {
    "youtube": {
        "label": "YouTube",
        "client_id_env": "YOUTUBE_CLIENT_ID",
        "client_secret_env": "YOUTUBE_CLIENT_SECRET",
        "client_id_param": "client_id",
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "revoke": "https://oauth2.googleapis.com/revoke",
        "scopes": ["https://www.googleapis.com/auth/youtube.upload",
                   "https://www.googleapis.com/auth/youtube.readonly"],
        "scope_sep": " ",
        "extra_authorize": {"access_type": "offline", "prompt": "consent"},
    },
    "tiktok": {
        "label": "TikTok",
        "client_id_env": "TIKTOK_CLIENT_KEY",
        "client_secret_env": "TIKTOK_CLIENT_SECRET",
        "client_id_param": "client_key",
        "authorize": "https://www.tiktok.com/v2/auth/authorize/",
        "token": "https://open.tiktokapis.com/v2/oauth/token/",
        "revoke": "https://open.tiktokapis.com/v2/oauth/revoke/",
        "scopes": ["user.info.basic", "video.publish"],
        "scope_sep": ",",
        "extra_authorize": {},
    },
}

# http(method, url, headers, body) -> (status, headers, body_bytes). Replaced in tests.
Http = Callable[[str, str, dict, bytes | None], tuple[int, dict, bytes]]


class PlatformError(Exception):
    """Safe to show the user. Never contains tokens, secrets or provider response bodies."""


class ReconnectRequired(PlatformError):
    """The platform no longer accepts the account's refresh token. The user has to connect again."""


def configured(platform: str) -> bool:
    cfg = CONFIG[platform]
    return bool(os.environ.get(cfg["client_id_env"]) and os.environ.get(cfg["client_secret_env"]))


def credentials(platform: str) -> tuple[str, str]:
    cfg = CONFIG[platform]
    cid, secret = os.environ.get(cfg["client_id_env"], ""), os.environ.get(cfg["client_secret_env"], "")
    if not cid or not secret:
        raise PlatformError(f"{cfg['label']} connections aren't set up on this server")
    return cid, secret


def redirect_uri(site_url: str, platform: str) -> str:
    return f"{site_url.rstrip('/')}/v1/accounts/{platform}/callback"


def hash_value(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def authorize_url(platform: str, site_url: str, state: str, verifier: str) -> str:
    cfg = CONFIG[platform]
    cid, _ = credentials(platform)
    params = {
        cfg["client_id_param"]: cid,
        "redirect_uri": redirect_uri(site_url, platform),
        "response_type": "code",
        "scope": cfg["scope_sep"].join(cfg["scopes"]),
        "state": state,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode("ascii")).digest()),
        "code_challenge_method": "S256",
        **cfg["extra_authorize"],
    }
    return f"{cfg['authorize']}?{urllib.parse.urlencode(params)}"


def new_state() -> tuple[str, str]:
    return secrets.token_urlsafe(32), secrets.token_urlsafe(48)


# ---------- token exchange, refresh and revoke ----------

def _token_response(platform: str, status: int, body: bytes) -> dict:
    data = _json(body)
    if status >= 400 or "access_token" not in data:
        err = str(data.get("error", "")) if isinstance(data, dict) else ""
        if err in ("invalid_grant", "invalid_token", "access_denied") or status in (400, 401):
            raise ReconnectRequired(f"{CONFIG[platform]['label']} needs you to connect again")
        raise PlatformError(f"{CONFIG[platform]['label']} didn't finish the request. Try again later.")
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "expires_in": int(data.get("expires_in") or 3600),
        "scope": data.get("scope", ""),
        "open_id": data.get("open_id", ""),
    }


def exchange_code(platform: str, code: str, verifier: str, site_url: str, http: Http) -> dict:
    cfg = CONFIG[platform]
    cid, secret = credentials(platform)
    form = {cfg["client_id_param"]: cid, "client_secret": secret, "code": code,
            "redirect_uri": redirect_uri(site_url, platform), "grant_type": "authorization_code",
            "code_verifier": verifier}
    status, _, body = http("POST", cfg["token"], {"Content-Type": "application/x-www-form-urlencoded"},
                           _form(form))
    return _token_response(platform, status, body)


def refresh(platform: str, refresh_token: str, http: Http) -> dict:
    cfg = CONFIG[platform]
    cid, secret = credentials(platform)
    form = {cfg["client_id_param"]: cid, "client_secret": secret,
            "refresh_token": refresh_token, "grant_type": "refresh_token"}
    status, _, body = http("POST", cfg["token"], {"Content-Type": "application/x-www-form-urlencoded"},
                           _form(form))
    return _token_response(platform, status, body)


def revoke(platform: str, token: str, http: Http) -> None:
    cfg = CONFIG[platform]
    cid, secret = credentials(platform)
    form = {cfg["client_id_param"]: cid, "client_secret": secret, "token": token}
    status, _, _ = http("POST", cfg["revoke"], {"Content-Type": "application/x-www-form-urlencoded"}, _form(form))
    if status >= 400:
        raise PlatformError(f"{CONFIG[platform]['label']} didn't confirm the disconnect")


# ---------- who the account is ----------

def identity(platform: str, access_token: str, http: Http) -> dict:
    """Returns {'platform_user_id', 'display_name'} for the connected account."""
    auth = {"Authorization": f"Bearer {access_token}"}
    if platform == "youtube":
        status, _, body = http("GET", "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
                               auth, None)
        items = _json(body).get("items") or []
        if status >= 400 or not items:
            raise PlatformError("no YouTube channel was found for this account")
        return {"platform_user_id": str(items[0]["id"]),
                "display_name": str(items[0].get("snippet", {}).get("title", ""))[:100]}
    status, _, body = http("GET", "https://open.tiktokapis.com/v2/user/info/?fields=open_id,display_name",
                           auth, None)
    user = (_json(body).get("data") or {}).get("user") or {}
    if status >= 400 or not user.get("open_id"):
        raise PlatformError("couldn't read the TikTok account")
    return {"platform_user_id": str(user["open_id"]), "display_name": str(user.get("display_name", ""))[:100]}


# ---------- uploads ----------

def upload(platform: str, access_token: str, video: Path, title: str, description: str, http: Http,
           sleep: Callable[[float], None] = time.sleep) -> str:
    """Uploads one clip as a private (YouTube) or self-only (TikTok) post. Returns the platform's ID."""
    data = Path(video).read_bytes()
    if platform == "youtube":
        return _upload_youtube(access_token, data, title, description, http)
    return _upload_tiktok(access_token, data, title, http, sleep)


def _upload_youtube(token: str, data: bytes, title: str, description: str, http: Http) -> str:
    meta = {
        "snippet": {"title": title[:100], "description": description[:5000], "categoryId": "22"},
        "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False},
    }
    start_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": "video/mp4",
        "X-Upload-Content-Length": str(len(data)),
    }
    status, headers, body = http(
        "POST", "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        start_headers, json.dumps(meta).encode("utf-8"))
    location = headers.get("location") or headers.get("Location")
    if status >= 400 or not location:
        raise PlatformError("YouTube didn't accept the upload. Try again later.")
    status, _, body = http("PUT", location, {"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"}, data)
    video_id = _json(body).get("id")
    if status >= 400 or not video_id:
        raise PlatformError("YouTube didn't finish the upload. Try again later.")
    return str(video_id)


def _upload_tiktok(token: str, data: bytes, title: str, http: Http, sleep: Callable[[float], None]) -> str:
    size = len(data)
    chunk = min(size, TIKTOK_CHUNK)
    total = max(1, math.ceil(size / chunk))
    init = {
        "post_info": {"title": title[:150], "privacy_level": "SELF_ONLY",
                      "disable_duet": True, "disable_stitch": True, "disable_comment": False},
        "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk,
                        "total_chunk_count": total},
    }
    status, _, body = http("POST", "https://open.tiktokapis.com/v2/post/publish/video/init/",
                           {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"},
                           json.dumps(init).encode("utf-8"))
    info = (_json(body).get("data") or {})
    if status >= 400 or not info.get("publish_id") or not info.get("upload_url"):
        raise PlatformError("TikTok didn't accept the upload. Try again later.")
    for index in range(total):
        start = index * chunk
        end = min(start + chunk, size) - 1
        status, _, _ = http("PUT", info["upload_url"], {
            "Content-Type": "video/mp4",
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(end - start + 1),
        }, data[start:end + 1])
        if status >= 400:
            raise PlatformError("TikTok didn't finish receiving the video. Try again later.")
    publish_id = str(info["publish_id"])
    for _ in range(12):
        status, _, body = http("POST", "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
                               {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"},
                               json.dumps({"publish_id": publish_id}).encode("utf-8"))
        state = str(((_json(body).get("data") or {}).get("status")) or "")
        if state == "PUBLISH_COMPLETE":
            return publish_id
        if state == "FAILED":
            raise PlatformError("TikTok couldn't publish this video. Try again later.")
        sleep(5)
    raise PlatformError("TikTok is still processing this video. Check TikTok before posting again.")


# ---------- HTTP and helpers ----------

def _form(values: dict) -> bytes:
    return urllib.parse.urlencode(values).encode("utf-8")


def _json(body: bytes) -> dict:
    try:
        data = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def default_http(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        # Keep the status. The response body can echo tokens, so it is not returned.
        return e.code, {}, b""
    except urllib.error.URLError:
        raise PlatformError("couldn't reach the platform. Try again later.")
