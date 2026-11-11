# AgentTrace — Record, replay, test, and debug AI-agent runs across frameworks

> **AgentTrace is a framework-independent flight recorder for AI agents.** Record a run, replay it exactly, diff two runs, and turn a failure into a regression test — without spending money on a second round of external calls.

```bash
agenttrace record -- python examples/customer_support_agent.py
agenttrace replay run_01J...
agenttrace diff run_old run_new
agenttrace eval tests/agent_cases.yaml
```

The commands above are the blueprint's killer workflow. In this reference
implementation, `record` runs the bundled demo agent (scripted, free,
deterministic); the SDK in `agenttrace/` is what you use to instrument your
own agent. See `examples/customer_support_agent.py`.

## Quickstart

```bash
# Record one run of the demo customer-support agent
python -m agenttrace record
python -m agenttrace record --query "Where is my delivery for order ORD-2002?"

# Inspect the timeline
python -m agenttrace list
python -m agenttrace timeline <run_id>

# Replay it exactly (no model calls, no live side effects)
python -m agenttrace replay <run_id> --mode exact

# Compare two runs
python -m agenttrace diff <run_a> <run_b>

# Run the regression suite (exits non-zero in --ci mode on any failure)
python -m agenttrace eval examples/agent_cases.json --ci

# Export a run as a portable test fixture
python -m agenttrace export <run_id> --out fixtures/run.json
```

Python 3.10+, standard library only (including `sqlite3`). No model API
key, no network calls, no paid services.

## What it does

- **Unified event model** — model request/response, tool call/result,
  retrieval, browser action, memory read/write, policy decision, error, and
  final output, with OpenTelemetry-compatible trace/span IDs.
- **Local-first recorder** — runs in SQLite (`agenttrace.db`) with a local
  artifact directory for request/response payloads.
- **Replay engine** — three explicit modes, stated in every report:
  - `exact`: recorded outputs for external calls; the model stub is never called.
  - `live`: the current stubs are called again (useful for comparison, not deterministic).
  - `hybrid`: recorded results for selected tools, live calls for the rest.
- **Safe replay** — side-effecting actions (payment, email, deletion) are
  never executed live on replay. They are blocked by default and return a
  `blocked_on_replay` mock; explicit approval is required and is recorded
  in the run so the approval is auditable.
- **Regression tests** — define input cases + expected properties
  (tool sequence, structured output, task success, latency, cost) in JSON;
  `eval --ci` exits non-zero when an assertion regresses.
- **Diff view** — changed prompts, tool arguments, retrieved documents,
  model metadata, and output fields between two runs.
- **Redaction** — secrets and configurable sensitive fields are removed
  before persistence *and* again on export. A recorded API key never
  appears in the database file or an exported fixture (tested).
- **Dataset export** — selected runs exported as portable JSON fixtures.

## The demo that should make developers share it

Record an agent failing on a customer-support task. Open one timeline and
see the exact model call, tool arguments, retrieved records, and failure.
Fix the code, replay the original recorded tools, and watch the test pass
without spending money on a second round of external tool calls.

```bash
python -m agenttrace record --query "I want a refund for order ORD-1001"
python -m agenttrace replay <run_id> --mode exact   # model stub: 0 calls
python -m agenttrace eval examples/agent_cases.json --ci
```

## Honest status

This is a **Python reference implementation** of the AgentTrace blueprint.
What genuinely works: the full event model, SQLite + artifact recording,
the three replay modes with side-effect gating, diff, redaction (tested
against a fake API key), dataset export, and the eval/CI runner — all
verified by the test suite below.

What is **not** built here (and is stated as the production path in
`ARCHITECTURE.md`): the Rust trace engine, OTLP ingestion/export, the
TypeScript SDK, framework adapters, MCP integration, PostgreSQL server
mode, and the Next.js dashboard. The model is a deterministic scripted
stub, not a real LLM; replay determinism is therefore about the recorded
evidence, not about reproducing sampling.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Repository layout

```text
agenttrace/           # SDK + recorder + replay + diff + eval + CLI
  events.py           # unified event model (OTel-compatible IDs)
  store.py            # SQLite store + artifact directory
  redaction.py        # secret/sensitive-field redaction
  runtime.py          # recording runtime + tool stubs + KB
  model_stub.py       # deterministic scripted model
  replay.py           # exact / live / hybrid replay engine
  diff.py             # run diff
  evals.py            # regression cases + CI exit code
  export.py           # dataset export
  demo_agent.py       # demo customer-support agent
  __main__.py         # CLI: record / replay / diff / eval / export / list / timeline
examples/             # runnable SDK example + eval cases
tests/                # unittest suite
```

## License

MIT. See `LICENSE`.
