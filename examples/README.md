# Examples

Runnable examples for the AgentTrace Python SDK. All of them use the
project's deterministic `ScriptedModel` stub — no network, no real
model calls, no API key, no cost. Run them from the repository root
with Python 3.10+ and no extra dependencies (standard library only).

| Example | Demonstrates | Run |
|---|---|---|
| [`customer_support_agent.py`](customer_support_agent.py) | Instrumenting your own agent: record one run of the bundled demo support agent to `.agenttrace/` and print the run id | `python examples/customer_support_agent.py` |
| [`record-and-replay/`](record-and-replay/) | Record a multi-tool agent run (3 tools: `search_kb`, `get_order`, `send_email`) and replay it exactly — same output, 0 model calls, side effect blocked | `python examples/record-and-replay/multi_tool_agent.py` |
| [`diff-runs/`](diff-runs/) | Diff two runs (refund vs. shipping) and print every difference; plus the "no changes" diff for identical runs | `python examples/diff-runs/diff_two_runs.py` |
| [`eval-ci/`](eval-ci/) | Run the eval suite in CI mode: a passing case (exit `0`) and a deliberately failing case (exit `1`) | `python examples/eval-ci/run_eval_ci.py; echo "exit=$?"` |
| [`agent_cases.json`](agent_cases.json) | Eval case file used by the CLI: refund flow, shipping status, cancellation | `python -m agenttrace eval examples/agent_cases.json --ci` |

## The CLI equivalents

The scripts above call the SDK directly. The same workflows exist
as CLI commands (see the repo `README.md` quickstart):

```bash
python -m agenttrace record --query "I want a refund for order ORD-1001"
python -m agenttrace replay <run_id> --mode exact
python -m agenttrace diff <run_a> <run_b>
python -m agenttrace eval examples/agent_cases.json --ci
python -m agenttrace export <run_id> --out fixtures/run.json
python -m agenttrace timeline <run_id>
```

## Notes

- `record-and-replay` keeps its store in
  `examples/record-and-replay/.agenttrace/` (git-ignored) so you can
  inspect the runs afterwards with `timeline` / `list` / `replay`.
  The `diff-runs` and `eval-ci` examples use a fresh temporary store
  on every run and leave no state behind.
- The `send_email` message id is random when recorded, so two fresh
  recordings of the same query differ in that one tool-result field.
  Replay returns the recorded value verbatim, and `diff` compares
  tool arguments rather than tool results, so neither example is
  affected by it.
