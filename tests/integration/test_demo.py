"""End-to-end: the real agent process, the real payments service, real SIGKILLs."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from chaos.harness import ROOT, start_service
from chaos.invariants import comparable, payments_for_run


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    db = tmp_path_factory.mktemp("svc") / "payments.db"
    proc, url = start_service(db)
    yield url, db
    proc.kill()
    proc.wait()


def agent(url, run_id, run_dir, strategy="aw", kill=None, extra=()):
    env = {k: v for k, v in os.environ.items() if k != "AGENTWAL_KILL_AT"}
    env["AGENTWAL_FAKE_LATENCY_MS"] = "0"
    if kill:
        env["AGENTWAL_KILL_AT"] = kill
    cmd = [sys.executable, "-m", "demo.agent", "--strategy", strategy, "--run-id", run_id, "--run-dir", str(run_dir),
           "--service", url, "--quiet", *extra]
    return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True).returncode


@pytest.fixture(scope="module")
def reference(service, tmp_path_factory):
    url, db = service
    d = tmp_path_factory.mktemp("ref")
    assert agent(url, "ref", d) == 0
    return json.loads((d / "output.json").read_text())


@pytest.mark.parametrize("kill", ["after_intent_before_effect:2", "after_effect_before_result:2",
                                  "before_intent_fsync:4", "llm_mid:5"])
def test_aw_survives_targeted_kill(service, reference, tmp_path, kill):
    url, db = service
    run_id = f"aw-{kill.replace(':', '-')}"
    assert agent(url, run_id, tmp_path, kill=kill) == -9  # really SIGKILLed
    assert not (tmp_path / "output.json").exists()
    assert agent(url, run_id, tmp_path) == 0
    out = json.loads((tmp_path / "output.json").read_text())
    pays = payments_for_run(str(db), run_id)
    assert comparable(out) == comparable(reference)
    assert sorted(p["ticket_id"] for p in pays) == sorted(d["ticket_id"] for d in out["decisions"] if d["payment_id"])
    assert len(pays) == 7  # exactly one payment per refund, none duplicated
    billed = len((tmp_path / "billing.jsonl").read_text().splitlines())
    assert billed == 10 + (1 if kill.startswith("llm_mid") else 0)


def test_rerunning_a_finished_run_does_nothing(service, tmp_path):
    url, db = service
    assert agent(url, "aw-done", tmp_path) == 0
    assert agent(url, "aw-done", tmp_path) == 0
    assert len(payments_for_run(str(db), "aw-done")) == 7
    assert len((tmp_path / "billing.jsonl").read_text().splitlines()) == 10


def test_checkpoint_baseline_duplicates_in_the_window(service, tmp_path):
    """B1 has no intents: a crash between the effect and the checkpoint repeats the refund."""
    url, db = service
    assert agent(url, "b1-window", tmp_path, strategy="b1", kill="after_effect_before_result:2") == -9
    assert agent(url, "b1-window", tmp_path, strategy="b1") == 0
    assert len(payments_for_run(str(db), "b1-window")) == 8


def test_journal_from_other_code_version_is_rejected(service, tmp_path):
    url, _db = service
    assert agent(url, "aw-ver", tmp_path, kill="llm_mid:3") == -9
    wal = Path(tmp_path / "journal.wal")
    data = wal.read_bytes()
    # rewrite RUN_START as if an older build of the agent wrote it
    from agentwal.records import RecordType, encode, scan

    recs = scan(data).records
    first = recs[0]
    payload = dict(first.payload, code_version="refund-agent-v0")
    wal.write_bytes(encode(RecordType.RUN_START, payload) + data[first.size:])
    assert agent(url, "aw-ver", tmp_path) == 3  # NonDeterminismError exit code
