"""AI Relay B V3.0：R1 结果交付与 Outbox 读取（T11）。

依据：主规格 04.4（回复文件导出）、17.1（R1 兼容结果交付）、17.2（结果版本）；
任务卡 S08/S09/D02 与“DB commit 在剪贴板副作用之前”。

职责边界（本轮）：
- 只做：读 Outbox 队首 PENDING → 读对应不可变 Result → 用提交时固定的
  protocol_text 逐字提供 → 交给 ClipboardSink（外部副作用，严禁在事务内）；
- 不做：模型执行 / result 判定 / 改 task 权威 / R2 ACK / POLL / Exchange。

顺序保证：
- 权威结果事务 COMMIT → 重启可从持久 Outbox 补发（D02）；
- clipboard 写成功只表达“已复制/已提供”，绝不表达“A 端已接收/ACK”；
- 剪贴板写失败或导出失败只标记交付问题，不破坏已提交的权威 Result/Outbox
  （S08：仍 COMPLETED，数据库回复仍可复制）。

reply 文件按主规格 4.4 命名 <revision>_<result_id>.response.txt，位于
replies/<task_key_hash>/ 下；task_id 不进入路径，恶意 task_id 无法路径逃逸（S09）。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from core.result_commit import sha256_hex
from infra.clock import Clock, SystemClock
from storage.database import Database
from storage.result_store import ResultStore, ResultStoreError


@runtime_checkable
class ClipboardSink(Protocol):
    """外部剪贴板写接口。实现必须在事务外调用并断言自身非事务。"""

    def write_text(self, *, text: str) -> None: ...


class ClipboardWriteError(Exception):
    """剪贴板写入失败（交付问题，不改变权威 Result）。"""


class ReplyExport(Protocol):
    """按不可变版本导出 reply 文件（主规格 4.4）。失败信息返回给调用方。"""

    def export(self, *, response_text: str, task_key: str, result_id: str,
               revision: int) -> None: ...


_SAFE_FILENAME_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class FileReplyExport:
    """把 RESPONSE 逐字导出到 replies/<task_hash>/<revision>_<result_id>.response.txt。

    路径安全（S09）：
    - 目录只由 sha256(task_key) 前缀生成，用户可控的 task_id/task_key 永远进不了目录名；
    - 文件名里的 result_id 先做净化校验（仅安全字符、非空、不以点开头），
      路径分隔符/冒号/系统保留写法一律拒绝，保证构造出的路径不可能逃出目录；
    - 落盘前再做 resolve 双重包含检查（含 both is_relative_to(root) 与父目录相等），
      防御性兜底。恶意 task_id 原样保留在 RESPONSE 的 IN_REPLY_TO，可正常回传。
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def export(self, *, response_text: str, task_key: str, result_id: str,
               revision: int) -> None:
        if not _SAFE_FILENAME_PART.fullmatch(result_id):
            raise ResultStoreError(
                f"result_id 不符合安全文件名格式，拒绝导出命名：{result_id!r}"
            )
        directory = self.root / sha256_hex(task_key)[:32]
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{revision}_{result_id}.response.txt"
        resolved_target = target.resolve()
        if not resolved_target.is_relative_to(self.root):
            raise ResultStoreError(
                f"回复文件逃出 replies 根目录被拒绝：{resolved_target}"
            )
        if resolved_target.parent != directory:
            raise ResultStoreError(
                f"回复文件不在任务 hash 目录内被拒绝：{resolved_target}"
            )
        target.write_text(response_text, encoding="utf-8")


class DeliveryOutcome:
    OFFERED = "offered"
    NO_PENDING = "no_pending"
    MARK_FAILED = "mark_failed"


class DeliveryResult:
    __slots__ = ("outcome", "response_text", "delivery_id", "export_error")

    def __init__(self, outcome: str, *, response_text: str | None = None,
                 delivery_id: str | None = None,
                 export_error: str | None = None) -> None:
        self.outcome = outcome
        self.response_text = response_text
        self.delivery_id = delivery_id
        self.export_error = export_error


class DeliveryService:
    """R1 交付：Outbox 队首 → 不可变 Result → 剪贴板（+可选文件导出）。"""

    def __init__(
        self,
        db: Database,
        *,
        result_store: ResultStore | None = None,
        clock: Clock | None = None,
        clipboard: ClipboardSink | None = None,
        reply_export: ReplyExport | None = None,
    ) -> None:
        self._db = db
        self._ops = result_store if result_store is not None else ResultStore(db)
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._clipboard = clipboard
        self._reply_export = reply_export

    def provide_once(self) -> DeliveryResult:
        """R1 单条结果提供：一次调用恰好一次外部副作用。

        顺序：读 PENDING → 读 immutable result → （可选导出失败仅提示）→
        剪贴板写 → 事务标记 OFFERED。剪贴板与导出都发生在事务外。
        """
        pending = self._ops.list_pending_deliveries(limit=1)
        if not pending:
            return DeliveryResult(DeliveryOutcome.NO_PENDING)
        head = pending[0]
        result = self._ops.get_result_by_id(head.result_id)
        if result is None:
            raise ResultStoreError(
                f"Outbox delivery_id={head.delivery_id} 引用的 result 不存在（数据损坏）"
            )
        if self._clipboard is None:
            raise ResultStoreError("DeliveryService 未配置 ClipboardSink，无法提供结果")
        export_error: str | None = None
        if self._reply_export is not None:
            try:
                self._reply_export.export(
                    response_text=result.protocol_text,
                    task_key=result.task_key,
                    result_id=result.result_id,
                    revision=result.revision,
                )
            except Exception as exc:  # noqa: BLE001 - 交付辅助失败不得破坏权威结果
                export_error = f"{type(exc).__name__}: {exc}"
        self._clipboard.write_text(text=result.protocol_text)
        now_iso = _utc_iso(self._clock.now())
        try:
            with self._db.transaction():
                marked = self._ops.mark_offered_in(
                    self._db.connection, delivery_id=head.delivery_id, now=now_iso
                )
        except (sqlite3.Error, ResultStoreError) as exc:
            # R1 crash window：剪贴板副作用已发生，但 OFFERED 落库失败。
            # 不自动重写剪贴板；返回结构化 MARK_FAILED，Outbox 仍 PENDING，
            # 显式恢复时可能再次提供（R1 无 ACK/无法与外部副作用原子化）。
            return DeliveryResult(
                DeliveryOutcome.MARK_FAILED,
                delivery_id=head.delivery_id,
                export_error=export_error,
            )
        if not marked:
            return DeliveryResult(
                DeliveryOutcome.MARK_FAILED,
                delivery_id=head.delivery_id,
                export_error=export_error,
            )
        return DeliveryResult(
            DeliveryOutcome.OFFERED,
            response_text=result.protocol_text,
            delivery_id=head.delivery_id,
            export_error=export_error,
        )


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")