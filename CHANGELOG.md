# Changelog

All notable changes to AgentTrace will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-11

Initial release: a Python reference implementation of the AgentTrace
blueprint — a framework-independent flight recorder for AI agents.
Python 3.10+, standard library only, no network calls, no model API
key required.

### Added

- **Unified event model** (`agenttrace/events.py`): 11 event types —
  `model_request`, `model_response`, `tool_call`, `tool_result`,
  `retrieval`, `browser_action`, `memory_read`, `memory_write`,
  `policy_decision`, `error`, `final_output` — each with
  OpenTelemetry-compatible trace/span IDs (128-bit trace ID, 64-bit
  span ID), sequence number, timestamps, duration, status,
  input/output payloads, and metadata (model, tokens, cost).
- **Local-first recorder** (`agenttrace/runtime.py`,
  `agenttrace/store.py`): `RecordRuntime` context manager records a
  run to SQLite (`.agenttrace/agenttrace.db`); payloads over 4 KB are
  stored as JSON files under `.agenttrace/artifacts/<run_id>/`.
- **Replay engine** (`agenttrace/replay.py`): three explicit modes —
  `exact` (recorded outputs for model and tool calls; the model stub
  is never invoked), `live` (current stubs called again; not
  deterministic), and `hybrid` (recorded results for selected tools,
  live calls for the rest; the model is always replayed exactly).
  Every replay creates a new run row (`mode=replay:<mode>`,
  `replay_of=<source run>`) and a report stating the mode and the
  replayed / live / fallback-live call counts. If the agent's path
  diverges from the recording, the engine falls back to a live call
  and records the fallback in the report instead of failing silently.
- **Side-effect safety on replay**: side-effecting tools (payment,
  email, deletion) are blocked by default and return
  `{"status": "blocked_on_replay"}` with a `policy_decision`
  (`side_effect_gate`, `allowed=false`) recorded; `--allow-side-effects`
  executes them and marks the event `side_effect_approved: True` so
  the approval is auditable.
- **Diff view** (`agenttrace/diff.py`): event-by-event comparison of
  two runs — model prompts, tool sequence and arguments, retrieved
  document IDs, model metadata (model, tokens, cost), and final
  output text — with a human-readable rendering.
- **Regression tests / eval** (`agenttrace/evals.py`): JSON case
  files (`examples/agent_cases.json`) with expectations for
  `tool_sequence`, `output_contains`, `output_equals`,
  `task_success`, `max_latency_ms`, and `max_cost_usd`;
  `eval --ci` exits non-zero when any assertion regresses.
- **Redaction** (`agenttrace/redaction.py`): key-based redaction
  (`api_key`, `password`, `token`, `authorization`, and others) and
  pattern-based redaction (`sk-...`, `ghp_...`, `AKIA...`, `xox...`,
  `Bearer ...`), applied on write to the store and again on export,
  configurable via `Redactor(extra_keys=..., extra_patterns=...)`.
- **Dataset export** (`agenttrace/export.py`): selected runs exported
  as portable, redacted JSON fixtures
  (`{agenttrace_version, run, events}`) with no database dependency.
- **Python SDK and CLI** (`agenttrace/__main__.py`): `record`,
  `replay`, `diff`, `eval`, `export`, `list`, and `timeline`
  commands; `examples/customer_support_agent.py` shows how to
  instrument an agent with `RecordRuntime`.
- **Demo agent** (`agenttrace/demo_agent.py`): a deterministic,
  scripted customer-support agent (policy check → retrieval → model
  → order lookup → optional side-effecting email → memory write →
  output) driven by `ScriptedModel`, a deterministic stub with fixed
  token counts and cost — no real LLM, no network, no cost.
- **Tests**: 28-test `unittest` suite across
  `tests/test_agenttrace.py`, `tests/test_edge_cases.py`, and
  `tests/test_fixtures.py`, covering recording, exact replay, the
  side-effect gate, hybrid replay, redaction (including every secret
  pattern/key and nested payloads), failed tool calls on replay,
  diff, eval/CI exit codes, export round-trips and fixture snapshots,
  retrieval-miss handling, and recording/replaying several hundred
  events.

### Not in this release

The Rust trace engine, OTLP ingestion/export, TypeScript SDK,
framework adapters, MCP integration, PostgreSQL server mode, and the
Next.js dashboard from the blueprint are not built here; see
`ARCHITECTURE.md` ("Production path") for the roadmap. The model is a
deterministic scripted stub, not a real LLM, so replay determinism
here means "the recorded evidence is reproduced", not "sampling is
reproduced".
