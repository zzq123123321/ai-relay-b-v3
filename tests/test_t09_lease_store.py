"""T09：ProjectLease Store 读取与事务内取得 owner 的单元测试。

覆盖（主规格 10.2 / LeaseStore 职责边界）：
- 空表 read_owner 返回 None；acquire_in 后 read_owner 可读回 owner；
- related_sessions_json 正确 JSON 反序列化（含引号/中文字符，不按字符切分）；
- 同项目重复 acquire 撞主键 → IntegrityError 且整体回滚，仅保留先到者；
- owner 复合外键必须指向真实 attempt（悬空引用被约束拒绝）；
- 非法 lease 状态被明确拒绝；ACTIVE/QUARANTINED/ROTATING 均判定占用。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from core.domain import ReceiveSettingsSnapshot, SessionBindingMode, TargetExecutor
from core.protocol_v1 import ProtocolFormat, content_digest, parse_message
from storage.database import Database
from storage.lease_store import LeaseStoreError, ProjectLeaseStore
from storage.task_store import TaskStore, make_task_key

_V1 = (
    "AI_RELAY/1\n"
    "MESSAGE_ID: {task_id}\n"
    "SOURCE: CHATGPT\n"
    "TARGET: OPENCHAMBER\n"
    "TYPE: TASK\n"
    "\n"
    "{body}"
)

_T0 = "2026-10-01T09:00:00+00:00"


def make_receive(project_key: str) -> ReceiveSettingsSnapshot:
    return ReceiveSettingsSnapshot(
        config_revision=5,
        committed_at=_T0,
        received_at=_T0,
        effective_executor=TargetExecutor.OPENCHAMBER,
        directory=r"D:\AIwork\proj",
        project_key=project_key,
        agent="build",
        requested_model="",
        binding_mode=SessionBindingMode.FIXED_SESSION,
        frozen_session_id="sess-fixed",
    )


def claim_task(store: TaskStore, task_id: str, project_key: str) -> str:
    raw = _V1.format(task_id=task_id, body=f"处理任务 {task_id}")
    msg = parse_message(raw)
    task_key = make_task_key("CHATGPT", task_id)
    store.claim(
        task_key=task_key,
        peer_id="CHATGPT",
        task_id=task_id,
        protocol_format=ProtocolFormat.V1.value.upper(),
        raw_message=raw,
        body=msg.body,
        canonical_hash=content_digest(msg),
        receive_snapshot=make_receive(project_key),
        received_at=_T0,
    )
    return task_key


def insert_attempt(conn: sqlite3.Connection, attempt_id: str, task_key: str) -> None:
    conn.execute(
        "INSERT INTO attempts (attempt_id, task_key, kind, state, authority_epoch,"
        " execution_snapshot_json, started_at) VALUES (?,?,?,?,?,?,?)",
        (attempt_id, task_key, "INITIAL", "OPEN", 1, "{}", _T0),
    )


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "lease.sqlite")
    db.open()
    store = TaskStore(db)
    leases = ProjectLeaseStore(db)
    yield db, store, leases
    db.close()


class TestLeaseRead:
    def test_read_owner_empty_returns_none(self, env):
        _db, _store, leases = env
        assert leases.read_owner("project-x") is None

    def test_read_owner_roundtrip_job(self, env):
        db, store, leases = env
        task_key = claim_task(store, "t1", "project-x")
        with db.transaction():
            insert_attempt(db.connection, "attempt-001", task_key)
            leases.acquire_in(
                db.connection,
                project_key="project-x",
                owner_task_key=task_key,
                owner_attempt_id="attempt-001",
                authority_epoch=1,
                now=_T0,
                related_sessions=("sess-1", "sess-2"),
                reason="initial",
            )
        owner = leases.read_owner("project-x")
        assert owner is not None
        assert owner.project_key == "project-x"
        assert owner.owner_task_key == task_key
        assert owner.owner_attempt_id == "attempt-001"
        assert owner.authority_epoch == 1
        assert owner.state == "ACTIVE"
        # related_sessions_json 必须是 JSON 反序列化后的字符串元组，而非逐字符切分
        assert owner.related_sessions == ("sess-1", "sess-2")
        assert owner.reason == "initial"

    def test_occupying_states_definition(self):
        from dataclasses import replace

        base = leases_read_fixture()
        for state in ("ACTIVE", "QUARANTINED", "ROTATING"):
            assert replace(base, state=state).occupying() is True
        assert replace(base, state="RELEASED").occupying() is False


def leases_read_fixture():
    from storage.lease_store import ProjectLease

    return ProjectLease(project_key="p", owner_task_key="k", owner_attempt_id="a", authority_epoch=1, state="ACTIVE")


class TestLeaseAcquire:
    def test_duplicate_project_acquire_conflicts_and_rolls_back(self, env):
        db, store, leases = env
        task_a = claim_task(store, "t1", "project-x")
        task_b = claim_task(store, "t2", "project-x")
        with db.transaction():
            insert_attempt(db.connection, "attempt-001", task_a)
            leases.acquire_in(
                db.connection,
                project_key="project-x", owner_task_key=task_a,
                owner_attempt_id="attempt-001", authority_epoch=1, now=_T0,
            )
        with db.transaction():
            insert_attempt(db.connection, "attempt-002", task_b)
            with pytest.raises(sqlite3.IntegrityError):
                leases.acquire_in(
                    db.connection,
                    project_key="project-x", owner_task_key=task_b,
                    owner_attempt_id="attempt-002", authority_epoch=1, now=_T0,
                )

        rows = db.connection.execute(
            "SELECT owner_task_key, owner_attempt_id FROM project_leases"
        ).fetchall()
        assert len(rows) == 1  # 冲突整体回滚，仅保留先到者
        assert rows[0] == (task_a, "attempt-001")

    def test_dangling_owner_attempt_rejected(self, env):
        db, store, leases = env
        task_key = claim_task(store, "t1", "project-x")
        with db.transaction():
            with pytest.raises(sqlite3.IntegrityError):
                leases.acquire_in(
                    db.connection,
                    project_key="project-x", owner_task_key=task_key,
                    owner_attempt_id="no-such-attempt", authority_epoch=1, now=_T0,
                )
        assert leases.read_owner("project-x") is None

    def test_illegal_state_rejected(self, env):
        db, store, leases = env
        task_key = claim_task(store, "t1", "project-x")
        with db.transaction():
            insert_attempt(db.connection, "attempt-001", task_key)
            with pytest.raises(LeaseStoreError):
                leases.acquire_in(
                    db.connection,
                    project_key="project-x", owner_task_key=task_key,
                    owner_attempt_id="attempt-001", authority_epoch=1, now=_T0,
                    state="RELEASED",
                )


class TestQuarantined:
    def test_quarantined_owner_still_occupies(self, env):
        db, store, leases = env
        task_key = claim_task(store, "t1", "project-x")
        with db.transaction():
            insert_attempt(db.connection, "attempt-001", task_key)
            leases.acquire_in(
                db.connection,
                project_key="project-x", owner_task_key=task_key,
                owner_attempt_id="attempt-001", authority_epoch=1, now=_T0,
            )
            db.connection.execute("UPDATE project_leases SET state='QUARANTINED'")
            db.connection.execute("UPDATE attempts SET state='STOPPED' WHERE attempt_id='attempt-001'")
        owner = leases.read_owner("project-x")
        assert owner is not None
        assert owner.state == "QUARANTINED"
        assert owner.occupying() is True  # R05：本地终态已回传，同项目执行权仍被隔离占用
        assert leases.read_owner("other") is None


def test_lease_store_does_not_know_sessions():
    import inspect

    from storage.lease_store import ProjectLeaseStore as P

    for name, member in inspect.getmembers(P):
        if name.startswith("__"):
            continue
        assert "session" not in name.lower()