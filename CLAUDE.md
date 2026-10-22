# AgentWAL
Crash-safe agent runs via a write-ahead journal. Spec: the P4 prompt (to be copied to docs/SPEC.md). Status: docs/PROGRESS.md.
- Env: `uv venv --python 3.12 .venv && uv pip install -e . pytest hypothesis`; tests: `.venv/bin/pytest`.
- Library is stdlib-only (`src/agentwal`). Demo in `demo/`, chaos harness in `chaos/`.
- Never weaken tests; never fabricate numbers (they come from `results/`). Ask before real-model spend.
