"""AI Relay B V3.0：持久任务认领与 FIFO（T07）。

边界（主规格 4.3 / 5.2 / 5.3）：
- 新 TaskId 认领在单个 BEGIN IMMEDIATE 事务内完成：去重 → 配额 → 分配 sequence
  → 插入 QUEUED → 写认领事件 → COMMIT。任何失败整体回滚，无半条任务。
- 序列权威来自 meta.next_sequence（SQLite），非进程内计数器，重启继续；
  事务失败被回滚时序列不发布，允许语义级 gap，不追求“看起来连续”。
- 同 ID 同 hash → EXISTING（不消耗序列/配额、不改旧记录）；
  同 ID 异 hash → CONFLICT（保留旧数据，last-write-wins 被拒绝）。
- 配额为可注入容量（默认附录 A queued_tasks=1000），占用按非终态计数。
- FIFO 一律按 sequence 排序，不依赖内存队列；本模块只读队首、不启动尝试。
- 坏记录：校验并暴露 corrupt_reasons，诊断可见、不静默删除；健康记录不受遮挡。
- 唯一约束竞态收敛为前提：并发同 ID INSERT 撞唯一键时重读判定 EXISTING/CONFLICT，
  不会把原始 UNIQUE 异常直接冒给上层。
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from enum import Enum

from core.domain import ReceiveSettingsSnapshot, config_to_dict
from core.protocol_v1 import content_digest, parse_message
from storage.database import Database, StorageError

DEFAULT_QUEUE_CAPACITY = 1000  # 附录A queued_tasks

_TASK_STATE_QUEUED = "QUEUED"
_TASK_STATE_ACTIVE = "ACTIVE"
_TASK_STATE_BLOCKED = "BLOCKED"
_OCCUPYING_STATES = (_TASK_STATE_QUEUED, _TASK_STATE_ACTIVE, _TASK_STATE_BLOCKED)
_LEGAL_STATES = frozenset(
    ("QUEUED", "ACTIVE", "BLOCKED", "COMPLETED", "FAILED", "STOPPED_BY_USER")
)

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

_SELECT_COLUMNS = (
    "task_key", "peer_id", "task_id", "sequence", "protocol_format",
    "raw_message", "body", "canonical_hash", "received_at",
    "ingress_snapshot_json", "state",
)


class TaskStoreError(StorageError):
    """TaskStore 内部失败（数据库损坏、权威状态缺失等系统异常）。"""

    code = "task_store_error"


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """tasks 表中的一行（含完整性校验结果）。"""

    task_key: str
    peer_id: str
    task_id: str
    sequence: int
    protocol_format: str
    raw_message: str
    body: str
    canonical_hash: str
    received_at: str
    ingress_snapshot_json: str
    state: str
    corrupt_reasons: tuple[str, ...] = ()


class ClaimOutcome(Enum):
    ACCEPTED = "ACCEPTED"
    EXISTING = "EXISTING"
    CONFLICT = "CONFLICT"
    QUEUE_FULL = "QUEUE_FULL"


@dataclass(frozen=True, slots=True)
class ClaimResult:
    """认领结果：业务结果用枚举字段表达，不做字符串匹配判断。"""

    outcome: ClaimOutcome
    task_key: str
    peer_id: str
    task_id: str
    canonical_hash: str
    sequence: int | None = None
    state: str | None = None
    recorded: TaskRecord | None = None


@dataclass(frozen=True, slots=True)
class ScheduleHead:
    """调度器事务内读取的队首（T09）。state 仅供防御复核，实际条件是 QUEUED。"""

    task_key: str
    task_id: str
    sequence: int
    state: str
    active_attempt_id: str | None
    blocked_reason: str | None
    ingress_snapshot_json: str
    corrupt_reasons: tuple[str, ...] = ()


class CancelOutcome(Enum):
    CANCELLED = "CANCELLED"
    NOT_FOUND = "NOT_FOUND"
    REJECTED_NOT_QUEUED = "REJECTED_NOT_QUEUED"
    REJECTED_HAS_ATTEMPT = "REJECTED_HAS_ATTEMPT"


@dataclass(frozen=True, slots=True)
class CancelResult:
    """“尚未发送”排队任务取消结果（主规格 5.3）。"""

    outcome: CancelOutcome
    task_key: str
    state: str | None = None
    attempt_count: int = 0


_ATTEMPT_KINDS = frozenset(
    ("INITIAL", "MANUAL_CONTINUE", "NEW_SESSION_RETRY", "MANUAL_RESOLUTION")
)


def make_task_key(peer_id: str, task_id: str) -> str:
    """前缀长度编码，保证 peer 与 task_id 之间无歧义，且 id 可含 ':'。"""
    return f"{len(peer_id)}:{peer_id}:{task_id}"


def _canonical_json(snapshot: ReceiveSettingsSnapshot) -> str:
    return json.dumps(config_to_dict(snapshot), ensure_ascii=False, sort_keys=True)


class TaskStore:
    """SQLite 权威任务认领与 FIFO。

    queue_capacity 为“占用容量”（QUEUED/ACTIVE/BLOCKED 非终态计数），
    默认附录 A 建议值 1000；测试可注入小容量验证配额。
    """

    def __init__(self, db: Database, *, queue_capacity: int = DEFAULT_QUEUE_CAPACITY) -> None:
        self._db = db
        if queue_capacity < 1:
            raise TaskStoreError("queue_capacity 必须为正整数")
        self._queue_capacity = queue_capacity
        self.fault_inject_after: int | None = None  # 仅测试用：第 N 条 SQL 后注入唯一键冲突

    @property
    def queue_capacity(self) -> int:
        return self._queue_capacity

    # ---------------------------------------------------------------- claim

    def claim(
        self,
        *,
        task_key: str,
        peer_id: str,
        task_id: str,
        protocol_format: str,
        raw_message: str,
        body: str,
        canonical_hash: str,
        receive_snapshot: ReceiveSettingsSnapshot,
        received_at: str,
    ) -> ClaimResult:
        snapshot_json = _canonical_json(receive_snapshot)
        completed = 0
        with self._db.transaction():
            conn = self._db.connection
            existing = self._select_existing(conn, task_key)
            completed = self._advance(completed)
            if existing is not None:
                if existing["canonical_hash"] == canonical_hash:
                    return ClaimResult(
                        outcome=ClaimOutcome.EXISTING,
                        task_key=task_key, peer_id=peer_id, task_id=task_id,
                        canonical_hash=canonical_hash,
                        sequence=existing["sequence"], state=existing["state"],
                        recorded=self._fetch(conn, task_key),
                    )
                return ClaimResult(
                    outcome=ClaimOutcome.CONFLICT,
                    task_key=task_key, peer_id=peer_id, task_id=task_id,
                    canonical_hash=canonical_hash,
                    sequence=existing["sequence"], state=existing["state"],
                    recorded=self._fetch(conn, task_key),
                )

            if self._occupancy(conn) >= self._queue_capacity:
                return ClaimResult(
                    outcome=ClaimOutcome.QUEUE_FULL,
                    task_key=task_key, peer_id=peer_id, task_id=task_id,
                    canonical_hash=canonical_hash,
                )
            completed = self._advance(completed)

            next_seq_row = conn.execute(
                "SELECT value FROM meta WHERE key='next_sequence'"
            ).fetchone()
            completed = self._advance(completed)
            if next_seq_row is None:
                raise TaskStoreError("meta.next_sequence 权威序列缺失，无法分配 sequence")
            try:
                sequence = int(next_seq_row[0])
            except (TypeError, ValueError) as exc:
                raise TaskStoreError("meta.next_sequence 损坏，无法解析为整数") from exc

            try:
                conn.execute(
                    "INSERT INTO tasks (task_key, peer_id, task_id, sequence,"
                    " protocol_format, raw_message, body, canonical_hash, received_at,"
                    " ingress_snapshot_json, state) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (task_key, peer_id, task_id, sequence, protocol_format, raw_message,
                     body, canonical_hash, received_at, snapshot_json, _TASK_STATE_QUEUED),
                )
            except sqlite3.IntegrityError:
                rerow = self._select_existing(conn, task_key)
                if rerow is None:
                    raise  # 不是并发同 ID 竞态，属于真实数据损坏
                if rerow["canonical_hash"] == canonical_hash:
                    return ClaimResult(
                        outcome=ClaimOutcome.EXISTING,
                        task_key=task_key, peer_id=peer_id, task_id=task_id,
                        canonical_hash=canonical_hash,
                        sequence=rerow["sequence"], state=rerow["state"],
                        recorded=self._fetch(conn, task_key),
                    )
                return ClaimResult(
                    outcome=ClaimOutcome.CONFLICT,
                    task_key=task_key, peer_id=peer_id, task_id=task_id,
                    canonical_hash=canonical_hash,
                    sequence=rerow["sequence"], state=rerow["state"],
                    recorded=self._fetch(conn, task_key),
                )
            completed = self._advance(completed)

            conn.execute(
                "UPDATE meta SET value=? WHERE key='next_sequence'", (str(sequence + 1),)
            )
            completed = self._advance(completed)

            conn.execute(
                "INSERT INTO events (event_id, ts_utc, level, event_code, task_key,"
                " summary_zh, fields_redacted_json, critical_audit)"
                " VALUES (?,?,?,?,?,?,?,0)",
                (
                    str(uuid.uuid4()), received_at, "INFO", "TASK_CLAIMED", task_key,
                    f"认领任务 {task_id}（peer={peer_id}）序列 {sequence}",
                    "{}",
                ),
            )
            completed = self._advance(completed)

            return ClaimResult(
                outcome=ClaimOutcome.ACCEPTED,
                task_key=task_key, peer_id=peer_id, task_id=task_id,
                canonical_hash=canonical_hash,
                sequence=sequence, state=_TASK_STATE_QUEUED,
                recorded=self._fetch(conn, task_key),
            )

    # ---------------------------------------------------------------- fifo

    def list_queued(self, *, limit: int | None = None) -> list[TaskRecord]:
        """按 sequence 升序读取全部 QUEUED（供 UI 暂显/诊断，非权威队列）。"""
        sql = (
            f"SELECT {', '.join(_SELECT_COLUMNS)} FROM tasks"
            " WHERE state='QUEUED' ORDER BY sequence ASC"
        )
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        conn = self._db.connection
        rows = conn.execute(sql, params).fetchall()
        return [self._record_from_row(row) for row in rows]

    def peek_next(self) -> TaskRecord | None:
        """读取最早的 QUEUED（sequence 最小）。只读，不启动尝试。"""
        sql = (
            f"SELECT {', '.join(_SELECT_COLUMNS)} FROM tasks"
            " WHERE state='QUEUED' ORDER BY sequence ASC LIMIT 1"
        )
        conn = self._db.connection
        row = conn.execute(sql).fetchone()
        return self._record_from_row(row) if row is not None else None

    # ---------------------------------------------------------------- scheduler primitives (T09)

    def read_schedulable_head(self, conn: sqlite3.Connection) -> ScheduleHead | None:
        """事务内读取最早 QUEUED（不含越序）。corrupt_reasons 直接携带完整性诊断。"""
        cols = _SELECT_COLUMNS + ("active_attempt_id", "blocked_reason")
        sql = (
            f"SELECT {', '.join(cols)} FROM tasks"
            " WHERE state='QUEUED' ORDER BY sequence ASC LIMIT 1"
        )
        row = conn.execute(sql).fetchone()
        if row is None:
            return None
        values = tuple(row)
        base_count = len(_SELECT_COLUMNS)
        record = self._record_from_row(values[:base_count])
        active_attempt_id = values[base_count]
        blocked_reason = values[base_count + 1]
        return ScheduleHead(
            task_key=record.task_key,
            task_id=record.task_id,
            sequence=record.sequence,
            state=record.state,
            active_attempt_id=active_attempt_id,
            blocked_reason=blocked_reason,
            ingress_snapshot_json=record.ingress_snapshot_json,
            corrupt_reasons=record.corrupt_reasons,
        )

    def slot_state(self, conn: sqlite3.Connection) -> tuple[bool, int, int]:
        """全局活动槽（主规格 01.3：默认同时只允许 1 个活动任务）。

        返回 (slot_busy, open_attempts, active_tasks)。OPEN 的 Attempt（无论是否正处于
        网络等待/冷却）都占用槽；activity 权威来自 SQLite，不依赖进程内存。
        """
        open_attempts = int(
            conn.execute("SELECT COUNT(*) FROM attempts WHERE state='OPEN'").fetchone()[0]
        )
        active_tasks = int(
            conn.execute("SELECT COUNT(*) FROM tasks WHERE state='ACTIVE'").fetchone()[0]
        )
        return (open_attempts > 0, open_attempts, active_tasks)

    def has_attempt_evidence(self, conn: sqlite3.Connection, task_key: str) -> bool:
        """该 task 是否存在任何 Attempt（作为“可能已发送”的坏队首分离证据）。"""
        row = conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE task_key=?", (task_key,)
        ).fetchone()
        return bool(row and row[0] > 0)

    def active_execution_detail(self, conn: sqlite3.Connection) -> dict | None:
        """当前活动执行信息（诊断/阻塞原因展示用）。"""
        row = conn.execute(
            "SELECT t.task_key, t.task_id, t.sequence, a.attempt_id, a.state, a.remote_state"
            " FROM attempts a JOIN tasks t ON t.task_key = a.task_key"
            " WHERE a.state='OPEN' ORDER BY t.sequence ASC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "task_key": row[0], "task_id": row[1], "sequence": row[2],
            "attempt_id": row[3], "attempt_state": row[4], "remote_state": row[5],
        }

    def activate_with_attempt(
        self,
        conn: sqlite3.Connection,
        *,
        task_key: str,
        attempt_id: str,
        kind: str,
        authority_epoch: int,
        execution_snapshot_json: str,
        started_at: str,
    ) -> bool:
        """在同一事务内创建 Attempt 并推进 Task → ACTIVE（主规格 5.3 原子启动）。

        调用方必须已持有 BEGIN IMMEDIATE 事务并先完成队首与屏障检查。仍返回 False
        表示任务已不是 QUEUED（并发竞争丢失），本方法不留下部分 Attempt。
        """
        if kind not in _ATTEMPT_KINDS:
            raise TaskStoreError(f"非法 Attempt kind：{kind!r}")
        head = conn.execute(
            "SELECT state, active_attempt_id FROM tasks WHERE task_key=?", (task_key,)
        ).fetchone()
        if head is None or head[0] != _TASK_STATE_QUEUED or head[1] is not None:
            return False
        conn.execute(
            "INSERT INTO attempts (attempt_id, task_key, kind, state, authority_epoch,"
            " execution_snapshot_json, started_at) VALUES (?,?,?,?,?,?,?)",
            (
                attempt_id, task_key, kind, "OPEN", authority_epoch,
                execution_snapshot_json, started_at,
            ),
        )
        cursor = conn.execute(
            "UPDATE tasks SET state=?, active_attempt_id=?, authority_epoch=?"
            " WHERE task_key=? AND state=?",
            (_TASK_STATE_ACTIVE, attempt_id, authority_epoch, task_key, _TASK_STATE_QUEUED),
        )
        if cursor.rowcount != 1:
            raise TaskStoreError("创建 Attempt 后任务状态被并发改变，禁止残留半个 Attempt")
        return True

    def mark_corrupt_unsent(
        self, conn: sqlite3.Connection, *, task_key: str, blocked_reason: str, now: str
    ) -> None:
        """坏队首且无发送证据：给明确本地终态（不拼凑任务），随后队列才可继续。"""
        cursor = conn.execute(
            "UPDATE tasks SET state=?, blocked_reason=? WHERE task_key=? AND state=?",
            ("STOPPED_BY_USER", blocked_reason, task_key, _TASK_STATE_QUEUED),
        )
        if cursor.rowcount != 1:
            raise TaskStoreError(f"坏队首本地终态写入失败：task {task_key}")
        _ = now  # 事件时间戳由调用方统一写入 write_event

    def write_event(
        self,
        conn: sqlite3.Connection,
        *,
        event_code: str,
        summary_zh: str,
        ts_utc: str,
        level: str = "INFO",
        task_key: str | None = None,
        attempt_id: str | None = None,
        critical_audit: int = 0,
    ) -> None:
        """在调用方事务内写入调度事件。"""
        conn.execute(
            "INSERT INTO events (event_id, ts_utc, level, event_code, task_key, attempt_id,"
            " summary_zh, fields_redacted_json, critical_audit)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()), ts_utc, level, event_code, task_key, attempt_id,
                summary_zh, "{}", critical_audit,
            ),
        )

    def cancel_queued(self, task_key: str, *, cancelled_at: str) -> CancelResult:
        """取消“尚未发送”的排队任务（主规格 5.3）。

        仅允许仍然 QUEUED 且无任何 Attempt 的任务；一旦创建 Attempt（进入发送边界）即拒绝。
        成功后写 STOPPED_BY_USER 终态与事件，不直接删除任务；下一 FIFO 任务才可成为队首。
        """
        try:
            with self._db.transaction():
                conn = self._db.connection
                row = conn.execute(
                    "SELECT state, active_attempt_id FROM tasks WHERE task_key=?",
                    (task_key,),
                ).fetchone()
                if row is None:
                    return CancelResult(outcome=CancelOutcome.NOT_FOUND, task_key=task_key)
                state, active_attempt_id = row[0], row[1]
                if state != _TASK_STATE_QUEUED:
                    return CancelResult(
                        outcome=CancelOutcome.REJECTED_NOT_QUEUED,
                        task_key=task_key, state=state,
                    )
                if active_attempt_id is not None:
                    return CancelResult(
                        outcome=CancelOutcome.REJECTED_HAS_ATTEMPT,
                        task_key=task_key, state=state,
                    )
                attempt_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM attempts WHERE task_key=?", (task_key,)
                    ).fetchone()[0]
                )
                if attempt_count > 0:
                    return CancelResult(
                        outcome=CancelOutcome.REJECTED_HAS_ATTEMPT,
                        task_key=task_key, state=state, attempt_count=attempt_count,
                    )
                conn.execute(
                    "UPDATE tasks SET state=?, blocked_reason=?"
                    " WHERE task_key=? AND state=?",
                    ("STOPPED_BY_USER", "queued_cancel", task_key, _TASK_STATE_QUEUED),
                )
                self.write_event(
                    conn,
                    event_code="QUEUED_CANCELLED",
                    summary_zh="用户显式取消未发送的排队任务",
                    ts_utc=cancelled_at,
                    task_key=task_key,
                )
                return CancelResult(
                    outcome=CancelOutcome.CANCELLED,
                    task_key=task_key, state="STOPPED_BY_USER", attempt_count=0,
                )
        except sqlite3.IntegrityError as exc:
            raise TaskStoreError(f"取消排队任务写入失败并回滚：{exc}") from exc

    # ---------------------------------------------------------------- internals

    def _select_existing(self, conn: sqlite3.Connection, task_key: str) -> dict | None:
        row = conn.execute(
            "SELECT sequence, state, canonical_hash FROM tasks WHERE task_key=?",
            (task_key,),
        ).fetchone()
        return (
            {"sequence": row[0], "state": row[1], "canonical_hash": row[2]}
            if row is not None
            else None
        )

    def _occupancy(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE state IN (?, ?, ?)", _OCCUPYING_STATES
        ).fetchone()
        return int(row[0]) if row else 0

    def _fetch(self, conn: sqlite3.Connection, task_key: str) -> TaskRecord:
        select = ", ".join(_SELECT_COLUMNS)
        row = conn.execute(f"SELECT {select} FROM tasks WHERE task_key=?", (task_key,)).fetchone()
        if row is None:
            raise TaskStoreError(f"认领后读回失败：task 不存在 {task_key}")
        return self._record_from_row(row)

    def _advance(self, completed: int) -> int:
        completed += 1
        if self.fault_inject_after is not None and completed == self.fault_inject_after:
            conn = self._db.connection
            conn.execute(  # 注入 next_sequence 主键冲突 → IntegrityError → 整体回滚
                "INSERT INTO meta (key, value) VALUES ('next_sequence', 'fault')"
            )
        return completed

    def _record_from_row(self, row: sqlite3.Row | tuple) -> TaskRecord:
        values = tuple(row) if not isinstance(row, sqlite3.Row) else tuple(row)
        by_name = dict(zip(_SELECT_COLUMNS, values))
        record = TaskRecord(
            task_key=by_name["task_key"],
            peer_id=by_name["peer_id"],
            task_id=by_name["task_id"],
            sequence=by_name["sequence"],
            protocol_format=by_name["protocol_format"],
            raw_message=by_name["raw_message"],
            body=by_name["body"],
            canonical_hash=by_name["canonical_hash"],
            received_at=by_name["received_at"],
            ingress_snapshot_json=by_name["ingress_snapshot_json"],
            state=by_name["state"],
        )
        return TaskRecord(
            task_key=record.task_key, peer_id=record.peer_id, task_id=record.task_id,
            sequence=record.sequence, protocol_format=record.protocol_format,
            raw_message=record.raw_message, body=record.body,
            canonical_hash=record.canonical_hash, received_at=record.received_at,
            ingress_snapshot_json=record.ingress_snapshot_json, state=record.state,
            corrupt_reasons=self._integrity_issues(record),
        )

    def _integrity_issues(self, record: TaskRecord) -> tuple[str, ...]:
        issues: list[str] = []
        if record.state not in _LEGAL_STATES:
            issues.append(f"非法 task state：{record.state!r}")
        if not record.received_at:
            issues.append("received_at 为空")
        if not _SHA256_HEX.match(record.canonical_hash):
            issues.append(f"canonical_hash 损坏：{record.canonical_hash!r}")
        else:
            try:
                reparsed = parse_message(
                    record.raw_message, envelope_limit_bytes=None, body_limit_bytes=None
                )
            except Exception:
                issues.append("raw_message 无法重新解析为协议消息")
            else:
                if content_digest(reparsed) != record.canonical_hash:
                    issues.append("canonical_hash 与 raw_message 的规范摘要不一致")
        return tuple(issues)