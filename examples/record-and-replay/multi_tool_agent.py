"""Example: record and replay a multi-tool agent run.

Run it directly (from the repository root)::

    python examples/record-and-replay/multi_tool_agent.py

What it does:

1. Records one run of a small customer-support agent that uses THREE
   different tools -- ``search_kb``, ``get_order`` and ``send_email`` --
   plus retrieval, a model call, a policy decision, a browser action and
   a memory write.
2. Replays that run in ``exact`` mode with a fresh scripted model and
   asserts the replay reproduces the recorded final output.
3. Prints the replay report, including the blocked side effect:
   ``send_email`` is never executed live on replay.

Everything is deterministic: the model is the project's
``ScriptedModel`` stub (no network, no real model calls, no cost).
The only non-deterministic value is the ``send_email`` message id,
which is random on record but returned verbatim from the recording
on replay, so the replayed output is still identical.

The run is stored in ``examples/record-and-replay/.agenttrace/``
(git-ignored). Inspect it afterwards with::

    python -m agenttrace timeline <run_id> --db examples/record-and-replay/.agenttrace
    python -m agenttrace list --db examples/record-and-replay/.agenttrace
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agenttrace import (  # noqa: E402
    FAKE_API_KEY, RecordRuntime, ReplayRuntime, ScriptedModel, Store,
)
from agenttrace.events import EventType  # noqa: E402
from agenttrace.runtime import extract_order_id  # noqa: E402

QUERY = "I want a refund for order ORD-1001"
DEFAULT_DB = Path(__file__).resolve().parent / ".agenttrace"


def multi_tool_agent(rt, query: str) -> str:
    """A support agent using three different tools.

    ``rt`` is either a ``RecordRuntime`` (first run) or a
    ``ReplayRuntime`` (replay): the agent code is identical in both
    cases, which is the whole point of AgentTrace.
    """
    rt.policy_decision(
        "pii_check", {"query": query},
        {"allowed": True, "reason": "no PII patterns detected"})

    # Retrieval (knowledge-base lookup) + a tool that searches the KB.
    docs = rt.retrieve(query)
    kb = rt.tool("search_kb", {"query": query})
    context = "\n".join(
        f"- {d['title']}: {d['text']}" for d in docs)
    kb_titles = ", ".join(d["title"] for d in kb["results"])

    prompt = (
        "You are a support agent. Use these docs:\n"
        f"{context}\n\nCustomer query: {query}\n\nAnswer concisely."
    )
    answer = rt.model(prompt)

    # Tool 2: look the order up. Tool 3 (below) emails the customer.
    order_id = extract_order_id(query)
    order = rt.tool("get_order", {"order_id": order_id})
    rt.browser_action(
        "open", f"https://support.example.invalid/orders/{order_id}")
    print(f"  order: {order['order_id']} status={order['status']} "
          f"total=${order['total_usd']:.2f} (KB hits: {kb_titles})")

    if "refund" in query.lower():
        # Side-effecting tool: runs for real when recording, but is
        # BLOCKED (mocked) on replay -- see the replay report below.
        # The agent deliberately ignores the result, so a blocked
        # replay still reaches the same final output.
        rt.tool("send_email", {
            "to": "customer@example.com",
            "subject": f"Your refund for {order_id}",
            "body": answer,
        }, side_effect=True)

    rt.memory_write("last_topic", query)
    rt.final_output(answer)
    return answer


def tool_sequence(store: Store, run_id: str) -> list[str]:
    return [e.name for e in store.get_events(run_id)
            if e.type == EventType.TOOL_CALL.value]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record and exactly replay a multi-tool agent run.")
    parser.add_argument("--db", default=str(DEFAULT_DB),
                        help="store directory (default: %(default)s)")
    parser.add_argument("--query", default=QUERY)
    args = parser.parse_args()

    store = Store(args.db)
    try:
        # -- 1. Record -------------------------------------------------
        record_model = ScriptedModel(api_key=FAKE_API_KEY)
        with RecordRuntime(store, "example-multi-tool-run",
                           model=record_model, api_key=FAKE_API_KEY,
                           metadata={"query": args.query}) as rt:
            answer = multi_tool_agent(rt, args.query)
            run_id = rt.run_id
        tools = tool_sequence(store, run_id)
        print(f"recorded run {run_id}")
        print(f"  tools used: {tools}")
        print(f"  model stub calls while recording: {record_model.calls}")
        print(f"  final output: {answer}")
        assert len(set(tools)) >= 3, f"expected >= 3 tools, got {tools}"

        # -- 2. Exact replay -------------------------------------------
        replay_model = ScriptedModel(api_key=FAKE_API_KEY)
        with ReplayRuntime(store, run_id, mode="exact",
                           model=replay_model) as rt:
            replayed = multi_tool_agent(rt, args.query)
            report = rt.report
            replay_run_id = rt.run_id
        print(f"\nreplayed {run_id} in exact mode -> new run {replay_run_id}")
        print(f"  mode: {report['mode']}  "
              f"replayed calls: {report['replayed_calls']}  "
              f"live calls: {report['live_calls']}  "
              f"fallback live calls: {report['fallback_live_calls']}")
        print(f"  blocked side effects: {report['blocked_side_effects'] or 'none'}")
        print(f"  model stub calls during replay: {replay_model.calls}")
        print(f"  replayed final output: {replayed}")

        # -- 3. Verify --------------------------------------------------
        assert replayed == answer, "replay output differs from recording"
        assert replay_model.calls == 0, \
            "exact replay must not call the model stub"
        assert report["live_calls"] == 0, "exact replay must have no live calls"
        assert report["fallback_live_calls"] == 0, \
            "agent path diverged from the recording"
        assert "send_email" in report["blocked_side_effects"], \
            "side-effecting email should be blocked on replay"
        print("\nOK: exact replay reproduced the recorded run "
              "(same final output, 0 model calls, email blocked).")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
