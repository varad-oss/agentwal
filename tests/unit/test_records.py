import struct

import pytest

from agentwal.records import HEADER_SIZE, RecordType, encode, request_hash, scan


def test_roundtrip():
    payloads = [(RecordType.RUN_START, {"run_id": "r", "code_version": "v1", "input_hash": "x"}),
                (RecordType.LLM_CALL, {"seq": 1, "request_hash": "ab", "preview": "héllo"}),
                (RecordType.RUN_END, {"output": [1, 2, {"a": None}]})]
    buf = b"".join(encode(t, p) for t, p in payloads)
    res = scan(buf)
    assert res.stop_reason == "eof" and res.torn_bytes == 0
    assert [(r.type, r.payload) for r in res.records] == payloads
    assert res.records[1].offset == len(encode(*payloads[0]))


def test_layout_is_length_crc_type_payload():
    data = encode(RecordType.VALUE, {"seq": 3})
    length, _crc, t = struct.unpack_from("<IIB", data)
    assert t == RecordType.VALUE and length == len(data) - HEADER_SIZE


@pytest.mark.parametrize("pos", [0, 4, 8, 9, 12])
def test_crc_detects_any_flipped_byte(pos):
    good = encode(RecordType.VALUE, {"seq": 1, "kind": "now", "value": 1.5})
    bad = bytearray(good)
    bad[pos] ^= 0x01
    res = scan(encode(RecordType.RUN_START, {"run_id": "r"}) + bytes(bad))
    assert len(res.records) == 1
    assert res.stop_reason in {"bad_crc", "short_payload", "bad_length", "bad_type"}


def test_short_header_and_short_payload():
    rec = encode(RecordType.VALUE, {"seq": 1})
    assert scan(rec[:5]).stop_reason == "short_header"
    assert scan(rec[:-1]).stop_reason == "short_payload"
    assert scan(rec[:-1]).valid_length == 0


def test_request_hash_is_order_independent_for_dict_keys():
    assert request_hash({"a": 1, "b": 2}) == request_hash({"b": 2, "a": 1})
    assert request_hash({"a": 1}) != request_hash({"a": 2})
