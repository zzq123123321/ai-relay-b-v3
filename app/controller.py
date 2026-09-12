"""AI Relay B V3.0：Fake 端到端闭环与应用控制器（T12 / G1 持久闭环）。

在既有核心之上提供一个显式、同步、可审计的控制器：每个方法恰好完成一步，
不启动线程 / timer / 死循环，适合 UI/脚本按用户节奏逐步驱动，也适合测试矩阵
在单个步骤之间插入重启/故障。

职责边界：
- accept_task：剪贴板接收路径（复用 T08 ClipboardReceiveController 语义）；
- tick_once：FIFO 调度取得执行资格（T09）；
- run_one_fake_cycle：对唯一 OPEN attempt 完成一次「dispatch(INITIAL_SEND) →
  FakeExecutor.completion_for → ResultCommitService.commit(确定性 result_id)」；
  REJECTED/UNKNOWN 绝不自动重发、绝不自动冒充足完成；提交存储故障（含注入的
  sqlite3.IntegrityError）结构化返回 COMMIT_STORE_ERROR，不吞异常、不改权威；
- offer_next_result：先 D01 gate（存在本进程未确认的 OFFERED 结果 → 阻塞），
  再 D08 入站预检（只有能证明「不是待处理 Relay TASK 或已持久」的内容才允许覆盖：
  QUEUE_FULL/ERROR/CONFLICT/IGNORED_BAD_PROTOCOL/IGNORED_NOT_A_TASK 一律阻塞，
  本端最近一次自写文本视为已持久出站放行），
  最后 DeliveryService.provide_once 提供不可变 protocol_text；
- confirm_local_delivery：把当前 OFFERED 的 delivery_id 记入进程内确认集合；
  重启后集合清空 → 再次 D01 安全侧失败（宁可重复阻塞，不重复覆盖剪贴板）。

确定性工厂跨重启稳定，保证同 operation/attempt/epoch 重建出同一个 result 与
delivery/event ID，配合 ResultCommitService 的 ALREADY_COMMITTED 幂等收敛。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import Enum

from adapters.fake_executor import FakeCompletionReadError, FakeExecutor
from app.commands import (
    ClipboardReceiveController,
    ReceiveOutcomeKind,
    looks_like_relay_message,
)
from core.delivery import ClipboardWriteError, DeliveryOutcome, DeliveryService
from core.dispatch import DispatchOutcome, DispatchProposal, DispatchService
from core.ingress import IngressService
from core.result_commit import (
    CandidateResult,
    CommitOutcome,
    ResultClaim,
    ResultCommitError,
    ResultCommitService,
    sha256_hex,
)
from core.scheduler import Scheduler
from core.settings_service import SettingsService
from infra.clock import Clock, SystemClock
from storage.database import Database
from storage.lease_store import ProjectLeaseStore
from storage.operation_store import OperationStore
from storage.result_store import ResultStore
from storage.task_store import TaskStore

DEFAULT_ENDPOINT = "http://127.0.0.1:57123"

# D08 入站预检的阻塞结论：只有这些才证明「尚未持久入站 / 具 Relay envelope 候选特征」，
# 一律禁止覆盖剪贴板。其余（ACCEPTED/EXISTING/IGNORED_PLAIN_TEXT）已证明安全。
_D08_BLOCKED_KINDS = frozenset({
    ReceiveOutcomeKind.QUEUE_FULL,          # 尚未成功持久
    ReceiveOutcomeKind.ERROR,               # 未提交，绝不可标「已看过」
    ReceiveOutcomeKind.CONFLICT,            # 内容冲突（同一 ID 不同正文）
    ReceiveOutcomeKind.IGNORED_BAD_PROTOCOL,  # Relay envelope 但协议损坏/未完整复制
    ReceiveOutcomeKind.IGNORED_NOT_A_TASK,  # Relay envelope 但非可入站 TASK（RESPONSE 等）
})


# ---------------------------------------------------------------- deterministic factories

def operation_key_for_attempt(attempt_id: str) -> str:
    """一次初始发送的唯一幂等键：由 attempt 派生，跨重启稳定。"""
    return f"INITIAL_SEND:{attempt_id}"


def result_id_for(task_key: str, attempt_id: str, epoch: int) -> str:
    return "res-" + sha256_hex(f"{task_key}|{attempt_id}|{epoch}")[:32]


def delivery_id_for(task_key: str, revision: int) -> str:
    return f"deliv-{sha256_hex(task_key)[:16]}-{revision}"


def event_id_for(result_id: str) -> str:
    return f"evt-{sha256_hex(result_id)[:24]}"


# ---------------------------------------------------------------- cycle kinds

class CycleKind(str, Enum):
    """一次 fake cycle 的结构化结论（禁止中文字符串匹配）。"""

    NO_OPEN_ATTEMPT = "no_open_attempt"        # 无 OPEN attempt，无事可做
    DISPATCH_BLOCKED = "dispatch_blocked"      # dispatch 拒绝/阻塞类结论，send<=1
    REJECTED_NO_COMMIT = "rejected_no_commit"  # 远端明确拒绝：绝不重发、绝不冒充足完成
    UNKNOWN_NO_COMMIT = "unknown_no_commit"    # 远端结果不明：绝不自动重发
    COMPLETION_READ_FAILED = "completion_read_failed"  # 已接受但完成不可读（可修复后重试）
    COMMITTED = "committed"                    # 赢得本次权威提交
    ALREADY_COMMITTED = "already_committed"    # 同确定性结果幂等（重启/重复轮）
    LOST_AUTHORITY = "lost_authority"
    RESULT_ID_CONFLICT = "result_id_conflict"
    COMMIT_STORE_ERROR = "commit_store_error"  # 提交存储故障（含注入 IntegrityError）


@dataclass(frozen=True, slots=True)
class CycleResult:
    kind: CycleKind
    task_key: str | None = None
    attempt_id: str | None = None
    operation_key: str | None = None
    result_id: str | None = None
    revision: int | None = None
    send_calls: int = 0
    detail: str = ""


# ---------------------------------------------------------------- offer kinds

class OfferKind(str, Enum):
    """一次结果提供的结构化结论。"""

    OFFERED = "offered"
    NO_PENDING = "no_pending"
    BLOCKED_UNCONFIRMED_OFFERED = "blocked_unconfirmed_offered"      # D01
    BLOCKED_INBOUND_NOT_PERSISTED = "blocked_inbound_not_persisted"  # D08
    MARK_FAILED = "mark_failed"
    CLIPBOARD_WRITE_FAILED = "clipboard_write_failed"


@dataclass(frozen=True, slots=True)
class OfferResult:
    kind: OfferKind
    delivery_id: str | None = None
    response_text: str | None = None
    write_calls: int = 0
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ConfirmResult:
    confirmed_count: int
    delivery_ids: tuple[str, ...] = ()


# ---------------------------------------------------------------- controller

class AppController:
    """同步显式闭环控制器：accept → tick → cycle → offer → confirm。"""

    def __init__(
        self,
        db: Database,
        *,
        settings: SettingsService | None = None,
        clipboard,
        clock: Clock | None = None,
        executor: FakeExecutor,
        endpoint: str = DEFAULT_ENDPOINT,
        task_store: TaskStore | None = None,
        scheduler: Scheduler | None = None,
        ingress: IngressService | None = None,
        receive: ClipboardReceiveController | None = None,
        dispatch: DispatchService | None = None,
        delivery: DeliveryService | None = None,
        commit: ResultCommitService | None = None,
        attempt_id_factory=None,
    ) -> None:
        self._db = db
        self._clock = clock if clock is not None else SystemClock()
        self._clipboard = clipboard
        self._endpoint = endpoint
        self._executor = executor
        self._confirmed_offered: set[str] = set()
        self._last_written_text: str | None = None  # 最近一次本端写入剪贴板的文本

        tasks = task_store if task_store is not None else TaskStore(db)
        self._task_store = tasks
        self._operation_store = OperationStore(db)

        if scheduler is None:
            scheduler = Scheduler(
                db, task_store=tasks, clock=self._clock,
                attempt_id_factory=attempt_id_factory,
            )
        self._scheduler = scheduler

        if receive is None:
            if ingress is None:
                ingress = IngressService(tasks, clock=self._clock)
            if settings is None:
                raise ValueError("必须提供 settings 或 receive 之一以构建接收路径")
            receive = ClipboardReceiveController(ingress, settings=settings)
        self._receive = receive

        if dispatch is None:
            dispatch = DispatchService(
                db, operation_store=self._operation_store, clock=self._clock,
            )
        self._dispatch = dispatch

        if commit is None:
            commit = ResultCommitService(
                db,
                result_store=ResultStore(db),
                lease_store=ProjectLeaseStore(db),
                clock=self._clock,
                result_id_factory=result_id_for,
                delivery_id_factory=delivery_id_for,
                event_id_factory=event_id_for,
            )
        self._commit = commit

        if delivery is None:
            delivery = DeliveryService(
                db, result_store=ResultStore(db),
                clock=self._clock, clipboard=clipboard,
            )
        self._delivery = delivery

    # ------------------------------------------------------------------ properties

    @property
    def db(self) -> Database:
        return self._db

    @property
    def executor(self) -> FakeExecutor:
        return self._executor

    @property
    def dispatch(self) -> DispatchService:
        return self._dispatch

    @property
    def delivery(self) -> DeliveryService:
        return self._delivery

    @property
    def receive(self) -> ClipboardReceiveController:
        return self._receive

    @property
    def commit(self) -> ResultCommitService:
        return self._commit

    @property
    def confirmed_offered(self) -> frozenset[str]:
        return frozenset(self._confirmed_offered)

    # ------------------------------------------------------------------ explicit steps

    def accept_task(self, text: str) -> object:
        """剪贴板接收路径：解析、去重、认领；返回 ReceiveResult。"""
        return self._receive.execute(text)

    def tick_once(self) -> object:
        """调度一步：尝试启动最早 QUEUED；返回 ScheduleResult。"""
        return self._scheduler.tick()

    def run_one_fake_cycle(self) -> CycleResult:
        """对唯一 OPEN attempt 完成一次确定性闭环（dispatch→completion→commit）。

        重试安全：operation ACCEPTED 后再次 cycle 走 ALREADY_EXISTS(ACCEPTED)，
        用同一确定性 result 幂等提交，绝不再 send_once。
        """
        row = self._db.connection.execute(
            "SELECT a.task_key, a.attempt_id, a.authority_epoch, a.control_revision,"
            " a.execution_snapshot_json, t.body"
            " FROM attempts a JOIN tasks t ON t.task_key=a.task_key"
            " WHERE a.state='OPEN' ORDER BY t.sequence ASC LIMIT 1"
        ).fetchone()
        if row is None:
            return CycleResult(CycleKind.NO_OPEN_ATTEMPT, detail="无 OPEN attempt")

        task_key, attempt_id, epoch, control_revision, exec_json, body = row
        snapshot = json.loads(exec_json)
        project_key = snapshot.get("receive", {}).get("project_key")
        session_id = snapshot.get("resolved_session_id")
        if not session_id:
            return CycleResult(
                CycleKind.DISPATCH_BLOCKED, task_key=task_key, attempt_id=attempt_id,
                detail="执行快照没有已解析 session_id（FIXED_SESSION 应冻结）",
            )
        if not project_key:
            return CycleResult(
                CycleKind.DISPATCH_BLOCKED, task_key=task_key, attempt_id=attempt_id,
                detail="执行快照里缺少 project_key",
            )

        operation_key = operation_key_for_attempt(attempt_id)
        proposal = DispatchProposal(
            operation_key=operation_key,
            kind="INITIAL_SEND",
            task_key=task_key,
            attempt_id=attempt_id,
            authority_epoch=epoch,
            control_revision=control_revision,
            endpoint=self._endpoint,
            session_id=session_id,
            project_key=project_key,
            prompt_body=body or "",
        )

        sends_before = self._executor.send_calls
        outcome = self._dispatch.dispatch_once(proposal, transport=self._executor)
        send_calls = self._executor.send_calls - sends_before

        accepted = outcome.outcome is DispatchOutcome.ACCEPTED
        if outcome.outcome is DispatchOutcome.ALREADY_EXISTS:
            if outcome.state == "REJECTED":
                return CycleResult(
                    CycleKind.REJECTED_NO_COMMIT, task_key=task_key,
                    attempt_id=attempt_id, operation_key=operation_key,
                    send_calls=0, detail=outcome.detail,
                )
            if outcome.state == "UNKNOWN":
                return CycleResult(
                    CycleKind.UNKNOWN_NO_COMMIT, task_key=task_key,
                    attempt_id=attempt_id, operation_key=operation_key,
                    send_calls=0, detail=outcome.detail,
                )
            accepted = outcome.state == "ACCEPTED"
        if not accepted:
            if outcome.outcome in (DispatchOutcome.REJECTED,):
                return CycleResult(
                    CycleKind.REJECTED_NO_COMMIT, task_key=task_key,
                    attempt_id=attempt_id, operation_key=operation_key,
                    send_calls=send_calls, detail=outcome.detail,
                )
            if outcome.outcome in (DispatchOutcome.UNKNOWN,):
                return CycleResult(
                    CycleKind.UNKNOWN_NO_COMMIT, task_key=task_key,
                    attempt_id=attempt_id, operation_key=operation_key,
                    send_calls=send_calls, detail=outcome.detail,
                )
            return CycleResult(
                CycleKind.DISPATCH_BLOCKED, task_key=task_key, attempt_id=attempt_id,
                operation_key=operation_key, send_calls=send_calls, detail=outcome.detail,
            )

        record = self._operation_store.read_by_key(operation_key)
        try:
            completion = self._executor.completion_for(record)
        except FakeCompletionReadError as exc:
            return CycleResult(
                CycleKind.COMPLETION_READ_FAILED, task_key=task_key,
                attempt_id=attempt_id, operation_key=operation_key,
                send_calls=send_calls, detail=str(exc),
            )

        candidate = CandidateResult(
            task_key=task_key,
            attempt_id=attempt_id,
            authority_epoch=epoch,
            result_state=completion.result_state,
            source="AUTO_RELAY",
            final_body=completion.final_body,
            result_id=result_id_for(task_key, attempt_id, epoch),
            claim=ResultClaim(
                endpoint=self._endpoint,
                session_id=session_id,
                message_id=completion.claim_message_id,
            ),
            remote_state=completion.remote_state,
        )
        try:
            committed = self._commit.commit(candidate)
        except (ResultCommitError, sqlite3.IntegrityError) as exc:
            return CycleResult(
                CycleKind.COMMIT_STORE_ERROR, task_key=task_key,
                attempt_id=attempt_id, operation_key=operation_key,
                result_id=candidate.result_id, send_calls=send_calls, detail=str(exc),
            )

        if committed.outcome is CommitOutcome.COMMITTED:
            kind = CycleKind.COMMITTED
        elif committed.outcome is CommitOutcome.ALREADY_COMMITTED:
            kind = CycleKind.ALREADY_COMMITTED
        elif committed.outcome is CommitOutcome.RESULT_ID_CONFLICT:
            kind = CycleKind.RESULT_ID_CONFLICT
        else:
            kind = CycleKind.LOST_AUTHORITY
        return CycleResult(
            kind, task_key=task_key, attempt_id=attempt_id,
            operation_key=operation_key, result_id=candidate.result_id,
            revision=committed.revision, send_calls=send_calls, detail=committed.detail,
        )

    def offer_next_result(self) -> OfferResult:
        """提供下一条权威结果：D01 gate → D08 入站预检 → 剪贴板（单次副作用）。"""
        rows = self._db.connection.execute(
            "SELECT delivery_id FROM outbox WHERE state='OFFERED' ORDER BY delivery_id"
        ).fetchall()
        unconfirmed = [row[0] for row in rows if row[0] not in self._confirmed_offered]
        if unconfirmed:
            return OfferResult(
                OfferKind.BLOCKED_UNCONFIRMED_OFFERED,
                delivery_id=unconfirmed[0], write_calls=0,
                detail=f"存在未确认的 OFFERED 结果 {unconfirmed[0]}，拒绝再次覆盖剪贴板",
            )

        text = self._clipboard.text() or ""
        if text.strip():
            # 本端最近一次写入的结果文本 = 已持久出站，证明「不是待处理入站」，
            # 视同 IGNORED_SELF_WRITE，允许覆盖（避免把上一份 RESPONSE 误当外来的
            # pending-inbound 无限阻塞；重启后记忆清空 → 一律按外来文本安全侧失败）。
            if text != self._last_written_text and looks_like_relay_message(text):
                received = self._receive.execute(text, reason="D08_PRE_OFFER")
                if received.kind in _D08_BLOCKED_KINDS:
                    return OfferResult(
                        OfferKind.BLOCKED_INBOUND_NOT_PERSISTED, write_calls=0,
                        detail=(
                            f"D08 入站预检未通过（kind={received.kind.value}）："
                            "剪贴板存在未持久化/不可证明已持久的入站 Relay 内容，拒绝覆盖"
                        ),
                    )

        try:
            result = self._delivery.provide_once()
        except ClipboardWriteError as exc:
            return OfferResult(
                OfferKind.CLIPBOARD_WRITE_FAILED, write_calls=1, detail=str(exc),
            )
        if result.outcome is DeliveryOutcome.OFFERED:
            self._last_written_text = result.response_text
            return OfferResult(
                OfferKind.OFFERED, delivery_id=result.delivery_id,
                response_text=result.response_text, write_calls=1,
            )
        if result.outcome is DeliveryOutcome.MARK_FAILED:
            # 剪贴板副作用已发生（写成功、OFFERED 落库失败）：同样记入自写，防止
            # 同进程内把已写入的文本误判为外来 pending-inbound。
            self._last_written_text = result.response_text
            return OfferResult(
                OfferKind.MARK_FAILED, delivery_id=result.delivery_id,
                write_calls=1, detail=result.export_error or "mark_offered 落库失败",
            )
        return OfferResult(OfferKind.NO_PENDING, write_calls=0)

    def confirm_local_delivery(self) -> ConfirmResult:
        """进程内确认当前全部 OFFERED 结果；重启后集合丢失 → D01 安全侧失败。"""
        rows = self._db.connection.execute(
            "SELECT delivery_id FROM outbox WHERE state='OFFERED' ORDER BY delivery_id"
        ).fetchall()
        confirmed: list[str] = []
        for (delivery_id,) in rows:
            if delivery_id not in self._confirmed_offered:
                self._confirmed_offered.add(delivery_id)
                confirmed.append(delivery_id)
        return ConfirmResult(confirmed_count=len(confirmed), delivery_ids=tuple(confirmed))

    # ------------------------------------------------------------------ stats

    def stats(self) -> dict:
        conn = self._db.connection

        def count(sql: str, params: tuple = ()) -> int:
            return int(conn.execute(sql, params).fetchone()[0])

        return {
            "tasks": count("SELECT COUNT(*) FROM tasks"),
            "terminal_tasks": count(
                "SELECT COUNT(*) FROM tasks WHERE state IN ('COMPLETED','FAILED','STOPPED_BY_USER')"
            ),
            "open_attempts": count("SELECT COUNT(*) FROM attempts WHERE state='OPEN'"),
            "results": count("SELECT COUNT(*) FROM results"),
            "outbox_pending": count("SELECT COUNT(*) FROM outbox WHERE state='PENDING'"),
            "outbox_offered": count("SELECT COUNT(*) FROM outbox WHERE state='OFFERED'"),
            "init_send_accepted": count(
                "SELECT COUNT(*) FROM operations WHERE kind='INITIAL_SEND' AND state='ACCEPTED'"
            ),
            "committed_events": count(
                "SELECT COUNT(*) FROM events WHERE event_code='RESULT_COMMITTED'"
            ),
            "leases": count("SELECT COUNT(*) FROM project_leases"),
            "confirmed_offered": len(self._confirmed_offered),
            "total_send_calls": self._executor.send_calls,
        }