"""AI Relay B V3.0：项目执行权 ProjectLease Store（T09 / T11R）。

职责（主规格 10.2 / T09 卡）：
- 持久化 project_leases：同一 project_key 同时只能存在一个有资格产生副作用的所有者；
- 读取 owner、验证 owner、检测冲突；所有者属于 Task/Attempt 执行资格，不属于某个会话；
- owner 不因普通网络等待或观察暂缺自动释放；隔离在 ACQUIRE → QUARANTINED/人工流程前持续。
- T11R：只为“正常安全完成”（owner task/attempt/epoch 精确匹配且 state=ACTIVE）提供
  conn-scope 精确 DELETE。project_leases 是“当前占用权表”而非 lease 历史表：删除精确
  owner row 即表示项目不再被占用；历史审计由 task/attempt/result/event 提供，因此
  不需要 RELEASED 状态，也不做 schema 迁移。QUARANTINED/ROTATING 一律禁止释放。

并发边界：本模块提供 conn 作用域原语（read_owner_in / acquire_in / release_active_in），
必须由调用方在同一个 Database.transaction()（BEGIN IMMEDIATE）内使用，与 TaskStore 的
attempt 创建或 ResultCommit 的结果事务组成同一事务，禁止“先提交再单独释放 owner”。

本模块不包含：OpenChamber 会话管理、网络检测、自动续接、轮换计数。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum

from storage.database import Database, StorageError

_LEASE_COLUMNS = (
    "project_key", "owner_task_key", "owner_attempt_id", "authority_epoch", "state",
    "related_sessions_json", "last_verified_at", "reason",
)

_OCCUPYING_STATES = ("ACTIVE", "QUARANTINED", "ROTATING")

_RELEASABLE_STATE = "ACTIVE"


class LeaseReleaseOutcome(str, Enum):
    """release_active_in 的结构化结论（稳定值，禁止按字符串判断）。

    RELEASED        精确 ACTIVE owner row 已删除，项目不再被占用。
    NOT_FOUND       没有 lease 行：项目本就无占用（或已被其它路径释放），不视为释放成功。
    OWNER_MISMATCH  lease 存在但 owner task/attempt/epoch 与调用方权威不一致：绝不删除，
                    防止旧 worker/代际交错误删新 owner。
    NOT_RELEASABLE  owner 精确匹配但 state=QUARANTINED/ROTATING：远端可能仍有副作用，
                    只允许未来的只读核验或人工风险确认解除，正常完成路径禁止释放。
    """

    RELEASED = "released"
    NOT_FOUND = "not_found"
    OWNER_MISMATCH = "owner_mismatch"
    NOT_RELEASABLE = "not_releasable"


@dataclass(frozen=True, slots=True)
class LeaseReleaseResult:
    """release_active_in 的返回：outcome + detail（detail 仅用于解释，不做判断依据）。"""

    outcome: LeaseReleaseOutcome
    detail: str = ""


class LeaseStoreError(StorageError):
    """LeaseStore 内部失败或期望的外部并发冲突。"""

    code = "lease_store_error"


@dataclass(frozen=True, slots=True)
class ProjectLease:
    """project_leases 表中的一行（只读投影）。occupying() 判断是否占用项目执行权。"""

    project_key: str
    owner_task_key: str
    owner_attempt_id: str
    authority_epoch: int
    state: str
    related_sessions: tuple[str, ...] = ()
    last_verified_at: str | None = None
    reason: str | None = None

    def occupying(self) -> bool:
        return self.state in _OCCUPYING_STATES


class ProjectLeaseStore:
    """项目执行权存储：读取与事务内取得 owner。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ---------------------------------------------------------------- public read

    def read_owner(self, project_key: str) -> ProjectLease | None:
        """只读查询当前 owner（无事务）。"""
        return self.read_owner_in(self._db.connection, project_key)

    def read_owner_in(self, conn: sqlite3.Connection, project_key: str) -> ProjectLease | None:
        """在调用方事务内读取 owner。"""
        row = conn.execute(
            f"SELECT {', '.join(_LEASE_COLUMNS)} FROM project_leases WHERE project_key=?",
            (project_key,),
        ).fetchone()
        return _lease_from_row(row)

    # ---------------------------------------------------------------- acquire（事务内）

    def acquire_in(
        self,
        conn: sqlite3.Connection,
        *,
        project_key: str,
        owner_task_key: str,
        owner_attempt_id: str,
        authority_epoch: int,
        now: str,
        state: str = "ACTIVE",
        related_sessions: tuple[str, ...] | None = None,
        reason: str | None = None,
    ) -> None:
        """在调用方事务内取得项目执行权。

        INSERT 受 project_leases 主键与复合外键约束：同项目已存在 owner 时抛
        IntegrityError（由调用方升级为明确冲突错误并回滚整个事务），不存在部分 owner。
        """
        if state not in _OCCUPYING_STATES:
            raise LeaseStoreError(f"非法 lease 状态：{state!r}")
        conn.execute(
            "INSERT INTO project_leases (project_key, owner_task_key, owner_attempt_id,"
            " authority_epoch, state, related_sessions_json, last_verified_at, reason)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                project_key,
                owner_task_key,
                owner_attempt_id,
                authority_epoch,
                state,
                "[]" if not related_sessions else _json_list(related_sessions),
                now,
                reason,
            ),
        )

    # ---------------------------------------------------------------- release（事务内）

    def release_active_in(
        self,
        conn: sqlite3.Connection,
        *,
        project_key: str,
        owner_task_key: str,
        owner_attempt_id: str,
        authority_epoch: int,
    ) -> LeaseReleaseResult:
        """正常权威完成后的精确 CAS 释放：只删除“精确 owner 且 state=ACTIVE”的 lease 行。

        语义等价于：DELETE ... WHERE project_key=? AND owner_task_key=? AND
        owner_attempt_id=? AND authority_epoch=? AND state='ACTIVE'。绝不按 project_key
        粗暴释放。非 ACTIVE（QUARANTINED/ROTATING）即使 owner 完全一致也禁止删除。
        必须在调用方 transaction() 内使用，与结果事务保持同一原子性；任一 SQL 失败整体回滚。
        """
        existing = self.read_owner_in(conn, project_key)
        if existing is None:
            return LeaseReleaseResult(
                LeaseReleaseOutcome.NOT_FOUND,
                "lease 行不存在：项目本无占用（或已被其它路径释放），不视为释放成功",
            )
        if (
            existing.owner_task_key != owner_task_key
            or existing.owner_attempt_id != owner_attempt_id
        ):
            return LeaseReleaseResult(
                LeaseReleaseOutcome.OWNER_MISMATCH,
                f"lease 由 task={existing.owner_task_key} attempt={existing.owner_attempt_id}"
                f" 持有，与调用方 {owner_task_key}/{owner_attempt_id} 不一致：绝不删除",
            )
        if existing.authority_epoch != authority_epoch:
            return LeaseReleaseResult(
                LeaseReleaseOutcome.OWNER_MISMATCH,
                f"lease.authority_epoch={existing.authority_epoch} 与调用方"
                f" {authority_epoch} 不一致：绝不删除",
            )
        if existing.state != _RELEASABLE_STATE:
            return LeaseReleaseResult(
                LeaseReleaseOutcome.NOT_RELEASABLE,
                f"lease state={existing.state!r} 不属于正常完成安全释放范围"
                "（QUARANTINED/ROTATING 仅由只读核验或人工风险确认解除）",
            )
        deleted = conn.execute(
            "DELETE FROM project_leases"
            " WHERE project_key=? AND owner_task_key=? AND owner_attempt_id=?"
            "   AND authority_epoch=? AND state='ACTIVE'",
            (project_key, owner_task_key, owner_attempt_id, authority_epoch),
        ).rowcount
        if deleted == 1:
            return LeaseReleaseResult(
                LeaseReleaseOutcome.RELEASED,
                "精确 ACTIVE lease row 已删除：项目不再被占用（历史仍由 task/attempt/result/event 记录）",
            )
        return LeaseReleaseResult(
            LeaseReleaseOutcome.NOT_FOUND,
            "CAS DELETE 影响 0 行：lease 已在事务内被释放或缺失",
        )


def _lease_from_row(row: sqlite3.Row | tuple | None) -> ProjectLease | None:
    if row is None:
        return None
    values = tuple(row)
    by_name = dict(zip(_LEASE_COLUMNS, values))
    sessions = by_name["related_sessions_json"]
    related = tuple(_parse_json_list(sessions)) if sessions else ()
    return ProjectLease(
        project_key=by_name["project_key"],
        owner_task_key=by_name["owner_task_key"],
        owner_attempt_id=by_name["owner_attempt_id"],
        authority_epoch=by_name["authority_epoch"],
        state=by_name["state"],
        related_sessions=related,
        last_verified_at=by_name["last_verified_at"],
        reason=by_name["reason"],
    )


def _parse_json_list(text: str) -> list[str]:
    import json

    try:
        parsed = json.loads(text)
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, str)]


def _json_list(values: tuple[str, ...]) -> str:
    import json

    return json.dumps(list(values), ensure_ascii=False)