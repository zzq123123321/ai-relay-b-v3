"""T17-B1：设置保存异常映射 + 配置 revision Presenter 合同。

对应 A 端 B1 第 10 节测试清单：
- Save Contract 1–8（map_settings_save_error）
- Presenter 9–19（present_settings_revision；int/str revision 归一）
"""

import dataclasses

import pytest

from app.commands import (
    SettingsSaveOutcomeKind,
    SettingsSaveResult,
    map_settings_save_error,
)
from app.snapshots import empty_snapshot, fake_snapshot
from core.settings_service import SettingsCommitError, SettingsValidationError
from storage.settings_store import SettingsConflictError
from ui.status_presenter import (
    SettingsRevisionPresentation,
    present_settings_revision,
)


class TestSaveMapping:
    def test_validation_error_kind(self):
        result = map_settings_save_error(
            SettingsValidationError("default_target 不能为 EXECUTOR"),
            base_revision=7,
        )
        assert result.kind is SettingsSaveOutcomeKind.VALIDATION_ERROR
        assert result.base_revision == 7
        assert result.new_revision is None
        assert result.actual_revision is None

    def test_validation_message_contains_still_using_old_config(self):
        result = map_settings_save_error(SettingsValidationError("目录不存在"), base_revision=7)
        assert "仍使用原配置" in result.message
        assert "目录不存在" in result.message

    def test_conflict_maps_actual_and_base(self):
        result = map_settings_save_error(
            SettingsConflictError("CAS 冲突", expected=12, actual=13),
            base_revision=12,
        )
        assert result.kind is SettingsSaveOutcomeKind.CONFLICT
        assert result.base_revision == 12
        assert result.actual_revision == 13
        assert "rev 13" in result.message
        assert "rev 12" in result.message
        assert "重新载入" in result.message
        assert "仍使用原配置" in result.message

    def test_conflict_does_not_fake_new_revision(self):
        result = map_settings_save_error(
            SettingsConflictError("CAS 冲突", expected=8, actual=9),
            base_revision=8,
        )
        assert result.new_revision is None
        assert result.actual_revision == 9

    def test_commit_error_kind(self):
        result = map_settings_save_error(SettingsCommitError("写库失败"), base_revision=7)
        assert result.kind is SettingsSaveOutcomeKind.COMMIT_ERROR
        assert result.actual_revision is None
        assert "仍使用原配置" in result.message

    def test_unknown_error_kind(self):
        result = map_settings_save_error(RuntimeError("磁盘忙"), base_revision=7)
        assert result.kind is SettingsSaveOutcomeKind.ERROR
        assert "仍使用原配置" in result.message

    def test_all_failures_leave_new_revision_none(self):
        errors = (
            SettingsValidationError("x"),
            SettingsConflictError("x", expected=1, actual=2),
            SettingsCommitError("x"),
            RuntimeError("x"),
        )
        for exc in errors:
            result = map_settings_save_error(exc, base_revision=1)
            assert result.new_revision is None
            assert result.kind is not SettingsSaveOutcomeKind.SAVED

    def test_validation_error_code_fields(self):
        exc = SettingsValidationError("xa")
        assert exc.code == "settings_validation"

    def test_first_save_conflict_no_rev_none(self):
        result = map_settings_save_error(
            SettingsConflictError("首次保存竞争", expected=None, actual=1),
            base_revision=None,
        )
        assert result.kind is SettingsSaveOutcomeKind.CONFLICT
        assert result.base_revision is None
        assert result.actual_revision == 1
        assert result.new_revision is None
        assert "rev None" not in result.message
        assert "尚未保存配置" in result.message
        assert "仍使用原配置" in result.message

    def test_conflict_actual_none_no_rev_none(self):
        result = map_settings_save_error(
            SettingsConflictError("反向异常边界", expected=1, actual=None),
            base_revision=1,
        )
        assert result.kind is SettingsSaveOutcomeKind.CONFLICT
        assert result.base_revision == 1
        assert result.actual_revision is None
        assert result.new_revision is None
        assert "rev None" not in result.message
        assert "没有生效配置" in result.message
        assert "仍使用原配置" in result.message

    def test_conflict_regular_revs_still_full_text(self):
        result = map_settings_save_error(
            SettingsConflictError("CAS 冲突", expected=12, actual=13),
            base_revision=12,
        )
        assert "rev 13" in result.message
        assert "rev 12" in result.message

    def test_frozen_save_result(self):
        r = SettingsSaveResult(
            kind=SettingsSaveOutcomeKind.SAVED,
            message="ok",
            base_revision=1,
            new_revision=2,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.kind = SettingsSaveOutcomeKind.ERROR  # type: ignore[misc]


class TestRevisionPresenter:
    def _active(self, config_revision=None):
        return fake_snapshot(config_revision=config_revision)

    def test_committed_none_shows_not_saved(self):
        p = present_settings_revision(empty_snapshot(), None)
        assert isinstance(p, SettingsRevisionPresentation)
        assert p.headline == "尚未保存配置"
        assert p.differs is None
        assert p.tone == "neutral"

    def test_committed_none_even_with_active(self):
        p = present_settings_revision(self._active("12"), None)
        assert p.headline == "尚未保存配置"

    def test_committed_with_no_active(self):
        p = present_settings_revision(empty_snapshot(), 12)
        assert p.headline == "当前生效配置 rev 12"
        assert p.detail == "当前无活动任务"
        assert p.differs is None

    def test_same_revision_int_and_str(self):
        p = present_settings_revision(self._active("12"), 12)
        assert p.differs is False
        assert p.tone == "success"
        assert p.detail == "当前活动任务使用相同配置版本"

    def test_active_older_differs(self):
        p = present_settings_revision(self._active("10"), 13)
        assert p.differs is True
        assert p.tone == "recovering"
        assert p.headline == "当前生效配置 rev 13"
        assert "仍使用 rev 10" in p.detail
        assert "新设置只影响之后接收/启动的任务" in p.detail

    @pytest.mark.parametrize("bad", [None, "", "rev10", "0", "-1", "1.2", "  ", "v2"])
    def test_active_invalid_revision_not_guessed(self, bad):
        p = present_settings_revision(self._active(bad), 13)
        assert p.headline == "当前生效配置 rev 13"
        assert p.detail == "当前活动任务未报告有效配置版本"
        assert p.differs is None
        assert p.tone == "neutral"

    def test_active_false_not_revision(self):
        p = present_settings_revision(fake_snapshot(config_revision="False"), 13)
        assert p.differs is None
        assert "未报告有效配置版本" in p.detail

    def test_present_does_not_mutate_snapshot(self):
        snap = self._active("10")
        present_settings_revision(snap, 13)
        assert snap.active_task.config_revision == "10"

    def test_detail_contains_new_settings_take_effect_later(self):
        p = present_settings_revision(self._active("10"), 13)
        assert "新设置只影响之后接收/启动的任务" in p.detail

    def test_large_revision_int_and_str_same(self):
        p = present_settings_revision(self._active("10000000000"), 10000000000)
        assert p.differs is False
        assert p.tone == "success"
        assert p.detail == "当前活动任务使用相同配置版本"

    def test_large_revision_different_shows_both(self):
        p = present_settings_revision(self._active("10000000000"), 10000000001)
        assert p.differs is True
        assert "rev 10000000001" in p.headline
        assert "仍使用 rev 10000000000" in p.detail