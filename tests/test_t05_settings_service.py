"""T05：SettingsService——草稿校验、原子提交发布、接收/执行快照与 FIXED/PROJECT_ROTATING。

覆盖验收关联：S01（校验/commit 失败生效配置不变）、S02（NaN/Infinity/bool 冒充数值拒绝）、
Q03（接收后改默认目录/执行端，已接受任务用原接收快照）、Q04（PROJECT_ROTATING 用执行期
已提交新会话，FIXED 不跟随）。均使用 pytest tmp_path。
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from core.domain import (
    ReceiveSettingsSnapshot,
    SessionBindingMode,
    SettingsDraft,
    SettingsSnapshot,
    TargetExecutor,
)
from core.settings_service import (
    SettingsCommitError,
    SettingsService,
    SettingsValidationError,
    validate_config,
)
from storage.database import Database
from storage.settings_store import SettingsConflictError, SettingsStore


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "relay.sqlite3")
    database.open()
    yield database
    database.close()


@pytest.fixture
def store(db):
    return SettingsStore(db)


@pytest.fixture
def service(store):
    return SettingsService(store)


def _submit(service, draft=None, *, executor=None, directory=None, session=None, base=None):
    if draft is None:
        draft = SettingsDraft.defaults()
    if executor is not None:
        draft["default_target"] = executor
    if directory is not None:
        draft["openchamber"]["directory"] = directory
    if session is not None:
        draft["openchamber"]["session_id"] = session
    if base is None and service.current is not None:
        base = service.current.revision
    return service.submit_draft(draft, base_revision=base)


# ------------------------------------------------------------------ 基础/不可变


def test_default_draft_validates_to_config_body():
    body = validate_config(SettingsDraft.defaults())
    assert body.default_target is TargetExecutor.OPENCHAMBER
    assert body.openchamber.url == "http://127.0.0.1:57123"
    assert body.recovery.poll_interval_seconds == 2.0


def test_snapshot_is_immutable(service):
    _submit(service, executor=TargetExecutor.OPENCHAMBER)
    snap = service.current
    assert isinstance(snap, SettingsSnapshot)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.revision = 99  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.config.openchamber.url = "http://evil"  # type: ignore[misc]


def test_mutating_draft_after_commit_does_not_affect_runtime(service):
    draft = SettingsDraft.defaults()
    _submit(service, draft=draft)
    executor_before = service.current.config.default_target
    draft["default_target"] = "REASONIX"
    draft["openchamber"]["directory"] = "C:\\changed"
    assert service.current.config.default_target is executor_before
    assert service.current.config.openchamber.directory == ""


# ------------------------------------------------------------------ 校验（S02）


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_rejected(service, bad: float):
    draft = SettingsDraft.defaults()
    draft["recovery"]["poll_interval_seconds"] = bad
    with pytest.raises(SettingsValidationError):
        _submit(service, draft=draft)
    assert service.current is None  # 校验失败不产生任何 revision


def test_bool_not_accepted_as_number(service):
    for path, key in (("recovery", "idle_confirmations"), ("limits", "queued_tasks")):
        draft = SettingsDraft.defaults()
        draft[path][key] = True
        with pytest.raises(SettingsValidationError):
            _submit(service, draft=draft)


def test_bool_not_accepted_as_float(service):
    draft = SettingsDraft.defaults()
    draft["recovery"]["cooldown_seconds"] = False
    with pytest.raises(SettingsValidationError):
        _submit(service, draft=draft)


def test_unknown_executor_rejected(service):
    for bad in ("EXECUTOR", "reasonix", "brains"):
        draft = SettingsDraft.defaults()
        draft["default_target"] = bad
        with pytest.raises(SettingsValidationError):
            _submit(service, draft=draft)


def test_blank_required_string_rejected(service):
    draft = SettingsDraft.defaults()
    draft["openchamber"]["agent"] = "   "
    with pytest.raises(SettingsValidationError):
        _submit(service, draft=draft)


def test_out_of_range_rejected(service):
    draft = SettingsDraft.defaults()
    draft["recovery"]["poll_interval_seconds"] = 99999
    with pytest.raises(SettingsValidationError):
        _submit(service, draft=draft)


def test_url_with_userinfo_and_bad_scheme_rejected(service):
    draft = SettingsDraft.defaults()
    draft["openchamber"]["url"] = "http://user:pass@127.0.0.1:1"
    with pytest.raises(SettingsValidationError):
        _submit(service, draft=draft)
    draft = SettingsDraft.defaults()
    draft["openchamber"]["url"] = "ftp://127.0.0.1"
    with pytest.raises(SettingsValidationError):
        _submit(service, draft=draft)


# ------------------------------------------------------------------ 目录校验


def test_directory_must_be_absolute_existing_dir(service, tmp_path):
    valid = tmp_path / "projA"
    valid.mkdir()

    ok = _submit(service, directory=str(valid))
    assert ok.config.openchamber.directory == str(valid)

    # 相对路径
    draft = SettingsDraft.defaults()
    draft["openchamber"]["directory"] = "projA"
    with pytest.raises(SettingsValidationError):
        _submit(service, draft=draft)

    # 不存在
    with pytest.raises(SettingsValidationError):
        _submit(service, directory=str(tmp_path / "missing"))

    # 普通文件而不是目录
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    with pytest.raises(SettingsValidationError):
        _submit(service, directory=str(a_file))


def test_empty_directory_stays_unified(service, tmp_path):
    snap = _submit(service, directory="")
    assert snap.config.openchamber.directory == ""


# ------------------------------------------------------------------ 提交/发布（S01）


def test_commit_success_publishes_new_snapshot(service):
    s1 = _submit(service, executor=TargetExecutor.OPENCHAMBER)
    assert s1.revision == 1
    assert service.current is s1
    s2 = _submit(service, executor=TargetExecutor.REASONIX)
    assert s2.revision == 2
    assert service.current is s2
    assert service.current.config.default_target is TargetExecutor.REASONIX


def test_validation_failure_keeps_current_unchanged(service, tmp_path):
    _submit(service, executor=TargetExecutor.OPENCHAMBER, directory=str(tmp_path))
    before = service.current
    before_executor = before.config.default_target
    before_dir = before.config.openchamber.directory

    draft = SettingsDraft.defaults()
    draft["default_target"] = "REASONIX"
    draft["recovery"]["poll_interval_seconds"] = math.nan
    with pytest.raises(SettingsValidationError):
        _submit(service, draft=draft)

    assert service.current is before  # 同一个对象
    assert service.current.revision == 1
    assert service.current.config.default_target is before_executor
    assert service.current.config.openchamber.directory == before_dir


def test_commit_failure_keeps_current_unchanged(service, store, tmp_path):
    """S01 场景 A：commit SQL 中途失败 → 旧快照对象与值、DB 指针与行数全部不变。"""
    proj_a = tmp_path / "projA"
    proj_b = tmp_path / "projB"
    proj_a.mkdir()
    proj_b.mkdir()
    for _ in range(7):
        _submit(service, executor=TargetExecutor.OPENCHAMBER, directory=str(proj_a))
    assert service.current.revision == 7
    current = service.current
    current_executor = current.config.default_target
    current_dir = current.config.openchamber.directory

    store.fault_inject_after = 2  # 插入 config_revisions 这一条之后失败
    draft = SettingsDraft.defaults()
    draft["default_target"] = "REASONIX"
    draft["openchamber"]["directory"] = str(proj_b)
    with pytest.raises(SettingsCommitError):
        _submit(service, draft=draft)
    store.fault_inject_after = None

    assert service.current is current
    assert service.current.revision == 7
    assert service.current.config.default_target is current_executor
    assert service.current.config.openchamber.directory == current_dir
    assert store.load_current().revision == 7
    rows = store._db.connection.execute(
        "SELECT COUNT(*) FROM config_revisions"
    ).fetchone()[0]
    assert rows == 7


def test_cas_conflict_keeps_current_unchanged(service, tmp_path):
    _submit(service, executor=TargetExecutor.OPENCHAMBER)
    _submit(service, executor=TargetExecutor.REASONIX)  # 当前 rev=2
    with pytest.raises(SettingsConflictError):
        service.submit_draft(SettingsDraft.defaults(), base_revision=1)  # B 仍基于旧 base
    assert service.current.revision == 2
    assert service.current.config.default_target is TargetExecutor.REASONIX


def test_cas_scenario_b_two_editors_same_base(service, tmp_path):
    """场景 B：A/B 都基于 rev7；A 得 rev8，B 必须失败且不覆盖。"""
    for _ in range(7):
        _submit(service, executor=TargetExecutor.OPENCHAMBER)
    _submit(service, executor=TargetExecutor.REASONIX)  # A: 7 → 8
    assert service.current.revision == 8
    with pytest.raises(SettingsConflictError):
        service.submit_draft(SettingsDraft.defaults(), base_revision=7)  # B 失败
    assert service.current.revision == 8
    assert service.current.config.default_target is TargetExecutor.REASONIX


# ------------------------------------------------------------------ 接收/执行快照（Q03/Q04）


def test_receive_snapshot_frozen_after_config_change(service, tmp_path):
    old_dir = tmp_path / "projA"
    old_dir.mkdir()
    _submit(service, executor="OPENCHAMBER", directory=str(old_dir), session="sess-A")
    receive = service.build_receive_snapshot()
    assert receive.config_revision == 1
    assert receive.effective_executor is TargetExecutor.OPENCHAMBER
    assert receive.binding_mode is SessionBindingMode.FIXED_SESSION
    assert receive.frozen_session_id == "sess-A"
    assert receive.directory == str(old_dir)

    new_dir = tmp_path / "projB"
    new_dir.mkdir()
    _submit(service, executor="REASONIX", directory=str(new_dir), session="sess-B")

    # Q03：接收后改默认目录/执行端，已接收任务仍用原接收快照
    assert receive.config_revision == 1
    assert receive.effective_executor is TargetExecutor.OPENCHAMBER
    assert receive.directory == str(old_dir)
    assert receive.frozen_session_id == "sess-A"

    new_receive = service.build_receive_snapshot()
    assert new_receive.config_revision == 2
    assert new_receive.effective_executor is TargetExecutor.REASONIX


def test_execution_snapshot_unaffected_by_later_changes(service, tmp_path):
    base_dir = tmp_path / "projA"
    base_dir.mkdir()
    _submit(service, executor="OPENCHAMBER", directory=str(base_dir), session="sess-1")
    receive = service.build_receive_snapshot()
    execution = service.build_execution_snapshot(receive)

    new_dir = tmp_path / "projB"
    new_dir.mkdir()
    _submit(service, executor="REASONIX", directory=str(new_dir), session="sess-2")

    assert execution.receive.config_revision == 1
    assert execution.receive.directory == str(base_dir)
    assert execution.resolved_session_id == "sess-1"
    assert execution.binding_revision == 1


def test_fixed_execution_uses_frozen_session(service):
    _submit(service, session="frozen-id")
    receive = service.build_receive_snapshot()
    assert receive.binding_mode is SessionBindingMode.FIXED_SESSION
    assert receive.frozen_session_id == "frozen-id"
    # 传入值被忽略：FIXED 不跟随
    execution = service.build_execution_snapshot(receive, resolved_session_id="ignored")
    assert execution.resolved_session_id == "frozen-id"
    assert execution.binding_revision == receive.config_revision


def test_fixed_without_session_rejected_at_execution(service):
    _submit(service, session="")
    receive = service.build_receive_snapshot()
    with pytest.raises(SettingsValidationError):
        service.build_execution_snapshot(receive)


def test_project_rotating_uses_committed_session_at_execution(service):
    """Q04：PROJECT_ROTATING 排队，成功轮换后执行使用已提交的新会话，接收快照不跟随。"""
    _submit(service)
    draft = SettingsDraft.defaults()
    draft["openchamber"]["session_policy"] = "PROJECT_ROTATING"
    service.submit_draft(draft, base_revision=1)

    receive = service.build_receive_snapshot()
    assert receive.config_revision == 2
    assert receive.binding_mode is SessionBindingMode.PROJECT_ROTATING
    assert receive.frozen_session_id is None

    # 用户后续又改配置（轮换产生已提交的新 session）
    draft2 = SettingsDraft.defaults()
    draft2["openchamber"]["session_policy"] = "PROJECT_ROTATING"
    draft2["openchamber"]["session_id"] = "rotated-session"
    rotated = service.submit_draft(draft2, base_revision=2)

    execution = service.build_execution_snapshot(
        receive,
        resolved_session_id="rotated-session",
        binding_revision=rotated.revision,
    )
    assert execution.receive.config_revision == 2  # 接收时仍是旧策略身份
    assert execution.resolved_session_id == "rotated-session"
    assert execution.binding_revision == 3
    assert service.current.revision == 3


def test_receive_requires_existing_config(service):
    with pytest.raises(SettingsCommitError):
        service.build_receive_snapshot()


# ------------------------------------------------------------------ 重启


def test_restart_reloads_current_and_revision_continues(tmp_path):
    db1 = Database(tmp_path / "relay.sqlite3")
    db1.open()
    svc1 = SettingsService(SettingsStore(db1))
    _submit(svc1, executor="OPENCHAMBER")
    _submit(svc1, executor="REASONIX")
    db1.close()

    db2 = Database(tmp_path / "relay.sqlite3")
    db2.open()
    svc2 = SettingsService(SettingsStore(db2))
    assert svc2.current is not None and svc2.current.revision == 2
    assert svc2.current.config.default_target is TargetExecutor.REASONIX
    s3 = _submit(svc2, executor="OPENCHAMBER")
    assert s3.revision == 3  # 重启不回退、不复用旧 revision
    db2.close()