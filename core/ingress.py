"""AI Relay B V3.0：协议层 → 持久任务认领层 的边界（T07）。

职责（主规格 5.1 / 04.3）：
- 接收 raw_text，调用 T06 parse_message 严格解析；
- 只认领 A 端 TASK（TYPE=TASK 且 SOURCE=CHATGPT）；
- 用 T06 content_digest 作为规范摘要；
- 构建/取得接收时冻结的 ReceiveSettingsSnapshot 并随同一次认领事务持久化；
- 调用 TaskStore.claim 并返回不可歧义的 ClaimResult，绝不执行任务。

正常业务结果（EXISTING/CONFLICT/QUEUE_FULL）用 ClaimResult 表达；
协议错误、非任务、非 CHATGPT 来源抛 IngressError（系统/策略错误，稳定 code），
数据库事务失败由 StorageError 上抛。错误一律不做字符串匹配判断。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Mapping

from core.domain import ReceiveSettingsSnapshot
from core.protocol_v1 import (
    MessageType,
    ProtocolError,
    ProtocolFormat,
    content_digest,
    parse_message,
)
from infra.clock import Clock, SystemClock
from storage.task_store import (
    ClaimResult,
    TaskStore,
    make_task_key,
)

PEER_ID_CHATGPT = "CHATGPT"
PEER_ID_LEGACY_DEFAULT = "legacy-default"


class IngressErrorCode(Enum):
    """入站拒绝原因稳定码；T08 起使用规范命名，PROTOCOL/NOT_FROM_CHATGPT 为 T07 别名。"""

    PROTOCOL_ERROR = "protocol_error"
    PROTOCOL = "protocol_error"  # T07 兼容别名
    NOT_TASK = "not_task"
    INVALID_SOURCE = "invalid_source"
    NOT_FROM_CHATGPT = "invalid_source"  # T07 兼容别名


class IngressError(Exception):
    """入站拒绝错误：携带稳定 IngressErrorCode；PROTOCOL 附带原始协议 code。"""

    def __init__(
        self,
        code: IngressErrorCode,
        message: str,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"[{code.value}] {message}")
        self.code = code
        self.message = message
        self.context = dict(context or {})


def peer_id_for(protocol_format: ProtocolFormat) -> str:
    """兼容模式 peer 固定 legacy-default；V1 A 端稳定身份为 CHATGPT。"""
    if protocol_format is ProtocolFormat.LEGACY_WEB:
        return PEER_ID_LEGACY_DEFAULT
    return PEER_ID_CHATGPT


def protocol_format_value(protocol_format: ProtocolFormat) -> str:
    return protocol_format.value.upper()


def _utc_iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat()


def _normalize_lf(text: str) -> str:
    return text.replace("\r\n", "\n")


class IngressService:
    """接收边界：解析协议 → 校验 A 端 TASK → 摘要 → 原子认领。"""

    def __init__(self, store: TaskStore, *, clock: Clock | None = None) -> None:
        self._store = store
        self._clock = clock if clock is not None else SystemClock()

    def accept(
        self,
        raw_text: str,
        *,
        receive_snapshot: ReceiveSettingsSnapshot,
    ) -> ClaimResult:
        """认领一条入站消息。

        协议解析失败、非 TASK、非 CHATGPT 来源抛 IngressError，绝不写入半条任务；
        正常认领结果全在 ClaimResult 中表达。store 内部为系统异常上抛。
        """
        try:
            message = parse_message(raw_text)
        except ProtocolError as exc:
            raise IngressError(
                IngressErrorCode.PROTOCOL_ERROR,
                f"入站协议解析失败：{exc.reason}",
                context={"protocol_code": exc.code.value},
            ) from exc

        if message.message_type is not MessageType.TASK:
            raise IngressError(
                IngressErrorCode.NOT_TASK,
                f"只接受 TYPE=TASK 的任务，实际为 {message.message_type.value}",
            )

        if message.source.upper() != PEER_ID_CHATGPT:
            raise IngressError(
                IngressErrorCode.INVALID_SOURCE,
                f"入站 SOURCE 必须为 CHATGPT，实际为 {message.source}",
            )

        peer_id = peer_id_for(message.protocol_format)
        task_key = make_task_key(peer_id, message.message_id)
        digest = content_digest(message)

        return self._store.claim(
            task_key=task_key,
            peer_id=peer_id,
            task_id=message.message_id,
            protocol_format=protocol_format_value(message.protocol_format),
            raw_message=_normalize_lf(raw_text),
            body=message.body,
            canonical_hash=digest,
            receive_snapshot=receive_snapshot,
            received_at=_utc_iso(self._clock.now()),
        )

    @property
    def store(self) -> TaskStore:
        return self._store