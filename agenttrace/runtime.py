"""Recording runtime: executes the (stubbed) agent and records every event.

The runtime is the SDK surface: a run context manager plus ``model``,
``tool``, ``retrieve``, ``memory_read``/``memory_write``,
``policy_decision``, ``browser_action``, ``final_output`` and ``error``
helpers. Each call writes one or two events (request + response for the
model, call + result for tools) with OpenTelemetry-compatible IDs.
"""
from __future__ import annotations

import re
import secrets
import time

from .events import Event, EventType, new_span_id, new_trace_id, now_iso
from .model_stub import ScriptedModel
from .store import Store

# Small in-memory knowledge base used by the demo agent's retrieval stub.
KB = {
    "refund": [
        {"id": "doc-refund-policy", "title": "Refund policy",
         "text": "Refunds are approved within 5 business days for orders under $100."},
        {"id": "doc-order-lookup", "title": "Order lookup",
         "text": "Look up an order by its ID to confirm status and total before refunding."},
    ],
    "shipping": [
        {"id": "doc-shipping", "title": "Shipping status",
         "text": "Standard shipping delivers in 3-5 business days with tracking."},
    ],
    "cancel": [
        {"id": "doc-cancel", "title": "Cancellation policy",
         "text": "Orders can be cancelled before they ship; shipped orders must be returned."},
    ],
    "default": [
        {"id": "doc-faq", "title": "General FAQ",
         "text": "Contact support for refunds, shipping updates, and cancellations."},
    ],
}

_ORDER_RE = re.compile(r"ORD-\d+", re.IGNORECASE)


def lookup_kb(query: str) -> list[dict]:
    low = query.lower()
    for key in ("refund", "shipping", "cancel"):
        if key in low:
            return list(KB[key])
    if "deliver" in low:
        return list(KB["shipping"])
    return list(KB["default"])


def extract_order_id(query: str) -> str:
    match = _ORDER_RE.search(query)
    return match.group(0).upper() if match else "ORD-1001"


def _tool_get_order(args: dict) -> dict:
    return {
        "order_id": args.get("order_id", "ORD-1001"),
        "status": "delivered",
        "total_usd": 49.00,
        "items": ["Widget Alpha"],
    }


def _tool_search_kb(args: dict) -> dict:
    return {"results": lookup_kb(args.get("query", ""))}


def _tool_send_email(args: dict) -> dict:
    return {"sent": True, "message_id": "msg-" + secrets.token_hex(4)}


# Tool name -> implementation. Side-effecting tools are flagged in
# TOOL_SIDE_EFFECTS; the replay engine never executes them live.
TOOL_IMPLS = {
    "get_order": _tool_get_order,
    "search_kb": _tool_search_kb,
    "send_email": _tool_send_email,
}
TOOL_SIDE_EFFECTS = {"send_email": True, "get_order": False, "search_kb": False}


class _Recorder:
    """Shared event-writing helpers for record and replay runtimes."""

    def __init__(self, store: Store):
        self.store = store
        self.trace_id = new_trace_id()
        self._seq = 0
        self._memory: dict = {}
        self.run_id: str | None = None

    def _record(self, type: str, name: str, input=None, output=None,
                meta=None, status: str = "ok", duration_ms: float | None = None,
                parent: str | None = None) -> Event:
        self._seq += 1
        event = Event(
            id=f"evt_{self._seq:04d}_{secrets.token_hex(4)}",
            run_id=self.run_id, seq=self._seq, trace_id=self.trace_id,
            span_id=new_span_id(), parent_span_id=parent, type=type, name=name,
            started_at=now_iso(), ended_at=now_iso(),
            duration_ms=duration_ms, status=status,
            input=input or {}, output=output or {}, meta=meta or {},
        )
        self.store.add_event(self.run_id, event)
        return event

    # -- pass-through event types (same in record and replay) ----------
    def policy_decision(self, name: str, input: dict, output: dict) -> dict:
        self._record(EventType.POLICY_DECISION.value, name, input, output)
        return output

    def memory_write(self, key: str, value) -> None:
        # The key name itself is a signal: memory_write("db_password",
        # secret) stores the secret under the generic "value" field, so
        # without this the value is never learned or redacted. When the
        # key is sensitive, register the value first — the store then
        # scrubs it from the recorded output like any learned secret.
        from .redaction import is_sensitive_key
        if is_sensitive_key(key):
            self.store.redactor.register_value(value)
        self._memory[key] = value
        self._record(EventType.MEMORY_WRITE.value, "memory_write",
                     {"key": key}, {"value": value})

    def memory_read(self, key: str):
        value = self._memory.get(key)
        self._record(EventType.MEMORY_READ.value, "memory_read",
                     {"key": key}, {"value": value, "found": value is not None})
        return value

    def browser_action(self, action: str, url: str) -> dict:
        result = {"title": "Fixture page", "status": 200, "url": url}
        self._record(EventType.BROWSER_ACTION.value, action,
                     {"action": action, "url": url}, result)
        return result

    def final_output(self, text: str) -> str:
        self._record(EventType.FINAL_OUTPUT.value, "final_output", {}, {"text": text})
        return text

    def error(self, name: str, message: str) -> None:
        self._record(EventType.ERROR.value, name, {}, {"message": message},
                     status="error")


class RecordRuntime(_Recorder):
    """Executes the agent's stubs for real and records the run."""

    def __init__(self, store: Store, run_name: str,
                 model: ScriptedModel | None = None,
                 api_key: str | None = None, metadata: dict | None = None):
        super().__init__(store)
        self.run_name = run_name
        self.model_stub = model or ScriptedModel(api_key=api_key)
        self.api_key = api_key
        self.metadata = metadata or {}
        # The model key is a secret the recorder itself knows: register it
        # so it is scrubbed from free text anywhere in this run, not only
        # from the Authorization header.
        if api_key:
            store.redactor.register_secret(api_key)

    def __enter__(self) -> "RecordRuntime":
        self.run_id = self.store.create_run(
            self.run_name, mode="record", metadata=self.metadata)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.store.finish_run(
            self.run_id, status="failed" if exc_type else "completed")
        return False

    def model(self, prompt: str, model_name: str | None = None) -> str:
        started = time.perf_counter()
        headers = {}
        if self.api_key:
            # Deliberately realistic: the key rides in the request headers.
            # The store redacts it before persistence (see test_redaction).
            headers = {"Authorization": f"Bearer {self.api_key}"}
        request = self._record(
            EventType.MODEL_REQUEST.value, "model_request",
            {"prompt": prompt, "model": model_name or ScriptedModel.MODEL_NAME,
             "headers": headers}, {})
        result = self.model_stub.complete(prompt, model=model_name)
        duration_ms = (time.perf_counter() - started) * 1000
        self._record(
            EventType.MODEL_RESPONSE.value, "model_response", {},
            {"text": result["text"]},
            meta={"model": result["model"], "tokens_in": result["tokens_in"],
                  "tokens_out": result["tokens_out"],
                  "cost_usd": result["cost_usd"]},
            duration_ms=duration_ms, parent=request.span_id)
        return result["text"]

    def tool(self, name: str, args: dict, side_effect: bool = False) -> dict:
        started = time.perf_counter()
        self._record(EventType.TOOL_CALL.value, name,
                     {"args": args, "side_effect": side_effect}, {})
        if name not in TOOL_IMPLS:
            self.error("unknown_tool", f"no tool named {name!r}")
            raise KeyError(f"unknown tool: {name}")
        result = TOOL_IMPLS[name](args)
        duration_ms = (time.perf_counter() - started) * 1000
        self._record(EventType.TOOL_RESULT.value, name, {},
                     {"result": result}, duration_ms=duration_ms)
        return result

    def retrieve(self, query: str) -> list[dict]:
        docs = lookup_kb(query)
        self._record(EventType.RETRIEVAL.value, "retrieve",
                     {"query": query}, {"documents": docs})
        return docs
