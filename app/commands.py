"""App 命令层：剪贴板快照 → 认领 → 提交后发布（T08）。

职责（主规格 5.2 / 本卡）：
- 接收纯文本 str（GUI 线程已复制成 ClipboardSnapshot，命令层只处理普通字符串）；
- 候选消息预过滤：非 Relay envelope 的普通文本安静忽略，绝不报协议错误；
- 真正合法性仍由 T06 parse_message 决定（本层不实现第二套协议解析器）；
- 提交成功（ACCEPTED/EXISTING/CONFLICT）后才发布“已接收”；失败不误标已处理；
- 系统异常必须保留重试通道：不写入任何防抖记忆，剪贴板内容可再次补拾。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from core.domain import ReceiveSettingsSnapshot
from core.ingress import IngressError, IngressErrorCode, IngressService
from core.protocol_v1 import LEGACY_BEGIN, PROTOCOL_MARKER
from core.settings_service import SettingsService
from storage.task_store import ClaimOutcome


class ReceiveOutcomeKind(str, Enum):
    """命令层结果种类：上层不再用字符串做机器判断。"""

    ACCEPTED = "accepted"  # 新任务已持久认领（commit 后发布）
    EXISTING = "existing"  # 重复消息已安全识别，未重复认领
    CONFLICT = "conflict"  # 同 ID 内容不同，保留旧记录（需 UI 提示）
    QUEUE_FULL = "queue_full"  # 当前未认领成功，可再次尝试
    IGNORED_PLAIN_TEXT = "ignored_plain_text"  # 普通文本，非错误
    IGNORED_SELF_WRITE = "ignored_self_write"  # Relay 自写忽略，非错误
    IGNORED_NOT_A_TASK = "ignored_not_a_task"  # RESPONSE/非 CHATGPT 等，非错误
    IGNORED_BAD_PROTOCOL = "ignored_bad_protocol"  # 看像 Relay 但协议故障，非错误
    ERROR = "error"  # 系统异常：未提交，绝不可标“已看过”


@dataclass(frozen=True, slots=True)
class ReceiveResult:
    """命令结果：只有在 TaskStore 事务已提交后才构成权威发布。"""

    kind: ReceiveOutcomeKind
    task_id: str | None = None
    peer_id: str | None = None
    sequence: int | None = None
    reason: str = ""
    error: str = ""

    @property
    def debounce(self) -> bool:
        """是否为本条内容的权威结果（可安全进入防抖缓存）。

        权威结果：ACCEPTED/EXISTING/CONFLICT + 各类确认忽略；
        失败（ERROR）与未入队（QUEUE_FULL）必须可再次尝试，禁止进防抖。
        """
        return self.kind not in (ReceiveOutcomeKind.ERROR, ReceiveOutcomeKind.QUEUE_FULL)


def looks_like_relay_message(text: str) -> bool:
    """极轻量预过滤：看是否值得交给 Ingress；不做第二套协议解析。"""
    normalized = text.replace("\r\n", "\n").lstrip()
    return normalized.startswith(PROTOCOL_MARKER) or normalized.startswith(LEGACY_BEGIN)


class ClipboardReceiveController:
    """剪贴板接收命令：快照 → 候选过滤 → 接收快照冻结 → Ingress → 包装结果。

    不持有任务/队列/Attempt/executor 等任何业务状态（本卡第28节）。
    """

    def __init__(
        self,
        ingress: IngressService,
        *,
        settings: SettingsService | None = None,
        snapshot_provider: Callable[[], ReceiveSettingsSnapshot] | None = None,
    ) -> None:
        if settings is None and snapshot_provider is None:
            raise ValueError("必须提供 settings 或 snapshot_provider 之一")
        if settings is not None and snapshot_provider is not None:
            raise ValueError("settings 与 snapshot_provider 只能二选一")
        self._ingress = ingress
        self._snapshot_provider = (
            snapshot_provider
            if snapshot_provider is not None
            else settings.build_receive_snapshot  # type: ignore[union-attr]
        )

    def execute(self, text: str, *, reason: str = "CHANGE_EVENT") -> ReceiveResult:
        """处理一份剪贴板文本副本，返回权威结果；提交成功后才算“已接收”。"""
        if not looks_like_relay_message(text):
            return ReceiveResult(
                kind=ReceiveOutcomeKind.IGNORED_PLAIN_TEXT,
                reason=reason,
                error="",
            )

        try:
            receive_snapshot = self._snapshot_provider()
        except Exception as exc:  # noqa: BLE001 - 快照冻结失败按系统异常处理，保留重试
            return ReceiveResult(
                kind=ReceiveOutcomeKind.ERROR,
                reason=reason,
                error=f"接收快照冻结失败：{exc}",
            )

        try:
            claim = self._ingress.accept(text, receive_snapshot=receive_snapshot)
        except IngressError as exc:
            return self._map_ingress_error(exc, reason)
        except Exception as exc:  # noqa: BLE001 - 数据库故障，绝不标“已看过”
            return ReceiveResult(
                kind=ReceiveOutcomeKind.ERROR,
                reason=reason,
                error=f"入库失败：{exc}",
            )

        return ReceiveResult(
            kind=self._map_outcome(claim.outcome),
            task_id=claim.task_id,
            peer_id=claim.peer_id,
            sequence=claim.sequence,
            reason=reason,
            error="",
        )

    @staticmethod
    def _map_outcome(outcome: ClaimOutcome) -> ReceiveOutcomeKind:
        if outcome is ClaimOutcome.ACCEPTED:
            return ReceiveOutcomeKind.ACCEPTED
        if outcome is ClaimOutcome.EXISTING:
            return ReceiveOutcomeKind.EXISTING
        if outcome is ClaimOutcome.CONFLICT:
            return ReceiveOutcomeKind.CONFLICT
        if outcome is ClaimOutcome.QUEUE_FULL:
            return ReceiveOutcomeKind.QUEUE_FULL
        raise AssertionError(f"未知 ClaimOutcome：{outcome}")

    @staticmethod
    def _map_ingress_error(exc: IngressError, reason: str) -> ReceiveResult:
        if exc.code is IngressErrorCode.NOT_TASK:
            return ReceiveResult(
                kind=ReceiveOutcomeKind.IGNORED_NOT_A_TASK,
                reason=reason,
                error=exc.message,
            )
        if exc.code is IngressErrorCode.INVALID_SOURCE:
            return ReceiveResult(
                kind=ReceiveOutcomeKind.IGNORED_NOT_A_TASK,
                reason=reason,
                error=exc.message,
            )
        if exc.code is IngressErrorCode.PROTOCOL_ERROR:
            return ReceiveResult(
                kind=ReceiveOutcomeKind.IGNORED_BAD_PROTOCOL,
                reason=reason,
                error=exc.message,
            )
        return ReceiveResult(kind=ReceiveOutcomeKind.ERROR, reason=reason, error=exc.message)