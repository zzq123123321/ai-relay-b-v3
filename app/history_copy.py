"""T16-B3：历史复制服务（只读，完全独立于 DeliveryService）。

边界（规格 U05 / UI-B15 / UI-A07）：
- 复制 result：只读取 get_result(result_id).protocol_text 并逐字写剪贴板；
  禁止 authoritative / 上一次结果 / 其它任务 的任何 fallback（U05）。
- 复制普通完整值（task_id/task_key/directory/canonical_hash/result_id/sha256）：
  逐字写完整 value，不截断、不查 DB、不改任何业务状态。
- 本服务永不修改 tasks/attempts/results/outbox/authority/schema；
  DeliveryService.provide_once 会把 Outbox 标记 OFFERED，故不适用于历史“复制”。
- 剪贴板写失败 → outcome=failed（UI 显示“写入剪贴板失败”），不重试任务、不改结果。
- 成功文案只表示“未确认交付”，绝不声称 A 端已收到/已交付/ACK 成功（UI-B15）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from core.delivery import ClipboardSink

__all__ = ["HistoryCopyResult", "HistoryCopyService", "ResultReader"]


class ResultReader(Protocol):
    """只读 result 读取端口（TaskQueries.get_result 满足）。"""

    def get_result(self, result_id: str):
        ...


@dataclass(frozen=True, slots=True)
class HistoryCopyResult:
    outcome: str  # success | missing | failed | unavailable
    message: str
    result_id: str | None = None


class HistoryCopyService:
    def __init__(
        self, *, result_reader: ResultReader, clipboard_sink: ClipboardSink
    ) -> None:
        self._reader = result_reader
        self._clipboard = clipboard_sink

    def copy_result(self, result_id: str) -> HistoryCopyResult:
        """exact result 复制：仅使用该 ResultDetail.protocol_text，逐字写。"""
        try:
            detail = self._reader.get_result(result_id)
        except Exception as exc:  # noqa: BLE001 只读查询失败 → failed，不吞错
            return HistoryCopyResult(
                "failed", f"读取失败：{type(exc).__name__}: {exc}", result_id
            )
        if detail is None:
            return HistoryCopyResult("missing", "此版本无法读取", result_id)
        try:
            self._clipboard.write_text(text=detail.protocol_text)
        except Exception:  # noqa: BLE001 剪贴板写失败 → failed
            return HistoryCopyResult("failed", "写入剪贴板失败", result_id)
        return HistoryCopyResult(
            "success", "已写入剪贴板（未确认交付）", result_id
        )

    def copy_value(self, *, kind: str, value: str) -> HistoryCopyResult:
        """普通完整值逐字复制；空值禁止写剪贴板。"""
        del kind  # 纯展示端口；复制不依赖类型
        if not value:
            return HistoryCopyResult("unavailable", "内容不可用")
        try:
            self._clipboard.write_text(text=value)
        except Exception:  # noqa: BLE001 剪贴板写失败 → failed
            return HistoryCopyResult("failed", "写入剪贴板失败")
        return HistoryCopyResult("success", "已复制到剪贴板")