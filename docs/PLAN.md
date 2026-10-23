# Plan

Built in one pass at Varad's request (no stop at each milestone boundary); learning notes were written at the end.

| # | Milestone | Acceptance criteria | Status |
|---|---|---|---|
| M0 | Research and design | `DESIGN.md` compares Temporal, DBOS, LangGraph with citations; ADRs for record format and fsync policy | done |
| M1 | Journal and recovery | encoding, CRC, fsync policies, torn-tail truncation; unit + Hypothesis tests pass | done |
| M2 | Durable context and replay | `ctx.llm`, `ctx.wrap`, `ctx.now/random/uuid`; non-determinism detection; integration test with a targeted crash passes | done |
| M3 | Demo, mock service, baselines | refund batch agent; payments service with/without idempotency; B0 and B1 | done |
| M4 | Chaos harness and report | 1,000-run chaos for B0, B1, AW; power-loss suites per fsync policy; torn writes; `EVAL_REPORT.md` generated from results | done (5,200 fake-model runs + 30 real-LLM runs with qwen3:8b via Ollama, free) |
| M5 | Docs and demo | README, Mermaid diagram of the crash windows, kill-and-resume demo script | done (`docs/demo.gif`, `make gif`) |

Commands: `make test`, `make chaos`, `make bench`, `make report`, `make chaos-real` (free local LLM), `make demo`.
