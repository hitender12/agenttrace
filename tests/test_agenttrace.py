"""Tests for AgentTrace (stdlib unittest)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agenttrace import (  # noqa: E402
    FAKE_API_KEY, RecordRuntime, ReplayRuntime, ScriptedModel, Store,
    customer_support_agent, diff_runs, eval_exit_code, export_run,
    load_cases, run_eval,
)

REFUND_QUERY = "I want a refund for order ORD-1001"
SHIPPING_QUERY = "Where is my delivery for order ORD-2002?"


class AgentTraceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "at")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _record(self, query=REFUND_QUERY):
        model = ScriptedModel(api_key=FAKE_API_KEY)
        with RecordRuntime(self.store, "test", model=model,
                           api_key=FAKE_API_KEY,
                           metadata={"query": query}) as rt:
            answer = customer_support_agent(rt, query)
            run_id = rt.run_id
        return answer, run_id, model

    # -- recording -------------------------------------------------------
    def test_record_run_and_inspect_timeline(self):
        answer, run_id, _ = self._record()
        self.assertIn("refund", answer.lower())
        events = self.store.get_events(run_id)
        types = [e.type for e in events]
        for expected in ("policy_decision", "retrieval", "model_request",
                         "model_response", "tool_call", "tool_result",
                         "memory_write", "final_output"):
            self.assertIn(expected, types, f"missing event type {expected}")
        self.assertEqual([e.seq for e in events],
                         sorted(e.seq for e in events))
        self.assertEqual(len({e.trace_id for e in events}), 1)
        # OpenTelemetry-compatible ID formats.
        self.assertEqual(len(events[0].trace_id), 32)
        self.assertEqual(len(events[0].span_id), 16)

    # -- exact replay ------------------------------------------------------
    def test_exact_replay_reproduces_logic_without_model_calls(self):
        answer, run_id, model = self._record()
        calls_before = model.calls
        self.assertGreater(calls_before, 0)
        with ReplayRuntime(self.store, run_id, mode="exact",
                           model=model) as rt:
            replayed = customer_support_agent(rt, REFUND_QUERY)
            report = rt.report
            new_run_id = rt.run_id
        self.assertEqual(replayed, answer)
        self.assertEqual(model.calls, calls_before,
                         "exact replay must not call the model stub")
        self.assertEqual(report["mode"], "exact")
        replay_events = self.store.get_events(new_run_id)
        self.assertTrue(any(e.meta.get("replayed") for e in replay_events))

    def test_side_effect_tool_blocked_on_replay(self):
        _, run_id, model = self._record()
        with ReplayRuntime(self.store, run_id, mode="exact",
                           model=model) as rt:
            result = rt.tool("send_email",
                             {"to": "customer@example.com", "body": "x"},
                             side_effect=True)
            blocked = list(rt.report["blocked_side_effects"])
        self.assertEqual(result["status"], "blocked_on_replay")
        self.assertIn("send_email", blocked)

    def test_side_effect_allowed_with_explicit_approval(self):
        _, run_id, model = self._record()
        with ReplayRuntime(self.store, run_id, mode="live",
                           allow_side_effects=True, model=model) as rt:
            result = rt.tool("send_email",
                             {"to": "customer@example.com", "body": "x"},
                             side_effect=True)
            new_run_id = rt.run_id
        self.assertTrue(result.get("sent"))
        events = self.store.get_events(new_run_id)
        approved = [e for e in events
                    if e.meta.get("side_effect_approved")]
        self.assertTrue(approved, "approved side effect must be auditable")

    def test_hybrid_replay_calls_selected_tools_live(self):
        _, run_id, model = self._record()
        calls = {"get_order": 0}
        from agenttrace.runtime import TOOL_IMPLS
        original = TOOL_IMPLS["get_order"]

        def counting(args):
            calls["get_order"] += 1
            return original(args)

        TOOL_IMPLS["get_order"] = counting
        try:
            with ReplayRuntime(self.store, run_id, mode="hybrid",
                               hybrid_tools=["get_order"],
                               model=model) as rt:
                customer_support_agent(rt, REFUND_QUERY)
                report = rt.report
        finally:
            TOOL_IMPLS["get_order"] = original
        self.assertEqual(calls["get_order"], 1)
        self.assertGreater(report["live_calls"], 0)

    # -- redaction -----------------------------------------------------------
    def test_redaction_removes_secrets(self):
        _, run_id, _ = self._record()
        raw = Path(self.tmp.name).rglob("*")
        for path in raw:
            if path.is_file():
                self.assertNotIn(FAKE_API_KEY.encode(), path.read_bytes(),
                                 f"secret leaked into {path}")
        out = Path(self.tmp.name) / "fixture.json"
        export_run(self.store, run_id, out)
        self.assertNotIn(FAKE_API_KEY, out.read_text(encoding="utf-8"))
        # The redacted marker is present instead.
        self.assertIn("[REDACTED", out.read_text(encoding="utf-8"))

    # -- diff ------------------------------------------------------------------
    def test_diff_detects_changed_tool_argument(self):
        _, run_a, _ = self._record(REFUND_QUERY)
        _, run_b, _ = self._record(SHIPPING_QUERY)
        diff = diff_runs(self.store, run_a, run_b)
        self.assertTrue(diff["changed"])
        arg_changes = [c for c in diff["changes"]
                       if c["kind"] == "tool_args_changed"]
        self.assertTrue(arg_changes, "expected a changed tool argument")
        self.assertTrue(any("ORD-2002" in str(c["new"]) for c in arg_changes))

    def test_diff_reports_no_change_for_identical_runs(self):
        _, run_a, _ = self._record(REFUND_QUERY)
        _, run_b, _ = self._record(REFUND_QUERY)
        diff = diff_runs(self.store, run_a, run_b)
        self.assertFalse(diff["changed"])

    # -- regression / CI ---------------------------------------------------------
    def test_eval_passes_and_ci_fails_on_regression(self):
        cases = load_cases(Path(__file__).resolve().parents[1]
                           / "examples" / "agent_cases.json")
        report = run_eval(self.store, cases)
        self.assertEqual(report["failed"], 0, json.dumps(report, indent=2))
        self.assertEqual(eval_exit_code(report), 0)

        regressed = [{
            "name": "deliberately wrong expectation",
            "input": {"query": REFUND_QUERY},
            "expect": {"tool_sequence": ["search_kb"],
                       "output_contains": "spaceship"},
        }]
        bad = run_eval(self.store, regressed)
        self.assertEqual(bad["failed"], 1)
        self.assertEqual(eval_exit_code(bad), 1)

    # -- export -------------------------------------------------------------------
    def test_export_round_trip(self):
        _, run_id, _ = self._record()
        out = Path(self.tmp.name) / "run.json"
        export_run(self.store, run_id, out)
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["run"]["id"], run_id)
        self.assertGreater(len(data["events"]), 0)
        self.assertTrue(all("trace_id" in e for e in data["events"]))


if __name__ == "__main__":
    unittest.main()
