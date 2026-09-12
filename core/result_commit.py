"""AI Relay B V3.0：权威结果原子提交（T11）+ 正常完成项目执行权安全释放（T11R）。

依据：主规格 04.3 结果事务、17.2 结果内容与版本、17.5 S04/S05/S06；
任务卡：同一事务提交 result、task/attempt 终态、Outbox 与消息认领。

T11R：权威完成 + 远端安全空闲 IDLE_VERIFIED 时，在同一个结果事务内按
task/attempt/epoch 精确释放 ACTIVE project lease（release_active_in）。非 ACTIVE
（QUARANTINED/ROTATING）、owner/epoch 不匹配、远端非 IDLE_VERIFIED 一律保留项目执行权；
lease 不存在不视为释放成功也不回滚合法结果。project_key 只取自 Attempt 已冻结的
execution_snapshot_json，Candidate 不能决定释放哪个项目。

不变量（核心价值）：
- 权威 = task.current_result_revision 指向的不可变 result revision；
- 提交前在 BEGIN IMMEDIATE 事务内重新校验 task/attempt 的 active_attempt_id 与
  authority_epoch（双重 CAS），旧 worker/旧 attempt/旧 epoch 一律 LOST_AUTHORITY；
- 同一事务完成：INSERT result → 发布 task/attempt 终态 → INSERT outbox →
  消息认领 → 安全条件满足时精确释放 ACTIVE lease → 关键审计事件；任一步 SQL 失败
  整事务回滚（S04），release 的 DELETE 也随事务回滚；
- 相同结果重复提交幂等 ALREADY_COMMITTED；同 result_id 异正文
  RESULT_ID_CONFLICT；双 worker 竞争只有一方赢得提交（S05）；
- R1 protocol_text 在提交时以不可变方式生成（含固定 RESPONSE MESSAGE_ID=result_id，
  补复制逐字一致），sha256=SHA-256(protocol_text)。

网络/剪贴板/文件导出不进入本事务；只做持久 DB 权威状态。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Mapping

from core.domain import AttemptStatus, TaskStatus, transition_attempt, transition_task
from core.protocol_v1 import ProtocolFormat, wrap_response
from infra.clock import Clock, SystemClock
from storage.database import Database
from storage.lease_store import (
    LeaseReleaseOutcome,
    LeaseReleaseResult,
    ProjectLeaseStore,
)
from storage.result_store import ResultStore, ResultStoreError

_COMMIT_STATE_TASK_ATTEMPT: Mapping[str, tuple[str, TaskStatus, AttemptStatus]] = {
    "COMPLETED": ("COMPLETED", TaskStatus.COMPLETED, AttemptStatus.COMPLETED),
    "FAILED": ("FAILED", TaskStatus.FAILED, AttemptStatus.FAILED),
    "STOPPED_BY_USER": ("STOPPED_BY_USER", TaskStatus.STOPPED_BY_USER,
                        AttemptStatus.STOPPED),
}

_STATE_ACTIVE = "ACTIVE"
_STATE_OPEN = "OPEN"

_SAFE_RELEASE_REMOTE_STATE = "IDLE_VERIFIED"

ResultIdFactory = Callable[[str, str, int], str]
DeliveryIdFactory = Callable[[str, int], str]
EventIdFactory = Callable[[str], str]


class CommitOutcome(Enum):
    """权威结果提交结论（主规格 04.3 的 STALE_ATTEMPT/STATE_CHANGED 聚为 LOST_AUTHORITY，
    detail 给出具体原因）。"""

    COMMITTED = "committed"                    # 赢得当前权威提交
    ALREADY_COMMITTED = "already_committed"    # 同结果幂等（同 result_id 同正文）
    RESULT_ID_CONFLICT = "result_id_conflict"  # 同 result_id 但正文不同
    LOST_AUTHORITY = "lost_authority"          # 旧 attempt/旧 epoch/已终态/已有权威


class CommitRequestError(ValueError):
    """候选结果本身不合法（空正文、非法状态等）。"""


class ResultCommitError(RuntimeError):
    """权威提交遇到无法归类为业务结果的存储故障。"""


@dataclass(frozen=True, slots=True)
class CommitResult:
    outcome: CommitOutcome
    result_id: str | None = None
    revision: int | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ResultClaim:
    """远端完成消息认领的 Fake identity（T11：无真实 attribution，只测原子合同）。"""

    endpoint: str
    session_id: str
    message_id: str


@dataclass(frozen=True, slots=True)
class CandidateResult:
    """一个 Worker 提出的完成候选——不是天然的权威结果。

    result_id 由调用方工厂注入（测试必须确定性）；提交时按
    task/attempt/epoch 双重 CAS 决定 COMMITTED 或失权。
    """

    task_key: str
    attempt_id: str
    authority_epoch: int
    result_state: str
    source: str
    final_body: str
    result_id: str
    claim: ResultClaim | None = None
    remote_state: str = "IDLE_VERIFIED"


def build_result_response(
    *,
    result_state: str,
    attempt_id: str,
    revision: int,
    source: str,
    remote_state: str,
    final_body: str,
    in_reply_to: str,
    response_message_id: str,
) -> str:
    """构造 R1 RESPONSE 完整文本（主规格 17.2：扩展状态放正文，不加新头）。

    RESPONSE MESSAGE_ID 固定为 result_id，保证同一不可变结果补复制逐字一致。
    """
    header_body = "\n".join(
        (
            f"任务状态：{result_state}",
            f"执行尝试：{attempt_id}",
            f"结果版本：{revision}",
            f"结果来源：{source.lower()}",
            f"远端状态：{remote_state}",
        )
    ) + "\n\n" + final_body
    return wrap_response(
        header_body,
        in_reply_to=in_reply_to,
        protocol_format=ProtocolFormat.V1,
        round_number=0,
        max_rounds=3,
        response_message_id=response_message_id,
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ResultCommitService:
    """把一个候选结果安全地提升为当前任务的唯一权威结果（单事务提交）。"""

    def __init__(
        self,
        db: Database,
        *,
        result_store: ResultStore | None = None,
        lease_store: ProjectLeaseStore | None = None,
        clock: Clock | None = None,
        result_id_factory: ResultIdFactory | None = None,
        delivery_id_factory: DeliveryIdFactory | None = None,
        event_id_factory: EventIdFactory | None = None,
    ) -> None:
        self._db = db
        self._ops = result_store if result_store is not None else ResultStore(db)
        self._leases = lease_store if lease_store is not None else ProjectLeaseStore(db)
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._result_id_factory = result_id_factory or _uuid_result_id
        self._delivery_id_factory = delivery_id_factory or _uuid_delivery_id
        self._event_id_factory = event_id_factory or _uuid_event_id

    def commit(self, candidate: CandidateResult) -> CommitResult:
        if not candidate.final_body.strip():
            raise CommitRequestError("权威结果正文不能为空")
        if candidate.result_state not in _COMMIT_STATE_TASK_ATTEMPT:
            raise CommitRequestError(f"非法 result state：{candidate.result_state!r}")
        if candidate.authority_epoch < 0:
            raise CommitRequestError("authority_epoch 不能为负")
        now_iso = _utc_iso(self._clock.now())
        try:
            with self._db.transaction():
                conn = self._db.connection
                task_row = self._ops.read_task_authority_in(conn, candidate.task_key)
                blocked = self._authority_gate(candidate, task_row, conn)
                if blocked is not None:
                    return blocked
                assert task_row is not None
                attempt_row = self._ops.read_attempt_authority_in(
                    conn, candidate.attempt_id
                )
                if attempt_row is None or attempt_row.task_key != candidate.task_key:
                    return CommitResult(
                        CommitOutcome.LOST_AUTHORITY,
                        detail="attempt 不存在或不属于本 task",
                    )
                if attempt_row.state != _STATE_OPEN:
                    return CommitResult(
                        CommitOutcome.LOST_AUTHORITY,
                        detail=f"attempt 非 OPEN（state={attempt_row.state}），不能提交权威结果",
                    )
                if attempt_row.authority_epoch != candidate.authority_epoch:
                    return CommitResult(
                        CommitOutcome.LOST_AUTHORITY,
                        detail=(
                            f"attempt 的 authority_epoch={attempt_row.authority_epoch}"
                            f" 与候选 {candidate.authority_epoch} 不一致"
                        ),
                    )
                if candidate.claim is not None and self._claim_already_bound(
                    conn, candidate.claim, candidate.task_key
                ):
                    return CommitResult(
                        CommitOutcome.LOST_AUTHORITY,
                        detail="该远端完成消息已被认领，不能作为第二个权威完成的证据",
                    )
                revision = self._ops.next_revision_in(conn, candidate.task_key)
                protocol_text = build_result_response(
                    result_state=candidate.result_state,
                    attempt_id=candidate.attempt_id,
                    revision=revision,
                    source=candidate.source,
                    remote_state=candidate.remote_state,
                    final_body=candidate.final_body,
                    in_reply_to=task_row.task_id,
                    response_message_id=candidate.result_id,
                )
                payload_hash = sha256_hex(protocol_text)
                created = self._ops.insert_result_in(
                    conn,
                    result_id=candidate.result_id,
                    task_key=candidate.task_key,
                    attempt_id=candidate.attempt_id,
                    revision=revision,
                    state=candidate.result_state,
                    source=candidate.source,
                    final_body=candidate.final_body,
                    protocol_text=protocol_text,
                    sha256=payload_hash,
                    remote_message_ids=(
                        [candidate.claim.message_id] if candidate.claim else []
                    ),
                    committed_at=now_iso,
                )
                if created == "conflict":
                    return CommitResult(
                        CommitOutcome.RESULT_ID_CONFLICT,
                        result_id=candidate.result_id,
                        detail="result_id/revision 唯一性冲突（同身份另一方案先落库）",
                    )
                task_state, task_status, attempt_status = (
                    _COMMIT_STATE_TASK_ATTEMPT[candidate.result_state]
                )
                transition_task(TaskStatus.ACTIVE, task_status)
                transition_attempt(AttemptStatus.OPEN, attempt_status)
                published = self._ops.publish_task_in(
                    conn,
                    task_key=candidate.task_key,
                    current_result_revision=revision,
                    task_state=task_state,
                    expected_attempt_id=candidate.attempt_id,
                    expected_epoch=candidate.authority_epoch,
                    now=now_iso,
                )
                if published != 1:
                    return CommitResult(
                        CommitOutcome.LOST_AUTHORITY,
                        result_id=candidate.result_id,
                        detail="task 终态发布 CAS 未成功（已有权威/状态变化）",
                    )
                attempt_done = self._ops.terminalize_attempt_in(
                    conn,
                    attempt_id=candidate.attempt_id,
                    attempt_state=attempt_status.value.upper(),
                    ended_at=now_iso,
                )
                if attempt_done != 1:
                    return CommitResult(
                        CommitOutcome.LOST_AUTHORITY,
                        result_id=candidate.result_id,
                        detail="attempt 终态 CAS 未成功",
                    )
                delivery_id = self._delivery_id_factory(candidate.task_key, revision)
                outbox = self._ops.insert_outbox_in(
                    conn,
                    delivery_id=delivery_id,
                    result_id=candidate.result_id,
                    peer_id=task_row.peer_id,
                    profile="legacy_v1",
                    now=now_iso,
                )
                if outbox == "existing":
                    return CommitResult(
                        CommitOutcome.LOST_AUTHORITY,
                        result_id=candidate.result_id,
                        revision=revision,
                        detail="该 result/peer 的 Outbox 已存在",
                    )
                if candidate.claim is not None:
                    self._ops.insert_claim_in(
                        conn,
                        endpoint=candidate.claim.endpoint,
                        session_id=candidate.claim.session_id,
                        message_id=candidate.claim.message_id,
                        task_key=candidate.task_key,
                        result_id=candidate.result_id,
                    )
                lease_note = self._release_completed_lease_in(
                    conn,
                    task_key=candidate.task_key,
                    attempt_id=candidate.attempt_id,
                    authority_epoch=candidate.authority_epoch,
                    result_state=candidate.result_state,
                    remote_state=candidate.remote_state,
                )
                self._ops.append_commit_event_in(
                    conn,
                    event_id=self._event_id_factory(candidate.result_id),
                    ts_utc=now_iso,
                    task_key=candidate.task_key,
                    attempt_id=candidate.attempt_id,
                    result_id=candidate.result_id,
                    session_id=candidate.claim.session_id if candidate.claim else None,
                )
                return CommitResult(
                    CommitOutcome.COMMITTED,
                    result_id=candidate.result_id,
                    revision=revision,
                    detail=f"revision={revision} 权威提交成功{lease_note}",
                )
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            if "meta.key" in message:
                raise  # 故障注入：让事务整体回滚并原样传播，绝不伪装成业务结果
            raise ResultCommitError(
                f"权威结果提交遇到未分类的存储约束冲突：{message}"
            ) from exc

    # ---------------------------------------------------------------- gates

    def _release_completed_lease_in(
        self,
        conn: sqlite3.Connection,
        *,
        task_key: str,
        attempt_id: str,
        authority_epoch: int,
        result_state: str,
        remote_state: str,
    ) -> str:
        """同一结果事务内的正常完成安全释放（T11R）。

        只在“成功权威完成 + 远端安全空闲 IDLE_VERIFIED”且能精确解析出项目身份时，
        尝试按 task/attempt/epoch 精确释放 ACTIVE lease。其余情况一律不触碰 lease：
        - lease 不存在：继续，不视为释放成功（也不回滚合法结果）；
        - owner/attempt/epoch 不一致：绝不删除（防误删他人/新代 owner）；
        - QUARANTINED/ROTATING：保留，只允许后续只读核验/人工风险确认解除；
        - 无法从 Attempt 冻结执行快照解析 project_key：跳过释放，仍完成提交。
        返回一段只读解释文本（不做业务判断依据）；失败信号由调用方最终 CommitResult 承载。
        """
        if result_state != "COMPLETED":
            return "；非 COMPLETED 终态，未释放项目执行权"
        if remote_state != _SAFE_RELEASE_REMOTE_STATE:
            return f"；远端状态 {remote_state!r} 非 IDLE_VERIFIED，不释放项目执行权"
        project_key = self._attempt_project_key_in(conn, attempt_id)
        if project_key is None:
            return "；无法从 Attempt 执行快照解析 project_key，跳过 lease 释放"
        release: LeaseReleaseResult = self._leases.release_active_in(
            conn,
            project_key=project_key,
            owner_task_key=task_key,
            owner_attempt_id=attempt_id,
            authority_epoch=authority_epoch,
        )
        if release.outcome is LeaseReleaseOutcome.RELEASED:
            return f"；项目 {project_key} lease 已安全释放（同事务）"
        if release.outcome is LeaseReleaseOutcome.NOT_FOUND:
            return f"；项目 {project_key} 无 lease 记录，未执行释放"
        if release.outcome is LeaseReleaseOutcome.OWNER_MISMATCH:
            return f"；项目 {project_key} lease owner 与权威不匹配，保留并绝不删除"
        return f"；项目 {project_key} lease 状态不允许正常释放，保留"

    def _attempt_project_key_in(
        self, conn: sqlite3.Connection, attempt_id: str
    ) -> str | None:
        """从 Attempt 已冻结的 execution_snapshot_json 解析 project_key（T11R）。

        project 身份必须来自 T09 冻结的不可变执行快照（receive.project_key），
        不允许 Candidate 自行决定释放哪个项目。快照缺失/损坏/无 project_key 时返回 None，
        调用方应跳过释放而不是猜测。
        """
        row = conn.execute(
            "SELECT execution_snapshot_json FROM attempts WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if row is None or not row[0]:
            return None
        try:
            data = json.loads(row[0])
            project_key = data["receive"]["project_key"]
        except (ValueError, KeyError, TypeError):
            return None
        if not isinstance(project_key, str) or not project_key.strip():
            return None
        return project_key

    def _authority_gate(
        self, candidate: CandidateResult, task_row, conn
    ) -> CommitResult | None:
        if task_row is None:
            return CommitResult(
                CommitOutcome.LOST_AUTHORITY, detail="task 不存在"
            )
        if task_row.current_result_revision > 0:
            current = self._ops.read_result_by_revision_in(
                conn, candidate.task_key, task_row.current_result_revision
            )
            if current is None:
                raise ResultStoreError(
                    "task.current_result_revision 指向不存在的 result版本（数据损坏）"
                )
            same_identity = (
                current.result_id == candidate.result_id
                and current.final_body == candidate.final_body
                and current.state == candidate.result_state
                and current.attempt_id == candidate.attempt_id
            )
            if same_identity:
                return CommitResult(
                    CommitOutcome.ALREADY_COMMITTED,
                    result_id=current.result_id,
                    revision=current.revision,
                    detail="相同 result identity 与正文已权威提交（幂等）",
                )
            if current.result_id == candidate.result_id:
                return CommitResult(
                    CommitOutcome.RESULT_ID_CONFLICT,
                    result_id=current.result_id,
                    revision=current.revision,
                    detail="同 result_id 但正文/状态/attempt 不同：禁止静默覆盖不可变结果",
                )
            return CommitResult(
                CommitOutcome.LOST_AUTHORITY,
                result_id=current.result_id,
                revision=current.revision,
                detail="已有其它权威结果，旧候选无权覆盖",
            )
        if task_row.state != _STATE_ACTIVE:
            return CommitResult(
                CommitOutcome.LOST_AUTHORITY,
                detail=f"task 非 ACTIVE（state={task_row.state}），不能提交结果",
            )
        if task_row.active_attempt_id != candidate.attempt_id:
            return CommitResult(
                CommitOutcome.LOST_AUTHORITY,
                detail=(
                    f"task.active_attempt_id={task_row.active_attempt_id} 与候选"
                    f" {candidate.attempt_id} 不一致（旧 worker 晚到）"
                ),
            )
        if task_row.authority_epoch != candidate.authority_epoch:
            return CommitResult(
                CommitOutcome.LOST_AUTHORITY,
                detail=(
                    f"task.authority_epoch={task_row.authority_epoch} 与候选"
                    f" {candidate.authority_epoch} 不一致（旧 epoch 失权）"
                ),
            )
        return None

    def _claim_already_bound(self, conn, claim: ResultClaim, task_key: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM remote_claims WHERE endpoint=? AND session_id=? AND message_id=?",
            (claim.endpoint, claim.session_id, claim.message_id),
        ).fetchone()
        return row is not None

    # ---------------------------------------------------------------- factories

    @property
    def result_store(self) -> ResultStore:
        return self._ops

    def new_result_id(self) -> str:
        """运行时默认工厂入口（测试应注入确定性工厂，不使用本方法）。"""
        return str(uuid.uuid4())


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _uuid_result_id(task_key: str, attempt_id: str, epoch: int) -> str:
    return f"res-{uuid.uuid4().hex}"


def _uuid_delivery_id(task_key: str, revision: int) -> str:
    return f"deliv-{uuid.uuid4().hex}"


def _uuid_event_id(result_id: str) -> str:
    return f"evt-{uuid.uuid4().hex}"