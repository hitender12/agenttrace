"""Fixture scenario: a support agent that fails on a retrieval miss.

The customer asks about a warranty topic that is not in the knowledge
base. Retrieval returns only the generic FAQ docs, none of which mention
the topic, so the agent records a ``retrieval_miss`` error and answers
honestly that it could not find the information instead of inventing
an answer.

This is a *task* failure, not a crash: the run completes, but it
contains an error event, so ``task_success`` is False for any eval
case built on it.

Record it with the engine (run from the repo root)::

    python fixtures/support_retrieval_miss.py

Replay it with the engine (see ``tests/test_fixtures.py``)::

    from agenttrace import ReplayRuntime
    with ReplayRuntime(store, run_id, mode="exact") as rt:
        answer = support_retrieval_miss_agent(rt, MISS_QUERY)

Exact replay is deterministic: the recorded retrieval result is
returned again, so the agent takes the same miss path and produces
the same answer without a live model call.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agenttrace import RecordRuntime, ScriptedModel, Store  # noqa: E402

MISS_QUERY = "What is the warranty period for the Widget Pro?"

# Words too short to be meaningful are ignored when judging relevance.
_MIN_WORD_LEN = 4


def _relevant_docs(query: str, docs: list[dict]) -> list[dict]:
    words = [w.strip("?.!,") for w in query.lower().split()]
    keywords = [w for w in words if len(w) >= _MIN_WORD_LEN]
    relevant = []
    for doc in docs:
        haystack = f"{doc.get('title', '')} {doc.get('text', '')}".lower()
        if any(word in haystack for word in keywords):
            relevant.append(doc)
    return relevant


def support_retrieval_miss_agent(rt, query: str = MISS_QUERY) -> str:
    """Run the support flow; fail gracefully when retrieval misses."""
    rt.policy_decision(
        "pii_check", {"query": query},
        {"allowed": True, "reason": "no PII patterns detected"})
    docs = rt.retrieve(query)
    relevant = _relevant_docs(query, docs)
    if not relevant:
        rt.error("retrieval_miss",
                 f"no relevant documents found for query: {query}")
        answer = (
            "I couldn't find information about that in our knowledge base. "
            "I've flagged this for a human support agent, who will follow up "
            "with the warranty details for the Widget Pro."
        )
        rt.final_output(answer)
        return answer
    context = "\n".join(f"- {d['title']}: {d['text']}" for d in relevant)
    answer = rt.model(
        "You are a support agent. Use these docs:\n"
        f"{context}\n\nCustomer query: {query}\n\nAnswer concisely.")
    rt.final_output(answer)
    return answer


def main() -> None:
    store = Store(".agenttrace")
    try:
        with RecordRuntime(store, "fixture-support-retrieval-miss",
                           model=ScriptedModel(),
                           metadata={"query": MISS_QUERY,
                                     "fixture": "support_retrieval_miss"}) as rt:
            answer = support_retrieval_miss_agent(rt, MISS_QUERY)
            print(f"recorded run {rt.run_id}")
            print(f"final output: {answer}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
