"""Overhead benchmark: what journaling costs per record and per agent step, per fsync policy.

1. Raw append latency: 2,000 records of a typical LLM_RESULT size under each policy.
2. Per-step overhead: the refund agent in-process with a zero-latency fake model and an
   in-memory payments tool, without a journal and with AgentWAL under each policy.
   One step = one ticket (an LLM call plus, for 7 of 10 tickets, a refund).
3. Journal bytes per step.

    python -m chaos.bench  ->  results/bench.json
"""

from __future__ import annotations

import json
import platform
import statistics
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

from agentwal import ToolSpec, durable_run
from agentwal.journal import Journal
from agentwal.records import RecordType
from chaos.harness import RESULTS
from demo.agent import RefundAgent, make_output
from demo.fake_model import FakeModel
from demo.tickets import TICKETS

POLICIES = ["always", "group:8:5", "none"] + (["always+full"] if sys.platform == "darwin" else [])


def bench_append(policy: str, n: int = 2000) -> dict:
    payload = {"seq": 0, "response": {"text": "x" * 160, "usage": {"input_tokens": 120, "output_tokens": 30}}}
    lat = []
    with tempfile.TemporaryDirectory() as d, Journal(Path(d) / "b.wal", policy) as j:
        for i in range(n):
            payload["seq"] = i
            t0 = time.perf_counter()
            j.append(RecordType.LLM_RESULT, payload)
            lat.append((time.perf_counter() - t0) * 1e6)
        fsyncs = j.fsync_calls
    lat.sort()
    return {"policy": policy, "records": n, "mean_us": statistics.mean(lat), "p50_us": lat[n // 2],
            "p99_us": lat[int(n * 0.99)], "fsyncs": fsyncs}


class MemPayments:
    def __init__(self):
        self.rows = {}

    def issue_refund(self, run_id, ticket_id, amount_cents, idempotency_key=None):
        key = idempotency_key or f"k{len(self.rows)}"
        self.rows.setdefault(key, {"payment_id": f"pay_{len(self.rows)}", "amount_cents": amount_cents})
        return self.rows[key]


def one_run(policy: str | None, d: Path, k: int) -> tuple[float, int]:
    model, pay = FakeModel(latency_ms=0), MemPayments()
    t0 = time.perf_counter()
    if policy is None:
        agent = RefundAgent(model, {"issue_refund": pay.issue_refund}, f"r{k}")
        make_output("b", 0.0, agent.run(TICKETS))
        return time.perf_counter() - t0, 0
    path = d / f"{policy}-{k}.wal"
    with durable_run(str(path), f"r{k}", input={"tickets": TICKETS}, fsync=policy) as ctx:
        agent = RefundAgent(ctx.llm(model), ctx.wrap({"issue_refund": ToolSpec(pay.issue_refund)}), f"r{k}")
        ctx.complete(make_output(ctx.uuid(), ctx.now(), agent.run(TICKETS)))
    return time.perf_counter() - t0, path.stat().st_size


def bench_steps(reps: int = 60) -> list[dict]:
    rows = []
    with tempfile.TemporaryDirectory() as d:
        for policy in [None, *POLICIES]:
            one_run(policy, Path(d), -1)  # warm up
            times, sizes = [], []
            for k in range(reps):
                t, size = one_run(policy, Path(d), k)
                times.append(t / len(TICKETS) * 1e6)
                sizes.append(size)
            times.sort()
            rows.append({"policy": policy or "no journal", "runs": reps, "step_us_p50": times[reps // 2],
                         "step_us_mean": statistics.mean(times), "step_us_p99": times[int(reps * 0.99)],
                         "journal_bytes_per_step": statistics.mean(sizes) / len(TICKETS)})
    base = rows[0]["step_us_p50"]
    for r in rows:
        r["overhead_us_p50"] = r["step_us_p50"] - base
    return rows


def main() -> None:
    RESULTS.mkdir(exist_ok=True)
    out = {
        "date": date.today().isoformat(),
        "machine": f"{platform.system()} {platform.machine()}, Python {platform.python_version()}",
        "note": "Durations depend on the disk. On macOS plain fsync does not flush the drive cache; '+full' uses F_FULLFSYNC.",
        "append": [bench_append(p) for p in POLICIES],
        "steps": bench_steps(),
    }
    (RESULTS / "bench.json").write_text(json.dumps(out, indent=2))
    for r in out["append"]:
        print(f"append {r['policy']:<12} p50 {r['p50_us']:8.1f} us  p99 {r['p99_us']:8.1f} us  fsyncs {r['fsyncs']}")
    for r in out["steps"]:
        print(f"step   {r['policy']:<12} p50 {r['step_us_p50']:8.1f} us  overhead {r['overhead_us_p50']:8.1f} us  "
              f"{r['journal_bytes_per_step']:.0f} B/step")


if __name__ == "__main__":
    main()
