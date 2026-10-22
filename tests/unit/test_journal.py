import os

from agentwal.journal import FsyncPolicy, Journal, PowerLossJournal, read_records
from agentwal.records import RecordType


def _fill(j, n, **kw):
    for i in range(n):
        j.append(RecordType.VALUE, {"seq": i + 1, "kind": "x", "value": i}, **kw)


def test_reopen_recovers_all_records(tmp_path):
    p = tmp_path / "a.wal"
    with Journal(p) as j:
        _fill(j, 5)
    j2 = Journal(p)
    assert len(j2.records) == 5 and j2.recovery.truncated_bytes == 0
    j2.close()


def test_torn_tail_is_truncated_and_appends_continue(tmp_path):
    p = tmp_path / "a.wal"
    with Journal(p) as j:
        _fill(j, 3)
        last = j.records[-1]
    with open(p, "r+b") as f:
        f.truncate(last.offset + 5)  # tear the last record
    j = Journal(p)
    assert len(j.records) == 2
    assert j.recovery.truncated_bytes == 5 and j.recovery.stop_reason == "short_header"
    assert os.path.getsize(p) == last.offset  # truncation happened on disk
    j.append(RecordType.VALUE, {"seq": 3, "kind": "x", "value": 99})
    j.close()
    recs = read_records(p).records
    assert [r.payload["value"] for r in recs] == [0, 1, 99]


def test_garbage_tail_is_truncated(tmp_path):
    p = tmp_path / "a.wal"
    with Journal(p) as j:
        _fill(j, 2)
    with open(p, "ab") as f:
        f.write(os.urandom(40))
    with Journal(p) as j:
        assert len(j.records) == 2 and j.recovery.truncated_bytes == 40


def test_fsync_always_syncs_every_record(tmp_path):
    with Journal(tmp_path / "a.wal", "always") as j:
        _fill(j, 4)
        assert j.fsync_calls == 4 and j.synced_count == 4


def test_fsync_none_never_syncs_even_on_barrier(tmp_path):
    with Journal(tmp_path / "a.wal", "none") as j:
        _fill(j, 4, barrier=True)
        j.sync()
        assert j.fsync_calls == 0 and j.synced_count == 0 and j.written_count == 4


def test_group_commit_syncs_every_n_and_on_barrier(tmp_path):
    with Journal(tmp_path / "a.wal", FsyncPolicy("group", group_n=3, group_ms=1e9)) as j:
        _fill(j, 7)
        assert j.fsync_calls == 2 and j.synced_count == 6
        j.append(RecordType.VALUE, {"seq": 8}, barrier=True)
        assert j.fsync_calls == 3 and j.synced_count == 8


def test_group_commit_time_trigger(tmp_path):
    with Journal(tmp_path / "a.wal", FsyncPolicy("group", group_n=1000, group_ms=0.0)) as j:
        _fill(j, 3)
        assert j.fsync_calls == 3


def test_defer_skips_policy_until_sync(tmp_path):
    with Journal(tmp_path / "a.wal", "always") as j:
        j.append(RecordType.VALUE, {"seq": 1}, defer=True)
        assert j.fsync_calls == 0
        j.sync()
        assert j.fsync_calls == 1


def test_policy_parse():
    assert FsyncPolicy.parse("group:4:2") == FsyncPolicy("group", 4, 2.0)
    assert FsyncPolicy.parse("always+full").full
    assert str(FsyncPolicy.parse("group:4:2")) == "group:4:2"


def test_power_loss_journal_drops_unsynced_on_crash(tmp_path):
    p = tmp_path / "a.wal"
    j = PowerLossJournal(p, FsyncPolicy("group", group_n=3, group_ms=1e9))
    _fill(j, 5)  # 3 synced, 2 buffered
    j.close(crashed=True)
    assert [r.payload["seq"] for r in read_records(p).records] == [1, 2, 3]


def test_power_loss_journal_none_policy_loses_everything_on_crash(tmp_path):
    p = tmp_path / "a.wal"
    j = PowerLossJournal(p, "none")
    _fill(j, 5, barrier=True)
    j.close(crashed=True)
    assert read_records(p).records == []


def test_power_loss_journal_clean_close_writes_buffer(tmp_path):
    p = tmp_path / "a.wal"
    j = PowerLossJournal(p, "none")
    _fill(j, 2)
    j.close()
    assert len(read_records(p).records) == 2


def test_ack_log_tracks_acknowledged_records(tmp_path):
    p, ack = tmp_path / "a.wal", tmp_path / "a.ack"
    j = PowerLossJournal(p, FsyncPolicy("group", group_n=2, group_ms=1e9), ack_log=ack)
    _fill(j, 3)
    j.close(crashed=True)
    last = ack.read_text().splitlines()[-1].split()
    assert int(last[0]) == 2  # only the two synced records were acknowledged
    assert len(read_records(p).records) >= int(last[0])
    assert os.path.getsize(p) == int(last[1])
