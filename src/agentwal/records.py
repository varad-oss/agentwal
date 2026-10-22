"""On-disk record format.

Every record is::

    [length u32 LE][crc32 u32 LE][type u8][payload: `length` bytes of canonical JSON]

`length` counts only the payload. The CRC covers the type byte and the payload, so a
flipped type byte is caught too. A record is valid only if the full header and payload
are present and the CRC matches; the first invalid record marks the end of the log
(see `scan`).
"""

from __future__ import annotations

import enum
import hashlib
import json
import struct
import zlib
from dataclasses import dataclass, field
from typing import Any

HEADER = struct.Struct("<IIB")  # length, crc32, type
HEADER_SIZE = HEADER.size  # 9 bytes
MAX_PAYLOAD = 16 * 1024 * 1024  # a length above this is treated as garbage, not a record


class RecordType(enum.IntEnum):
    RUN_START = 1  # run_id, code_version, input_hash
    LLM_CALL = 2  # seq, request_hash, preview
    LLM_RESULT = 3  # seq, response
    EFFECT_INTENT = 4  # seq, tool, args, idempotency_key, request_hash
    EFFECT_RESULT = 5  # seq, result | error, recovered_by
    VALUE = 6  # seq, kind, value (+ request_hash for journaled read-only tools)
    RUN_END = 7  # output


@dataclass(frozen=True)
class Record:
    type: RecordType
    payload: dict[str, Any]
    offset: int = field(default=-1, compare=False)  # byte offset in the file, -1 if not on disk
    size: int = field(default=0, compare=False)  # encoded size in bytes

    @property
    def seq(self) -> int | None:
        return self.payload.get("seq")


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def request_hash(obj: Any) -> str:
    """Stable 64-bit hash of a JSON-able request, used to detect non-determinism."""
    return hashlib.sha256(canonical_json(obj)).hexdigest()[:16]


def encode(rtype: RecordType, payload: dict[str, Any]) -> bytes:
    body = canonical_json(payload)
    if len(body) > MAX_PAYLOAD:
        raise ValueError(f"record payload too large: {len(body)} bytes")
    tbyte = bytes([int(rtype)])
    crc = zlib.crc32(tbyte + body) & 0xFFFFFFFF
    return HEADER.pack(len(body), crc, int(rtype)) + body


@dataclass
class ScanResult:
    records: list[Record]
    valid_length: int  # bytes up to and including the last valid record
    total_length: int  # bytes in the buffer
    stop_reason: str  # "eof" | "short_header" | "short_payload" | "bad_length" | "bad_crc" | "bad_type" | "bad_json"

    @property
    def torn_bytes(self) -> int:
        return self.total_length - self.valid_length


def scan(buf: bytes) -> ScanResult:
    """Decode records from the start of `buf` until the first invalid one.

    Never raises on bad data: whatever follows the last valid record is reported as
    torn so the caller can truncate it. This mirrors how most WALs treat corruption:
    a record is trusted only if every record before it was valid.
    """
    records: list[Record] = []
    pos = 0
    n = len(buf)
    reason = "eof"
    while pos < n:
        if n - pos < HEADER_SIZE:
            reason = "short_header"
            break
        length, crc, tval = HEADER.unpack_from(buf, pos)
        if length > MAX_PAYLOAD:
            reason = "bad_length"
            break
        end = pos + HEADER_SIZE + length
        if end > n:
            reason = "short_payload"
            break
        body = buf[pos + HEADER_SIZE : end]
        if zlib.crc32(bytes([tval]) + body) & 0xFFFFFFFF != crc:
            reason = "bad_crc"
            break
        try:
            rtype = RecordType(tval)
        except ValueError:
            reason = "bad_type"
            break
        try:
            payload = json.loads(body)
        except ValueError:
            reason = "bad_json"
            break
        records.append(Record(rtype, payload, offset=pos, size=end - pos))
        pos = end
    return ScanResult(records, pos, n, reason)
