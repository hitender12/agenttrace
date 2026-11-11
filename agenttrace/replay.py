"""Replay engine: exact, live, and hybrid replay.

Replay semantics (from the blueprint, stated honestly):

- **exact**: external model/tool calls return their recorded outputs. Best
  for reproducing downstream application logic; the model stub is never
  called.
- **live**: the current stubs are called again. Useful for comparison, but
  not deterministic.
- **hybrid**: recorded results for selected tools, live calls for the rest
  (the model is always replayed exactly in hybrid mode).

Every replay report states which mode was used. Side-effecting actions
(payment, email, deletion) are NEVER executed live on replay: they are
blocked by default and return a ``blocked_on_replay`` mock, unless the
caller passes ``allow_side_effects=True`` — and even then the call is
recorded with ``side_effect_approved: True`` so the approval is auditable.
"""
from __future__ import annotations

import time

from .events import EventType
from .model_stub import ScriptedModel
from .runtime import TOOL_IMPLS, TOOL_SIDE_EFFECTS, _Recorder, lookup_kb
from .store import Store


class ReplayRuntime(_Recorder):
    """Re-runs an agent function against a recorded run."""

    def __init__(self, store: Store, source_run_id: str, mode: str = "exact",
                 hybrid_tools=None, allow_side_effects: bool = False,
                 model: ScriptedModel | None = None):
        super().__init__(store)
        if mode not in ("exact", "live", "hybrid"):
            raise ValueError(f"unknown replay mode: {mode!r}")
        self.source_run_id = source_run_id
        self.mode = mode
        self.hybrid_tools = set(hybrid_tools or [])
        self.allow_side_effects = allow_side_effects
        self.model_stub = model or ScriptedModel()
        self.source_events = store.get_events(source_run_id)
        if not self.source_events:
            raise ValueError(f"no such run or empty run: {source_run_id}")
        self._recorded: dict = {}
        for event in self.source_events:
            self._recorded.setdefault((event.type, event.name), []).append(event)
        self._cursors: dict = {}
        self.report = {
            "mode": mode,
            "source_run_id": source_run_id,
            "replayed_calls": 0,
            "live_calls": 0,
            "fallback_live_calls": 0,
            "blocked_side_effects": [],
        }

    def __enter__(self) -> "ReplayRuntime":
        self.run_id = self.store.create_run(
            f"replay:{self.mode}:{self.source_run_id}",
            mode=f"replay:{self.mode}",
            metadata={"replay_mode": self.mode,
                      "hybrid_tools": sorted(self.hybrid_tools)},
            replay_of=self.source_run_id)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.store.finish_run(
            self.run_id, status="failed" if exc_type else "completed")
        return False

    # -- recorded-event lookup ------------------------------------------
    def _next_recorded(self, type: str, name: str):
        events = self._recorded.get((type, name), [])
        index = self._cursors.get((type, name), 0)
        self._cursors[(type, name)] = index + 1
        return events[index] if index < len(events) else None

    # -- model ------------------------------------------------------------
    def model(self, prompt: str, model_name: str | None = None) -> str:
        if self.mode == "live":
            return self._model_live(prompt, model_name)
        recorded_request = self._next_recorded(
            EventType.MODEL_REQUEST.value, "model_request")
        recorded_response = self._next_recorded(
            EventType.MODEL_RESPONSE.value, "model_response")
        if recorded_response is None:
            # Agent path diverged from the recording; fall back to a live
            # call and say so in the report instead of failing silently.
            self.report["fallback_live_calls"] += 1
            return self._model_live(prompt, model_name)
        request = self._record(
            EventType.MODEL_REQUEST.value, "model_request",
            {"prompt": prompt,
             "model": recorded_response.meta.get("model", "scripted-1")}, {},
            meta={"replayed_from": recorded_request.id if recorded_request else None})
        self._record(
            EventType.MODEL_RESPONSE.value, "model_response", {},
            {"text": recorded_response.output.get("text", "")},
            meta={**recorded_response.meta, "replayed": True},
            parent=request.span_id)
        self.report["replayed_calls"] += 1
        return recorded_response.output.get("text", "")

    def _model_live(self, prompt: str, model_name: str | None = None) -> str:
        started = time.perf_counter()
        request = self._record(
            EventType.MODEL_REQUEST.value, "model_request",
            {"prompt": prompt, "model": model_name or ScriptedModel.MODEL_NAME}, {})
        result = self.model_stub.complete(prompt, model=model_name)
        self._record(
            EventType.MODEL_RESPONSE.value, "model_response", {},
            {"text": result["text"]},
            meta={"model": result["model"], "tokens_in": result["tokens_in"],
                  "tokens_out": result["tokens_out"],
                  "cost_usd": result["cost_usd"], "live": True},
            duration_ms=(time.perf_counter() - started) * 1000,
            parent=request.span_id)
        self.report["live_calls"] += 1
        return result["text"]

    # -- tools ------------------------------------------------------------
    def tool(self, name: str, args: dict, side_effect: bool = False) -> dict:
        is_side_effect = side_effect or TOOL_SIDE_EFFECTS.get(name, False)
        if is_side_effect and not self.allow_side_effects:
            self.policy_decision(
                "side_effect_gate", {"tool": name, "args": args},
                {"allowed": False, "reason": "side effects are blocked on replay"})
            self._record(EventType.TOOL_CALL.value, name,
                         {"args": args, "side_effect": True}, {})
            result = {"status": "blocked_on_replay", "tool": name}
            self._record(EventType.TOOL_RESULT.value, name, {},
                         {"result": result, "blocked": True})
            self.report["blocked_side_effects"].append(name)
            return result
        live = self.mode == "live" or (
            self.mode == "hybrid" and name in self.hybrid_tools)
        if live:
            return self._tool_live(name, args, is_side_effect)
        recorded = self._next_recorded(EventType.TOOL_RESULT.value, name)
        self._record(EventType.TOOL_CALL.value, name,
                     {"args": args, "side_effect": is_side_effect}, {})
        if recorded is None:
            self.report["fallback_live_calls"] += 1
            return self._tool_live(name, args, is_side_effect)
        result = recorded.output.get("result", {})
        self._record(EventType.TOOL_RESULT.value, name, {},
                     {"result": result}, meta={"replayed": True})
        self.report["replayed_calls"] += 1
        return result

    def _tool_live(self, name: str, args: dict, side_effect: bool) -> dict:
        started = time.perf_counter()
        self._record(EventType.TOOL_CALL.value, name,
                     {"args": args, "side_effect": side_effect}, {})
        if name not in TOOL_IMPLS:
            self.error("unknown_tool", f"no tool named {name!r}")
            raise KeyError(f"unknown tool: {name}")
        result = TOOL_IMPLS[name](args)
        self._record(
            EventType.TOOL_RESULT.value, name, {}, {"result": result},
            meta={"live": True,
                  **({"side_effect_approved": True} if side_effect else {})},
            duration_ms=(time.perf_counter() - started) * 1000)
        self.report["live_calls"] += 1
        return result

    # -- retrieval ----------------------------------------------------------
    def retrieve(self, query: str) -> list[dict]:
        if self.mode == "live":
            docs = lookup_kb(query)
            self._record(EventType.RETRIEVAL.value, "retrieve",
                         {"query": query}, {"documents": docs},
                         meta={"live": True})
            self.report["live_calls"] += 1
            return docs
        recorded = self._next_recorded(EventType.RETRIEVAL.value, "retrieve")
        docs = (recorded.output.get("documents", []) if recorded
                else lookup_kb(query))
        self._record(EventType.RETRIEVAL.value, "retrieve",
                     {"query": query}, {"documents": docs},
                     meta={"replayed": True,
                           "replayed_from": recorded.id if recorded else None})
        self.report["replayed_calls"] += 1
        return docs
