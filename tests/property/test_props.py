"""Hypothesis properties for the journal and for replay under random crashes."""

import os
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from agentwal import KILL_POINTS, ToolSpec, durable_run
from agentwal.records import RecordType, encode, scan
from tests.helpers import CountingModel, Ledger, SimulatedCrash

json_scalars = st.none() | st.booleans() | st.integers(-10**9, 10**9) | st.text(max_size=20)
json_values = st.recursive(json_scalars, lambda c: st.lists(c, max_size=4) | st.dictionaries(st.text(max_size=8), c, max_size=4), max_leaves=10)
records = st.lists(st.tuples(st.sampled_from(list(RecordType)), st.dictionaries(st.text(max_size=8), json_values, max_size=4)), max_size=12)


@given(records)
def test_encode_scan_roundtrip(recs):
    buf = b"".join(encode(t, p) for t, p in recs)
    res = scan(buf)
    assert [(r.type, r.payload) for r in res.records] == recs and res.torn_bytes == 0


@given(records, st.data())
def test_truncation_at_any_offset_recovers_a_prefix(recs, data):
    encoded = [encode(t, p) for t, p in recs]
    buf = b"".join(encoded)
    cut = data.draw(st.integers(0, len(buf)))
    res = scan(buf[:cut])
    k = len(res.records)
    assert [(r.type, r.payload) for r in res.records] == recs[:k]
    assert res.valid_length == sum(map(len, encoded[:k])) <= cut
    if k < len(recs):
        assert cut < sum(map(len, encoded[: k + 1]))  # the next record really was incomplete


@given(records, st.data())
def test_corrupting_one_byte_never_yields_wrong_records(recs, data):
    buf = bytearray(b"".join(encode(t, p) for t, p in recs))
    if not buf:
        return
    i = data.draw(st.integers(0, len(buf) - 1))
    buf[i] ^= data.draw(st.integers(1, 255))
    res = scan(bytes(buf))
    # whatever is recovered is a correct prefix of what was written
    assert [(r.type, r.payload) for r in res.records] == recs[: len(res.records)]


# -- replay under random crashes ----------------------------------------------------------

ops = st.lists(st.sampled_from(["llm", "effect", "value", "read"]), min_size=1, max_size=10)
crash = st.tuples(st.sampled_from(KILL_POINTS), st.integers(1, 4))
policies = st.sampled_from(["always", "group:3:1000", "none"])


def program(ctx, model, ledger, plan):
    llm = ctx.llm(model)
    tools = ctx.wrap({"pay": ToolSpec(ledger.pay), "look": ToolSpec(lambda i: {"i": i}, effect=False)})
    out, last = [], 0
    for i, op in enumerate(plan):
        if op == "llm":
            last = llm.complete({"prompt": f"{i}:{last}"})["amount"]
        elif op == "effect":
            out.append(tools.pay(step=i, amount=last)["payment_id"])
        elif op == "value":
            ctx.uuid()  # journaled, so it never changes control flow on replay
        else:
            tools.look(i=i)
        out.append(last)
    return out


def attempt(path, model, ledger, plan, hook, policy, power_loss):
    with durable_run(path, "prop", fault_hook=hook, fsync=policy, power_loss=power_loss) as ctx:
        if ctx.done:
            return ctx.output
        return ctx.complete(program(ctx, model, ledger, plan))


def hook_for(point, nth):
    hits = {"n": 0}

    def h(p):
        if p == point:
            hits["n"] += 1
            if hits["n"] == nth:
                raise SimulatedCrash(p)
    return h


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ops, st.lists(crash, max_size=4), st.lists(st.floats(0, 1), max_size=4), policies, st.booleans())
def test_replay_yields_the_same_effects_exactly_once(plan, crashes, tears, policy, power_loss):
    with tempfile.TemporaryDirectory() as d:
        ref_ledger = Ledger()
        ref = attempt(os.path.join(d, "ref"), CountingModel(), ref_ledger, plan, None, "always", False)
        path = os.path.join(d, "j")
        model, ledger = CountingModel(), Ledger()
        schedule = list(crashes)
        out = None
        for i in range(len(schedule) + 1):
            hook = hook_for(*schedule[i]) if i < len(schedule) else None
            try:
                out = attempt(path, model, ledger, plan, hook, policy, power_loss)
                break
            except SimulatedCrash:
                if i < len(tears) and Path(path).exists():  # also tear the journal at a random byte
                    size = Path(path).stat().st_size
                    with open(path, "r+b") as f:
                        f.truncate(int(size * tears[i]))
        assert out == ref
        key_set = lambda l: sorted((r["key"], r["step"], r["amount"]) for r in l.rows)  # noqa: E731
        assert key_set(ledger) == key_set(ref_ledger)  # same effects, each exactly once
