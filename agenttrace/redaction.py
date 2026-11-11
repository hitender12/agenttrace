"""Redaction of secrets and sensitive fields before persistence/export.

Redaction is applied defensively in two places: when an event is written to
the store, and again when a run is exported. A secret that enters a run
must never appear in the SQLite file, an artifact file, or an exported
fixture.

Three mechanisms, in order of strength:

1. **Key matching (substring, case-insensitive).** Any field whose name
   contains ``password``, ``passwd``, ``pwd``, ``secret``, ``token``,
   ``api_key``/``apikey``/``api-key``, ``auth``, ``credential``,
   ``private`` or ``access_key`` has its value redacted, whatever the
   exact key spelling (``db_password``, ``auth_token``, ``x-api-key``,
   ``clientSecret`` all match). The raw value is also *learned* (see 3).
2. **Shape matching in free text.** Known secret shapes are scrubbed
   wherever they appear: ``sk-...`` / ``ghp_...`` / AWS / Slack tokens,
   ``Bearer ...`` headers, JWTs, PEM private-key blocks, long hex
   strings (>= 32 chars), and ``password=...`` / ``token: ...``
   assignments written inline in text.
3. **Secret learning.** A value first seen under a sensitive key (or
   registered explicitly with :meth:`Redactor.register_secret`, e.g. the
   model API key) is scrubbed from *every* later string in the same run
   — so a secret that also turns up in an error message, a tool result
   or a retrieval document is removed there too.

Honest limit (documented, tested): an arbitrary string that has never
appeared under a sensitive key, was never registered, and matches no
known secret shape cannot be distinguished from ordinary text by any
redactor, and is *not* redacted. If your agent handles a credential,
pass it under a sensitive field name or call ``register_secret`` —
that is the contract this module can keep.
"""
from __future__ import annotations

import re

REDACTED = "[REDACTED]"

# Substrings (lowercased, separators stripped) that mark a field name as
# sensitive. Matching is by substring, so db_password, auth_token,
# x-api-key, clientSecret, sessionToken, etc. all match.
SENSITIVE_KEY_SUBSTRINGS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "api-key",
    "auth",
    "bearer",
    "credential",
    "private",
    "accesskey",
    "access_key",
    "sessionkey",
    "session_key",
    "sessiontoken",
    "session_token",
)

# (pattern, replacement) pairs for secret-shaped values in free text.
SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "[REDACTED_TOKEN]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED_TOKEN]"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]{10,}", re.IGNORECASE), "Bearer [REDACTED]"),
    # JWT: three base64url segments.
    (re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
     "[REDACTED_JWT]"),
    # PEM private key block (including the base64 body).
    (re.compile(r"-----BEGIN (?:[A-Z ]+)?PRIVATE KEY-----.*?-----END (?:[A-Z ]+)?PRIVATE KEY-----",
                re.DOTALL), "[REDACTED_PRIVATE_KEY]"),
    # Long bare hex strings (>= 32 chars): session ids, hashes used as
    # bearer tokens. Note: 32-hex OpenTelemetry trace/span ids live in
    # event columns, not payloads, so they are not affected.
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "[REDACTED_HEX]"),
    # Inline sensitive assignment in free text: password=..., token: "...".
    # The value is also *learned* (registered) so later occurrences of the
    # same secret in bare free text are scrubbed too.
    (re.compile(
        r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|auth[_-]?token|client[_-]?secret)"
        r"(\s*[:=]\s*)([\"']?)([^\s\"',;}\]]+)",
    ), None),
]

# Minimum length for a learned/registered secret. Shorter strings are
# too likely to be ordinary words and would cause false-positive scrubbing.
MIN_SECRET_LENGTH = 8


def _normalise_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def is_sensitive_key(key: str) -> bool:
    """True when a field name marks its value as a secret (substring match)."""
    normalised = _normalise_key(key)
    return any(sub in normalised for sub in SENSITIVE_KEY_SUBSTRINGS)


class Redactor:
    """Recursively redacts secrets from dicts, lists, and strings."""

    def __init__(self, extra_keys=None, extra_patterns=None):
        self.keys = {k.lower() for k in (extra_keys or [])}
        self.patterns = list(SECRET_PATTERNS) + list(extra_patterns or [])
        # Secrets learned from sensitive-keyed values (or registered
        # explicitly): scrubbed from all free text in this redactor's run.
        self._known_secrets: set[str] = set()

    def register_secret(self, value) -> None:
        """Remember a secret value so it is scrubbed from all free text."""
        if isinstance(value, str) and len(value) >= MIN_SECRET_LENGTH:
            # Never learn an already-redacted marker: scrubbing it would
            # corrupt later markers (e.g. turn [REDACTED_TOKEN] into
            # [REDACTED]]).
            if "[REDACTED" not in value:
                self._known_secrets.add(value)

    @property
    def known_secrets(self) -> frozenset:
        return frozenset(self._known_secrets)

    def _is_sensitive(self, key: str) -> bool:
        return str(key).lower() in self.keys or is_sensitive_key(key)

    def _redact_sensitive_value(self, value):
        """Redact a value that sits under a sensitive key, learning it first."""
        if isinstance(value, str):
            self.register_secret(value)
            return REDACTED
        if isinstance(value, dict):
            return {k: self._redact_sensitive_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            redacted = [self._redact_sensitive_value(v) for v in value]
            return type(value)(redacted) if isinstance(value, tuple) else redacted
        return REDACTED

    def redact(self, value):
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                # Scrub a secret that leaked into a dict *key*.
                clean_key = self.redact_text(str(k)) if isinstance(k, str) else k
                out[clean_key] = (
                    self._redact_sensitive_value(v) if self._is_sensitive(k)
                    else self.redact(v)
                )
            return out
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self.redact(v) for v in value)
        if isinstance(value, str):
            return self.redact_text(value)
        return value

    def redact_text(self, text: str) -> str:
        for pattern, replacement in self.patterns:
            if replacement is None:
                # Inline-assignment pattern: redact the value *and* learn
                # it, so the same secret is scrubbed from later free text.
                def _assign_repl(match, _pattern=pattern):
                    self.register_secret(match.group(4))
                    return f"{match.group(1)}{match.group(2)}{match.group(3)}{REDACTED}"

                text = pattern.sub(_assign_repl, text)
            else:
                text = pattern.sub(replacement, text)
        # Learned/registered secrets last: longest first so a secret that
        # contains a shorter secret is scrubbed whole.
        for secret in sorted(self._known_secrets, key=len, reverse=True):
            if secret in text:
                text = text.replace(secret, REDACTED)
        return text
