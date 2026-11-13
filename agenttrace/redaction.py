"""Redaction of secrets and sensitive fields before persistence/export.

Redaction is applied defensively in two places: when an event is written to
the store, and again when a run is exported. A secret that enters a run
must never appear in the SQLite file, an artifact file, or an exported
fixture.

Three mechanisms, in order of strength (plus name redaction, which
happens at write time in the store — see :mod:`agenttrace.store`):

1. **Key matching (substring, case-insensitive).** Any field whose name
   contains ``password``, ``passwd``, ``pwd``, ``secret``, ``token``,
   ``api_key``/``apikey``/``api-key``, ``auth``, ``credential``,
   ``private``, ``access_key``, ``sessionid``/``session_id``,
   ``db_pass``/``dbpass``, ``encryption_key``, ``signing_key``,
   ``csrf``, or a bare ``pass`` key has its value redacted, whatever
   the exact key spelling (``db_password``, ``auth_token``,
   ``x-api-key``, ``clientSecret`` all match). The raw value is also
   *learned* (see 3). ``pass`` is sensitive only as a standalone key:
   ``bypass``/``compass``/``passage`` are not matched.
2. **Shape matching in free text.** Known secret shapes are scrubbed
   wherever they appear: ``sk-...`` / ``ghp_...`` / AWS / Slack tokens,
   ``Bearer ...`` headers, JWTs, PEM private-key blocks, long hex
   strings (>= 32 chars), and ``password=...`` / ``token: ...``
   assignments written inline in text. Trailing punctuation is
   stripped from learned assignment values (``password=hunter2.``
   learns ``hunter2``; the period is preserved in the output).
3. **Secret learning.** A value first seen under a sensitive key (or
   registered explicitly with :meth:`Redactor.register_secret`, e.g. the
   model API key) is scrubbed from *every* later string in the same run
   — so a secret that also turns up in an error message, a tool result
   or a retrieval document is removed there too.

   Learning is **retroactive**, not forward-only: learned secrets are
   persisted per run in the store (``learned_secrets`` table), and a
   finished run is re-redacted with the run's full learned set, so a
   secret that first appears in free text and is only *later* seen
   under a sensitive key is still scrubbed from the earlier occurrence
   — in the database, in artifact files, and in exports (including
   exports from a freshly reopened store). See :mod:`agenttrace.store`.

   The persisted set stores only a secret's **SHA-256 digest and
   length, never the secret itself**: the database file contains no
   recoverable secret, yet a fresh store can still scrub it (see
   :meth:`Redactor.register_hashed_secret`).

   Generic values are never learned: a value that is itself a common
   placeholder word (``password``, ``secret``, ``token``, ...) is not
   a secret, and learning it would scrub ordinary English from later
   text. See :data:`GENERIC_VALUES`.

Honest limit (documented, tested): an arbitrary string that has never
appeared under a sensitive key, was never registered, and matches no
known secret shape cannot be distinguished from ordinary text by any
redactor, and is *not* redacted. If your agent handles a credential,
pass it under a sensitive field name or call ``register_secret`` —
that is the contract this module can keep.
"""
from __future__ import annotations

import hashlib
import re

REDACTED = "[REDACTED]"

# Substrings (lowercased, separators stripped) that mark a field name as
# sensitive. Matching is by substring, so db_password, auth_token,
# x-api-key, clientSecret, sessionToken, etc. all match. Normalisation
# strips separators, so the list holds the normalised forms: ``db_pass``
# normalises to ``dbpass``, ``session_id`` to ``sessionid``.
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
    "sessionid",
    "dbpass",
    "encryptionkey",
    "signingkey",
    "csrf",
    "passcode",
)

# Values that are generic placeholder words, not secrets. Learning one
# would scrub ordinary English from later text (e.g. learning the
# literal word "password" turns "enter your password here" into
# "enter your [REDACTED] here"). Compared case-insensitively after
# stripping surrounding whitespace/quotes.
GENERIC_VALUES = frozenset({
    "password", "passwd", "pwd", "secret", "token", "apikey", "api_key",
    "api-key", "auth", "bearer", "credential", "credentials", "private",
    "public", "changeme", "password123", "12345678", "123456789",
    "letmein", "admin", "test", "demo", "example", "none", "null",
    "undefined", "unknown", "n/a", "na", "redacted", "xxx", "yyy",
    "qwerty", "asdfgh", "pass", "passw0rd",
})

# Trailing characters stripped from an inline-assignment value before
# it is learned: sentence punctuation that belongs to the surrounding
# text, not to the secret ("password=hunter2." learns "hunter2").
_ASSIGNMENT_TRAILING = ".,:!?)]>}"

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
    # same secret in bare free text are scrubbed too. The negative
    # lookbehind keeps ``pass`` from matching inside ordinary words
    # ("bypass=..." is not an assignment of a secret called "pass");
    # ``_`` and ``-`` are treated as separators, so ``db_pass=...``
    # matches too.
    (re.compile(
        r"(?i)(?<![A-Za-z0-9])(password|passwd|pwd|pass|db[_-]?pass|secret|token|"
        r"api[_-]?key|auth[_-]?token|client[_-]?secret|session[_-]?id|"
        r"encryption[_-]?key|signing[_-]?key|csrf)"
        r"(\s*[:=]\s*)([\"']?)([^\s\"',;}\]]+)",
    ), None),
]

# Minimum length for a learned/registered secret. Shorter strings are
# too likely to be ordinary words and would cause false-positive scrubbing.
MIN_SECRET_LENGTH = 8


def _normalise_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def is_sensitive_key(key: str) -> bool:
    """True when a field name marks its value as a secret (substring match).

    The bare key ``pass`` is sensitive on its own, but only as an exact
    match — a ``pass`` *substring* would also flag ``bypass`` or
    ``compass``, which are ordinary words, not secrets.
    """
    normalised = _normalise_key(key)
    if normalised == "pass":
        return True
    return any(sub in normalised for sub in SENSITIVE_KEY_SUBSTRINGS)


class Redactor:
    """Recursively redacts secrets from dicts, lists, and strings."""

    def __init__(self, extra_keys=None, extra_patterns=None):
        self.keys = {k.lower() for k in (extra_keys or [])}
        self.patterns = list(SECRET_PATTERNS) + list(extra_patterns or [])
        # Secrets learned from sensitive-keyed values (or registered
        # explicitly): scrubbed from all free text in this redactor's run.
        self._known_secrets: set[str] = set()
        # Secrets known only by digest+length (loaded from the store's
        # persisted learned set by a freshly reopened redactor): the
        # secret itself is never stored, yet it can still be scrubbed
        # (see _scrub_hashed).
        self._hashed_secrets: set[tuple] = set()

    def register_hashed_secret(self, digest: str, length: int) -> None:
        """Remember a secret by its SHA-256 digest and length.

        Used to prime a freshly reopened redactor from the store's
        persisted learned set: the secret itself is never stored, only
        its hash, so the database file contains no recoverable secret —
        yet :meth:`redact_text` can still scrub it.
        """
        if digest and length and length >= MIN_SECRET_LENGTH:
            self._hashed_secrets.add((digest, length))

    def _scrub_hashed(self, text: str) -> str:
        """Replace occurrences of hash-known secrets in ``text``.

        For each (digest, length), slide a window of that length over
        the text and replace windows whose SHA-256 matches. This is
        how a fresh store scrubs a secret it only knows by hash.
        """
        for digest, length in sorted(self._hashed_secrets,
                                     key=lambda pair: -pair[1]):
            if length <= 0 or length > len(text):
                continue
            positions = []
            i = 0
            while i + length <= len(text):
                window = text[i:i + length]
                if hashlib.sha256(window.encode("utf-8")).hexdigest() == digest:
                    positions.append(i)
                    i += length
                else:
                    i += 1
            if not positions:
                continue
            parts = []
            last = 0
            for pos in positions:
                parts.append(text[last:pos])
                parts.append(REDACTED)
                last = pos + length
            parts.append(text[last:])
            text = "".join(parts)
        return text

    def register_secret(self, value) -> None:
        """Remember a secret value so it is scrubbed from all free text.

        Generic placeholder words (see :data:`GENERIC_VALUES`) are never
        learned: learning the literal word ``password`` would scrub
        ordinary English from later text.
        """
        if isinstance(value, str) and len(value) >= MIN_SECRET_LENGTH:
            # Never learn an already-redacted marker: scrubbing it would
            # corrupt later markers (e.g. turn [REDACTED_TOKEN] into
            # [REDACTED]]).
            if "[REDACTED" not in value:
                if value.strip().strip("\"'").lower() not in GENERIC_VALUES:
                    self._known_secrets.add(value)

    def register_value(self, value) -> None:
        """Register every string leaf of a value (dict/list/str) as a secret.

        Used when a whole value sits under a sensitive key — e.g. the
        ``value`` of ``memory_write("db_password", value)`` — so the
        secret is learned even though its own field name is generic.
        """
        if isinstance(value, str):
            self.register_secret(value)
        elif isinstance(value, dict):
            for v in value.values():
                self.register_value(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                self.register_value(v)

    def learn_from(self, value) -> None:
        """Learn every secret a structure carries, without redacting it.

        Walks dicts (values under sensitive keys are registered),
        lists, and strings (inline ``key=value`` assignments are
        learned). Used to prime the redactor before a second,
        scrubbing pass — e.g. when importing a hand-made fixture, whose
        free-text secrets have no earlier sensitive-keyed occurrence
        to learn from.
        """
        if isinstance(value, dict):
            for k, v in value.items():
                if self._is_sensitive(k):
                    self.register_value(v)
                else:
                    self.learn_from(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                self.learn_from(v)
        elif isinstance(value, str):
            # redact_text learns inline-assignment values as a side
            # effect; the redacted output is discarded here.
            self.redact_text(value)

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
                # Trailing sentence punctuation belongs to the surrounding
                # text, not the secret: "password=hunter2." learns
                # "hunter2" (and keeps the "." in the output).
                def _assign_repl(match, _pattern=pattern):
                    raw = match.group(4)
                    stripped = raw.rstrip(_ASSIGNMENT_TRAILING)
                    trailing = raw[len(stripped):]
                    self.register_secret(stripped)
                    return (f"{match.group(1)}{match.group(2)}"
                            f"{match.group(3)}{REDACTED}{trailing}")

                text = pattern.sub(_assign_repl, text)
            else:
                text = pattern.sub(replacement, text)
        # Learned/registered secrets last: longest first so a secret that
        # contains a shorter secret is scrubbed whole.
        for secret in sorted(self._known_secrets, key=len, reverse=True):
            if secret in text:
                text = text.replace(secret, REDACTED)
        # Secrets known only by digest+length (persisted learned set,
        # fresh store): scrub by hash-window matching.
        if self._hashed_secrets:
            text = self._scrub_hashed(text)
        return text
