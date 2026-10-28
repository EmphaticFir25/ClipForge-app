"""Payments through Stripe Checkout (hosted by Stripe).

We never see or store card numbers. The browser goes to Stripe's hosted page to pay,
then Stripe tells us the result through a signed webhook. Plans only change when a
verified webhook arrives, never because the browser says so.

Billing is off until these environment variables are set:
    STRIPE_SECRET_KEY            sk_test_... (use test mode first)
    STRIPE_WEBHOOK_SECRET        whsec_... from the webhook endpoint settings
    STRIPE_PRICE_<PLAN>_<INTERVAL>   e.g. STRIPE_PRICE_CREATOR_MONTHLY=price_...
    CLIPFORGE_SITE_URL           e.g. https://clipforge.example (used for return URLs)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

STRIPE_API = "https://api.stripe.com/v1"
TOLERANCE_S = 300  # reject webhooks older than five minutes
PAID_PLANS = ("creator", "pro", "business")
INTERVALS = ("monthly", "yearly")


class BillingError(ValueError):
    pass


class BillingDisabled(BillingError):
    pass


@dataclass
class BillingConfig:
    secret_key: str = ""
    webhook_secret: str = ""
    site_url: str = "http://127.0.0.1:8000"
    prices: dict[tuple[str, str], str] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return bool(self.secret_key and self.webhook_secret and self.prices)

    @classmethod
    def from_env(cls, env: dict | None = None) -> "BillingConfig":
        env = os.environ if env is None else env
        prices = {}
        for plan in PAID_PLANS:
            for interval in INTERVALS:
                key = f"STRIPE_PRICE_{plan.upper()}_{interval.upper()}"
                if env.get(key):
                    prices[(plan, interval)] = env[key]
        return cls(
            secret_key=env.get("STRIPE_SECRET_KEY", ""),
            webhook_secret=env.get("STRIPE_WEBHOOK_SECRET", ""),
            site_url=env.get("CLIPFORGE_SITE_URL", "http://127.0.0.1:8000").rstrip("/"),
            prices=prices,
        )


def checkout_form(cfg: BillingConfig, user: dict, plan: str, interval: str) -> dict:
    """Form fields for POST /v1/checkout/sessions. The user id travels with the session."""
    if plan not in PAID_PLANS:
        raise BillingError("choose a paid plan")
    if interval not in INTERVALS:
        raise BillingError("interval must be monthly or yearly")
    price = cfg.prices.get((plan, interval))
    if not price:
        raise BillingError(f"the {plan} {interval} price isn't configured yet")
    return {
        "mode": "subscription",
        "line_items[0][price]": price,
        "line_items[0][quantity]": "1",
        "client_reference_id": user["id"],
        "customer_email": user["email"],
        "metadata[plan]": plan,
        "subscription_data[metadata][plan]": plan,
        "allow_promotion_codes": "true",
        "success_url": f"{cfg.site_url}/app?billing=success",
        "cancel_url": f"{cfg.site_url}/pricing?billing=cancelled",
    }


def create_checkout_session(cfg: BillingConfig, form: dict,
                            post: Callable[[str, bytes, dict], dict] | None = None) -> str:
    """Create a Checkout session and return the URL to send the browser to."""
    if not cfg.enabled:
        raise BillingDisabled("billing isn't configured on this server")
    body = urllib.parse.urlencode(form).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {cfg.secret_key}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = (post or _post_json)(f"{STRIPE_API}/checkout/sessions", body, headers)
    url = data.get("url")
    if not url:
        raise BillingError("Stripe didn't return a checkout link")
    return url


def _post_json(url: str, body: bytes, headers: dict) -> dict:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise BillingError(f"Stripe rejected the request (HTTP {e.code})")


def verify_signature(payload: bytes, header: str | None, secret: str, now: float | None = None) -> bool:
    """Check a Stripe-Signature header: t=<unix time>,v1=<hmac>[,v1=...]."""
    if not header or not secret:
        return False
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    timestamps = [p.split("=", 1)[1] for p in header.split(",") if p.startswith("t=")]
    signatures = [p.split("=", 1)[1] for p in header.split(",") if p.startswith("v1=")]
    if not timestamps or not signatures:
        return False
    try:
        t = int(timestamps[0])
    except ValueError:
        return False
    now = time.time() if now is None else now
    if abs(now - t) > TOLERANCE_S:
        return False
    expected = hmac.new(secret.encode("utf-8"), f"{t}.".encode("utf-8") + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in signatures)


def handle_event(store, event: dict) -> str:
    """Apply one verified Stripe event to the user's plan. Returns what happened."""
    kind = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    metadata = obj.get("metadata") or {}

    if kind == "checkout.session.completed":
        user_id = obj.get("client_reference_id")
        plan = metadata.get("plan")
        if not user_id or plan not in PAID_PLANS:
            return "ignored"
        store.set_billing(user_id, plan=plan,
                          customer_id=obj.get("customer"), subscription_id=obj.get("subscription"))
        return f"upgraded to {plan}"

    if kind == "customer.subscription.updated" and obj.get("status") == "active" and metadata.get("plan") in PAID_PLANS:
        user = store.user_by_customer(obj.get("customer"))
        if user:
            store.set_billing(user["id"], plan=metadata["plan"], subscription_id=obj.get("id"))
            return f"plan set to {metadata['plan']}"
        return "ignored"

    if kind in ("customer.subscription.deleted", "customer.subscription.updated") and (
        kind == "customer.subscription.deleted" or obj.get("status") in ("canceled", "unpaid", "incomplete_expired")
    ):
        user = store.user_by_customer(obj.get("customer"))
        if user:
            store.set_billing(user["id"], plan="free", subscription_id=None)
            return "downgraded to free"
        return "ignored"

    return "ignored"


def cancel_subscription(cfg: BillingConfig, subscription_id: str,
                        request: Callable[[str, dict], dict] | None = None) -> None:
    """Cancels a Stripe subscription immediately. Raises BillingError if Stripe refuses."""
    if not cfg.enabled:
        raise BillingDisabled("billing isn't configured on this server")
    if not subscription_id.startswith("sub_"):
        raise BillingError("unknown subscription")
    url = f"{STRIPE_API}/subscriptions/{urllib.parse.quote(subscription_id, safe='')}"
    headers = {"Authorization": f"Bearer {cfg.secret_key}"}
    (request or _delete_json)(url, headers)


def _delete_json(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        # Report the status only. Stripe's body and our key never reach the caller.
        raise BillingError(f"Stripe refused the cancellation (status {e.code})")

