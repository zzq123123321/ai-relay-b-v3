"""AI Relay B V3.0：权威 Result 存储原语（T11）。

依据：主规格 04.3 结果事务、17.2 结果内容与版本、17.5 S04–S09/D02。

设计要点：
- results 不可变（schema 触发器禁止 UPDATE/DELETE）；revision 在 task 内单调递增，
  UNIQUE(task_key,revision) 是版本唯一约束。
- "权威"的表达 = tasks.current_result_revision 指向该 task 的某个 revision；
  ResultRecord.status 据此派生出 domain.ResultStatus（AUTHORITATIVE/CANDIDATE）。
  失权候选直接拒绝、不落库（schema 无 CANDIDATE/SUPERSEDED 状态列），
  与主规格 04.3“另一个返回 STALE_ATTEMPT/STATE_CHANGED”一致。
- 本模块只提供 conn-scope 事务内写原语与只读查询；业务顺序（三次校验、
  结果+outbox+claim+event 同事务）由 core/result_commit.py 组合。
- 故障注入：复用 T10 模式，写原语入口推进 _fault_step，在第 N 个检查点
  撞 meta 主键制造 sqlite3.IntegrityError，让调用方整事务回滚（S04）。

不引入网络、UI、文件系统依赖；不做 schema 迁移。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from core.domain import ResultStatus

_STATE_COMPLETED = "COMPLETED"
_STATE_FAILED = "FAILED"
_STATE_STOPPED_BY_USER = "STOPPED_BY_USER"

_RESULT_STATES = (_STATE_COMPLETED, _STATE_FAILED, _STATE_STOPPED_BY_USER)

_RESULT_COLUMNS = (
    "result_id", "task_key", "attempt_id", "revision", "state", "source",
    "final_body", "protocol_text", "sha256", "remote_message_ids_json", "committed_at",
)


class ResultStoreError(Exception):
    """Result/Outbox 存储层可预期错误基类。"""

    code = "result_store_error"


class InconsistentAuthorityError(ResultStoreError):
    """存储状态自相矛盾（如 task 的 current_result_revision 指向不存在的版本）。"""

    code = "inconsistent_result_authority"


@dataclass(frozen=True, slots=True)
class TaskAuthorityRow:
    """事务内重读 task 的权威信息快照。"""

    task_key: str
    peer_id: str
    task_id: str
    state: str
    active_attempt_id: str | None
    authority_epoch: int
    current_result_revision: int


@dataclass(frozen=True, slots=True)
class AttemptAuthorityRow:
    """事务内重读 attempt 的权威信息快照。"""

    attempt_id: str
    task_key: str
    state: str
    authority_epoch: int
    remote_state: str | None


@dataclass(frozen=True, slots=True)
class ResultRecord:
    """不可变 Results 表行的只读模型；authoritative 由 task 指针派生。"""

    result_id: str
    task_key: str
    attempt_id: str
    revision: int
    state: str
    source: str
    final_body: str
    protocol_text: str
    sha256: str
    remote_message_ids: tuple[str, ...] = ()
    committed_at: str | None = None
    authoritative: bool = False
    delivery_id: str | None = None

    @property
    def status(self) -> ResultStatus:
        return ResultStatus.AUTHORITATIVE if self.authoritative else ResultStatus.CANDIDATE


@dataclass(frozen=True, slots=True)
class DeliveryRow:
    """Outbox 待提供项。"""

    delivery_id: str
    result_id: str
    peer_id: str
    state: str
    profile: str
    offered_count: int
    last_error: str | None


class ResultStore:
    """结果权威存储的原语集合（conn-scope 写原语 + 只读查询）。

    写原语必须由调用方在单一 Database.transaction()（BEGIN IMMEDIATE）内组合；
    每一处都会推进故障注入检查点。只有 list_pending_deliveries / get_* 为只读，
    不要求事务。
    """

    def __init__(self, db: Any) -> None:
        self._db = db
        self._fault_step = 0
        self.fault_inject_after: int | None = None

    # ---------------------------------------------------------------- read

    def get_result_by_id(self, result_id: str) -> ResultRecord | None:
        row = self._db.connection.execute(
            f"SELECT {', '.join(_RESULT_COLUMNS)} FROM results WHERE result_id=?",
            (result_id,),
        ).fetchone()
        return _record_from_row(row) if row is not None else None

    def get_authoritative_for_task(self, task_key: str) -> ResultRecord | None:
        row = self._db.connection.execute(
            "SELECT r.result_id, r.task_key, r.attempt_id, r.revision, r.state, r.source,"
            " r.final_body, r.protocol_text, r.sha256, r.remote_message_ids_json,"
            " r.committed_at"
            " FROM tasks t JOIN results r ON r.task_key=t.task_key"
            "   AND r.revision=t.current_result_revision"
            " WHERE t.task_key=? AND t.current_result_revision>0",
            (task_key,),
        ).fetchone()
        if row is None:
            return None
        record = _record_from_row(row)
        return _as_authoritative(record) if record is not None else None

    def read_task_authority_in(self, conn: sqlite3.Connection, task_key: str) -> TaskAuthorityRow | None:
        row = conn.execute(
            "SELECT task_key, peer_id, task_id, state, active_attempt_id, authority_epoch,"
            " current_result_revision FROM tasks WHERE task_key=?",
            (task_key,),
        ).fetchone()
        if row is None:
            return None
        return TaskAuthorityRow(
            task_key=row[0], peer_id=row[1], task_id=row[2], state=row[3],
            active_attempt_id=row[4], authority_epoch=row[5],
            current_result_revision=row[6],
        )

    def read_attempt_authority_in(self, conn: sqlite3.Connection,
                                  attempt_id: str) -> AttemptAuthorityRow | None:
        row = conn.execute(
            "SELECT attempt_id, task_key, state, authority_epoch, remote_state"
            " FROM attempts WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            return None
        return AttemptAuthorityRow(
            attempt_id=row[0], task_key=row[1], state=row[2],
            authority_epoch=row[3], remote_state=row[4],
        )

    def read_result_by_revision_in(self, conn: sqlite3.Connection, task_key: str,
                                   revision: int) -> ResultRecord | None:
        row = conn.execute(
            f"SELECT {', '.join(_RESULT_COLUMNS)} FROM results"
            " WHERE task_key=? AND revision=?",
            (task_key, revision),
        ).fetchone()
        return _record_from_row(row) if row is not None else None

    def next_revision_in(self, conn: sqlite3.Connection, task_key: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(revision),0)+1 FROM results WHERE task_key=?",
            (task_key,),
        ).fetchone()
        return int(row[0])

    # ---------------------------------------------------------------- txn writers

    def insert_result_in(
        self,
        conn: sqlite3.Connection,
        *,
        result_id: str,
        task_key: str,
        attempt_id: str,
        revision: int,
        state: str,
        source: str,
        final_body: str,
        protocol_text: str,
        sha256: str,
        remote_message_ids: list[str],
        committed_at: str,
    ) -> str:
        if state not in _RESULT_STATES:
            raise ResultStoreError(f"非法 result state：{state!r}")
        self._advance(conn)
        try:
            conn.execute(
                "INSERT INTO results (result_id, task_key, attempt_id, revision, state,"
                " source, final_body, protocol_text, sha256, remote_message_ids_json,"
                " committed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    result_id, task_key, attempt_id, revision, state, source,
                    final_body, protocol_text, sha256,
                    json.dumps(remote_message_ids, ensure_ascii=False), committed_at,
                ),
            )
        except sqlite3.IntegrityError:
            return "conflict"
        return "created"

    def publish_task_in(
        self,
        conn: sqlite3.Connection,
        *,
        task_key: str,
        current_result_revision: int,
        task_state: str,
        expected_attempt_id: str,
        expected_epoch: int,
        now: str,
    ) -> int:
        self._advance(conn)
        cursor = conn.execute(
            "UPDATE tasks SET state=?, current_result_revision=?, active_attempt_id=?" 
            " WHERE task_key=? AND state='ACTIVE' AND active_attempt_id=?"
            " AND authority_epoch=? AND current_result_revision=0",
            (task_state, current_result_revision, None,
             task_key, expected_attempt_id, expected_epoch),
        )
        return cursor.rowcount

    def terminalize_attempt_in(
        self,
        conn: sqlite3.Connection,
        *,
        attempt_id: str,
        attempt_state: str,
        ended_at: str,
    ) -> int:
        self._advance(conn)
        cursor = conn.execute(
            "UPDATE attempts SET state=?, ended_at=?"
            " WHERE attempt_id=? AND state='OPEN'",
            (attempt_state, ended_at, attempt_id),
        )
        return cursor.rowcount

    def insert_outbox_in(
        self,
        conn: sqlite3.Connection,
        *,
        delivery_id: str,
        result_id: str,
        peer_id: str,
        profile: str,
        now: str,
    ) -> str:
        self._advance(conn)
        try:
            conn.execute(
                "INSERT INTO outbox (delivery_id, result_id, peer_id, state, profile,"
                " offered_count) VALUES (?,?,?,?,?,0)",
                (delivery_id, result_id, peer_id, "PENDING", profile),
            )
        except sqlite3.IntegrityError:
            return "existing"
        return "created"

    def insert_claim_in(
        self,
        conn: sqlite3.Connection,
        *,
        endpoint: str,
        session_id: str,
        message_id: str,
        task_key: str,
        result_id: str,
    ) -> str:
        self._advance(conn)
        try:
            conn.execute(
                "INSERT INTO remote_claims (endpoint, session_id, message_id, task_key,"
                " result_id) VALUES (?,?,?,?,?)",
                (endpoint, session_id, message_id, task_key, result_id),
            )
        except sqlite3.IntegrityError:
            return "existing"
        return "created"

    def append_commit_event_in(
        self,
        conn: sqlite3.Connection,
        *,
        event_id: str,
        ts_utc: str,
        task_key: str,
        attempt_id: str,
        result_id: str,
        session_id: str | None,
    ) -> None:
        self._advance(conn)
        conn.execute(
            "INSERT INTO events (event_id, ts_utc, level, event_code, task_key,"
            " attempt_id, result_id, session_id, summary_zh, fields_redacted_json,"
            " critical_audit) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
            (
                event_id, ts_utc, "INFO", "RESULT_COMMITTED", task_key, attempt_id,
                result_id, session_id, "权威结果与Outbox完成一次原子提交",
                "{}",
            ),
        )

    # ---------------------------------------------------------------- delivery

    def list_pending_deliveries(self, *, limit: int = 1) -> list[DeliveryRow]:
        rows = self._db.connection.execute(
            "SELECT delivery_id, result_id, peer_id, state, profile, offered_count,"
            " last_error FROM outbox WHERE state='PENDING' ORDER BY delivery_id LIMIT ?",
            (limit,),
        ).fetchall()
        return [_delivery_from_row(row) for row in rows]

    def mark_offered_in(self, conn: sqlite3.Connection, *, delivery_id: str,
                        now: str) -> bool:
        self._advance(conn)
        cursor = conn.execute(
            "UPDATE outbox SET state='OFFERED', offered_at=?, offered_count=offered_count+1,"
            " last_error=NULL WHERE delivery_id=? AND state='PENDING'",
            (now, delivery_id),
        )
        return cursor.rowcount == 1

    # ---------------------------------------------------------------- internals

    def _advance(self, conn: sqlite3.Connection) -> None:
        self._fault_step += 1
        if self.fault_inject_after is not None and self._fault_step == self.fault_inject_after:
            conn.execute("INSERT INTO meta (key, value) VALUES ('next_sequence', 'fault')")


def _as_authoritative(record: ResultRecord) -> ResultRecord:
    return ResultRecord(
        result_id=record.result_id, task_key=record.task_key,
        attempt_id=record.attempt_id, revision=record.revision, state=record.state,
        source=record.source, final_body=record.final_body,
        protocol_text=record.protocol_text, sha256=record.sha256,
        remote_message_ids=record.remote_message_ids,
        committed_at=record.committed_at, authoritative=True,
    )


def _record_from_row(row: sqlite3.Row | tuple) -> ResultRecord | None:
    if row is None:
        return None
    message_ids = _safe_json_list(row[9])
    return ResultRecord(
        result_id=row[0], task_key=row[1], attempt_id=row[2], revision=int(row[3]),
        state=row[4], source=row[5], final_body=row[6], protocol_text=row[7],
        sha256=row[8], remote_message_ids=message_ids, committed_at=row[10],
    )


def _delivery_from_row(row: sqlite3.Row | tuple) -> DeliveryRow:
    return DeliveryRow(
        delivery_id=row[0], result_id=row[1], peer_id=row[2], state=row[3],
        profile=row[4], offered_count=int(row[5]), last_error=row[6],
    )


def _safe_json_list(raw: str) -> tuple[str, ...]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))