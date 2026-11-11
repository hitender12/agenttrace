"""Redaction of secrets and sensitive fields before persistence/export.

Redaction is applied defensively in two places: when an event is written to
the store, and again when a run is exported. A secret that enters a run
must never appear in the SQLite file or in an exported fixture.
"""
from __future__ import annotations

import re

REDACTED = "[REDACTED]"

SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "password",
    "passwd",
    "secret",
    "client_secret",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "bearer",
    "private_key",
}

# (pattern, replacement) pairs for secret-shaped values in free text.
SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "[REDACTED_TOKEN]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED_TOKEN]"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]{10,}", re.IGNORECASE), "Bearer [REDACTED]"),
]


class Redactor:
    """Recursively redacts secrets from dicts, lists, and strings."""

    def __init__(self, extra_keys=None, extra_patterns=None):
        self.keys = set(SENSITIVE_KEYS) | {k.lower() for k in (extra_keys or [])}
        self.patterns = list(SECRET_PATTERNS) + list(extra_patterns or [])

    def redact(self, value):
        if isinstance(value, dict):
            return {
                k: (REDACTED if str(k).lower() in self.keys else self.redact(v))
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self.redact(v) for v in value)
        if isinstance(value, str):
            return self.redact_text(value)
        return value

    def redact_text(self, text: str) -> str:
        for pattern, replacement in self.patterns:
            text = pattern.sub(replacement, text)
        return text
