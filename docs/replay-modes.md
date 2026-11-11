# Replay modes

AgentTrace replays a recorded run by re-running the same agent
function against the recorded events. The replay engine
(`agenttrace/replay.py`, `ReplayRuntime`) indexes the source run's
events by `(type, name)` in call order and serves them back in order.

Do not claim that replaying an AI run always reproduces the same
model output. The model provider, sampling, time, external state, and
hidden environment can change. In this reference implementation the
model is a deterministic scripted stub, so replay determinism here
means "the recorded evidence is reproduced" — not "sampling is
reproduced". For a real LLM, exact replay reproduces the *recorded*
outputs, which is what lets you test downstream application logic
without paying for a second round of external calls.

**Every replay report states which mode was used.** The CLI prints
the mode, the replayed / live / fallback-live call counts, and the
blocked side effects after every replay. Every replay also creates a
new run row with `mode=replay:<mode>` and `replay_of=<source run id>`,
so a replay is itself a recorded, inspectable run.

```bash
python -m agenttrace replay <run_id> --mode exact
python -m agenttrace replay <run_id> --mode live
python -m agenttrace replay <run_id> --mode hybrid --hybrid-tools search_kb,get_order
```

## Exact replay (`--mode exact`, the default)

Model and tool calls return their recorded outputs. The model stub is
never invoked — the test suite verifies this with a call counter
(`test_exact_replay_reproduces_logic_without_model_calls`).

- **Model:** the recorded `model_response` text is returned; the
  replay records a new `model_request`/`model_response` pair marked
  `meta.replayed: True` (and `replayed_from` pointing at the source
  event).
- **Tools:** the recorded `tool_result` is returned, marked
  `meta.replayed: True`.
- **Retrieval:** the recorded documents are returned, marked
  `meta.replayed: True`.
- **Memory / policy / browser / output:** these helpers are shared
  with the recorder (`_Recorder` in `runtime.py`), so they execute
  their normal behaviour and record a fresh event — memory reads
  return values written during *this* replay run, and
  `browser_action` returns its stubbed fixture page. They are not
  served from the recording.

Best for: reproducing downstream application logic, debugging a
failure, and turning a failure into a regression test — without
spending money on a second round of external calls.

## Live re-run (`--mode live`)

The current stubs are called again, and the results are recorded as
live (`meta.live: True`). The model stub runs, tools execute their
current implementations, and retrieval re-looks up the knowledge base.

Useful for: comparing a run against the current code and current
tools — for example, after fixing the agent, re-run live and `diff`
against the original to see what changed. **Not deterministic:** if a
tool or the model changed, the results change. Do not use live mode to
prove a bug is fixed; use exact replay plus `eval` for that.

## Hybrid replay (`--mode hybrid --hybrid-tools TOOL,...`)

Recorded results for selected tools, live calls for the rest. The
model is **always** replayed exactly in hybrid mode — only tools are
ever live.

```bash
# Replay the model and get_order exactly, but call search_kb live
python -m agenttrace replay <run_id> --mode hybrid --hybrid-tools search_kb
```

Best for: testing one changed tool in isolation while keeping
everything else frozen. For example, if you changed the knowledge
base, replay hybrid with `search_kb` live to see whether the new
retrieval changes the outcome, while the model and all other tools
stay exactly as recorded.

## What happens when the agent diverges

If the agent's path diverges from the recording — a recorded output
is missing because the agent now calls a model or tool in a different
order, or an extra time — the engine **falls back to a live call**
and records the fallback in the report
(`fallback_live_calls`), instead of failing silently. A replay with
fallbacks is still a valid replay, but read the report: a high
fallback count means the agent has changed enough that the recording
no longer describes its behaviour, and exact replay is no longer a
faithful reproduction of the original run.

## Side-effect safety rules

These rules apply in **all three modes**. They are the most important
part of replay, and they are not optional.

### 1. Side effects are blocked by default

A tool is side-effecting if it is flagged in `TOOL_SIDE_EFFECTS` in
`runtime.py` or called with `side_effect=True` at the call site.
Built-in side-effecting tool: `send_email`. On replay, a
side-effecting tool is **never executed live by default**. Instead
the engine:

- records a `policy_decision` event (`side_effect_gate`,
  `{"allowed": false, "reason": "side effects are blocked on replay"}`);
- records the `tool_call` and a `tool_result` of
  `{"status": "blocked_on_replay", "tool": <name>}`;
- adds the tool name to `report["blocked_side_effects"]`;
- returns `{"status": "blocked_on_replay"}` to the agent, which must
  handle that value (the demo agent ignores the email result, so it
  continues normally).

This is what "never silently replay a payment, email, deletion, or
other side effect" means in practice: the action does not happen, and
the run says so.

### 2. Explicit approval is required — and is auditable

Passing `--allow-side-effects` on the CLI (or
`allow_side_effects=True` to `ReplayRuntime`) executes side-effecting
tools for real. The resulting `tool_result` event is marked
`meta.side_effect_approved: True`, so the approval is visible in the
recorded run and in any export of it. Approval is per replay run,
not a stored setting — every replay that allows side effects must ask
again.

### 3. Mark new side-effecting tools honestly

When you add a tool (see `CONTRIBUTING.md`), set its entry in
`TOOL_SIDE_EFFECTS` to `True` if it changes the world — sends email,
charges a card, deletes data, writes to an external system, or
mutates shared state. When in doubt, mark it `True`. Mislabelling a
side-effecting tool as safe is a safety bug: it lets the tool execute
on replay without approval. A read-only tool (`get_order`,
`search_kb`) is safe to leave `False`.

### 4. Replay only where side effects are safe

Even with the gate, live and hybrid replay execute the *current*
tool stubs for the tools you mark live, and `--allow-side-effects`
executes side-effecting tools for real. Replay a run only in an
environment where that is acceptable — a local dev machine with stub
tools, not against production systems or real customer data. The
gate blocks replayed side effects; it does not sandbox a live tool
that you chose to run.

### 5. The report is the record

After every replay, check the report the CLI prints:

```text
replayed <source> in exact mode -> new run <run_id>
  mode: exact  replayed calls: 5  live calls: 0  fallback live calls: 0
  blocked side effects: ['send_email']
  model stub calls during replay: 0
```

If `live calls` or `fallback live calls` is non-zero in exact mode,
or `blocked side effects` lists a tool you expected to run, the
replay did not do what you assumed — inspect the new run's timeline
before drawing conclusions from it:

```bash
python -m agenttrace timeline <new_run_id>
```
