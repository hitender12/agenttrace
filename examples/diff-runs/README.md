# Diff two agent runs

Records two runs of the demo support agent with different queries
(refund vs. shipping), diffs them with `diff_runs` / `render_diff`,
and prints every difference: changed prompts, tool sequence and
arguments, retrieved documents, model metadata (tokens/cost), and
final output. Then records the refund query a second time and shows
the "no changes" diff for identical behaviour.

## Run

From the repository root:

```bash
python examples/diff-runs/diff_two_runs.py
```

The example uses a fresh temporary store, so it leaves no state
behind. Exit code is `0` on success.

## Expected output

```text
run A (refund):  run_... -> Your refund of $49.00 for order ORD-1001 has been approved. ...
run B (shipping): run_... -> Your order ORD-1001 was delivered on 2026-11-08 ...

diff run_... -> run_...
- prompt_changed: model_request[0].prompt
    old: 'You are a support agent. Use these docs:\n- Refund policy: ...'
    new: 'You are a support agent. Use these docs:\n- Shipping status: ...'
- tool_sequence_changed: tool_sequence
    old: ['get_order', 'send_email']
    new: ['get_order']
- tool_args_changed: tool_call[0].args
    old: {'order_id': 'ORD-1001'}
    new: {'order_id': 'ORD-2002'}
- retrieval_changed: retrieval[0].documents
    old: ['doc-refund-policy', 'doc-order-lookup']
    new: ['doc-shipping']
- model_metadata_changed: model_response[0].tokens_in
    ...
- output_changed: final_output.text
    old: 'Your refund of $49.00 ...'
    new: 'Your order ORD-1001 was delivered ...'

change kinds found: ['model_metadata_changed', 'output_changed', 'prompt_changed',
 'retrieval_changed', 'tool_args_changed', 'tool_sequence_changed']

diff run_... -> run_...
no changes

OK: diff detected every change class for A -> B and reported no changes for A -> C.
```

(Run ids and token counts vary per run; the change *kinds* do not.)

## Same thing via the CLI

```bash
python -m agenttrace record --name run-a --query "I want a refund for order ORD-1001"
python -m agenttrace record --name run-b --query "Where is my delivery for order ORD-2002?"
python -m agenttrace diff <run_a> <run_b>
```

## Caveat

Diff compares by call order, not by semantic alignment of steps, and
it compares tool *arguments* rather than tool *results* — so the
random `send_email` message id does not show up as a change. See
`ARCHITECTURE.md` ("Honest limits").
