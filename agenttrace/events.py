"""Unified event model for AgentTrace.

Event types follow the blueprint: model request/response, tool call/result,
retrieval, browser action, memory read/write, policy decision, error, and
final output. Trace/span IDs use the OpenTelemetry format: 128-bit trace
IDs (32 lowercase hex chars) and 64-bit span IDs (16 lowercase hex chars),
so recorded runs keep their original trace context.
"""
from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum


class EventType(str, Enum):
    MODEL_REQUEST = "model_request"
    MODEL_RESPONSE = "model_response"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    RETRIEVAL = "retrieval"
    BROWSER_ACTION = "browser_action"
    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"
    POLICY_DECISION = "policy_decision"
    ERROR = "error"
    FINAL_OUTPUT = "final_output"


def new_trace_id() -> str:
    """128-bit trace ID, OpenTelemetry format (32 lowercase hex chars)."""
    return secrets.token_hex(16)


def new_span_id() -> str:
    """64-bit span ID, OpenTelemetry format (16 lowercase hex chars)."""
    return secrets.token_hex(8)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Event:
    id: str
    run_id: str
    seq: int
    trace_id: str
    span_id: str
    parent_span_id: str | None
    type: str
    name: str
    started_at: str
    ended_at: str | None = None
    duration_ms: float | None = None
    status: str = "ok"
    input: dict = field(default_factory=dict)
    output: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        allowed = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in allowed})
