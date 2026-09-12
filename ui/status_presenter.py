"""工作台状态展示层（纯函数，T15-B1）。

接收权威 Snapshot（app.snapshots），输出不可变 Presentation 模型，
供 Dashboard（T15-B2）渲染。本模块：
- 不 import PySide6 / QTimer / QObject；
- 不查询 DB / Store / Controller / Dispatch，不 send / continue / timer；
- 不调用 datetime.now() 判断过期（is_stale 由上游快照权威给出，
  expires_at 仅用于显示/调试，不作为第二套时间权威）；
- 不生成假百分比；不显示“达到最大次数/停止续接/恢复耗尽”。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.snapshots import (
    PROGRESS_STAGES,
    STATUS_TONES,
    ActiveTaskSnapshot,
    ApplicationSnapshot,
)

# 恢复状态条：7 个用户可见节点（主规格 3.2 / A 端 T15-A 审核定稿）。
# NONE / PAUSED / BLOCKED 是分支状态，不占节点。
RECOVERY_STEP_LABELS: tuple[str, ...] = (
    "等待网络",        # index 0  WAIT_NETWORK
    "恢复核对",        # index 1  VERIFYING
    "计划复查",        # index 2  SUSPECTED / SCHEDULED
    "续接发送",        # index 3  SENDING
    "已入会话待进展",  # index 4  AWAITING_PROGRESS
    "已恢复执行",      # index 5  WATCHING（恢复后）
    "冷却",           # index 6  COOLDOWN
)

# 阶段条中文标签（顺序与 PROGRESS_STAGES 一致，只有 4 段）。
PROGRESS_STEP_LABELS: dict[str, str] = {
    "RECEIVED": "已接收",
    "EXECUTE_VERIFY": "执行与验证",
    "RESULT_SAVED": "结果保存",
    "RESULT_DELIVERY": "结果交付",
}

BLOCKED_REASON_LABELS: dict[str, str] = {
    "USER_ACTION": "需要您在界面中选择下一步操作",
    "CONFIGURATION": "需要检查配置",
    "SESSION_MISSING": "原会话记录缺失，需要人工核对",
    "ATTRIBUTION_UNVERIFIED": "归属尚未核实，需要人工确认",
    "DISPATCH_UNRESOLVED": "分发目标未确定，需要处理",
    "CAPABILITY_UNSUPPORTED": "当前能力不支持自动续接",
    "LOCAL_STORAGE": "本地存储不可用",
}

_SHOW_CHAIN_PHASES = frozenset(
    {"WAIT_NETWORK", "VERIFYING", "SUSPECTED", "SCHEDULED", "SENDING",
     "AWAITING_PROGRESS", "COOLDOWN", "PAUSED", "BLOCKED"}
)

_CHAIN_INDEX: dict[str, int] = {
    "WAIT_NETWORK": 0,
    "VERIFYING": 1,
    "SUSPECTED": 2,
    "SCHEDULED": 2,
    "SENDING": 3,
    "AWAITING_PROGRESS": 4,
    "COOLDOWN": 6,
}


def _tone(tone: str | None, default: str = "neutral") -> str:
    if tone in STATUS_TONES:
        return str(tone)
    return default


def _clock(dt: datetime | None) -> str:
    return dt.strftime("%H:%M:%S") if dt is not None else "—"


def format_clock(dt: datetime | None) -> str:
    """展示用时钟（HH:MM:SS）；上课快照的确定性输出，不依赖当前时间。"""
    return _clock(dt)


def format_timestamp(dt: datetime | None) -> str:
    """展示用完整时间（YYYY-MM-DD HH:MM:SS）或 '—'。"""
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt is not None else "—"


@dataclass(frozen=True, slots=True)
class StatusPresentation:
    headline: str
    detail: str
    tone: str
    is_terminal: bool = False
    show_recovery_bar: bool = False
    highlight_step_index: int | None = None
    next_action_text: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryStepPresentation:
    label: str
    state: str  # done | current | todo
    tone: str


@dataclass(frozen=True, slots=True)
class ProgressStagePresentation:
    code: str
    label: str
    state: str  # done | current | todo


@dataclass(frozen=True, slots=True)
class ProgressPresentation:
    stages: tuple[ProgressStagePresentation, ...]
    stage_code: str | None
    completed: bool


@dataclass(frozen=True, slots=True)
class CountersPresentation:
    resume_total: int
    consecutive_no_progress: int
    total_text: str
    consecutive_text: str


@dataclass(frozen=True, slots=True)
class ConnectionPresentation:
    headline: str
    detail: str
    tone: str


@dataclass(frozen=True, slots=True)
class EventRowPresentation:
    occurred_at: datetime | None
    event_code: str | None
    summary: str
    tone: str
    time_text: str


def _recovery(snapshot: ApplicationSnapshot):
    task = snapshot.active_task
    if task is None:
        return None, None, None
    recovery = snapshot.recovery
    if recovery is None:
        return task, None, None
    return task, recovery, snapshot.auto_resume


def is_superseded_update(
    candidate: ApplicationSnapshot,
    current: ApplicationSnapshot,
) -> bool:
    """身份守卫：candidate 是否应当被拒绝（旧任务/旧attempt/旧epoch）。

    规则（sequence 优先，全部比较只读 Snapshot 字段）：
    - 两者都有 sequence：小→拒绝；大→新任务允许；
      相同 → 再比 task_id（不同→拒绝）→ epoch（低→拒绝，高→新代允许）；
      同 epoch → attempt（当前无 attempt 且候选有→QUEUED→STARTED 合法，
      当前有且候选无→回退拒绝；都有且不同→拒绝）。
    - 任一没有 sequence（legacy/T14）：task_id 不同→安全侧拒绝；
      相同 → 依次比 epoch / attempt。
    - 无法证明更旧 → False（接受）。
    """
    cand = candidate.active_task
    curr = current.active_task
    if cand is None or curr is None:
        return False

    both_seq = cand.sequence is not None and curr.sequence is not None
    if both_seq:
        if cand.sequence < curr.sequence:
            return True
        if cand.sequence > curr.sequence:
            return False
        # sequence 相同：先看 task_id
        if cand.task_id != curr.task_id:
            return True
    else:
        # legacy：无 sequence 可比，task_id 不同一律安全侧拒绝
        if cand.task_id != curr.task_id:
            return True

    if cand.authority_epoch is not None and curr.authority_epoch is not None:
        if cand.authority_epoch < curr.authority_epoch:
            return True
        if cand.authority_epoch > curr.authority_epoch:
            return False

    if cand.attempt_id is not None and curr.attempt_id is not None:
        if cand.attempt_id != curr.attempt_id:
            return True
        return False
    if curr.attempt_id is None and cand.attempt_id is not None:
        return False  # QUEUED→STARTED 合法推进
    if curr.attempt_id is not None and cand.attempt_id is None:
        return True   # 有 attempt 回退到无 attempt → 旧
    return False


def recovery_steps(snapshot: ApplicationSnapshot) -> tuple[RecoveryStepPresentation, ...]:
    """7 节点恢复状态条：『等待网络→恢复核对→计划复查→续接发送→
    已入会话待进展→已恢复执行→冷却』。

    - 恢复中：当前阶段=current，之前=done，之后=todo；
    - 已恢复执行（WATCHING 且已有真实进展）：当前阶段=done，其余 done；
    - PAUSED / BLOCKED：阶段条全量走 todo+neutral（分支状态不占节点）；
    - 无恢复上下文：全 todo（不冒充失败）。
    """
    task, recovery, _auto = _recovery(snapshot)
    if task is None or recovery is None or recovery.phase in ("NONE", None):
        return tuple(
            RecoveryStepPresentation(label=label, state="todo", tone="neutral")
            for label in RECOVERY_STEP_LABELS
        )

    phase = recovery.phase
    if phase in ("PAUSED", "BLOCKED"):
        return tuple(
            RecoveryStepPresentation(label=label, state="todo", tone="neutral")
            for label in RECOVERY_STEP_LABELS
        )
    if phase == "WATCHING":
        # “已恢复执行”之后：全部视为 done（真实进展已确认）
        return tuple(
            RecoveryStepPresentation(label=label, state="done", tone="success")
            for label in RECOVERY_STEP_LABELS
        )

    idx = _CHAIN_INDEX.get(phase)
    if idx is None:
        return tuple(
            RecoveryStepPresentation(label=label, state="todo", tone="neutral")
            for label in RECOVERY_STEP_LABELS
        )
    steps: list[RecoveryStepPresentation] = []
    for i, label in enumerate(RECOVERY_STEP_LABELS):
        if i < idx:
            state = "done"
            tone = "success"
        elif i == idx:
            state = "current"
            tone = "recovering"
        else:
            state = "todo"
            tone = "neutral"
        steps.append(RecoveryStepPresentation(label=label, state=state, tone=tone))
    return tuple(steps)


def _next_action_text(recovery) -> str | None:
    if recovery is None or recovery.next_check_at is None:
        return None
    return f"下次检查 {_clock(recovery.next_check_at)}"


def _task_detail(task: ActiveTaskSnapshot, recovery) -> str:
    if recovery is not None and recovery.last_real_progress_at is not None:
        return f"最后真实进展 {format_timestamp(recovery.last_real_progress_at)}"
    if task.received_at is not None:
        return f"接收于 {format_timestamp(task.received_at)}"
    return "执行中，等待状态更新"


def present_status(
    snapshot: ApplicationSnapshot,
) -> StatusPresentation:
    """工作台主状态（头部区块）：标题 + 摘要 + tone + 是否终态。

    优先级：Task 终态 > 恢复分支/恢复阶段 > QUEUED > 无任务空态。
    """
    task, recovery, auto = _recovery(snapshot)
    if task is None:
        return StatusPresentation("等待接收任务", "系统就绪，等待 A 端发送任务", "neutral")

    state = task.state
    if state == "COMPLETED":
        return StatusPresentation("已完成", "任务执行完成（结果交付以右栏交付进度为准）", "success", is_terminal=True)
    if state == "FAILED":
        return StatusPresentation("失败", _task_detail(task, recovery), "danger", is_terminal=True)
    if state == "STOPPED_BY_USER":
        return StatusPresentation("已停止", "您已停止该任务", "neutral", is_terminal=True)
    if state == "QUEUED":
        return StatusPresentation("排队中", "等待执行（无恢复过程）", "neutral")
    if state == "BLOCKED":
        reason = recovery.blocked_reason if recovery is not None else None
        hint = action_hint(reason)
        reason_text = BLOCKED_REASON_LABELS.get(reason, "需要人工处理")
        detail = f"恢复被阻断：{reason_text}" if reason else "任务被阻断，需要人工处理"
        if reason and hint:
            detail += f"；{hint}"
        return StatusPresentation(
            "待人工处理",
            detail,
            "recovering",
            show_recovery_bar=recovery is not None,
        )

    if auto is not None and auto.paused_by_user and recovery is not None \
            and recovery.phase not in (None, "NONE"):
        return StatusPresentation(
            "自动续接暂停",
            "您已暂停原会话自动续接；任务执行不受影响",
            "neutral",
            show_recovery_bar=True,
        )

    if recovery is None or recovery.phase in (None, "NONE"):
        return StatusPresentation(
            "正常执行",
            _task_detail(task, None),
            "success",
        )

    phase = recovery.phase
    if phase == "WATCHING":
        if recovery.interruption_id is not None \
                and recovery.last_real_progress_at is not None:
            return StatusPresentation(
                "已恢复执行",
                f"消息已确认进入原会话 · 最后真实进展 {format_timestamp(recovery.last_real_progress_at)}",
                "success",
                show_recovery_bar=True,
                highlight_step_index=5,
            )
        return StatusPresentation("正常执行", _task_detail(task, recovery), "success")

    if phase == "WAIT_NETWORK":
        return StatusPresentation(
            "等待网络",
            "网络暂不可达，系统会继续检测，不会判为失败",
            "recovering",
            show_recovery_bar=True,
            highlight_step_index=0,
            next_action_text=_next_action_text(recovery),
        )
    if phase == "VERIFYING":
        return StatusPresentation(
            "恢复核对",
            "正在核对原会话与消息证据",
            "recovering",
            show_recovery_bar=True,
            highlight_step_index=1,
        )
    if phase in ("SUSPECTED", "SCHEDULED"):
        return StatusPresentation(
            "计划复查",
            "已安排自动复查，暂不发送续接",
            "recovering",
            show_recovery_bar=True,
            highlight_step_index=2,
            next_action_text=_next_action_text(recovery),
        )
    if phase == "SENDING":
        return StatusPresentation(
            "续接发送",
            "正在发送续接消息；尚未确认进入原会话，不视为已恢复",
            "recovering",
            show_recovery_bar=True,
            highlight_step_index=3,
        )
    if phase == "AWAITING_PROGRESS":
        return StatusPresentation(
            "续接待进展",
            "消息已进入原会话通道，仍需等待真实新进展",
            "recovering",
            show_recovery_bar=True,
            highlight_step_index=4,
            next_action_text=_next_action_text(recovery),
        )
    if phase == "COOLDOWN":
        return StatusPresentation(
            "冷却中",
            f"连续 {recovery.consecutive_no_progress} 次未确认进展，暂停自动重试",
            "recovering",
            show_recovery_bar=True,
            highlight_step_index=6,
            next_action_text=_next_action_text(recovery),
        )
    if phase == "BLOCKED":
        reason = recovery.blocked_reason or "UNKNOWN"
        hint = action_hint(reason)
        reason_text = BLOCKED_REASON_LABELS.get(reason, "需要人工处理")
        detail = f"恢复被阻断：{reason_text}"
        if hint:
            detail += f"；{hint}"
        return StatusPresentation(
            "待人工处理",
            detail,
            "recovering",
            show_recovery_bar=True,
        )
    return StatusPresentation("正常执行", _task_detail(task, recovery), "success")


def progress_presentation(
    snapshot: ApplicationSnapshot,
) -> ProgressPresentation:
    """四段阶段条：只读 progress_stage / progress_stage_completed。

    严禁根据 task.state==COMPLETED 推断完成；COMPLETED 但结果交付未完成时，
    RESULT_DELIVERY 保持 current（不亮完），也不生成任何百分比。
    """
    stage_code = snapshot.progress_stage
    if stage_code not in PROGRESS_STAGES:
        stage_code = None
    completed = bool(snapshot.progress_stage_completed)

    stage_index = PROGRESS_STAGES.index(stage_code) if stage_code is not None else -1
    stages: list[ProgressStagePresentation] = []
    for idx, code in enumerate(PROGRESS_STAGES):
        label = PROGRESS_STEP_LABELS[code]
        if stage_code is None:
            state = "todo"
        elif idx < stage_index:
            state = "done"
        elif idx == stage_index:
            state = "done" if completed else "current"
        else:
            state = "todo"
        stages.append(ProgressStagePresentation(code=code, label=label, state=state))
    return ProgressPresentation(tuple(stages), stage_code=stage_code, completed=completed)


def counters(
    snapshot: ApplicationSnapshot,
) -> CountersPresentation:
    """恢复计数：『累计续接 N 次』与『连续未恢复 N 次』完全独立。

    累计按 task 跨 attempt 求和永不清零；连续仅真实进展后清零。
    不出现『连续失败 N 次 / 达到最大次数 / 停止续接 / 恢复耗尽』等文案。
    """
    recovery = snapshot.recovery
    total = recovery.resume_total if recovery is not None else 0
    consecutive = recovery.consecutive_no_progress if recovery is not None else 0
    return CountersPresentation(
        resume_total=total,
        consecutive_no_progress=consecutive,
        total_text=f"累计续接 {total} 次",
        consecutive_text=f"连续未恢复 {consecutive} 次",
    )


def connection_presentation(
    snapshot: ApplicationSnapshot,
) -> ConnectionPresentation:
    """模型/接口连接卡：来源 + 最后检测时间。

    T15 结构化 connection 优先；connection 缺失时回退 T14
    connection_healthy / connection_source。is_stale 为权威展示结论，
    过期时显示『状态可能已过期』并降到 recovering，绝不含糊为绿灯。
    """
    conn = snapshot.connection
    if conn is not None:
        observed = (
            f"最后检测 {format_timestamp(conn.last_observed_at)}"
            if conn.last_observed_at is not None
            else "检测时间未知"
        )
        source = conn.source or "未知来源"
        if conn.is_stale is True:
            return ConnectionPresentation(
                "连接状态可能已过期",
                f"{observed} · {source}",
                "recovering",
            )
        if conn.transport_ok is False or conn.payload_valid is False:
            return ConnectionPresentation(
                "连接不可用",
                f"{observed} · {source}",
                "danger",
            )
        if conn.transport_ok is True:
            return ConnectionPresentation(
                "模型/接口连接正常",
                f"{observed} · {source}",
                "success",
            )
        # 结构化存在但未给出明确结论 → 交给时间字段判断
        if conn.is_stale is None and conn.last_observed_at is None:
            return ConnectionPresentation(
                "连接状态未知",
                "尚未收到接口检测结果",
                "neutral",
            )

    if snapshot.connection_source is not None:
        if snapshot.connection_healthy:
            return ConnectionPresentation(
                "模型/接口连接正常",
                f"来源 {snapshot.connection_source}",
                "success",
            )
        return ConnectionPresentation(
            "接口未连接",
            f"来源 {snapshot.connection_source}",
            "recovering",
        )
    if snapshot.connection_healthy:
        return ConnectionPresentation("模型/接口连接正常", "连接正常", "success")
    return ConnectionPresentation("连接状态未知", "尚未连接模型/接口", "neutral")


def event_rows(
    snapshot: ApplicationSnapshot,
    max_rows: int = 6,
) -> tuple[EventRowPresentation, ...]:
    """近期事件：倒序（最新在前）、只截取最多 max_rows 条。

    事件只包含脱敏摘要；绝不展示完整正文/prompt/token/secret。
    【注意】max_rows 不允许小于 4——工作台右栏按规格展示 4–6 条。
    """
    if max_rows < 4:
        raise ValueError("event_rows 至少展示 4 条")
    ordered = sorted(
        snapshot.events,
        key=lambda e: e.occurred_at if e.occurred_at is not None else datetime.min,
        reverse=True,
    )
    rows: list[EventRowPresentation] = []
    for ev in ordered[:max_rows]:
        rows.append(
            EventRowPresentation(
                occurred_at=ev.occurred_at,
                event_code=ev.event_code,
                summary=ev.summary or "",
                tone=_tone(ev.tone),
                time_text=_clock(ev.occurred_at),
            )
        )
    return tuple(rows)


def action_hint(blocked_reason: str | None) -> str | None:
    if not blocked_reason:
        return None
    hint = BLOCKED_REASON_LABELS.get(blocked_reason)
    if hint is None:
        return "请检查任务状态后处理"
    if blocked_reason == "USER_ACTION":
        return None  # 文案已在 task_detail 中给出
    return hint


def queue_brief_rows(
    snapshot: ApplicationSnapshot,
    max_rows: int = 3,
) -> tuple[str, ...]:
    """队列摘要（工作台只列前 2–3 项；计数以 waiting_task_count 为准）。

    返回单行文本列表：『seq ### · 标题 · 状态』；不含阻塞原因细节。
    """
    items = snapshot.queue_brief[:max_rows]
    rows: list[str] = []
    for item in items:
        seq = f"seq {item.sequence}" if item.sequence is not None else "—"
        rows.append(f"{seq} · {item.title or item.task_id or '未知任务'} · {item.state or '未知状态'}")
    return tuple(rows)