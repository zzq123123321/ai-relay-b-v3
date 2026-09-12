"""T07：TaskStore 持久认领、去重冲突、FIFO、配额、并发与坏记录测试。

对应验收关联：
- P04：同 ID 同内容重复 100 次并发 → 数据库仅 1 行、1 个 sequence；
- P05：同 ID 不同正文/WORKDIR/扩展头 → CONFLICT，旧数据逐字段不变；
- P08：消息超限/队列满 → 不落数据、不谎称已入队；
- Q01：T1 运行中接收 T2/T3 后重启 → 按 sequence 保留、顺序恢复；
- Q02：队首记录损坏 → 可诊断、不静默删除、健康记录不被永久遮挡。
并发采用多个独立 Database 实例指向同一 SQLite 文件制造真实竞争；
生产 single-writer 合同不变。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from core.domain import ReceiveSettingsSnapshot, SessionBindingMode, TargetExecutor
from core.protocol_v1 import MessageType, ProtocolFormat, content_digest, parse_message
from storage.database import Database
from storage.task_store import (
    ClaimOutcome,
    TaskStore,
    TaskStoreError,
    make_task_key,
)


def make_receive_snapshot(**kw) -> ReceiveSettingsSnapshot:
    return ReceiveSettingsSnapshot(
        config_revision=kw.get("config_revision", 5),
        committed_at=kw.get("committed_at", "2026-09-12T00:00:00+00:00"),
        received_at=kw.get("received_at", "2026-09-12T00:00:00+00:00"),
        effective_executor=kw.get("effective_executor", TargetExecutor.OPENCHAMBER),
        directory=kw.get("directory", r"D:\AIwork\proj"),
        project_key=kw.get("project_key", "proj"),
        agent=kw.get("agent", "build"),
        requested_model=kw.get("requested_model", ""),
        binding_mode=kw.get("binding_mode", SessionBindingMode.PROJECT_ROTATING),
    )


def v1_raw(task_id: str, body: str) -> str:
    return (
        "AI_RELAY/1\n"
        f"MESSAGE_ID: {task_id}\n"
        "SOURCE: CHATGPT\n"
        "TARGET: OPENCHAMBER\n"
        "TYPE: TASK\n"
        "\n"
        f"{body}"
    )


def _claim(
    store: TaskStore,
    task_id: str,
    body: str,
    *,
    peer_id: str = "CHATGPT",
    at: str = "2026-09-12T00:00:00+00:00",
):
    raw = v1_raw(task_id, body)
    msg = parse_message(raw)
    return store.claim(
        task_key=make_task_key(peer_id, task_id),
        peer_id=peer_id,
        task_id=task_id,
        protocol_format=ProtocolFormat.V1.value.upper(),
        raw_message=raw,
        body=msg.body,
        canonical_hash=content_digest(msg),
        receive_snapshot=make_receive_snapshot(),
        received_at=at,
    )


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "db.sqlite")
    database.open()
    yield database
    database.close()


@pytest.fixture
def store(db):
    return TaskStore(db, queue_capacity=1000)


class TestClaimBasics:
    def test_new_task_accepted_sequence_one(self, db, store):
        result = _claim(store, "task-1", "修复甲")
        assert result.outcome is ClaimOutcome.ACCEPTED
        assert result.sequence == 1
        assert result.state == "QUEUED"
        row = db.connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE task_key=?", (result.task_key,)
        ).fetchone()
        assert row[0] == 1

    def test_sequences_monotonic(self, db, store):
        s1 = _claim(store, "a", "body")
        s2 = _claim(store, "b", "body")
        s3 = _claim(store, "c", "body")
        assert [s1.sequence, s2.sequence, s3.sequence] == [1, 2, 3]

    def test_claim_persists_snapshot_json(self, db, store):
        import json

        result = _claim(store, "a", "body")
        rec = store.peek_next()
        assert rec is not None
        snap = json.loads(rec.ingress_snapshot_json)
        assert snap["config_revision"] == 5
        assert snap["project_key"] == "proj"
        assert result.state == rec.state == "QUEUED"


class TestDedupConflict:
    def test_same_id_same_hash_returns_existing(self, db, store):
        first = _claim(store, "t-1", "修复甲")
        second = _claim(store, "t-1", "修复甲")
        assert first.outcome is ClaimOutcome.ACCEPTED
        assert second.outcome is ClaimOutcome.EXISTING
        assert second.sequence == first.sequence == 1
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        rec = store.peek_next()
        assert rec is not None
        assert rec.body == "修复甲"
        assert rec.sequence == 1

    def test_same_id_diff_hash_conflict_keeps_old(self, db, store):
        first = _claim(store, "t-1", "修改 A")
        before = store.peek_next()
        assert before is not None
        first_hash = before.canonical_hash

        conflict = _claim(store, "t-1", "删除 B")
        assert conflict.outcome is ClaimOutcome.CONFLICT
        after = store.peek_next()
        assert after is not None
        assert after.canonical_hash == first_hash
        assert after.body == "修改 A"
        assert after.sequence == before.sequence
        assert after.state == before.state
        assert after.received_at == before.received_at
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1

    def test_duplicate_at_full_queue_returns_existing(self, tmp_path):
        db_full = Database(tmp_path / "db.sqlite")
        db_full.open()
        try:
            s = TaskStore(db_full, queue_capacity=1)
            first = _claim(s, "t-1", "修复甲")
            assert first.outcome is ClaimOutcome.ACCEPTED
            dup = _claim(s, "t-1", "修复甲")
            assert dup.outcome is ClaimOutcome.EXISTING
            other = _claim(s, "t-2", "修复乙")
            assert other.outcome is ClaimOutcome.QUEUE_FULL
            assert db_full.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        finally:
            db_full.close()


class TestQuota:
    def test_queue_full_rejects_new_without_data(self, tmp_path):
        db_full = Database(tmp_path / "db.sqlite")
        db_full.open()
        try:
            s = TaskStore(db_full, queue_capacity=2)
            assert _claim(s, "a", "1").outcome is ClaimOutcome.ACCEPTED
            assert _claim(s, "b", "2").outcome is ClaimOutcome.ACCEPTED
            full = _claim(s, "c", "3")
            assert full.outcome is ClaimOutcome.QUEUE_FULL
            assert full.sequence is None
            assert db_full.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
        finally:
            db_full.close()

    def test_terminal_state_releases_slot_and_no_ghost(self, tmp_path):
        from datetime import datetime, timezone

        db_q = Database(tmp_path / "db.sqlite")
        db_q.open()
        try:
            s = TaskStore(db_q, queue_capacity=1)
            _claim(s, "a", "1")
            assert _claim(s, "b", "2").outcome is ClaimOutcome.QUEUE_FULL
            db_q.connection.execute(
                "UPDATE tasks SET state='COMPLETED' WHERE task_id='a'"
            )
            done = _claim(s, "b", "2")
            assert done.outcome is ClaimOutcome.ACCEPTED
            assert done.sequence == 2
            rows = db_q.connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE state IN ('QUEUED','ACTIVE','BLOCKED')"
            ).fetchone()
            assert rows[0] == 1
        finally:
            db_q.close()

    def test_occupancy_counts_active_and_blocked(self, tmp_path):
        db_q = Database(tmp_path / "db.sqlite")
        db_q.open()
        try:
            s = TaskStore(db_q, queue_capacity=2)
            _claim(s, "a", "1")
            _claim(s, "b", "2")
            assert _claim(s, "c", "3").outcome is ClaimOutcome.QUEUE_FULL
            for st in ("ACTIVE", "BLOCKED"):
                db_q.connection.execute(
                    f"UPDATE tasks SET state='{st}' WHERE task_id='a'"
                )
                assert _claim(s, "c", "3").outcome is ClaimOutcome.QUEUE_FULL
        finally:
            db_q.close()

    def test_queue_capacity_must_be_positive(self, db):
        with pytest.raises(TaskStoreError):
            TaskStore(db, queue_capacity=0)


class TestFifo:
    def test_list_ordered_by_sequence(self, db, store):
        _claim(store, "a", "1")
        _claim(store, "b", "2")
        _claim(store, "c", "3")
        queued = store.list_queued()
        assert [r.task_id for r in queued] == ["a", "b", "c"]
        assert [r.sequence for r in queued] == [1, 2, 3]

    def test_peek_next_returns_earliest(self, db, store):
        _claim(store, "a", "1")
        _claim(store, "b", "2")
        peek = store.peek_next()
        assert peek is not None
        assert peek.task_id == "a"
        assert peek.sequence == 1

    def test_peek_next_skips_terminal(self, db, store):
        _claim(store, "a", "1")
        _claim(store, "b", "2")
        db.connection.execute("UPDATE tasks SET state='COMPLETED' WHERE task_id='a'")
        peek = store.peek_next()
        assert peek is not None
        assert peek.task_id == "b"
        assert peek.sequence == 2

    def test_peek_next_empty(self, db, store):
        assert store.peek_next() is None
        assert store.list_queued() == []


class TestRestartRecovery:
    def test_fifo_restored_and_sequence_continues(self, tmp_path):
        path = tmp_path / "restart.sqlite"
        db1 = Database(path)
        db1.open()
        store1 = TaskStore(db1, queue_capacity=1000)
        _claim(store1, "a", "1")
        _claim(store1, "b", "2")
        _claim(store1, "c", "3")
        db1.close()

        db2 = Database(path)
        db2.open()
        try:
            store2 = TaskStore(db2)
            queued = store2.list_queued()
            assert [r.task_id for r in queued] == ["a", "b", "c"]
            assert [r.sequence for r in queued] == [1, 2, 3]
            d = _claim(store2, "d", "4")
            assert d.outcome is ClaimOutcome.ACCEPTED
            assert d.sequence == 4
        finally:
            db2.close()


class TestTransactionFailure:
    @pytest.mark.parametrize("completed", [1, 2, 3, 4, 5])
    def test_fault_at_every_statement_rolls_back(self, tmp_path, completed):
        db_f = Database(tmp_path / "db.sqlite")
        db_f.open()
        try:
            s = TaskStore(db_f, queue_capacity=1000)
            s.fault_inject_after = completed
            with pytest.raises(sqlite3.IntegrityError):
                _claim(s, "a", "正文")
            assert db_f.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
            assert db_f.connection.execute(
                "SELECT value FROM meta WHERE key='next_sequence'"
            ).fetchone()[0] == "1"
            s.fault_inject_after = None
            ok = _claim(s, "a", "正文")
            assert ok.outcome is ClaimOutcome.ACCEPTED
            assert ok.sequence == 1
        finally:
            db_f.close()

    def test_missing_sequence_meta_raises_storage_error(self, db, store):
        db.connection.execute("DELETE FROM meta WHERE key='next_sequence'")
        with pytest.raises(TaskStoreError):
            _claim(store, "a", "正文")


class TestCorruptRecords:
    def _corrupt(self, db, task_id: str, column: str, value: str) -> None:
        db.connection.execute(f"UPDATE tasks SET {column}=? WHERE task_id=?", (value, task_id))

    def test_corrupt_head_diagnosable_and_not_deleted(self, db, store):
        _claim(store, "a", "修复甲")
        _claim(store, "b", "修复乙")
        self._corrupt(db, "a", "canonical_hash", "zzz")

        queued = store.list_queued()
        assert len(queued) == 2  # 不静默删除
        rec_a = next(r for r in queued if r.task_id == "a")
        rec_b = next(r for r in queued if r.task_id == "b")
        assert rec_a.corrupt_reasons
        assert not rec_b.corrupt_reasons
        assert any("canonical_hash" in reason for reason in rec_a.corrupt_reasons)

        peek = store.peek_next()
        assert peek is not None
        assert peek.task_id == "a"  # 队首仍可诊断读取
        assert peek.corrupt_reasons

    def test_corrupt_head_does_not_hide_healthy_followers(self, db, store):
        _claim(store, "a", "修复甲")
        _claim(store, "b", "修复乙")
        _claim(store, "c", "修复丙")
        raw_a = v1_raw("a", "修复甲")
        tampered_raw = raw_a.replace("修复甲", "被人篡改")
        self._corrupt(db, "a", "raw_message", tampered_raw)
        queued = store.list_queued()
        assert [r.task_id for r in queued] == ["a", "b", "c"]
        assert any(r.corrupt_reasons for r in queued if r.task_id == "a")
        assert all(not r.corrupt_reasons for r in queued if r.task_id in ("b", "c"))

    def test_bad_state_and_empty_received_at_flag(self, db, store):
        _claim(store, "a", "修复甲")
        rec_ok = store.peek_next()
        assert rec_ok is not None
        assert not rec_ok.corrupt_reasons
        self._corrupt(db, "a", "received_at", "")
        rec_bad = store.peek_next()
        assert rec_bad is not None
        assert any("received_at" in r for r in rec_bad.corrupt_reasons)


class TestConcurrentClaims:
    def test_p04_concurrent_100_unique_ids(self, tmp_path):
        path = tmp_path / "concurrent.sqlite"
        bootstrap = Database(path)
        bootstrap.open()
        bootstrap.close()

        n_tasks = 100
        workers = 4
        barrier = threading.Barrier(workers)
        results: list = []
        lock = threading.Lock()

        def worker(idx: int) -> None:
            own_db = Database(path)
            own_db.open()
            try:
                s = TaskStore(own_db, queue_capacity=1000)
                barrier.wait()
                for k in range(idx, n_tasks, workers):
                    res = _claim(s, f"uniq-{k:03d}", "正文", at="2026-09-12T00:00:00+00:00")
                    with lock:
                        results.append(res)
            finally:
                own_db.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == n_tasks
        accepted = [r for r in results if r.outcome is ClaimOutcome.ACCEPTED]
        assert len(accepted) == n_tasks
        sequences = sorted(r.sequence for r in accepted)
        assert sequences == list(range(1, n_tasks + 1))
        task_ids = {r.task_id for r in accepted}
        assert len(task_ids) == n_tasks

        check = Database(path)
        check.open()
        try:
            s = TaskStore(check)
            queued = s.list_queued()
            assert len(queued) == n_tasks
            assert [r.sequence for r in queued] == list(range(1, n_tasks + 1))
            assert all(not r.corrupt_reasons for r in queued)
        finally:
            check.close()

    def test_p04_concurrent_100_same_task_dedup(self, tmp_path):
        path = tmp_path / "same.sqlite"
        bootstrap = Database(path)
        bootstrap.open()
        bootstrap.close()

        n_claims = 100
        workers = 4
        barrier = threading.Barrier(workers)
        results: list = []
        lock = threading.Lock()

        def worker(idx: int) -> None:
            own_db = Database(path)
            own_db.open()
            try:
                s = TaskStore(own_db, queue_capacity=1000)
                barrier.wait()
                for k in range(idx, n_claims, workers):
                    res = _claim(s, "same-1", "修复甲", at="2026-09-12T00:00:00+00:00")
                    with lock:
                        results.append(res)
            finally:
                own_db.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == n_claims
        accepted = [r for r in results if r.outcome is ClaimOutcome.ACCEPTED]
        existing = [r for r in results if r.outcome is ClaimOutcome.EXISTING]
        assert len(accepted) == 1
        assert len(existing) == n_claims - 1
        seqs = {r.sequence for r in accepted}
        assert seqs == {1}

        check = Database(path)
        check.open()
        try:
            s = TaskStore(check)
            assert len(s.list_queued()) == 1
            assert s.peek_next() is not None
            assert s.peek_next().sequence == 1
        finally:
            check.close()


class TestIdScheme:
    def test_task_key_escapes_separator_collision(self):
        assert make_task_key("CHATGPT", "x:y") != make_task_key("CHATGPT", "x")
        assert make_task_key("CHATGPT", "legacy-default:z") != make_task_key(
            "legacy-default", "z"
        )
        assert make_task_key("a", "b") == "1:a:b"
        assert make_task_key("aa", "b") == "2:aa:b"
        assert make_task_key("a", "b") != make_task_key("aa", "b")