"""AI Relay B V3.0：项目执行权 ProjectLease Store（T09）。

职责（主规格 10.2 / T09 卡）：
- 持久化 project_leases：同一 project_key 同时只能存在一个有资格产生副作用的所有者；
- 读取 owner、验证 owner、检测冲突；所有者属于 Task/Attempt 执行资格，不属于某个会话；
- owner 不因普通网络等待或观察暂缺自动释放；隔离在 ACQUIRE → QUARANTINED/人工流程前持续。

并发边界：本模块提供 conn 作用域原语（read_owner_in / acquire_in），必须由调用方在
同一个 Database.transaction()（BEGIN IMMEDIATE）内使用，与 TaskStore 的 attempt 创建
组成同一事务，禁止“先提交 Attempt 再单独申请 owner”。

本模块不包含：OpenChamber 会话管理、网络检测、自动续接、轮换计数。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from storage.database import Database, StorageError

_LEASE_COLUMNS = (
    "project_key", "owner_task_key", "owner_attempt_id", "authority_epoch", "state",
    "related_sessions_json", "last_verified_at", "reason",
)

_OCCUPYING_STATES = ("ACTIVE", "QUARANTINED", "ROTATING")


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