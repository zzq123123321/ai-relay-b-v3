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
from dataclasses import dataclass, fields, is_dataclass
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


# ---------------------------------------------------------------------------
# T05：配置领域。草稿（可变输入）绝不直接成为生效引用；
# SettingsSnapshot 是不可变生效快照，由 SettingsService 校验并持久提交后发布。
# ---------------------------------------------------------------------------

class TargetExecutor(str, Enum):
    """真实执行端。default_target 只能是这类真实执行端，不能是抽象占位符 EXECUTOR。"""

    OPENCHAMBER = "OPENCHAMBER"
    REASONIX = "REASONIX"


class SessionBindingMode(str, Enum):
    """会话绑定模式（主规格 6.2）。命名与附录A 可读样例保持一致（FIXED_SESSION）。"""

    FIXED_SESSION = "FIXED_SESSION"
    PROJECT_ROTATING = "PROJECT_ROTATING"


class BusyResumePolicy(str, Enum):
    CONFIRMED_IDLE_ONLY = "confirmed_idle_only"
    VERIFIED_QUEUE_ONCE = "verified_queue_once"


class TransportProfile(str, Enum):
    LEGACY_V1 = "legacy_v1"
    RELIABLE_V2 = "reliable_v2"


class UiTheme(str, Enum):
    LIGHT = "light"
    DARK = "dark"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class OpenChamberConfig:
    url: str
    directory: str
    session_id: str
    session_policy: SessionBindingMode
    agent: str
    model: str
    capability_profile: str
    auto_open_session: bool


@dataclass(frozen=True, slots=True)
class RecoveryConfig:
    automatic_resume: bool
    resume_after_restart: bool
    completion_timeout_seconds: float
    poll_interval_seconds: float
    completion_grace_seconds: float
    idle_confirmations: int
    recovery_delay_seconds: float
    awaiting_progress_seconds: float
    cumulative_resume_limit: int | None
    no_progress_cooldown_threshold: int
    cooldown_seconds: float
    fast_read_retry_delays_seconds: tuple[float, ...]
    sustained_read_delays_seconds: tuple[float, ...]
    busy_stale_log_seconds: float
    busy_review_seconds: float
    busy_resume_policy: BusyResumePolicy
    resume_truncated_output: bool
    prompt_version: str


@dataclass(frozen=True, slots=True)
class HttpConfig:
    connect_timeout_seconds: float
    read_timeout_seconds: float
    operation_budget_seconds: float
    automatic_post_retries: int


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    model_probe_target: str
    interval_seconds: float
    recovery_successes: int
    zerotier_enabled: bool
    external_bridge_enabled: bool
    external_bridge_path: str
    sample_ttl_seconds: float


@dataclass(frozen=True, slots=True)
class RotationConfig:
    enabled: bool
    success_threshold: int
    inherit_auto_accept: bool


@dataclass(frozen=True, slots=True)
class DeliveryConfig:
    profile: TransportProfile
    automatic_ack: bool
    max_unconfirmed_offers: int


@dataclass(frozen=True, slots=True)
class LimitsConfig:
    incoming_body_bytes: int
    incoming_envelope_bytes: int
    queued_tasks: int


@dataclass(frozen=True, slots=True)
class LogsConfig:
    event_retention_days: int
    event_budget_bytes: int
    debug_enabled: bool
    debug_file_bytes: int
    debug_files: int
    debug_retention_days: int
    include_task_body: bool
    include_model_response: bool


@dataclass(frozen=True, slots=True)
class UiConfig:
    theme: UiTheme
    density: str
    always_on_top: bool
    close_action: str
    auto_start_with_windows: bool
    quit_warning_seconds: float


@dataclass(frozen=True, slots=True)
class ConfigBody:
    """一份经过校验、可整体持久化的配置正文。全部叶子字段已通过 SettingsService 校验。"""

    schema_version: int
    default_target: TargetExecutor
    openchamber: OpenChamberConfig
    recovery: RecoveryConfig
    http: HttpConfig
    network: NetworkConfig
    rotation: RotationConfig
    delivery: DeliveryConfig
    limits: LimitsConfig
    logs: LogsConfig
    ui: UiConfig


@dataclass(frozen=True, slots=True)
class SettingsSnapshot:
    """已持久提交的生效配置快照。不可变；发布前必须已写入 revision。"""

    revision: int
    created_at: str
    config: ConfigBody

    def __post_init__(self) -> None:
        if not isinstance(self.revision, int) or isinstance(self.revision, bool):
            raise DomainError(ErrorCode.INVALID_RESULT, f"revision 必须是整数：{self.revision!r}")
        if self.revision < 1:
            raise DomainError(ErrorCode.INVALID_RESULT, f"revision 必须 >=1：{self.revision!r}")


class SettingsDraft(dict):
    """用户正在编辑、尚未生效的配置草稿（可变的输入载体）。

    仅可用于向 SettingsService.submit_draft 提交；禁止把草稿本身用作运行时引用。
    支持 from_mapping/defaults 快速构造与 dict 常规修改。
    """

    @classmethod
    def from_mapping(cls, mapping: Mapping) -> "SettingsDraft":
        return cls(mapping)

    @classmethod
    def defaults(cls) -> "SettingsDraft":
        """附录A 默认样例行（default_settings_v3.json）对应的草稿。"""
        return cls.from_mapping(
            {
                "schema_version": 1,
                "default_target": "OPENCHAMBER",
                "openchamber": {
                    "url": "http://127.0.0.1:57123",
                    "directory": "",
                    "session_id": "",
                    "session_policy": "FIXED_SESSION",
                    "agent": "build",
                    "model": "",
                    "capability_profile": "UNVERIFIED",
                    "auto_open_session": True,
                },
                "recovery": {
                    "automatic_resume": True,
                    "resume_after_restart": True,
                    "completion_timeout_seconds": 0,
                    "poll_interval_seconds": 2,
                    "completion_grace_seconds": 5,
                    "idle_confirmations": 3,
                    "recovery_delay_seconds": 10,
                    "awaiting_progress_seconds": 30,
                    "cumulative_resume_limit": None,
                    "no_progress_cooldown_threshold": 3,
                    "cooldown_seconds": 60,
                    "fast_read_retry_delays_seconds": [3, 8],
                    "sustained_read_delays_seconds": [15, 30],
                    "busy_stale_log_seconds": 30,
                    "busy_review_seconds": 120,
                    "busy_resume_policy": "confirmed_idle_only",
                    "resume_truncated_output": True,
                    "prompt_version": "continue_zh_v1",
                },
                "http": {
                    "connect_timeout_seconds": 3,
                    "read_timeout_seconds": 10,
                    "operation_budget_seconds": 20,
                    "automatic_post_retries": 0,
                },
                "network": {
                    "model_probe_target": "",
                    "interval_seconds": 5,
                    "recovery_successes": 2,
                    "zerotier_enabled": False,
                    "external_bridge_enabled": False,
                    "external_bridge_path": "",
                    "sample_ttl_seconds": 20,
                },
                "rotation": {"enabled": False, "success_threshold": 5, "inherit_auto_accept": False},
                "delivery": {
                    "profile": "legacy_v1",
                    "automatic_ack": False,
                    "max_unconfirmed_offers": 1,
                },
                "limits": {
                    "incoming_body_bytes": 2097152,
                    "incoming_envelope_bytes": 4194304,
                    "queued_tasks": 1000,
                },
                "logs": {
                    "event_retention_days": 30,
                    "event_budget_bytes": 209715200,
                    "debug_enabled": False,
                    "debug_file_bytes": 10485760,
                    "debug_files": 5,
                    "debug_retention_days": 7,
                    "include_task_body": False,
                    "include_model_response": False,
                },
                "ui": {
                    "theme": "light",
                    "density": "comfortable",
                    "always_on_top": False,
                    "close_action": "hide_to_tray",
                    "auto_start_with_windows": False,
                    "quit_warning_seconds": 15,
                },
            }
        )


@dataclass(frozen=True, slots=True)
class ReceiveSettingsSnapshot:
    """任务接收时冻结的配置语义（主规格 6.2）。此后配置变化不改变排队任务本快照。"""

    config_revision: int
    committed_at: str
    received_at: str
    effective_executor: TargetExecutor
    directory: str
    project_key: str
    agent: str
    requested_model: str
    binding_mode: SessionBindingMode
    frozen_session_id: str | None = None

    def __post_init__(self) -> None:
        if self.binding_mode is SessionBindingMode.FIXED_SESSION:
            if self.frozen_session_id is None:
                raise DomainError(ErrorCode.INVALID_RESULT, "FIXED_SESSION 接收快照必须冻结 session_id")


@dataclass(frozen=True, slots=True)
class ExecutionSettingsSnapshot:
    """开始执行时解析的不可变执行快照。整个 Attempt 使用同一份；不受之后配置修改影响。"""

    receive: ReceiveSettingsSnapshot
    binding_revision: int
    resolved_session_id: str | None
    execution_started_at: str


def config_to_dict(config: ConfigBody) -> dict:
    """把 ConfigBody 序列化为可 JSON 的嵌套字典；Enum 输出其稳定字符串值。"""

    def _jsonable(value: object) -> object:
        if isinstance(value, Enum):
            return value.value
        if is_dataclass(value) and not isinstance(value, type):
            return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
        if isinstance(value, Mapping):
            return {str(k): _jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_jsonable(item) for item in value]
        return value

    return _jsonable(config)  # type: ignore[return-value]