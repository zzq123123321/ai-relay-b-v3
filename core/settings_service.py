"""AI Relay B V3.0：配置草稿校验、原子提交与不可变快照服务（T05）。

权威顺序（主规格 6.1）：表单草稿 → 完整校验 → 数据库事务提交新 revision → 提交成功 → 才发布新快照。
- 校验失败：根本不进入数据库写事务，current 引用与数据库都不变。
- 提交失败（含 CAS 冲突 / SQL 中途失败）：事务整体 ROLLBACK，current 引用保持旧快照对象与值。
- 禁止“先改内存再保存”：SettingsService 只维护已提交快照，从不持有草稿作为生效配置。

接收/执行快照分离（主规格 6.2）：接收时冻结 effective_executor、project、配置 revision、
agent/model 与会话策略；执行时冻结 endpoint/directory/session/策略等。当前 Attempt 使用同一份
不可变 ExecutionSettingsSnapshot，不随后续配置修改而改变。
"""

from __future__ import annotations

import math
import os
import urllib.parse
from datetime import timezone
from typing import Any, Mapping

from core.domain import (
    BusyResumePolicy,
    ConfigBody,
    DeliveryConfig,
    ExecutionSettingsSnapshot,
    HttpConfig,
    LimitsConfig,
    LogsConfig,
    NetworkConfig,
    OpenChamberConfig,
    ReceiveSettingsSnapshot,
    RecoveryConfig,
    RotationConfig,
    SessionBindingMode,
    SettingsDraft,
    SettingsSnapshot,
    TargetExecutor,
    TransportProfile,
    UiConfig,
    UiTheme,
)
from infra.clock import Clock, SystemClock


class SettingsError(Exception):
    """配置领域可预期错误基类（稳定机器码 code + 中文 message）。"""

    code = "settings_error"

    def __init__(self, message: str, *, context: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.context = dict(context or {})

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


class SettingsValidationError(SettingsError):
    """草稿校验失败：不进入数据库写事务。"""

    code = "settings_validation"


class SettingsCommitError(SettingsError):
    """持久提交失败（数据库层或并发 CAS）。"""

    code = "settings_commit"


# ---------------------------------------------------------------------------
# 数值界值：主规格 6.3 要求浮点有限且上下界合法。实现界值以附录A默认在范围内为准，
# 属于 T05 实施界值；A 端如需调整可改此处并同步测试。
# ---------------------------------------------------------------------------

_FIELD_BOUNDS: dict[str, tuple[float, float]] = {
    "completion_timeout_seconds": (0.0, 86400.0),
    "poll_interval_seconds": (0.05, 300.0),
    "completion_grace_seconds": (0.05, 3600.0),
    "recovery_delay_seconds": (0.0, 86400.0),
    "awaiting_progress_seconds": (0.0, 86400.0),
    "cooldown_seconds": (0.0, 86400.0),
    "busy_stale_log_seconds": (0.0, 86400.0),
    "busy_review_seconds": (0.0, 86400.0),
    "connect_timeout_seconds": (0.1, 300.0),
    "read_timeout_seconds": (0.1, 3600.0),
    "operation_budget_seconds": (0.0, 86400.0),
    "interval_seconds": (0.05, 3600.0),
    "sample_ttl_seconds": (1.0, 86400.0),
    "quit_warning_seconds": (0.0, 36000.0),
}

_RETRY_LIST_BOUNDS = (0.0, 600.0)


def _fail(name: str, reason: str) -> None:
    raise SettingsValidationError(f"配置项 {name!r} {reason}")


def _require_section(draft: Mapping, name: str) -> Mapping:
    value = draft.get(name)
    if not isinstance(value, Mapping):
        _fail(name, "缺少或不是对象")
    return value


def _clean_str(value: Any, name: str, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        _fail(name, f"必须是字符串，实际为 {type(value).__name__}")
    stripped = value.strip()
    if not allow_empty and not stripped:
        _fail(name, "不能为空字符串或纯空格")
    return stripped


def _clean_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        _fail(name, f"必须是布尔值，实际为 {type(value).__name__}")
    return value


def _clean_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        _fail(name, "不允许把布尔值当作整数（True/False 不能伪装为 1/0）")
    if not isinstance(value, int):
        _fail(name, f"必须是整数，实际为 {type(value).__name__}")
    if not (minimum <= value <= maximum):
        _fail(name, f"超出范围 [{minimum}, {maximum}]：{value}")
    return value


def _clean_float(value: Any, name: str) -> float:
    if isinstance(value, bool):
        _fail(name, "不允许把布尔值当作数值（True/False 不能伪装为 1/0）")
    if not isinstance(value, (int, float)):
        _fail(name, f"必须是数值，实际为 {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        _fail(name, f"必须是有限数，实际为 {number!r}（拒绝 NaN / +/-Infinity）")
    bare = name.rsplit(".", 1)[-1]
    if bare in _FIELD_BOUNDS:
        low, high = _FIELD_BOUNDS[bare]
        if not (low <= number <= high):
            _fail(name, f"超出范围 [{low}, {high}]：{number}")
    return number


def _clean_optional_int(value: Any, name: str, *, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    return _clean_int(value, name, minimum=minimum, maximum=maximum)


def _clean_enum(value: Any, name: str, allowlist: tuple[str, ...]) -> str:
    if isinstance(value, str):
        candidate = value
    elif hasattr(value, "value") and isinstance(value.value, str):
        candidate = value.value
    else:
        _fail(name, f"必须是允许的枚举值之一 {allowlist}，实际为 {value!r}")
    if candidate not in allowlist:
        _fail(name, f"不在允许集合 {allowlist} 内：{candidate!r}")
    return candidate


def _clean_retry_list(value: Any, name: str) -> tuple[float, ...]:
    """重试延迟序列：list[float]，元素有限且在范围内。"""
    if not isinstance(value, (list, tuple)) or not value:
        _fail(name, "必须是非空数组")
    cleaned: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            _fail(name, f"元素必须是数值，实际为 {item!r}")
        number = float(item)
        if not math.isfinite(number):
            _fail(name, f"元素必须是有限数，实际为 {number!r}")
        low, high = _RETRY_LIST_BOUNDS
        if not (low <= number <= high):
            _fail(name, f"元素超出范围 [{low}, {high}]：{number}")
        cleaned.append(number)
    return tuple(cleaned)


def _clean_http_url(value: Any, name: str, *, allow_empty: bool) -> str:
    raw = _clean_str(value, name, allow_empty=allow_empty)
    if not raw:
        return ""
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError as exc:
        _fail(name, f"URL 无法解析：{exc}")
    if parts.scheme not in ("http", "https"):
        _fail(name, f"只允许 http/https，实际 scheme 为 {parts.scheme!r}")
    if parts.username is not None or parts.password is not None:
        _fail(name, "URL 内不允许携带用户名/密码")
    if not parts.hostname:
        _fail(name, f"缺少主机名：{raw!r}")
    return raw


def _clean_project_directory(value: Any, name: str, *, allow_empty: bool) -> str:
    raw = _clean_str(value, name, allow_empty=allow_empty)
    if not raw:
        return ""
    normalized = os.path.normpath(raw)
    if not os.path.isabs(normalized):
        _fail(name, f"必须是绝对路径：{raw!r}（相对路径按非法目录拒绝）")
    if not os.path.exists(normalized):
        _fail(name, f"目录不存在：{normalized!r}")
    if not os.path.isdir(normalized):
        _fail(name, f"存在但不是目录（是普通文件）：{normalized!r}")
    return normalized


def _build_openchamber(raw: Mapping) -> OpenChamberConfig:
    section = _require_section(raw, "openchamber")
    return OpenChamberConfig(
        url=_clean_http_url(section.get("url"), "openchamber.url", allow_empty=False),
        directory=_clean_project_directory(
            section.get("directory"), "openchamber.directory", allow_empty=True
        ),
        session_id=_clean_str(section.get("session_id"), "openchamber.session_id", allow_empty=True),
        session_policy=SessionBindingMode(
            _clean_enum(
                section.get("session_policy"),
                "openchamber.session_policy",
                allowlist=(SessionBindingMode.FIXED_SESSION.value, SessionBindingMode.PROJECT_ROTATING.value),
            )
        ),
        agent=_clean_str(section.get("agent"), "openchamber.agent", allow_empty=False),
        model=_clean_str(section.get("model"), "openchamber.model", allow_empty=True),
        capability_profile=_clean_str(
            section.get("capability_profile"), "openchamber.capability_profile", allow_empty=False
        ),
        auto_open_session=_clean_bool(
            section.get("auto_open_session"), "openchamber.auto_open_session"
        ),
    )


def _build_recovery(raw: Mapping) -> RecoveryConfig:
    section = _require_section(raw, "recovery")
    return RecoveryConfig(
        automatic_resume=_clean_bool(section.get("automatic_resume"), "recovery.automatic_resume"),
        resume_after_restart=_clean_bool(
            section.get("resume_after_restart"), "recovery.resume_after_restart"
        ),
        completion_timeout_seconds=_clean_float(
            section.get("completion_timeout_seconds"), "recovery.completion_timeout_seconds"
        ),
        poll_interval_seconds=_clean_float(
            section.get("poll_interval_seconds"), "recovery.poll_interval_seconds"
        ),
        completion_grace_seconds=_clean_float(
            section.get("completion_grace_seconds"), "recovery.completion_grace_seconds"
        ),
        idle_confirmations=_clean_int(
            section.get("idle_confirmations"), "recovery.idle_confirmations", minimum=1, maximum=100
        ),
        recovery_delay_seconds=_clean_float(
            section.get("recovery_delay_seconds"), "recovery.recovery_delay_seconds"
        ),
        awaiting_progress_seconds=_clean_float(
            section.get("awaiting_progress_seconds"), "recovery.awaiting_progress_seconds"
        ),
        cumulative_resume_limit=_clean_optional_int(
            section.get("cumulative_resume_limit"),
            "recovery.cumulative_resume_limit",
            minimum=1,
            maximum=1000000,
        ),
        no_progress_cooldown_threshold=_clean_int(
            section.get("no_progress_cooldown_threshold"),
            "recovery.no_progress_cooldown_threshold",
            minimum=1,
            maximum=1000,
        ),
        cooldown_seconds=_clean_float(section.get("cooldown_seconds"), "recovery.cooldown_seconds"),
        fast_read_retry_delays_seconds=_clean_retry_list(
            section.get("fast_read_retry_delays_seconds"), "recovery.fast_read_retry_delays_seconds"
        ),
        sustained_read_delays_seconds=_clean_retry_list(
            section.get("sustained_read_delays_seconds"), "recovery.sustained_read_delays_seconds"
        ),
        busy_stale_log_seconds=_clean_float(
            section.get("busy_stale_log_seconds"), "recovery.busy_stale_log_seconds"
        ),
        busy_review_seconds=_clean_float(
            section.get("busy_review_seconds"), "recovery.busy_review_seconds"
        ),
        busy_resume_policy=BusyResumePolicy(
            _clean_enum(
                section.get("busy_resume_policy"),
                "recovery.busy_resume_policy",
                allowlist=(BusyResumePolicy.CONFIRMED_IDLE_ONLY.value, BusyResumePolicy.VERIFIED_QUEUE_ONCE.value),
            )
        ),
        resume_truncated_output=_clean_bool(
            section.get("resume_truncated_output"), "recovery.resume_truncated_output"
        ),
        prompt_version=_clean_str(
            section.get("prompt_version"), "recovery.prompt_version", allow_empty=False
        ),
    )


def _build_http(raw: Mapping) -> HttpConfig:
    section = _require_section(raw, "http")
    return HttpConfig(
        connect_timeout_seconds=_clean_float(
            section.get("connect_timeout_seconds"), "http.connect_timeout_seconds"
        ),
        read_timeout_seconds=_clean_float(
            section.get("read_timeout_seconds"), "http.read_timeout_seconds"
        ),
        operation_budget_seconds=_clean_float(
            section.get("operation_budget_seconds"), "http.operation_budget_seconds"
        ),
        automatic_post_retries=_clean_int(
            section.get("automatic_post_retries"), "http.automatic_post_retries", minimum=0, maximum=100
        ),
    )


def _build_network(raw: Mapping) -> NetworkConfig:
    section = _require_section(raw, "network")
    return NetworkConfig(
        model_probe_target=_clean_http_url(
            section.get("model_probe_target"), "network.model_probe_target", allow_empty=True
        ),
        interval_seconds=_clean_float(section.get("interval_seconds"), "network.interval_seconds"),
        recovery_successes=_clean_int(
            section.get("recovery_successes"), "network.recovery_successes", minimum=1, maximum=100
        ),
        zerotier_enabled=_clean_bool(section.get("zerotier_enabled"), "network.zerotier_enabled"),
        external_bridge_enabled=_clean_bool(
            section.get("external_bridge_enabled"), "network.external_bridge_enabled"
        ),
        external_bridge_path=_clean_project_directory(
            section.get("external_bridge_path"), "network.external_bridge_path", allow_empty=True
        ),
        sample_ttl_seconds=_clean_float(section.get("sample_ttl_seconds"), "network.sample_ttl_seconds"),
    )


def _build_rotation(raw: Mapping) -> RotationConfig:
    section = _require_section(raw, "rotation")
    return RotationConfig(
        enabled=_clean_bool(section.get("enabled"), "rotation.enabled"),
        success_threshold=_clean_int(
            section.get("success_threshold"), "rotation.success_threshold", minimum=1, maximum=100000
        ),
        inherit_auto_accept=_clean_bool(
            section.get("inherit_auto_accept"), "rotation.inherit_auto_accept"
        ),
    )


def _build_delivery(raw: Mapping) -> DeliveryConfig:
    section = _require_section(raw, "delivery")
    return DeliveryConfig(
        profile=TransportProfile(
            _clean_enum(
                section.get("profile"),
                "delivery.profile",
                allowlist=(TransportProfile.LEGACY_V1.value, TransportProfile.RELIABLE_V2.value),
            )
        ),
        automatic_ack=_clean_bool(section.get("automatic_ack"), "delivery.automatic_ack"),
        max_unconfirmed_offers=_clean_int(
            section.get("max_unconfirmed_offers"), "delivery.max_unconfirmed_offers", minimum=1, maximum=1000
        ),
    )


def _build_limits(raw: Mapping) -> LimitsConfig:
    section = _require_section(raw, "limits")
    body = _clean_int(
        section.get("incoming_body_bytes"), "limits.incoming_body_bytes", minimum=1, maximum=2**40
    )
    envelope = _clean_int(
        section.get("incoming_envelope_bytes"),
        "limits.incoming_envelope_bytes",
        minimum=1,
        maximum=2**40,
    )
    if envelope < body:
        _fail("limits.incoming_envelope_bytes", f"必须 >= incoming_body_bytes（{body}），实际 {envelope}")
    return LimitsConfig(
        incoming_body_bytes=body,
        incoming_envelope_bytes=envelope,
        queued_tasks=_clean_int(
            section.get("queued_tasks"), "limits.queued_tasks", minimum=1, maximum=100000
        ),
    )


def _build_logs(raw: Mapping) -> LogsConfig:
    section = _require_section(raw, "logs")
    return LogsConfig(
        event_retention_days=_clean_int(
            section.get("event_retention_days"), "logs.event_retention_days", minimum=1, maximum=3650
        ),
        event_budget_bytes=_clean_int(
            section.get("event_budget_bytes"), "logs.event_budget_bytes", minimum=1, maximum=2**40
        ),
        debug_enabled=_clean_bool(section.get("debug_enabled"), "logs.debug_enabled"),
        debug_file_bytes=_clean_int(
            section.get("debug_file_bytes"), "logs.debug_file_bytes", minimum=1, maximum=2**40
        ),
        debug_files=_clean_int(section.get("debug_files"), "logs.debug_files", minimum=1, maximum=100),
        debug_retention_days=_clean_int(
            section.get("debug_retention_days"), "logs.debug_retention_days", minimum=1, maximum=3650
        ),
        include_task_body=_clean_bool(section.get("include_task_body"), "logs.include_task_body"),
        include_model_response=_clean_bool(
            section.get("include_model_response"), "logs.include_model_response"
        ),
    )


def _build_ui(raw: Mapping) -> UiConfig:
    section = _require_section(raw, "ui")
    return UiConfig(
        theme=UiTheme(
            _clean_enum(
                section.get("theme"), "ui.theme", allowlist=tuple(t.value for t in UiTheme)
            )
        ),
        density=_clean_str(section.get("density"), "ui.density", allow_empty=False),
        always_on_top=_clean_bool(section.get("always_on_top"), "ui.always_on_top"),
        close_action=_clean_str(section.get("close_action"), "ui.close_action", allow_empty=False),
        auto_start_with_windows=_clean_bool(
            section.get("auto_start_with_windows"), "ui.auto_start_with_windows"
        ),
        quit_warning_seconds=_clean_float(
            section.get("quit_warning_seconds"), "ui.quit_warning_seconds"
        ),
    )


def validate_config(draft: SettingsDraft | Mapping) -> ConfigBody:
    """把草稿整体校验并构造为不可变 ConfigBody。

    任一项不合格抛 SettingsValidationError；本函数不写数据库。
    """
    if not isinstance(draft, Mapping):
        raise SettingsValidationError(f"草稿必须是映射结构，实际为 {type(draft).__name__}")

    default_target_raw = _clean_enum(
        draft.get("default_target"),
        "default_target",
        allowlist=tuple(e.value for e in TargetExecutor),
    )

    schema_version = _clean_int(
        draft.get("schema_version"), "schema_version", minimum=1, maximum=1
    )

    return ConfigBody(
        schema_version=schema_version,
        default_target=TargetExecutor(default_target_raw),
        openchamber=_build_openchamber(draft),
        recovery=_build_recovery(draft),
        http=_build_http(draft),
        network=_build_network(draft),
        rotation=_build_rotation(draft),
        delivery=_build_delivery(draft),
        limits=_build_limits(draft),
        logs=_build_logs(draft),
        ui=_build_ui(draft),
    )


def _project_key_of(directory: str) -> str:
    """目录规范化 key（Windows normcase + normpath）。空目录以空字符串表示默认项目。"""
    if not directory:
        return ""
    return os.path.normcase(os.path.normpath(directory))


def _utc_iso(value: Any) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


class SettingsService:
    """配置的运行期门面：校验草稿、原子提交、发布只读快照、构建接收/执行快照。"""

    def __init__(self, store: Any, *, clock: Clock | None = None) -> None:
        self._store = store
        self._clock = clock if clock is not None else SystemClock()
        self._current: SettingsSnapshot | None = store.load_current()

    @property
    def current(self) -> SettingsSnapshot | None:
        """当前唯一生效的只读快照；从未提交时为 None。"""
        return self._current

    def submit_draft(
        self,
        draft: SettingsDraft,
        *,
        base_revision: int | None,
        actor: str = "user",
    ) -> SettingsSnapshot:
        """先完整校验，再 CAS 提交，提交成功后才发布新快照。

        校验失败 / 数据库失败 / CAS 冲突都只抛异常，self._current 保持原快照对象与值。
        """
        config = validate_config(draft)  # 校验失败：不进入数据库写事务
        created_at = _utc_iso(self._clock.now())
        try:
            snapshot = self._store.commit(
                base_revision=base_revision,
                config=config,
                created_at=created_at,
                actor=actor,
            )
        except SettingsError:
            raise
        except Exception as exc:  # noqa: BLE001 - 数据库层异常统一为提交失败
            raise SettingsCommitError(
                f"配置提交失败，当前生效配置保持不变：{exc}", context={"base_revision": base_revision}
            ) from exc
        self._current = snapshot  # 只有提交成功后才替换生效引用
        return snapshot

    # ---------------------------------------------------------------- 接收/执行快照

    def build_receive_snapshot(
        self,
        *,
        config: SettingsSnapshot | None = None,
        received_at: str | None = None,
    ) -> ReceiveSettingsSnapshot:
        """接收任务时冻结的配置语义（主规格 6.2）。默认使用当前生效快照。"""
        cfg = config if config is not None else self._current
        if cfg is None:
            raise SettingsCommitError("尚无生效配置，无法构建接收快照")
        oc = cfg.config.openchamber
        directory = oc.directory
        binding = oc.session_policy
        frozen_session = oc.session_id if binding is SessionBindingMode.FIXED_SESSION else None
        return ReceiveSettingsSnapshot(
            config_revision=cfg.revision,
            committed_at=cfg.created_at,
            received_at=received_at if received_at is not None else _utc_iso(self._clock.now()),
            effective_executor=cfg.config.default_target,
            directory=directory,
            project_key=_project_key_of(directory),
            agent=oc.agent,
            requested_model=oc.model,
            binding_mode=binding,
            frozen_session_id=frozen_session,
        )

    def build_execution_snapshot(
        self,
        receive: ReceiveSettingsSnapshot,
        *,
        resolved_session_id: str | None = None,
        binding_revision: int | None = None,
        execution_started_at: str | None = None,
    ) -> ExecutionSettingsSnapshot:
        """开始执行时构建不可变执行快照（主规格 6.2）。

        FIXED_SESSION：使用接收时冻结的 session_id，忽略传入值，缺失即报错（不做静默创建）。
        PROJECT_ROTATING：在执行屏障内使用“已提交的新项目会话”（由调用方传入），FIXED 不跟随。
        """
        started_at = execution_started_at if execution_started_at is not None else _utc_iso(self._clock.now())
        if receive.binding_mode is SessionBindingMode.FIXED_SESSION:
            if not receive.frozen_session_id:
                raise SettingsValidationError(
                    "FIXED_SESSION 缺少已绑定的 session_id：进入执行阶段前必须明确绑定，"
                    "不允许静默创建新会话"
                )
            resolved = receive.frozen_session_id
        else:
            resolved = resolved_session_id  # PROJECT_ROTATING：执行期解析的已提交会话
        bind_revision = (
            binding_revision if binding_revision is not None else receive.config_revision
        )
        return ExecutionSettingsSnapshot(
            receive=receive,
            binding_revision=bind_revision,
            resolved_session_id=resolved,
            execution_started_at=started_at,
        )