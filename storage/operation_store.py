"""AI Relay B V3.0：发送 Operation 账本与一次发送资格（T10）。

依据（主规格 03.1 / 04.3 / 07.2，T10 卡）：
- operation_id 是一次可能产生远端副作用的操作实例身份；operation_key 是同一逻辑副作用
  操作的稳定幂等键（多个 timer/Proposal/双击指向同一次动作必须收敛到同一 operation）。
- 状态机：PREPARED → SENDING → ACCEPTED/REJECTED/UNKNOWN；UNKNOWN 不是失败，不能自动重发；
  并发同类 Proposal 被 UNIQUE(operation_key) 收敛，同 key 异内容必须显式 CONFLICT。
- 网络调用严格位于数据库事务之外；本模块只做持久化账本与 CAS 原语，不接触任何网络/发送。
- 三次权威检查（task/attempt/epoch/control_revision/owner）由调用方（DispatchService）
  在本模块提供的 conn 作用域原语之上执行；写入失败不产生非法半状态。

状态持久化使用 schema 大写串（'PREPARED'…'UNKNOWN'），与任务/尝试表风格一致。
T10 不做 CANCELLED 写入：schema 状态列支持 CANCELLED，但 core/domain 的 OperationStatus
  T03 枚举无 CANCELLED，且 T10 不修改 core/domain.py，因此“发送权被撤销”用
  保持 PREPARED + 结构化 SEND_RIGHT_REVOKED 表达。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from core.domain import OperationStatus, transition_operation
from storage.database import Database, StorageError

_OPERATION_COLUMNS = (
    "operation_id", "operation_key", "kind", "task_key", "attempt_id",
    "authority_epoch", "control_revision", "endpoint", "session_id", "project_key",
    "interruption_id", "state", "pre_snapshot_json", "prompt_hash", "prompt_text",
    "remote_user_id", "evidence_json", "created_at", "updated_at", "finalized_at",
)

_STATE_PREPARED = "PREPARED"
_STATE_SENDING = "SENDING"
_STATE_ACCEPTED = "ACCEPTED"
_STATE_REJECTED = "REJECTED"
_STATE_UNKNOWN = "UNKNOWN"

_SEND_KINDS = ("INITIAL_SEND", "CONTINUE")

# 同一 endpoint/session 同时最多一个未决发送（与 schema 部分唯一索引语义一致）。
_UNRESOLVED_STATES = (_STATE_SENDING, _STATE_UNKNOWN)


class OperationStoreError(StorageError):
    """OperationStore 系统级失败（数据库损坏、权威数据缺位等）。"""

    code = "operation_store_error"


class PrepareOutcome(str, Enum):
    CREATED = "created"
    EXISTING = "existing"      # 同 key 同身份：幂等收敛，复用已有 operation
    CONFLICT = "conflict"      # 同 key 异身份：拒绝，原 operation 不修改


class AcquireOutcome(str, Enum):
    GRANTED = "granted"                # 唯一发送权（PREPARED → SENDING 已提交）
    NOT_FOUND = "not_found"
    NOT_PREPARED = "not_prepared"      # 已 SENDING/终态，本调用方没有发送权
    REVOKED = "revoked"                # 权威条件（epoch/owner 等）已失效，保持 PREPARED
    SESSION_HAS_UNRESOLVED = "session_has_unresolved"


class FinalizeOutcome(str, Enum):
    FINALIZED = "finalized"
    ALREADY_FINAL = "already_final"
    INVALID_TRANSITION = "invalid_transition"
    NOT_FOUND = "not_found"
    CAS_LOST = "cas_lost"


@dataclass(frozen=True, slots=True)
class PrepareResult:
    outcome: PrepareOutcome
    operation_id: str | None = None
    record: "OperationRecord | None" = None


@dataclass(frozen=True, slots=True)
class AcquireResult:
    outcome: AcquireOutcome
    operation_id: str | None = None
    state: str | None = None


@dataclass(frozen=True, slots=True)
class FinalizeResult:
    outcome: FinalizeOutcome
    operation_id: str | None = None
    state: str | None = None


@dataclass(frozen=True, slots=True)
class OperationRecord:
    """operations 表的一行只读投影。"""

    operation_id: str
    operation_key: str
    kind: str
    task_key: str | None
    attempt_id: str | None
    authority_epoch: int | None
    control_revision: int | None
    endpoint: str
    session_id: str | None
    project_key: str | None
    interruption_id: str | None
    state: str
    pre_snapshot_json: str
    prompt_hash: str | None
    prompt_text: str | None
    remote_user_id: str | None
    evidence_json: str
    created_at: str
    updated_at: str
    finalized_at: str | None

    def identity_tuple(self) -> tuple:
        return (
            self.kind, self.task_key, self.attempt_id, self.authority_epoch,
            self.control_revision, self.endpoint, self.session_id,
            self.project_key, self.interruption_id,
        )


class DispatchProposalLike(Protocol):
    """DispatchService 传入的身份契约；避免 operation_store 反向依赖 core.dispatch。"""

    operation_key: str
    kind: str
    task_key: str
    attempt_id: str
    authority_epoch: int
    control_revision: int
    endpoint: str
    session_id: str | None
    project_key: str
    interruption_id: str | None


@dataclass(frozen=True, slots=True)
class AuthorityStatus:
    """一次权威检查（task/attempt/project owner）的结构化结果。ok 为 True 才允许发送。"""

    task_present: bool
    task_active: bool
    active_attempt_matches: bool
    task_epoch_matches: bool
    attempt_present: bool
    attempt_open: bool
    attempt_epoch_matches: bool
    control_revision_matches: bool
    lease_present: bool
    lease_owner_matches: bool
    lease_epoch_matches: bool
    lease_allows_send: bool
    lease_state: str | None = None

    @property
    def ok(self) -> bool:
        return all(
            (
                self.task_present, self.task_active, self.active_attempt_matches,
                self.task_epoch_matches, self.attempt_present, self.attempt_open,
                self.attempt_epoch_matches, self.control_revision_matches,
                self.lease_present, self.lease_owner_matches, self.lease_epoch_matches,
                self.lease_allows_send,
            )
        )

    @property
    def detail(self) -> str:
        if self.ok:
            return ""
        issues: list[str] = []
        if not self.task_present:
            issues.append("task 不存在")
        else:
            if not self.task_active:
                issues.append(f"task 非 ACTIVE（task.state={self._task_state_or_na}）")
            if not self.active_attempt_matches:
                issues.append("task.active_attempt_id 与 proposal.attempt_id 不一致")
            if not self.task_epoch_matches:
                issues.append("task.authority_epoch 与 proposal 不一致")
        if not self.attempt_present:
            issues.append("attempt 不存在")
        else:
            if not self.attempt_open:
                issues.append(f"attempt 非 OPEN（attempt.state={self._attempt_state_or_na}）")
            if not self.attempt_epoch_matches:
                issues.append("attempt.authority_epoch 与 proposal 不一致")
            if not self.control_revision_matches:
                issues.append("attempt.control_revision 与 proposal 不一致")
        if not self.lease_present:
            issues.append("project lease 不存在")
        else:
            if not self.lease_owner_matches:
                issues.append("lease owner 与 task/attempt 不一致")
            if not self.lease_epoch_matches:
                issues.append("lease authority_epoch 与 proposal 不一致")
            if not self.lease_allows_send:
                issues.append(f"lease state={self.lease_state!r} 不允许发送（仅 ACTIVE 可发送）")
        return "；".join(issues)

    _task_state: str | None = None
    _attempt_state: str | None = None

    @property
    def _task_state_or_na(self) -> str:
        return self._task_state or "NA"

    @property
    def _attempt_state_or_na(self) -> str:
        return self._attempt_state or "NA"


class OperationStore:
    """持久 Operation 账本：读取、CAS、状态受控转换与 SENDING 恢复。

    全部写方法为 conn 作用域原语（除 sweep_stale_sendings 自持事务），
    必须由调用方在单一 Database.transaction() 内组合使用。
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        # 仅测试用：第 N 个内建检查点注入 meta 主键冲突，验证本模块写入整体回滚。
        self.fault_inject_after: int | None = None
        self._fault_step = 0

    # ---------------------------------------------------------------- read

    def read_by_key(self, operation_key: str) -> OperationRecord | None:
        return self._select_by_key(self._db.connection, operation_key)

    def read_by_id(self, operation_id: str) -> OperationRecord | None:
        row = self._db.connection.execute(
            f"SELECT {', '.join(_OPERATION_COLUMNS)} FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        return _record_from_row(row) if row is not None else None

    def read_operations_for(self, *, task_key: str | None = None,
                            attempt_id: str | None = None) -> list[OperationRecord]:
        sql = f"SELECT {', '.join(_OPERATION_COLUMNS)} FROM operations WHERE 1=1"
        params: list[object] = []
        if task_key is not None:
            sql += " AND task_key=?"
            params.append(task_key)
        if attempt_id is not None:
            sql += " AND attempt_id=?"
            params.append(attempt_id)
        sql += " ORDER BY created_at, operation_id"
        rows = self._db.connection.execute(sql, tuple(params)).fetchall()
        return [_record_from_row(row) for row in rows]

    def unresolved_send_for_session(self, *, endpoint: str,
                                    session_id: str | None) -> OperationRecord | None:
        if session_id is None:
            return None
        row = self._db.connection.execute(
            "SELECT operation_id, operation_key, state FROM operations"
            " WHERE endpoint=? AND session_id=? AND kind IN (?, ?) AND state IN (?, ?)"
            " ORDER BY created_at, operation_id LIMIT 1",
            (endpoint, session_id, *("INITIAL_SEND", "CONTINUE"), *_UNRESOLVED_STATES),
        ).fetchone()
        if row is None:
            return None
        return self.read_by_id(row[0])

    # ---------------------------------------------------------------- authority

    def check_authority_in(self, conn: sqlite3.Connection,
                           proposal: DispatchProposalLike) -> AuthorityStatus:
        """在调用方事务内检查发送权威条件（主规格 07.2① / 08 边界）。

        task=ACTIVE 且 active_attempt/epoch 匹配；attempt=OPEN 且 epoch/control_revision
        匹配；project lease 必须由本 task+attempt+epoch 持有且 state=ACTIVE
        （QUARANTINED/ROTATING 不允许发送）。
        """
        task = conn.execute(
            "SELECT state, active_attempt_id, authority_epoch FROM tasks WHERE task_key=?",
            (proposal.task_key,),
        ).fetchone()
        task_present = task is not None
        task_active = bool(task and task[0] == "ACTIVE")
        active_attempt_matches = bool(task and task[1] == proposal.attempt_id)
        task_epoch_matches = bool(task and task[2] == proposal.authority_epoch)
        task_state = task[0] if task else None

        attempt = conn.execute(
            "SELECT task_key, state, authority_epoch, control_revision FROM attempts"
            " WHERE attempt_id=?",
            (proposal.attempt_id,),
        ).fetchone()
        attempt_present = bool(attempt and attempt[0] == proposal.task_key)
        attempt_open = bool(attempt and attempt[1] == "OPEN")
        attempt_epoch_matches = bool(attempt and attempt[2] == proposal.authority_epoch)
        control_revision_matches = bool(attempt and attempt[3] == proposal.control_revision)
        attempt_state = attempt[1] if attempt else None

        lease = conn.execute(
            "SELECT owner_task_key, owner_attempt_id, authority_epoch, state"
            " FROM project_leases WHERE project_key=?",
            (proposal.project_key,),
        ).fetchone()
        lease_present = lease is not None
        lease_owner_matches = bool(
            lease and lease[0] == proposal.task_key and lease[1] == proposal.attempt_id
        )
        lease_epoch_matches = bool(lease and lease[2] == proposal.authority_epoch)
        lease_state = lease[3] if lease else None
        lease_allows_send = bool(lease_state == "ACTIVE")

        return AuthorityStatus(
            task_present=task_present,
            task_active=task_active,
            active_attempt_matches=active_attempt_matches,
            task_epoch_matches=task_epoch_matches,
            attempt_present=attempt_present,
            attempt_open=attempt_open,
            attempt_epoch_matches=attempt_epoch_matches,
            control_revision_matches=control_revision_matches,
            lease_present=lease_present,
            lease_owner_matches=lease_owner_matches,
            lease_epoch_matches=lease_epoch_matches,
            lease_allows_send=lease_allows_send,
            lease_state=lease_state,
            _task_state=task_state,
            _attempt_state=attempt_state,
        )

    # ---------------------------------------------------------------- prepare

    def prepare_in(
        self,
        conn: sqlite3.Connection,
        *,
        proposal: DispatchProposalLike,
        operation_id: str,
        pre_snapshot_json: str,
        prompt_text: str,
        prompt_hash: str,
        created_at: str,
    ) -> PrepareResult:
        """事务内写 PREPARED。

        同 operation_key 收敛：首次插入成功 → CREATED；UNIQUE 冲突时重读判定身份——
        相同 → EXISTING（幂等复用），不同 → CONFLICT（原 operation 不修改）。
        """
        self._advance(conn)
        try:
            conn.execute(
                "INSERT INTO operations (operation_id, operation_key, kind, task_key,"
                " attempt_id, authority_epoch, control_revision, endpoint, session_id,"
                " project_key, interruption_id, state, pre_snapshot_json, prompt_hash,"
                " prompt_text, evidence_json, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    operation_id, proposal.operation_key, proposal.kind, proposal.task_key,
                    proposal.attempt_id, proposal.authority_epoch, proposal.control_revision,
                    proposal.endpoint, proposal.session_id, proposal.project_key,
                    proposal.interruption_id, _STATE_PREPARED, pre_snapshot_json,
                    prompt_hash, prompt_text, "{}", created_at, created_at,
                ),
            )
        except sqlite3.IntegrityError:
            existing = self._select_by_key(conn, proposal.operation_key)
            if existing is None:
                raise OperationStoreError(
                    f"operation_key UNIQUE 冲突但重读不存在（数据损坏）：{proposal.operation_key}"
                )
            if (
                existing.identity_tuple() == proposal_identity(proposal)
                and existing.operation_id == operation_id
            ):
                return PrepareResult(outcome=PrepareOutcome.EXISTING,
                                     operation_id=existing.operation_id, record=existing)
            return PrepareResult(outcome=PrepareOutcome.CONFLICT,
                                 operation_id=existing.operation_id, record=existing)
        record = self._select_by_key(conn, proposal.operation_key)
        if record is None:
            raise OperationStoreError("PREPARED 写入后读回失败（数据损坏）")
        return PrepareResult(outcome=PrepareOutcome.CREATED,
                             operation_id=record.operation_id, record=record)

    # ---------------------------------------------------------------- acquire

    def acquire_send_right_in(
        self,
        conn: sqlite3.Connection,
        *,
        proposal: DispatchProposalLike,
        operation_id: str,
        now: str,
    ) -> AcquireResult:
        """事务内 CAS：PREPARED → SENDING，只有取得成功的一方才拥有一次发送权。

        同 endpoint/session 已存在未决发送（SENDING/UNKNOWN）时拒绝（结构化结果）；
        并发越过预检查仍可能触发部分唯一索引 → 由 DispatchService 在边界转译为
        SESSION_HAS_UNRESOLVED_OPERATION，不允许裸 IntegrityError 泄漏。
        """
        row = conn.execute(
            "SELECT operation_id, state FROM operations WHERE operation_key=?",
            (proposal.operation_key,),
        ).fetchone()
        if row is None:
            return AcquireResult(outcome=AcquireOutcome.NOT_FOUND)
        current_id, current_state = row[0], row[1]
        if current_state != _STATE_PREPARED:
            return AcquireResult(outcome=AcquireOutcome.NOT_PREPARED,
                                 operation_id=current_id, state=current_state)
        if proposal.session_id is not None:
            blocker = conn.execute(
                "SELECT operation_id FROM operations WHERE endpoint=? AND session_id=?"
                " AND operation_id<>? AND kind IN (?, ?) AND state IN (?, ?) LIMIT 1",
                (proposal.endpoint, proposal.session_id, operation_id, *_SEND_KINDS,
                 *_UNRESOLVED_STATES),
            ).fetchone()
            if blocker is not None:
                return AcquireResult(outcome=AcquireOutcome.SESSION_HAS_UNRESOLVED,
                                     operation_id=current_id, state=current_state)
        self._advance(conn)
        cursor = conn.execute(
            "UPDATE operations SET state=?, updated_at=? WHERE operation_key=?"
            " AND operation_id=? AND state=?",
            (_STATE_SENDING, now, proposal.operation_key, operation_id, _STATE_PREPARED),
        )
        if cursor.rowcount != 1:
            return AcquireResult(outcome=AcquireOutcome.NOT_PREPARED,
                                 operation_id=current_id, state=current_state)
        return AcquireResult(outcome=AcquireOutcome.GRANTED,
                             operation_id=current_id, state=_STATE_SENDING)

    # ---------------------------------------------------------------- finalize

    def finalize_in(
        self,
        conn: sqlite3.Connection,
        *,
        proposal: DispatchProposalLike,
        operation_id: str,
        target_state: str,
        remote_user_id: str | None,
        evidence_json: str,
        finalized_at: str,
        now: str,
    ) -> FinalizeResult:
        """事务内最终态写入（SENDING → ACCEPTED/REJECTED/UNKNOWN；统计收敛用 SENDING 源）。

        UNKNOWN 允许经对账收敛到 ACCEPTED/REJECTED（core.domain 状态机）；任何反向
        （终态 → SENDING、UNKNOWN → SENDING）一律拒绝且不修改数据。
        """
        row = conn.execute(
            "SELECT operation_id, state FROM operations WHERE operation_key=?",
            (proposal.operation_key,),
        ).fetchone()
        if row is None:
            return FinalizeResult(outcome=FinalizeOutcome.NOT_FOUND)
        current_id, from_state = row[0], row[1]
        if from_state == target_state:
            return FinalizeResult(outcome=FinalizeOutcome.ALREADY_FINAL,
                                  operation_id=current_id, state=from_state)
        try:
            transition_operation(
                OperationStatus(from_state.lower()),
                OperationStatus(target_state.lower()),
            )
        except Exception:
            return FinalizeResult(outcome=FinalizeOutcome.INVALID_TRANSITION,
                                  operation_id=current_id, state=from_state)
        self._advance(conn)
        cursor = conn.execute(
            "UPDATE operations SET state=?, remote_user_id=?, evidence_json=?,"
            " finalized_at=?, updated_at=? WHERE operation_key=? AND operation_id=?"
            " AND state=?",
            (target_state, remote_user_id, evidence_json, finalized_at, now,
             proposal.operation_key, operation_id, from_state),
        )
        if cursor.rowcount != 1:
            current = self._select_by_key(conn, proposal.operation_key)
            return FinalizeResult(outcome=FinalizeOutcome.CAS_LOST,
                                  operation_id=current_id,
                                  state=current.state if current else None)
        return FinalizeResult(outcome=FinalizeOutcome.FINALIZED,
                              operation_id=current_id, state=target_state)

    # ---------------------------------------------------------------- recover

    def recover_sending_as_unknown_in(
        self,
        conn: sqlite3.Connection,
        *,
        operation_key: str,
        operation_id: str,
        now: str,
        evidence: str | None = None,
    ) -> bool:
        """SENDING → UNKNOWN（重启恢复）：视为可能已发送，不重发、不自动重试。"""
        cursor = conn.execute(
            "UPDATE operations SET state=?, evidence_json=?, finalized_at=?, updated_at=?"
            " WHERE operation_key=? AND operation_id=? AND state=?",
            (_STATE_UNKNOWN, evidence or '{"decision": "restart_recovery"}', now, now,
             operation_key, operation_id, _STATE_SENDING),
        )
        return cursor.rowcount == 1

    def sweep_stale_sendings(self, *, now: str) -> list[str]:
        """自持事务：把所有残留 SENDING 定为 UNKNOWN（进程重启后调用）。

        SENDING 之后程序崩溃视为可能发送，不能自动重发；收敛到 UNKNOWN 并保留证据。
        """
        recovered: list[str] = []
        with self._db.transaction():
            conn = self._db.connection
            rows = conn.execute(
                "SELECT operation_id, operation_key FROM operations WHERE state=?",
                (_STATE_SENDING,),
            ).fetchall()
            for operation_id, operation_key in rows:
                self._advance(conn)
                if self.recover_sending_as_unknown_in(
                    conn, operation_key=operation_key, operation_id=operation_id, now=now
                ):
                    recovered.append(operation_key)
        return recovered

    # ---------------------------------------------------------------- internals

    def _select_by_key(self, conn: sqlite3.Connection,
                       operation_key: str) -> OperationRecord | None:
        row = conn.execute(
            f"SELECT {', '.join(_OPERATION_COLUMNS)} FROM operations WHERE operation_key=?",
            (operation_key,),
        ).fetchone()
        return _record_from_row(row) if row is not None else None

    def _advance(self, conn: sqlite3.Connection) -> None:
        self._fault_step += 1
        if self.fault_inject_after is not None and self._fault_step == self.fault_inject_after:
            conn.execute("INSERT INTO meta (key, value) VALUES ('next_sequence', 'fault')")


def proposal_identity(proposal: DispatchProposalLike) -> tuple:
    """proposal 的身份指纹：与 OperationRecord.identity_tuple() 对齐用于冲突判定。"""
    return (
        proposal.kind, proposal.task_key, proposal.attempt_id, proposal.authority_epoch,
        proposal.control_revision, proposal.endpoint, proposal.session_id,
        proposal.project_key, proposal.interruption_id,
    )


def _record_from_row(row: sqlite3.Row | tuple) -> OperationRecord:
    values = tuple(row)
    by_name = dict(zip(_OPERATION_COLUMNS, values))
    return OperationRecord(
        operation_id=by_name["operation_id"],
        operation_key=by_name["operation_key"],
        kind=by_name["kind"],
        task_key=by_name["task_key"],
        attempt_id=by_name["attempt_id"],
        authority_epoch=by_name["authority_epoch"],
        control_revision=by_name["control_revision"],
        endpoint=by_name["endpoint"],
        session_id=by_name["session_id"],
        project_key=by_name["project_key"],
        interruption_id=by_name["interruption_id"],
        state=by_name["state"],
        pre_snapshot_json=by_name["pre_snapshot_json"],
        prompt_hash=by_name["prompt_hash"],
        prompt_text=by_name["prompt_text"],
        remote_user_id=by_name["remote_user_id"],
        evidence_json=by_name["evidence_json"],
        created_at=by_name["created_at"],
        updated_at=by_name["updated_at"],
        finalized_at=by_name["finalized_at"],
    )


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)