"""Example: diff two agent runs and print the differences.

Run it directly (from the repository root)::

    python examples/diff-runs/diff_two_runs.py

What it does, in a fresh temporary store (no leftover state):

1. Records run A: the demo support agent answering a REFUND query.
2. Records run B: the same agent answering a SHIPPING query.
3. Prints ``agenttrace diff`` output for A -> B: changed prompts,
   tool sequence and arguments, retrieved documents, model metadata
   (tokens/cost), and final output.
4. Records run C: the same refund query as A, and diffs A -> C to
   show the "no changes" case. (The random ``send_email`` message id
   differs between A and C, but diff compares tool *arguments*, not
   tool results, so identical behaviour still reports no changes.)

Everything is deterministic: the model is the project's
``ScriptedModel`` stub (no network, no real model calls, no cost).
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agenttrace import (  # noqa: E402
    FAKE_API_KEY, RecordRuntime, ScriptedModel, Store,
    customer_support_agent, diff_runs, render_diff,
)

REFUND_QUERY = "I want a refund for order ORD-1001"
SHIPPING_QUERY = "Where is my delivery for order ORD-2002?"


def record(store: Store, name: str, query: str) -> tuple[str, str]:
    model = ScriptedModel(api_key=FAKE_API_KEY)
    with RecordRuntime(store, name, model=model, api_key=FAKE_API_KEY,
                       metadata={"query": query}) as rt:
        answer = customer_support_agent(rt, query)
        run_id = rt.run_id
    return run_id, answer


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="agenttrace-diff-example-") as tmp:
        store = Store(Path(tmp) / "at")
        try:
            run_a, answer_a = record(store, "diff-run-a-refund", REFUND_QUERY)
            run_b, answer_b = record(store, "diff-run-b-shipping", SHIPPING_QUERY)
            print(f"run A (refund):  {run_a} -> {answer_a[:60]}...")
            print(f"run B (shipping): {run_b} -> {answer_b[:60]}...")

            diff = diff_runs(store, run_a, run_b)
            print(f"\n{render_diff(diff)}")
            assert diff["changed"], "expected the refund/shipping runs to differ"
            kinds = sorted({c["kind"] for c in diff["changes"]})
            print(f"\nchange kinds found: {kinds}")
            for expected in ("prompt_changed", "tool_sequence_changed",
                             "tool_args_changed", "retrieval_changed",
                             "model_metadata_changed", "output_changed"):
                assert expected in kinds, f"missing change kind {expected}"

            # Same query recorded twice: behaviour is identical, so
            # the diff reports no changes.
            run_c, _ = record(store, "diff-run-c-refund-again", REFUND_QUERY)
            same = diff_runs(store, run_a, run_c)
            print(f"\n{render_diff(same)}")
            assert not same["changed"], \
                "two recordings of the same query should not differ"
            print("\nOK: diff detected every change class for A -> B "
                  "and reported no changes for A -> C.")
            return 0
        finally:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
