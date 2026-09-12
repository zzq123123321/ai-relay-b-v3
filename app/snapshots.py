"""T15 工作台权威不可变快照（view-model，T14 接口向后兼容）。

数据流向：

    DB / Controller / Client
            ↓
        Snapshot
            ↓
     StatusPresenter（纯函数）→ Presentation → Dashboard

本模块只承载权威业务展示数据，不做任何查询/推断/IO；
禁止 import Qt 与任何业务模块（core/storage/adapters/app.controller 等）。

T15 合同要点（主规格第03/09/14 章，A 端审核定稿）：
- ActiveTaskSnapshot 增加持久 sequence（T07 SQLite FIFO sequence）作为身份维度，
  旧任务/旧attempt/旧epoch 回调守卫在 Presenter 层按 sequence→epoch→attempt 完成；
- connection_healthy / connection_source 为 T14 兼容字段，保留；新增结构化
  ConnectionSnapshot 供 T15 Presenter 优先使用；
- resume_total 与 consecutive_no_progress 是两个完全独立的计数（互不可推算）；
- progress_stage / progress_stage_completed 是快照权威输入，
  Task COMPLETED ≠ 结果交付完成（Outbox 仍可 PENDING）；
- 近期事件只携带 脱敏摘要，不携带完整正文/prompt/token/secret。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

TASK_STATES = ("QUEUED", "ACTIVE", "BLOCKED", "COMPLETED", "FAILED", "STOPPED_BY_USER")

# 主规格 3.2 Recovery phase 正式枚举；PAUSED/BLOCKED/NONE 是分支状态，
# 不作为用户可见阶段条节点（阶段条只有 7 个节点，见 status_presenter）。
RECOVERY_PHASES = (
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

# 阶段条只有 4 个正式用户可见阶段（主规格 14.2）；
# 顺序即阶段顺序，可用于 Presenter 计算 done/current/todo。
PROGRESS_STAGES = (
    "RECEIVED",  # 已接收
    "EXECUTE_VERIFY",  # 执行与验证
    "RESULT_SAVED",  # 结果保存
    "RESULT_DELIVERY",  # 结果交付
)

# Presenter/UI 可复用的展示 tone（与 ui/components.StatusBadge.TONES 一致）。
STATUS_TONES = ("success", "recovering", "danger", "neutral")


@dataclass(frozen=True, slots=True)
class ActiveTaskSnapshot:
    """当前活动任务身份与业务状态（不可变）。

    身份维度：sequence（持久 FIFO，T07 SQLite 权威，UI 不得生成）、
    attempt_id、authority_epoch。旧回调守卫以此三者为依据（见 presenter.guard）。
    """

    task_id: str | None = None
    title: str | None = None
    project: str | None = None
    state: str | None = None
    session: str | None = None
    # ---- T15 新增（含身份维度与工作台字段） ----
    sequence: int | None = None
    attempt_id: str | None = None
    authority_epoch: int | None = None
    requested_model: str | None = None
    parsed_model: str | None = None
    actual_model: str | None = None
    running_since: datetime | None = None
    received_at: datetime | None = None
    config_revision: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectionSnapshot:
    """模型/接口连接状态（结构化，T15 优先使用）。

    is_stale 是上游给 UI 的权威展示结论（TTL 由业务决定，UI/Presenter 不发明 30s/60s）；
    expires_at 仅用于显示/调试，不作为第二套时间权威。
    """

    source: str | None = None
    transport_ok: bool | None = None
    payload_valid: bool | None = None
    last_observed_at: datetime | None = None
    expires_at: datetime | None = None
    is_stale: bool | None = None


@dataclass(frozen=True, slots=True)
class AutoResumeSnapshot:
    """原会话自动续接开关（只展示，不放 send/timer/controller 等运行时对象）。"""

    enabled: bool | None = None
    paused_by_user: bool = False
    control_revision: int | None = None


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    """恢复状态（主规格 9.4 / renewal runtime 的纯数据表达）。"""

    phase: str | None = None
    interruption_id: str | None = None
    recovery_round_no: int = 0
    resume_total: int = 0                # 累计已确认进入原会话的续接（按 task 跨 attempt 求和）
    resume_send_attempts: int = 0        # 审计本地发送尝试（含结果不明），与 resume_total 独立
    consecutive_no_progress: int = 0     # 当前连续未恢复（冷却依据），仅真实进展后清零
    next_check_at: datetime | None = None
    last_real_progress_at: datetime | None = None
    pending_operation_id: str | None = None
    blocked_reason: str | None = None


@dataclass(frozen=True, slots=True)
class QueueItemSnapshot:
    """队列摘要项（工作台只列前 2–3 项；计数由 waiting_task_count 权威表达）。"""

    task_id: str | None = None
    sequence: int | None = None
    title: str | None = None
    project: str | None = None
    state: str | None = None
    blocked_reason: str | None = None


@dataclass(frozen=True, slots=True)
class EventSnapshot:
    """近期事件（只承载脱敏摘要，绝不承载完整正文/prompt/token/secret）。"""

    occurred_at: datetime | None = None
    event_code: str | None = None
    summary: str | None = None
    tone: str | None = None


@dataclass(frozen=True, slots=True)
class ApplicationSnapshot:
    """主窗/工作台当前活动状态（不可变 view-model）。

    page_stack / 导航选中 / 焦点 属于 UI 状态，不放在业务快照里。
    T14 字段（connection_healthy/connection_source）保留，向后兼容；
    T15 Presenter 优先使用结构化 connection / auto_resume / recovery。
    """

    active_task: ActiveTaskSnapshot | None = None
    stop_available: bool = False
    receiving_enabled: bool = False
    connection_healthy: bool = False          # T14 兼容
    connection_source: str | None = None       # T14 兼容
    # ---- T15 新增 ----
    connection: ConnectionSnapshot | None = None
    auto_resume: AutoResumeSnapshot | None = None
    recovery: RecoverySnapshot | None = None
    progress_stage: str | None = None
    progress_stage_completed: bool | None = None
    waiting_task_count: int | None = None
    queue_brief: tuple[QueueItemSnapshot, ...] = ()
    events: tuple[EventSnapshot, ...] = ()

    def replace_snapshot(self, **changes: object) -> "ApplicationSnapshot":
        """换发新快照（frozen 语义）；调用方负责构造完整一致性。"""
        return replace(self, **changes)


def fake_snapshot(
    *,
    task_id: str = "task-123",
    title: str = "测试任务：验证主窗壳页面切换保持活动任务",
    project: str = "ai-relay-b-v3",
    state: str = "ACTIVE",
    session: str | None = "rel_fake_session_0001",
    stop_available: bool = True,
    receiving_enabled: bool = True,
    connection_healthy: bool = True,
    connection_source: str = "Fake：未连接 OpenChamber/Reasonix",
    sequence: int | None = None,
    attempt_id: str | None = None,
    authority_epoch: int | None = None,
    requested_model: str | None = None,
    parsed_model: str | None = None,
    actual_model: str | None = None,
    running_since: datetime | None = None,
    received_at: datetime | None = None,
    config_revision: str | None = None,
    connection: ConnectionSnapshot | None = None,
    auto_resume: AutoResumeSnapshot | None = None,
    recovery: RecoverySnapshot | None = None,
    progress_stage: str | None = None,
    progress_stage_completed: bool | None = None,
    waiting_task_count: int | None = None,
    queue_brief: tuple[QueueItemSnapshot, ...] = (),
    events: tuple[EventSnapshot, ...] = (),
) -> ApplicationSnapshot:
    """独立构造 Fake 快照（T14 键不变；T15 字段安全默认 None/空）。

    sequence 为 None 表示“未知”（legacy/T14 构造），身份守卫按兼容规则处理。
    """
    active = ActiveTaskSnapshot(
        task_id=task_id,
        title=title,
        project=project,
        state=state,
        session=session,
        sequence=sequence,
        attempt_id=attempt_id,
        authority_epoch=authority_epoch,
        requested_model=requested_model,
        parsed_model=parsed_model,
        actual_model=actual_model,
        running_since=running_since,
        received_at=received_at,
        config_revision=config_revision,
    )
    return ApplicationSnapshot(
        active_task=active,
        stop_available=stop_available,
        receiving_enabled=receiving_enabled,
        connection_healthy=connection_healthy,
        connection_source=connection_source,
        connection=connection,
        auto_resume=auto_resume,
        recovery=recovery,
        progress_stage=progress_stage,
        progress_stage_completed=progress_stage_completed,
        waiting_task_count=waiting_task_count,
        queue_brief=queue_brief,
        events=events,
    )


def empty_snapshot(
    *,
    receiving_enabled: bool = True,
    connection_healthy: bool = False,
    connection_source: str | None = None,
    connection: ConnectionSnapshot | None = None,
    auto_resume: AutoResumeSnapshot | None = None,
    recovery: RecoverySnapshot | None = None,
    progress_stage: str | None = None,
    progress_stage_completed: bool | None = None,
    waiting_task_count: int | None = None,
    queue_brief: tuple[QueueItemSnapshot, ...] = (),
    events: tuple[EventSnapshot, ...] = (),
) -> ApplicationSnapshot:
    """无活动任务的空快照（UI-A01 空状态）；停止入口不可用。"""
    return ApplicationSnapshot(
        active_task=None,
        stop_available=False,
        receiving_enabled=receiving_enabled,
        connection_healthy=connection_healthy,
        connection_source=connection_source,
        connection=connection,
        auto_resume=auto_resume,
        recovery=recovery,
        progress_stage=progress_stage,
        progress_stage_completed=progress_stage_completed,
        waiting_task_count=waiting_task_count,
        queue_brief=queue_brief,
        events=events,
    )