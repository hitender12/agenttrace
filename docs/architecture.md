# Architecture

This document explains how the AgentTrace reference implementation is
put together. It mirrors `ARCHITECTURE.md` at the repository root and
adds the detail a contributor needs before changing the recorder,
store, or replay engine. For the production path (Rust engine, OTLP,
TypeScript SDK, dashboard), see `ARCHITECTURE.md` — none of that is
built here.

## Big picture

```text
Your agent (Python SDK)
        |
  RecordRuntime  -- records every event via the SDK helpers
        |
  Redactor  ------ scrubs secrets before persistence
        |
  Store  --------- SQLite (runs + events) + artifacts/ (large payloads)
        |
  +-----+-----+-----+-----+
  |     |     |     |     |
Replay Diff  Eval  Export  CLI
engine       (CI)  (JSON)
```

There is no server, no network, and no background process. Everything
runs in the process that records or replays the run, against a local
directory (`.agenttrace/` by default).

## The event model (`agenttrace/events.py`)

One event per observable step. The 11 event types are:

| Type | Meaning |
|---|---|
| `model_request` | A prompt sent to the model, with model name and headers |
| `model_response` | The model's text, with tokens and cost in `meta` |
| `tool_call` | A tool invoked, with its arguments |
| `tool_result` | What the tool returned |
| `retrieval` | A knowledge-base lookup, with the documents returned |
| `browser_action` | A browser action (stubbed: returns a fixture page) |
| `memory_read` / `memory_write` | Reads/writes to the runtime's in-memory memory dict |
| `policy_decision` | A policy check and its allow/deny outcome |
| `error` | A failure, recorded with status `error` |
| `final_output` | The agent's final answer |

Every event is an `Event` dataclass with:

- `id` — `evt_<seq>_<random hex>`
- `run_id`, `seq` — sequence number within the run (1, 2, 3, …)
- `trace_id` — 128-bit, 32 lowercase hex chars (OpenTelemetry format)
- `span_id` — 64-bit, 16 lowercase hex chars; `parent_span_id` links
  a response to its request (model response → model request)
- `type`, `name` — the event type and a human-readable name (for
  `tool_call`/`tool_result`, the name is the tool name)
- `started_at`, `ended_at`, `duration_ms`, `status` (`ok` or `error`)
- `input`, `output` — JSON payloads
- `meta` — model name, `tokens_in`/`tokens_out`, `cost_usd`, and
  replay markers (`replayed`, `live`, `replayed_from`)

Trace/span IDs are OpenTelemetry-compatible so recorded runs keep
their original trace context and the format maps onto the future Rust
engine and OTLP ingestion without changing the recorded format. The
run/event JSON is the interchange contract.

## The recorder (`agenttrace/runtime.py`)

`RecordRuntime` is a context manager and the main SDK surface. On
`__enter__` it creates a run row (`mode="record"`); on `__exit__` it
marks the run `completed` (or `failed` if an exception propagated).
Its helpers — `model()`, `tool()`, `retrieve()`, `memory_read()` /
`memory_write()`, `policy_decision()`, `browser_action()`,
`final_output()`, `error()` — each write one or two events through the
shared `_Recorder` base class.

The model is `ScriptedModel` (`agenttrace/model_stub.py`) by default:
a deterministic stub that answers from canned templates chosen by
keywords in the prompt, with fixed token counts and a fixed price
(`$0.002` per 1K tokens, model name `scripted-1`). It makes no network
calls and costs nothing. A custom model can be injected via
`RecordRuntime(..., model=...)` as long as it implements
`.complete(prompt, model=None)` returning `{"text", "model",
"tokens_in", "tokens_out", "cost_usd"}`. `RecordRuntime` also accepts
an `api_key`, which is deliberately placed in the model request
headers so the redaction tests can prove it never reaches the store.

Tools are plain functions registered in two dicts in `runtime.py`:

- `TOOL_IMPLS`: `name -> callable(args) -> dict`. Built-ins:
  `get_order` (returns a canned order), `search_kb` (knowledge-base
  lookup), `send_email` (returns a fake `message_id`).
- `TOOL_SIDE_EFFECTS`: `name -> bool`. Only `send_email` is `True`
  today. A tool call may also pass `side_effect=True` at the call
  site; either flag marks the call side-effecting.

The knowledge base is an in-memory dict keyed by `refund`,
`shipping`, `cancel`, and `default`; `lookup_kb()` picks documents by
keyword. There is no real retrieval, browser, or MCP integration in
this implementation.

The demo agent (`agenttrace/demo_agent.py`) exercises the whole
surface in a fixed order: policy check (`pii_check`) → retrieval →
model → `get_order` → `send_email` (only for refund queries, with
`side_effect=True`) → `memory_write("last_topic", ...)` →
`final_output`.

## Storage (`agenttrace/store.py`)

`Store(base_dir)` creates `<base_dir>/agenttrace.db` (SQLite) and
`<base_dir>/artifacts/`. The default base directory is `.agenttrace`
in the current working directory. Two tables:

- `runs`: `id` (`run_` + 16 hex chars), `name`, `mode` (`record` or
  `replay:<mode>`), `replay_of` (source run for replays, else NULL),
  `status` (`running` / `completed` / `failed`), `started_at`,
  `ended_at`, `metadata_json`.
- `events`: one row per event, with `run_id`, `seq`, trace/span IDs,
  type, name, timestamps, duration, status, and `input_json` /
  `output_json` / `meta_json` payloads. Indexed on `(run_id, seq)`.

Payloads are redacted before writing (see below). A payload whose
JSON text is over 4 KB (`ARTIFACT_THRESHOLD`) is written as a JSON
file under `artifacts/<run_id>/<event_id>_<field>.json` and the row
stores `{"$artifact": "<relative path>"}` instead; `get_events`
transparently loads artifact files back. This keeps the database
small while payloads stay inspectable on disk.

Storage is a single SQLite file with **no multi-user access control
and no encryption at rest** — see `SECURITY.md`. It is local scratch
data for one developer.

## Redaction (`agenttrace/redaction.py`)

`Redactor` works recursively over dicts, lists, tuples, and strings.
Two rule kinds:

- **Key-based:** field names in `SENSITIVE_KEYS` (`api_key`,
  `apikey`, `password`, `passwd`, `secret`, `client_secret`, `token`,
  `access_token`, `refresh_token`, `authorization`, `bearer`,
  `private_key`, case-insensitive) are replaced with `[REDACTED]`.
- **Pattern-based:** secret-shaped values in free text —
  `sk-...` keys, `ghp_...` GitHub tokens, `AKIA...` AWS keys,
  `xox...` Slack tokens, `Bearer ...` headers — are replaced with
  a labelled placeholder.

Redaction runs twice: in `Store.add_event` before anything is written
to SQLite or an artifact file, and again in `export_run` on the way
out. Extend the rules with
`Redactor(extra_keys=[...], extra_patterns=[(regex, replacement), ...])`
and pass it to `Store(..., redactor=...)`. Redaction is best-effort:
it cannot recognise credentials under unlisted keys or in formats it
has no pattern for — see `SECURITY.md`.

## Replay (`agenttrace/replay.py`)

`ReplayRuntime` re-runs the same agent function against a recorded
run. It indexes the source run's events by `(type, name)` in call
order and serves them back in order. Full semantics, including the
three modes and the side-effect gate, are in
[`docs/replay-modes.md`](replay-modes.md).

Every replay creates a new run row with `mode=replay:<mode>` and
`replay_of=<source run id>`, and exposes a `report` dict (`mode`,
`source_run_id`, `replayed_calls`, `live_calls`,
`fallback_live_calls`, `blocked_side_effects`) that the CLI prints.

## Diff (`agenttrace/diff.py`)

`diff_runs(store, run_a, run_b)` compares two runs event by event:
model prompts, tool sequence and arguments, retrieved document IDs,
model metadata (model, tokens, cost), and final output text. It
returns a structured list of changes plus a human-readable rendering
via `render_diff`. Comparison is by call order, not by semantic
alignment of steps — if the two runs take different paths, the diff
reports the divergence as changes from the point they split.

## Eval / regression tests (`agenttrace/evals.py`)

Cases are JSON files (`examples/agent_cases.json`): a list of
`{name, input: {query}, expect: {...}}`. Supported expectations:

- `tool_sequence` — exact list of tool names called, in order
- `output_contains` — substring of the final answer (case-insensitive)
- `output_equals` — exact final answer
- `task_success` — true when the run has a `final_output` and no
  `error` event
- `max_latency_ms`, `max_cost_usd` — budgets for the whole case

Each case runs the agent live in a fresh recording and is checked
against its expectations. `eval_exit_code(report)` returns 1 when
anything fails — that is the CI contract; the CLI's `eval --ci`
exits with that code.

## Export (`agenttrace/export.py`)

`export_run(store, run_id, out_path)` writes
`{agenttrace_version: 1, run, events}` as indented JSON, redacted a
second time. Fixtures are portable: they carry no database
dependency, and `Event.from_dict` can read them back.

## CLI (`agenttrace/__main__.py`)

`python -m agenttrace` exposes `record`, `replay`, `diff`, `eval`,
`export`, `list`, and `timeline`. `record` runs the bundled demo
agent (scripted, free, deterministic) — to instrument your own agent,
use the SDK as shown in `examples/customer_support_agent.py`. All
commands take `--db` (default `.agenttrace`). `replay` takes
`--mode exact|live|hybrid`, `--hybrid-tools TOOL,...`, and
`--allow-side-effects`.

## What is not here (production path)

Per the blueprint: a Rust trace engine (event ingestion,
normalization, replay scheduling, diffing, high-throughput storage),
OTLP-compatible ingestion/export, a TypeScript SDK, framework
adapters, MCP integration, PostgreSQL server mode, S3-compatible
artifact storage, and a Next.js dashboard. The Python event model
and replay semantics here are designed to map onto that engine
without changing the recorded format.
