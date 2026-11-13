"""Round-2 fix regressions: the re-audit's 22 leaks out of 41 checks.

Each test reproduces one re-audit finding with a fresh probe (new key
spellings, new secret placements, new CLI failure modes) and pins the
fix, so a future regression is caught by CI. Fake secrets only.

Covers:
  A  key-spelling gaps (sessionid, db_pass, encryption_key, ...)
  B  retroactive learning (persisted, hashed; fresh-store export)
  C  run/event names redacted at write time
  D  memory_write key-name trap
  E  inline-assignment trailing punctuation
  F  import re-redaction (two-pass, learned secrets persisted)
  G  artifact files scrubbed
  H  generic-value stoplist (no over-redaction)
  I  "never a traceback" (corrupt store, bad cases, deep YAML, bad import)
  J  YAML duplicate keys inside sequence items
  K  replay retrieve() fallback accounting
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agenttrace import (
    FAKE_API_KEY, RecordRuntime, Redactor, ScriptedModel, Store,
    export_run,
)
from agenttrace.demo_agent import customer_support_agent
from agenttrace.events import Event, now_iso
from agenttrace.importer import import_run
from agenttrace.redaction import is_sensitive_key
from agenttrace.replay import ReplayRuntime
from agenttrace.yamlmini import YamlSubsetError, parse_yaml_subset

SECRET_A = "Round2Secret-1a2b3c4d"
SECRET_B = "Round2Secret-5e6f7a8b"
SECRET_C = "Round2Secret-9c0d1e2f"


class Round2TestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / "at"
        self.store = Store(self.base)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _store_bytes(self, base=None) -> bytes:
        blob = b""
        for path in Path(base or self.base).rglob("*"):
            if path.is_file():
                blob += path.read_bytes()
        return blob

    # -- A: key spellings -------------------------------------------------
    def test_key_spelling_gaps(self):
        for key in ("sessionid", "session_id", "sessionId", "db_pass",
                    "dbpass", "encryption_key", "signing_key", "csrf",
                    "pass", "passcode"):
            self.assertTrue(is_sensitive_key(key), key)
        # ... but not ordinary words that merely contain "pass".
        for key in ("bypass", "compass", "passage", "order_id", "username"):
            self.assertFalse(is_sensitive_key(key), key)

    def test_db_pass_assignment_learns(self):
        redactor = Redactor()
        out = redactor.redact_text(
            f"db_pass={SECRET_A} and later bare {SECRET_A}")
        self.assertNotIn(SECRET_A, out)

    # -- B: retroactive learning ------------------------------------------
    def _record_before_then_learn(self, secret):
        with RecordRuntime(self.store, "retro", model=ScriptedModel(),
                           api_key=None) as rt:
            rt.memory_write("note", f"the code is {secret} do not share")
            rt.memory_write("deep", {"a": {"b": {"c": f"val {secret}"}}})
            rt.memory_write("big", "x" * 5000 + f" hidden {secret}")
            rt.memory_write("learn", {"db_password": secret})
            return rt.run_id

    def test_retroactive_learning_cleans_db_and_export(self):
        run_id = self._record_before_then_learn(SECRET_A)
        blob = self._store_bytes()
        self.assertNotIn(SECRET_A.encode(), blob,
                         "before-learning occurrence still in store")
        out = Path(self.tmp.name) / "retro.json"
        export_run(self.store, run_id, out)
        self.assertNotIn(SECRET_A, out.read_text())

    def test_retroactive_export_from_fresh_store(self):
        run_id = self._record_before_then_learn(SECRET_A)
        self.store.close()
        fresh = Store(self.base)
        try:
            out = Path(self.tmp.name) / "fresh.json"
            export_run(fresh, run_id, out)
            self.assertNotIn(SECRET_A, out.read_text())
        finally:
            fresh.close()

    def test_retroactive_export_without_finish(self):
        """Crash case: the run never finished, so no rescrub ran — the
        export must still be clean, from the persisted learned set."""
        run_id = self.store.create_run("crash")
        for seq, payload in enumerate((
                {"value": f"code {SECRET_B}"},
                {"value": {"db_password": SECRET_B}}), start=1):
            self.store.add_event(run_id, Event(
                id=f"evt_{seq:04d}_aaaaaaaa", run_id=run_id, seq=seq,
                trace_id="a" * 32, span_id="b" * 16, parent_span_id=None,
                type="memory_write", name="memory_write",
                started_at=now_iso(), input={"key": "note"},
                output=payload, meta={}))
        self.store.close()
        fresh = Store(self.base)
        try:
            out = Path(self.tmp.name) / "crash.json"
            export_run(fresh, run_id, out)
            self.assertNotIn(SECRET_B, out.read_text())
        finally:
            fresh.close()

    def test_learned_secrets_stored_hashed_not_plaintext(self):
        run_id = self._record_before_then_learn(SECRET_A)
        rows = self.store._conn.execute(
            "SELECT secret_hash, secret_len FROM learned_secrets"
            " WHERE run_id = ?", (run_id,)).fetchall()
        self.assertTrue(rows, "no learned secrets persisted")
        digest = hashlib.sha256(SECRET_A.encode()).hexdigest()
        self.assertIn((digest, len(SECRET_A)), [(r[0], r[1]) for r in rows])
        # And the plaintext never appears anywhere in the store files.
        self.assertNotIn(SECRET_A.encode(), self._store_bytes())

    def test_hashed_secret_scrubs_by_hash(self):
        redactor = Redactor()
        digest = hashlib.sha256(SECRET_C.encode()).hexdigest()
        redactor.register_hashed_secret(digest, len(SECRET_C))
        out = redactor.redact_text(f"the code is {SECRET_C} here")
        self.assertNotIn(SECRET_C, out)
        self.assertIn("[REDACTED]", out)

    # -- C: names -----------------------------------------------------------
    def test_names_redacted(self):
        jwt = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
               ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
               ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
        with RecordRuntime(self.store, f"run-{jwt}",
                           model=ScriptedModel()) as rt:
            rt.error(f"failed {SECRET_A}", "something failed")
            rt.memory_write("later", {"db_password": SECRET_A})
            run_id = rt.run_id
        blob = self._store_bytes()
        self.assertNotIn(jwt.encode(), blob, "JWT in run name leaked")
        self.assertNotIn(SECRET_A.encode(), blob,
                         "late-learned secret in event name leaked")
        run = self.store.get_run(run_id)
        self.assertNotIn(jwt, run["name"])

    # -- D: memory_write key trap -------------------------------------------
    def test_memory_write_sensitive_key_learns_value(self):
        with RecordRuntime(self.store, "mw", model=ScriptedModel()) as rt:
            rt.memory_write("db_password", SECRET_A)
            rt.memory_write("note", f"later {SECRET_A} here")
            run_id = rt.run_id
        blob = self._store_bytes()
        self.assertNotIn(SECRET_A.encode(), blob)

    # -- E: trailing punctuation --------------------------------------------
    def test_assignment_trailing_period(self):
        redactor = Redactor()
        out = redactor.redact_text(
            f"password={SECRET_A}. then bare {SECRET_A} here")
        self.assertNotIn(SECRET_A, out)
        self.assertTrue(out.startswith("password=[REDACTED]."),
                        f"period lost: {out!r}")

    # -- F: import -----------------------------------------------------------
    def _handmade_fixture(self, path):
        fixture = {
            "agenttrace_version": 1,
            "run": {"id": "run_fixture00001", "name": "handmade",
                    "mode": "record", "replay_of": None,
                    "status": "completed",
                    "started_at": "2026-11-11T00:00:00+00:00",
                    "ended_at": None, "metadata": {}},
            "events": [{
                "id": "evt_0001_abcd1234", "run_id": "run_fixture00001",
                "seq": 1, "trace_id": "a" * 32, "span_id": "b" * 16,
                "parent_span_id": None, "type": "memory_write",
                "name": "memory_write",
                "started_at": "2026-11-11T00:00:00+00:00",
                "ended_at": None, "duration_ms": None, "status": "ok",
                "input": {"key": "config"},
                "output": {"value": f"free text {SECRET_A} here",
                           "db_pass": SECRET_B,
                           "sessionid": SECRET_C},
                "meta": {},
            }, {
                "id": "evt_0002_abcd1234", "run_id": "run_fixture00001",
                "seq": 2, "trace_id": "a" * 32, "span_id": "b" * 16,
                "parent_span_id": None, "type": "memory_write",
                "name": "memory_write",
                "started_at": "2026-11-11T00:00:00+00:00",
                "ended_at": None, "duration_ms": None, "status": "ok",
                "input": {"key": "config2"},
                "output": {"value": f"db_password={SECRET_A}"},
                "meta": {},
            }],
        }
        Path(path).write_text(json.dumps(fixture))
        return path

    def test_handmade_fixture_import_scrubs_everything(self):
        fix_path = self._handmade_fixture(Path(self.tmp.name) / "hm.json")
        run_id = import_run(self.store, fix_path)
        blob = self._store_bytes()
        for secret in (SECRET_A, SECRET_B, SECRET_C):
            self.assertNotIn(secret.encode(), blob,
                             f"{secret[:12]}... leaked on import")
        out = Path(self.tmp.name) / "imported.json"
        export_run(self.store, run_id, out)
        for secret in (SECRET_A, SECRET_B, SECRET_C):
            self.assertNotIn(secret, out.read_text(),
                             f"{secret[:12]}... leaked on re-export")

    # -- H: stoplist ----------------------------------------------------------
    def test_generic_values_not_learned(self):
        redactor = Redactor()
        redactor.redact({"db_password": "password"})
        self.assertNotIn("password", redactor.known_secrets)
        out = redactor.redact_text("enter your password here")
        self.assertEqual(out, "enter your password here")
        redactor.redact({"api_key": "secret", "token": "token"})
        out = redactor.redact_text("the secret token is here")
        self.assertEqual(out, "the secret token is here")

    # -- I: never a traceback ---------------------------------------------------
    def _run_cli(self, *argv):
        return subprocess.run(
            [sys.executable, "-m", "agenttrace", *argv],
            cwd="/home/hatch/workspace/github-projects/agenttrace",
            capture_output=True, text=True)

    def _assert_clean_error(self, proc):
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("error:", proc.stderr)

    def test_cli_corrupt_store_is_clean_error(self):
        bad = Path(self.tmp.name) / "bad"
        bad.mkdir()
        (bad / "agenttrace.db").write_bytes(b"not sqlite at all")
        self._assert_clean_error(self._run_cli("list", "--db", str(bad)))

    def test_cli_json_without_cases_key_is_clean_error(self):
        cases = Path(self.tmp.name) / "cases.json"
        cases.write_text(json.dumps({"name": "oops"}))
        self._assert_clean_error(
            self._run_cli("eval", str(cases), "--db",
                          str(Path(self.tmp.name) / "db")))

    def test_yaml_2000_deep_is_clean_error(self):
        deep = "a:\n" + "".join("  " * i + f"k{i}:\n"
                                for i in range(1, 2000)) + "  " * 2000 + "v: 1\n"
        with self.assertRaises(YamlSubsetError):
            parse_yaml_subset(deep)

    def test_cli_import_missing_event_fields_is_clean_error(self):
        bad = {"agenttrace_version": 1,
               "run": {"id": "run_x", "name": "x", "mode": "record",
                       "status": "completed",
                       "started_at": "2026-11-11T00:00:00+00:00",
                       "metadata": {}},
               "events": [{"type": "memory_write", "name": "memory_write"}]}
        path = Path(self.tmp.name) / "bad.json"
        path.write_text(json.dumps(bad))
        self._assert_clean_error(
            self._run_cli("import", str(path), "--db",
                          str(Path(self.tmp.name) / "db")))

    def test_cli_import_wrong_field_types_is_clean_error(self):
        bad = {"agenttrace_version": 1,
               "run": {"id": "run_x", "name": "x", "mode": "record",
                       "status": "completed",
                       "started_at": "2026-11-11T00:00:00+00:00",
                       "metadata": {}},
               "events": [{"id": "e1", "run_id": "run_x",
                           "seq": "not-an-int",
                           "trace_id": "a" * 32, "span_id": "b" * 16,
                           "type": "memory_write", "name": "memory_write",
                           "started_at": "2026-11-11T00:00:00+00:00",
                           "status": "ok", "input": {}, "output": {},
                           "meta": {}}]}
        path = Path(self.tmp.name) / "bad2.json"
        path.write_text(json.dumps(bad))
        self._assert_clean_error(
            self._run_cli("import", str(path), "--db",
                          str(Path(self.tmp.name) / "db2")))

    def test_cli_import_directory_is_clean_error(self):
        self._assert_clean_error(
            self._run_cli("import", self.tmp.name, "--db",
                          str(Path(self.tmp.name) / "db3")))

    # -- J: YAML duplicate keys in sequence items -----------------------------
    def test_yaml_duplicate_key_in_sequence_item(self):
        with self.assertRaises(YamlSubsetError):
            parse_yaml_subset(
                "cases:\n  - name: a\n    name: b\n    input: x\n")

    # -- K: replay retrieve() fallback -----------------------------------------
    def test_retrieve_fallback_counted_and_labelled(self):
        with RecordRuntime(self.store, "k", model=ScriptedModel(),
                           api_key=FAKE_API_KEY) as rt:
            customer_support_agent(rt, "I want a refund for order ORD-1001")
            run_id = rt.run_id

        def agent(rt2):
            # First retrieve consumes the recorded one; the second has
            # nothing recorded and must fall back to a live KB lookup.
            rt2.retrieve("refund policy please")
            rt2.retrieve("shipping status please")
            return rt2.final_output("done")

        with ReplayRuntime(self.store, run_id, mode="exact") as rt:
            agent(rt)
            report = rt.report
            replay_run = rt.run_id
        self.assertEqual(report["fallback_live_calls"], 1)
        self.assertEqual(report["live_calls"], 1)
        fallback = [e for e in self.store.get_events(replay_run)
                    if e.type == "retrieval" and e.meta.get("live")]
        self.assertEqual(len(fallback), 1)
        self.assertNotIn("replayed", fallback[0].meta)


if __name__ == "__main__":
    unittest.main()
