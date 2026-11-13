# Changelog

All notable changes to AgentTrace will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Redaction leaks, round 2 (independent re-audit: 22 leaks out of
  41 fresh checks).** All 12 finding groups fixed and covered by
  `tests/test_fixes_round2.py` (20 tests):
  - Key spellings: `sessionid`/`session_id`, `db_pass`/`dbpass`,
    `encryption_key`, `signing_key`, `csrf`, and a bare `pass` key are
    now sensitive (`bypass`/`compass`/`passage` are not — `pass` is
    only sensitive as a standalone key); inline-assignment learning
    covers `db_pass=...`.
  - Retroactive learning: learned secrets are persisted per run as
    SHA-256 digests + lengths in a `learned_secrets` table — never the
    secret itself, so the database file contains no recoverable
    secret — and `finish_run` re-redacts earlier occurrences and
    artifact files; a fresh store re-redacts from the persisted set
    on export, including the crash case where the run never finished.
    `PRAGMA secure_delete=ON` zeroes freed content so a secret an
    earlier write stored verbatim does not linger in free pages.
  - Run names and event type/name are redacted at write time.
  - `memory_write` key-name trap closed: when the `key` argument is
    itself a sensitive key name, the value is learned and redacted.
  - Inline-assignment trailing punctuation is stripped from learned
    values (`password=hunter2.` learns `hunter2`, period preserved).
  - Import runs the full redactor in two passes (learn the fixture's
    own secrets first, then redact) and persists the imported run's
    learned set, so hand-made fixtures carrying unlearned free-text
    or `db_pass`/`sessionid`-keyed secrets land scrubbed.
  - Generic values are not learned (stoplist: `password`, `secret`,
    `token`, `changeme`, ...), so "enter your password here" is no
    longer over-redacted.
  - "Never a traceback" now holds across the CLI: a corrupt SQLite
    store, a JSON case file without a `cases` key, a ~2000-level YAML
    file (new 100-level depth guard in `yamlmini`), an import fixture
    with missing or wrongly typed event fields, and importing a
    directory all print `error: ...` and exit 2.
  - YAML duplicate keys inside `- key: value` sequence items now
    raise `YamlSubsetError`, as the docstring claimed.
  - Replay `retrieve()` fallbacks are counted in
    `fallback_live_calls` (and `live_calls`) with `meta.live: True`,
    like model and tool fallbacks.
  - Docs updated to match: redaction sections in README,
    ARCHITECTURE, and `docs/architecture.md`, fallback accounting in
    `docs/replay-modes.md`, the yamlmini docstring, and a PEP 668
    note (`pip install -e .` needs a venv or `--break-system-packages`
    on Debian/Ubuntu).
- **Redaction leaks (QA audit).** Substring matching on field names
  (`db_password`, `auth_token`, `x-api-key`, `clientSecret`, ...),
  shape matching for JWTs, PEM keys, long hex strings and inline
  `password=...` assignments, secret learning (a value first seen
  under a sensitive key, or registered, is scrubbed from all free
  text in the run), and redaction of run metadata. The 10-probe
  audit suite plus harder nested/artifact probes now shows 0 leaks
  (`tests/test_secrets_probes.py`). Honest limit documented: an
  arbitrary string never seen under a sensitive key, never
  registered, and matching no known shape cannot be distinguished
  from ordinary text and is not redacted.
- **README commands now run verbatim.** `record -- <script>` records
  an agent script as a subprocess; `eval` accepts YAML case files
  (small subset reader, `agenttrace/yamlmini.py`) and reports bad
  files as plain `error: ...` (exit 2), never a traceback; run-id
  examples use the real `run_<16 hex>` format; install instructions
  added. Every Quickstart command is covered by
  `tests/test_readme_commands.py`.
- **Exact-mode approval marker.** `replay --mode exact
  --allow-side-effects` now records `meta.side_effect_approved: True`
  on the served result (and lists the tool in
  `report["approved_side_effects"]`); the flag is never silently inert.
- **Order-aware demo stub.** The scripted model answers about the
  order id in the query instead of always answering about ORD-1001.
- **`import` command added** (spec M5): imports exported fixtures,
  re-redacts on the way in, and mints fresh run/event ids on collision.
- **Diff of a missing run is an error** (`ValueError`), not a phantom
  diff.
- **`--db` accepts a file path** (`agenttrace.db`) as well as a store
  directory. Read-only commands refuse a missing store instead of
  creating an empty one as a side effect.
- **`timeline --payloads`** shows each event's input/output.
  `eval` can evaluate a recorded run (`input: {run_id: ...}`) without
  re-running the agent live.

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
