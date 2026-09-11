"""T04：数据库结构、显式事务与回滚/备份基础测试。

覆盖范围（对应任务卡 T04）：
- 首次建库、schema 版本登记、重复打开不重复迁移
- PRAGMA：foreign_keys=ON / journal_mode=WAL 实测生效
- 外键拒绝孤儿行；唯一键（含复合/部分唯一）冲突回滚
- 当前版本兼容；未来版本明确拒绝
- 显式事务：COMMIT 可见 / ROLLBACK 原子回滚；无嵌套
- S04：任务+结果+Outbox 事务在第 1..N 步后故障注入，全部回滚无残留
- L05：WAL 活跃数据库在线备份 → 一致快照，独立重开验证 schema 版本与数据
- 锁冲突：第二连接写被 busy_timeout 拒绝，释放后库完好可用
- 单写者合同：非 owner 线程发起事务必须被拒绝
- close 之后一切访问抛错
只使用 pytest tmp_path，不触碰任何生产数据文件。
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from storage.database import (
    Database,
    DatabaseClosedError,
    StorageError,
    TransactionOwnershipError,
)
from storage.schema import (
    SUPPORTED_SCHEMA_VERSION,
    SchemaVersionError,
    discover_migrations,
    migrate,
    read_schema_version,
)

EXPECTED_TABLES = [
    "meta",
    "config_revisions",
    "project_bindings",
    "tasks",
    "attempts",
    "project_leases",
    "operations",
    "interruptions",
    "recovery_runtime",
    "observations",
    "results",
    "outbox",
    "remote_claims",
    "monitor_bindings",
    "events",
    "ui_prefs",
    "exchanges",
]


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {row[0] for row in rows}


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ------------------------------------------------------------------ 基础结构


def test_first_open_creates_schema_and_meta_version(tmp_path):
    db = Database(tmp_path / "relay.sqlite3")
    db.open()
    try:
        conn = db.connection
        missing = set(EXPECTED_TABLES) - _table_names(conn)
        assert not missing, f"缺失表：{sorted(missing)}"
        assert db.schema_version() == SUPPORTED_SCHEMA_VERSION == 1
        assert db.journal_mode == "wal"
        assert db.foreign_keys_enabled is True
    finally:
        db.close()


def test_reopen_does_not_repeat_migration(tmp_path):
    path = tmp_path / "relay.sqlite3"
    db1 = Database(path)
    db1.open()
    db1.close()

    db2 = Database(path)
    db2.open()
    try:
        assert db2.schema_version() == 1
        conn = db2.connection
        rows = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchall()
        assert rows == [("1",)]
        assert _count(conn, "meta") == 2  # schema_version + next_sequence
        assert set(EXPECTED_TABLES) <= _table_names(conn)
    finally:
        db2.close()


def test_discover_migrations_ordering():
    migrations = discover_migrations()
    versions = [v for v, _path in migrations]
    assert versions == sorted(versions)
    assert versions == [1]


# ------------------------------------------------------------------ 版本兼容


def test_future_schema_version_rejected(tmp_path):
    path = tmp_path / "future.sqlite3"
    raw = sqlite3.connect(str(path), autocommit=True)
    raw.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    raw.execute("INSERT INTO meta VALUES ('schema_version','999')")
    raw.close()

    db = Database(path)
    with pytest.raises(SchemaVersionError):
        db.open()
    assert not db.is_open


def test_schema_version_read_of_raw_connection(tmp_path):
    raw = sqlite3.connect(str(tmp_path / "plain.sqlite3"), autocommit=True)
    raw.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    raw.execute("INSERT INTO meta VALUES ('schema_version','1')")
    assert read_schema_version(raw) == 1
    assert migrate(raw, target=1) == 1
    raw.close()


# ------------------------------------------------------------------ PRAGMA 实测


def test_foreign_keys_rejects_orphan_attempt(tmp_path):
    db = Database(tmp_path / "relay.sqlite3")
    db.open()
    try:
        assert db.foreign_keys_enabled is True
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            with db.transaction():
                db.connection.execute(
                    "INSERT INTO attempts"
                    " (attempt_id, task_key, kind, state, authority_epoch,"
                    "  execution_snapshot_json, started_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    ("a-orphan", "missing-task", "INITIAL", "OPEN", 0, "{}", "t"),
                )
        assert _count(db.connection, "attempts") == 0
    finally:
        db.close()


def test_wal_and_foreign_keys_verified(tmp_path):
    db = Database(tmp_path / "relay.sqlite3")
    db.open()
    assert db.journal_mode == "wal"
    assert db.foreign_keys_enabled is True
    db.close()


# ------------------------------------------------------------------ 唯一键冲突回滚


def _insert_task(
    conn: sqlite3.Connection,
    task_key: str,
    sequence: int,
    peer: str = "peer-1",
    task_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO tasks (task_key, peer_id, task_id, sequence, protocol_format,"
        " raw_message, body, canonical_hash, received_at, ingress_snapshot_json, state)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (task_key, peer, task_id or f"tid-{task_key}", sequence, "V2", f"raw-{task_key}",
         f"body-{task_key}", f"hash-{task_key}", "2026-01-01T00:00:00Z", "{}", "QUEUED"),
    )


def test_duplicate_primary_key_rolls_back_everything(tmp_path):
    db = Database(tmp_path / "relay.sqlite3")
    db.open()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction():
                _insert_task(db.connection, "dup", 1)
                _insert_task(db.connection, "dup", 2)  # 主键重复
        assert _count(db.connection, "tasks") == 0  # 第一条也被回滚
    finally:
        db.close()


def test_duplicate_peer_task_unique_rollback(tmp_path):
    db = Database(tmp_path / "relay.sqlite3")
    db.open()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction():
                _insert_task(db.connection, "k1", 1, peer="p", task_id="same-tid")
                _insert_task(db.connection, "k2", 2, peer="p", task_id="same-tid")  # UNIQUE(peer_id,task_id)
        assert _count(db.connection, "tasks") == 0
    finally:
        db.close()


def test_duplicate_operation_key_rollback(tmp_path):
    db = Database(tmp_path / "relay.sqlite3")
    db.open()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction():
                db.connection.execute(
                    "INSERT INTO operations (operation_id, operation_key, kind, endpoint,"
                    " state, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    ("op-1", "opkey", "INITIAL_SEND", "ep", "PREPARED", "t", "t"),
                )
                db.connection.execute(
                    "INSERT INTO operations (operation_id, operation_key, kind, endpoint,"
                    " state, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    ("op-2", "opkey", "INITIAL_SEND", "ep", "PREPARED", "t", "t"),
                )
        assert _count(db.connection, "operations") == 0
    finally:
        db.close()


def test_duplicate_result_revision_rollback(tmp_path):
    db = Database(tmp_path / "relay.sqlite3")
    db.open()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction():
                _insert_task(db.connection, "rt", 1)
                db.connection.execute(
                    "INSERT INTO attempts (attempt_id, task_key, kind, state,"
                    " authority_epoch, execution_snapshot_json, started_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    ("at-rt", "rt", "INITIAL", "OPEN", 0, "{}", "t"),
                )
                db.connection.execute(
                    "INSERT INTO results (result_id, task_key, attempt_id, revision, state,"
                    " source, final_body, protocol_text, sha256, committed_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ("r-1", "rt", "at-rt", 1, "COMPLETED", "AUTO_RELAY",
                     "ok", "txt", "h", "t"),
                )
                # 同一 (task_key,revision) 重复 → 冲突，连同上面全部回滚
                db.connection.execute(
                    "INSERT INTO results (result_id, task_key, attempt_id, revision, state,"
                    " source, final_body, protocol_text, sha256, committed_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ("r-2", "rt", "at-rt", 1, "COMPLETED", "AUTO_RELAY",
                     "ok", "txt", "h", "t"),
                )
        assert _count(db.connection, "results") == 0
        assert _count(db.connection, "tasks") == 0
    finally:
        db.close()


# ------------------------------------------------------------------ 显式事务语义


def test_commit_visible_only_after_transaction_exit(tmp_path):
    path = tmp_path / "relay.sqlite3"
    writer = Database(path)
    writer.open()
    reader = Database(path)
    reader.open()
    try:
        with writer.transaction():
            _insert_task(writer.connection, "vis", 1)
            # 未提交：对另一连接不可见
            assert _count(reader.connection, "tasks") == 0
        assert _count(reader.connection, "tasks") == 1
    finally:
        reader.close()
        writer.close()


def test_rollback_atomic_on_exception(tmp_path):
    db = Database(tmp_path / "relay.sqlite3")
    db.open()
    try:
        with pytest.raises(RuntimeError):
            with db.transaction():
                _insert_task(db.connection, "rb", 1)
                _insert_task(db.connection, "rb2", 2)
                raise RuntimeError("boom")
        assert _count(db.connection, "tasks") == 0
    finally:
        db.close()


def test_nested_transaction_rejected(tmp_path):
    db = Database(tmp_path / "relay.sqlite3")
    db.open()
    try:
        with pytest.raises(StorageError):
            with db.transaction():
                _insert_task(db.connection, "n", 1)
                with db.transaction():
                    _insert_task(db.connection, "n2", 2)
        # 外层因嵌套错误被回滚
        assert _count(db.connection, "tasks") == 0
    finally:
        db.close()


def test_transaction_rejected_while_backup(tmp_path):
    db = Database(tmp_path / "relay.sqlite3")
    db.open()
    try:
        with db.transaction():
            _insert_task(db.connection, "b", 1)
            with pytest.raises(StorageError):
                db.backup_to(tmp_path / "no.sqlite3")
        assert _count(db.connection, "tasks") == 1
    finally:
        db.close()


# ------------------------------------------------------------------ S04 多故障点回滚


def _result_tx_steps(task_key: str, sequence: int) -> list[tuple[str, list]]:
    attempt_id = f"att-{task_key}"
    result_id = f"res-{task_key}"
    return [
        ("INSERT INTO tasks (task_key, peer_id, task_id, sequence, protocol_format,"
         " raw_message, body, canonical_hash, received_at, ingress_snapshot_json, state)"
         " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
         [task_key, "peer-1", f"tid-{task_key}", sequence, "V2", f"raw-{task_key}",
          f"body-{task_key}", f"hash-{task_key}", "2026-01-01T00:00:00Z", "{}", "QUEUED"]),
        ("UPDATE tasks SET state='ACTIVE', active_attempt_id=? WHERE task_key=?",
         [attempt_id, task_key]),
        ("INSERT INTO attempts (attempt_id, task_key, kind, state, authority_epoch,"
         " execution_snapshot_json, started_at) VALUES (?,?,?,?,?,?,?)",
         [attempt_id, task_key, "INITIAL", "OPEN", 0, "{}", "t"]),
        ("INSERT INTO results (result_id, task_key, attempt_id, revision, state, source,"
         " final_body, protocol_text, sha256, committed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
         [result_id, task_key, attempt_id, 1, "COMPLETED", "AUTO_RELAY",
          "final", "txt", "h", "t"]),
        ("INSERT INTO outbox (delivery_id, result_id, peer_id, state, profile, offered_count)"
         " VALUES (?,?,?,?,?,?)",
         [f"d-{task_key}", result_id, "peer-1", "PENDING", "reliable_v2", 0]),
        ("UPDATE tasks SET state='COMPLETED', authority_epoch=1,"
         " current_result_revision=1, active_attempt_id=? WHERE task_key=?",
         [attempt_id, task_key]),
    ]


@pytest.mark.parametrize("fail_after", [1, 2, 3, 4, 5, 6])
def test_s04_result_tx_failure_at_every_step_rolls_back(tmp_path, fail_after: int):
    """任务终态+结果+Outbox 事务在第 1..N 步后均原子回滚，无孤儿数据。"""
    db = Database(tmp_path / f"relay_{fail_after}.sqlite3")
    db.open()
    try:
        steps = _result_tx_steps(task_key=f"task-{fail_after}", sequence=fail_after)
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            with db.transaction():
                for sql, params in steps[:fail_after]:
                    db.connection.execute(sql, params)
                # SQL 中间故障注入：重复插入已存在的 meta 主键
                db.connection.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?)",
                    ("schema_version", "2"),
                )
        for table in ("tasks", "attempts", "results", "outbox"):
            assert _count(db.connection, table) == 0, (
                f"故障点 {fail_after} 后 {table} 残留 {db.connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]}"
            )
    finally:
        db.close()


# ------------------------------------------------------------------ L05 WAL 在线备份


def test_l05_wal_active_backup_consistent_snapshot(tmp_path):
    """WAL 活跃且有未提交 checkpoint 的数据时，在线备份仍得到一致快照。"""
    path = tmp_path / "relay.sqlite3"
    db = Database(path, busy_timeout_ms=2000)
    db.open()
    with db.transaction():
        for sql, params in _result_tx_steps("bkp", 1):
            db.connection.execute(sql, params)
    assert db.journal_mode == "wal"
    assert _count(db.connection, "results") == 1

    backup_path = tmp_path / "backup.sqlite3"
    db.backup_to(backup_path)
    db.close()

    with_same_data = Database(backup_path)
    with_same_data.open()
    try:
        assert with_same_data.schema_version() == SUPPORTED_SCHEMA_VERSION
        assert _count(with_same_data.connection, "tasks") == 1
        assert _count(with_same_data.connection, "attempts") == 1
        assert _count(with_same_data.connection, "results") == 1
        assert _count(with_same_data.connection, "outbox") == 1
        row = with_same_data.connection.execute(
            "SELECT state, authority_epoch, current_result_revision FROM tasks"
        ).fetchone()
        assert row == ("COMPLETED", 1, 1)
    finally:
        with_same_data.close()


def test_backup_to_rejects_notify_tx_and_after_close(tmp_path):
    db = Database(tmp_path / "relay.sqlite3")
    db.open()
    db.backup_to(tmp_path / "empty.sqlite3")
    db.close()
    with pytest.raises(DatabaseClosedError):
        db.backup_to(tmp_path / "again.sqlite3")


# ------------------------------------------------------------------ 锁冲突


def test_lock_conflict_rejected_then_reusable(tmp_path):
    path = tmp_path / "relay.sqlite3"
    db1 = Database(path, busy_timeout_ms=2000)
    db1.open()
    db2 = Database(path, busy_timeout_ms=150)
    db2.open()
    try:
        tx1 = db1.transaction()
        tx1.__enter__()
        _insert_task(db1.connection, "lock", 1)
        with pytest.raises(sqlite3.OperationalError, match="locked|busy"):
            tx2 = db2.transaction()
            tx2.__enter__()
        tx1.__exit__(None, None, None)

        # db1 提交后，db2 可正常读写，库没有损坏
        with db2.transaction():
            _insert_task(db2.connection, "after", 2, peer="peer-2")
        assert _count(db2.connection, "tasks") == 2
    finally:
        db2.close()
        db1.close()


# ------------------------------------------------------------------ 单写者合同


def test_single_writer_contract_rejects_foreign_thread(tmp_path):
    db = Database(tmp_path / "relay.sqlite3")
    db.open()
    results: list[type[BaseException] | None] = []

    def foreign_writer() -> None:
        try:
            with db.transaction():
                db.connection.execute("SELECT 1")
            results.append(None)
        except BaseException as exc:  # noqa: BLE001 - 测试需要捕获并记录
            results.append(type(exc))

    thread = threading.Thread(target=foreign_writer)
    thread.start()
    thread.join()
    assert results == [TransactionOwnershipError]

    with db.transaction():
        _insert_task(db.connection, "owner-ok", 1)
    assert _count(db.connection, "tasks") == 1


# ------------------------------------------------------------------ close 行为


def test_all_ops_raise_after_close(tmp_path):
    db = Database(tmp_path / "relay.sqlite3")
    db.open()
    db.close()
    assert not db.is_open
    with pytest.raises(DatabaseClosedError):
        db.connection
    with pytest.raises(DatabaseClosedError):
        db.schema_version()
    with pytest.raises(DatabaseClosedError):
        with db.transaction():
            pass
    with pytest.raises(DatabaseClosedError):
        db.journal_mode
    with pytest.raises(DatabaseClosedError):
        db.foreign_keys_enabled


def test_close_rolls_back_active_transaction(tmp_path):
    db = Database(tmp_path / "relay.sqlite3")
    db.open()
    tx = db.transaction()
    tx.__enter__()
    _insert_task(db.connection, "leak", 1)
    db.close()

    re = Database(tmp_path / "relay.sqlite3")
    re.open()
    try:
        assert _count(re.connection, "tasks") == 0
    finally:
        re.close()