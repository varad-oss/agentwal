# Pitch video narration

Transcript of [agentwal-pitch.mp4](agentwal-pitch.mp4) (76 s). Synthetic voice: Kokoro `am_michael`, generated locally.

Your AI agent just issued a refund. Then it crashed.
Restart it, and it pays again. Save a checkpoint after every step, and it still pays twice, because the crash lands between the payment and the save.
Meet AgentWAL: a write-ahead journal for LLM agents.
Before any side effect, it writes an intent to disk, with an idempotency key. Then it pays. Then it records the result. Every crash window has a recovery path.
Kill it anywhere. Rerun the same command. Finished calls replay from the journal, the open payment retries with the same key, and the ledger ends with exactly seven refunds.
We killed it over seven thousand times. In a thousand runs each: naive restart paid twenty-four hundred refunds twice. Checkpointing, a hundred and sixty-three. AgentWAL: zero.
No duplicate side effects. No lost progress. No LLM call paid twice. For about a tenth of a millisecond per step.
AgentWAL. Open source and built from first principles. Find it on GitHub.
