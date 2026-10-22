"""Anthropic-backed model adapter for the real-model chaos runs (needs ANTHROPIC_API_KEY in .env)."""

from __future__ import annotations

import json
import os
from typing import Any

# $ per million tokens (input, output), from the Anthropic pricing table, Oct 2026.
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00), "claude-opus-5-5": (4.00, 20.00)}
DEFAULT_MODEL = "claude-haiku-5-5"


class AnthropicModel:
    def __init__(self, model: str | None = None, billing_log: str | None = None):
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("AGENTWAL_MODEL", DEFAULT_MODEL)
        self.name = self.model
        self.billing_log = billing_log

    def cost(self, usage: dict[str, int]) -> float:
        pin, pout = PRICES.get(self.model, (0.0, 0.0))
        return usage["input_tokens"] * pin / 1e6 + usage["output_tokens"] * pout / 1e6

    def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=request.get("max_tokens", 1024),
            system=request["system"],
            messages=request["messages"],
            output_config={"effort": "low"},
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
        cost = self.cost(usage)
        if self.billing_log:
            with open(self.billing_log, "a") as f:
                f.write(json.dumps({"cost_usd": cost, **usage}) + "\n")
        return {"text": text, "usage": usage, "model": self.model, "cost_usd": cost}
