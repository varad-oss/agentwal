# Progress

_Last updated 2026-10-23._

## Done
- **M1 Journal and recovery** (`src/agentwal/records.py`, `journal.py`): record format `[len u32][crc32 u32][type u8][JSON]`, fsync policies (always / group:N:MS / none, `+full` = F_FULLFSYNC on macOS), barriers, torn-tail truncation, `PowerLossJournal` (un-fsynced records die with the process), ack sidecar log for invariant 3.
- **M2 Durable context and replay** (`context.py`, `replay.py`, `errors.py`): `durable_run`, `ctx.llm`, `ctx.wrap` (two-phase effects, key `aw:{run_id}:{seq}`, retry-with-key or `reconcile`), `ctx.now/random/uuid`, journaled read-only tools, `NonDeterminismError` (changed request, changed call type, fewer calls, code version), tool errors journaled.
- Tests: 46 passing (`tests/unit`, `tests/property` with Hypothesis: roundtrip, truncation-prefix, single-byte corruption, replay under random crashes + tears + fsync policies + power loss gives the same effects exactly once).
- **M3 (mostly)**: `demo/fake_model.py`, `demo/real_model.py` (claude-haiku-5-5, configurable via `AGENTWAL_MODEL`), `demo/payments_service.py` (stdlib HTTP + SQLite, Idempotency-Key, `GET /payments?key=`, `--no-idempotency`), `demo/agent.py` with strategies `aw`, `b1`, `b0`, `chaos/killpoints.py` (`AGENTWAL_KILL_AT=point:n`).
- Manual smoke (one kill at `after_effect_before_result:3`, then rerun): AW 7 payments (correct), B1 8 (one duplicate), B0 10 (three duplicates). Not yet a measured result; numbers for the report must come from `make chaos`.

## Left
- Integration test (`tests/integration/`) spawning the service + agent with a targeted kill between intent and effect.
- `docs/DESIGN.md` (research notes gathered: Temporal event-history replay + nondeterminism errors + idempotent activities; DBOS step outputs checkpointed in Postgres, interrupted step retried, steps should be idempotent; LangGraph checkpointers, durability modes exit/async/sync, interrupted task re-runs, side effects belong in tasks). Sources: docs.temporal.io/workflow-execution, /workflow-definition, /activities; docs.dbos.dev/architecture; docs.langchain.com/oss/python/langgraph/durable-execution. ADRs in `docs/DECISIONS.md`.
- M4: `chaos/harness.py` (parallel runs, random-timer + targeted kills, restarts capped at 10, power-loss suites per fsync policy, torn-write mode), `chaos/invariants.py`, `chaos/report.py`, `chaos/bench.py`, Makefile, run 1,000 fake-model runs per strategy, generate `docs/EVAL_REPORT.md`. Real-model runs need Varad's OK.
- M5: README, Mermaid diagram, demo script/recording, `docs/learning/M*.md`, CI workflow, SPEC.md/PLAN.md.
