# Architecture decision records

## ADR-1: Record format — length + CRC32 + type + canonical JSON

**Context.** Recovery must tell a complete record from a torn or corrupt one without any index.
**Decision.** `[length u32][crc32 u32][type u8][payload JSON]`, little-endian, CRC over type + payload, payload as canonical JSON (sorted keys, compact separators). Lengths over 16 MB are treated as garbage.
**Why.** Same scheme as the Raft KV store's WAL: the length frames the record, the CRC proves it is whole. JSON keeps the journal debuggable (`agentwal.read_records`) and LLM responses are JSON anyway. Canonical encoding makes request hashes stable.
**Trade-offs.** JSON is larger than a binary encoding (~860 bytes per agent step, see `results/bench.json`). CRC32 detects accidental corruption, not tampering. A corrupt record in the middle of the log discards everything after it — acceptable for a single-writer log where corruption is almost always at the tail.

## ADR-2: fsync policy — configurable, with barriers before effects

**Context.** fsync per record is the safest and the slowest; no fsync is fast and loses data on power failure.
**Decision.** Three policies (`always`, `group:N:MS`, `none`), plus `+full` for macOS `F_FULLFSYNC`. Independently of the policy, `EFFECT_INTENT` and `EFFECT_RESULT` are barriers that force an fsync (except under `none`). Default: `always`.
**Why.** Correctness only needs the intent to be durable before the external effect (the classic *output commit* rule). LLM records can ride along in a group commit: losing one costs a re-billed call, not a duplicated refund — and the barrier before the next effect flushes them anyway. `none` is kept to measure, under simulated power loss, what fsync protects.
**Trade-offs.** Group commit is lazy (checked at append time, no background flusher). On macOS plain `fsync` does not flush the drive cache; `+full` does, at a much higher cost (measured in the benchmark).

## ADR-3: Idempotency key = `aw:{run_id}:{seq}`

**Decision.** Keys are derived from the run id and the call's sequence number, not random.
**Why.** A retry after a crash must send the same key even if the intent record was lost (power loss before fsync); a deterministic key makes that automatic. Readable keys make the payments table easy to audit.
**Trade-off.** If non-deterministic code makes a *different* effect land on the same seq, the receiver sees a reused key with different parameters; the mock service rejects it with 409 (as Stripe does). That is exactly what the `aw-pl-none-stoch` suite shows.

## ADR-4: Replay by sequence number, not by name

**Decision.** Calls are matched by their position in the run, with a request hash to detect drift.
**Why.** Agent loops have no stable step names; position plus hash is what Temporal effectively does with its event history.

## ADR-5: Mock payments service on the stdlib

**Decision.** `http.server.ThreadingHTTPServer` + SQLite rather than FastAPI.
**Why.** Thousands of agent processes start against it; a stdlib service keeps the demo dependency-free and fast to start. The library itself is stdlib-only.

## ADR-6: Only permanent tool errors are journaled

**Context.** The first version journaled any exception from an effect tool as its final result. A connection reset can happen *after* the receiver applied the effect, so recording "failed" would be wrong, and replay would make the run fail forever.
**Decision.** A tool raises `PermanentToolError` when the effect definitely did not happen (card declined, validation error); that is journaled and replayed as `ToolError`. Any other exception propagates with the intent left open, so the next attempt retries with the same key, like a crash in window ③.
**Consequence.** The `aw-pl-none-stoch` suite was re-run after this change (results in `results/`); the other suites never raise from a tool, so they are unaffected.
