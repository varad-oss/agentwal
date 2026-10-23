"""Chaos harness: run the refund agent many times, SIGKILL it, restart it, check invariants.

Each run gets at least one kill on its first attempt: either at a random moment (a
timer in the harness sends SIGKILL) or at a targeted kill point (the agent kills itself
via AGENTWAL_KILL_AT). Every later attempt is killed again with probability
`--rekill`. The harness restarts the agent until it completes, capped at 10 restarts.

    python -m chaos.harness --config aw --runs 1000
    python -m chaos.harness --config all            # every suite in CONFIGS
    python -m chaos.harness --config smoke          # 50 AgentWAL runs (CI)

Per-run rows go to results/chaos_<config>.jsonl, the aggregate to
results/chaos_<config>_summary.json.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from agentwal import KILL_POINTS
from agentwal.journal import read_records
from chaos.invariants import acked_records_survive, check_run

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
RUNS = ROOT / "runs"
MAX_RESTARTS = 10
# How often each kill point is reached in one uninterrupted run (10 tickets, 7 refunds).
MAX_HITS = {"llm_mid": 10, "before_intent_fsync": 7, "after_intent_before_effect": 7, "after_effect_before_result": 7}


@dataclass
class Config:
    name: str
    strategy: str = "aw"
    runs: int = 1000
    fsync: str = "always"
    power_loss: bool = False
    recovery: str = "retry"
    idempotent_service: bool = True
    stochastic: bool = False
    torn: bool = False  # after each kill, also truncate the journal at a random byte offset
    model: str = "fake"
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def deterministic(self) -> bool:
        return self.model == "fake" and not self.stochastic


CONFIGS = {c.name: c for c in [
    Config("aw", description="AgentWAL, process crash (kill -9), fsync=always, idempotent receiver"),
    Config("b1", strategy="b1", description="Checkpoint after each step, process crash"),
    Config("b0", strategy="b0", description="Naive restart from scratch, process crash"),
    Config("aw-torn", runs=300, torn=True, description="AgentWAL, and the journal is torn at a random byte after every kill"),
    Config("aw-pl-always", runs=300, power_loss=True, fsync="always", description="AgentWAL, simulated power loss, fsync=always"),
    Config("aw-pl-group", runs=300, power_loss=True, fsync="group:8:5", description="AgentWAL, simulated power loss, group commit (8 records / 5 ms)"),
    Config("aw-pl-none", runs=300, power_loss=True, fsync="none", description="AgentWAL, simulated power loss, no fsync"),
    Config("aw-pl-always-stoch", runs=200, power_loss=True, fsync="always", stochastic=True,
           description="Power loss, fsync=always, model samples refund amounts"),
    Config("aw-pl-none-stoch", runs=200, power_loss=True, fsync="none", stochastic=True,
           description="Power loss, no fsync, model samples refund amounts"),
    Config("aw-noidem", runs=300, idempotent_service=False, description="AgentWAL retry-with-key, receiver ignores idempotency keys"),
    Config("aw-reconcile-noidem", runs=300, idempotent_service=False, recovery="reconcile",
           description="AgentWAL reconcile-by-key, receiver ignores idempotency keys"),
    Config("smoke", runs=50, description="CI smoke test: AgentWAL, process crash"),
    Config("real", runs=30, model="ollama", description="AgentWAL with a real LLM (qwen3:8b, local via Ollama, free), process crash"),
]}
DEFAULT_SUITE = [n for n in CONFIGS if n not in ("smoke", "real")]


# -- payments service ----------------------------------------------------------------------
def start_service(db: Path, idempotent: bool = True) -> tuple[subprocess.Popen, str]:
    cmd = [sys.executable, "-m", "demo.payments_service", "--db", str(db), "--port", "0"]
    if not idempotent:
        cmd.append("--no-idempotency")
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, text=True)
    line = proc.stdout.readline().strip()
    if not line.startswith("READY"):
        proc.kill()
        raise RuntimeError(f"payments service failed to start: {line!r}")
    return proc, f"http://127.0.0.1:{line.split()[1]}"


def agent_cmd(cfg: Config, run_id: str, run_dir: Path, url: str) -> list[str]:
    cmd = [sys.executable, "-m", "demo.agent", "--strategy", cfg.strategy, "--run-id", run_id,
           "--run-dir", str(run_dir), "--service", url, "--fsync", cfg.fsync, "--recovery", cfg.recovery,
           "--model", cfg.model, "--quiet"]
    if cfg.power_loss:
        cmd.append("--power-loss")
    if cfg.stochastic:
        cmd.append("--stochastic")
    return cmd


def run_attempt(cmd: list[str], kill: dict | None, timeout: float = 120) -> tuple[int, float]:
    env = dict(os.environ)
    env.pop("AGENTWAL_KILL_AT", None)
    if kill and kill["mode"] == "targeted":
        env["AGENTWAL_KILL_AT"] = f"{kill['point']}:{kill['nth']}"
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if kill and kill["mode"] == "timer":
        try:
            proc.wait(timeout=kill["delay"])
        except subprocess.TimeoutExpired:
            proc.kill()  # SIGKILL
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return -999, time.perf_counter() - t0
    return proc.returncode, time.perf_counter() - t0


def plan_kill(rng: random.Random, t_ref: float, strategy: str) -> dict:
    if rng.random() < 0.5:
        # random moment: anywhere from interpreter start-up to the end of a normal run
        return {"mode": "timer", "delay": rng.uniform(0.25, 1.0) * t_ref}
    point = rng.choice(KILL_POINTS)
    return {"mode": "targeted", "point": point, "nth": rng.randint(1, MAX_HITS[point])}


def landed_at(journal: Path) -> str:
    """For a random kill on AgentWAL: the type of the last durable record (where the crash landed)."""
    recs = read_records(journal).records
    return recs[-1].type.name if recs else "EMPTY"


def run_one(cfg: Config, i: int, seed: int, url: str, db: Path, reference: dict | None, t_ref: float,
            rekill: float) -> dict:
    rng = random.Random(f"{seed}:{cfg.name}:{i}")
    for tries in range(5):  # retry only if a random-timer kill missed the first attempt
        run_id = f"{cfg.name}-{i}" + (f"-r{tries}" if tries else "")
        run_dir = RUNS / cfg.name / run_id
        shutil.rmtree(run_dir, ignore_errors=True)
        run_dir.mkdir(parents=True)
        cmd = agent_cmd(cfg, run_id, run_dir, url)
        kills: list[dict] = []
        ack_violations = 0
        rc, attempts, wall = None, 0, 0.0
        missed = False
        while attempts <= MAX_RESTARTS:
            kill = plan_kill(rng, t_ref, cfg.strategy) if attempts == 0 or rng.random() < rekill else None
            rc, dt = run_attempt(cmd, kill)
            wall += dt
            attempts += 1
            if rc == -9:
                k = dict(kill or {"mode": "timer"})
                k["attempt"] = attempts
                if cfg.strategy == "aw":
                    journal, ack = run_dir / "journal.wal", run_dir / "journal.ack"
                    ok, acked, recovered = acked_records_survive(journal, ack)
                    k.update(acked=acked, recovered=recovered, ack_ok=ok)
                    ack_violations += not ok
                    if k["mode"] == "timer":
                        k["landed_at"] = landed_at(journal)
                    if cfg.torn and journal.exists():
                        size = journal.stat().st_size
                        cut = rng.randint(0, size)
                        with open(journal, "r+b") as f:
                            f.truncate(cut)
                        k["torn_at"] = cut
                        # the harness, not the agent, removed acknowledged records: reset the ack log
                        with open(ack, "a") as f:
                            f.write(f"\n{len(read_records(journal).records)} {cut}\n")
                kills.append(k)
                continue
            if attempts == 1 and rc == 0:
                missed = True  # the timer fired after the run had already finished
            break
        if not missed:
            break
    res = check_run(run_dir, run_id, str(db), reference, cfg.deterministic)
    billing = run_dir / "billing.jsonl"
    bills = [json.loads(ln) for ln in billing.read_text().splitlines() if ln.strip()] if billing.exists() else []
    evs = [json.loads(ln) for ln in (run_dir / "events.jsonl").read_text().splitlines() if ln.strip().endswith("}")]
    recovered_events = [e for e in evs if e["event"] == "recovered"]
    journal = run_dir / "journal.wal"
    row = {
        "config": cfg.name, "run": i, "run_id": run_id, "exit_code": rc, "attempts": attempts,
        "restarts": attempts - 1, "kills": kills, "n_kills": len(kills), "timer_misses": tries,
        "ack_violations": ack_violations, "llm_calls_billed": len(bills),
        "llm_cost_usd": sum(b.get("cost_usd", 0.0) for b in bills),
        # recovery time of each restart (the first attempt has nothing to recover)
        "recovery_ms": [e["recovery_ms"] for e in recovered_events[1:]],
        "journal_bytes": journal.stat().st_size if journal.exists() else 0,
        "wall_s": wall, **res,
    }
    violations = []
    if not row["completed"]:
        violations.append("incomplete")
    if row["duplicates"]:
        violations.append("duplicate_effect")
    if row["lost"]:
        violations.append("lost_effect")
    if row["wrong"]:
        violations.append("wrong_effect")
    if row["output_matches_reference"] is False:
        violations.append("output_mismatch")
    if ack_violations:
        violations.append("acked_record_lost")
    if row["nondeterminism"]:
        violations.append("nondeterminism")
    if row["errors"]:
        violations.append("error")
    row["violations"] = violations
    return row


def reference_run(cfg: Config, url: str, db: Path, n: int = 3) -> tuple[dict | None, float]:
    """Uninterrupted runs: the reference output and the typical wall time of one run."""
    times, output = [], None
    for k in range(n):
        run_id = f"{cfg.name}-ref{k}"
        run_dir = RUNS / cfg.name / run_id
        shutil.rmtree(run_dir, ignore_errors=True)
        rc, dt = run_attempt(agent_cmd(cfg, run_id, run_dir, url), None)
        if rc != 0:
            raise RuntimeError(f"reference run for {cfg.name} failed with exit code {rc}")
        times.append(dt)
        output = json.loads((run_dir / "output.json").read_text())
    return output, statistics.median(times)


def pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q / 100 * (len(xs) - 1))))]


def summarize(cfg: Config, rows: list[dict], t_ref: float, seconds: float) -> dict:
    kills = [k for r in rows for k in r["kills"]]
    per_point: dict[str, int] = {}
    for k in kills:
        key = k["point"] if k.get("mode") == "targeted" else "random"
        per_point[key] = per_point.get(key, 0) + 1
    landed: dict[str, int] = {}
    for k in kills:
        if "landed_at" in k:
            landed[k["landed_at"]] = landed.get(k["landed_at"], 0) + 1
    rec = [x for r in rows for x in r["recovery_ms"]]
    viol: dict[str, int] = {}
    for r in rows:
        for v in r["violations"]:
            viol[v] = viol.get(v, 0) + 1
    calls = [r["llm_calls_billed"] for r in rows if r["completed"]]
    return {
        "config": cfg.name, "description": cfg.description, "settings": asdict(cfg), "date": date.today().isoformat(),
        "model_name": os.environ.get("AGENTWAL_OLLAMA_MODEL", "qwen3:8b") if cfg.model == "ollama" else cfg.model,
        "runs": len(rows), "total_kills": len(kills), "kills_by_point": dict(sorted(per_point.items())),
        "random_kills_landed_after": dict(sorted(landed.items())),
        "restarts_total": sum(r["restarts"] for r in rows), "max_restarts": max((r["restarts"] for r in rows), default=0),
        "timer_misses": sum(r["timer_misses"] for r in rows),
        "runs_completed": sum(r["completed"] for r in rows),
        "duplicated_effects": sum(r["duplicates"] for r in rows),
        "lost_effects": sum(r["lost"] for r in rows),
        "wrong_effects": sum(r["wrong"] for r in rows),
        "runs_with_violation": sum(bool(r["violations"]) for r in rows),
        "violations_by_type": dict(sorted(viol.items())),
        "nondeterminism_errors": sum(r["nondeterminism"] for r in rows),
        "acked_record_lost": sum(r["ack_violations"] for r in rows),
        "recovery_ms_p50": pct(rec, 50), "recovery_ms_p99": pct(rec, 99), "recoveries_measured": len(rec),
        "llm_calls_needed_per_run": 10,
        "llm_calls_billed_mean": statistics.mean(calls) if calls else None,  # completed runs only
        "llm_calls_wasted_total": sum(c - 10 for c in calls),
        "llm_cost_usd_total": sum(r["llm_cost_usd"] for r in rows),
        "journal_bytes_mean": statistics.mean([r["journal_bytes"] for r in rows]) if rows else 0,
        "reference_run_s": t_ref, "harness_seconds": seconds,
    }


def run_config(cfg: Config, runs: int, workers: int, seed: int, rekill: float) -> dict:
    RESULTS.mkdir(exist_ok=True)
    (RUNS / cfg.name).mkdir(parents=True, exist_ok=True)
    db = RUNS / cfg.name / "payments.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    svc, url = start_service(db, cfg.idempotent_service)
    t0 = time.time()
    try:
        reference, t_ref = reference_run(cfg, url, db)
        out = RESULTS / f"chaos_{cfg.name}.jsonl"
        rows: list[dict] = []
        with open(out, "w") as f, ThreadPoolExecutor(workers) as pool:
            futs = [pool.submit(run_one, cfg, i, seed, url, db, reference, t_ref, rekill) for i in range(runs)]
            for n, fut in enumerate(futs, 1):
                row = fut.result()
                rows.append(row)
                f.write(json.dumps(row) + "\n")
                if n % 100 == 0 or n == runs:
                    bad = sum(bool(r["violations"]) for r in rows)
                    print(f"  [{cfg.name}] {n}/{runs} runs, {sum(r['n_kills'] for r in rows)} kills, {bad} with violations",
                          flush=True)
    finally:
        svc.kill()
        svc.wait()
    summary = summarize(cfg, rows, t_ref, time.time() - t0)
    (RESULTS / f"chaos_{cfg.name}_summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="aw", help=f"one of {list(CONFIGS)} or 'all'")
    ap.add_argument("--runs", type=int, help="override the number of runs")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--seed", type=int, default=20261023)
    ap.add_argument("--rekill", type=float, default=0.3, help="probability of killing each restart again")
    args = ap.parse_args(argv)
    names = DEFAULT_SUITE if args.config == "all" else args.config.split(",")
    failed = False
    for name in names:
        cfg = CONFIGS[name]
        runs = args.runs or cfg.runs
        print(f"== {name}: {cfg.description} ({runs} runs)", flush=True)
        s = run_config(cfg, runs, args.workers if cfg.model == "fake" else min(args.workers, 2), args.seed, args.rekill)
        print(f"   kills={s['total_kills']} dup={s['duplicated_effects']} lost={s['lost_effects']} "
              f"violating_runs={s['runs_with_violation']} recovery p50={s['recovery_ms_p50']} ms "
              f"in {s['harness_seconds']:.0f}s", flush=True)
        if name in ("aw", "smoke", "aw-torn", "aw-pl-always", "aw-pl-group", "aw-reconcile-noidem") and s["runs_with_violation"]:
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
