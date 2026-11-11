"""Regression tests: input cases + expected properties, CI mode included.

A case file is JSON or YAML (``.yaml``/``.yml`` — a small subset reader,
see :mod:`agenttrace.yamlmini`):

.. code-block:: yaml

    cases:
      - name: refund flow
        input:
          query: I want a refund for order ORD-1001
        expect:
          tool_sequence: [get_order, send_email]
          output_contains: refund
          task_success: true
          max_latency_ms: 5000
          max_cost_usd: 0.05

Each case is normally executed live with the demo agent, then compared
against its expectations: tool sequence, structured output, task
success, latency, and cost. A case may instead evaluate a *recorded*
run — ``input: {run_id: run_<16 hex>}`` — in which case the recorded
events are checked directly and the agent is not re-run. ``run_eval``
returns a report; CI mode is just the exit code — the CLI exits
non-zero when any assertion regresses.

Bad case files never produce a traceback: :func:`load_cases` raises
:class:`CaseFileError` with a plain message, and the CLI turns that
into ``error: ...`` on stderr with exit code 2.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .demo_agent import FAKE_API_KEY, customer_support_agent
from .events import EventType
from .model_stub import ScriptedModel
from .runtime import RecordRuntime
from .store import Store
from .yamlmini import YamlSubsetError, parse_yaml_subset


class CaseFileError(ValueError):
    """A case file is missing, unreadable, or malformed."""


def load_cases(path) -> list[dict]:
    """Load cases from a JSON or YAML file; raises CaseFileError cleanly."""
    path = Path(path)
    if not path.is_file():
        raise CaseFileError(f"no such case file: {path}")
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    try:
        if suffix in (".yaml", ".yml"):
            data = parse_yaml_subset(text)
        elif suffix == ".json":
            data = json.loads(text)
        else:
            raise CaseFileError(
                f"unsupported case file type {suffix!r} for {path} "
                "(use .json, .yaml or .yml)")
    except YamlSubsetError as exc:
        raise CaseFileError(f"{path}: {exc}")
    except json.JSONDecodeError as exc:
        raise CaseFileError(f"{path}: invalid JSON: {exc}")
    cases = data["cases"] if isinstance(data, dict) else data
    if not isinstance(cases, list):
        raise CaseFileError(
            f"{path}: expected a top-level list or a 'cases' list")
    for i, case in enumerate(cases):
        if not isinstance(case, dict) or "name" not in case or "input" not in case:
            raise CaseFileError(
                f"{path}: case {i} must be a mapping with 'name' and 'input'")
        if not isinstance(case["input"], dict):
            raise CaseFileError(
                f"{path}: case {case['name']!r} has a non-mapping 'input'")
    return cases


def _check(name: str, ok: bool, expected, actual) -> dict:
    return {"name": name, "ok": bool(ok), "expected": expected, "actual": actual}


def _checks_for_answer(answer: str, tool_sequence: list, cost: float,
                       latency_ms: float, has_error: bool, has_output: bool,
                       expect: dict) -> list[dict]:
    checks: list[dict] = []
    if "tool_sequence" in expect:
        checks.append(_check("tool_sequence",
                             tool_sequence == expect["tool_sequence"],
                             expect["tool_sequence"], tool_sequence))
    if "output_contains" in expect:
        checks.append(_check("output_contains",
                             expect["output_contains"].lower() in answer.lower(),
                             expect["output_contains"], answer))
    if "output_equals" in expect:
        checks.append(_check("output_equals",
                             answer == expect["output_equals"],
                             expect["output_equals"], answer))
    if "task_success" in expect:
        success = (not has_error) and has_output
        checks.append(_check("task_success", success == expect["task_success"],
                             expect["task_success"], success))
    if "max_latency_ms" in expect:
        checks.append(_check("max_latency_ms",
                             latency_ms <= expect["max_latency_ms"],
                             expect["max_latency_ms"], round(latency_ms, 1)))
    if "max_cost_usd" in expect:
        checks.append(_check("max_cost_usd", cost <= expect["max_cost_usd"],
                             expect["max_cost_usd"], cost))
    return checks


def _evaluate_recorded_run(store: Store, run_id: str) -> dict:
    """Derive the check inputs from a recorded run (no live re-run)."""
    run = store.get_run(run_id)
    if run is None:
        raise CaseFileError(f"no such recorded run: {run_id}")
    events = store.get_events(run_id)
    tool_sequence = [e.name for e in events if e.type == EventType.TOOL_CALL.value]
    cost = sum(e.meta.get("cost_usd", 0.0) for e in events
               if e.type == EventType.MODEL_RESPONSE.value)
    has_error = any(e.type == EventType.ERROR.value for e in events)
    outputs = [e for e in events if e.type == EventType.FINAL_OUTPUT.value]
    answer = outputs[0].output.get("text", "") if outputs else ""
    # Latency from the run's own timestamps when present.
    latency_ms = 0.0
    if run.get("started_at") and run.get("ended_at"):
        from datetime import datetime
        try:
            start = datetime.fromisoformat(run["started_at"])
            end = datetime.fromisoformat(run["ended_at"])
            latency_ms = (end - start).total_seconds() * 1000
        except ValueError:
            latency_ms = 0.0
    return {"answer": answer, "tool_sequence": tool_sequence, "cost": cost,
            "latency_ms": round(latency_ms, 1), "has_error": has_error,
            "has_output": bool(outputs)}


def run_case(store: Store, case: dict) -> dict:
    expect = case.get("expect", {})
    if "run_id" in case["input"]:
        # Evaluate a recorded run: no live agent execution.
        run_id = case["input"]["run_id"]
        recorded = _evaluate_recorded_run(store, run_id)
        checks = _checks_for_answer(
            recorded["answer"], recorded["tool_sequence"], recorded["cost"],
            recorded["latency_ms"], recorded["has_error"],
            recorded["has_output"], expect)
        return {"name": case["name"], "run_id": run_id, "source": "recorded",
                "ok": all(c["ok"] for c in checks), "checks": checks,
                "latency_ms": recorded["latency_ms"],
                "cost_usd": round(recorded["cost"], 6)}

    query = case["input"].get("query")
    if query is None:
        raise CaseFileError(
            f"case {case['name']!r}: 'input' needs 'query' (live case) "
            "or 'run_id' (recorded-run case)")
    started = time.perf_counter()
    model = ScriptedModel(api_key=FAKE_API_KEY)
    with RecordRuntime(store, f"eval:{case['name']}", model=model,
                       api_key=FAKE_API_KEY,
                       metadata={"eval_case": case["name"]}) as rt:
        answer = customer_support_agent(rt, query)
        run_id = rt.run_id
    latency_ms = (time.perf_counter() - started) * 1000

    events = store.get_events(run_id)
    tool_sequence = [e.name for e in events if e.type == EventType.TOOL_CALL.value]
    cost = sum(e.meta.get("cost_usd", 0.0) for e in events
               if e.type == EventType.MODEL_RESPONSE.value)
    has_error = any(e.type == EventType.ERROR.value for e in events)
    has_output = any(e.type == EventType.FINAL_OUTPUT.value for e in events)

    checks = _checks_for_answer(answer, tool_sequence, cost, latency_ms,
                                has_error, has_output, expect)
    return {"name": case["name"], "run_id": run_id, "source": "live",
            "ok": all(c["ok"] for c in checks), "checks": checks,
            "latency_ms": round(latency_ms, 1), "cost_usd": round(cost, 6)}


def run_eval(store: Store, cases: list[dict]) -> dict:
    results = [run_case(store, case) for case in cases]
    passed = sum(1 for r in results if r["ok"])
    return {"passed": passed, "failed": len(results) - passed,
            "results": results}


def eval_exit_code(report: dict) -> int:
    """CI mode: 0 when every case passes, 1 when anything regresses."""
    return 0 if report["failed"] == 0 else 1
