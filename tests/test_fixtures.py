"""Tests for the demo agent scenario fixtures in fixtures/.

Each fixture is a self-contained agent that can be recorded and
replayed by the existing engine (RecordRuntime / ReplayRuntime), and
each ships with an exported JSON snapshot of a recorded run.

Run with:  python -m unittest discover -s tests
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agenttrace import (  # noqa: E402
    FAKE_API_KEY, RecordRuntime, ReplayRuntime, ScriptedModel, Store,
)
from agenttrace.events import EventType  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "fixtures"


def _load_fixture(name: str):
    """Load a fixture module from fixtures/ without making it a package."""
    path = FIXTURES / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


srm = _load_fixture("support_retrieval_miss")
ra = _load_fixture("research_agent")


class FixtureTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "at")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _record(self, agent_fn, *args, name="fixture"):
        model = ScriptedModel(api_key=FAKE_API_KEY)
        with RecordRuntime(self.store, name, model=model,
                           api_key=FAKE_API_KEY) as rt:
            answer = agent_fn(rt, *args)
            run_id = rt.run_id
        return answer, run_id, model


class SupportRetrievalMissFixtureTestCase(FixtureTestCase):
    def test_retrieval_miss_records_error_and_honest_answer(self):
        answer, run_id, _ = self._record(
            srm.support_retrieval_miss_agent, srm.MISS_QUERY,
            name="fixture-support-retrieval-miss")
        self.assertIn("couldn't find information", answer)
        events = self.store.get_events(run_id)
        types = [e.type for e in events]
        self.assertIn(EventType.RETRIEVAL.value, types)
        self.assertIn(EventType.ERROR.value, types)
        self.assertIn(EventType.FINAL_OUTPUT.value, types)
        error = next(e for e in events
                     if e.type == EventType.ERROR.value)
        self.assertEqual(error.name, "retrieval_miss")
        self.assertIn("no relevant documents", error.output["message"])
        # A failed task: no model call, no tool call, no invented answer.
        self.assertNotIn(EventType.MODEL_REQUEST.value, types)
        self.assertNotIn(EventType.TOOL_CALL.value, types)
        # The run completes (graceful failure), but it is not a success:
        # any eval built on it must see task_success == False.
        has_error = any(e.type == EventType.ERROR.value for e in events)
        self.assertTrue(has_error)

    def test_retrieval_miss_replays_exactly(self):
        answer, run_id, model = self._record(
            srm.support_retrieval_miss_agent, srm.MISS_QUERY,
            name="fixture-support-retrieval-miss")
        calls_before = model.calls
        with ReplayRuntime(self.store, run_id, mode="exact",
                           model=model) as rt:
            replayed = srm.support_retrieval_miss_agent(rt, srm.MISS_QUERY)
            report = rt.report
        self.assertEqual(replayed, answer)
        self.assertEqual(model.calls, calls_before,
                         "exact replay must not call the model stub")
        self.assertGreater(report["replayed_calls"], 0)
        self.assertEqual(report["fallback_live_calls"], 0)

    def test_exported_fixture_snapshot_is_valid(self):
        data = json.loads(
            (FIXTURES / "support_retrieval_miss.json").read_text(
                encoding="utf-8"))
        self.assertEqual(data["agenttrace_version"], 1)
        self.assertEqual(data["run"]["metadata"]["fixture"],
                         "support_retrieval_miss")
        types = [e["type"] for e in data["events"]]
        self.assertIn("retrieval", types)
        self.assertIn("error", types)
        self.assertIn("final_output", types)
        self.assertNotIn(FAKE_API_KEY, json.dumps(data))


class ResearchAgentFixtureTestCase(FixtureTestCase):
    def test_research_agent_uses_multiple_tools_and_retrieval(self):
        answer, run_id, _ = self._record(
            ra.research_agent, ra.TOPICS, name="fixture-research-agent")
        events = self.store.get_events(run_id)
        types = [e.type for e in events]
        for expected in ("policy_decision", "retrieval", "tool_call",
                         "tool_result", "browser_action", "model_request",
                         "model_response", "memory_write", "final_output"):
            self.assertIn(expected, types, f"missing event type {expected}")
        tool_calls = [e.name for e in events
                      if e.type == EventType.TOOL_CALL.value]
        self.assertEqual(tool_calls, ["search_kb", "search_kb"],
                         "one search_kb call per topic")
        retrievals = [e for e in events
                      if e.type == EventType.RETRIEVAL.value]
        self.assertEqual(len(retrievals), len(ra.TOPICS))
        browser = [e for e in events
                   if e.type == EventType.BROWSER_ACTION.value]
        self.assertEqual(len(browser), len(ra.TOPICS))
        memory = [e for e in events
                  if e.type == EventType.MEMORY_WRITE.value]
        self.assertEqual(len(memory), len(ra.TOPICS) + 1)  # per-topic + summary
        self.assertTrue(answer)

    def test_research_agent_replays_exactly(self):
        answer, run_id, model = self._record(
            ra.research_agent, ra.TOPICS, name="fixture-research-agent")
        calls_before = model.calls
        with ReplayRuntime(self.store, run_id, mode="exact",
                           model=model) as rt:
            replayed = ra.research_agent(rt, ra.TOPICS)
            report = rt.report
        self.assertEqual(replayed, answer)
        self.assertEqual(model.calls, calls_before,
                         "exact replay must not call the model stub")
        # Every retrieval + tool result + model response is replayed.
        self.assertEqual(report["replayed_calls"], 2 * len(ra.TOPICS) + 1)
        self.assertEqual(report["fallback_live_calls"], 0)
        self.assertEqual(report["live_calls"], 0)

    def test_exported_fixture_snapshot_is_valid(self):
        data = json.loads(
            (FIXTURES / "research_agent.json").read_text(encoding="utf-8"))
        self.assertEqual(data["agenttrace_version"], 1)
        self.assertEqual(data["run"]["metadata"]["fixture"],
                         "research_agent")
        self.assertGreaterEqual(len(data["events"]), 15)
        tool_calls = [e["name"] for e in data["events"]
                      if e["type"] == "tool_call"]
        self.assertEqual(tool_calls, ["search_kb", "search_kb"])
        self.assertNotIn(FAKE_API_KEY, json.dumps(data))


if __name__ == "__main__":
    unittest.main()
