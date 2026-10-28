"""Billing tests: signed webhooks, checkout form, and plan changes. No network or Stripe account needed."""
import hashlib
import hmac

import pytest

from webapp import billing as b
from webapp.service import Service
from webapp.store import Store

SECRET = "whsec_test"
NOW = 1_800_000_000


def sign(payload: bytes, secret: str = SECRET, t: int = NOW) -> str:
    mac = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


@pytest.fixture
def svc(tmp_path):
    return Service(Store(tmp_path / "app.sqlite3"), tmp_path, probe_minutes=lambda p: 1.0)


@pytest.fixture
def cfg():
    return b.BillingConfig(secret_key="sk_test", webhook_secret=SECRET, site_url="https://example.test",
                           prices={("creator", "monthly"): "price_c_m", ("pro", "yearly"): "price_p_y"})


def test_valid_signature_is_accepted():
    body = b'{"type":"ping"}'
    assert b.verify_signature(body, sign(body), SECRET, now=NOW)


def test_tampered_body_wrong_secret_or_missing_header_rejected():
    body = b'{"type":"ping"}'
    assert not b.verify_signature(body + b" ", sign(body), SECRET, now=NOW)
    assert not b.verify_signature(body, sign(body, secret="other"), SECRET, now=NOW)
    assert not b.verify_signature(body, None, SECRET, now=NOW)
    assert not b.verify_signature(body, sign(body), "", now=NOW)


def test_old_or_future_timestamps_rejected():
    body = b"{}"
    assert not b.verify_signature(body, sign(body, t=NOW - 3600), SECRET, now=NOW)
    assert not b.verify_signature(body, sign(body, t=NOW + 3600), SECRET, now=NOW)


def test_malformed_header_rejected():
    assert not b.verify_signature(b"{}", "v1=abc", SECRET, now=NOW)
    assert not b.verify_signature(b"{}", "t=notanumber,v1=abc", SECRET, now=NOW)


def test_billing_disabled_without_secrets():
    cfg = b.BillingConfig.from_env({})
    assert not cfg.enabled
    with pytest.raises(b.BillingDisabled):
        b.create_checkout_session(cfg, {})


def test_from_env_reads_configured_prices():
    cfg = b.BillingConfig.from_env({
        "STRIPE_SECRET_KEY": "sk", "STRIPE_WEBHOOK_SECRET": SECRET,
        "STRIPE_PRICE_PRO_YEARLY": "price_p", "CLIPFORGE_SITE_URL": "https://site.test/",
    })
    assert cfg.enabled
    assert cfg.prices == {("pro", "yearly"): "price_p"}
    assert cfg.site_url == "https://site.test"


def test_checkout_form_only_allows_configured_paid_prices(cfg):
    user = {"id": "u1", "email": "a@example.com"}
    form = b.checkout_form(cfg, user, "creator", "monthly")
    assert form["line_items[0][price]"] == "price_c_m"
    assert form["client_reference_id"] == "u1"
    assert form["success_url"] == "https://example.test/app?billing=success"
    with pytest.raises(b.BillingError):
        b.checkout_form(cfg, user, "free", "monthly")
    with pytest.raises(b.BillingError):
        b.checkout_form(cfg, user, "business", "monthly")  # no price configured
    with pytest.raises(b.BillingError):
        b.checkout_form(cfg, user, "creator", "weekly")


def test_create_session_sends_key_and_returns_stripe_url(cfg):
    seen = {}

    def fake_post(url, body, headers):
        seen.update(url=url, headers=headers)
        return {"url": "https://checkout.stripe.test/c/1"}

    result = b.create_checkout_session(cfg, {"mode": "subscription"}, post=fake_post)
    assert result == "https://checkout.stripe.test/c/1"
    assert seen["url"].endswith("/checkout/sessions")
    assert seen["headers"]["Authorization"] == "Bearer sk_test"


def test_completed_checkout_upgrades_user(svc):
    svc.create_user("a@example.com")
    user = svc.store.user_by_email("a@example.com")
    event = {"type": "checkout.session.completed", "data": {"object": {
        "client_reference_id": user["id"], "metadata": {"plan": "creator"},
        "customer": "cus_1", "subscription": "sub_1"}}}
    assert b.handle_event(svc.store, event) == "upgraded to creator"
    after = svc.store.user_by_email("a@example.com")
    assert after["plan"] == "creator"
    assert after["stripe_customer_id"] == "cus_1"


def test_cancelled_subscription_downgrades_to_free(svc):
    svc.create_user("a@example.com")
    user = svc.store.user_by_email("a@example.com")
    b.handle_event(svc.store, {"type": "checkout.session.completed", "data": {"object": {
        "client_reference_id": user["id"], "metadata": {"plan": "pro"}, "customer": "cus_9", "subscription": "sub_9"}}})
    result = b.handle_event(svc.store, {"type": "customer.subscription.deleted", "data": {"object": {
        "customer": "cus_9", "id": "sub_9"}}})
    assert result == "downgraded to free"
    assert svc.store.user_by_email("a@example.com")["plan"] == "free"


def test_free_plan_and_unknown_events_are_ignored(svc):
    svc.create_user("a@example.com")
    user = svc.store.user_by_email("a@example.com")
    free = {"type": "checkout.session.completed", "data": {"object": {
        "client_reference_id": user["id"], "metadata": {"plan": "free"}}}}
    assert b.handle_event(svc.store, free) == "ignored"
    assert b.handle_event(svc.store, {"type": "invoice.paid", "data": {"object": {}}}) == "ignored"
    assert svc.store.user_by_email("a@example.com")["plan"] == "free"


def test_schema_migration_adds_billing_columns_to_old_database(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, key_hash TEXT NOT NULL, "
                 "plan TEXT NOT NULL DEFAULT 'free', created_at TEXT NOT NULL)")
    conn.commit()
    conn.close()
    store = Store(path)
    cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(users)").fetchall()}
    assert {"stripe_customer_id", "stripe_subscription_id"} <= cols
