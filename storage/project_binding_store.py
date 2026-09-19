"""AI Relay B V3.0：项目绑定 ProjectBindingsStore（T23-02 / card 1）。

职责（主规格 10.3 binding，T23-02 卡）：
- 为 `project_bindings` 提供唯一权威持久化：`read` / `read_in` /
  `insert_initial_in` / `cas_update_in`。binding 存在意味着“该 display_path 权威地
  指向某个 endpoint 的某个 session”；它是 OpenChamber 会话解析与所有权判定的数据源。
- 本卡**只**做持久化与 CAS 原语，不接入 OpenChamber、不接入 scheduler、
  不消费 created_session_id。网络、发送、业务轮换策略一律不在本模块出现。
- 并发边界：本模块提供 conn 作用域原语（read_in / insert_initial_in /
  cas_update_in），必须由调用方在同一个 Database.transaction()（BEGIN IMMEDIATE）
  内使用，与 TaskStore / OperationStore / LeaseStore 的组合事务同一原子性。

CAS 规则（T23-02）：
- update 以 `binding_revision` 为 CAS 键：`WHERE project_key=? AND
  binding_revision=expected_binding_revision`，成功时
  `binding_revision=expected_binding_revision + 1`。这一比较与自增必须由 SQL
  完成，禁止“先 read 再在事务外判断再普通 UPDATE”的路径。
- identity 字段（project_key / display_path / endpoint）不参与 CAS，也绝不修改。
- 幂等收敛：若所有可变字段与当前行完全一致，视为 NO_CHANGE，不写库、
  不增加 binding_revision。
- 权威错误用稳定 Outcome 表达（UPDATED / NO_CHANGE / NOT_FOUND /
  REVISION_MISMATCH）；非法输入或计数器跳变抛 ProjectBindingStoreError，
  不靠 detail 字符串做判断依据。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum

from storage.database import Database, StorageError

_BINDING_COLUMNS = (
    "project_key", "display_path", "endpoint", "session_id", "binding_revision",
    "rotation_count", "rotation_sequence", "rotation_base_title",
    "candidate_operation_id", "config_revision",
)


class ProjectBindingStoreError(StorageError):
    """ProjectBindingsStore 系统级失败或非法输入/CAS 跳变。"""

    code = "project_binding_store_error"


class UpdateOutcome(str, Enum):
    """cas_update_in 的稳定结果（禁止按字符串判断）。"""

    UPDATED = "updated"          # CAS 成功，binding_revision 精确 +1
    NO_CHANGE = "no_change"      # 所有可变字段与当前完全一致：不写库、不 +1
    NOT_FOUND = "not_found"      # 无该 project_key 的 binding 行
    REVISION_MISMATCH = "revision_mismatch"  # 当前 binding_revision != 期望，拒绝


@dataclass(frozen=True, slots=True)
class UpdateResult:
    """cas_update_in 的结构化返回：outcome + 新旧 binding_revision（仅解释用）。"""

    outcome: UpdateOutcome
    current_binding_revision: int | None = None
    new_binding_revision: int | None = None


@dataclass(frozen=True, slots=True)
class ProjectBinding:
    """project_bindings 表的一行只读投影（不可变）。"""

    project_key: str
    display_path: str
    endpoint: str
    session_id: str | None
    binding_revision: int
    rotation_count: int
    rotation_sequence: int
    rotation_base_title: str
    candidate_operation_id: str | None
    config_revision: int | None


def _project_binding_from_row(row: sqlite3.Row | tuple) -> ProjectBinding:
    values = tuple(row)
    by_name = dict(zip(_BINDING_COLUMNS, values))
    return ProjectBinding(
        project_key=by_name["project_key"],
        display_path=by_name["display_path"],
        endpoint=by_name["endpoint"],
        session_id=by_name["session_id"],
        binding_revision=by_name["binding_revision"],
        rotation_count=by_name["rotation_count"],
        rotation_sequence=by_name["rotation_sequence"],
        rotation_base_title=by_name["rotation_base_title"],
        candidate_operation_id=by_name["candidate_operation_id"],
        config_revision=by_name["config_revision"],
    )


class ProjectBindingStore:
    """项目绑定（binding revision CAS）的权威持久化 Store。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ---------------------------------------------------------------- 读取

    def read(self, project_key: str) -> ProjectBinding | None:
        """只读查询当前 binding（非事务，走 Store 自身 writer 连接）。"""
        return self.read_in(self._db.connection, project_key)

    def read_in(
        self, conn: sqlite3.Connection, project_key: str
    ) -> ProjectBinding | None:
        """在调用方事务内读取 binding 行（无事务时也安全，纯 SELECT）。"""
        row = conn.execute(
            f"SELECT {', '.join(_BINDING_COLUMNS)}"
            " FROM project_bindings WHERE project_key=?",
            (project_key,),
        ).fetchone()
        return _project_binding_from_row(row) if row is not None else None

    # ---------------------------------------------------------------- 初始插入

    def insert_initial_in(
        self,
        conn: sqlite3.Connection,
        *,
        project_key: str,
        display_path: str,
        endpoint: str,
        session_id: str | None = None,
        rotation_base_title: str = "",
        config_revision: int | None = None,
    ) -> ProjectBinding:
        """事务内新建初始 binding 行（fix revision 0 / counters 0 / candidate NULL）。

        初始行固定：binding_revision=0、rotation_count=0、rotation_sequence=0、
        candidate_operation_id=NULL。调用方不得伪造初始 revision/counter。
        identity 字段（project_key/display_path/endpoint）必须 nonblank；
        session_id 若非 None 必须 nonblank；config_revision 若非 None 必须是正整数。
        同 project_key 已存在时不覆盖，抛稳定冲突错误（保留原行）。
        """
        project_key = project_key.strip()
        display_path = display_path.strip()
        endpoint = endpoint.strip()
        if not project_key:
            raise ProjectBindingStoreError("project_key 不能为空")
        if not display_path:
            raise ProjectBindingStoreError("display_path 不能为空")
        if not endpoint:
            raise ProjectBindingStoreError("endpoint 不能为空")
        if session_id is not None and not session_id.strip():
            raise ProjectBindingStoreError("session_id 不能为空白字符串")
        if config_revision is not None:
            if not isinstance(config_revision, int) or isinstance(config_revision, bool):
                raise ProjectBindingStoreError(
                    f"config_revision 必须是正整数，实际为 {config_revision!r}"
                )
            if config_revision <= 0:
                raise ProjectBindingStoreError(
                    f"config_revision 必须是正整数，实际为 {config_revision}"
                )

        try:
            conn.execute(
                "INSERT INTO project_bindings (project_key, display_path, endpoint,"
                " session_id, binding_revision, rotation_count, rotation_sequence,"
                " rotation_base_title, candidate_operation_id, config_revision)"
                " VALUES (?,?,?,?,0,0,0,?,NULL,?)",
                (
                    project_key, display_path, endpoint,
                    session_id if session_id is not None else None,
                    rotation_base_title,
                    config_revision,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ProjectBindingStoreError(
                f"project_bindings 已存在 project_key={project_key!r}："
                "初始插入不得覆盖既有绑定"
            ) from exc
        row = conn.execute(
            f"SELECT {', '.join(_BINDING_COLUMNS)}"
            " FROM project_bindings WHERE project_key=?",
            (project_key,),
        ).fetchone()
        if row is None:
            raise ProjectBindingStoreError(
                f"初始插入后读回失败（数据损坏）：project_key={project_key!r}"
            )
        return _project_binding_from_row(row)

    # ---------------------------------------------------------------- CAS 更新

    def cas_update_in(
        self,
        conn: sqlite3.Connection,
        *,
        project_key: str,
        expected_binding_revision: int,
        session_id: str | None,
        candidate_operation_id: str | None,
        rotation_count: int,
        rotation_sequence: int,
        rotation_base_title: str,
        config_revision: int | None,
    ) -> UpdateResult:
        """事务内 binding_revision CAS 更新可变字段。

        CAS 键：`WHERE project_key=? AND binding_revision=expected`，成功即
        `binding_revision=expected+1`。允许更新的字段只有：session_id、
        candidate_operation_id、rotation_count、rotation_sequence、
        rotation_base_title、config_revision。identity 字段（project_key /
        display_path / endpoint）绝不修改。
        rotation_count / rotation_sequence 只允许 {当前值, 当前值+1}，禁止减少、
        跳变 >1、负值。全部可变字段与当前一致 → NO_CHANGE（不写库、不 +1）。
        """
        current = self.read_in(conn, project_key)
        if current is None:
            return UpdateResult(outcome=UpdateOutcome.NOT_FOUND)
        if not isinstance(expected_binding_revision, int) or expected_binding_revision < 0:
            raise ProjectBindingStoreError(
                f"expected_binding_revision 必须是 >=0 整数，实际为"
                f" {expected_binding_revision!r}"
            )
        if current.binding_revision != expected_binding_revision:
            return UpdateResult(
                outcome=UpdateOutcome.REVISION_MISMATCH,
                current_binding_revision=current.binding_revision,
            )

        # —— 计数器单调约束：只允许不变或精确 +1；禁止减少/跳变/负值
        for name, new, current_val in (
            ("rotation_count", rotation_count, current.rotation_count),
            ("rotation_sequence", rotation_sequence, current.rotation_sequence),
        ):
            if not isinstance(new, int) or isinstance(new, bool) or new < 0:
                raise ProjectBindingStoreError(
                    f"{name} 必须是 >=0 整数，实际为 {new!r}"
                )
            if new < current_val:
                raise ProjectBindingStoreError(
                    f"{name} 从 {current_val} 降至 {new}：计数禁止减少"
                )
            if new > current_val + 1:
                raise ProjectBindingStoreError(
                    f"{name} 跳变为 {new}，当前为 {current_val}：只允许不变或 +1"
                )

        # —— 非法可变字段：空白 / 非法 config_revision
        if session_id is not None and not session_id.strip():
            raise ProjectBindingStoreError("session_id 不能为空白字符串")
        if candidate_operation_id is not None and not candidate_operation_id.strip():
            raise ProjectBindingStoreError("candidate_operation_id 不能为空白字符串")
        if config_revision is not None:
            if not isinstance(config_revision, int) or isinstance(config_revision, bool):
                raise ProjectBindingStoreError(
                    f"config_revision 必须是正整数，实际为 {config_revision!r}"
                )
            if config_revision <= 0:
                raise ProjectBindingStoreError(
                    f"config_revision 必须是正整数，实际为 {config_revision}"
                )

        session_id_norm = session_id if session_id is not None else None
        candidate_norm = candidate_operation_id if candidate_operation_id is not None else None

        # —— no-op 收敛：全部可变字段与当前一致就完全不动
        if (
            session_id_norm == current.session_id
            and candidate_norm == current.candidate_operation_id
            and rotation_count == current.rotation_count
            and rotation_sequence == current.rotation_sequence
            and (rotation_base_title or "") == current.rotation_base_title
            and config_revision == current.config_revision
        ):
            return UpdateResult(
                outcome=UpdateOutcome.NO_CHANGE,
                current_binding_revision=current.binding_revision,
                new_binding_revision=current.binding_revision,
            )

        new_revision = expected_binding_revision + 1
        cursor = conn.execute(
            "UPDATE project_bindings SET session_id=?, candidate_operation_id=?,"
            " rotation_count=?, rotation_sequence=?, rotation_base_title=?,"
            " config_revision=?, binding_revision=?"
            " WHERE project_key=? AND binding_revision=?",
            (
                session_id_norm, candidate_norm, rotation_count, rotation_sequence,
                rotation_base_title or "", config_revision, new_revision,
                project_key, expected_binding_revision,
            ),
        )
        if cursor.rowcount != 1:
            # 并发 CAS 失去（rowcount=0）：重读判定当前 revision，稳定返回 mismatch
            latest = self.read_in(conn, project_key)
            latest_rev = latest.binding_revision if latest is not None else None
            return UpdateResult(
                outcome=UpdateOutcome.REVISION_MISMATCH,
                current_binding_revision=latest_rev,
            )
        return UpdateResult(
            outcome=UpdateOutcome.UPDATED,
            current_binding_revision=expected_binding_revision,
            new_binding_revision=new_revision,
        )
