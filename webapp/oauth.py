"""Sign in with Google or GitHub.

The provider proves the email address. We never see the password. Each sign-in:
  1. the browser goes to the provider with a random state (and a PKCE challenge for Google),
  2. the provider sends the browser back with a code and the same state,
  3. the state is checked and used up (single use, 10 minutes),
  4. the code is exchanged server to server for an access token,
  5. the verified email is read, and the account is found or created.
The access token is used once and discarded. It is never stored or logged.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

STATE_TTL = timedelta(minutes=10)
TIMEOUT_S = 15

PROVIDERS = {
    "google": {
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "userinfo": "https://openidconnect.googleapis.com/v1/userinfo",
        "scope": "openid email",
        "pkce": True,
    },
    "github": {
        "authorize": "https://github.com/login/oauth/authorize",
        "token": "https://github.com/login/oauth/access_token",
        "emails": "https://api.github.com/user/emails",
        "scope": "user:email",
        "pkce": False,
    },
}

# http(method, url, form_or_none, headers) -> parsed JSON. Replaced in tests.
HttpCall = Callable[[str, str, dict | None, dict], dict]


class OAuthError(Exception):
    """Safe to show the user. Never contains tokens or secrets."""


def credentials(provider: str) -> tuple[str, str] | None:
    name = provider.upper()
    client_id = os.environ.get(f"{name}_CLIENT_ID", "")
    secret = os.environ.get(f"{name}_CLIENT_SECRET", "")
    return (client_id, secret) if client_id and secret else None


def enabled_providers() -> dict[str, bool]:
    return {name: credentials(name) is not None for name in PROVIDERS}


def redirect_uri(site_url: str, provider: str) -> str:
    return f"{site_url.rstrip('/')}/v1/auth/oauth/{provider}/callback"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def start_url(provider: str, site_url: str, store, now: datetime | None = None) -> str:
    """Stores a fresh state and returns the provider's sign-in address."""
    cfg = PROVIDERS[provider]
    creds = credentials(provider)
    if creds is None:
        raise OAuthError("this sign-in option isn't set up yet")
    now = now or datetime.now(timezone.utc)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    store.insert_oauth_state({
        "state_hash": _hash(state), "provider": provider, "verifier": verifier,
        "created_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + STATE_TTL).isoformat(timespec="seconds"),
    })
    params = {
        "client_id": creds[0],
        "redirect_uri": redirect_uri(site_url, provider),
        "response_type": "code",
        "scope": cfg["scope"],
        "state": state,
    }
    if cfg["pkce"]:
        params["code_challenge"] = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        params["code_challenge_method"] = "S256"
    if provider == "google":
        params["prompt"] = "select_account"
    return f"{cfg['authorize']}?{urllib.parse.urlencode(params)}"


def complete(provider: str, code: str, state: str, site_url: str, store,
             http: HttpCall | None = None, now: datetime | None = None) -> str:
    """Checks the state, exchanges the code, and returns the verified email address."""
    if provider not in PROVIDERS:
        raise OAuthError("unknown sign-in option")
    if not code or not state:
        raise OAuthError("the sign-in was cancelled")
    creds = credentials(provider)
    if creds is None:
        raise OAuthError("this sign-in option isn't set up yet")
    now = now or datetime.now(timezone.utc)
    row = store.claim_oauth_state(_hash(state), provider, now.isoformat(timespec="seconds"))
    if row is None:
        raise OAuthError("that sign-in link has expired or was already used. Try again.")

    http = http or _request
    cfg = PROVIDERS[provider]
    form = {
        "client_id": creds[0], "client_secret": creds[1], "code": code,
        "redirect_uri": redirect_uri(site_url, provider), "grant_type": "authorization_code",
    }
    if cfg["pkce"]:
        form["code_verifier"] = row["verifier"]
    try:
        token = http("POST", cfg["token"], form, {"Accept": "application/json"}).get("access_token")
    except OAuthError:
        raise
    except Exception:
        raise OAuthError(f"{provider.title()} didn't complete the sign-in. Try again.")
    if not token:
        raise OAuthError(f"{provider.title()} didn't complete the sign-in. Try again.")

    auth = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        if provider == "google":
            info = http("GET", cfg["userinfo"], None, auth)
            email, verified = info.get("email"), info.get("email_verified") is True
        else:
            rows = http("GET", cfg["emails"], None, auth)
            rows = rows if isinstance(rows, list) else []
            primary = next((r for r in rows if r.get("primary") and r.get("verified")), None)
            email, verified = (primary.get("email"), True) if primary else (None, False)
    except OAuthError:
        raise
    except Exception:
        raise OAuthError(f"couldn't read your email from {provider.title()}. Try again.")
    if not email or not verified:
        raise OAuthError(f"your email address isn't verified with {provider.title()}. Verify it there and try again.")
    return str(email).strip().lower()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request(method: str, url: str, form: dict | None, headers: dict) -> dict:
    data = urllib.parse.urlencode(form).encode("utf-8") if form is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            body = r.read() or b"{}"
    except urllib.error.HTTPError as e:
        # Report the status only. The provider's body can echo tokens.
        raise OAuthError(f"provider returned status {e.code}")
    except urllib.error.URLError:
        raise OAuthError("couldn't reach the sign-in provider")
    return json.loads(body)
