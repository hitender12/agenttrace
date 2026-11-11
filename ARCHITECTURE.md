# AgentTrace — Architecture

## Problem

An AI agent can fail because a model response changed, a tool returned
different data, a retrieval result was missing, a browser page changed, or
a retry path was triggered. A screenshot or plain-text log rarely captures
enough context to reproduce the failure. AgentTrace answers one question:
**what exactly happened in this run, and can I reproduce it?**

## Components (this reference implementation)

```text
Demo agent / your agent (Python SDK)
        |
  RecordRuntime  -- records every event via the SDK
        |
  Redactor  ---- scrubs secrets before persistence
        |
  Store  ------- SQLite (runs + events) + artifacts/ (large payloads)
        |
  +-----+-----+-----+-----+
  |     |     |     |     |
Replay Diff  Eval  Export  CLI
engine       (CI)  (JSON)
```

### Event model (`events.py`)

One event per observable step. Types: `model_request`, `model_response`,
`tool_call`, `tool_result`, `retrieval`, `browser_action`, `memory_read`,
`memory_write`, `policy_decision`, `error`, `final_output`. Each event
carries `trace_id` (128-bit, 32 hex chars) and `span_id` (64-bit, 16 hex
chars) in OpenTelemetry format, a sequence number, timestamps, duration,
status, `input`/`output` payloads, and `meta` (model name, tokens, cost).

### Recorder (`runtime.py`, `store.py`)

`RecordRuntime` is a context manager. It creates a run row, executes the
agent's stubs, and writes events. Payloads over 4 KB are stored as JSON
files under `artifacts/<run_id>/` and referenced from the row. The store
redacts payloads before writing, so a secret never lands in the database
file or an artifact file.

### Replay engine (`replay.py`)

`ReplayRuntime` re-runs the same agent function against a recorded run.
It indexes recorded events by `(type, name)` in call order and serves
them back:

- **exact**: model and tool calls return recorded outputs; the model stub
  is never invoked (verified by a call counter in tests).
- **live**: current stubs are called again; results are recorded as live.
- **hybrid**: tools listed in `hybrid_tools` run live; everything else
  is replayed exactly. The model is always replayed exactly in hybrid
  mode.

If the agent's path diverges from the recording (a recorded output is
missing), the engine falls back to a live call and records the fallback
in the report — it never fails silently. Every replay creates a new run
row with `mode=replay:<mode>` and `replay_of=<source run>`, and the
report states the mode, replayed/live/fallback counts, and blocked
side effects.

**Side-effect rule.** A tool flagged side-effecting (or called with
`side_effect=True`) is blocked on replay by default: the engine records a
`policy_decision` (`side_effect_gate`, allowed=false) and returns
`{"status": "blocked_on_replay"}` without invoking the tool. With
`allow_side_effects=True` the stub executes, and the event is marked
`side_effect_approved: True` so the approval is auditable.

### Diff (`diff.py`)

Compares two runs event-by-event: model prompts, tool sequence and
arguments, retrieved document IDs, model metadata (model, tokens, cost),
and final output text. Returns a structured list of changes plus a
human-readable rendering.

### Regression tests (`evals.py`)

Cases are JSON: `{name, input: {query}, expect: {...}}`. Expectations can
cover `tool_sequence`, `output_contains`, `output_equals`, `task_success`,
`max_latency_ms`, and `max_cost_usd`. Each case runs the agent live in a
fresh recording and is checked against its expectations. `eval_exit_code`
returns 1 when anything fails — that is the CI contract.

### Redaction (`redaction.py`)

Two layers: key-based (field names like `api_key`, `password`, `token`,
`authorization`) and pattern-based (e.g. `sk-...`, `ghp_...`, AWS access
keys, `xox...` tokens, `Bearer ...` headers). Applied on write and again
on export. Configurable via `extra_keys` / `extra_patterns`.

### Export (`export.py`)

Writes `{agenttrace_version, run, events}` as indented JSON, redacted.
Fixtures are portable: they carry no database dependency.

## Production path (not implemented here)

Per the blueprint: a Rust trace engine (event ingestion, normalization,
replay scheduling, diffing, high-throughput storage), OTLP-compatible
ingestion/export, a TypeScript SDK, framework adapters, MCP integration,
PostgreSQL server mode, S3-compatible artifact storage, and a Next.js
dashboard (run timeline, event inspector, diffs, test suites, dataset
management). The Python event model and replay semantics here are
designed to map onto that engine without changing the recorded format:
run/event JSON is the interchange contract.

## Honest limits

- The model is a deterministic scripted stub, not a real LLM. Replay
  determinism here means "the recorded evidence is reproduced", not
  "sampling is reproduced" — the blueprint is explicit that replaying a
  real AI run will not always reproduce the same model output.
- The knowledge base and tools are in-memory stubs; there is no real
  retrieval, browser, or MCP integration.
- Storage is a single SQLite file with no multi-user access control.
- Diffing compares by call order, not by semantic alignment of steps.
