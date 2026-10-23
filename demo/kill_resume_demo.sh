#!/usr/bin/env bash
# Kill-and-resume demo: AgentWAL vs checkpoint-after-step, killed right after a refund is paid.
# Recorded for the README with:  make gif   (asciinema --headless + agg)
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
DIR=runs/demo
mkdir -p "$DIR"
rm -rf "${DIR:?}"/aw "${DIR:?}"/b1 "${DIR:?}"/payments.db*

say() { printf '\n\033[1;36m$ %s\033[0m\n' "$*"; sleep "${DEMO_PAUSE:-1}"; }

$PY -m demo.payments_service --db "$DIR/payments.db" --port 8765 >/dev/null &
SVC=$!
trap 'kill $SVC 2>/dev/null' EXIT
sleep 0.5
URL=http://127.0.0.1:8765
# Run an attempt that is expected to be SIGKILLed; hides bash's own "Killed: 9" job notice.
killed_run() { { bash -c "$*"; } 2>/dev/null || echo "agent exited with code $? (137 = killed by SIGKILL, like kill -9)"; }
count() { curl -s "$URL/payments?run_id=$1" | $PY -c 'import json,sys; print(len(json.load(sys.stdin)), "payments in the ledger for", sys.argv[1])' "$1"; }

say "# AgentWAL: kill -9 the agent right after the 3rd refund is paid, before it is journaled"
say "AGENTWAL_KILL_AT=after_effect_before_result:3 python -m demo.agent --strategy aw --run-id demo-aw ..."
killed_run AGENTWAL_KILL_AT=after_effect_before_result:3 $PY -m demo.agent --strategy aw --run-id demo-aw --run-dir "$DIR/aw" --service $URL
count demo-aw
say "python -m agentwal.dump $DIR/aw/journal.wal   # the last intent has no result"
$PY -m agentwal.dump "$DIR/aw/journal.wal" | tail -4 | cut -c1-${DEMO_COLS:-200}
say "# rerun: completed calls replay from the journal; the open intent retries with the same key"
$PY -m demo.agent --strategy aw --run-id demo-aw --run-dir "$DIR/aw" --service $URL
count demo-aw
echo "LLM calls billed: $(wc -l < "$DIR/aw/billing.jsonl" | tr -d " ") (10 needed)"

say "# Baseline B1 (checkpoint after each step), same kill point"
killed_run AGENTWAL_KILL_AT=after_effect_before_result:3 $PY -m demo.agent --strategy b1 --run-id demo-b1 --run-dir "$DIR/b1" --service $URL
$PY -m demo.agent --strategy b1 --run-id demo-b1 --run-dir "$DIR/b1" --service $URL
count demo-b1
echo "(7 refunds were intended: B1 paid one of them twice)"
