"""AI Relay B V3.0：SQLite 连接与事务基础（T04）。

关键约定（主规格 4.1）：
- Python 3.12 显式事务模式：sqlite3.connect(autocommit=True)，事务用 SQL
  BEGIN IMMEDIATE / COMMIT / ROLLBACK 控制，connection.commit() 不做事务提交。
- PRAGMA foreign_keys=ON、journal_mode=WAL、synchronous=FULL、busy_timeout：
  设置后必须核实实际结果，不静默假定成功（WAL 返回 'wal'、foreign_keys 返回 1）。
- 单写者合同：事务只能由创建连接的线程（owner）发起；其它线程写必须抛明确错误。
  本轮不启动后台 writer 线程，只建立可执行与可测试的合同基础。
- 网络请求、UI、用户确认不得出现在事务内；本模块不引入任何网络依赖。
- close 之后任何访问都抛 DatabaseClosedError。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from types import TracebackType
from typing import Optional

from storage.schema import (
    SUPPORTED_SCHEMA_VERSION,
    check_compatible,
    migrate,
    read_schema_version,
)


class StorageError(Exception):
    """数据库层可预期错误基类。"""

    code = "storage_error"


class DatabaseClosedError(StorageError):
    """在连接关闭后仍尝试访问。"""

    code = "storage_closed"


class TransactionOwnershipError(StorageError):
    """从非 owner 线程发起写事务，违反单写者合同。"""

    code = "storage_writer_contract"


class TransactionNestingError(StorageError):
    """在未结束的事务上再次发起事务。"""

    code = "storage_transaction_nested"


class IncompatibleRuntimeError(StorageError):
    """运行时无法兑现要求的连接设置（如 WAL 不可用）。"""

    code = "storage_pragma_not_verified"


class Transaction:
    """显式事务上下文管理器：`with db.transaction():` 成功即 COMMIT，异常即 ROLLBACK。"""

    def __init__(self, db: "Database") -> None:
        self._db = db

    def __enter__(self) -> "Transaction":
        self._db._begin_transaction()
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> bool:
        self._db._end_transaction(rollback=exc_type is not None)
        return False


class Database:
    """单个 SQLite 权威存储的打开连接。

    同一连接：默认意图为单写者。用 db.transaction() 发起显式事务，
    只有 owner（打开连接的线程）允许，其它线程写由单写者合同拒绝。
    """

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 3000) -> None:
        self._path = Path(path)
        self._busy_timeout_ms = int(busy_timeout_ms)
        if self._busy_timeout_ms < 0:
            raise StorageError("busy_timeout_ms 不能为负")
        self._conn: Optional[sqlite3.Connection] = None
        self._owner_tid: Optional[int] = None
        self._in_transaction = False

    # ------------------------------------------------------------------ lifecycle

    @property
    def path(self) -> Path:
        return self._path

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    @property
    def in_transaction(self) -> bool:
        self._check_open()
        return self._in_transaction

    @property
    def connection(self) -> sqlite3.Connection:
        """原始连接（仅 owner 线程可访问），用于服务端 SQL、Store 组合、测试注入。"""
        self._check_open()
        self._check_writer()
        conn = self._conn
        assert conn is not None
        return conn

    @property
    def journal_mode(self) -> str:
        self._check_open()
        conn = self._conn
        assert conn is not None
        row = conn.execute("PRAGMA journal_mode").fetchone()
        return row[0].lower() if row else "unknown"

    @property
    def foreign_keys_enabled(self) -> bool:
        self._check_open()
        conn = self._conn
        assert conn is not None
        row = conn.execute("PRAGMA foreign_keys").fetchone()
        return bool(row and row[0] == 1)

    def open(self) -> None:
        """打开/创建数据库并应用连接设置与迁移。重复调用是幂等（已打开直接返回）。"""
        if self._conn is not None:
            return
        self._in_transaction = False
        conn = sqlite3.connect(
            str(self._path),
            timeout=self._busy_timeout_ms / 1000.0,
            autocommit=True,
        )
        try:
            self._apply_connection_settings(conn)
            check_compatible(conn)
            current = read_schema_version(conn)
            if current is None or current < SUPPORTED_SCHEMA_VERSION:
                migrate(conn)
            self._conn = conn
            self._owner_tid = threading.get_ident()
        except BaseException:
            conn.close()
            raise

    def close(self) -> None:
        """关闭连接。若有未结束事务则先回滚，保证不留半提交状态。"""
        conn = self._conn
        if conn is None:
            return
        if self._in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            self._in_transaction = False
        conn.close()
        self._conn = None
        self._owner_tid = None

    def _apply_connection_settings(self, conn: sqlite3.Connection) -> None:
        conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        fk_row = conn.execute("PRAGMA foreign_keys").fetchone()
        if not fk_row or fk_row[0] != 1:
            raise IncompatibleRuntimeError("PRAGMA foreign_keys=ON 未生效")
        wal_row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        if not wal_row or wal_row[0].lower() != "wal":
            actual = wal_row[0] if wal_row else None
            raise IncompatibleRuntimeError(
                f"PRAGMA journal_mode=WAL 未生效，实际为 {actual!r}"
            )
        conn.execute("PRAGMA synchronous = FULL")

    # ------------------------------------------------------------------ schema

    def schema_version(self) -> int:
        self._check_open()
        conn = self._conn
        assert conn is not None
        return read_schema_version(conn)

    # ------------------------------------------------------------------ transaction

    def transaction(self) -> Transaction:
        """发起显式事务。owner 线程、已打开、无嵌套才允许。

        使用 SQLite 的 `BEGIN IMMEDIATE` 立即获取写锁，尽早暴露锁竞争。
        """
        self._check_open()
        self._check_writer()
        if self._in_transaction:
            raise TransactionNestingError("当前已有未结束事务，禁止嵌套")
        return Transaction(self)

    def _begin_transaction(self) -> None:
        self._check_open()
        self._check_writer()
        if self._in_transaction:
            raise TransactionNestingError("当前已有未结束事务，禁止嵌套")
        conn = self._conn
        assert conn is not None
        conn.execute("BEGIN IMMEDIATE")
        self._in_transaction = True

    def _end_transaction(self, *, rollback: bool) -> None:
        self._check_open()
        self._check_writer()
        if not self._in_transaction:
            raise StorageError("没有进行中的事务可以结束")
        conn = self._conn
        assert conn is not None
        if rollback:
            conn.execute("ROLLBACK")
        else:
            conn.execute("COMMIT")
        self._in_transaction = False

    # ------------------------------------------------------------------ backup

    def backup_to(self, dest_path: str | Path) -> None:
        """在线备份当前数据库到 dest_path（sqlite3.Connection.backup，非文件复制）。

        WAL 模式下会备份主库与活跃 WAL，得到一致快照；任一侧失败由调用方处置，
        目标文件若已存在将被覆盖（SQLite backup 语义）。
        """
        self._check_open()
        self._check_writer()
        if self._in_transaction:
            raise StorageError("事务未结束时不能备份")
        conn = self._conn
        assert conn is not None
        dest = sqlite3.connect(str(dest_path), autocommit=True)
        try:
            conn.backup(dest)
        finally:
            dest.close()

    # ------------------------------------------------------------------ guards

    def _check_open(self) -> None:
        if self._conn is None:
            raise DatabaseClosedError(
                f"数据库未打开或已关闭：{self._path}"
            )

    def _check_writer(self) -> None:
        owner = self._owner_tid
        if owner is None or owner != threading.get_ident():
            raise TransactionOwnershipError(
                "数据库连接由其它线程创建，违反单写者合同："
                "连接必须由同一线程创建与使用"
            )