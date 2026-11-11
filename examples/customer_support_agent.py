"""Example: instrument your own agent with the AgentTrace SDK.

Run it directly::

    python examples/customer_support_agent.py

It records one run of the bundled demo agent to ``.agenttrace/`` and
prints the run id. Then inspect it::

    python -m agenttrace timeline <run_id>
    python -m agenttrace replay <run_id>
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agenttrace import (  # noqa: E402
    FAKE_API_KEY, RecordRuntime, ScriptedModel, Store, customer_support_agent,
)

QUERY = "I want a refund for order ORD-1001"


def main() -> None:
    # `agenttrace record -- python examples/customer_support_agent.py`
    # sets AGENTTRACE_DB so the run lands in the --db store the user
    # asked for; running the script directly defaults to .agenttrace/.
    store = Store(os.environ.get("AGENTTRACE_DB", ".agenttrace"))
    try:
        model = ScriptedModel(api_key=FAKE_API_KEY)
        with RecordRuntime(store, "example-support-run", model=model,
                           api_key=FAKE_API_KEY,
                           metadata={"query": QUERY}) as rt:
            answer = customer_support_agent(rt, QUERY)
            print(f"recorded run {rt.run_id}")
            print(f"final output: {answer}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
