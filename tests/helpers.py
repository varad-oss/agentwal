"""Shared test doubles: an in-memory idempotent service, a counting fake model, crash hooks."""

from __future__ import annotations

from typing import Any


class SimulatedCrash(BaseException):
    """Raised by a fault hook to stop a run in-process (BaseException so agent code can't swallow it)."""


def crash_at(point: str, nth: int = 1):
    hits = {"n": 0}

    def hook(p: str) -> None:
        if p == point:
            hits["n"] += 1
            if hits["n"] == nth:
                raise SimulatedCrash(p)

    return hook


class Ledger:
    """In-memory payments receiver. Honours idempotency keys unless `idempotent=False`."""

    def __init__(self, idempotent: bool = True):
        self.idempotent = idempotent
        self.rows: list[dict[str, Any]] = []
        self.calls = 0

    def pay(self, idempotency_key: str | None = None, **args: Any) -> dict[str, Any]:
        self.calls += 1
        if self.idempotent and idempotency_key is not None:
            for r in self.rows:
                if r["key"] == idempotency_key:
                    return {"payment_id": r["id"], "replayed": True}
        row = {"id": f"p{len(self.rows) + 1}", "key": idempotency_key, **args}
        self.rows.append(row)
        return {"payment_id": row["id"], "replayed": False}

    def reconcile(self, key: str) -> dict[str, Any] | None:
        for r in self.rows:
            if r["key"] == key:
                return {"payment_id": r["id"], "replayed": True}
        return None


class CountingModel:
    """Deterministic model: echoes a function of the prompt and counts (billed) calls."""

    def __init__(self):
        self.calls = 0

    def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        prompt = request["prompt"]
        return {"text": f"answer:{prompt}", "amount": (sum(map(ord, prompt)) % 50) + 1}
