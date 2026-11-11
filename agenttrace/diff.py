"""Diff view: what changed between two runs.

Compares, in order: model prompts, tool arguments, retrieved documents,
model metadata (model, tokens, cost), and final output fields. A run pair
with no differences reports ``changed: False``.
"""
from __future__ import annotations

from itertools import zip_longest

from .events import EventType
from .store import Store


def _by_type(events, event_type: str, name: str | None = None):
    return [e for e in events
            if e.type == event_type and (name is None or e.name == name)]


def diff_runs(store: Store, run_a: str, run_b: str) -> dict:
    # A missing run is an error, not an empty run: diffing against
    # nothing would otherwise report phantom changes (or "no changes").
    for run_id in (run_a, run_b):
        if store.get_run(run_id) is None:
            raise ValueError(f"no such run: {run_id}")
    events_a = store.get_events(run_a)
    events_b = store.get_events(run_b)
    changes: list[dict] = []

    def add(kind: str, field: str, old, new):
        if old != new:
            changes.append({"kind": kind, "field": field, "old": old, "new": new})

    # 1. Changed prompts.
    for i, (a, b) in enumerate(zip_longest(
            _by_type(events_a, EventType.MODEL_REQUEST.value),
            _by_type(events_b, EventType.MODEL_REQUEST.value))):
        add("prompt_changed", f"model_request[{i}].prompt",
            a.input.get("prompt") if a else None,
            b.input.get("prompt") if b else None)

    # 2. Changed tool arguments (and tool sequence).
    calls_a = _by_type(events_a, EventType.TOOL_CALL.value)
    calls_b = _by_type(events_b, EventType.TOOL_CALL.value)
    add("tool_sequence_changed", "tool_sequence",
        [e.name for e in calls_a], [e.name for e in calls_b])
    for i, (a, b) in enumerate(zip_longest(calls_a, calls_b)):
        add("tool_args_changed", f"tool_call[{i}].args",
            a.input.get("args") if a else None,
            b.input.get("args") if b else None)

    # 3. Changed retrieved documents.
    for i, (a, b) in enumerate(zip_longest(
            _by_type(events_a, EventType.RETRIEVAL.value),
            _by_type(events_b, EventType.RETRIEVAL.value))):
        add("retrieval_changed", f"retrieval[{i}].documents",
            [d.get("id") for d in a.output.get("documents", [])] if a else None,
            [d.get("id") for d in b.output.get("documents", [])] if b else None)

    # 4. Changed model metadata.
    for i, (a, b) in enumerate(zip_longest(
            _by_type(events_a, EventType.MODEL_RESPONSE.value),
            _by_type(events_b, EventType.MODEL_RESPONSE.value))):
        for key in ("model", "tokens_in", "tokens_out", "cost_usd"):
            add("model_metadata_changed", f"model_response[{i}].{key}",
                a.meta.get(key) if a else None,
                b.meta.get(key) if b else None)

    # 5. Changed output fields.
    out_a = _by_type(events_a, EventType.FINAL_OUTPUT.value)
    out_b = _by_type(events_b, EventType.FINAL_OUTPUT.value)
    add("output_changed", "final_output.text",
        out_a[0].output.get("text") if out_a else None,
        out_b[0].output.get("text") if out_b else None)

    return {"run_a": run_a, "run_b": run_b,
            "changed": bool(changes), "changes": changes}


def render_diff(diff: dict) -> str:
    lines = [f"diff {diff['run_a']} -> {diff['run_b']}"]
    if not diff["changed"]:
        lines.append("no changes")
        return "\n".join(lines)
    for change in diff["changes"]:
        lines.append(f"- {change['kind']}: {change['field']}")
        lines.append(f"    old: {change['old']!r}")
        lines.append(f"    new: {change['new']!r}")
    return "\n".join(lines)
