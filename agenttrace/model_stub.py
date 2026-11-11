"""Deterministic scripted model stub.

No real model calls, no network, no cost. The stub answers from canned
templates chosen by keywords in the prompt, with deterministic token counts
and cost, so recorded runs and exact replays are reproducible.
"""
from __future__ import annotations


class ScriptedModel:
    MODEL_NAME = "scripted-1"
    PRICE_PER_1K_TOKENS_USD = 0.002

    def __init__(self, api_key: str | None = None):
        self.calls = 0
        self.api_key = api_key

    def complete(self, prompt: str, model: str | None = None) -> dict:
        self.calls += 1
        low = prompt.lower()
        if "refund" in low:
            text = (
                "Your refund of $49.00 for order ORD-1001 has been approved. "
                "It will reach your original payment method within 5 business days."
            )
        elif "shipping" in low or "delivery" in low:
            text = (
                "Your order ORD-1001 was delivered on 2026-11-08 via standard shipping. "
                "Tracking shows it was left at the front desk."
            )
        elif "cancel" in low:
            text = (
                "Your order ORD-1001 can still be cancelled because it has not shipped yet. "
                "I have started the cancellation; you will get a confirmation email."
            )
        else:
            text = (
                "Thanks for reaching out. I checked your order ORD-1001 and it is in good "
                "standing. Let me know if you need a refund, shipping update, or cancellation."
            )
        tokens_in = max(1, len(prompt.split()))
        tokens_out = max(1, len(text.split()))
        cost = (tokens_in + tokens_out) / 1000 * self.PRICE_PER_1K_TOKENS_USD
        return {
            "text": text,
            "model": model or self.MODEL_NAME,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": round(cost, 6),
        }
