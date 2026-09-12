"""T07：入站边界的协议解析 + 认领衔接测试（P04/P05/P08/Q01/Q02 的仪表边界）。

IngressService 只负责：严格解析 → 只认领 CHATGPT 的 TASK → 规范摘要 → 原子认领。
全部正常业务结果走 ClaimResult；协议/策略错误抛 IngressError，绝不写入半条任务。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from core.domain import ReceiveSettingsSnapshot, SessionBindingMode, TargetExecutor
from core.ingress import (
    IngressError,
    IngressErrorCode,
    IngressService,
    PEER_ID_CHATGPT,
    PEER_ID_LEGACY_DEFAULT,
)
from core.protocol_v1 import ProtocolFormat, content_digest, parse_message
from infra.clock import FakeClock
from storage.database import Database
from storage.task_store import ClaimOutcome, TaskStore


def make_snapshot(**kw) -> ReceiveSettingsSnapshot:
    return ReceiveSettingsSnapshot(
        config_revision=kw.get("config_revision", 7),
        committed_at=kw.get("committed_at", "2026-09-12T00:00:00+00:00"),
        received_at=kw.get("received_at", "2026-09-12T00:00:00+00:00"),
        effective_executor=kw.get("effective_executor", TargetExecutor.OPENCHAMBER),
        directory=kw.get("directory", r"D:\AIwork\proj"),
        project_key=kw.get("project_key", "proj"),
        agent=kw.get("agent", "build"),
        requested_model=kw.get("requested_model", ""),
        binding_mode=kw.get("binding_mode", SessionBindingMode.PROJECT_ROTATING),
    )


def v1_task(task_id: str, body: str = "修复仪表盘") -> str:
    return (
        "AI_RELAY/1\n"
        f"MESSAGE_ID: {task_id}\n"
        "SOURCE: CHATGPT\n"
        "TARGET: OPENCHAMBER\n"
        "TYPE: TASK\n"
        "\n"
        f"{body}"
    )


def v1_response(task_id: str) -> str:
    return (
        "AI_RELAY/1\n"
        f"MESSAGE_ID: {task_id}\n"
        "SOURCE: CHATGPT\n"
        "TARGET: OPENCHAMBER\n"
        "TYPE: RESPONSE\n"
        f"IN_REPLY_TO: {task_id}-orig\n"
        "\n"
        "完成"
    )


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "db.sqlite")
    database.open()
    yield database
    database.close()


@pytest.fixture
def store(db):
    return TaskStore(db)


@pytest.fixture
def clock():
    return FakeClock(wall=datetime(2026, 9, 12, 9, 30, 0, tzinfo=timezone.utc))


@pytest.fixture
def ingress(db, store, clock):
    return IngressService(store, clock=clock)


class TestAcceptTask:
    def test_accept_persists_queued_v1_task(self, db, ingress):
        result = ingress.accept(v1_task("m1"), receive_snapshot=make_snapshot())
        assert result.outcome is ClaimOutcome.ACCEPTED
        assert result.sequence == 1
        assert result.state == "QUEUED"
        assert result.peer_id == PEER_ID_CHATGPT
        row = db.connection.execute(
            "SELECT raw_message, body, protocol_format, peer_id, task_id, "
            "received_at, ingress_snapshot_json FROM tasks"
        ).fetchone()
        assert row is not None
        assert row[0].startswith("AI_RELAY/1")
        assert row[1] == "修复仪表盘"
        assert row[2] == ProtocolFormat.V1.value.upper()
        assert row[3] == PEER_ID_CHATGPT
        assert row[4] == "m1"
        assert row[5] == "2026-09-12T09:30:00+00:00"
        inner = json.loads(row[6])
        assert inner["config_revision"] == 7
        assert inner["project_key"] == "proj"

    def test_accept_legacy_uses_legacy_default_peer(self, db, ingress):
        raw = (
            "----- AI_RELAY_BEGIN -----\n"
            "TASK_ID: m-legacy\n"
            "SOURCE: CHATGPT\n"
            "TARGET: OPENCHAMBER\n"
            "TYPE: TASK\n"
            "CONTENT: 旧格式正文\n"
            "----- AI_RELAY_END -----"
        )
        result = ingress.accept(raw, receive_snapshot=make_snapshot())
        assert result.outcome is ClaimOutcome.ACCEPTED
        parsed = parse_message(raw)
        assert parsed.protocol_format is ProtocolFormat.LEGACY_WEB
        row = db.connection.execute(
            "SELECT peer_id FROM tasks WHERE task_id='m-legacy'"
        ).fetchone()
        assert row == (PEER_ID_LEGACY_DEFAULT,)
        assert result.peer_id == PEER_ID_LEGACY_DEFAULT

    def test_accept_persists_frozen_snapshot_revision(self, db, ingress):
        ingress.accept(
            v1_task("m2"), receive_snapshot=make_snapshot(config_revision=42)
        )
        row = db.connection.execute(
            "SELECT ingress_snapshot_json FROM tasks WHERE task_id='m2'"
        ).fetchone()
        inner = json.loads(row[0])
        assert inner["config_revision"] == 42
        # 冻结快照包含接收时刻，允许 set();ingress 不再改写
        assert inner["received_at"]

    def test_accept_duplicate_returns_existing(self, ingress):
        first = ingress.accept(v1_task("m3"), receive_snapshot=make_snapshot())
        second = ingress.accept(v1_task("m3"), receive_snapshot=make_snapshot())
        assert first.outcome is ClaimOutcome.ACCEPTED
        assert second.outcome is ClaimOutcome.EXISTING
        assert second.sequence == 1

    def test_accept_conflict_keeps_old_data(self, db, ingress):
        ingress.accept(v1_task("m4", "正文 A"), receive_snapshot=make_snapshot())
        conflict = ingress.accept(
            v1_task("m4", "正文 B"), receive_snapshot=make_snapshot()
        )
        assert conflict.outcome is ClaimOutcome.CONFLICT
        row = db.connection.execute(
            "SELECT body FROM tasks WHERE task_id='m4'"
        ).fetchone()
        assert row[0] == "正文 A"

    def test_accept_does_not_update_original_received_at(self, db, clock, ingress):
        clock.wall = datetime(2026, 9, 12, 9, 30, 0, tzinfo=timezone.utc)
        ingress.accept(v1_task("m5"), receive_snapshot=make_snapshot())
        clock.wall = datetime(2026, 9, 12, 10, 0, 0, tzinfo=timezone.utc)
        ingress.accept(v1_task("m5"), receive_snapshot=make_snapshot())
        row = db.connection.execute(
            "SELECT received_at FROM tasks WHERE task_id='m5'"
        ).fetchone()
        assert row[0] == "2026-09-12T09:30:00+00:00"


class TestRejects:
    def test_reject_response(self, db, ingress):
        with pytest.raises(IngressError) as ei:
            ingress.accept(v1_response("m9"), receive_snapshot=make_snapshot())
        assert ei.value.code is IngressErrorCode.NOT_TASK
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

    def test_reject_non_chatgpt_source(self, db, ingress):
        raw = (
            "AI_RELAY/1\n"
            "MESSAGE_ID: m9\n"
            "SOURCE: OTHER_BOT\n"
            "TARGET: OPENCHAMBER\n"
            "TYPE: TASK\n"
            "\n"
            "正文"
        )
        with pytest.raises(IngressError) as ei:
            ingress.accept(raw, receive_snapshot=make_snapshot())
        assert ei.value.code is IngressErrorCode.NOT_FROM_CHATGPT
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

    def test_reject_protocol_error_carries_code(self, db, ingress):
        with pytest.raises(IngressError) as ei:
            ingress.accept("not a relay message", receive_snapshot=make_snapshot())
        assert ei.value.code is IngressErrorCode.PROTOCOL
        assert "protocol_code" in ei.value.context
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

    def test_reject_oversize_message(self, db, ingress):
        body = "x" * (3_000_000)
        raw = (
            "AI_RELAY/1\n"
            "MESSAGE_ID: big\n"
            "SOURCE: CHATGPT\n"
            "TARGET: OPENCHAMBER\n"
            "TYPE: TASK\n"
            "\n"
            + body
        )
        with pytest.raises(IngressError) as ei:
            ingress.accept(raw, receive_snapshot=make_snapshot())
        assert ei.value.code is IngressErrorCode.PROTOCOL
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

    def test_protocol_error_has_explicit_code_field(self):
        err = IngressError(
            IngressErrorCode.NOT_TASK, "原因", context={"detail": "d"}
        )
        assert str(err) == "[not_task] 原因"


class TestQueueLimits:
    def test_queue_full_rejects_new(self, tmp_path):
        db_full = Database(tmp_path / "db.sqlite")
        db_full.open()
        try:
            s = TaskStore(db_full, queue_capacity=1)
            svc = IngressService(s, clock=FakeClock())
            svc.accept(v1_task("a"), receive_snapshot=make_snapshot())
            full = svc.accept(v1_task("b"), receive_snapshot=make_snapshot())
            assert full.outcome is ClaimOutcome.QUEUE_FULL
            assert full.sequence is None
            dup = svc.accept(v1_task("a"), receive_snapshot=make_snapshot())
            assert dup.outcome is ClaimOutcome.EXISTING  # 已存在优先于队列满
        finally:
            db_full.close()


class TestRecoveryOrdering:
    def test_restart_recover_order_and_sequence(self, tmp_path, clock):
        path = tmp_path / "ing.sqlite"
        db1 = Database(path)
        db1.open()
        try:
            s1 = TaskStore(db1)
            svc1 = IngressService(s1, clock=clock)
            svc1.accept(v1_task("n1"), receive_snapshot=make_snapshot())
            svc1.accept(v1_task("n2"), receive_snapshot=make_snapshot())
        finally:
            db1.close()

        db2 = Database(path)
        db2.open()
        try:
            s2 = TaskStore(db2)
            svc2 = IngressService(s2, clock=clock)
            queued = s2.list_queued()
            assert [r.task_id for r in queued] == ["n1", "n2"]
            assert [r.sequence for r in queued] == [1, 2]
            third = svc2.accept(v1_task("n3"), receive_snapshot=make_snapshot())
            assert third.outcome is ClaimOutcome.ACCEPTED
            assert third.sequence == 3
        finally:
            db2.close()

    def test_accept_never_creates_attempts(self, db, ingress):
        ingress.accept(v1_task("m8"), receive_snapshot=make_snapshot())
        count = db.connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
        assert count == 0  # 认领只入队，绝不触达执行