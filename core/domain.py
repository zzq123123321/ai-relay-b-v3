"""V3 领域类型：四级身份、状态轴与不可变合同（T03）。

- 四种强类型 ID：TaskId / AttemptId / OperationId / ResultId，彼此不可混用。
- 状态轴以主规格 3.2 为准：
  Task = QUEUED / ACTIVE / BLOCKED / COMPLETED / FAILED / STOPPED_BY_USER（后三者终态）
  Attempt = OPEN / COMPLETED / FAILED / STOPPED / SUPERSEDED（OPEN 可多次中断/续接，其余终态不可原地复活）
  Operation = PREPARED / SENDING / ACCEPTED / REJECTED / UNKNOWN（UNKNOWN 不是失败，不能自动重发）
  Result = CANDIDATE / AUTHORITATIVE / SUPERSEDED / DELIVERED（同一任务可多个 ResultId，仅一个当前权威引用）
- Observation / Command / Decision 为不可变领域对象，决策不含副作用。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from core.errors import DomainError, ErrorCode


def _require_non_blank_id(cls_name: str, value: str) -> None:
    if not value or not value.strip():
        raise DomainError(
            ErrorCode.INVALID_ID,
            f"{cls_name} 不能为空字符串或纯空白：{value!r}",
        )


@dataclass(frozen=True, slots=True)
class TaskId:
    """A端原始业务任务身份，贯穿整个生命周期。"""

    value: str

    def __post_init__(self) -> None:
        _require_non_blank_id(type(self).__name__, self.value)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class AttemptId:
    """同一 Task 的一次执行尝试；显式新会话重试会生成新的 AttemptId。"""

    value: str

    def __post_init__(self) -> None:
        _require_non_blank_id(type(self).__name__, self.value)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class OperationId:
    """一次有副作用或可能有副作用的发送操作（初始发送、每次续接、建会话等）。"""

    value: str

    def __post_init__(self) -> None:
        _require_non_blank_id(type(self).__name__, self.value)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ResultId:
    """一个不可变的结果版本；提交后不覆盖，重复制复用同一版本。"""

    value: str

    def __post_init__(self) -> None:
        _require_non_blank_id(type(self).__name__, self.value)

    def __str__(self) -> str:
        return self.value


# ---------------------------------------------------------------------------
# 状态轴
# ---------------------------------------------------------------------------


class TaskStatus(Enum):
    """任务业务状态。COMPLETED / FAILED / STOPPED_BY_USER 为业务终态。"""

    QUEUED = "queued"
    ACTIVE = "active"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED_BY_USER = "stopped_by_user"

    def is_terminal(self) -> bool:
        return self in _TASK_TERMINAL


class AttemptStatus(Enum):
    """一次执行尝试的状态。仅 OPEN 可继续；其余为终态，不可原地复活。"""

    OPEN = "open"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    SUPERSEDED = "superseded"

    def is_terminal(self) -> bool:
        return self in _ATTEMPT_TERMINAL


class OperationStatus(Enum):
    """一次发送操作的状态。UNKNOWN 表示无法确认远端是否收到，不是失败，不能自动重发；需对账。"""

    PREPARED = "prepared"
    SENDING = "sending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"

    def is_terminal(self) -> bool:
        return self in _OPERATION_TERMINAL


class ResultStatus(Enum):
    """结果版本来源：候选 / 权威 / 被替代 / 最终可交付。"""

    CANDIDATE = "candidate"
    AUTHORITATIVE = "authoritative"
    SUPERSEDED = "superseded"
    DELIVERED = "delivered"


# ---------------------------------------------------------------------------
# 状态转移表（纯函数，终态吸收；叶达 worker 不能把终态复活）
# ---------------------------------------------------------------------------

_TASK_TERMINAL = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.STOPPED_BY_USER}
)
_ATTEMPT_TERMINAL = frozenset(
    {
        AttemptStatus.COMPLETED,
        AttemptStatus.FAILED,
        AttemptStatus.STOPPED,
        AttemptStatus.SUPERSEDED,
    }
)
_OPERATION_TERMINAL = frozenset({OperationStatus.ACCEPTED, OperationStatus.REJECTED})

_TASK_TRANSITIONS: Mapping[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset(
        {TaskStatus.ACTIVE, TaskStatus.BLOCKED, TaskStatus.STOPPED_BY_USER}
    ),
    TaskStatus.ACTIVE: frozenset(
        {
            TaskStatus.BLOCKED,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.STOPPED_BY_USER,
        }
    ),
    TaskStatus.BLOCKED: frozenset(
        {TaskStatus.ACTIVE, TaskStatus.COMPLETED, TaskStatus.STOPPED_BY_USER}
    ),
    # 终态（COMPLETED / FAILED / STOPPED_BY_USER）没有出边，不可复活。
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.STOPPED_BY_USER: frozenset(),
}

_ATTEMPT_TRANSITIONS: Mapping[AttemptStatus, frozenset[AttemptStatus]] = {
    AttemptStatus.OPEN: frozenset(
        {
            AttemptStatus.COMPLETED,
            AttemptStatus.FAILED,
            AttemptStatus.STOPPED,
            AttemptStatus.SUPERSEDED,
        }
    ),
    # 终态吸收。
    AttemptStatus.COMPLETED: frozenset(),
    AttemptStatus.FAILED: frozenset(),
    AttemptStatus.STOPPED: frozenset(),
    AttemptStatus.SUPERSEDED: frozenset(),
}

_OPERATION_TRANSITIONS: Mapping[OperationStatus, frozenset[OperationStatus]] = {
    OperationStatus.PREPARED: frozenset({OperationStatus.SENDING}),
    OperationStatus.SENDING: frozenset(
        {OperationStatus.ACCEPTED, OperationStatus.REJECTED, OperationStatus.UNKNOWN}
    ),
    OperationStatus.UNKNOWN: frozenset(
        {OperationStatus.ACCEPTED, OperationStatus.REJECTED}
    ),
    # ACCEPTED / REJECTED 吸收；UNKNOWN 可经对账收敛到两者之一，但不能自动重发。
    OperationStatus.ACCEPTED: frozenset(),
    OperationStatus.REJECTED: frozenset(),
}


def transition_task(current: TaskStatus, next_state: TaskStatus) -> TaskStatus:
    if next_state not in _TASK_TRANSITIONS[current]:
        raise DomainError(
            ErrorCode.INVALID_TRANSITION,
            f"任务状态不可从 {current.value} 转到 {next_state.value}（终态不可复活）",
        )
    return next_state


def transition_attempt(current: AttemptStatus, next_state: AttemptStatus) -> AttemptStatus:
    if next_state not in _ATTEMPT_TRANSITIONS[current]:
        raise DomainError(
            ErrorCode.INVALID_TRANSITION,
            f"执行尝试状态不可从 {current.value} 转到 {next_state.value}（终态不可复活）",
        )
    return next_state


def transition_operation(
    current: OperationStatus, next_state: OperationStatus
) -> OperationStatus:
    if next_state not in _OPERATION_TRANSITIONS[current]:
        raise DomainError(
            ErrorCode.INVALID_TRANSITION,
            f"发送操作状态不可从 {current.value} 转到 {next_state.value}（UNKNOWN 需对账而非自动重发）",
        )
    return next_state


def require_active_attempt(status: AttemptStatus) -> None:
    """普通迟到 worker 不得操作已进入终态的旧 Attempt。"""
    if status.is_terminal():
        raise DomainError(
            ErrorCode.STALE_ATTEMPT,
            f"执行尝试已处于终态 {status.value}，不能原地复活；如需继续应创建新的 AttemptId",
        )


# ---------------------------------------------------------------------------
# 结果：不可变版本 + 当前权威引用（仅类型，不落库）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Result:
    """一个不可变的结果版本。revision 从 1 递增，SUPERSEDED 不做覆盖。"""

    task_id: TaskId
    attempt_id: AttemptId
    result_id: ResultId
    revision: int
    status: ResultStatus
    created_at: datetime
    summary: str = ""

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise DomainError(
                ErrorCode.INVALID_RESULT,
                f"结果 revision 必须 >= 1，收到 {self.revision}",
            )

    def is_authoritative(self) -> bool:
        return self.status is ResultStatus.AUTHORITATIVE


@dataclass(frozen=True, slots=True)
class AuthoritativeResultRef:
    """任务的“当前权威结果引用”。任务可有多个 ResultId，但只指向一个权威版本。"""

    task_id: TaskId
    result_id: ResultId
    revision: int
    authority_epoch: int

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise DomainError(
                ErrorCode.INVALID_RESULT,
                f"权威结果引用 revision 必须 >= 1，收到 {self.revision}",
            )
        if self.authority_epoch < 1:
            raise DomainError(
                ErrorCode.INVALID_RESULT,
                f"authority_epoch 必须 >= 1，收到 {self.authority_epoch}",
            )


# ---------------------------------------------------------------------------
# 不可变领域合同：Observation / Command / Decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Observation:
    """一次事实观察（网络不可达、执行端状态变化、消息/工具进展等）。

    只描述“发生了什么”，不含“接下来做什么”。但可携带 deadline 对账所需的时钟信息。
    """

    observation_id: str
    task_id: TaskId
    attempt_id: AttemptId
    source: str
    kind: str
    observed_at: datetime
    payload: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not self.observation_id or not self.observation_id.strip():
            raise DomainError(
                ErrorCode.INVALID_OBSERVATION,
                f"observation_id 不能为空或纯空白：{self.observation_id!r}",
            )
        if not self.kind or not self.kind.strip():
            raise DomainError(
                ErrorCode.INVALID_OBSERVATION,
                f"Observation.kind 不能为空或纯空白：{self.kind!r}",
            )
        if not self.source or not self.source.strip():
            raise DomainError(
                ErrorCode.INVALID_OBSERVATION,
                f"Observation.source 不能为空或纯空白：{self.source!r}",
            )


class CommandKind(str, Enum):
    """已定义但本任务仅建模、不执行。"""

    START_ATTEMPT = "start_attempt"
    SEND_INITIAL = "send_initial"
    SEND_CONTINUE = "send_continue"
    STOP_LOCAL_WAIT = "stop_local_wait"
    MANUAL_COMPLETE = "manual_complete"


@dataclass(frozen=True, slots=True)
class Command:
    """一次“准备执行的动作”描述；不含执行副作用。"""

    command_id: str
    task_id: TaskId
    attempt_id: AttemptId
    kind: CommandKind
    created_at: datetime
    operation_id: str | None = None
    payload: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not self.command_id or not self.command_id.strip():
            raise DomainError(
                ErrorCode.INVALID_COMMAND,
                f"command_id 不能为空或纯空白：{self.command_id!r}",
            )


class DecisionAction(str, Enum):
    """根据观察作出的纯决策动作。"""

    WAIT = "wait"
    CONTINUE_OBSERVING = "continue_observing"
    PROPOSE_CONTINUE = "propose_continue"
    BLOCK = "block"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class Decision:
    """纯决策结果。不直接执行网络/sleep/写入；后续由协调器转成 Command。"""

    decision_id: str
    task_id: TaskId
    attempt_id: AttemptId
    action: DecisionAction
    decided_at: datetime
    reason_code: str | None = None
    next_check_at: datetime | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.decision_id or not self.decision_id.strip():
            raise DomainError(
                ErrorCode.INVALID_DECISION,
                f"decision_id 不能为空或纯空白：{self.decision_id!r}",
            )