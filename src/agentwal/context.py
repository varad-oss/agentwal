"""The durable context: journaled LLM calls, two-phase tool effects and journaled values.

Replay model: after a crash the agent code simply runs again from the top. Every journaled
operation takes the next sequence number. If the journal already holds a completed entry
at that number, its recorded result is returned without doing any work; the first
operation with no completed entry runs for real. Because the agent's control flow only
depends on journaled values, the code takes the same path up to the crash point.
"""

from __future__ import annotations

import random as _random
import time
import uuid as _uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

from .errors import NonDeterminismError, PermanentToolError, RunMismatchError, ToolError
from .journal import FsyncPolicy, Journal, PowerLossJournal
from .records import RecordType, canonical_json, request_hash
from .replay import ReplayIndex

FaultHook = Callable[[str], None]

# Named points where the fault hook fires. The chaos harness kills the process at these.
KILL_POINTS = (
    "before_intent_fsync",  # EFFECT_INTENT written, not yet fsynced
    "after_intent_before_effect",  # intent durable, tool not yet called
    "after_effect_before_result",  # tool returned, EFFECT_RESULT not yet written
    "llm_mid",  # model responded (and billed), LLM_RESULT not yet written
)


class ModelAdapter(Protocol):
    def complete(self, request: dict[str, Any]) -> dict[str, Any]: ...


def idempotency_key(run_id: str, seq: int) -> str:
    """Deterministic key for the effect at `seq`. Every retry of that effect sends the same key."""
    return f"aw:{run_id}:{seq}"


@dataclass
class ToolSpec:
    """How to call a tool.

    * effect tools are called as ``fn(**args, idempotency_key=key)``;
    * read-only tools (``effect=False``) as ``fn(**args)``;
    * ``reconcile(key)`` (effects only, optional) asks the receiver whether the effect
      with this key already happened, returning its result or ``None``.
    """

    fn: Callable[..., Any]
    effect: bool = True
    reconcile: Callable[[str], Any | None] | None = None


@dataclass
class RunStats:
    llm_live: int = 0
    llm_replayed: int = 0
    llm_reissued: int = 0  # LLM_CALL found without LLM_RESULT: paid again
    effects_live: int = 0
    effects_replayed: int = 0
    effects_retried: int = 0  # intent without result, re-invoked with the same key
    effects_reconciled: int = 0  # intent without result, resolved via reconcile(key)
    values_live: int = 0
    values_replayed: int = 0
    reads_live: int = 0
    reads_replayed: int = 0
    recovery_ms: float = 0.0  # journal open + replay until the first live operation
    truncated_bytes: int = 0
    records_recovered: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class DurableContext:
    journal: Journal
    run_id: str
    fault_hook: FaultHook | None = None
    recovery: str = "retry"  # what to do with an intent without a result: "retry" | "reconcile"
    journal_reads: bool = True
    on_caught_up: Callable[[DurableContext], None] | None = None
    stats: RunStats = field(default_factory=RunStats)
    _index: ReplayIndex = field(default_factory=ReplayIndex)
    _seq: int = 0
    _t_open: float = 0.0
    _caught_up: bool = False

    # -- lifecycle ---------------------------------------------------------------------
    @property
    def done(self) -> bool:
        """True if the journal already holds RUN_END (the run finished before)."""
        return self._index.run_end is not None

    @property
    def output(self) -> Any:
        return self._index.run_end.payload["output"] if self._index.run_end else None

    @property
    def replaying(self) -> bool:
        return self._seq < self._index.max_seq

    def complete(self, output: Any) -> Any:
        """Record the run's final output. Replaying a finished run checks it is unchanged."""
        self.mark_caught_up()
        if self._seq < self._index.max_seq:
            nxt = self._index.entries.get(self._seq + 1)
            raise NonDeterminismError(
                self._seq + 1,
                f"{nxt.call.type.name} (journal has {self._index.max_seq - self._seq} more calls)" if nxt else "?",
                "run completed",
                reason="code made fewer calls than the journal recorded",
            )
        if self._index.run_end is not None:
            if canonical_json(self._index.run_end.payload["output"]) != canonical_json(output):
                raise NonDeterminismError(
                    self._seq, str(self._index.run_end.payload["output"])[:200], str(output)[:200],
                    reason="final output differs from the recorded RUN_END",
                )
            return output
        rec = self.journal.append(RecordType.RUN_END, {"output": output}, barrier=True)
        self.journal.sync()
        self._index.run_end = rec
        return output

    # -- helpers -----------------------------------------------------------------------
    def _fault(self, point: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(point)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def mark_caught_up(self) -> None:
        if not self._caught_up:
            self._caught_up = True
            self.stats.recovery_ms = (time.perf_counter() - self._t_open) * 1000
            if self.on_caught_up is not None:
                self.on_caught_up(self)

    # -- LLM ---------------------------------------------------------------------------
    def llm(self, adapter: ModelAdapter) -> JournaledLLM:
        return JournaledLLM(self, adapter)

    def _llm_call(self, adapter: ModelAdapter, request: dict[str, Any]) -> dict[str, Any]:
        seq = self._next_seq()
        h = request_hash({"llm": request})
        preview = canonical_json(request).decode()[:240]
        entry = self._index.match(seq, RecordType.LLM_CALL, h, f"LLM_CALL hash={h} {preview}")
        if entry is not None and entry.result is not None:
            self.stats.llm_replayed += 1
            return entry.result.payload["response"]
        self.mark_caught_up()
        if entry is None:
            self.journal.append(RecordType.LLM_CALL, {"seq": seq, "request_hash": h, "preview": preview})
        else:
            self.stats.llm_reissued += 1  # crashed mid-call last time; this one is paid twice
        response = adapter.complete(request)
        self.stats.llm_live += 1
        self._fault("llm_mid")
        self.journal.append(RecordType.LLM_RESULT, {"seq": seq, "response": response})
        return response

    # -- tools -------------------------------------------------------------------------
    def wrap(self, tools: Mapping[str, ToolSpec | Callable[..., Any]]) -> WrappedTools:
        specs = {name: t if isinstance(t, ToolSpec) else ToolSpec(t) for name, t in tools.items()}
        return WrappedTools(self, specs)

    def _effect(self, name: str, spec: ToolSpec, args: dict[str, Any]) -> Any:
        seq = self._next_seq()
        key = idempotency_key(self.run_id, seq)
        h = request_hash({"tool": name, "args": args})
        entry = self._index.match(seq, RecordType.EFFECT_INTENT, h, f"EFFECT_INTENT {name}({args}) hash={h}")
        if entry is not None and entry.result is not None:
            self.stats.effects_replayed += 1
            return _unwrap(entry.result.payload)
        self.mark_caught_up()
        if entry is None:
            self.journal.append(
                RecordType.EFFECT_INTENT,
                {"seq": seq, "tool": name, "args": args, "idempotency_key": key, "request_hash": h},
                defer=True,
            )
            self._fault("before_intent_fsync")
            self.journal.sync()  # the intent must be durable before the world is touched
        else:
            # Intent without result: the effect may or may not have happened.
            if self.recovery == "reconcile" and spec.reconcile is not None:
                found = spec.reconcile(key)
                if found is not None:
                    self.stats.effects_reconciled += 1
                    self.journal.append(
                        RecordType.EFFECT_RESULT,
                        {"seq": seq, "result": found, "recovered_by": "reconcile"},
                        barrier=True,
                    )
                    return found
            self.stats.effects_retried += 1
        self._fault("after_intent_before_effect")
        try:
            result = spec.fn(**args, idempotency_key=key)
        except PermanentToolError as e:  # definitely not applied: record it so replay is deterministic
            self.journal.append(
                RecordType.EFFECT_RESULT, {"seq": seq, "error": f"{type(e).__name__}: {e}"}, barrier=True
            )
            raise ToolError(f"{name} failed: {e}") from e
        # Any other exception propagates with the intent left open: the effect may or may not
        # have happened (e.g. a connection reset), so a later attempt retries with the same key.
        self.stats.effects_live += 1
        self._fault("after_effect_before_result")
        self.journal.append(
            RecordType.EFFECT_RESULT,
            {"seq": seq, "result": result, "recovered_by": "retry" if entry is not None else None},
            barrier=True,
        )
        return result

    def _read(self, name: str, spec: ToolSpec, args: dict[str, Any]) -> Any:
        if not self.journal_reads:
            self.stats.reads_live += 1
            return spec.fn(**args)
        return self._value(f"read:{name}", lambda: spec.fn(**args), {"tool": name, "args": args}, reads=True)

    # -- journaled non-determinism --------------------------------------------------------
    def _value(self, kind: str, produce: Callable[[], Any], request: Any = None, reads: bool = False) -> Any:
        seq = self._next_seq()
        h = request_hash({"value": kind, "request": request})
        entry = self._index.match(seq, RecordType.VALUE, h, f"VALUE kind={kind} hash={h}")
        if entry is not None:
            if reads:
                self.stats.reads_replayed += 1
            else:
                self.stats.values_replayed += 1
            return entry.call.payload["value"]
        self.mark_caught_up()
        value = produce()
        if reads:
            self.stats.reads_live += 1
        else:
            self.stats.values_live += 1
        self.journal.append(RecordType.VALUE, {"seq": seq, "kind": kind, "value": value, "request_hash": h})
        return value

    def now(self) -> float:
        """Wall-clock time (seconds since the epoch), fixed on first execution."""
        return self._value("now", time.time)

    def random(self) -> float:
        return self._value("random", _random.random)

    def uuid(self) -> str:
        return self._value("uuid", lambda: str(_uuid.uuid4()))


def _unwrap(payload: dict[str, Any]) -> Any:
    if "error" in payload:
        raise ToolError(payload["error"])
    return payload["result"]


class JournaledLLM:
    """Drop-in model adapter whose calls are recorded and replayed."""

    def __init__(self, ctx: DurableContext, adapter: ModelAdapter):
        self._ctx = ctx
        self._adapter = adapter

    def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._ctx._llm_call(self._adapter, request)


class WrappedTools:
    """Tools wrapped with journaling. Call as ``tools["name"](**args)`` or ``tools.name(**args)``."""

    def __init__(self, ctx: DurableContext, specs: dict[str, ToolSpec]):
        self._ctx = ctx
        self._specs = specs

    def __getitem__(self, name: str) -> Callable[..., Any]:
        spec = self._specs[name]
        if spec.effect:
            return lambda **args: self._ctx._effect(name, spec, args)
        return lambda **args: self._ctx._read(name, spec, args)

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def __contains__(self, name: str) -> bool:
        return name in self._specs


@contextmanager
def durable_run(
    path: str,
    run_id: str,
    *,
    input: Any = None,
    code_version: str = "v1",
    fsync: str | FsyncPolicy = "always",
    power_loss: bool = False,
    ack_log: str | None = None,
    recovery: str = "retry",
    journal_reads: bool = True,
    fault_hook: FaultHook | None = None,
    on_caught_up: Callable[[DurableContext], None] | None = None,
) -> Iterator[DurableContext]:
    """Open (or recover) the journal at `path` and yield a DurableContext.

    The first execution writes RUN_START. A later execution checks the journal belongs
    to the same run_id, input and code_version, then replays.
    ``power_loss=True`` swaps in the fault-injecting PowerLossJournal (testing only).
    """
    if recovery not in ("retry", "reconcile"):
        raise ValueError("recovery must be 'retry' or 'reconcile'")
    t0 = time.perf_counter()
    jcls = PowerLossJournal if power_loss else Journal
    journal = jcls(path, fsync=fsync, ack_log=ack_log)
    crashed = True
    try:
        index = ReplayIndex.build(journal.records)
        ihash = request_hash({"input": input})
        if index.run_start is None:
            if journal.records:
                raise RunMismatchError("journal has records but no RUN_START")
            index.run_start = journal.append(
                RecordType.RUN_START,
                {"run_id": run_id, "code_version": code_version, "input_hash": ihash},
                barrier=True,
            )
        else:
            p = index.run_start.payload
            if p["run_id"] != run_id:
                raise RunMismatchError(f"journal is for run {p['run_id']!r}, not {run_id!r}")
            if p["input_hash"] != ihash:
                raise RunMismatchError(f"run {run_id!r} was started with a different input")
            if p["code_version"] != code_version:
                raise NonDeterminismError(
                    0, f"code_version={p['code_version']}", f"code_version={code_version}",
                    reason="code version changed; replaying an old journal with new code is unsafe",
                )
        ctx = DurableContext(
            journal=journal, run_id=run_id, fault_hook=fault_hook, recovery=recovery,
            journal_reads=journal_reads, on_caught_up=on_caught_up, _index=index, _t_open=t0,
        )
        ctx.stats.truncated_bytes = journal.recovery.truncated_bytes
        ctx.stats.records_recovered = journal.recovery.records_recovered
        yield ctx
        crashed = False
    except Exception:
        # An ordinary exception is not a crash: the process lives on and the OS keeps
        # what was written. Only BaseExceptions (e.g. a simulated crash) count as one.
        crashed = False
        raise
    finally:
        journal.close(crashed=crashed)
