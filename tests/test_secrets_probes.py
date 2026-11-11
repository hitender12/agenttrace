"""Secrets-probe suite: the 10-probe redaction audit, as regression tests.

Reproduces the two independent QA audits' findings: secrets planted
under substring-style keys (``db_password``, ``auth_token``), in nested
JSON, in free text, in run metadata, in error messages and in tool
results must never appear in the database file, an artifact file, or
an exported fixture. Runs as part of the normal suite (``python -m
unittest discover -s tests``), so a future redaction regression is
caught by CI.

The probes are fake secrets, clearly marked as such.
"""
import re
import tempfile
import unittest
from pathlib import Path

from agenttrace import (
    FAKE_API_KEY, RecordRuntime, Redactor, ScriptedModel, Store,
    export_run,
)
from agenttrace.runtime import TOOL_IMPLS

# One distinct fake secret per probe.
DB_PASSWORD = "DbPass-9f2c7a1b"
AUTH_TOKEN = "AuthTok-77aa19c4"
FREE_TEXT_SECRET = "ZebraMoon42!"
NESTED_SECRET = "NestSecret-51bd08e3"
META_SECRET = "MetaSecret-88cc02d1"
JWT_SECRET = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
              ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
              ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
HEX_SECRET = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
PEM_SECRET = ("-----BEGIN PRIVATE KEY-----\n"
              "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
              "-----END PRIVATE KEY-----")
INLINE_SECRET = "InlinePass-44dd91e7"
ARTIFACT_SECRET = "ArtifactSecret-63fe07a2"
X_API_KEY = "XApiKey-19ab34cd"
CLIENT_SECRET = "ClientSec-72ef08ab"

ALL_PROBE_SECRETS = [
    DB_PASSWORD, AUTH_TOKEN, FREE_TEXT_SECRET, NESTED_SECRET, META_SECRET,
    JWT_SECRET, HEX_SECRET, PEM_SECRET, INLINE_SECRET, ARTIFACT_SECRET,
    X_API_KEY, CLIENT_SECRET,
]


class SecretsProbeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / "at"
        self.store = Store(self.base)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _store_bytes(self) -> bytes:
        blob = b""
        for path in self.base.rglob("*"):
            if path.is_file():
                blob += path.read_bytes()
        return blob

    def _assert_no_leak(self, run_id, label=""):
        blob = self._store_bytes()
        for secret in ALL_PROBE_SECRETS:
            self.assertNotIn(secret.encode(), blob,
                             f"{label}: {secret[:12]}... leaked into store")
        out = Path(self.tmp.name) / "fixture.json"
        export_run(self.store, run_id, out)
        text = out.read_text(encoding="utf-8")
        for secret in ALL_PROBE_SECRETS:
            self.assertNotIn(secret, text,
                             f"{label}: {secret[:12]}... leaked into export")
        return text

    def _record_probes(self):
        """Plant every probe secret in the places the audits found leaks."""
        TOOL_IMPLS["leaky_tool"] = lambda args: {
            "status": "ok",
            # Secret in a tool *result* (free text).
            "detail": f"connected with {FREE_TEXT_SECRET}",
        }
        try:
            model = ScriptedModel(api_key=FAKE_API_KEY)
            with RecordRuntime(self.store, "probe", model=model,
                               api_key=FAKE_API_KEY,
                               # Probe 5: secret in run metadata.
                               metadata={"db_password": META_SECRET,
                                         "owner": "qa"}) as rt:
                rt.tool("get_order", {
                    "order_id": "ORD-1001",
                    # Probe 1: substring-style sensitive keys.
                    "db_password": DB_PASSWORD,
                    "auth_token": AUTH_TOKEN,
                    "x-api-key": X_API_KEY,
                    "clientSecret": CLIENT_SECRET,
                    # Probe 3: the free-text secret, first seen under a
                    # sensitive key so the redactor learns it.
                    "recovery_secret": FREE_TEXT_SECRET,
                    "backup_password": ARTIFACT_SECRET,
                    # Probe 4: secret in nested JSON.
                    "credentials": {"db_password": NESTED_SECRET,
                                    "hosts": [f"db.internal/{NESTED_SECRET}"]},
                })
                # Probe 6: secret in an error message.
                rt.error("db_connect",
                         f"login failed for token {FREE_TEXT_SECRET}")
                # Probe 7: secret in a tool result.
                rt.tool("leaky_tool", {"order_id": "ORD-1001"})
                # Probe 8: JWT in free text.
                rt.memory_write("auth", f"session jwt: {JWT_SECRET}")
                # Probe 9: long hex + PEM block in free text.
                rt.memory_write("keys", f"hex {HEX_SECRET}\n{PEM_SECRET}")
                # Probe 10: inline password= assignment (also teaches the
                # redactor the value for later bare occurrences).
                rt.memory_write("config", f"password={INLINE_SECRET}")
                rt.memory_write("later", f"again {INLINE_SECRET} in text")
                # Harder: secret inside a >4 KB payload (artifact spill).
                rt.memory_write(
                    "big",
                    "x" * 5000 + f" hidden {ARTIFACT_SECRET} "
                    f"and {FREE_TEXT_SECRET}")
                # Harder: secret as a dict key.
                rt.memory_write("by_key", {FREE_TEXT_SECRET: "value"})
                return rt.run_id
        finally:
            del TOOL_IMPLS["leaky_tool"]

    def test_ten_probe_suite_zero_leaks(self):
        """The audit's 10 probes: 0 leaks in the store or the export."""
        run_id = self._record_probes()
        text = self._assert_no_leak(run_id, label="10-probe")
        self.assertIn("[REDACTED", text)

    def test_learned_secret_scrubbed_from_later_free_text(self):
        """A secret learned from `password=X` is scrubbed from later text."""
        redactor = Redactor()
        out = redactor.redact_text(
            f"password={INLINE_SECRET} then bare {INLINE_SECRET} here")
        self.assertNotIn(INLINE_SECRET, out)
        self.assertIn("[REDACTED]", out)

    def test_sensitive_key_substrings(self):
        """Substring key matching: spelling variants all redact."""
        redactor = Redactor()
        payload = {
            "db_password": "v1", "DB_PASSWORD": "v2",
            "auth_token": "v3", "x-api-key": "v4",
            "clientSecret": "v5", "sessionToken": "v6",
            "privateKey": "v7", "access_key": "v8",
            "credential": "v9", "order_id": "ORD-1001",
        }
        out = redactor.redact(payload)
        for key in payload:
            if key == "order_id":
                self.assertEqual(out[key], "ORD-1001")
            else:
                self.assertEqual(out[key], "[REDACTED]", key)

    def test_registered_secret_is_scrubbed_from_free_text(self):
        redactor = Redactor()
        redactor.register_secret(FREE_TEXT_SECRET)
        out = redactor.redact_text(
            f"the code is {FREE_TEXT_SECRET}, do not share")
        self.assertNotIn(FREE_TEXT_SECRET, out)
        self.assertIn("[REDACTED]", out)

    def test_unregistered_shapeless_secret_is_a_documented_limitation(self):
        """Honest boundary: a string never seen under a sensitive key,
        never registered, and matching no known shape cannot be
        distinguished from ordinary text — and is NOT redacted. This
        test pins the limitation so it is a deliberate contract, not
        an accidental gap. See redaction.py module docstring."""
        redactor = Redactor()
        never_seen = "QuixoticPlum77?"
        out = redactor.redact_text(f"my code is {never_seen}")
        self.assertIn(never_seen, out)


if __name__ == "__main__":
    unittest.main()
