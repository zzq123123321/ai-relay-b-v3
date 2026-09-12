"""T15-B1 快照层（app/snapshots.py）定向测试。

验收依据：主规格 03/09/14.2–14.3 + A 端 T15-A 审核定稿。
覆盖：T14 接口向后兼容、T15 身份/连接/恢复/阶段/队列/事件字段合同、
不可变与 tuple 结构、进度阶段与恢复枚举常量的正式集合。
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dataclasses import FrozenInstanceError  # noqa: E402

import pytest  # noqa: E402

from app.snapshots import (  # noqa: E402
    PROGRESS_STAGES,
    RECOVERY_PHASES,
    ActiveTaskSnapshot,
    ApplicationSnapshot,
    AutoResumeSnapshot,
    ConnectionSnapshot,
    EventSnapshot,
    QueueItemSnapshot,
    RecoverySnapshot,
    empty_snapshot,
    fake_snapshot,
)


# ------------------------------------------------------------- T14 兼容


def test_legacy_t14_snapshot_construction_still_works():
    """旧构造方式（fake_snapshot() 无 T15 参数）仍然有效并保留旧字段。"""
    snap = fake_snapshot()
    assert snap.active_task.task_id == "task-123"
    assert snap.active_task.state == "ACTIVE"
    assert snap.connection_healthy is True
    assert snap.connection_source is not None
    assert snap.stop_available is True
    assert snap.receiving_enabled is True
    assert callable(snap.replace_snapshot)


def test_legacy_t14_fields_preserved_and_t15_fields_default_safe():
    """旧字段保留；新的 T15 字段一律安全默认（None/0/空 tuple）。"""
    snap = fake_snapshot()
    task = snap.active_task
    assert task.sequence is None
    assert task.attempt_id is None
    assert task.authority_epoch is None
    assert task.requested_model is None
    assert task.parsed_model is None
    assert task.actual_model is None
    assert task.running_since is None
    assert task.received_at is None
    assert task.config_revision is None
    assert snap.connection is None
    assert snap.auto_resume is None
    assert snap.recovery is None
    assert snap.progress_stage is None
    assert snap.progress_stage_completed is None
    assert snap.waiting_task_count is None
    assert snap.queue_brief == ()
    assert snap.events == ()


def test_empty_snapshot_stays_activity_free():
    """empty_snapshot：无活动任务、停止不可用（UI-A01 空状态）。"""
    snap = empty_snapshot()
    assert snap.active_task is None
    assert snap.stop_available is False
    assert snap.connection_healthy is False
    assert snap.queue_brief == ()
    assert snap.events == ()


def test_replace_snapshot_immutably_reissues():
    """replace_snapshot 不修改原快照，只换发新快照（嵌套用 replace_of 换 task）。"""
    from dataclasses import replace as dataclass_replace

    snap = fake_snapshot(state="ACTIVE")
    changed_task = dataclass_replace(snap.active_task, state="COMPLETED")
    replaced = snap.replace_snapshot(active_task=changed_task)
    assert snap.active_task.state == "ACTIVE"
    assert replaced.active_task.state == "COMPLETED"
    assert replaced is not snap
    assert replaced.active_task.task_id == snap.active_task.task_id


# ------------------------------------------------------------- T15 合同


def test_active_task_carries_identity_and_model_fields():
    """ActiveTaskSnapshot 携带 sequence/attempt_id/authority_epoch 与模型字段。"""
    t = datetime(2026, 9, 10, 8, 30, 0)
    task = ActiveTaskSnapshot(
        task_id="t1",
        sequence=11,
        attempt_id="a1",
        authority_epoch=2,
        requested_model="qwen3.8-27b",
        parsed_model="qwen3.8-27b",
        actual_model="qwen3.8-27b",
        running_since=t,
        received_at=t,
        config_revision="cfg-1",
    )
    assert task.sequence == 11
    assert task.attempt_id == "a1"
    assert task.authority_epoch == 2
    assert task.actual_model == "qwen3.8-27b"
    assert task.running_since == t
    assert task.config_revision == "cfg-1"


def test_progress_stage_enum_is_exactly_four_in_order():
    """阶段条只有 4 段且顺序固定（主规格 14.2）。"""
    assert PROGRESS_STAGES == (
        "RECEIVED",
        "EXECUTE_VERIFY",
        "RESULT_SAVED",
        "RESULT_DELIVERY",
    )


def test_recovery_phase_enum_contains_branch_states():
    """恢复 phase 全集含 NONE/PAUSED/BLOCKED 分支态（11 个，阶段条取 7 节点）。"""
    assert RECOVERY_PHASES == (
        "WATCHING",
        "WAIT_NETWORK",
        "VERIFYING",
        "SUSPECTED",
        "SCHEDULED",
        "SENDING",
        "AWAITING_PROGRESS",
        "COOLDOWN",
        "PAUSED",
        "BLOCKED",
        "NONE",
    )


def test_connection_snapshot_carries_stale_as_authoritative():
    """is_stale 是上游给 UI 的权威结论；expires_at 只用于显示。"""
    t = datetime(2026, 9, 10, 12, 0, 0)
    conn = ConnectionSnapshot(
        source="S1",
        transport_ok=True,
        payload_valid=True,
        last_observed_at=t,
        expires_at=t,
        is_stale=True,
    )
    assert conn.is_stale is True
    assert conn.expires_at == t
    assert conn.transport_ok is True
    assert hasattr(conn, "source")


def test_resume_and_consecutive_are_independent_fields():
    """resume_total 与 consecutive_no_progress 是两个独立字段，不可互推。"""
    r1 = RecoverySnapshot(resume_total=7, consecutive_no_progress=0)
    r2 = RecoverySnapshot(resume_total=8, consecutive_no_progress=3)
    assert r1.resume_total == 7 and r1.consecutive_no_progress == 0
    assert r2.resume_total == 8 and r2.consecutive_no_progress == 3


def test_recovery_snapshot_full_contract():
    """恢复快照携带阶段、中断、计数、下次检查、真进展与阻断原因。"""
    t = datetime(2026, 9, 10, 12, 34, 56)
    rec = RecoverySnapshot(
        phase="COOLDOWN",
        interruption_id="i-9",
        recovery_round_no=3,
        resume_total=7,
        resume_send_attempts=12,
        consecutive_no_progress=3,
        next_check_at=t,
        last_real_progress_at=t,
        pending_operation_id="op-1",
        blocked_reason="SESSION_MISSING",
    )
    assert rec.phase == "COOLDOWN"
    assert rec.resume_total == 7
    assert rec.resume_send_attempts == 12
    assert rec.consecutive_no_progress == 3
    assert rec.next_check_at == t
    assert rec.last_real_progress_at == t
    assert rec.pending_operation_id == "op-1"
    assert rec.blocked_reason == "SESSION_MISSING"


def test_queue_and_event_snapshots_are_semantic_tuples():
    """工作台队列/事件都是 tuple 集合，不承载完整正文/敏感内容。"""
    q1 = QueueItemSnapshot(task_id="t2", sequence=12, title="q", project="p", state="QUEUED")
    e1 = EventSnapshot(occurred_at=None, event_code="E1", summary="s", tone="recovering")
    snap = fake_snapshot(
        waiting_task_count=5,
        queue_brief=(q1,),
        events=(e1,),
    )
    assert isinstance(snap.queue_brief, tuple)
    assert isinstance(snap.events, tuple)
    assert snap.queue_brief[0].task_id == "t2"
    assert snap.events[0].summary == "s"


def test_application_snapshot_holds_t15_blocks_side_by_side():
    """ApplicationSnapshot 同时承载 connection/auto_resume/recovery/progress。"""
    conn = ConnectionSnapshot(source="S1")
    auto = AutoResumeSnapshot(enabled=True)
    rec = RecoverySnapshot(phase="SENDING")
    snap = fake_snapshot(
        connection=conn,
        auto_resume=auto,
        recovery=rec,
        progress_stage="RESULT_DELIVERY",
        progress_stage_completed=False,
    )
    assert snap.connection is conn
    assert snap.auto_resume is auto
    assert snap.recovery is rec
    assert snap.progress_stage == "RESULT_DELIVERY"
    assert snap.progress_stage_completed is False


def test_all_snapshots_are_frozen_objects():
    """所有快照 dataclass 均为 frozen：对既有字段 setattr 必须拒绝。"""
    for obj, attr, value in [
        (ActiveTaskSnapshot(), "task_id", "x"),
        (ConnectionSnapshot(), "source", "x"),
        (AutoResumeSnapshot(), "enabled", True),
        (RecoverySnapshot(), "phase", "x"),
        (QueueItemSnapshot(), "task_id", "x"),
        (EventSnapshot(), "summary", "x"),
        (empty_snapshot(), "connection_healthy", True),
    ]:
        with pytest.raises(FrozenInstanceError):
            setattr(obj, attr, value)