"""Example: run the eval suite in CI mode.

Run it directly (from the repository root)::

    python examples/eval-ci/run_eval_ci.py; echo "exit=$?"

What it does, in a fresh temporary store (no leftover state):

Phase 1 -- all passing:
    Loads ``cases_passing.json`` (one refund-flow case), runs it with
    ``run_eval``, and prints the CI exit code from ``eval_exit_code``:
    0, because every case passes.

Phase 2 -- one deliberately failing case:
    Loads ``cases.json`` (the same passing case plus a case whose
    expectations are deliberately wrong: it expects tool sequence
    ``["search_kb"]`` and output containing ``"spaceship"`` for a
    refund query). Prints the failed checks and the CI exit code: 1.

The script exits with the Phase 2 code (1). That non-zero exit IS
the demonstration: in CI, ``eval --ci`` fails the build when any
deterministic assertion regresses. If you only want the green path,
run the CLI against ``cases_passing.json`` (see README.md).

Everything is deterministic: cases run the demo agent against the
project's ``ScriptedModel`` stub (no network, no real model calls,
no cost).
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agenttrace import eval_exit_code, load_cases, run_eval  # noqa: E402
from agenttrace.store import Store  # noqa: E402

HERE = Path(__file__).resolve().parent


def show_report(title: str, report: dict) -> int:
    print(f"\n== {title} ==")
    for result in report["results"]:
        mark = "PASS" if result["ok"] else "FAIL"
        print(f"[{mark}] {result['name']} "
              f"(latency {result['latency_ms']} ms, "
              f"cost ${result['cost_usd']:.6f})")
        for check in result["checks"]:
            if not check["ok"]:
                print(f"       failed check {check['name']}: "
                      f"expected {check['expected']!r}, "
                      f"got {check['actual']!r}")
    print(f"{report['passed']} passed, {report['failed']} failed")
    code = eval_exit_code(report)
    print(f"CI mode: exit {code}")
    return code


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="agenttrace-eval-example-") as tmp:
        store = Store(Path(tmp) / "at")
        try:
            passing = run_eval(store, load_cases(HERE / "cases_passing.json"))
            code_passing = show_report(
                "Phase 1: passing cases only (cases_passing.json)", passing)
            assert code_passing == 0, "passing cases should exit 0"

            mixed = run_eval(store, load_cases(HERE / "cases.json"))
            code_mixed = show_report(
                "Phase 2: passing + deliberately failing case (cases.json)",
                mixed)
            assert mixed["failed"] == 1, "expected exactly one failing case"
            assert code_mixed == 1, "failing case should exit 1"

            print("\nOK: CI mode exits 0 when all cases pass and 1 when "
                  "any assertion regresses.")
            print("This script now exits 1 on purpose, to demonstrate the "
                  "failing CI gate (see Phase 2 above).")
            return code_mixed
        finally:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
