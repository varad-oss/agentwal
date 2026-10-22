"""Append-only journal file with CRC-checked records, fsync policies and torn-tail recovery.

Durability vocabulary used throughout:

* **written**  - `os.write` returned. The bytes are in the OS page cache. They survive a
  process crash (`kill -9`) but not an OS crash or power loss.
* **synced**   - `fsync` returned after the write. The bytes survive power loss (modulo
  drive caches; on macOS plain `fsync` does not flush the drive cache, see `full`).

`Journal` exposes `written_count` / `synced_count` so callers and the chaos harness can
tell exactly what was acknowledged at each level.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .records import Record, RecordType, ScanResult, encode, scan


@dataclass(frozen=True)
class FsyncPolicy:
    """When to fsync.

    * ``always`` - fsync after every record.
    * ``group``  - fsync after `group_n` records or once `group_ms` has passed since the
      last fsync (checked when a record is appended; there is no background flusher).
    * ``none``   - never fsync; durability is left to the OS.

    Independently of the mode, callers can pass ``barrier=True`` to `append`. Under
    ``always`` and ``group`` a barrier forces an fsync before `append` returns; the
    durable context uses it for effect intents and results, because a record must be
    on disk *before* the external world is touched. Under ``none`` barriers are ignored:
    that is the point of measuring ``none``.
    """

    mode: str = "always"
    group_n: int = 8
    group_ms: float = 5.0
    full: bool = False  # use F_FULLFSYNC on macOS (flushes the drive's write cache too)

    def __post_init__(self) -> None:
        if self.mode not in ("always", "group", "none"):
            raise ValueError(f"unknown fsync mode {self.mode!r}")

    @classmethod
    def parse(cls, spec: str | FsyncPolicy) -> FsyncPolicy:
        """Parse ``always``, ``none``, ``group`` or ``group:N:MS``; append ``+full`` for F_FULLFSYNC."""
        if isinstance(spec, FsyncPolicy):
            return spec
        full = spec.endswith("+full")
        spec = spec.removesuffix("+full")
        parts = spec.split(":")
        if parts[0] == "group" and len(parts) == 3:
            return cls("group", int(parts[1]), float(parts[2]), full)
        return cls(parts[0], full=full)

    def __str__(self) -> str:
        s = f"group:{self.group_n}:{self.group_ms:g}" if self.mode == "group" else self.mode
        return s + ("+full" if self.full else "")


def _fsync(fd: int, full: bool) -> None:
    if full and sys.platform == "darwin":
        import fcntl

        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    else:
        os.fsync(fd)


def _fsync_dir(path: Path) -> None:
    try:
        dfd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def read_records(path: str | os.PathLike) -> ScanResult:
    """Read-only scan of a journal file (never truncates). Missing file = empty journal."""
    try:
        buf = Path(path).read_bytes()
    except FileNotFoundError:
        buf = b""
    return scan(buf)


@dataclass
class RecoveryInfo:
    records_recovered: int
    truncated_bytes: int
    stop_reason: str
    scan_ms: float


class Journal:
    """A single-segment append-only journal.

    Opening a journal performs recovery: every record is CRC-checked from the start and a
    torn or corrupt tail is truncated at the last valid record (and the truncation is
    fsynced), so new appends never follow garbage.

    `ack_log` (optional) is a sidecar file used by the chaos harness: after each record is
    acknowledged at the journal's acknowledgement level, a line ``"<count> <bytes>\\n"`` is
    appended with a raw `os.write`. It lets the harness check invariant 3 ("every
    acknowledged record survives recovery") from outside the process.
    """

    def __init__(
        self,
        path: str | os.PathLike,
        fsync: str | FsyncPolicy = "always",
        ack_log: str | os.PathLike | None = None,
    ):
        self.path = Path(path)
        self.policy = FsyncPolicy.parse(fsync)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()
        existed = self.path.exists()
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        if not existed:
            _fsync_dir(self.path)
        buf = self._read_all()
        res = scan(buf)
        if res.torn_bytes:
            os.ftruncate(self._fd, res.valid_length)
            _fsync(self._fd, self.policy.full)
        os.lseek(self._fd, res.valid_length, os.SEEK_SET)
        self.records: list[Record] = list(res.records)
        self.recovery = RecoveryInfo(
            len(res.records), res.torn_bytes, res.stop_reason, (time.perf_counter() - t0) * 1000
        )
        self._size = res.valid_length  # bytes written (may be ahead of what is on disk)
        self.written_count = len(self.records)
        self.synced_count = len(self.records)  # everything recovered is on disk by definition
        self._synced_size = self._size
        self._pending = 0  # records written since the last fsync
        self._last_sync = time.monotonic()
        self.fsync_calls = 0
        self.fsync_seconds = 0.0
        self._ack_fd = (
            os.open(ack_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644) if ack_log else None
        )
        self._closed = False

    # -- low-level I/O, overridden by the power-loss simulator ------------------------
    def _read_all(self) -> bytes:
        os.lseek(self._fd, 0, os.SEEK_SET)
        chunks = []
        while True:
            b = os.read(self._fd, 1 << 20)
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks)

    def _write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            n = os.write(self._fd, view)
            view = view[n:]

    def _do_fsync(self) -> None:
        _fsync(self._fd, self.policy.full)

    # -- acknowledgement level ---------------------------------------------------------
    @property
    def ack_count(self) -> int:
        """Records acknowledged to the caller. For a real journal: written (survives kill -9)."""
        return self.written_count

    def _emit_ack(self) -> None:
        if self._ack_fd is not None:
            os.write(self._ack_fd, f"{self.ack_count} {self._ack_size()}\n".encode())

    def _ack_size(self) -> int:
        return self._size

    # -- public API --------------------------------------------------------------------
    def append(
        self, rtype: RecordType, payload: dict[str, Any], *, barrier: bool = False, defer: bool = False
    ) -> Record:
        """Write one record and fsync it according to the policy.

        ``barrier=True`` forces an fsync (unless the policy is ``none``). ``defer=True``
        skips the policy entirely; the caller promises to call `sync()` itself (used to
        place a fault-injection point between the write and the fsync).
        """
        data = encode(rtype, payload)
        rec = Record(rtype, payload, offset=self._size, size=len(data))
        self._write(data)
        self._size += len(data)
        self.records.append(rec)
        self.written_count += 1
        self._pending += 1
        mode = self.policy.mode
        if defer:
            self._emit_ack()
        elif mode == "always" or (mode == "group" and barrier):
            self.sync()
        elif mode == "group":
            if self._pending >= self.policy.group_n or (
                (time.monotonic() - self._last_sync) * 1000 >= self.policy.group_ms
            ):
                self.sync()
            else:
                self._emit_ack()
        else:
            self._emit_ack()
        return rec

    def sync(self) -> None:
        """Make every written record durable (no-op under the ``none`` policy)."""
        if self.policy.mode == "none":
            return
        if self._pending:
            t0 = time.perf_counter()
            self._do_fsync()
            self.fsync_seconds += time.perf_counter() - t0
            self.fsync_calls += 1
            self._pending = 0
        self._last_sync = time.monotonic()
        self.synced_count = self.written_count
        self._synced_size = self._size
        self._emit_ack()

    @property
    def size(self) -> int:
        return self._size

    def close(self, crashed: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        os.close(self._fd)
        if self._ack_fd is not None:
            os.close(self._ack_fd)

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, exc_type, *_exc) -> None:
        self.close(crashed=exc_type is not None)


class PowerLossJournal(Journal):
    """Fault-injecting journal that models an OS crash / power loss.

    Records are held in a user-space buffer and reach the file only when the journal
    fsyncs. If the process is killed (or `close(crashed=True)` is called) the buffer is
    simply gone, exactly like un-fsynced bytes in the page cache when the power fails.
    So `kill -9` of a process using this class has power-loss semantics, which lets the
    chaos harness test what each fsync policy actually protects.

    Under the ``none`` policy nothing is ever "fsynced", so nothing reaches the file
    until a clean `close()`.
    """

    def __init__(self, *args: Any, **kwargs: Any):
        self._buf = bytearray()
        super().__init__(*args, **kwargs)

    @property
    def ack_count(self) -> int:
        return self.synced_count  # only synced records count as acknowledged here

    def _ack_size(self) -> int:
        return self._synced_size

    def _write(self, data: bytes) -> None:
        self._buf += data

    def _do_fsync(self) -> None:
        if self._buf:
            Journal._write(self, bytes(self._buf))
            self._buf.clear()
        super()._do_fsync()

    def close(self, crashed: bool = False) -> None:
        if not self._closed and not crashed and self._buf:
            # A clean exit: the OS would eventually write the page cache out.
            Journal._write(self, bytes(self._buf))
            self._buf.clear()
        super().close(crashed)
