# Claude Code Implementation Prompt — Project 4: AgentWAL

> **How to use this file**
>
> 1. **When:** Sun 25 Oct afternoon (Day 3), in a second Claude Code session while TrustDesk's evals run; finish Mon 26 Oct morning. About one day of build time. Most testing uses a fake model, so it costs almost nothing.
> 2. Create an empty folder `agentwal`, `git init`, start Claude Code in **plan mode**, fill in section 0's brackets, and paste everything below the line.
> 3. After M0 the spec lives in the repo as `docs/SPEC.md`; start later sessions with the session-start prompt at the bottom.

---

## 0. Who I am, what we are building, and how we work

I am Varad, a B.Tech student at IIT Kharagpur applying for **Agentic AI Intern** roles; deadline **[DATE]**; API budget for this project about **[BUDGET — e.g., $2]**. I built a Raft-replicated key-value store in Go with an LSM-tree and a CRC32-checked write-ahead log, and tested it by killing the leader with `kill -9`. This project applies the same ideas to LLM agents.

We are building **AgentWAL**: a small Python library that makes an agent run **crash-safe**. After a `kill -9` at any point, rerunning the same command resumes the run; a completed tool side effect (such as a refund) is never repeated, no acknowledged step is lost, and completed LLM calls are not paid for twice. A chaos harness proves it.

This is a focused study of **durable execution** — the idea behind workflow engines such as Temporal and DBOS and behind LangGraph's checkpointing — built from first principles. It is not a replacement for those systems; the README should say so.

### Working agreement

1. **Research first:** before designing, read how Temporal (deterministic replay of an event history), DBOS (workflow steps recorded in a database) and LangGraph (checkpoints between steps; an interrupted node re-runs from its start) handle crashes and side effects. Write a one-page comparison in `docs/DESIGN.md` and cite the docs you used. Never rely on memory for their APIs.
2. **Plan first** in plan mode: `docs/PLAN.md` with milestones and acceptance criteria; wait for my approval.
3. **One milestone at a time**; at each boundary run tests, show the demo command, write `docs/learning/M<n>.md`, update `docs/PROGRESS.md`, commit, and **stop** until I type `continue`.
4. **Tests are the source of truth**; never weaken them. **Never fabricate results**; numbers come from `results/` via Makefile commands.
5. **Ask before spending** on real-model runs (pilot first). Secrets only in `.env`. Push back when the spec is wrong. Keep `CLAUDE.md` short.

---

## 1. Goal and scope

**Guarantees to provide and prove:**

1. **No duplicated side effects:** each intended tool effect is applied exactly once, assuming the receiving service honours idempotency keys (or offers a reconciliation lookup).
2. **No lost progress:** every step recorded as complete before a crash is still complete after recovery.
3. **Deterministic resume:** after recovery the run reaches the same final output as an uninterrupted run, because completed LLM calls and non-deterministic values are replayed from the journal instead of re-sampled.
4. **Cost saving on resume:** completed LLM calls are not re-billed.

**Non-goals:** distributed or multi-worker execution, a general workflow engine, cross-machine replication, matching Temporal or DBOS features.

---

## 2. Design

### 2.1 The journal (write-ahead log)

- Append-only segment file. Record layout: `[length u32][crc32 u32][type u8][payload JSON]`.
- Record types: `RUN_START(run_id, code_version, input_hash)`, `LLM_CALL(seq, request_hash)`, `LLM_RESULT(seq, response)`, `EFFECT_INTENT(seq, tool, args, idempotency_key)`, `EFFECT_RESULT(seq, result)`, `VALUE(seq, kind, value)` for journaled clock/random/uuid values, `RUN_END(output)`.
- Durability: configurable fsync policy — every record, group commit (every N records or T ms), or none — and **measure** the latency each costs.
- Recovery: scan from the start, verify each CRC, and **truncate a torn tail** at the last valid record.

### 2.2 The durable context API

```python
with durable_run("runs/demo.wal", run_id="r-42") as ctx:
    llm = ctx.llm(model_adapter)        # journaled model calls
    tools = ctx.wrap(tools)             # effects journaled with intents + idempotency keys
    result = Agent(llm=llm, tools=tools).run(task)
```

- **Replay model:** after a crash the agent code runs again from the top. Each journaled call is matched by sequence number: completed calls return their recorded results immediately; the first incomplete call executes for real.
- **Non-determinism detection:** if a replayed call's request hash differs from the journal (the code changed, or used an unjournaled random value), raise `NonDeterminismError` naming both requests.
- **Effects, two-phase:** write and fsync `EFFECT_INTENT` with a key derived from `(run_id, seq)` → call the tool with that key → write and fsync `EFFECT_RESULT`. On recovery, an intent with a result returns the recorded result; an intent **without** a result re-invokes the tool with the **same key** (the receiver deduplicates) or calls the tool's `reconcile(key)` to learn whether the effect happened.
- Journaled sources of non-determinism: `ctx.now()`, `ctx.random()`, `ctx.uuid()`.
- Read-only tools can be journaled too (configurable) so replay is fully deterministic.

### 2.3 Demo agent and mock service

- **Refund batch agent:** works through 10 support tickets; for each, the model (fake or real) decides whether to refund and how much, then calls `issue_refund`.
- **Mock payments service:** a separate local process (small FastAPI or socket server) backed by SQLite. It honours an `Idempotency-Key` header (deduplicates) and exposes `GET /payments?key=` for reconciliation. A flag can disable idempotency, to show what breaks.

### 2.4 Baselines

- **B0 Naive:** no journal; after a crash, start the batch over.
- **B1 Checkpoint-after-step:** save the agent's state after each completed step (as many frameworks do), with no effect intents.
- **AW AgentWAL.**

---

## 3. Chaos harness (the centerpiece)

- Run the demo **1,000 times with the fake model** (free and fast) and **30 times with the real model**, killing the agent process with `SIGKILL` at random moments plus targeted kill points: before the intent fsync, after the intent but before the effect, after the effect but before the result, and in the middle of an LLM call. Use a fault-injection hook or environment variable to hit targeted points.
- Every fake-model run takes at least one kill. After each kill, restart until the run completes (cap at 10 restarts). Report runs, total kills and kills per targeted point; my CV quotes total kills.
- **Process crash versus power loss:** `kill -9` only kills the process, so records already written to the OS page cache survive even without fsync. To test what fsync protects (OS crashes and power loss), add a fault-injecting journal writer that drops every record not yet fsynced at the moment of the "crash", and run the chaos suite under each fsync policy. Report results per policy.
- **Invariants**, checked against the payments database and an uninterrupted reference run:
  1. exactly one payment per intended `(run_id, step)` — no duplicates, none missing;
  2. final output identical to the reference;
  3. every record acknowledged before the crash survives recovery;
  4. no `NonDeterminismError` while the code is unchanged.
- **Metrics per strategy (B0, B1, AW):** duplicated effects, lost effects, runs violating any invariant, recovery time (p50, p99), per-step overhead versus no journal (fsync on, group commit, off), journal bytes per step, and LLM calls (and dollars, in real-model runs) saved on resume.
- **Torn-write test:** truncate the journal at random byte offsets; recovery must stop at the last valid record and the run must still finish correctly.
- **Property-based tests (Hypothesis):** random record sequences and crash points; replay must yield the same set of effects.

---

## 4. Testing

- **Unit:** record encoding and CRC, torn-tail truncation, sequence matching, non-determinism detection, idempotency-key derivation, reconciliation paths.
- **Integration:** the demo agent with the fake model and the mock service, including a targeted crash between intent and effect.
- **Chaos suite:** `make chaos` (1,000 fake-model runs) and `make chaos-real` (30 real-model runs, asks for confirmation and prints the pilot cost).
- CI runs unit and integration tests plus a 50-run chaos smoke test.

---

## 5. Repository layout

```
agentwal/
  CLAUDE.md  README.md  Makefile  pyproject.toml  .env.example
  src/agentwal/ journal.py  records.py  context.py  replay.py  errors.py
  demo/ agent.py  payments_service.py  fake_model.py
  chaos/ harness.py  killpoints.py  invariants.py  report.py
  results/   docs/ SPEC.md PLAN.md PROGRESS.md DESIGN.md DECISIONS.md EVAL_REPORT.md learning/
  tests/ unit/ integration/ property/
```

---

## 6. Milestones and acceptance criteria

| # | Milestone | Est. | Acceptance criteria |
|---|---|---|---|
| M0 | Research and design | 45 min | `docs/DESIGN.md` compares Temporal, DBOS and LangGraph crash handling with citations; ADRs for record format and fsync policy. |
| M1 | Journal and recovery | 1.5–2 h | Encoding, CRC, fsync policies, torn-tail truncation; unit and Hypothesis tests pass. |
| M2 | Durable context and replay | 2 h | `ctx.llm`, `ctx.wrap`, `ctx.now/random/uuid`; non-determinism detection; integration test with a targeted crash passes. |
| M3 | Demo, mock service, baselines | 1.5 h | Refund batch agent; payments service with and without idempotency; B0 and B1 implemented. |
| M4 | Chaos harness and report | 1.5 h + runs | 1,000-run chaos for B0, B1 and AW; 30 real-model runs after my OK; `docs/EVAL_REPORT.md` generated from results. |
| M5 | Docs and demo | 1 h | README (section 9), a Mermaid sequence diagram of intent → effect → result with the crash windows marked, a terminal recording of kill-and-resume. |

---

## 7. Cut order if we fall behind

Real-model chaos runs → group-commit measurement → the B0 baseline. **Never cut:** the B1 baseline (my CV compares against it), the targeted crash between intent and effect, the 1,000-run fake-model chaos for AgentWAL, torn-write recovery, and the invariant checks.

---

## 8. Teach me as we go (required)

After each milestone write `docs/learning/M<n>.md`: what we built and why; key concepts with pointers to code; how to verify it myself; three interview questions with model answers; one break-it exercise. If I type `quiz me`, ask the questions one at a time and grade my answers honestly. Make sure I can explain: why exactly-once *delivery* is impossible but exactly-once *effects* are achievable with idempotent receivers; why checkpoint-after-step still duplicates effects (the window between the effect and the checkpoint); why replay requires determinism; and what fsync actually guarantees, and why `kill -9` alone cannot test it.

---

## 9. README structure

1. One-line pitch and a GIF of a run being killed and resumed.
2. Results table: B0, B1 and AgentWAL × duplicated effects, lost effects, invariant violations, recovery p50/p99, per-step overhead — with run counts and date.
3. How it works: the intent → effect → result diagram with crash windows; replay and non-determinism detection.
4. Guarantees and their assumptions (idempotent receiver or reconciliation; deterministic agent code outside the journal).
5. How this relates to Temporal, DBOS and LangGraph checkpoints, honestly scoped.
6. Quickstart: `make chaos` with the fake model, no API key needed.
7. How it was built: "Implemented with Claude Code as a pair programmer; design, failure model and chaos experiments by me."

---

## 10. Definition of done

- [ ] `make test` and `make chaos` green; results committed and the report regenerated from them.
- [ ] Zero invariant violations for AgentWAL across the chaos runs, with B0/B1 violation counts reported alongside.
- [ ] I can draw the crash windows and explain each recovery path without notes.

---

### Session-start prompt

> Read `CLAUDE.md`, `docs/SPEC.md`, `docs/PLAN.md` and `docs/PROGRESS.md`. Summarize where we are in 5 bullets, then continue with the next step of the current milestone under the working agreement. Stop at the milestone boundary.

### Milestone-review prompt

> Before I type `continue`: list each changed file with one line on why; show the test output; name the two places most likely to break in a live demo and how to check them; then quiz me with the three questions from the learning note.
