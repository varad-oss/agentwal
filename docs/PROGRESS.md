# Progress

_Last updated 2026-10-23._

## State
All milestones M0–M5 are built (see `docs/PLAN.md`). Built in one pass; learning notes are in `docs/learning/M0.md`–`M5.md`.

- Library: `src/agentwal/` — records, journal (fsync policies, torn-tail recovery, `PowerLossJournal`), replay index, durable context, `python -m agentwal.dump`.
- Demo: `demo/agent.py` (strategies `aw`, `b1`, `b0`), fake model, free local-LLM adapter (Ollama), payments service, `make demo`.
- Chaos: `chaos/harness.py` (suites in `CONFIGS`), `invariants.py`, `bench.py`, `report.py`, `real.py`.
- Tests: unit, property (Hypothesis), integration (real processes and SIGKILLs). `make test`.
- Real-model runs done 2026-10-24: 30 runs with qwen3:8b via Ollama (free), results in `results/chaos_real*`. Rerun with `make chaos-real` only when no other project is using Ollama (16 GB machine).
- Results: `results/chaos_*.jsonl` + `_summary.json`, `results/bench.json`; report in `docs/EVAL_REPORT.md`, README table generated from them.

## Open items (need Varad)
- **GIF** for the README: record `make demo` (asciinema + agg, or vhs) to `docs/demo.gif`.
- Optional: push to GitHub so CI runs (`.github/workflows/ci.yml`: tests + 50-run chaos smoke).

## Known limitations
- Single process, single journal file; no compaction or segment rotation.
- A corrupt record in the middle of the journal discards everything after it (treated as a torn tail).
- Recovery of an intent without a result relies on an idempotent receiver or a reconcile lookup; the `aw-noidem` suite shows the failure without them.
