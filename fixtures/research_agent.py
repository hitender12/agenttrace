"""Fixture scenario: a multi-tool research agent.

The agent researches several topics in one run, exercising more of the
event model than the single-topic support demo: two retrieval queries,
repeated ``search_kb`` tool calls, a browser action per topic, a
synthesis model call, memory writes, and a final output.

Record it with the engine (run from the repo root)::

    python fixtures/research_agent.py

Replay it with the engine (see ``tests/test_fixtures.py``)::

    from agenttrace import ReplayRuntime
    with ReplayRuntime(store, run_id, mode="exact") as rt:
        answer = research_agent(rt, TOPICS)

Exact replay is deterministic: recorded retrieval results, tool
results, and the model response are returned again, so the agent
produces the same summary without live model or tool calls.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agenttrace import RecordRuntime, ScriptedModel, Store  # noqa: E402

TOPICS = ["refund policy", "shipping times"]


def research_agent(rt, topics: list[str] | None = None) -> str:
    """Research each topic, then synthesize one summary."""
    topics = list(topics or TOPICS)
    rt.policy_decision(
        "research_scope", {"topics": topics},
        {"allowed": True, "reason": "public docs only"})
    findings: list[str] = []
    for topic in topics:
        docs = rt.retrieve(topic)
        result = rt.tool("search_kb", {"query": topic})
        for doc in result["results"]:
            findings.append(f"{topic}: {doc['title']}")
        rt.browser_action(
            "open", f"https://docs.example.com/{topic.replace(' ', '-')}")
        rt.memory_write(f"research:{topic}",
                        [d["id"] for d in docs])
    prompt = (
        "You are a research agent. Synthesize these findings into a "
        "short summary:\n" + "\n".join(f"- {f}" for f in findings)
    )
    answer = rt.model(prompt)
    rt.memory_write("research_findings", findings)
    rt.final_output(answer)
    return answer


def main() -> None:
    store = Store(".agenttrace")
    try:
        with RecordRuntime(store, "fixture-research-agent",
                           model=ScriptedModel(),
                           metadata={"topics": TOPICS,
                                     "fixture": "research_agent"}) as rt:
            answer = research_agent(rt, TOPICS)
            print(f"recorded run {rt.run_id}")
            print(f"final output: {answer}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
