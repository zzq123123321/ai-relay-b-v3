"""AI Relay B V3.0：SQLite 结构版本与迁移器。

职责：
- 登记当前程序支持的 schema 版本（SUPPORTED_SCHEMA_VERSION）。
- 从 storage/schema/ 读取 *_<name>.sql 迁移脚本，按编号升序应用。
- 拒绝打开版本高于支持范围的数据库（不降级、不静默跳过）。
- 脚本为纯 DDL/DML，不含 PRAGMA/BEGIN/COMMIT（连接设置与事务边界由此模块统一控制）。
- 迁移整体在一个显式事务内执行：任一步失败全部回滚。

不依赖网络、UI 与业务 Store。连接对象由调用方（storage/database.py）提供。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

SUPPORTED_SCHEMA_VERSION = 1

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"

META_SCHEMA_VERSION_KEY = "schema_version"


class SchemaError(Exception):
    """数据库结构层面的可预期错误基类（带稳定机器码 hint）。"""

    code = "schema_error"


class SchemaVersionError(SchemaError):
    """数据库版本与程序支持范围不兼容。"""

    code = "schema_version_incompatible"


class MigrationError(SchemaError):
    """迁移执行失败或迁移后版本登记不一致。"""

    code = "schema_migration_failed"


def normalize_version(value: object, *, where: str) -> int:
    try:
        version = int(value)  # type: ignore[arg-type,union-attr]
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"{where}：schema_version 不是合法整数，实际为 {value!r}") from exc
    if version < 0:
        raise SchemaError(f"{where}：schema_version 不能为负，实际为 {version!r}")
    return version


def discover_migrations() -> list[tuple[int, Path]]:
    """返回 (编号, 脚本路径) 列表，按编号升序。

    文件名约定：NNN_description.sql，NNN 为 0 填充三位数字。
    """
    migrations: list[tuple[int, Path]] = []
    if not SCHEMA_DIR.is_dir():
        raise MigrationError(f"迁移目录不存在：{SCHEMA_DIR}")
    for path in sorted(SCHEMA_DIR.glob("*.sql")):
        name = path.stem
        if "_" not in name:
            raise MigrationError(f"迁移脚本文件名不含编号分隔：{path.name}")
        number_text, _description = name.split("_", 1)
        if not number_text.isdigit():
            raise MigrationError(f"迁移脚本编号不是数字：{path.name}")
        migrations.append((int(number_text), path))
    if not migrations:
        raise MigrationError(f"迁移目录中没有迁移脚本：{SCHEMA_DIR}")
    return migrations


def has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def read_schema_version(conn: sqlite3.Connection) -> int | None:
    """读取 meta.schema_version；未初始化（无 meta 表）返回 None。"""
    if not has_table(conn, "meta"):
        return None
    row = conn.execute(
        "SELECT value FROM meta WHERE key=?",
        (META_SCHEMA_VERSION_KEY,),
    ).fetchone()
    if row is None:
        raise SchemaError("meta 表存在但缺少 schema_version 登记项")
    return normalize_version(row[0], where="迁移读取")


def check_compatible(conn: sqlite3.Connection) -> None:
    """打开已有数据库前检查版本兼容：高于支持范围必须抛错。"""
    current = read_schema_version(conn)
    if current is None:
        return
    if current > SUPPORTED_SCHEMA_VERSION:
        raise SchemaVersionError(
            f"数据库 schema 版本 {current} 高于本程序支持版本 {SUPPORTED_SCHEMA_VERSION}，"
            f"拒绝打开以避免降级或未知迁移"
        )


def iter_script_statements(script: str) -> Iterator[str]:
    """按完整 SQL 语句切分脚本，正确处理引号、注释与 CREATE TRIGGER ... END 块。

    使用 sqlite3.complete_statement 判断语句完整性，避免简单按分号切分
    把触发器内部的 `SELECT RAISE(ABORT, '...');` 误判为语句边界。
    """
    buffer: list[str] = []
    for raw_line in script.splitlines():
        line = raw_line.split("--", 1)[0].strip()
        if not line:
            continue
        buffer.append(line)
        candidate = "\n".join(buffer)
        if sqlite3.complete_statement(candidate):
            yield candidate
            buffer.clear()
    if buffer:
        raise MigrationError("迁移脚本包含未以分号闭合的语句片段")


def _apply_script_in_transaction(conn: sqlite3.Connection, path: Path) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in iter_script_statements(path.read_text(encoding="utf-8")):
            conn.execute(statement)
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def migrate(conn: sqlite3.Connection, *, target: int = SUPPORTED_SCHEMA_VERSION) -> int:
    """把数据库迁移到 target 版本，返回迁移后的版本。

    已是最新 → 不执行任何脚本；未初始化 → 从 0 全量应用；
    中间版本 → 仅应用编号大于当前版本且不超过 target 的脚本。
    """
    current = read_schema_version(conn)
    if current is None:
        current = 0
    if current > target:
        raise SchemaVersionError(
            f"迁移目标 {target} 低于当前版本 {current}，禁止降级"
        )

    applied = current
    for version, path in discover_migrations():
        if version <= applied:
            continue
        if version > target:
            break
        _apply_script_in_transaction(conn, path)
        registered = read_schema_version(conn)
        if registered != version:
            raise MigrationError(
                f"迁移脚本 {path.name} 执行后应登记 schema_version={version}，"
                f"实际登记 {registered}"
            )
        applied = version

    if applied != target:
        raise MigrationError(
            f"需要迁移到 {target}，但脚本应用后版本为 {applied}"
        )
    return applied