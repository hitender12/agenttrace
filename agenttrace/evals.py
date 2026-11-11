"""Regression tests: input cases + expected properties, CI mode included.

A case file is JSON (a YAML-ish dict format):

.. code-block:: json

    {"cases": [
      {"name": "refund flow",
       "input": {"query": "I want a refund for order ORD-1001"},
       "expect": {"tool_sequence": ["get_order", "send_email"],
                  "output_contains": "refund",
                  "task_success": true,
                  "max_latency_ms": 5000,
                  "max_cost_usd": 0.05}}
    ]}

Each case is executed live with the demo agent, then compared against its
expectations: tool sequence, structured output, task success, latency, and
cost. ``run_eval`` returns a report; CI mode is just the exit code — the
CLI exits non-zero when any assertion regresses.
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


def load_cases(path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["cases"] if isinstance(data, dict) else data


def _check(name: str, ok: bool, expected, actual) -> dict:
    return {"name": name, "ok": bool(ok), "expected": expected, "actual": actual}


def run_case(store: Store, case: dict) -> dict:
    query = case["input"]["query"]
    expect = case.get("expect", {})
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

    return {"name": case["name"], "run_id": run_id,
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
