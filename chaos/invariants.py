"""Invariant checks for one chaos run, against the payments database and a reference run.

1. exactly one payment per intended (run_id, ticket): no duplicates, none missing, right amount;
2. final output identical to an uninterrupted reference run (deterministic model only);
3. every record acknowledged before a crash survives recovery (checked by the harness at
   each kill with `acked_records_survive`);
4. no NonDeterminismError while the code is unchanged.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from agentwal.journal import read_records


def payments_for_run(db_path: str, run_id: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, idempotency_key, ticket_id, amount_cents FROM payments WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
    finally:
        conn.close()
    return [{"payment_id": f"pay_{r[0]}", "key": r[1], "ticket_id": r[2], "amount_cents": r[3]} for r in rows]


def comparable(output: dict[str, Any]) -> list[dict[str, Any]]:
    """The part of the output that must match the reference: decisions without ids and timestamps."""
    return [{k: d[k] for k in ("ticket_id", "refund", "amount_cents", "reason")} for d in output["decisions"]]


def check_effects(decisions: list[dict[str, Any]], payments: list[dict[str, Any]], output_has_ids: bool) -> dict[str, int]:
    """Compare intended refunds with the payments that actually exist."""
    intended = {d["ticket_id"]: d for d in decisions if d["refund"] and d["amount_cents"] > 0}
    by_ticket = Counter(p["ticket_id"] for p in payments)
    duplicates = sum(n - 1 for n in by_ticket.values() if n > 1) + sum(
        n for t, n in by_ticket.items() if t not in intended
    )
    lost = sum(1 for t in intended if by_ticket[t] == 0)
    wrong = 0
    for t, d in intended.items():
        rows = [p for p in payments if p["ticket_id"] == t]
        if rows and rows[0]["amount_cents"] != d["amount_cents"]:
            wrong += 1
        elif rows and output_has_ids and d.get("payment_id") not in {p["payment_id"] for p in rows}:
            wrong += 1  # the output points at a payment that is not the one in the ledger
    return {"duplicates": duplicates, "lost": lost, "wrong": wrong}


def acked_records_survive(journal: Path, ack_log: Path) -> tuple[bool, int, int]:
    """Invariant 3: the journal holds at least as many valid records as were last acknowledged."""
    acked = 0
    if ack_log.exists():
        lines = ack_log.read_text().split("\n")
        complete = [ln for ln in lines[:-1] if ln.strip()]  # last element is "" or a torn line
        if complete:
            acked = int(complete[-1].split()[0])
    recovered = len(read_records(journal).records)
    return recovered >= acked, acked, recovered


def events(run_dir: Path) -> list[dict[str, Any]]:
    p = run_dir / "events.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass  # a line torn by kill -9
    return out


def check_run(
    run_dir: Path, run_id: str, db_path: str, reference: dict[str, Any] | None, deterministic: bool
) -> dict[str, Any]:
    out_path = run_dir / "output.json"
    output = json.loads(out_path.read_text()) if out_path.exists() else None
    payments = payments_for_run(db_path, run_id)
    if output is not None:
        decisions, has_ids = output["decisions"], True
    else:  # the run never finished: judge against what it should have done
        decisions, has_ids = (reference["decisions"] if reference else []), False
    eff = check_effects(decisions, payments, has_ids)
    if output is None and not deterministic:
        # The intended decisions of an unfinished sampled run are unknown: only an extra
        # payment for the same ticket is provably wrong.
        by_ticket = Counter(p["ticket_id"] for p in payments)
        eff = {"duplicates": sum(n - 1 for n in by_ticket.values() if n > 1), "lost": 0, "wrong": 0}
    evs = events(run_dir)
    batch_ids = {e["batch_id"] for e in evs if e["event"] == "batch"}
    res = {
        "completed": output is not None,
        **eff,
        "payments": len(payments),
        "output_matches_reference": (
            None if not deterministic or reference is None or output is None
            else comparable(output) == comparable(reference)
        ),
        "batch_id_stable": len(batch_ids) <= 1,
        "nondeterminism": sum(e["event"] == "nondeterminism" for e in evs),
        "errors": [e["error"] for e in evs if e["event"] == "error"],
    }
    return res
