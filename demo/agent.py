"""Refund batch agent, runnable under three crash-handling strategies.

    aw  AgentWAL: journaled LLM calls, two-phase effects with idempotency keys.
    b1  Checkpoint-after-step: state saved (atomically, fsynced) after each ticket.
    b0  Naive: no journal; after a crash the whole batch starts over.

Usage (one attempt; rerun the same command after a crash to resume):

    python -m demo.agent --strategy aw --run-id r-42 --run-dir runs/r-42 --service http://127.0.0.1:8765

Files in --run-dir: journal.wal (+ journal.ack), checkpoint.json (b1), output.json on
completion, events.jsonl (one line per attempt milestone), billing.jsonl (model calls).
Exit codes: 0 done, 3 NonDeterminismError, 4 any other error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agentwal import NonDeterminismError, ToolSpec, durable_run
from chaos.killpoints import from_env
from demo.tickets import TICKETS

CODE_VERSION = "refund-agent-v1"
SYSTEM = (
    "You are a support refund agent. Decide whether the ticket qualifies for a refund under policy: "
    "damaged items, non-delivery and duplicate charges get a full refund; late delivery gets a 20% goodwill "
    "credit; anything else gets no refund. Reply with only a JSON object: "
    '{"refund": true|false, "amount_cents": <int>, "reason": "<short reason>"}.'
)


# -- payments client --------------------------------------------------------------------
class PaymentsClient:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def _req(self, method: str, path: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def issue_refund(self, run_id: str, ticket_id: str, amount_cents: int, idempotency_key: str | None = None) -> dict:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        code, body = self._req("POST", "/payments", {"run_id": run_id, "ticket_id": ticket_id,
                                                     "amount_cents": amount_cents}, headers)
        if code >= 400:
            raise RuntimeError(f"payments service returned {code}: {body}")
        return {"payment_id": body["payment_id"], "amount_cents": body["amount_cents"]}

    def reconcile(self, key: str) -> dict | None:
        code, body = self._req("GET", f"/payments?key={key}")
        return {"payment_id": body["payment_id"], "amount_cents": body["amount_cents"]} if code == 200 else None


# -- the agent ----------------------------------------------------------------------------
def build_request(ticket: dict) -> dict:
    return {
        "system": SYSTEM,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": f"TICKET_JSON: {json.dumps(ticket, sort_keys=True)}\n"}],
    }


def parse_decision(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    d = json.loads(text[start : end + 1])
    return {"refund": bool(d["refund"]), "amount_cents": int(d.get("amount_cents") or 0), "reason": str(d.get("reason", ""))}


class RefundAgent:
    """Works through tickets: the model decides, then the agent calls issue_refund."""

    def __init__(self, llm, tools, run_id: str):
        self.llm, self.tools, self.run_id = llm, tools, run_id

    def step(self, ticket: dict) -> dict:
        decision = parse_decision(self.llm.complete(build_request(ticket))["text"])
        out = {"ticket_id": ticket["ticket_id"], **decision, "payment_id": None}
        if decision["refund"] and decision["amount_cents"] > 0:
            pay = self.tools["issue_refund"](run_id=self.run_id, ticket_id=ticket["ticket_id"],
                                             amount_cents=decision["amount_cents"])
            out["payment_id"] = pay["payment_id"]
        return out

    def run(self, tickets: list[dict]) -> list[dict]:
        return [self.step(t) for t in tickets]


def make_output(batch_id: str, started_at: float, decisions: list[dict]) -> dict:
    return {"batch_id": batch_id, "started_at": started_at, "decisions": decisions,
            "total_refunded_cents": sum(d["amount_cents"] for d in decisions if d["payment_id"])}


# -- plumbing shared by the strategies --------------------------------------------------------
class Events:
    def __init__(self, path: Path, strategy: str):
        self.fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        self.strategy = strategy
        self.pid = os.getpid()

    def emit(self, kind: str, **data: Any) -> None:
        os.write(self.fd, (json.dumps({"event": kind, "pid": self.pid, "strategy": self.strategy, **data}) + "\n").encode())


class HookedLLM:
    """Baseline model wrapper that fires the same `llm_mid` kill point as AgentWAL."""

    def __init__(self, model, hook):
        self.model, self.hook = model, hook

    def complete(self, request):
        r = self.model.complete(request)
        if self.hook:
            self.hook("llm_mid")
        return r


def hooked_tool(fn: Callable, hook) -> Callable:
    """Baseline tool wrapper firing the same effect kill points as AgentWAL (no intents, no keys)."""

    def call(**args):
        if hook:
            hook("before_intent_fsync")
            hook("after_intent_before_effect")
        r = fn(**args)
        if hook:
            hook("after_effect_before_result")
        return r

    return call


def write_output(run_dir: Path, output: dict) -> None:
    tmp = run_dir / "output.json.tmp"
    tmp.write_text(json.dumps(output, indent=1))
    os.replace(tmp, run_dir / "output.json")


# -- strategies ----------------------------------------------------------------------------
def run_aw(args, model, payments, hook, events) -> dict:
    run_dir = Path(args.run_dir)
    tools_spec = {"issue_refund": ToolSpec(payments.issue_refund, reconcile=payments.reconcile)}

    def caught_up(ctx):
        events.emit("recovered", recovery_ms=ctx.stats.recovery_ms, truncated_bytes=ctx.stats.truncated_bytes,
                    records_recovered=ctx.stats.records_recovered)

    with durable_run(
        str(run_dir / "journal.wal"), args.run_id, input={"tickets": TICKETS}, code_version=CODE_VERSION,
        fsync=args.fsync, power_loss=args.power_loss, ack_log=str(run_dir / "journal.ack"),
        recovery=args.recovery, fault_hook=hook, on_caught_up=caught_up,
    ) as ctx:
        if ctx.done:
            ctx.mark_caught_up()
            return ctx.output
        batch_id, started_at = ctx.uuid(), ctx.now()
        events.emit("batch", batch_id=batch_id)
        agent = RefundAgent(ctx.llm(model), ctx.wrap(tools_spec), args.run_id)
        output = ctx.complete(make_output(batch_id, started_at, agent.run(TICKETS)))
        events.emit("stats", **ctx.stats.as_dict())
        return output


def run_b1(args, model, payments, hook, events) -> dict:
    run_dir = Path(args.run_dir)
    ckpt = run_dir / "checkpoint.json"
    t0 = time.perf_counter()
    state = json.loads(ckpt.read_text()) if ckpt.exists() else None
    if state is None:
        state = {"next": 0, "results": [], "batch_id": str(uuid.uuid4()), "started_at": time.time()}
    events.emit("recovered", recovery_ms=(time.perf_counter() - t0) * 1000)
    events.emit("batch", batch_id=state["batch_id"])
    agent = RefundAgent(HookedLLM(model, hook), {"issue_refund": hooked_tool(payments.issue_refund, hook)}, args.run_id)
    for i in range(state["next"], len(TICKETS)):
        state["results"].append(agent.step(TICKETS[i]))
        state["next"] = i + 1
        tmp = run_dir / "checkpoint.json.tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ckpt)  # atomic checkpoint after the step
    return make_output(state["batch_id"], state["started_at"], state["results"])


def run_b0(args, model, payments, hook, events) -> dict:
    events.emit("recovered", recovery_ms=0.0)
    batch_id, started_at = str(uuid.uuid4()), time.time()
    events.emit("batch", batch_id=batch_id)
    agent = RefundAgent(HookedLLM(model, hook), {"issue_refund": hooked_tool(payments.issue_refund, hook)}, args.run_id)
    return make_output(batch_id, started_at, agent.run(TICKETS))


STRATEGIES = {"aw": run_aw, "b1": run_b1, "b0": run_b0}


def make_model(args, billing_log: str):
    if args.model == "fake":
        from demo.fake_model import FakeModel

        return FakeModel(billing_log=billing_log, stochastic=args.stochastic)
    from demo.local_model import OllamaModel

    return OllamaModel(billing_log=billing_log)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", choices=STRATEGIES, default="aw")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--service", required=True, help="payments service base URL")
    ap.add_argument("--fsync", default="always")
    ap.add_argument("--power-loss", action="store_true", help="simulate power loss: un-fsynced records die with the process")
    ap.add_argument("--recovery", choices=["retry", "reconcile"], default="retry")
    ap.add_argument("--model", choices=["fake", "ollama"], default="fake", help="ollama = free local LLM")
    ap.add_argument("--stochastic", action="store_true", help="fake model samples refund amounts")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    events = Events(run_dir / "events.jsonl", args.strategy)
    events.emit("start")
    model = make_model(args, str(run_dir / "billing.jsonl"))
    payments = PaymentsClient(args.service)
    try:
        output = STRATEGIES[args.strategy](args, model, payments, from_env(), events)
    except NonDeterminismError as e:
        events.emit("nondeterminism", error=str(e))
        print(f"NonDeterminismError: {e}", file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001
        events.emit("error", error=f"{type(e).__name__}: {e}")
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 4
    write_output(run_dir, output)
    events.emit("done")
    if not args.quiet:
        refunds = [d for d in output["decisions"] if d["payment_id"]]
        print(f"done: {len(refunds)} refunds, {output['total_refunded_cents']} cents, batch {output['batch_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
