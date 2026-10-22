"""Replay index: what the journal says happened at each sequence number."""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import NonDeterminismError, RunMismatchError
from .records import Record, RecordType

# Record types that open a call at a sequence number, and the type that completes each.
CALL_TYPES = {RecordType.LLM_CALL, RecordType.EFFECT_INTENT, RecordType.VALUE}
RESULT_FOR = {RecordType.LLM_CALL: RecordType.LLM_RESULT, RecordType.EFFECT_INTENT: RecordType.EFFECT_RESULT}


@dataclass
class Entry:
    call: Record
    result: Record | None = None

    @property
    def complete(self) -> bool:
        return self.call.type == RecordType.VALUE or self.result is not None


@dataclass
class ReplayIndex:
    run_start: Record | None = None
    run_end: Record | None = None
    entries: dict[int, Entry] = field(default_factory=dict)

    @property
    def max_seq(self) -> int:
        return max(self.entries, default=0)

    @classmethod
    def build(cls, records: list[Record]) -> ReplayIndex:
        idx = cls()
        for rec in records:
            t = rec.type
            if t == RecordType.RUN_START:
                if idx.run_start is not None:
                    raise RunMismatchError("journal contains two RUN_START records")
                idx.run_start = rec
            elif t == RecordType.RUN_END:
                idx.run_end = rec
            elif t in CALL_TYPES:
                seq = rec.payload["seq"]
                if seq in idx.entries:
                    raise RunMismatchError(f"journal has two calls at seq={seq}")
                idx.entries[seq] = Entry(rec)
            else:  # a result record
                seq = rec.payload["seq"]
                entry = idx.entries.get(seq)
                if entry is None or RESULT_FOR.get(entry.call.type) != t:
                    raise RunMismatchError(f"{t.name} at seq={seq} has no matching call record")
                entry.result = rec
        return idx

    def match(self, seq: int, rtype: RecordType, rhash: str, describe: str) -> Entry | None:
        """Return the journal entry for `seq` if there is one, after checking it is the same call.

        Raises NonDeterminismError if the journal recorded a different kind of call or a
        different request at this position.
        """
        entry = self.entries.get(seq)
        if entry is None:
            return None
        rec = entry.call
        if rec.type != rtype:
            raise NonDeterminismError(
                seq, _describe(rec), describe, reason=f"expected {rec.type.name}, code issued {rtype.name}"
            )
        if rec.payload.get("request_hash") != rhash:
            raise NonDeterminismError(seq, _describe(rec), describe)
        return entry


def _describe(rec: Record) -> str:
    p = rec.payload
    if rec.type == RecordType.LLM_CALL:
        return f"LLM_CALL hash={p.get('request_hash')} {p.get('preview', '')}"
    if rec.type == RecordType.EFFECT_INTENT:
        return f"EFFECT_INTENT {p.get('tool')}({p.get('args')}) hash={p.get('request_hash')}"
    if rec.type == RecordType.VALUE:
        return f"VALUE kind={p.get('kind')} hash={p.get('request_hash')}"
    return f"{rec.type.name} {p}"
