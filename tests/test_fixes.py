"""Regression tests for the QA-audit fixes (findings 2-7).

Covers: order-aware demo stub, exact-mode approval marker, diff of
missing runs, clean eval errors, YAML case files, recorded-run eval,
import round-trip, --db as a file path, timeline payloads, and
`record -- <script>`.
"""
import json
import subprocess
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
from agenttrace.evals import CaseFileError  # noqa: E402
from agenttrace.importer import import_run  # noqa: E402
from agenttrace.yamlmini import YamlSubsetError, parse_yaml_subset  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
REFUND_QUERY = "I want a refund for order ORD-1001"


class OrderAwareStubTestCase(unittest.TestCase):
    def test_answers_about_the_order_asked(self):
        model = ScriptedModel()
        for query, order in (
                ("Where is my delivery for order ORD-2002?", "ORD-2002"),
                ("I want a refund for order ORD-9876", "ORD-9876"),
                ("Please cancel order ORD-5555", "ORD-5555")):
            answer = model.complete(query)["text"]
            self.assertIn(order, answer, query)
            self.assertNotIn("ORD-1001", answer, query)

    def test_no_order_in_query_falls_back_to_default(self):
        model = ScriptedModel()
        answer = model.complete("hello, I need help")["text"]
        self.assertIn("ORD-1001", answer)


class ExactModeApprovalTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "at")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _record_refund(self):
        model = ScriptedModel(api_key=FAKE_API_KEY)
        with RecordRuntime(self.store, "refund", model=model,
                           api_key=FAKE_API_KEY,
                           metadata={"query": REFUND_QUERY}) as rt:
            customer_support_agent(rt, REFUND_QUERY)
            return rt.run_id

    def test_exact_mode_approval_is_recorded(self):
        run_id = self._record_refund()
        model = ScriptedModel(api_key=FAKE_API_KEY)
        with ReplayRuntime(self.store, run_id, mode="exact",
                           allow_side_effects=True, model=model) as rt:
            customer_support_agent(rt, REFUND_QUERY)
            report = rt.report
            new_run_id = rt.run_id
        events = self.store.get_events(new_run_id)
        approved = [e for e in events if e.meta.get("side_effect_approved")]
        self.assertTrue(approved,
                        "exact-mode approval must leave an audit marker")
        self.assertIn("send_email", report["approved_side_effects"])

    def test_exact_mode_without_approval_still_blocks(self):
        run_id = self._record_refund()
        model = ScriptedModel(api_key=FAKE_API_KEY)
        with ReplayRuntime(self.store, run_id, mode="exact",
                           model=model) as rt:
            result = rt.tool("send_email",
                             {"to": "c@example.com", "body": "x"},
                             side_effect=True)
        self.assertEqual(result["status"], "blocked_on_replay")


class DiffMissingRunTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "at")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_diff_missing_run_raises(self):
        model = ScriptedModel(api_key=FAKE_API_KEY)
        with RecordRuntime(self.store, "r", model=model,
                           api_key=FAKE_API_KEY) as rt:
            rt.final_output("hi")
            run_id = rt.run_id
        with self.assertRaises(ValueError):
            diff_runs(self.store, "run_does_not_exist", run_id)
        with self.assertRaises(ValueError):
            diff_runs(self.store, run_id, "run_does_not_exist")


class EvalFileHandlingTestCase(unittest.TestCase):
    def test_missing_case_file_is_a_clean_error(self):
        with self.assertRaises(CaseFileError) as ctx:
            load_cases("does-not-exist.json")
        self.assertIn("no such case file", str(ctx.exception))

    def test_malformed_json_is_a_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(CaseFileError) as ctx:
                load_cases(path)
            self.assertIn("invalid JSON", str(ctx.exception))

    def test_yaml_cases_load_and_pass(self):
        cases = load_cases(REPO / "examples" / "agent_cases.yaml")
        self.assertEqual([c["name"] for c in cases],
                         ["refund flow", "shipping status", "cancellation"])
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "at")
            try:
                report = run_eval(store, cases)
            finally:
                store.close()
        self.assertEqual(report["failed"], 0, json.dumps(report, indent=2))
        self.assertEqual(eval_exit_code(report), 0)

    def test_yaml_subset_rejects_unsupported_features(self):
        for text in (
                "a:\n\tb: 1\n",          # tabs
                "a: 1\na: 2\n",          # duplicate key
                "a: &anchor 1\n",        # anchor
                "a: {x: 1}\n",           # flow mapping
                "a: 1\n---\nb: 2\n",      # multi-document
        ):
            with self.subTest(text=text):
                with self.assertRaises(YamlSubsetError):
                    parse_yaml_subset(text)

    def test_eval_recorded_run_without_live_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "at")
            try:
                model = ScriptedModel(api_key=FAKE_API_KEY)
                with RecordRuntime(store, "refund", model=model,
                                   api_key=FAKE_API_KEY,
                                   metadata={"query": REFUND_QUERY}) as rt:
                    customer_support_agent(rt, REFUND_QUERY)
                    run_id = rt.run_id
                calls_before = model.calls
                cases = [{"name": "recorded refund",
                          "input": {"run_id": run_id},
                          "expect": {"tool_sequence": ["get_order", "send_email"],
                                     "output_contains": "refund",
                                     "task_success": True}}]
                report = run_eval(store, cases)
                self.assertEqual(report["failed"], 0, json.dumps(report, indent=2))
                self.assertEqual(report["results"][0]["source"], "recorded")
                self.assertEqual(model.calls, calls_before,
                                 "recorded-run eval must not call the model")
            finally:
                store.close()

    def test_eval_recorded_run_missing_id_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "at")
            try:
                with self.assertRaises(CaseFileError):
                    run_eval(store, [{"name": "gone",
                                      "input": {"run_id": "run_nope"},
                                      "expect": {}}])
            finally:
                store.close()


class ImportTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "at")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _record_and_export(self):
        model = ScriptedModel(api_key=FAKE_API_KEY)
        with RecordRuntime(self.store, "r", model=model,
                           api_key=FAKE_API_KEY) as rt:
            rt.final_output("hello")
            run_id = rt.run_id
        out = Path(self.tmp.name) / "fixture.json"
        export_run(self.store, run_id, out)
        return run_id, out

    def test_import_round_trip(self):
        # Import into a *different* store: the original run id is kept.
        run_id, out = self._record_and_export()
        with tempfile.TemporaryDirectory() as tmp:
            other = Store(Path(tmp) / "other")
            try:
                imported = import_run(other, out)
            finally:
                other.close()
        self.assertEqual(imported, run_id)
        events = self.store.get_events(run_id)
        self.assertEqual(len(events), 1)

    def test_import_into_same_store_mints_fresh_id(self):
        # The fixture's run already exists here: import must not collide.
        run_id, out = self._record_and_export()
        imported = import_run(self.store, out)
        self.assertNotEqual(imported, run_id)
        self.assertEqual(len(self.store.get_events(imported)), 1)

    def test_import_twice_mints_fresh_ids(self):
        _, out = self._record_and_export()
        first = import_run(self.store, out)
        second = import_run(self.store, out)
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.store.get_events(second)), 1)

    def test_import_bad_files_are_clean_errors(self):
        missing = Path(self.tmp.name) / "nope.json"
        with self.assertRaises(ValueError):
            import_run(self.store, missing)
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text("{oops", encoding="utf-8")
        with self.assertRaises(ValueError):
            import_run(self.store, bad)
        not_fixture = Path(self.tmp.name) / "plain.json"
        not_fixture.write_text('{"hello": "world"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            import_run(self.store, not_fixture)


class CliTestCase(unittest.TestCase):
    def _cli(self, *args, cwd=None):
        return subprocess.run([sys.executable, "-m", "agenttrace", *args],
                              cwd=cwd or str(REPO), capture_output=True,
                              text=True, timeout=120)

    def test_eval_missing_file_exits_2_without_traceback(self):
        proc = self._cli("eval", "does-not-exist.json")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("error: no such case file", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_diff_missing_run_exits_2_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp) / "at"
            proc = self._cli("record", "--db", str(db_dir))
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            proc = self._cli("diff", "run_nope", "run_nope2",
                             "--db", str(db_dir))
            self.assertEqual(proc.returncode, 2)
            self.assertIn("error: no such run", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)

    def test_missing_store_exits_2_without_traceback(self):
        proc = self._cli("diff", "run_nope", "run_nope2")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("error: no AgentTrace store", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_db_accepts_a_file_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp) / "at"
            proc = self._cli("record", "--db", str(db_dir))
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            # The same store opened via the .db file must show the run.
            proc = self._cli("list", "--db", str(db_dir / "agenttrace.db"))
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("demo", proc.stdout)

    def test_record_script_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp) / "at"
            # Options come before '--'; everything after it is the script.
            proc = self._cli("record", "--db", str(db_dir), "--", "python3",
                             "examples/customer_support_agent.py")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("recorded run run_", proc.stdout)
            listed = self._cli("list", "--db", str(db_dir))
            self.assertIn("example-support-run", listed.stdout)

    def test_timeline_payloads_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp) / "at"
            proc = self._cli("record", "--db", str(db_dir))
            run_id = proc.stdout.split()[2]
            plain = self._cli("timeline", run_id, "--db", str(db_dir))
            self.assertIn("use --payloads", plain.stdout)
            self.assertNotIn("ORD-1001", plain.stdout)
            with_payloads = self._cli("timeline", run_id, "--payloads",
                                      "--db", str(db_dir))
            self.assertEqual(with_payloads.returncode, 0)
            self.assertIn("ORD-1001", with_payloads.stdout)


if __name__ == "__main__":
    unittest.main()
