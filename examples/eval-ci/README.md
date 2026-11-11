# Eval suite in CI mode

Runs the AgentTrace regression suite the way CI does: each case runs
the demo agent live (against the scripted model stub — no network,
no real model calls, no cost) and is checked against its expected
properties (tool sequence, output content, task success, latency,
cost). `eval_exit_code` is `0` when every case passes and `1` when
anything regresses — that exit code is the CI contract.

## Run

From the repository root:

```bash
python examples/eval-ci/run_eval_ci.py; echo "exit=$?"
```

The script runs two phases in a fresh temporary store:

- **Phase 1** — `cases_passing.json` (one refund-flow case): all pass,
  CI exit code `0`.
- **Phase 2** — `cases.json` (the same passing case plus one
  **deliberately failing** case whose expectations are wrong on
  purpose: it expects tool sequence `["search_kb"]` and output
  containing `"spaceship"` for a refund query): 1 passed, 1 failed,
  CI exit code `1`.

The script itself exits `1` at the end, **on purpose**, to demonstrate
the failing CI gate. That is the expected result — do not "fix" it by
removing the failing case; the failing case is the point.

## Expected output

```text
== Phase 1: passing cases only (cases_passing.json) ==
[PASS] refund flow (latency ... ms, cost $0.000...)
1 passed, 0 failed
CI mode: exit 0

== Phase 2: passing + deliberately failing case (cases.json) ==
[PASS] refund flow (latency ... ms, cost $0.000...)
[FAIL] deliberately failing: wrong tool sequence + impossible output (...)
       failed check tool_sequence: expected ['search_kb'], got ['get_order', 'send_email']
       failed check output_contains: expected 'spaceship', got 'Your refund of $49.00 ...'
1 passed, 1 failed
CI mode: exit 1

OK: CI mode exits 0 when all cases pass and 1 when any assertion regresses.
This script now exits 1 on purpose, to demonstrate the failing CI gate (see Phase 2 above).
```

## Same thing via the CLI (the form a CI job uses)

```bash
# Green path — exits 0
python -m agenttrace eval examples/eval-ci/cases_passing.json --ci; echo "exit=$?"

# Red path — exits 1
python -m agenttrace eval examples/eval-ci/cases.json --ci; echo "exit=$?"
```

Without `--ci`, `eval` still prints the per-case results but always
exits `0`, so the `--ci` flag is what makes it a gate.

## Files

- `run_eval_ci.py` — the runnable demo (both phases, asserts the exit codes).
- `cases.json` — 1 passing case + 1 deliberately failing case.
- `cases_passing.json` — the passing case on its own (green CI run).
