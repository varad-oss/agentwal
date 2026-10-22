import pytest

from agentwal import NonDeterminismError, RunMismatchError, ToolError, ToolSpec, durable_run, idempotency_key
from agentwal.journal import read_records
from agentwal.records import RecordType
from tests.helpers import CountingModel, Ledger, SimulatedCrash, crash_at


def agent(ctx, model, ledger, prompts=("a", "b", "c")):
    llm = ctx.llm(model)
    tools = ctx.wrap({"pay": ToolSpec(ledger.pay, reconcile=ledger.reconcile)})
    out = []
    for p in prompts:
        r = llm.complete({"prompt": p})
        pay = tools.pay(ticket=p, amount=r["amount"])
        out.append([p, r["amount"], pay["payment_id"]])
    return out


def run(path, model, ledger, hook=None, **kw):
    with durable_run(str(path), "r1", fault_hook=hook, **kw) as ctx:
        if ctx.done:
            return ctx.output, ctx
        return ctx.complete(agent(ctx, model, ledger)), ctx


def test_idempotency_key_is_derived_from_run_and_seq():
    assert idempotency_key("r1", 4) == "aw:r1:4"
    assert idempotency_key("r1", 4) != idempotency_key("r2", 4)


def test_uninterrupted_run_and_full_replay(tmp_path):
    model, ledger = CountingModel(), Ledger()
    out1, _ = run(tmp_path / "j", model, ledger)
    assert model.calls == 3 and len(ledger.rows) == 3
    out2, ctx = run(tmp_path / "j", model, ledger)  # finished: returns the recorded output
    assert out2 == out1 and model.calls == 3 and ledger.calls == 3


def test_replay_of_finished_run_reexecutes_nothing(tmp_path):
    model, ledger = CountingModel(), Ledger()
    out1, _ = run(tmp_path / "j", model, ledger)
    with durable_run(str(tmp_path / "j"), "r1") as ctx:
        assert ctx.complete(agent(ctx, model, ledger)) == out1
        assert ctx.stats.llm_replayed == 3 and ctx.stats.effects_replayed == 3
    assert model.calls == 3 and ledger.calls == 3


@pytest.mark.parametrize("point,nth", [
    ("llm_mid", 2), ("before_intent_fsync", 2), ("after_intent_before_effect", 2),
    ("after_effect_before_result", 2), ("after_effect_before_result", 3),
])
def test_crash_then_resume_gives_exactly_once_effects(tmp_path, point, nth):
    ref_model, ref_ledger = CountingModel(), Ledger()
    ref, _ = run(tmp_path / "ref", ref_model, ref_ledger)
    model, ledger = CountingModel(), Ledger()
    with pytest.raises(SimulatedCrash):
        run(tmp_path / "j", model, ledger, hook=crash_at(point, nth))
    out, ctx = run(tmp_path / "j", model, ledger)
    assert out == ref
    assert len(ledger.rows) == 3  # exactly one payment per step
    assert model.calls == 3 + (1 if point == "llm_mid" else 0)  # only a mid-call crash re-bills


def test_intent_without_result_retries_with_same_key(tmp_path):
    model, ledger = CountingModel(), Ledger()
    with pytest.raises(SimulatedCrash):
        run(tmp_path / "j", model, ledger, hook=crash_at("after_effect_before_result", 1))
    key = ledger.rows[0]["key"]
    _, ctx = run(tmp_path / "j", model, ledger)
    assert ctx.stats.effects_retried == 1
    assert ledger.calls == 4 and len(ledger.rows) == 3  # one retry, deduplicated by the receiver
    rec = [r for r in read_records(tmp_path / "j").records if r.type == RecordType.EFFECT_RESULT][0]
    assert rec.payload["recovered_by"] == "retry" and rec.payload["result"]["replayed"] is True
    assert key == idempotency_key("r1", 2)  # seq 1 is the first LLM call, seq 2 the first payment


def test_reconcile_finds_effect_that_happened(tmp_path):
    model, ledger = CountingModel(), Ledger(idempotent=False)
    with pytest.raises(SimulatedCrash):
        run(tmp_path / "j", model, ledger, hook=crash_at("after_effect_before_result", 1), recovery="reconcile")
    _, ctx = run(tmp_path / "j", model, ledger, recovery="reconcile")
    assert ctx.stats.effects_reconciled == 1 and ctx.stats.effects_retried == 0
    assert len(ledger.rows) == 3  # no duplicate even though the receiver does not deduplicate


def test_reconcile_not_found_invokes_tool(tmp_path):
    model, ledger = CountingModel(), Ledger(idempotent=False)
    with pytest.raises(SimulatedCrash):
        run(tmp_path / "j", model, ledger, hook=crash_at("after_intent_before_effect", 1), recovery="reconcile")
    assert ledger.rows == []
    _, ctx = run(tmp_path / "j", model, ledger, recovery="reconcile")
    assert ctx.stats.effects_retried == 1 and len(ledger.rows) == 3


def test_without_idempotency_or_reconcile_the_window_duplicates(tmp_path):
    """Documents the assumption: retry-with-key is only safe if the receiver deduplicates."""
    model, ledger = CountingModel(), Ledger(idempotent=False)
    with pytest.raises(SimulatedCrash):
        run(tmp_path / "j", model, ledger, hook=crash_at("after_effect_before_result", 1))
    run(tmp_path / "j", model, ledger)
    assert len(ledger.rows) == 4


def test_nondeterminism_changed_request(tmp_path):
    model, ledger = CountingModel(), Ledger()
    run(tmp_path / "j", model, ledger)
    with durable_run(str(tmp_path / "j"), "r1") as ctx:
        llm = ctx.llm(model)
        with pytest.raises(NonDeterminismError) as ei:
            llm.complete({"prompt": "DIFFERENT"})
    assert ei.value.seq == 1 and "DIFFERENT" in str(ei.value) and "journal recorded" in str(ei.value)


def test_nondeterminism_changed_call_type(tmp_path):
    model, ledger = CountingModel(), Ledger()
    run(tmp_path / "j", model, ledger)
    with durable_run(str(tmp_path / "j"), "r1") as ctx:
        with pytest.raises(NonDeterminismError, match="expected LLM_CALL"):
            ctx.wrap({"pay": ledger.pay}).pay(ticket="a", amount=1)


def test_nondeterminism_fewer_calls(tmp_path):
    model, ledger = CountingModel(), Ledger()
    run(tmp_path / "j", model, ledger)
    with durable_run(str(tmp_path / "j"), "r1") as ctx:
        agent(ctx, model, ledger, prompts=("a",))
        with pytest.raises(NonDeterminismError, match="fewer calls"):
            ctx.complete([])


def test_unjournaled_randomness_is_detected(tmp_path):
    import random

    def flaky(ctx):
        return ctx.llm(CountingModel()).complete({"prompt": str(random.random())})

    with pytest.raises(SimulatedCrash):
        with durable_run(str(tmp_path / "j"), "r1", fault_hook=crash_at("llm_mid", 1)) as ctx:
            flaky(ctx)
    # LLM_CALL is journaled without a result; the re-issued request differs -> detected
    with durable_run(str(tmp_path / "j"), "r1") as ctx:
        with pytest.raises(NonDeterminismError):
            flaky(ctx)


def test_journaled_values_are_stable_across_restarts(tmp_path):
    def body(ctx):
        return [ctx.now(), ctx.random(), ctx.uuid()]

    with pytest.raises(SimulatedCrash):
        with durable_run(str(tmp_path / "j"), "r1", fault_hook=crash_at("llm_mid")) as ctx:
            first = body(ctx)
            ctx.llm(CountingModel()).complete({"prompt": "x"})
    with durable_run(str(tmp_path / "j"), "r1") as ctx:
        assert body(ctx) == first and ctx.stats.values_replayed == 3


def test_code_version_and_input_mismatch(tmp_path):
    run(tmp_path / "j", CountingModel(), Ledger())
    with pytest.raises(NonDeterminismError, match="code version"):
        with durable_run(str(tmp_path / "j"), "r1", code_version="v2"):
            pass
    with pytest.raises(RunMismatchError):
        with durable_run(str(tmp_path / "j"), "r1", input={"other": 1}):
            pass
    with pytest.raises(RunMismatchError):
        with durable_run(str(tmp_path / "j"), "r2"):
            pass


def test_read_only_tools_journaled_or_not(tmp_path):
    calls = {"n": 0}

    def lookup(ticket):
        calls["n"] += 1
        return {"ticket": ticket, "n": calls["n"]}

    for journal_reads, expected in [(True, 1), (False, 2)]:
        calls["n"] = 0
        p = tmp_path / f"j{journal_reads}"
        for _ in range(2):
            with durable_run(str(p), "r1", journal_reads=journal_reads) as ctx:
                ctx.wrap({"lookup": ToolSpec(lookup, effect=False)}).lookup(ticket="t1")
        assert calls["n"] == expected


def test_tool_errors_are_journaled_and_replayed(tmp_path):
    calls = {"n": 0}

    def boom(idempotency_key, **_):
        calls["n"] += 1
        raise RuntimeError("card declined")

    for _ in range(2):
        with durable_run(str(tmp_path / "j"), "r1") as ctx:
            with pytest.raises(ToolError, match="card declined"):
                ctx.wrap({"pay": boom}).pay(amount=1)
    assert calls["n"] == 1
