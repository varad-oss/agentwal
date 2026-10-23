"""Real-model chaos runs with a free local LLM (Ollama, qwen3:8b by default; no API key, no cost).

1. One uninterrupted pilot run; prints model calls, tokens and wall time.
2. Estimates how long the 30 chaos runs will take.
3. Runs the `real` suite (pass --pilot-only to stop after the estimate).

Needs `ollama serve` running and the model pulled (`ollama pull qwen3:8b`). The model is
large for a 16 GB machine, so don't run this while another project is using Ollama.
"""

from __future__ import annotations

import json
import shutil
import sys

from chaos.harness import CONFIGS, RUNS, agent_cmd, run_attempt, run_config, start_service


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cfg = CONFIGS["real"]
    (RUNS / "real").mkdir(parents=True, exist_ok=True)
    svc, url = start_service(RUNS / "real" / "pilot.db")
    try:
        run_dir = RUNS / "real" / "pilot"
        shutil.rmtree(run_dir, ignore_errors=True)
        rc, dt = run_attempt(agent_cmd(cfg, "real-pilot", run_dir, url), None, timeout=1800)
        billing = run_dir / "billing.jsonl"
        bills = [json.loads(ln) for ln in billing.read_text().splitlines()] if billing.exists() else []
    finally:
        svc.kill()
        svc.wait()
    if rc != 0:
        print(f"pilot failed (exit {rc}); see {run_dir}/events.jsonl", file=sys.stderr)
        return 1
    print(f"pilot: {len(bills)} model calls, {sum(b['input_tokens'] for b in bills)} in / "
          f"{sum(b['output_tokens'] for b in bills)} out tokens, cost $0 (local model), {dt:.0f}s wall time")
    est = dt * (cfg.runs * 1.5 + 3) / 2  # restarts re-run parts of a run; 3 reference runs; 2 workers
    print(f"estimated time for {cfg.runs} chaos runs: ~{est / 60:.0f} min")
    if "--pilot-only" in argv:
        return 0
    s = run_config(cfg, cfg.runs, 2, 20261023, 0.3)
    print(json.dumps({k: s[k] for k in ("runs", "total_kills", "duplicated_effects", "lost_effects",
                                        "runs_with_violation", "llm_calls_wasted_total")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
