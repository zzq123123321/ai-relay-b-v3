"""AI Relay B V3.0：配置 revision 持久化（T05）。

只负责配置的 SQLite 持久化，不包含 UI、网络、OpenChamber 逻辑。

权威规则（主规格 6.1）：
- revision 来自数据库 meta.active_config_revision 指针，单调递增、重启不倒退。
- 提交带 base_revision 的 CAS：两个编辑者基于同一 revision 时，后写者必须被拒绝。
- 写入必须在 Database.transaction() 内；事务中途失败整体 ROLLBACK，不产生半条 revision。
- 读回时校验 sha256 与配置校验，属于损坏则明确报错，不静默回退。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from core.domain import (
    ConfigBody,
    SettingsSnapshot,
    config_to_dict,
)
from core.settings_service import (
    SettingsDraft,
    SettingsError,
    SettingsValidationError,
    validate_config,
)
from storage.database import Database, StorageError

META_CURRENT_KEY = "active_config_revision"

CANONICAL_SEPARATORS = (",", ":")


class SettingsConflictError(SettingsError):
    """CAS 失败：声称的 base_revision 与数据库当前生效 revision 不一致，拒绝覆盖。"""

    code = "settings_conflict"

    def __init__(
        self,
        message: str,
        *,
        expected: int | None,
        actual: int | None,
    ) -> None:
        super().__init__(message)
        self.expected = expected
        self.actual = actual


def _canonical_json(config: ConfigBody) -> str:
    return json.dumps(
        config_to_dict(config),
        sort_keys=True,
        ensure_ascii=False,
        separators=CANONICAL_SEPARATORS,
    )


def _sha256_of(config: ConfigBody) -> str:
    return hashlib.sha256(_canonical_json(config).encode("utf-8")).hexdigest()


class SettingsStore:
    """数据库配置表（config_revisions + meta.active_config_revision 指针）的读写门面。"""

    def __init__(self, db: Database) -> None:
        self._db = db
        # 仅用于测试：在第 N 条受保护语句执行后注入 duplicate-key 冲突，验证全事务回滚
        self._fault_inject_after: int | None = None

    # ---------------------------------------------------------------- 读取

    def load_current(self) -> SettingsSnapshot | None:
        """读取当前生效 revision；尚未提交任何配置时返回 None。"""
        revision = self._read_current_revision()
        if revision is None:
            return None
        return self.get_revision(revision)

    def get_revision(self, revision: int) -> SettingsSnapshot | None:
        conn = self._db.connection
        row = conn.execute(
            "SELECT config_json, sha256, created_at FROM config_revisions WHERE revision=?",
            (revision,),
        ).fetchone()
        if row is None:
            return None
        config_json, stored_sha256, created_at = row
        config = self._parse_config_row(revision, config_json, stored_sha256)
        return SettingsSnapshot(revision=revision, created_at=created_at, config=config)

    def _parse_config_row(self, revision: int, config_json: str, stored_sha256: str) -> ConfigBody:
        try:
            raw: Mapping[str, Any] = json.loads(config_json)
        except (ValueError, TypeError) as exc:
            raise StorageError(
                f"config_revisions {revision} 的 config_json 不是合法 JSON：{exc}"
            ) from exc
        try:
            config = validate_config(SettingsDraft.from_mapping(raw))
        except SettingsValidationError as exc:
            raise StorageError(
                f"config_revisions {revision} 无法通过配置校验（数据损坏）：{exc}"
            ) from exc
        actual = hashlib.sha256(_canonical_json(config).encode("utf-8")).hexdigest()
        if actual != stored_sha256:
            raise StorageError(
                f"config_revisions {revision} sha256 不匹配：存储 {stored_sha256}，实际 {actual}"
            )
        return config

    # ---------------------------------------------------------------- CAS 提交

    def commit(
        self,
        *,
        base_revision: int | None,
        config: ConfigBody,
        created_at: str,
        actor: str = "user",
    ) -> SettingsSnapshot:
        """CAS 提交新配置。

        base_revision 必须等于数据库当前生效 revision（从未提交时为 None），
        否则抛 SettingsConflictError 并完整回滚，不覆盖并发写入的新配置。
        """
        canonical = _canonical_json(config)
        digest = _sha256_of(config)
        completed = 0

        with self._db.transaction():
            conn = self._db.connection
            current = self._read_current_revision(conn)
            completed += 1
            self._maybe_inject(completed)

            if current != base_revision:
                raise SettingsConflictError(
                    f"配置 CAS 冲突：期望 base_revision={base_revision!r}，"
                    f"当前生效为 {current!r}；拒绝覆盖更新的配置",
                    expected=base_revision,
                    actual=current,
                )

            new_revision = 1 if current is None else current + 1
            conn.execute(
                "INSERT INTO config_revisions"
                " (revision, config_json, sha256, created_at, actor)"
                " VALUES (?, ?, ?, ?, ?)",
                (new_revision, canonical, digest, created_at, actor),
            )
            completed += 1
            self._maybe_inject(completed)

            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (META_CURRENT_KEY, str(new_revision)),
            )
            completed += 1
            self._maybe_inject(completed)

        return SettingsSnapshot(revision=new_revision, created_at=created_at, config=config)

    # ---------------------------------------------------------------- 工具

    def _maybe_inject(self, completed: int) -> None:
        """test-only：在第 completed 条 SQL 语句完成后注入 duplicate-key 冲突。

        冲突发生在本事务内，__exit__ 走 ROLLBACK，整条事务（含已执行的语句）全部回退。
        """
        if self._fault_inject_after is not None and completed == self._fault_inject_after:
            self._db.connection.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', 'fault')"
            )

    def _read_current_revision(self, conn: Any | None = None) -> int | None:
        target = conn if conn is not None else self._db.connection
        row = target.execute(
            "SELECT value FROM meta WHERE key=?", (META_CURRENT_KEY,)
        ).fetchone()
        if row is None:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError) as exc:
            raise StorageError(
                f"meta.active_config_revision 不是合法整数：{row[0]!r}"
            ) from exc

    @property
    def fault_inject_after(self) -> int | None:
        """test-only：故障注入位点（受保护语句序号）。生产代码不要设置。"""
        return self._fault_inject_after

    @fault_inject_after.setter
    def fault_inject_after(self, statement_index: int | None) -> None:
        self._fault_inject_after = statement_index