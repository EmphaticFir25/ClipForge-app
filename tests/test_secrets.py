"""Stored platform tokens are encrypted, and secrets are masked in logs."""
import logging

import pytest
from cryptography.fernet import Fernet

from webapp.secretbox import RedactingFilter, SecretBox, SecretError, redact


def test_round_trip_and_ciphertext_hides_the_plaintext():
    box = SecretBox(Fernet.generate_key())
    token = box.seal("ya29.refresh-token-value")
    assert "refresh-token-value" not in token
    assert box.open(token) == "ya29.refresh-token-value"


def test_wrong_key_cannot_open_and_the_error_leaks_nothing():
    token = SecretBox(Fernet.generate_key()).seal("secret-value-123")
    with pytest.raises(SecretError) as err:
        SecretBox(Fernet.generate_key()).open(token)
    assert "secret-value-123" not in str(err.value) and token not in str(err.value)


def test_invalid_key_is_rejected_with_a_clear_message():
    with pytest.raises(SecretError, match="not a valid Fernet key"):
        SecretBox("not-a-key")


def test_missing_key_in_environment_is_an_error(monkeypatch):
    monkeypatch.delenv("CLIPFORGE_DATA_KEY", raising=False)
    with pytest.raises(SecretError, match="not set"):
        SecretBox.from_env()


@pytest.mark.parametrize("raw,leaked", [
    ("signed in with cf_abcdefghijklmnopqrstuvwxyz123456", "cf_abcdefghijklmnopqrstuvwxyz123456"),
    # built at runtime so no secret-shaped literal sits in the repo
    ("stripe key " + "sk_" + "test_" + "x" * 24, "sk_test_" + "x" * 24),
    ("webhook whsec_abc123def456", "whsec_abc123def456"),
    ("Authorization: Bearer ya29.a0AfH6SMBx", "ya29.a0AfH6SMBx"),
    ("callback?access_token=zzz111&state=ok", "zzz111"),
    ("opened http://x/app#login=TOKENVALUE123", "TOKENVALUE123"),
])
def test_known_secret_shapes_are_masked(raw, leaked):
    out = redact(raw)
    assert leaked not in out
    assert "redacted" in out


def test_log_records_are_masked_by_the_filter(caplog):
    logger = logging.getLogger("clipforge.test")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO, logger="clipforge.test"):
        logger.info("key %s used", "cf_abcdefghijklmnopqrstuvwxyz123456")
    assert "abcdefghijklmnopqrstuvwxyz" not in caplog.text
    assert "cf_[redacted]" in caplog.text


def test_exception_text_is_masked_too(caplog):
    logger = logging.getLogger("clipforge.test.exc")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.ERROR, logger="clipforge.test.exc"):
        try:
            raise RuntimeError("upstream said sk_live_SECRETVALUE999")
        except RuntimeError:
            logger.exception("request failed")
    assert "SECRETVALUE999" not in caplog.text
