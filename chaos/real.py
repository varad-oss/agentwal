"""Real-model chaos runs, gated on a pilot and an explicit "yes".

1. One uninterrupted pilot run with the real model; prints its cost.
2. Estimates the cost of 30 runs (with kills, restarts re-bill only interrupted calls).
3. Asks for confirmation, then runs the `real` suite.

Needs ANTHROPIC_API_KEY in .env and `uv pip install -e ".[real]"`.
"""

from __future__ import annotations

import json
import shutil
import sys

from chaos.harness import CONFIGS, RUNS, agent_cmd, run_attempt, run_config, start_service


def main() -> int:
    cfg = CONFIGS["real"]
    (RUNS / "real").mkdir(parents=True, exist_ok=True)
    db = RUNS / "real" / "pilot.db"
    svc, url = start_service(db)
    try:
        run_dir = RUNS / "real" / "pilot"
        shutil.rmtree(run_dir, ignore_errors=True)
        rc, dt = run_attempt(agent_cmd(cfg, "real-pilot", run_dir, url), None)
        bills = [json.loads(ln) for ln in (run_dir / "billing.jsonl").read_text().splitlines()] if (run_dir / "billing.jsonl").exists() else []
    finally:
        svc.kill()
        svc.wait()
    if rc != 0:
        print(f"pilot failed (exit {rc}); see {run_dir}/events.jsonl", file=sys.stderr)
        return 1
    cost = sum(b["cost_usd"] for b in bills)
    est = cost * cfg.runs * 1.3  # +30% for calls re-billed after mid-call kills and reference runs
    print(f"pilot: {len(bills)} model calls, {sum(b['input_tokens'] for b in bills)} in / "
          f"{sum(b['output_tokens'] for b in bills)} out tokens, ${cost:.4f}, {dt:.1f}s")
    print(f"estimated cost of {cfg.runs} chaos runs: ~${est:.3f}")
    if input("run them? [y/N] ").strip().lower() != "y":
        print("cancelled")
        return 0
    s = run_config(cfg, cfg.runs, 4, 20261023, 0.3)
    print(json.dumps({k: s[k] for k in ("runs", "total_kills", "duplicated_effects", "lost_effects",
                                        "runs_with_violation", "llm_cost_usd_total")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
