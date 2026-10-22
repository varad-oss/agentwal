"""A free, deterministic stand-in for an LLM that decides refunds.

It reads the ticket JSON embedded in the prompt and applies fixed rules, sleeping a few
milliseconds to look like a network call. Every call is appended to a billing log, the
way a provider would bill it, so the chaos harness can count calls paid twice.

``stochastic=True`` adds sampling noise to the refund amount (like a real model at
temperature > 0). It exists to show what happens when a non-deterministic decision is
lost and re-sampled after a crash.
"""

from __future__ import annotations

import json
import os
import random
import time
from typing import Any

RULES = [  # (keywords, fraction of the order refunded, reason)
    (("damaged", "cracked", "torn"), 1.0, "item arrived damaged"),
    (("never arrived", "stolen"), 1.0, "item not delivered"),
    (("double charge", "double charged", "charged twice"), 1.0, "duplicate charge"),
    (("late",), 0.2, "late delivery goodwill credit"),
]


def extract_ticket(request: dict[str, Any]) -> dict[str, Any]:
    text = request["messages"][-1]["content"]
    start = text.index("TICKET_JSON:") + len("TICKET_JSON:")
    return json.loads(text[start:].strip().splitlines()[0])


def decide(ticket: dict[str, Any]) -> dict[str, Any]:
    text = ticket["text"].lower()
    for words, frac, reason in RULES:
        if any(w in text for w in words):
            return {"refund": True, "amount_cents": int(ticket["order_cents"] * frac), "reason": reason}
    return {"refund": False, "amount_cents": 0, "reason": "no refund policy applies"}


class FakeModel:
    name = "fake"

    def __init__(self, latency_ms: float | None = None, billing_log: str | None = None, stochastic: bool = False):
        self.latency_ms = float(os.environ.get("AGENTWAL_FAKE_LATENCY_MS", 15)) if latency_ms is None else latency_ms
        self.billing_log = billing_log
        self.stochastic = stochastic
        self.calls = 0

    def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.latency_ms:
            time.sleep(self.latency_ms / 1000)
        ticket = extract_ticket(request)
        d = decide(ticket)
        if self.stochastic and d["refund"]:
            d["amount_cents"] = int(d["amount_cents"] * random.SystemRandom().uniform(0.5, 1.0))
        text = json.dumps(d)
        usage = {"input_tokens": len(json.dumps(request)) // 4, "output_tokens": len(text) // 4}
        self.calls += 1
        if self.billing_log:
            fd = os.open(self.billing_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, (json.dumps({"ticket_id": ticket["ticket_id"], "cost_usd": 0.0, **usage}) + "\n").encode())
            finally:
                os.close(fd)
        return {"text": text, "usage": usage, "model": self.name}
