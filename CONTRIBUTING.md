# Contributing to AgentTrace

Thank you for helping make AgentTrace better. This guide covers local
setup, running the tests, adding a framework adapter, and what we expect
from a pull request.

## Local setup

AgentTrace is a Python reference implementation. It uses **Python 3.10+**
and the **standard library only** — there are no third-party
dependencies, no `requirements.txt`, and no network installs.

```bash
git clone https://github.com/hitender12/agenttrace.git
cd agenttrace
python3 --version   # must be 3.10 or newer
```

No virtual environment is required, but one is fine if you prefer it:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Verify the setup with the quickstart from the README:

```bash
python -m agenttrace record
python -m agenttrace list
python -m agenttrace replay <run_id> --mode exact
```

Recording writes to `.agenttrace/` (SQLite database
`agenttrace.db` plus an `artifacts/` directory) in the current working
directory. That directory is local scratch data — do not commit it, and
do not share it without checking it for sensitive content first (see
`SECURITY.md`).

## Running the tests

The test suite uses the standard library `unittest` — no pytest, no
extra installs:

```bash
python -m unittest discover -s tests -v
```

The suite (28 tests across `tests/test_agenttrace.py`,
`tests/test_edge_cases.py`, and `tests/test_fixtures.py`) covers
recording, exact replay without model calls, the side-effect gate
(blocked by default, allowed with explicit approval), hybrid replay,
redaction of a fake API key and of every secret pattern/key, failed
tool calls on replay, diffing, eval/CI exit codes, export round-trips
and fixture snapshots, retrieval-miss handling, and recording/replaying
several hundred events.
Every test uses a temporary directory for its store, so running the
suite does not touch your real `.agenttrace/` data.

Before opening a PR, run the full suite and confirm it passes. If you
change replay, redaction, or eval behaviour, add or update a test that
pins the behaviour — a test that proves the new behaviour is required,
not optional.

There is no linter or formatter configured in this repository yet. Keep
code in the existing style: short module docstrings that state what the
module does and what it deliberately does not do, type hints on public
functions, and no new third-party dependencies without discussion in
an issue first.

## How the SDK works (the pattern adapters follow)

The SDK surface is the `RecordRuntime` context manager in
`agenttrace/runtime.py`, plus the `ReplayRuntime` in
`agenttrace/replay.py`. Both share the `_Recorder` base class, which
provides the same event-writing helpers to record and replay:

| Helper | Event(s) written |
|---|---|
| `rt.model(prompt)` | `model_request` + `model_response` |
| `rt.tool(name, args, side_effect=False)` | `tool_call` + `tool_result` |
| `rt.retrieve(query)` | `retrieval` |
| `rt.memory_read(key)` / `rt.memory_write(key, value)` | `memory_read` / `memory_write` |
| `rt.policy_decision(name, input, output)` | `policy_decision` |
| `rt.browser_action(action, url)` | `browser_action` |
| `rt.final_output(text)` | `final_output` |
| `rt.error(name, message)` | `error` (status `error`) |

Every event carries an OpenTelemetry-compatible `trace_id` (128-bit,
32 hex chars) and `span_id` (64-bit, 16 hex chars), a sequence number,
timestamps, duration, status, `input`/`output` payloads, and `meta`
(model name, tokens, cost). See `docs/architecture.md` for the full
event model and storage layout.

`examples/customer_support_agent.py` is the canonical example of
instrumenting an agent: it wraps each step of the demo agent
(`agenttrace/demo_agent.py`) in these helpers inside a
`RecordRuntime` block. Copy that shape for your own agent.

## How to add a framework adapter

A framework adapter is a thin integration that routes a framework's
model and tool calls through the SDK above — it must never force the
framework on users, and it must not change the recorded event format.

1. **Instrument, don't reimplement.** Wrap the framework's model call
   with `rt.model(prompt)` and each tool invocation with
   `rt.tool(name, args)`. Use `rt.retrieve()`,
   `rt.policy_decision()`, and the memory/browser helpers for the
   matching framework concepts. Keep event names stable: replay
   matches recorded events by `(type, name)` in call order, so
   renaming a tool breaks exact replay of older runs.
2. **Register tools in `agenttrace/runtime.py`.** Built-in tools live
   in two dicts that must be updated together:
   - `TOOL_IMPLS`: `name -> callable(args) -> dict`
   - `TOOL_SIDE_EFFECTS`: `name -> bool` — set `True` for any tool
     that changes the world (sends email, charges a card, deletes
     data, writes to an external system). Side-effecting tools are
     blocked on replay by default; mislabelling one is a safety bug,
     not a cosmetic issue. When in doubt, mark it `True` and let the
     caller pass `side_effect=True` explicitly.
   - Prefer passing `side_effect=True` at the call site
     (`rt.tool("send_email", {...}, side_effect=True)`) over relying
     on the registry when the side-effect nature depends on the
     arguments.
3. **Keep the model swappable.** `RecordRuntime` accepts a `model`
   argument (`ScriptedModel` by default). An adapter should accept a
   model object with a `.complete(prompt, model=None)` method
   returning `{"text", "model", "tokens_in", "tokens_out",
   "cost_usd"}` — see `agenttrace/model_stub.py` for the contract.
   Do not hard-code API keys or provider SDKs into the recorder; if a
   key must travel with a request (for example in headers), the store
   redacts it before persistence, but the adapter should still avoid
   logging it.
4. **Add tests and an example.** A new adapter needs at least:
   - a test that records a run through the adapter and replays it in
     `exact` mode with zero live model calls;
   - a test that a side-effecting tool from the adapter is blocked
     on replay (`{"status": "blocked_on_replay"}`);
   - a runnable example under `examples/` following
     `customer_support_agent.py`.
5. **Document what it does not do.** The production path (Rust engine,
   OTLP ingestion, TypeScript SDK, MCP integration, PostgreSQL server
   mode, dashboard) is described in `ARCHITECTURE.md` and is not part
   of this reference implementation. An adapter PR should not claim
   those features, and should note its own limits in its docstring
   and example (for example: "in-memory stubs only, no live provider
   calls").

## Pull request expectations

- **Small and reviewable.** One change per PR. If a PR touches
  replay, redaction, and the CLI at once, split it.
- **Tests pass.** `python -m unittest discover -s tests -v` must be
  green, including any new tests you add for the change.
- **Docs match the code.** If you change a CLI flag, a replay mode,
  an event type, or a redaction rule, update `README.md`,
  `ARCHITECTURE.md`, or the relevant file under `docs/` in the same
  PR. Do not describe features that do not exist.
- **No secrets in fixtures.** Never commit a real API key, token, or
  password — in code, in tests, or in an exported run fixture. The
  only key that may appear is the obviously fake
  `sk-test-...` value in `agenttrace/demo_agent.py`, which exists to
  prove redaction works. If a fixture you want to share contains
  model outputs or tool results with real user data, redact or
  replace them first (see `SECURITY.md`).
- **Honest claims.** Do not claim replay is deterministic for real
  LLM calls, do not claim a security property the code does not
  enforce, and do not present the scripted `ScriptedModel` stub as a
  real model. The README's "Honest status" section is the standard:
  state what works, what is not built, and what the limits are.
- **Describe the change.** In the PR body, say what changed, why,
  how you tested it (commands and results), and what you deliberately
  left out. Link the issue if there is one.

## Reporting bugs and requesting features

Open a GitHub issue with a minimal reproduction: the command you ran,
the Python version, the smallest agent/query that triggers it, and
what you expected versus what happened. For a replay bug, attach the
exported fixture (`python -m agenttrace export <run_id> --out
fixture.json`) after checking it contains no secrets — exports are
redacted a second time on the way out, but check anyway. For a
security issue, do **not** open a public issue; follow `SECURITY.md`.
