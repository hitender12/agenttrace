# Record & replay a multi-tool agent run

Records one run of a small customer-support agent that uses **three
different tools** — `search_kb`, `get_order`, and `send_email` — plus
retrieval, a model call, a policy decision, a browser action, and a
memory write. Then replays it in `exact` mode and verifies the replay
reproduces the recorded final output with **zero** model calls, while
the side-effecting `send_email` tool is blocked (mocked) instead of
being executed live.

Everything is deterministic: the model is AgentTrace's `ScriptedModel`
stub — no network, no real model calls, no cost.

## Run

From the repository root:

```bash
python examples/record-and-replay/multi_tool_agent.py
```

Options:

```bash
python examples/record-and-replay/multi_tool_agent.py --query "Where is my delivery for order ORD-2002?"
python examples/record-and-replay/multi_tool_agent.py --db /tmp/my-agenttrace-store
```

## Expected output

```text
recorded run run_...
  tools used: ['search_kb', 'get_order', 'send_email']
  model stub calls while recording: 1
  final output: Your refund of $49.00 for order ORD-1001 has been approved. ...

replayed run_... in exact mode -> new run run_...
  mode: exact  replayed calls: 4  live calls: 0  fallback live calls: 0
  blocked side effects: ['send_email']
  model stub calls during replay: 0
  replayed final output: Your refund of $49.00 ...

OK: exact replay reproduced the recorded run (same final output, 0 model calls, email blocked).
```

Exit code is `0` on success; any assertion failure exits non-zero.

## Inspect the runs afterwards

Runs are stored in `examples/record-and-replay/.agenttrace/`
(git-ignored):

```bash
python -m agenttrace list --db examples/record-and-replay/.agenttrace
python -m agenttrace timeline <run_id> --db examples/record-and-replay/.agenttrace
python -m agenttrace replay <run_id> --mode exact --db examples/record-and-replay/.agenttrace
```

## Caveat

The `send_email` message id is random when recorded, so two fresh
recordings of the same query differ in that one tool-result field.
Replay still returns the recorded value verbatim, so a replayed run
is identical to its own recording — and `diff` ignores tool results,
so two recordings of the same query still diff as "no changes"
(see `../diff-runs/`).
