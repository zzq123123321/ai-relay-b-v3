"""T23-02：ProjectBindingStore 单元测试（主规格 10.3 binding / T23-02 卡）。

覆盖：
- read / read_in：空表、读回、事务内未提交读；
- insert_initial_in：初始行固定 revision=0 / counters=0 / candidate=NULL，
  调用方无法伪造；identity 空白校验；config_revision 正整数校验；
  duplicate 不覆盖原行（稳定冲突错误）；
- cas_update_in：CAS 成功精确 +1；REVISION_MISMATCH；NOT_FOUND；
  NO_CHANGE 幂等收敛（不写库、不 +1）；
- rotation 计数器单调约束：same/+1 允许；decrease / 跳变>1 / 负值拒绝；
- 可变字段 set/replace/clear：session_id / candidate_operation_id /
  rotation_base_title / config_revision；identity 字段永不修改；
- 失败 CAS 后原行逐字节不变；caller 事务回滚完全生效；
- 并发 stale CAS 只有先到者胜。
"""

from __future__ import annotations

import sqlite3

import pytest

from storage.database import Database
from storage.project_binding_store import (
    ProjectBindingStore,
    ProjectBindingStoreError,
    UpdateOutcome,
)

_PK = "p-t23"
_DP = r"D:\AIwork\proj"
_EP = "http://127.0.0.1:57123"


def _seed_config_revisions(db, *revisions):
    if not revisions:
        return
    with db.transaction():
        db.connection.executemany(
            "INSERT INTO config_revisions (revision, config_json, sha256,"
            " created_at, actor) VALUES (?,?,?,?,?)",
            [(r, "{}", "sha", "2026-10-01T09:00:00+00:00", "test")
             for r in revisions],
        )


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t23.sqlite")
    db.open()
    store = ProjectBindingStore(db)
    yield db, store
    db.close()


def _insert(db, store, *, project_key=_PK, display_path=_DP, endpoint=_EP,
            session_id=None, title="", config_revision=None):
    with db.transaction():
        return store.insert_initial_in(
            db.connection, project_key=project_key, display_path=display_path,
            endpoint=endpoint, session_id=session_id,
            rotation_base_title=title, config_revision=config_revision,
        )


def _cas(db, store, **kw):
    defaults = dict(
        project_key=_PK, expected_binding_revision=0,
        session_id="sess-0", candidate_operation_id=None,
        rotation_count=0, rotation_sequence=0,
        rotation_base_title="", config_revision=None,
    )
    defaults.update(kw)
    with db.transaction():
        return store.cas_update_in(db.connection, **defaults)


class TestRead:
    def test_read_empty_returns_none(self, env):
        _db, store = env
        assert store.read(_PK) is None

    def test_read_in_empty_returns_none(self, env):
        db, store = env
        with db.transaction():
            assert store.read_in(db.connection, "no-such-project") is None

    def test_read_roundtrip_all_fields(self, env):
        db, store = env
        _seed_config_revisions(db, 7)
        _insert(db, store, session_id="sess-init", title="base-A",
                config_revision=7)
        row = store.read(_PK)
        assert row is not None
        assert row.project_key == _PK
        assert row.display_path == _DP
        assert row.endpoint == _EP
        assert row.session_id == "sess-init"
        assert row.binding_revision == 0
        assert row.rotation_count == 0
        assert row.rotation_sequence == 0
        assert row.rotation_base_title == "base-A"
        assert row.candidate_operation_id is None
        assert row.config_revision == 7

    def test_read_in_sees_uncommitted_row_inside_txn(self, env):
        db, store = env
        with db.transaction():
            store.insert_initial_in(
                db.connection, project_key=_PK, display_path=_DP,
                endpoint=_EP)
            row = store.read_in(db.connection, _PK)
            assert row is not None
            assert row.binding_revision == 0


class TestInsertInitial:
    def test_initial_row_fixed_defaults(self, env):
        db, store = env
        row = _insert(db, store)
        assert row.binding_revision == 0
        assert row.rotation_count == 0
        assert row.rotation_sequence == 0
        assert row.candidate_operation_id is None
        assert row.session_id is None
        assert row.rotation_base_title == ""
        assert row.config_revision is None

    def test_insert_duplicate_raises_stable_error(self, env):
        db, store = env
        first = _insert(db, store, session_id="sess-a")
        with pytest.raises(ProjectBindingStoreError):
            _insert(db, store, session_id="sess-b")
        now = store.read(_PK)
        assert now is not None
        assert now.session_id == first.session_id
        assert now.binding_revision == first.binding_revision

    def test_duplicate_keeps_every_original_field(self, env):
        db, store = env
        _seed_config_revisions(db, 5, 9)
        _insert(db, store, session_id="sess-a", title="orig",
                config_revision=5)
        with pytest.raises(ProjectBindingStoreError):
            _insert(db, store, display_path=r"C:\other", endpoint="http://x",
                    session_id="sess-b", title="new", config_revision=9)
        row = store.read(_PK)
        assert row.display_path == _DP
        assert row.endpoint == _EP
        assert row.session_id == "sess-a"
        assert row.rotation_base_title == "orig"
        assert row.config_revision == 5

    @pytest.mark.parametrize("project_key", ["", "   "])
    def test_blank_project_key_rejected(self, env, project_key):
        db, store = env
        with pytest.raises(ProjectBindingStoreError):
            store.insert_initial_in(
                db.connection, project_key=project_key, display_path=_DP,
                endpoint=_EP)

    @pytest.mark.parametrize("display_path", ["", "  "])
    def test_blank_display_path_rejected(self, env, display_path):
        db, store = env
        with pytest.raises(ProjectBindingStoreError):
            store.insert_initial_in(
                db.connection, project_key=_PK, display_path=display_path,
                endpoint=_EP)

    @pytest.mark.parametrize("endpoint", ["", "\t"])
    def test_blank_endpoint_rejected(self, env, endpoint):
        db, store = env
        with pytest.raises(ProjectBindingStoreError):
            store.insert_initial_in(
                db.connection, project_key=_PK, display_path=_DP,
                endpoint=endpoint)

    @pytest.mark.parametrize("session_id", ["", "   "])
    def test_blank_session_id_rejected(self, env, session_id):
        db, store = env
        with pytest.raises(ProjectBindingStoreError):
            store.insert_initial_in(
                db.connection, project_key=_PK, display_path=_DP,
                endpoint=_EP, session_id=session_id)

    @pytest.mark.parametrize(
        "config_revision",
        [0, -3, 1.5, True, "5", object()])
    def test_invalid_config_revision_rejected(self, env, config_revision):
        db, store = env
        with pytest.raises(ProjectBindingStoreError):
            store.insert_initial_in(
                db.connection, project_key=_PK, display_path=_DP,
                endpoint=_EP, config_revision=config_revision)


class TestCasSuccess:
    def test_cas_success_updates_and_increments_by_exactly_one(self, env):
        db, store = env
        _insert(db, store)
        result = _cas(db, store, session_id="sess-1")
        assert result.outcome is UpdateOutcome.UPDATED
        assert result.current_binding_revision == 0
        assert result.new_binding_revision == 1
        row = store.read(_PK)
        assert row is not None
        assert row.binding_revision == 1
        assert row.session_id == "sess-1"

    def test_repeated_cas_revision_increments_each_time(self, env):
        db, store = env
        _insert(db, store)
        _cas(db, store, expected_binding_revision=0,
             rotation_count=1, session_id="s0")
        second = _cas(db, store, expected_binding_revision=1,
                      rotation_count=1, rotation_sequence=1,
                      session_id="s1")
        assert second.outcome is UpdateOutcome.UPDATED
        assert second.new_binding_revision == 2
        assert store.read(_PK).binding_revision == 2

    def test_stale_expected_revision_rejected(self, env):
        db, store = env
        _insert(db, store)
        _cas(db, store, expected_binding_revision=0,
             rotation_count=1, session_id="s0")
        result = _cas(db, store, expected_binding_revision=0,
                      rotation_count=1, rotation_sequence=1,
                      session_id="s1")
        assert result.outcome is UpdateOutcome.REVISION_MISMATCH
        assert result.current_binding_revision == 1
        row = store.read(_PK)
        assert row.binding_revision == 1
        assert row.session_id == "s0"

    def test_cas_on_missing_project_not_found(self, env):
        db, store = env
        result = _cas(db, store, project_key="p-absent",
                      expected_binding_revision=0)
        assert result.outcome is UpdateOutcome.NOT_FOUND
        assert result.current_binding_revision is None

    def test_future_expected_revision_mismatch(self, env):
        db, store = env
        _insert(db, store)
        result = _cas(db, store, expected_binding_revision=5,
                      session_id="s-x")
        assert result.outcome is UpdateOutcome.REVISION_MISMATCH
        assert result.current_binding_revision == 0
        assert store.read(_PK).binding_revision == 0

    @pytest.mark.parametrize("bad", [-1, 0.5, "1", None])
    def test_non_integer_expected_revision_rejected(self, env, bad):
        db, store = env
        _insert(db, store)
        with pytest.raises(ProjectBindingStoreError):
            _cas(db, store, expected_binding_revision=bad,
                 session_id="s-x")

    def test_cas_failure_leaves_row_byte_for_byte_unchanged(self, env):
        db, store = env
        before = store.read(_PK)
        assert before is None or before is not None
        _seed_config_revisions(db, 3)
        _insert(db, store, session_id="sess-a", title="t0",
                config_revision=3)
        before = store.read(_PK)
        result = _cas(db, store, expected_binding_revision=99,
                      session_id="sess-b", candidate_operation_id="op-1",
                      rotation_count=1, rotation_sequence=1,
                      rotation_base_title="t1", config_revision=9)
        assert result.outcome is UpdateOutcome.REVISION_MISMATCH
        after = store.read(_PK)
        assert after == before


class TestCasNoChange:
    def test_all_equal_returns_no_change_and_no_bump(self, env):
        db, store = env
        _insert(db, store, session_id="sess-x")
        result = _cas(db, store, session_id="sess-x")
        assert result.outcome is UpdateOutcome.NO_CHANGE
        assert result.current_binding_revision == 0
        assert result.new_binding_revision == 0
        row = store.read(_PK)
        assert row.binding_revision == 0
        assert row.session_id == "sess-x"

    def test_no_change_does_not_touch_db_row(self, env):
        db, store = env
        _seed_config_revisions(db, 4)
        _insert(db, store, session_id="sess-x", title="keep",
                config_revision=4)
        result = _cas(db, store, session_id="sess-x", rotation_count=0,
                      rotation_sequence=0, rotation_base_title="keep",
                      config_revision=4)
        assert result.outcome is UpdateOutcome.NO_CHANGE
        row = store.read(_PK)
        assert row.binding_revision == 0
        assert row.config_revision == 4

    def test_one_field_changed_is_update_not_no_change(self, env):
        db, store = env
        _insert(db, store, session_id="sess-x")
        result = _cas(db, store, session_id="sess-x",
                      rotation_count=1, rotation_sequence=0,
                      rotation_base_title="t1", config_revision=None)
        assert result.outcome is UpdateOutcome.UPDATED
        assert result.new_binding_revision == 1


class TestMutableFields:
    def test_session_id_set_from_none(self, env):
        db, store = env
        _insert(db, store)
        _cas(db, store, session_id="sess-new")
        assert store.read(_PK).session_id == "sess-new"

    def test_session_id_replace(self, env):
        db, store = env
        _insert(db, store, session_id="sess-a")
        _cas(db, store, expected_binding_revision=0,
             rotation_count=1, rotation_sequence=0,
             rotation_base_title="t", config_revision=None,
             session_id="sess-b")
        assert store.read(_PK).session_id == "sess-b"

    def test_session_id_clear_to_none(self, env):
        db, store = env
        _insert(db, store, session_id="sess-a")
        _cas(db, store, expected_binding_revision=0, session_id=None,
             rotation_count=1, rotation_sequence=0,
             rotation_base_title="t", config_revision=None)
        assert store.read(_PK).session_id is None

    def test_blank_session_id_rejected_in_cas(self, env):
        db, store = env
        _insert(db, store)
        with pytest.raises(ProjectBindingStoreError):
            _cas(db, store, session_id="   ")

    def test_candidate_set_from_none(self, env):
        db, store = env
        _insert(db, store)
        _cas(db, store, expected_binding_revision=0,
             candidate_operation_id="op-001", session_id=None,
             rotation_count=0, rotation_sequence=0,
             rotation_base_title="t", config_revision=None)
        row = store.read(_PK)
        assert row.candidate_operation_id == "op-001"
        assert row.binding_revision == 1

    def test_candidate_replace(self, env):
        db, store = env
        _insert(db, store)
        _cas(db, store, expected_binding_revision=0,
             candidate_operation_id="op-001", session_id="s0",
             rotation_count=0, rotation_sequence=0,
             rotation_base_title="t", config_revision=None)
        _cas(db, store, expected_binding_revision=1,
             candidate_operation_id="op-002", session_id="s0",
             rotation_count=0, rotation_sequence=0,
             rotation_base_title="t", config_revision=None)
        assert store.read(_PK).candidate_operation_id == "op-002"

    def test_candidate_clear_to_none(self, env):
        db, store = env
        _insert(db, store)
        _cas(db, store, expected_binding_revision=0,
             candidate_operation_id="op-001", session_id="s0",
             rotation_count=0, rotation_sequence=0,
             rotation_base_title="t", config_revision=None)
        _cas(db, store, expected_binding_revision=1,
             candidate_operation_id=None, session_id="s0",
             rotation_count=0, rotation_sequence=0,
             rotation_base_title="t", config_revision=None)
        assert store.read(_PK).candidate_operation_id is None

    def test_blank_candidate_rejected_in_cas(self, env):
        db, store = env
        _insert(db, store)
        with pytest.raises(ProjectBindingStoreError):
            _cas(db, store, expected_binding_revision=0,
                 candidate_operation_id="  ", session_id="s0",
                 rotation_count=0, rotation_sequence=0,
                 rotation_base_title="t", config_revision=None)

    def test_rotation_base_title_update(self, env):
        db, store = env
        _insert(db, store)
        _cas(db, store, expected_binding_revision=0,
             rotation_base_title="标题B", session_id=None,
             rotation_count=1, rotation_sequence=0, config_revision=None)
        assert store.read(_PK).rotation_base_title == "标题B"

    @pytest.mark.parametrize(
        "config_revision",
        [0, -2, 2.5, "3", True])
    def test_invalid_config_revision_rejected_in_cas(
        self, env, config_revision):
        db, store = env
        _insert(db, store)
        with pytest.raises(ProjectBindingStoreError):
            _cas(db, store, expected_binding_revision=0,
                 rotation_count=1, rotation_sequence=0,
                 rotation_base_title="t",
                 config_revision=config_revision)

    def test_config_revision_update_and_clear(self, env):
        db, store = env
        _seed_config_revisions(db, 6)
        _insert(db, store)
        _cas(db, store, expected_binding_revision=0,
             rotation_count=1, rotation_sequence=0,
             rotation_base_title="t", config_revision=6)
        assert store.read(_PK).config_revision == 6
        _cas(db, store, expected_binding_revision=1,
             rotation_count=1, rotation_sequence=0,
             rotation_base_title="t", config_revision=None)
        assert store.read(_PK).config_revision is None


class TestCounterMonotonic:
    @pytest.mark.parametrize("field", ["rotation_count", "rotation_sequence"])
    def test_same_counter_allowed(self, env, field):
        db, store = env
        _insert(db, store)
        _cas(db, store, expected_binding_revision=0,
             rotation_count=1, rotation_sequence=1)
        # 计数器不变（same），但其余字段变化 → 仍为 UPDATED
        other = {"rotation_count": 1, "rotation_sequence": 1}
        result = _cas(db, store, expected_binding_revision=1,
                      **other, rotation_base_title="t-same")
        assert result.outcome is UpdateOutcome.UPDATED
        assert store.read(_PK).binding_revision == 2

    @pytest.mark.parametrize("field", ["rotation_count", "rotation_sequence"])
    def test_plus_one_allowed(self, env, field):
        db, store = env
        _insert(db, store)
        result = _cas(db, store, expected_binding_revision=0,
                      **{field: 1},
                      **({"rotation_count": 0} if field == "rotation_sequence"
                         else {"rotation_sequence": 0}))
        assert result.outcome is UpdateOutcome.UPDATED

    @pytest.mark.parametrize("field", ["rotation_count", "rotation_sequence"])
    def test_decrease_rejected(self, env, field):
        db, store = env
        _insert(db, store)
        _cas(db, store, expected_binding_revision=0,
             rotation_count=1, rotation_sequence=1)
        other = 1
        bad = -1 if field == "rotation_count" else 0
        if field == "rotation_count":
            kw = dict(rotation_count=0, rotation_sequence=1,
                      expected_binding_revision=1)
        else:
            kw = dict(rotation_count=1, rotation_sequence=0,
                      expected_binding_revision=1)
        with pytest.raises(ProjectBindingStoreError):
            _cas(db, store, **kw)
        row = store.read(_PK)
        assert row.rotation_count == 1
        assert row.rotation_sequence == 1
        assert row.binding_revision == 1

    @pytest.mark.parametrize("field", ["rotation_count", "rotation_sequence"])
    def test_jump_over_two_rejected(self, env, field):
        db, store = env
        _insert(db, store)
        kw = dict(expected_binding_revision=0)
        kw[field] = 2
        kw["rotation_count"] = 2 if field == "rotation_count" else 0
        kw["rotation_sequence"] = 0 if field == "rotation_count" else 2
        with pytest.raises(ProjectBindingStoreError):
            _cas(db, store, **kw)
        row = store.read(_PK)
        assert row.binding_revision == 0
        assert row.rotation_count == 0
        assert row.rotation_sequence == 0

    @pytest.mark.parametrize("field", ["rotation_count", "rotation_sequence"])
    def test_negative_rejected(self, env, field):
        db, store = env
        _insert(db, store)
        kw = dict(expected_binding_revision=0,
                  rotation_count=-1 if field == "rotation_count" else 0,
                  rotation_sequence=0 if field == "rotation_count" else -1)
        with pytest.raises(ProjectBindingStoreError):
            _cas(db, store, **kw)

    @pytest.mark.parametrize("field", ["rotation_count", "rotation_sequence"])
    def test_non_integer_rejected(self, env, field):
        db, store = env
        _insert(db, store)
        kw = dict(expected_binding_revision=0)
        kw[field] = 1.0
        kw["rotation_count"] = 1.0 if field == "rotation_count" else 0
        kw["rotation_sequence"] = 0 if field == "rotation_count" else 1.0
        with pytest.raises(ProjectBindingStoreError):
            _cas(db, store, **kw)


class TestIdentityStable:
    def test_identity_fields_never_touched_by_cas(self, env):
        db, store = env
        _seed_config_revisions(db, 9)
        _insert(db, store)
        _cas(db, store, expected_binding_revision=0,
             session_id="sess-9", candidate_operation_id="op-9",
             rotation_count=1, rotation_sequence=1,
             rotation_base_title="t9", config_revision=9)
        row = store.read(_PK)
        assert row.project_key == _PK
        assert row.display_path == _DP
        assert row.endpoint == _EP


class TestConcurrencyAndRollback:
    def test_stale_concurrent_cas_only_one_winner(self, env):
        db, store = env
        _insert(db, store)
        first = _cas(db, store, expected_binding_revision=0,
                     rotation_count=1, session_id="winner")
        assert first.outcome is UpdateOutcome.UPDATED
        second = _cas(db, store, expected_binding_revision=0,
                      rotation_count=1, rotation_sequence=1,
                      session_id="loser")
        assert second.outcome is UpdateOutcome.REVISION_MISMATCH
        row = store.read(_PK)
        assert row.session_id == "winner"
        assert row.binding_revision == 1

    def test_failed_cas_inside_txn_rolls_back_whole_txn(self, env):
        db, store = env
        _seed_config_revisions(db, 1)
        _insert(db, store)
        before = store.read(_PK)
        with pytest.raises(RuntimeError):
            with db.transaction():
                store.cas_update_in(
                    db.connection, project_key=_PK,
                    expected_binding_revision=0, session_id="s1",
                    candidate_operation_id="op-1", rotation_count=1,
                    rotation_sequence=0, rotation_base_title="t1",
                    config_revision=1)
                raise RuntimeError("rollback trigger")
        assert store.read(_PK) == before

    def test_insert_failure_rolls_back_txn(self, env):
        db, store = env
        with pytest.raises(ProjectBindingStoreError):
            with db.transaction():
                store.insert_initial_in(
                    db.connection, project_key="  ", display_path=_DP,
                    endpoint=_EP)
        assert store.read("  ".strip()) is None
        assert store.read(_PK) is None