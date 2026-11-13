# AgentTrace — Record, replay, test, and debug AI-agent runs across frameworks

> **AgentTrace is a framework-independent flight recorder for AI agents.** Record a run, replay it exactly, diff two runs, and turn a failure into a regression test — without spending money on a second round of external calls.

Install it (Python 3.10+, standard library only) and the `agenttrace`
command becomes available:

```bash
pip install -e .
agenttrace record -- python3 examples/customer_support_agent.py
agenttrace replay run_9f2c1a7b4e8d03f5
agenttrace diff run_9f2c1a7b4e8d03f5 run_1a2b3c4d5e6f7081
agenttrace eval examples/agent_cases.yaml
```

On PEP 668 systems (Debian/Ubuntu, where `pip install` refuses to
touch the system environment) run it inside a virtualenv, or use
`pip install -e . --break-system-packages`. The `python3 -m agenttrace`
commands in the Quickstart below need no install at all.

Run ids look like `run_` followed by 16 hex characters (for example
`run_9f2c1a7b4e8d03f5`); `agenttrace list` shows the real ones. The
commands above are the blueprint's killer workflow. In this reference
implementation, `record` runs the bundled demo agent (scripted, free,
deterministic) or — with `--` and a script path — records your own
agent script; the SDK in `agenttrace/` is what you use to instrument
your own agent. See `examples/customer_support_agent.py`.

Every command in the Quickstart below runs verbatim from the repository
root (verified by `tests/test_readme_commands.py`); `python3` is used
because that is the interpreter name on this machine — plain `python`
works wherever it exists. The hero block above shows the workflow
shape with placeholder run ids — use `agenttrace list` for real ones.

## Quickstart

```bash
# Record one run of the demo customer-support agent
python3 -m agenttrace record
python3 -m agenttrace record --query "Where is my delivery for order ORD-2002?"

# Record your own agent script instead (the script records itself
# with the SDK; its run lands in the --db store — put --db before `--`)
python3 -m agenttrace record -- python3 examples/customer_support_agent.py

# Inspect the timeline (add --payloads to see each event's input/output)
python3 -m agenttrace list
python3 -m agenttrace timeline <run_id>
python3 -m agenttrace timeline <run_id> --payloads

# Replay it exactly (no model calls, no live side effects)
python3 -m agenttrace replay <run_id> --mode exact

# Compare two runs
python3 -m agenttrace diff <run_a> <run_b>

# Run the regression suite (exits non-zero in --ci mode on any failure)
python3 -m agenttrace eval examples/agent_cases.json --ci
python3 -m agenttrace eval examples/agent_cases.yaml --ci

# Export a run as a portable test fixture, and import one back
python3 -m agenttrace export <run_id> --out fixtures/run.json
python3 -m agenttrace import fixtures/run.json
```

`--db` accepts a store directory (default `.agenttrace/`) or the
`agenttrace.db` file inside one, for example
`--db .agenttrace/agenttrace.db`. Bad input (a missing case file, an
unknown run id, a malformed fixture, a corrupt store, an import
fixture with missing or wrongly typed event fields, importing a
directory) prints a plain `error: ...` message and exits 2 — never a
traceback.

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
  `blocked_on_replay` mock; explicit approval (`--allow-side-effects`)
  is required, and the approval is recorded in the run
  (`meta.side_effect_approved: True`) so it is auditable — including in
  `exact` mode, where the recorded result is served under that approval.
- **Regression tests** — define input cases + expected properties
  (tool sequence, structured output, task success, latency, cost) in
  JSON or YAML; `eval --ci` exits non-zero when an assertion regresses.
  A case can also evaluate a *recorded* run instead of re-running the
  agent live: `input: {run_id: run_9f2c1a7b4e8d03f5}`.
- **Diff view** — changed prompts, tool arguments, retrieved documents,
  model metadata, and output fields between two runs. Diffing a run id
  that does not exist is an error, not an empty diff.
- **Redaction** — secrets are removed before persistence *and* again on
  export, by five mechanisms: (1) substring matching on field names —
  any key containing `password`, `secret`, `token`, `api_key`, `auth`,
  `credential`, `private`, `sessionid`, `db_pass`, `encryption_key`,
  `signing_key`, `csrf`, or a bare `pass` key is redacted, whatever its
  exact spelling (`db_password`, `auth_token`, `x-api-key`,
  `clientSecret`) — with care that `bypass`/`compass`/`passage` are not
  matched; (2) shape matching in free text (`sk-...`, `ghp_...`, AWS
  keys, Slack tokens, `Bearer ...`, JWTs, PEM private keys, long hex
  strings, `password=...` assignments, with trailing punctuation
  stripped so `password=hunter2.` learns `hunter2`); (3) secret
  learning — a value first seen under a sensitive key, or registered
  with `Redactor.register_secret()`, is scrubbed from all free text in
  the run, so the same secret in an error message or tool result is
  removed too — and learning is retroactive: learned secrets are
  persisted per run as SHA-256 digests + lengths (never the secret
  itself), `finish_run` re-redacts earlier occurrences and artifact
  files, and a fresh store re-redacts from the persisted set on
  export, so the fix survives a restart; (4) run names and event
  type/name are redacted at write time; (5) the `memory_write` key
  trap is closed — when the `key` argument of `memory_write` is itself
  a sensitive key name, the value is learned and redacted. Run metadata
  is redacted as well. Generic values are not learned (`password`,
  `secret`, `token`, ...), so "enter your password here" stays
  readable. Honest limit: an arbitrary string that never appeared under
  a sensitive key, was never registered, and matches no known secret
  shape cannot be distinguished from ordinary text by any redactor,
  and is not redacted — if your agent handles a credential, pass it
  under a sensitive field name or register it. (Tested by
  `tests/test_secrets_probes.py`: 10 probes plus harder nested/artifact
  cases, and `tests/test_fixes_round2.py`: 20 round-2 regression tests,
  both at 0 leaks.)
- **Dataset export/import** — selected runs exported as portable JSON
  fixtures (`export`), and imported back into any store (`import`).
  Importing the same fixture twice mints fresh run/event ids instead
  of colliding, and re-redacts everything on the way in — import runs
  the full redactor, learning the fixture's own secrets first, so a
  hand-made fixture carrying an unlearned free-text secret or a
  `db_pass`/`sessionid`-keyed one lands scrubbed.

## The demo that should make developers share it

Record an agent failing on a customer-support task. Open one timeline and
see the exact model call, tool arguments, retrieved records, and failure.
Fix the code, replay the original recorded tools, and watch the test pass
without spending money on a second round of external tool calls.

```bash
python3 -m agenttrace record --query "I want a refund for order ORD-1001"
python3 -m agenttrace replay <run_id> --mode exact   # model stub: 0 calls
python3 -m agenttrace eval examples/agent_cases.yaml --ci
```

## Honest status

This is a **Python reference implementation** of the AgentTrace blueprint.
What genuinely works: the full event model, SQLite + artifact recording,
the three replay modes with side-effect gating and auditable approvals
(retrieval fallbacks are counted as live, not replayed), diff,
redaction (five mechanisms, tested against a 10-probe suite plus 20
round-2 regression tests, 0 leaks), dataset export/import, and the
eval/CI runner for JSON and YAML cases and for recorded runs — all
verified by the test suite below.

What is **not** built here (and is stated as the production path in
`ARCHITECTURE.md`): the Rust trace engine, OTLP ingestion/export, the
TypeScript SDK, framework adapters, MCP integration, PostgreSQL server
mode, and the Next.js dashboard. The model is a deterministic scripted
stub, not a real LLM; replay determinism is therefore about the recorded
evidence, not about reproducing sampling. The stub answers about the
order id in the query (it is order-aware); the knowledge base and tools
are in-memory stubs.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Repository layout

```text
agenttrace/                 # SDK + recorder + replay + diff + eval + CLI
  __init__.py             # public API: Store, runtimes, diff, eval, export/import
  __main__.py             # CLI: record / replay / diff / eval / export / import / list / timeline
  events.py               # unified event model (OTel-compatible IDs)
  store.py                # SQLite store + artifact directory (--db: dir or .db file)
  redaction.py            # secret/sensitive-field redaction (keys, shapes, learning)
  runtime.py              # recording runtime + tool stubs + KB
  model_stub.py           # deterministic scripted model (order-aware)
  replay.py               # exact / live / hybrid replay engine
  diff.py                 # run diff (missing runs are an error)
  evals.py                # regression cases (JSON/YAML, live or recorded runs) + CI exit code
  yamlmini.py             # small YAML-subset reader for case files
  export.py               # dataset export (re-redacted)
  importer.py             # dataset import (re-redacted, collision-safe)
  demo_agent.py           # demo customer-support agent
docs/
  architecture.md         # component overview
  replay-modes.md         # exact / live / hybrid semantics + side-effect rule
examples/
  customer_support_agent.py   # instrument-your-own-agent example (record -- python3 ...)
  agent_cases.json            # regression cases (JSON)
  agent_cases.yaml            # regression cases (YAML)
  record-and-replay/          # multi-tool record + replay example
  diff-runs/                  # diff example
  eval-ci/                    # eval-in-CI example
fixtures/
  research_agent.py / .json   # recorded-run fixtures
  support_retrieval_miss.py / .json
tests/
  test_agenttrace.py      # core: record / replay / diff / eval / export
  test_edge_cases.py      # redaction shapes, failed-tool replay, eval CI, perf smoke
  test_fixtures.py        # fixture loading
  test_secrets_probes.py  # 10-probe redaction suite + harder probes (CI)
  test_fixes.py           # fix regressions: YAML eval, import, markers, CLI errors
  test_fixes_round2.py    # round-2 redaction regressions (20 tests, CI)
  test_readme_commands.py # every command in this README runs verbatim
.github/workflows/ci.yml # tests on Python 3.10/3.11/3.12
pyproject.toml            # packaging (console script: agenttrace)
CHANGELOG.md  CONTRIBUTING.md  CODE_OF_CONDUCT.md  SECURITY.md  LICENSE
ARCHITECTURE.md
```

## License

MIT. See `LICENSE`.
