# Progress

_Last updated 2026-10-23._

## State
All milestones M0–M5 are built (see `docs/PLAN.md`). Built in one pass; learning notes are in `docs/learning/M0.md`–`M5.md`.

- Library: `src/agentwal/` — records, journal (fsync policies, torn-tail recovery, `PowerLossJournal`), replay index, durable context, `python -m agentwal.dump`.
- Demo: `demo/agent.py` (strategies `aw`, `b1`, `b0`), fake model, Anthropic adapter, payments service, `make demo`.
- Chaos: `chaos/harness.py` (suites in `CONFIGS`), `invariants.py`, `bench.py`, `report.py`, `real.py`.
- Tests: unit, property (Hypothesis), integration (real processes and SIGKILLs). `make test`.
- Results: `results/chaos_*.jsonl` + `_summary.json`, `results/bench.json`; report in `docs/EVAL_REPORT.md`, README table generated from them.

## Open items (need Varad)
- **Real-model runs**: not run. `make chaos-real` (needs `ANTHROPIC_API_KEY` in `.env` and `uv pip install -e ".[real]"`) runs a pilot, prints the cost, asks before the 30 runs. Default model `claude-haiku-5-5` (set `AGENTWAL_MODEL` to change).
- **GIF** for the README: record `make demo` (asciinema + agg, or vhs) to `docs/demo.gif`.
- Optional: push to GitHub so CI runs (`.github/workflows/ci.yml`: tests + 50-run chaos smoke).

## Known limitations
- Single process, single journal file; no compaction or segment rotation.
- A corrupt record in the middle of the journal discards everything after it (treated as a torn tail).
- Recovery of an intent without a result relies on an idempotent receiver or a reconcile lookup; the `aw-noidem` suite shows the failure without them.
