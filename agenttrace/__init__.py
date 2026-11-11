"""AgentTrace: record, replay, test, and debug AI-agent runs across frameworks.

This is the Python reference implementation of the AgentTrace blueprint.
The production engine is Rust + OTLP ingestion + a dashboard (not
implemented here); this package proves the event model, the local recorder,
safe replay, diffing, redaction, dataset export, and CI regression tests.
"""
from __future__ import annotations

from .demo_agent import FAKE_API_KEY, customer_support_agent
from .diff import diff_runs, render_diff
from .evals import eval_exit_code, load_cases, run_eval
from .events import Event, EventType, new_span_id, new_trace_id
from .export import export_run
from .model_stub import ScriptedModel
from .redaction import Redactor
from .replay import ReplayRuntime
from .runtime import RecordRuntime
from .store import Store

__version__ = "0.1.0"

__all__ = [
    "Event", "EventType", "new_span_id", "new_trace_id",
    "Redactor", "ScriptedModel", "Store",
    "RecordRuntime", "ReplayRuntime",
    "customer_support_agent", "FAKE_API_KEY",
    "diff_runs", "render_diff",
    "load_cases", "run_eval", "eval_exit_code",
    "export_run",
    "__version__",
]
