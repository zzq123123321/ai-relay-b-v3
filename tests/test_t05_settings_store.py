"""T05：SettingsStore——配置 revision 持久化、CAS 与失败回滚（存储层）。

覆盖：首次提交、单调 revision、重启后指针与新增、CAS 冲突拒绝、
第 1/2/3 条 SQL 后故障注入完整回滚。均使用 pytest tmp_path。
"""

from __future__ import annotations

import sqlite3

import pytest

from core.domain import SettingsDraft
from core.settings_service import validate_config
from storage.database import Database
from storage.settings_store import SettingsConflictError, SettingsStore


def _config(*, executor: str = "OPENCHAMBER", session: str = "") -> "object":
    draft = SettingsDraft.defaults()
    draft["default_target"] = executor
    draft["openchamber"]["session_id"] = session
    return validate_config(draft)


def _commit(
    store: SettingsStore,
    *,
    base: int | None = None,
    executor: str = "OPENCHAMBER",
    session: str = "",
    timestamp: str = "2026-09-12T00:00:00Z",
):
    return store.commit(
        base_revision=base,
        config=_config(executor=executor, session=session),
        created_at=timestamp,
        actor="tester",
    )


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "relay.sqlite3")
    database.open()
    yield database
    database.close()


@pytest.fixture
def store(db):
    return SettingsStore(db)


def _revision_rows(db: Database) -> int:
    return db.connection.execute("SELECT COUNT(*) FROM config_revisions").fetchone()[0]


def test_commit_first_revision_and_roundtrip(store):
    snapshot = _commit(store, executor="OPENCHAMBER")
    assert snapshot.revision == 1
    loaded = store.load_current()
    assert loaded is not None
    assert loaded.revision == 1
    assert loaded.config.default_target.value == "OPENCHAMBER"
    assert loaded.created_at == "2026-09-12T00:00:00Z"


def test_commit_monotonic_revisions(store):
    r1 = _commit(store, base=None, executor="OPENCHAMBER")
    r2 = _commit(store, base=1, executor="REASONIX")
    r3 = _commit(store, base=2, executor="OPENCHAMBER")
    assert (r1.revision, r2.revision, r3.revision) == (1, 2, 3)
    assert store.load_current().revision == 3
    assert _revision_rows(store._db) == 3


def test_load_current_none_before_any_commit(store):
    assert store.load_current() is None
    assert store.get_revision(1) is None


def test_get_revision_existing_and_missing(store):
    _commit(store, executor="REASONIX")
    got = store.get_revision(1)
    assert got is not None and got.config.default_target.value == "REASONIX"
    assert store.get_revision(2) is None


def test_cas_conflict_rejected_no_overwrite(store):
    first = _commit(store, base=None, executor="OPENCHAMBER")
    assert first.revision == 1
    # 第二个编辑者仍声称基于 revision 1，但当前已是 1 → 提交 rev2 成功
    second = _commit(store, base=1, executor="REASONIX")
    assert second.revision == 2
    # 第三方基于旧的 base=1 提交 → 必须被拒绝，不得覆盖 rev2
    with pytest.raises(SettingsConflictError) as err:
        _commit(store, base=1, executor="OPENCHAMBER")
    assert err.value.code == "settings_conflict"
    assert err.value.expected == 1 and err.value.actual == 2
    current = store.load_current()
    assert current.revision == 2
    assert current.config.default_target.value == "REASONIX"
    assert _revision_rows(store._db) == 2


def test_cas_first_commit_requires_none(store):
    with pytest.raises(SettingsConflictError):
        _commit(store, base=0)
    assert store.load_current() is None
    assert _revision_rows(store._db) == 0


def test_restart_reload_keeps_revision_and_continues(tmp_path):
    db1 = Database(tmp_path / "relay.sqlite3")
    db1.open()
    st1 = SettingsStore(db1)
    _commit(st1, base=None, executor="OPENCHAMBER")
    _commit(st1, base=1, executor="REASONIX")
    db1.close()

    db2 = Database(tmp_path / "relay.sqlite3")
    db2.open()
    st2 = SettingsStore(db2)
    loaded = st2.load_current()
    assert loaded is not None and loaded.revision == 2
    assert loaded.config.default_target.value == "REASONIX"
    # 重启后不回落，继续递增
    r3 = _commit(st2, base=2, executor="OPENCHAMBER")
    assert r3.revision == 3
    assert st2.load_current().revision == 3
    db2.close()


@pytest.mark.parametrize("completed", [1, 2, 3])
def test_commit_failure_at_every_statement_rolls_back(db, store, completed: int):
    """第 completed 条 SQL 后注入冲突：整条事务回滚，无半条 revision、指针不变。"""
    _commit(store, base=None, executor="OPENCHAMBER")
    _commit(store, base=1, executor="REASONIX")
    for rev in range(2, 7):  # base=2→rev3 ... base=6→rev7
        _commit(store, base=rev, executor="OPENCHAMBER")
    assert store.load_current().revision == 7

    store.fault_inject_after = completed
    with pytest.raises(sqlite3.IntegrityError):
        _commit(store, base=7, executor="REASONIX")
    store.fault_inject_after = None

    current = store.load_current()
    assert current.revision == 7
    assert current.config.default_target.value == "OPENCHAMBER"
    assert _revision_rows(db) == 7  # 没有第 8 行
    pointer = db.connection.execute(
        "SELECT value FROM meta WHERE key='active_config_revision'"
    ).fetchone()
    assert pointer == ("7",)


def test_commit_error_is_db_level(store):
    store.fault_inject_after = 2
    with pytest.raises(sqlite3.IntegrityError):
        _commit(store, base=None, executor="OPENCHAMBER")
    store.fault_inject_after = None