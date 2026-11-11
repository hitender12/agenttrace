"""Edge-case tests for AgentTrace: redaction, failed-tool replay,
eval CI failure path, and a large-run performance smoke test.

Run with:  python -m unittest discover -s tests
"""
import json
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agenttrace import (  # noqa: E402
    FAKE_API_KEY, RecordRuntime, Redactor, ReplayRuntime, ScriptedModel,
    Store, customer_support_agent, eval_exit_code, export_run, load_cases,
    run_eval,
)
from agenttrace.events import EventType  # noqa: E402
from agenttrace.runtime import TOOL_IMPLS  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

# Distinct, clearly fake secrets — one per pattern the redactor handles.
# None of these is a real credential.
OPENAI_KEY = "sk-" + "A" * 20
GITHUB_TOKEN = "ghp_" + "b" * 36
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
SLACK_TOKENS = {
    "bot": "xoxb-123456789012-abcdefghij",
    "user": "xoxp-123456789012-abcdefghij",
    "app": "xoxa-123456789012-abcdefghij",
    "refresh": "xoxr-123456789012-abcdefghij",
    "session": "xoxs-123456789012-abcdefghij",
}
BEARER_TOKEN = "Bearer abcdefghij1234567890"
ALL_FAKE_SECRETS = [OPENAI_KEY, GITHUB_TOKEN, AWS_KEY, BEARER_TOKEN,
                    *SLACK_TOKENS.values()]


class RedactionEdgeCaseTestCase(unittest.TestCase):
    def test_every_secret_pattern_is_redacted_in_free_text(self):
        redactor = Redactor()
        cases = [
            (f"key={OPENAI_KEY}", "sk-"),
            (f"token={GITHUB_TOKEN}", "ghp_"),
            (f"aws={AWS_KEY}", "AKIA"),
            (f"auth: {BEARER_TOKEN}", "Bearer abcdefghij"),
            ("lowercase bearer abcdefghij1234567890", "bearer abcdefghij"),
        ]
        for text, leaked_fragment in cases:
            with self.subTest(text=text):
                out = redactor.redact_text(text)
                self.assertNotIn(leaked_fragment, out)
                self.assertIn("[REDACTED", out)
        for name, token in SLACK_TOKENS.items():
            with self.subTest(slack_kind=name):
                out = redactor.redact_text(f"slack token {token} leaked")
                self.assertNotIn(token, out)
                self.assertIn("[REDACTED_TOKEN]", out)

    def test_secret_patterns_do_not_clobber_surrounding_text(self):
        redactor = Redactor()
        out = redactor.redact_text(
            f"call support with {OPENAI_KEY} and reference ORD-1001")
        self.assertIn("call support with", out)
        self.assertIn("ORD-1001", out)
        self.assertNotIn(OPENAI_KEY, out)

    def test_every_sensitive_key_is_redacted_case_insensitively(self):
        redactor = Redactor()
        payload = {
            "api_key": "k1", "API_KEY": "k2", "ApiKey": "k3",
            "password": "p1", "Passwd": "p2", "secret": "s1",
            "client_secret": "cs1", "token": "t1",
            "access_token": "at1", "refresh_token": "rt1",
            "authorization": "auth1", "bearer": "b1",
            "private_key": "pk1",
            # Non-sensitive keys must survive untouched.
            "order_id": "ORD-1001", "subject": "Your refund",
        }
        out = redactor.redact(payload)
        for key in ("api_key", "API_KEY", "ApiKey", "password", "Passwd",
                    "secret", "client_secret", "token", "access_token",
                    "refresh_token", "authorization", "bearer", "private_key"):
            self.assertEqual(out[key], "[REDACTED]", key)
        self.assertEqual(out["order_id"], "ORD-1001")
        self.assertEqual(out["subject"], "Your refund")

    def test_redaction_recurses_into_nested_dicts_lists_and_tuples(self):
        redactor = Redactor()
        payload = {"outer": [{"token": GITHUB_TOKEN},
                             ("nested", f"key {OPENAI_KEY} here")]}
        out = redactor.redact(payload)
        self.assertEqual(out["outer"][0]["token"], "[REDACTED]")
        self.assertNotIn(OPENAI_KEY, out["outer"][1][1])
        self.assertIn("[REDACTED", out["outer"][1][1])
        # Non-string scalars pass through unchanged.
        self.assertEqual(redactor.redact({"n": 42, "f": 1.5, "b": True,
                                          "z": None}),
                         {"n": 42, "f": 1.5, "b": True, "z": None})

    def test_redact_does_not_mutate_the_input(self):
        redactor = Redactor()
        payload = {"token": GITHUB_TOKEN, "items": [OPENAI_KEY]}
        redactor.redact(payload)
        self.assertEqual(payload["token"], GITHUB_TOKEN)
        self.assertEqual(payload["items"], [OPENAI_KEY])

    def test_extra_keys_and_extra_patterns(self):
        redactor = Redactor(
            extra_keys={"internal_id"},
            extra_patterns=[(re.compile(r"SECRET-\d+"), "[REDACTED_CUSTOM]")])
        out = redactor.redact({"internal_id": "abc",
                               "note": "code SECRET-12345 here"})
        self.assertEqual(out["internal_id"], "[REDACTED]")
        self.assertIn("[REDACTED_CUSTOM]", out["note"])
        self.assertNotIn("SECRET-12345", out["note"])

    def test_short_token_below_pattern_minimum_is_left_alone(self):
        # Documents the current minimum-length rule: "sk-" needs 16+
        # characters after it. Short fragments are not secret-shaped.
        redactor = Redactor()
        self.assertEqual(redactor.redact_text("sk-abc"), "sk-abc")

    # -- redaction through the store / export -------------------------
    def test_no_secret_pattern_survives_in_store_or_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "at")
            try:
                model = ScriptedModel(api_key=OPENAI_KEY)
                with RecordRuntime(store, "secret-soup", model=model,
                                   api_key=OPENAI_KEY) as rt:
                    rt.tool("get_order", {
                        "order_id": "ORD-1001",
                        "github_token": GITHUB_TOKEN,
                        "aws_key": AWS_KEY,
                        "slack": SLACK_TOKENS["bot"],
                        "password": "hunter2",
                    })
                    rt.final_output(
                        f"escalated with {BEARER_TOKEN} "
                        f"and slack {SLACK_TOKENS['user']}")
                    run_id = rt.run_id
                # Raw bytes of everything the store wrote.
                for path in Path(tmp).rglob("*"):
                    if path.is_file():
                        blob = path.read_bytes()
                        for secret in ALL_FAKE_SECRETS:
                            self.assertNotIn(
                                secret.encode(), blob,
                                f"{secret[:8]}... leaked into {path}")
                        self.assertNotIn(b"hunter2", blob)
                out = Path(tmp) / "fixture.json"
                export_run(store, run_id, out)
                text = out.read_text(encoding="utf-8")
                for secret in ALL_FAKE_SECRETS:
                    self.assertNotIn(secret, text)
                self.assertNotIn("hunter2", text)
                self.assertIn("[REDACTED", text)
            finally:
                store.close()


class FailedToolCallReplayTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "at")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _agent_unknown_tool_caught(self, rt, query="refund ORD-1001"):
        """Calls a tool that does not exist; handles the failure."""
        rt.policy_decision("pii_check", {"query": query},
                           {"allowed": True, "reason": "ok"})
        try:
            rt.tool("nonexistent_tool", {"order_id": "ORD-1001"})
        except KeyError:
            rt.error("tool_failed", "no tool named 'nonexistent_tool'")
            return rt.final_output("I could not look up your order.")
        return rt.final_output("looked up")

    def _agent_unknown_tool_uncaught(self, rt, query="refund ORD-1001"):
        rt.policy_decision("pii_check", {"query": query},
                           {"allowed": True, "reason": "ok"})
        rt.tool("nonexistent_tool", {"order_id": "ORD-1001"})
        return rt.final_output("unreachable")

    def test_failed_tool_call_is_recorded_with_error_event(self):
        model = ScriptedModel(api_key=FAKE_API_KEY)
        with RecordRuntime(self.store, "failed-tool", model=model,
                           api_key=FAKE_API_KEY) as rt:
            answer = self._agent_unknown_tool_caught(rt)
            run_id = rt.run_id
        self.assertIn("could not look up", answer)
        events = self.store.get_events(run_id)
        self.assertTrue(any(e.type == EventType.ERROR.value for e in events),
                        "failed tool call must leave an error event")
        self.assertTrue(any(e.name == "unknown_tool" for e in events
                            if e.type == EventType.ERROR.value))
        self.assertTrue(any(e.type == EventType.TOOL_CALL.value
                            and e.name == "nonexistent_tool" for e in events))
        # The agent handled the failure, so the run still completes.
        self.assertEqual(self.store.get_run(run_id)["status"], "completed")

    def test_replay_of_failed_tool_call_reproduces_the_failure(self):
        model = ScriptedModel(api_key=FAKE_API_KEY)
        with RecordRuntime(self.store, "failed-tool", model=model,
                           api_key=FAKE_API_KEY) as rt:
            original = self._agent_unknown_tool_caught(rt)
            run_id = rt.run_id
        calls_before = model.calls
        with ReplayRuntime(self.store, run_id, mode="exact",
                           model=model) as rt:
            replayed = self._agent_unknown_tool_caught(rt)
            report = rt.report
            replay_run_id = rt.run_id
        self.assertEqual(replayed, original)
        self.assertEqual(model.calls, calls_before,
                         "exact replay must not call the model stub")
        # The failed call has no recorded result, so replay falls back
        # to a live call, fails the same way, and says so in the report.
        self.assertEqual(report["fallback_live_calls"], 1)
        replay_events = self.store.get_events(replay_run_id)
        self.assertTrue(any(e.type == EventType.ERROR.value
                            for e in replay_events),
                        "replay must record the reproduced failure")

    def test_uncaught_tool_failure_marks_the_run_failed(self):
        model = ScriptedModel(api_key=FAKE_API_KEY)
        with self.assertRaises(KeyError):
            with RecordRuntime(self.store, "failed-tool", model=model,
                               api_key=FAKE_API_KEY) as rt:
                run_id = rt.run_id
                self._agent_unknown_tool_uncaught(rt)
        self.assertEqual(self.store.get_run(run_id)["status"], "failed")
        events = self.store.get_events(run_id)
        self.assertTrue(any(e.type == EventType.ERROR.value for e in events))
        # Replay reproduces the same failure and the same failed status.
        with self.assertRaises(KeyError):
            with ReplayRuntime(self.store, run_id, mode="exact",
                               model=model) as rt:
                replay_run_id = rt.run_id
                self._agent_unknown_tool_uncaught(rt)
        self.assertEqual(self.store.get_run(replay_run_id)["status"],
                         "failed")

    def test_tool_returning_error_payload_replays_exactly(self):
        calls = {"n": 0}

        def flaky(args):
            calls["n"] += 1
            return {"status": "error", "code": "upstream_503",
                    "message": "order service unavailable"}

        TOOL_IMPLS["flaky_tool"] = flaky
        try:
            model = ScriptedModel(api_key=FAKE_API_KEY)

            def agent(rt):
                rt.policy_decision("pii_check", {"query": "x"},
                                   {"allowed": True, "reason": "ok"})
                result = rt.tool("flaky_tool", {"order_id": "ORD-1001"})
                return rt.final_output(
                    "failed" if result.get("status") == "error" else "ok")

            with RecordRuntime(self.store, "flaky", model=model,
                               api_key=FAKE_API_KEY) as rt:
                original = agent(rt)
                run_id = rt.run_id
            self.assertEqual(calls["n"], 1)
            self.assertEqual(original, "failed")
            # Remove the implementation: exact replay must not need it.
            del TOOL_IMPLS["flaky_tool"]
            with ReplayRuntime(self.store, run_id, mode="exact",
                               model=model) as rt:
                replayed = agent(rt)
                report = rt.report
            self.assertEqual(replayed, original)
            self.assertEqual(calls["n"], 1,
                             "exact replay must not re-execute the tool")
            self.assertEqual(report["fallback_live_calls"], 0)
        finally:
            TOOL_IMPLS.pop("flaky_tool", None)


class EvalCiFailurePathTestCase(unittest.TestCase):
    def _run_cli_eval(self, cases, ci=True):
        with tempfile.TemporaryDirectory() as tmp:
            cases_path = Path(tmp) / "cases.json"
            cases_path.write_text(json.dumps({"cases": cases}),
                                  encoding="utf-8")
            cmd = [sys.executable, "-m", "agenttrace", "eval",
                   str(cases_path), "--db", str(Path(tmp) / "at")]
            if ci:
                cmd.append("--ci")
            proc = subprocess.run(cmd, cwd=str(REPO), capture_output=True,
                                  text=True, timeout=120)
            return proc

    def test_ci_mode_exits_zero_when_all_cases_pass(self):
        cases = load_cases(REPO / "examples" / "agent_cases.json")
        proc = self._run_cli_eval(cases, ci=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("3 passed, 0 failed", proc.stdout)
        self.assertIn("CI mode: exit 0", proc.stdout)

    def test_ci_mode_exits_nonzero_on_regression(self):
        regressed = [{
            "name": "deliberately wrong expectation",
            "input": {"query": "I want a refund for order ORD-1001"},
            "expect": {"tool_sequence": ["search_kb"],
                       "output_contains": "spaceship"},
        }]
        proc = self._run_cli_eval(regressed, ci=True)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("[FAIL]", proc.stdout)
        self.assertIn("0 passed, 1 failed", proc.stdout)
        self.assertIn("CI mode: exit 1", proc.stdout)

    def test_ci_mode_reports_which_check_regressed(self):
        regressed = [{
            "name": "wrong tool sequence",
            "input": {"query": "I want a refund for order ORD-1001"},
            "expect": {"tool_sequence": ["get_order", "get_order"]},
        }]
        proc = self._run_cli_eval(regressed, ci=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("failed check tool_sequence", proc.stdout)

    def test_eval_without_ci_exits_zero_even_on_regression(self):
        # CI mode is just the exit code: without --ci the command
        # reports the failure but does not fail the build.
        regressed = [{
            "name": "deliberately wrong expectation",
            "input": {"query": "I want a refund for order ORD-1001"},
            "expect": {"output_contains": "spaceship"},
        }]
        proc = self._run_cli_eval(regressed, ci=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("[FAIL]", proc.stdout)

    def test_eval_exit_code_matches_report(self):
        cases = load_cases(REPO / "examples" / "agent_cases.json")
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "at")
            try:
                report = run_eval(store, cases)
            finally:
                store.close()
        self.assertEqual(report["failed"], 0)
        self.assertEqual(eval_exit_code(report), 0)


class LargeRunPerformanceTestCase(unittest.TestCase):
    # Generous bounds for a shared CI runner: the point is to catch a
    # pathological blow-up (e.g. quadratic replay), not to benchmark
    # to the millisecond.
    EVENT_COUNT = 500
    TIME_BUDGET_S = 10.0

    def test_record_and_replay_several_hundred_events(self):
        tool_calls = self.EVENT_COUNT // 2  # 250 calls -> 500 events
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "at")
            try:
                model = ScriptedModel(api_key=FAKE_API_KEY)

                def bulk_agent(rt):
                    rt.policy_decision("bulk", {"n": tool_calls},
                                       {"allowed": True, "reason": "ok"})
                    for i in range(tool_calls):
                        rt.tool("get_order", {"order_id": f"ORD-{1000 + i}"})
                    rt.memory_write("checked", tool_calls)
                    return rt.final_output(f"checked {tool_calls} orders")

                started = time.perf_counter()
                with RecordRuntime(store, "large-run", model=model,
                                   api_key=FAKE_API_KEY) as rt:
                    original = bulk_agent(rt)
                    run_id = rt.run_id
                record_s = time.perf_counter() - started

                events = store.get_events(run_id)
                self.assertGreaterEqual(len(events), self.EVENT_COUNT)
                self.assertEqual([e.seq for e in events],
                                 sorted(e.seq for e in events))

                started = time.perf_counter()
                with ReplayRuntime(store, run_id, mode="exact",
                                   model=model) as rt:
                    replayed = bulk_agent(rt)
                    report = rt.report
                replay_s = time.perf_counter() - started

                self.assertEqual(replayed, original)
                self.assertEqual(report["replayed_calls"], tool_calls)
                self.assertEqual(report["fallback_live_calls"], 0)
                self.assertLess(record_s, self.TIME_BUDGET_S,
                                f"recording {len(events)} events took "
                                f"{record_s:.2f}s")
                self.assertLess(replay_s, self.TIME_BUDGET_S,
                                f"replaying {len(events)} events took "
                                f"{replay_s:.2f}s")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
