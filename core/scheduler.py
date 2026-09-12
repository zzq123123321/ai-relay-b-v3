"""AI Relay B V3.0：持久 FIFO 调度与项目执行权取得（T09）。

依据（主规格 01.3 / 05.3 / 10.2，T09 卡）：队首检查全局槽、启动/轮换/项目隔离屏障；
在一个事务内原子完成 QUEUED→ACTIVE、创建 Attempt、取得 project owner；网络等待仍占
活动槽；实现“尚未发送”队列取消与可说明的阻塞原因；本轮只使用 Fake 执行资格
（Task ACTIVE + Attempt OPEN + owner 已取得 + ExecutionSnapshot 已冻结，不实际发送）。

不越序铁律：只允许最早 QUEUED 成为候选；队首被隔离/轮换/启动对账挡住时，后续任务
即使自身条件全部满足也不得越过（除非主规格 Q02 明确允许的“确定未发送的坏队首”给出
本地终态后队列才能继续）。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

from core.domain import (
    ExecutionSettingsSnapshot,
    ReceiveSettingsSnapshot,
    SessionBindingMode,
    TargetExecutor,
    config_to_dict,
)
from infra.clock import Clock, SystemClock
from storage.database import Database, StorageError
from storage.lease_store import LeaseStoreError, ProjectLeaseStore
from storage.task_store import TaskStore


class SchedulerError(StorageError):
    """调度器内部失败（权威状态缺失、非法注入等系统异常）。"""

    code = "scheduler_error"


class ScheduleOutcome(str, Enum):
    """调度结果类别：任何结果都是本地可解释的结构化值。"""

    NO_TASK = "no_task"
    STARTED = "started"
    BLOCKED = "blocked"
    LOCAL_REJECTED = "local_rejected"


class ScheduleBlockReason(str, Enum):
    """阻塞原因（稳定值，供 UI 解释，不做中文字符串匹配）。"""

    STARTUP_RECONCILE_PENDING = "startup_reconcile_pending"
    ROTATION_PENDING = "rotation_pending"
    GLOBAL_SLOT_BUSY = "global_slot_busy"
    PROJECT_BUSY = "project_busy"
    RISK_OF_CONCURRENCY = "risk_of_concurrency"
    LOST_RACE = "lost_race"
    CORRUPT_HEAD_MAYBE_SENT = "corrupt_head_maybe_sent"


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    """调度结果：调用方依据 outcome + 结构化字段判断，禁止匹配中文字符串。"""

    outcome: ScheduleOutcome
    task_key: str | None = None
    task_id: str | None = None
    sequence: int | None = None
    attempt_id: str | None = None
    project_key: str | None = None
    block_reason: ScheduleBlockReason | None = None
    detail: str = ""
    local_rejected_count: int = 0


class ExecutionBuilder(Protocol):
    """从接收快照构建不可变执行快照（主规格 6.2）。"""

    def __call__(
        self, receive: ReceiveSettingsSnapshot, execution_started_at: str
    ) -> ExecutionSettingsSnapshot: ...


def receive_snapshot_from_json(text: str) -> ReceiveSettingsSnapshot:
    """把 tasks.ingress_snapshot_json 还原为接收快照（复用 T05 序列化格式）。"""
    data = json.loads(text)
    return ReceiveSettingsSnapshot(
        config_revision=data["config_revision"],
        committed_at=data["committed_at"],
        received_at=data["received_at"],
        effective_executor=TargetExecutor(data["effective_executor"]),
        directory=data["directory"],
        project_key=data["project_key"],
        agent=data["agent"],
        requested_model=data["requested_model"],
        binding_mode=SessionBindingMode(data["binding_mode"]),
        frozen_session_id=data.get("frozen_session_id"),
    )


def _default_execution_builder(
    receive: ReceiveSettingsSnapshot, execution_started_at: str
) -> ExecutionSettingsSnapshot:
    """T09 默认执行快照（与 settings_service.build_execution_snapshot 同语义）：
    FIXED_SESSION 使用接收时冻结的 session_id，缺失即报错；PROJECT_ROTATING 此刻尚无
    已提交新会话则解析为 None（轮换由后续流程执行，本卡不建真实会话）。
    """
    if receive.binding_mode is SessionBindingMode.FIXED_SESSION:
        if not receive.frozen_session_id:
            raise SchedulerError(
                "FIXED_SESSION 缺少已绑定的 session_id：进入执行阶段前必须明确绑定"
            )
        resolved = receive.frozen_session_id
    else:
        resolved = None  # PROJECT_ROTATING：T09 无已提交的新项目会话
    return ExecutionSettingsSnapshot(
        receive=receive,
        binding_revision=receive.config_revision,
        resolved_session_id=resolved,
        execution_started_at=execution_started_at,
    )


def _utc_iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat()


class Scheduler:
    """FIFO 调度器：同步、确定性的 tick()，不启动任何后台线程。

    决策（能否取得执行资格）与权威写入（Attempt + owner + ACTIVE）在同一事务内完成。
    """

    def __init__(
        self,
        db: Database,
        *,
        task_store: TaskStore | None = None,
        lease_store: ProjectLeaseStore | None = None,
        clock: Clock | None = None,
        attempt_id_factory: Callable[[str], str] | None = None,
        execution_builder: ExecutionBuilder | None = None,
        startup_ready: Callable[[], bool] | None = None,
        rotation_pending: Callable[[], bool] | None = None,
        max_local_rejects: int = 50,
    ) -> None:
        self._db = db
        self._tasks = task_store if task_store is not None else TaskStore(db)
        self._leases = lease_store if lease_store is not None else ProjectLeaseStore(db)
        self._clock = clock if clock is not None else SystemClock()
        self._attempt_id_factory = attempt_id_factory or (lambda task_key: str(uuid.uuid4()))
        self._execution_builder = execution_builder or _default_execution_builder
        self._startup_ready = startup_ready if startup_ready is not None else (lambda: True)
        self._rotation_pending = rotation_pending if rotation_pending is not None else (lambda: False)
        if max_local_rejects < 1:
            raise SchedulerError("max_local_rejects 必须为正整数")
        self._max_local_rejects = max_local_rejects
        # 仅测试用：第 N 个事务内检查点注入 meta 主键冲突，验证原子回滚
        self.fault_inject_after: int | None = None
        self._fault_step = 0

    # ------------------------------------------------------------------ tick

    def tick(self) -> ScheduleResult:
        """尝试启动最早可调度 QUEUED 任务。

        阻塞/无任务：不写任何业务数据；坏队首（无发送证据）给出本地终态后继续；
        STARTED：Attempt + project owner + Task=ACTIVE 在同一事务内原子提交。
        """
        if not self._startup_ready():
            return ScheduleResult(
                outcome=ScheduleOutcome.BLOCKED,
                block_reason=ScheduleBlockReason.STARTUP_RECONCILE_PENDING,
                detail="启动对账未完成，暂缓派发新任务",
            )
        self._fault_step = 0
        now_iso = _utc_iso(self._clock.now())
        local_rejected = 0
        try:
            with self._db.transaction():
                conn = self._db.connection
                for _ in range(self._max_local_rejects):
                    self._checkpoint(conn)
                    head = self._tasks.read_schedulable_head(conn)
                    if head is None:
                        return ScheduleResult(
                            outcome=ScheduleOutcome.NO_TASK,
                            local_rejected_count=local_rejected,
                        )
                    if head.state != "QUEUED":
                        return ScheduleResult(
                            outcome=ScheduleOutcome.BLOCKED,
                            block_reason=ScheduleBlockReason.RISK_OF_CONCURRENCY,
                            task_key=head.task_key, sequence=head.sequence,
                            local_rejected_count=local_rejected,
                            detail="队首已非 QUEUED，权威状态异常，禁止猜测",
                        )
                    if head.active_attempt_id is not None:
                        return ScheduleResult(
                            outcome=ScheduleOutcome.BLOCKED,
                            block_reason=ScheduleBlockReason.RISK_OF_CONCURRENCY,
                            task_key=head.task_key, sequence=head.sequence,
                            local_rejected_count=local_rejected,
                            detail="QUEUED 任务却带 active_attempt_id，状态不一致，禁止猜测",
                        )

                    if head.corrupt_reasons:
                        outcome = self._handle_corrupt_head(
                            conn, head, now_iso, local_rejected
                        )
                        if outcome is not None:
                            return outcome
                        local_rejected += 1
                        continue

                    result = self._evaluate_free_slot(conn, head, now_iso, local_rejected)
                    if result is not None:
                        return result
                    return self._start_head(conn, head, now_iso, local_rejected)
                return ScheduleResult(
                    outcome=ScheduleOutcome.LOCAL_REJECTED,
                    local_rejected_count=local_rejected,
                    detail="连续坏队首达到批处理上限，已在本地给出终态，可再次 tick 继续",
                )
        except sqlite3.IntegrityError as exc:
            raise SchedulerError(f"调度事务故障注入/冲突：{exc}") from exc

    # ------------------------------------------------------------------ corrupt head

    def _handle_corrupt_head(
        self, conn: sqlite3.Connection, head, now_iso: str, local_rejected: int
    ) -> ScheduleResult | None:
        """坏队首分离（Q02）：有 Attempt 证据（可能已发送）→ 隔离核验不跳过；
        无任何发送证据 → 给明确本地终态（不拼凑任务）并允许队列继续。"""
        maybe_sent = self._tasks.has_attempt_evidence(conn, head.task_key)
        if maybe_sent:
            return ScheduleResult(
                outcome=ScheduleOutcome.BLOCKED,
                block_reason=ScheduleBlockReason.CORRUPT_HEAD_MAYBE_SENT,
                task_key=head.task_key, sequence=head.sequence,
                local_rejected_count=local_rejected,
                detail="队首损坏且存在发送证据，必须隔离核验，禁止越过",
            )
        self._tasks.mark_corrupt_unsent(
            conn,
            task_key=head.task_key,
            blocked_reason="corrupt_unsent_local_reject",
            now=now_iso,
        )
        self._tasks.write_event(
            conn,
            event_code="CORRUPT_HEAD_LOCAL_REJECT",
            summary_zh=f"队首记录损坏且确认未发送，已给出本地终态（{','.join(head.corrupt_reasons)}）",
            ts_utc=now_iso,
            task_key=head.task_key,
        )
        return None  # 继续扫描下一个

    # ------------------------------------------------------------------ barriers

    def _evaluate_free_slot(
        self, conn: sqlite3.Connection, head, now_iso: str, local_rejected: int
    ) -> ScheduleResult | None:
        """全局槽、轮换屏障、项目隔离屏障检查（都不通过则返回 None 表示可启动）。"""
        if self._rotation_pending():
            return ScheduleResult(
                outcome=ScheduleOutcome.BLOCKED,
                block_reason=ScheduleBlockReason.ROTATION_PENDING,
                task_key=head.task_key, sequence=head.sequence,
                local_rejected_count=local_rejected,
                detail="会话轮换屏障未解除",
            )
        slot_busy, open_attempts, _active_tasks = self._tasks.slot_state(conn)
        if slot_busy:
            detail = self._active_detail()
            return ScheduleResult(
                outcome=ScheduleOutcome.BLOCKED,
                block_reason=ScheduleBlockReason.GLOBAL_SLOT_BUSY,
                task_key=head.task_key, sequence=head.sequence,
                project_key=self._project_key_of(head),
                local_rejected_count=local_rejected,
                detail=f"已有活动执行占用全局槽（活动 Attempt 数={open_attempts}）：{detail or ''}",
            )
        receive = receive_snapshot_from_json(head.ingress_snapshot_json)
        lease = self._leases.read_owner_in(conn, receive.project_key)
        if lease is not None:
            if lease.owner_task_key == head.task_key:
                return ScheduleResult(
                    outcome=ScheduleOutcome.BLOCKED,
                    block_reason=ScheduleBlockReason.RISK_OF_CONCURRENCY,
                    task_key=head.task_key, sequence=head.sequence,
                    project_key=receive.project_key,
                    local_rejected_count=local_rejected,
                    detail="项目 owner 与本队首相同但任务仍 QUEUED，状态不一致，禁止猜测",
                )
            if lease.occupying():
                return ScheduleResult(
                    outcome=ScheduleOutcome.BLOCKED,
                    block_reason=ScheduleBlockReason.PROJECT_BUSY,
                    task_key=head.task_key, sequence=head.sequence,
                    project_key=receive.project_key,
                    local_rejected_count=local_rejected,
                    detail=f"项目执行权被任务 {lease.owner_task_key}（attempt {lease.owner_attempt_id}，"
                    f"key={lease.project_key}，state={lease.state}）占用",
                )
        return None

    # ------------------------------------------------------------------ start

    def _start_head(
        self, conn: sqlite3.Connection, head, now_iso: str, local_rejected: int
    ) -> ScheduleResult:
        receive = receive_snapshot_from_json(head.ingress_snapshot_json)
        attempt_id = self._attempt_id_factory(head.task_key)
        if not attempt_id or not attempt_id.strip():
            raise SchedulerError(f"attempt_id_factory 返回空值：{attempt_id!r}")
        execution = self._execution_builder(receive, now_iso)
        execution_json = json.dumps(
            config_to_dict(execution), ensure_ascii=False, sort_keys=True
        )
        epoch = max(1, self._authority_epoch_of(conn, head))

        self._checkpoint(conn)  # Attempt 创建前
        started = self._tasks.activate_with_attempt(
            conn,
            task_key=head.task_key,
            attempt_id=attempt_id,
            kind="INITIAL",
            authority_epoch=epoch,
            execution_snapshot_json=execution_json,
            started_at=now_iso,
        )
        if not started:
            return ScheduleResult(
                outcome=ScheduleOutcome.BLOCKED,
                block_reason=ScheduleBlockReason.LOST_RACE,
                task_key=head.task_key, sequence=head.sequence,
                project_key=receive.project_key,
                local_rejected_count=local_rejected,
                detail="任务已不是 QUEUED（另一调度器先启动），本次竞争丢失",
            )
        self._checkpoint(conn)  # Attempt + Task=ACTIVE 后、owner 前
        try:
            self._leases.acquire_in(
                conn,
                project_key=receive.project_key,
                owner_task_key=head.task_key,
                owner_attempt_id=attempt_id,
                authority_epoch=epoch,
                now=now_iso,
            )
        except sqlite3.IntegrityError as exc:
            raise LeaseStoreError(
                f"项目执行权被并发占用或外键不成立，本次启动整体回滚：{exc}"
            ) from exc
        self._checkpoint(conn)  # owner 取得后
        self._tasks.write_event(
            conn,
            event_code="TASK_STARTED",
            summary_zh=f"调度取得执行资格 task={head.task_id} attempt={attempt_id} project={receive.project_key}",
            ts_utc=now_iso,
            task_key=head.task_key,
            attempt_id=attempt_id,
        )
        self._checkpoint(conn)  # 事件写入阶段
        return ScheduleResult(
            outcome=ScheduleOutcome.STARTED,
            task_key=head.task_key,
            task_id=head.task_id,
            sequence=head.sequence,
            attempt_id=attempt_id,
            project_key=receive.project_key,
            local_rejected_count=local_rejected,
            detail="Task=ACTIVE，Attempt=OPEN，project owner 已取得，ExecutionSnapshot 已冻结",
        )

    # ------------------------------------------------------------------ helpers

    def _active_detail(self) -> str:
        """当前活动执行的包装文本（只读，事务外读取也不影响决策权威）。"""
        detail = self._tasks.active_execution_detail(self._db.connection)
        if detail is None:
            return ""
        return (
            f"{detail['task_id']}(seq={detail['sequence']})"
            f"/attempt={detail['attempt_id']}[{detail['attempt_state']}/{detail['remote_state']}]"
        )

    def _project_key_of(self, head) -> str:
        receive = receive_snapshot_from_json(head.ingress_snapshot_json)
        return receive.project_key

    def _authority_epoch_of(self, conn: sqlite3.Connection, head) -> int:
        row = conn.execute(
            "SELECT authority_epoch FROM tasks WHERE task_key=?", (head.task_key,)
        ).fetchone()
        if row is None:
            raise SchedulerError(f"权威任务缺失：{head.task_key}")
        return int(row[0]) + 1  # 首次执行 epoch=1；后续 SUPERSEDED 流程再递增

    def _checkpoint(self, conn: sqlite3.Connection) -> None:
        """测试用故障注入：第 N 个检查点触发 meta 主键冲突，验证整体原子回滚。"""
        self._fault_step += 1
        if self.fault_inject_after is not None and self._fault_step == self.fault_inject_after:
            conn.execute("INSERT INTO meta (key, value) VALUES ('next_sequence', 'fault')")