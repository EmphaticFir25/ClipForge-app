"""Encryption for secrets the server must keep but can't show again: platform OAuth tokens.

The key comes from CLIPFORGE_DATA_KEY, a Fernet key (generate with
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
Keep it in the host's secret store, never in the repo. Losing it makes stored tokens unreadable.
"""
from __future__ import annotations

import logging
import os
import re

from cryptography.fernet import Fernet, InvalidToken


class SecretError(Exception):
    pass


class SecretBox:
    def __init__(self, key: str | bytes):
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError):
            raise SecretError("CLIPFORGE_DATA_KEY is not a valid Fernet key")

    @classmethod
    def from_env(cls) -> "SecretBox":
        key = os.environ.get("CLIPFORGE_DATA_KEY")
        if not key:
            raise SecretError("CLIPFORGE_DATA_KEY is not set")
        return cls(key)

    def seal(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def open(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError):
            # Never echo the token or the key in the error
            raise SecretError("stored secret could not be decrypted")


# Things that must never reach a log file. Each pattern is replaced by a fixed marker.
_PATTERNS = [
    (re.compile(r"cf_[A-Za-z0-9_\-]{20,}"), "cf_[redacted]"),
    (re.compile(r"sk_(?:live|test)_[A-Za-z0-9]+"), "sk_[redacted]"),
    (re.compile(r"whsec_[A-Za-z0-9]+"), "whsec_[redacted]"),
    (re.compile(r"ya29\.[A-Za-z0-9._\-]+"), "[redacted-token]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"), "Bearer [redacted]"),
    (re.compile(r"(?i)(access_token|refresh_token|client_secret|password|token)=[^&\s]+"), r"\1=[redacted]"),
    (re.compile(r"(?i)#login=[A-Za-z0-9_\-]+"), "#login=[redacted]"),
]


def redact(text: str) -> str:
    for pattern, marker in _PATTERNS:
        text = pattern.sub(marker, text)
    return text


class RedactingFilter(logging.Filter):
    """Attach to handlers so secrets are masked whatever module logs them."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_info and record.exc_text is None:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True
