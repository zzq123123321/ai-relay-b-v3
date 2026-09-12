"""AI Relay B V3.0：只读任务历史查询契约（T16-B1）。

依据：主规格 14.1（PAGE02 任务记录/ PANEL01 任务详情）、14.3（历史结果缺失
不回退）、17.5（UI-A07/UI-A12/UI-B15、U05）；分轮 T16 卡与 A 端 5 项合同修正。

职责边界（本轮）：
- 只做只读查询：分页列表、task 详情、Attempt 续接链、Result 版本、exact result_id。
- 不做：任何 INSERT / UPDATE / DELETE / BEGIN IMMEDIATE；不创建 outbox；不表示
  “复制成功 / A 端已收到”；不做 schema migration（migration = NO，v1 已具备
  全部权威表与字段，results 由 schema 触发器保证不可变）。
- 坏数据原则：身份字段仍可读、corrupt_reasons 可见、无法解析的 JSON 字段 = None，
  绝不用 except: continue 隐藏坏记录，健康记录不受遮挡。
- exact result_id：按 id 精确读取，缺失返回 None，绝不回退到权威版本/其它 task。
- 搜索范围冻结：task_id / project_key / result_id（instr() 子串，普通文本无
  wildcard 语义）；不搜索 raw_message / body / final_body / protocol_text；
  不从正文猜“任务标题”（schema 无权威 title 字段）。
- project_key 搜索用 json_valid 保护 json_extract，malformed JSON 只影响自身，
  不能让整页查询失败；SQLite 若缺 JSON1 扩展则在构造时明确报错（不改 schema）。
- 交付状态按 result_id + task.peer_id 精确关联 outbox（UNIQUE(result_id,peer_id)
  保证至多一行，绝不按 delivery_id 排序“猜最新”）。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from storage.database import Database, StorageError
from storage.schema import read_schema_version

_TASK_STATES = frozenset(
    ("QUEUED", "ACTIVE", "BLOCKED", "COMPLETED", "FAILED", "STOPPED_BY_USER")
)
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_PROJECT_KEY_PATH = "$.project_key"
_DIRECTORY_PATH = "$.directory"
_REQUESTED_MODEL_PATH = "$.requested_model"
_FROZEN_SESSION_PATH = "$.frozen_session_id"
_CONFIG_REVISION_PATH = "$.config_revision"

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


class TaskQueryError(StorageError):
    """TaskQueries 只读查询的输入/环境错误（明确抛出，不静默忽略）。"""

    code = "task_query_error"


@dataclass(frozen=True, slots=True)
class TaskHistoryRow:
    """任务记录列表中的一行（PAGE02）。corrupt_reasons 携带完整性诊断。"""

    task_key: str
    peer_id: str
    task_id: str
    sequence: int
    protocol_format: str
    state: str
    blocked_reason: str | None
    received_at: str
    current_result_revision: int
    current_result_id: str | None
    delivery_state: str | None
    project_key: str | None
    directory: str | None
    corrupt_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskPageResult:
    """keyset 分页结果。total_count 为相同 search/filter 条件下的查询时刻总览数字。"""

    items: tuple[TaskHistoryRow, ...]
    next_cursor: int | None
    has_more: bool
    total_count: int


@dataclass(frozen=True, slots=True)
class AttemptHistoryRow:
    """Attempt 续接链节点。resolved_session_id 从 immutable 执行快照安全解析，失败=None。"""

    attempt_id: str
    task_key: str
    parent_attempt_id: str | None
    kind: str
    state: str
    authority_epoch: int
    remote_state: str
    started_at: str
    ended_at: str | None
    resolved_session_id: str | None
    corrupt_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskDetail:
    """任务详情（PANEL01）：身份、原始任务、执行上下文与完整 Attempt 链。"""

    task_key: str
    peer_id: str
    task_id: str
    sequence: int
    protocol_format: str
    state: str
    blocked_reason: str | None
    received_at: str
    authority_epoch: int
    active_attempt_id: str | None
    current_result_revision: int
    raw_message: str
    body: str
    canonical_hash: str
    project_key: str | None
    directory: str | None
    requested_model: str | None
    frozen_session_id: str | None
    config_revision: int | None
    attempts: tuple[AttemptHistoryRow, ...]
    corrupt_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResultVersionRow:
    """一个 Result 版本（revision DESC）。authoritative 由 current_result_revision 派生。"""

    result_id: str
    task_key: str
    attempt_id: str
    revision: int
    state: str
    source: str
    sha256: str
    committed_at: str
    authoritative: bool
    delivery_state: str | None
    corrupt_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResultDetail:
    """按 result_id 精确读取的完整 Result；缺失返回 None，无任何回落。"""

    result_id: str
    task_key: str
    attempt_id: str
    revision: int
    state: str
    source: str
    final_body: str
    protocol_text: str
    sha256: str
    remote_message_ids: tuple[str, ...]
    committed_at: str
    authoritative: bool
    delivery_state: str | None
    corrupt_reasons: tuple[str, ...] = ()


class TaskQueries:
    """只读任务历史查询服务（UI 不直接 SQL，全部经本服务）。"""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._probe_json1()

    # ---------------------------------------------------------------- list

    def list_tasks(
        self,
        *,
        cursor_sequence: int | None = None,
        limit: int = _DEFAULT_LIMIT,
        search_text: str | None = None,
        state_filter: tuple[str, ...] | None = None,
    ) -> TaskPageResult:
        """按 sequence DESC 的 keyset 分页读取任务记录。

        - 游标：cursor_sequence = 上一页“最后一项”的 sequence → WHERE sequence < ?；
          首次传 None。新任务获得更大 sequence，不会污染旧游标的下一页（无翻页漂移）。
        - limit 约束 1..100；state_filter 只接受 schema 六种状态，未知状态明确报错。
        - 推荐一次读 limit+1 判定 has_more。
        """
        limit = self._checked_limit(limit)
        if cursor_sequence is not None:
            if not isinstance(cursor_sequence, int) or cursor_sequence < 1:
                raise TaskQueryError(
                    f"cursor_sequence 必须为正整数，收到 {cursor_sequence!r}"
                )
        states = self._checked_states(state_filter)

        base_where, base_params = self._build_filters(states, search_text)
        count_where = f"WHERE {base_where}" if base_where else ""

        count_row = self._db.connection.execute(
            f"SELECT COUNT(*) FROM tasks t {count_where}", base_params
        ).fetchone()
        total_count = int(count_row[0]) if count_row else 0

        where_parts: list[str] = []
        params: list[object] = list(base_params)
        if base_where:
            where_parts.append(base_where)
        if cursor_sequence is not None:
            where_parts.append("t.sequence < ?")
            params.append(cursor_sequence)
        where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        params.append(limit + 1)
        rows = self._db.connection.execute(
            f"SELECT t.task_key, t.peer_id, t.task_id, t.sequence, t.protocol_format,"
            f" t.state, t.blocked_reason, t.received_at, t.ingress_snapshot_json,"
            f" t.canonical_hash, t.current_result_revision, ar.result_id, o.state"
            f" FROM tasks t"
            f" LEFT JOIN results ar"
            f"   ON ar.task_key=t.task_key AND ar.revision=t.current_result_revision"
            f" LEFT JOIN outbox o"
            f"   ON o.result_id=ar.result_id AND o.peer_id=t.peer_id"
            f" {where_sql} ORDER BY t.sequence DESC LIMIT ?",
            params,
        ).fetchall()

        page_rows = rows[:limit]
        items = tuple(self._history_from_row(row) for row in page_rows)
        has_more = len(rows) > limit
        next_cursor = items[-1].sequence if (has_more and items) else None
        return TaskPageResult(
            items=items, next_cursor=next_cursor,
            has_more=has_more, total_count=total_count,
        )

    # ---------------------------------------------------------------- detail

    def list_task_attempts(self, task_key: str) -> tuple[AttemptHistoryRow, ...]:
        """按 started_at ASC, attempt_id ASC 的确定顺序读取完整 Attempt 续接链。"""
        rows = self._db.connection.execute(
            "SELECT attempt_id, task_key, parent_attempt_id, kind, state, authority_epoch,"
            " remote_state, started_at, ended_at, execution_snapshot_json"
            " FROM attempts WHERE task_key=?"
            " ORDER BY started_at ASC, attempt_id ASC",
            (task_key,),
        ).fetchall()
        return tuple(self._attempt_from_row(row) for row in rows)

    def get_task_detail(self, task_key: str) -> TaskDetail | None:
        """任务详情；不存在返回 None（不扔“找不到”作为系统异常）。"""
        row = self._db.connection.execute(
            "SELECT task_key, peer_id, task_id, sequence, protocol_format, state,"
            " blocked_reason, received_at, authority_epoch, active_attempt_id,"
            " current_result_revision, raw_message, body, canonical_hash,"
            " ingress_snapshot_json"
            " FROM tasks WHERE task_key=?",
            (task_key,),
        ).fetchone()
        if row is None:
            return None
        task_key_, peer_id, task_id, sequence, protocol_format, state, blocked_reason, (
            received_at
        ), authority_epoch, active_attempt_id, current_rev, raw_message, body, (
            canonical_hash
        ), ingress_json = tuple(row)

        ingress, ingress_issues = _parse_ingress(ingress_json)
        issues = list(ingress_issues)
        if current_rev > 0 and not self._authority_result_exists(task_key_, current_rev):
            issues.append("current_result_revision 指向不存在的 Result 版本")
        if not _SHA256_HEX.match(canonical_hash):
            issues.append(f"canonical_hash 损坏：{canonical_hash!r}")

        attempts = self.list_task_attempts(task_key_)
        return TaskDetail(
            task_key=task_key_, peer_id=peer_id, task_id=task_id,
            sequence=sequence, protocol_format=protocol_format, state=state,
            blocked_reason=blocked_reason, received_at=received_at,
            authority_epoch=authority_epoch, active_attempt_id=active_attempt_id,
            current_result_revision=current_rev, raw_message=raw_message,
            body=body, canonical_hash=canonical_hash,
            project_key=ingress["project_key"], directory=ingress["directory"],
            requested_model=ingress["requested_model"],
            frozen_session_id=ingress["frozen_session_id"],
            config_revision=ingress["config_revision"],
            attempts=attempts,
            corrupt_reasons=tuple(issues),
        )

    # ---------------------------------------------------------------- results

    def list_task_result_versions(self, task_key: str) -> tuple[ResultVersionRow, ...]:
        """全部 Result 版本（revision DESC）。delivery 按 result_id+task.peer_id 精确关联。"""
        peer_row = self._db.connection.execute(
            "SELECT peer_id FROM tasks WHERE task_key=?", (task_key,)
        ).fetchone()
        if peer_row is None:
            return ()
        peer_id = peer_row[0]
        current_rev_row = self._db.connection.execute(
            "SELECT current_result_revision FROM tasks WHERE task_key=?", (task_key,)
        ).fetchone()
        current_rev = int(current_rev_row[0]) if current_rev_row is not None else 0
        rows = self._db.connection.execute(
            "SELECT r.result_id, r.task_key, r.attempt_id, r.revision, r.state, r.source,"
            " r.sha256, r.committed_at, r.remote_message_ids_json, o.state"
            " FROM results r"
            " LEFT JOIN outbox o ON o.result_id=r.result_id AND o.peer_id=?"
            " WHERE r.task_key=? ORDER BY r.revision DESC",
            (peer_id, task_key),
        ).fetchall()
        versions: list[ResultVersionRow] = []
        for row in rows:
            result_id, task_key_, attempt_id, revision, state, source, sha256, (
                committed_at
            ), remote_ids_json, delivery_state_col = tuple(row)
            versions.append(
                ResultVersionRow(
                    result_id=result_id, task_key=task_key_, attempt_id=attempt_id,
                    revision=int(revision), state=state, source=source, sha256=sha256,
                    committed_at=committed_at,
                    authoritative=(int(revision) == current_rev),
                    delivery_state=delivery_state_col,
                    corrupt_reasons=self._remote_ids_issues(remote_ids_json),
                )
            )
        return tuple(versions)

    def get_result(self, result_id: str) -> ResultDetail | None:
        """按 result_id 精确读取；缺失返回 None，绝不回落权威版本或其它 task。"""
        row = self._db.connection.execute(
            "SELECT r.result_id, r.task_key, r.attempt_id, r.revision, r.state, r.source,"
            " r.final_body, r.protocol_text, r.sha256, r.remote_message_ids_json,"
            " r.committed_at, t.current_result_revision, t.peer_id, o.state"
            " FROM results r"
            " JOIN tasks t ON t.task_key=r.task_key"
            " LEFT JOIN outbox o ON o.result_id=r.result_id AND o.peer_id=t.peer_id"
            " WHERE r.result_id=?",
            (result_id,),
        ).fetchone()
        if row is None:
            return None
        result_id_, task_key, attempt_id, revision, state, source, final_body, (
            protocol_text
        ), sha256, remote_ids_json, committed_at, current_rev, _, delivery_state_col = (
            tuple(row)
        )
        message_ids, ids_issues = _parse_remote_message_ids(remote_ids_json)
        if not _SHA256_HEX.match(sha256):
            ids_issues = ids_issues + (f"sha256 损坏：{sha256!r}",)
        return ResultDetail(
            result_id=result_id_, task_key=task_key, attempt_id=attempt_id,
            revision=int(revision), state=state, source=source,
            final_body=final_body, protocol_text=protocol_text, sha256=sha256,
            remote_message_ids=message_ids, committed_at=committed_at,
            authoritative=(int(revision) == int(current_rev)),
            delivery_state=delivery_state_col,
            corrupt_reasons=ids_issues,
        )

    # ---------------------------------------------------------------- internals

    def _probe_json1(self) -> None:
        """构造时验证 SQLite 提供 json_valid/json_extract；不修改任何 schema。"""
        try:
            self._db.connection.execute("SELECT json_valid('{}')").fetchone()
        except sqlite3.Error as exc:
            raise TaskQueryError(
                "当前 SQLite 缺 JSON1 扩展（json_valid/json_extract），无法安全搜索"
                " project_key；禁止为此修改 schema，需升级运行时"
            ) from exc

    def _checked_limit(self, limit: int) -> int:
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise TaskQueryError(f"limit 必须为整数，收到 {limit!r}")
        if not (1 <= limit <= _MAX_LIMIT):
            raise TaskQueryError(f"limit 必须在 1..{_MAX_LIMIT}，收到 {limit}")
        return limit

    def _checked_states(self, state_filter: tuple[str, ...] | None) -> tuple[str, ...]:
        if state_filter is None or len(state_filter) == 0:
            return ()
        seen: list[str] = []
        for value in state_filter:
            if not isinstance(value, str) or value not in _TASK_STATES:
                raise TaskQueryError(f"未知任务状态 filter：{value!r}")
            if value not in seen:
                seen.append(value)
        return tuple(seen)

    def _build_filters(
        self, states: tuple[str, ...], search_text: str | None
    ) -> tuple[str, list[object]]:
        where: list[str] = []
        params: list[object] = []
        if states:
            placeholders = ", ".join("?" for _ in states)
            where.append(f"t.state IN ({placeholders})")
            params.extend(states)
        if search_text is not None and search_text != "":
            conds = [
                "instr(t.task_id, ?) > 0",
                "CASE WHEN json_valid(t.ingress_snapshot_json)=1 THEN"
                " instr(json_extract(t.ingress_snapshot_json, ?), ?) > 0 ELSE 0 END",
                "EXISTS (SELECT 1 FROM results rr"
                " WHERE rr.task_key=t.task_key AND instr(rr.result_id, ?) > 0)",
            ]
            params += [search_text, _PROJECT_KEY_PATH, search_text, search_text]
            where.append("(" + " OR ".join(conds) + ")")
        return " AND ".join(where), params

    def _authority_result_exists(self, task_key: str, revision: int) -> bool:
        row = self._db.connection.execute(
            "SELECT COUNT(*) FROM results WHERE task_key=? AND revision=?",
            (task_key, revision),
        ).fetchone()
        return bool(row and int(row[0]) > 0)

    def _history_from_row(self, row: sqlite3.Row | tuple) -> TaskHistoryRow:
        values = tuple(row)
        task_key = values[0]
        sequence = int(values[3])
        state = values[5]
        current_rev = int(values[10])
        current_result_id = values[11]
        delivery_state = values[12]

        ingress_values, ingress_issues = _parse_ingress(values[8])
        issues = list(ingress_issues)
        if current_rev > 0 and current_result_id is None:
            issues.append("current_result_revision 指向不存在的 Result 版本")
        if not _SHA256_HEX.match(values[9]):
            issues.append(f"canonical_hash 损坏：{values[9]!r}")
        if not state:
            issues.append("state 为空")

        return TaskHistoryRow(
            task_key=task_key, peer_id=values[1], task_id=values[2],
            sequence=sequence, protocol_format=values[4], state=state,
            blocked_reason=values[6], received_at=values[7],
            current_result_revision=current_rev, current_result_id=current_result_id,
            delivery_state=delivery_state,
            project_key=ingress_values["project_key"],
            directory=ingress_values["directory"],
            corrupt_reasons=tuple(issues),
        )

    def _attempt_from_row(self, row: sqlite3.Row | tuple) -> AttemptHistoryRow:
        values = tuple(row)
        resolved, issues = _parse_resolved_session(values[9])
        return AttemptHistoryRow(
            attempt_id=values[0], task_key=values[1],
            parent_attempt_id=values[2], kind=values[3], state=values[4],
            authority_epoch=int(values[5]), remote_state=values[6],
            started_at=values[7], ended_at=values[8],
            resolved_session_id=resolved,
            corrupt_reasons=issues,
        )

    def _remote_ids_issues(self, remote_ids_json: str) -> tuple[str, ...]:
        _, issues = _parse_remote_message_ids(remote_ids_json)
        return issues


def _parse_ingress(raw: str) -> tuple[dict[str, object], tuple[str, ...]]:
    """从 tasks.ingress_snapshot_json 安全解析执行上下文；坏记录返回 None 字段+原因。"""
    issues: list[str] = []
    result: dict[str, object] = {
        "project_key": None, "directory": None, "requested_model": None,
        "frozen_session_id": None, "config_revision": None,
    }
    if not isinstance(raw, str) or raw == "":
        issues.append("ingress_snapshot_json 为空")
        return result, tuple(issues)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        issues.append("ingress_snapshot_json 无法解析为 JSON")
        return result, tuple(issues)
    if not isinstance(data, dict):
        issues.append("ingress_snapshot_json 不是 JSON 对象")
        return result, tuple(issues)

    project_key = _as_str_or_none(data.get("project_key"))
    directory = _as_str_or_none(data.get("directory"))
    requested_model = _as_str_or_none(data.get("requested_model"))
    frozen_session_id = _as_str_or_none(data.get("frozen_session_id"))
    config_revision = data.get("config_revision")
    if project_key is None and "project_key" in data:
        issues.append("project_key 非字符串")
    if directory is None and "directory" in data:
        issues.append("directory 非字符串")
    if config_revision is not None and not isinstance(config_revision, int):
        issues.append("config_revision 非整数")
        config_revision = None
    result.update(
        project_key=project_key, directory=directory,
        requested_model=requested_model, frozen_session_id=frozen_session_id,
        config_revision=config_revision,
    )
    return result, tuple(issues)


def _parse_resolved_session(raw: str) -> tuple[str | None, tuple[str, ...]]:
    """从 attempts.execution_snapshot_json 安全解析 resolved_session_id。"""
    if not isinstance(raw, str) or raw == "":
        return None, ("execution_snapshot_json 为空",)
    data, ok = _safe_load_object(raw)
    if not ok:
        return None, ("execution_snapshot_json 无法解析为 JSON",)
    value = data.get("resolved_session_id")
    if value is None:
        return None, ()
    if not isinstance(value, str):
        return None, ("resolved_session_id 非字符串",)
    return value, ()


def _parse_remote_message_ids(raw: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not isinstance(raw, str) or raw == "":
        return (), ("remote_message_ids_json 为空",)
    data, ok = _safe_load_object(raw)
    if not ok:
        return (), ("remote_message_ids_json 无法解析为 JSON",)
    if not isinstance(data, list):
        return (), ("remote_message_ids_json 不是数组",)
    ids = tuple(item for item in data if isinstance(item, str))
    if len(ids) != len(data):
        return ids, ("remote_message_ids 含非字符串元素",)
    return ids, ()


def _safe_load_object(raw: str) -> tuple[object, bool]:
    try:
        return json.loads(raw), True
    except (TypeError, ValueError):
        return None, False


def _as_str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None