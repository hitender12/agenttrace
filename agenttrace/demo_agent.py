"""Demo customer-support agent (scripted, deterministic, free).

The agent is deliberately small: policy check -> retrieval -> model ->
order lookup -> optional side-effecting email -> memory write -> output.
It exists to exercise the recorder and the replay engine, not to answer
real support tickets.
"""
from __future__ import annotations

import re

from .runtime import extract_order_id

# Fake key used only to prove redaction works: it is recorded in the model
# request headers and must never appear in the stored run or any export.
FAKE_API_KEY = "sk-test-4eC39HqLyjWDarjtT1zdp7dc"


def customer_support_agent(rt, query: str) -> str:
    rt.policy_decision(
        "pii_check", {"query": query},
        {"allowed": True, "reason": "no PII patterns detected"})
    docs = rt.retrieve(query)
    context = "\n".join(f"- {d['title']}: {d['text']}" for d in docs)
    prompt = (
        "You are a support agent. Use these docs:\n"
        f"{context}\n\nCustomer query: {query}\n\nAnswer concisely."
    )
    answer = rt.model(prompt)
    order_id = extract_order_id(query)
    rt.tool("get_order", {"order_id": order_id})
    if "refund" in query.lower():
        rt.tool("send_email", {
            "to": "customer@example.com",
            "subject": f"Your refund for {order_id}",
            "body": answer,
        }, side_effect=True)
    rt.memory_write("last_topic", query)
    rt.final_output(answer)
    return answer
