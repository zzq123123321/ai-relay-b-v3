"""Windows/Qt 剪贴板接收适配器（T08）。

职责（主规格 5.2 / 15.2 ACT01、ACT02）：
- 监听 QClipboard.dataChanged，在所属 GUI 线程立即将 clipboard.text() 复制为
  不可变 ClipboardSnapshot；之后 command / ingress 只处理该快照的普通 str 副本；
- start / pause / resume / retry_current 统一走 capture_current(reason) 补拾路径；
- 自写一次性保护：只为 Relay 自己刚写的一次忽略，绝不形成永久黑名单；
- 轻量防抖只在“已形成权威结果”后生效；失败（未提交/队满）绝不写入防抖缓存。

线程边界（本卡核心不变量）：QClipboard 只能在其绑定线程访问。本适配器在
GUI 线程内读出文本后彻底断开与 Qt 对象的关系；禁止把 QClipboard / QMimeData
传给后台业务层。
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

from infra.clock import Clock, SystemClock


class PickupReason(str, Enum):
    """快照来源，仅用于诊断；不改变认领核心语义（主规格 5.2）。"""

    CHANGE_EVENT = "CHANGE_EVENT"
    STARTUP_PICKUP = "STARTUP_PICKUP"
    RESUME_PICKUP = "RESUME_PICKUP"
    MANUAL_RETRY = "MANUAL_RETRY"


@dataclass(frozen=True, slots=True)
class ClipboardSnapshot:
    """不可变文本快照：产生后即使系统剪贴板下一秒改变，本次处理仍用这份 str。"""

    text: str
    digest: str
    captured_at: str
    reason: PickupReason


class SnapshotOutcome(Protocol):
    """handler 返回结果的契约：kind + debounce 两个属性即可。"""

    kind: str
    debounce: bool


@dataclass(frozen=True, slots=True)
class AdapterResult:
    """适配器自身的判定结果（未进入 handler 的路径）。"""

    kind: str
    debounce: bool = False
    detail: str = ""


class ClipboardSource(Protocol):
    """与 QClipboard 兼容的最小接口：生产传 QApplication.clipboard()；测试用 FakeClipboard。"""

    def text(self) -> str: ...

    def setText(self, text: str) -> None: ...

    def connect_data_changed(self, callback: Callable[[], None]) -> None: ...


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utc_iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat()


class QClipboardListener:
    """Qt 剪贴板监听器：GUI 线程复制快照 + 自写一次性保护 + 权威结果防抖。

    不能持有 Task 状态 / queue / Attempt / executor / session / result（主规格本卡第28节）。
    """

    def __init__(
        self,
        clipboard: ClipboardSource,
        *,
        handler: Callable[[ClipboardSnapshot], SnapshotOutcome] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._clipboard = clipboard
        self._handler = handler
        self._clock = clock if clock is not None else SystemClock()
        self._gui_thread = threading.current_thread()
        self._enabled = False
        self._pending_self_digest: str | None = None
        self._last_confirmed_digest: str | None = None
        clipboard.connect_data_changed(self._on_data_changed)

    # ---------------------------------------------------------------- 控制

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def gui_thread_id(self) -> int:
        """诊断用途：监听器所属 GUI 线程（验证 QClipboard 只在 GUI 线程读取）。"""
        return self._gui_thread.ident or 0

    def start(self) -> SnapshotOutcome | None:
        """ACT01 幂等启动：补拾当前剪贴板合法任务，走统一 capture_current 路径。"""
        if self._enabled:
            return None
        self._enabled = True
        return self.capture_current(PickupReason.STARTUP_PICKUP)

    def pause(self) -> None:
        """ACT02 暂停：只停止新拾取，不影响已有 QUEUED / 当前任务。"""
        self._enabled = False

    def resume(self) -> SnapshotOutcome:
        """恢复接收：补拾当前剪贴板内容（可能覆盖了暂停期间的新任务）。"""
        self._enabled = True
        self._pending_self_digest = None  # 防御：清理可能残留的一次性 marker
        return self.capture_current(PickupReason.RESUME_PICKUP)

    def retry_current(self) -> SnapshotOutcome:
        """显式重试通道：对当前剪贴板内容再次补拾（失败后可重试，不依赖用户重新复制）。"""
        return self.capture_current(PickupReason.MANUAL_RETRY)

    # ---------------------------------------------------------------- 自写保护

    def write_text_from_relay(self, text: str) -> None:
        """Relay 主动写剪贴板的 self-write primitive（未来出站专用；本轮仅供测试自用）。

        先登记一次性 marker 再 setText；收到完全相同文本时消费 marker 并忽略本次事件。
        消费后 marker 立即清空，不会形成永久黑名单。
        """
        self._pending_self_digest = _digest(text)
        self._clipboard.setText(text)

    # ---------------------------------------------------------------- 内部

    def capture_current(self, reason: PickupReason) -> SnapshotOutcome:
        """公共补拾入口：先复制文本快照，再按统一逻辑处理。"""
        snapshot = self._capture(reason)
        return self._ingest(snapshot)

    def _capture(self, reason: PickupReason) -> ClipboardSnapshot:
        assert threading.current_thread() is self._gui_thread, "QClipboard 只能在 GUI 线程读取"
        text = self._clipboard.text() or ""
        return ClipboardSnapshot(
            text=text,
            digest=_digest(text),
            captured_at=_utc_iso(self._clock.now()),
            reason=reason,
        )

    def _on_data_changed(self) -> None:
        snapshot = self._capture(PickupReason.CHANGE_EVENT)
        if not self._enabled:
            # 暂停期间：只消费/清理自写 marker，不进入业务层（暂停不接收新任务）
            self._clear_pending_if_needed(snapshot)
            return
        self._ingest(snapshot)

    def _ingest(self, snapshot: ClipboardSnapshot) -> SnapshotOutcome:
        matched = self._clear_pending_if_needed(snapshot)
        if not self._enabled and snapshot.reason is not PickupReason.MANUAL_RETRY:
            return AdapterResult(kind="IGNORED_PAUSED", debounce=False)
        if snapshot.digest == self._last_confirmed_digest:
            # 已形成权威结果的内容不再重复处理；真正去重仍由 TaskStore.claim 兜底
            return AdapterResult(kind="DEBOUNCED", debounce=True, detail="同内容已权威处理")
        if matched:
            # 一次性自写：消费 marker 后本次忽略；不写入防抖，未来外部同文本可正常处理
            return AdapterResult(kind="IGNORED_SELF_WRITE", debounce=False, detail="Relay 自写忽略一次")
        if self._handler is None:
            return AdapterResult(kind="NO_HANDLER", debounce=False)
        result = self._handler(snapshot)
        if result is not None and getattr(result, "debounce", False):
            self._last_confirmed_digest = snapshot.digest
        return result

    def _clear_pending_if_needed(self, snapshot: ClipboardSnapshot) -> bool:
        """自写 marker 一次式语义：命中则消费并返回 True；不命中则清理 stale marker。"""
        if self._pending_self_digest is None:
            return False
        if snapshot.digest == self._pending_self_digest:
            self._pending_self_digest = None
            return True
        self._pending_self_digest = None  # 旧 marker 已过期，避免吞掉未来外部同文本
        return False