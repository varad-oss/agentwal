"""Print a journal in human-readable form: python -m agentwal.dump runs/demo/journal.wal"""

from __future__ import annotations

import json
import sys

from .journal import read_records


def describe(rec) -> str:
    p = dict(rec.payload)
    if "preview" in p:
        p["preview"] = p["preview"][:60] + "..."
    if "response" in p:
        p["response"] = {"text": p["response"].get("text", "")[:70]}
    return f"{rec.offset:>7}  {rec.type.name:<14} {json.dumps(p)[:150]}"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__)
        return 2
    res = read_records(argv[0])
    print(f" offset  type           payload   ({len(res.records)} valid records, {res.valid_length} bytes)")
    for rec in res.records:
        print(describe(rec))
    if res.torn_bytes:
        print(f"   ...   {res.torn_bytes} torn bytes after the last valid record ({res.stop_reason})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
