"""T17-B3：真实设置保存闭环集成测试（Controller + MainWindow UI 流）。

关键做法（R3 任务书 §6 建议）：
- 使用真实 SettingsService + 小型 Fake SettingsStore，
  不 mock 掉 validate → CAS commit → 成功才替换 current 的语义；
- UI 流通过页面 `save_requested` 信号 / handler 真实走通成功与失败两端。
"""

from __future__ import annotations

import os
import sys
from copy import deepcopy
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.commands import (  # noqa: E402
    SettingsSaveController,
    SettingsSaveOutcomeKind,
    SettingsSaveResult,
)
from app.snapshots import fake_snapshot  # noqa: E402
from core.domain import ConfigBody, SettingsDraft, SettingsSnapshot  # noqa: E402
from core.settings_service import SettingsService  # noqa: E402
from storage.settings_store import SettingsConflictError  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402


class FakeSettingsStore:
    """最小 CAS store：load_current / commit 语义与真实 SettingsStore 一致。"""

    def __init__(self) -> None:
        self._revision: int | None = None
        self._config: ConfigBody | None = None
        self._created_at: str = ""
        self.fail_commit_generic = False

    def load_current(self) -> SettingsSnapshot | None:
        if self._revision is None:
            return None
        return SettingsSnapshot(
            revision=self._revision,
            created_at=self._created_at,
            config=self._config,
        )

    def commit(self, *, base_revision, config, created_at, actor="user") -> SettingsSnapshot:
        if self.fail_commit_generic:
            raise RuntimeError("sqlite 写入失败")
        current = self._revision
        if current != base_revision:
            raise SettingsConflictError(
                "config 已被并发更新",
                expected=base_revision,
                actual=current,
            )
        self._revision = 1 if current is None else current + 1
        self._config = config
        self._created_at = created_at
        return SettingsSnapshot(
            revision=self._revision,
            created_at=self._created_at,
            config=config,
        )


def _draft(**overrides) -> SettingsDraft:
    draft = deepcopy(SettingsDraft.defaults())
    for key, value in overrides.items():
        draft[key] = value
    return draft


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    app.setStyleSheet("")


def _make_ctrl(revision_seed: int | None = None) -> tuple[SettingsService, SettingsSaveController]:
    store = FakeSettingsStore()
    svc = SettingsService(store)
    if revision_seed is not None:
        for _ in range(revision_seed):
            svc.submit_draft(_draft(), base_revision=svc.current.revision if svc.current is not None else None)
    ctrl = SettingsSaveController(svc)
    return svc, ctrl


def _win(ctrl):
    win = MainWindow(fake_snapshot(config_revision="7"), settings_save_controller=ctrl)
    win.show()
    win.activateWindow()
    QApplication.processEvents()
    return win


def _set_url_text(page, text: str) -> None:
    page._widgets["openchamber.url"].setText(text)
    QApplication.processEvents()


# ================================================================ Controller


class TestSettingsSaveController:
    def test_current_always_equals_service_current(self):
        svc, ctrl = _make_ctrl()
        assert ctrl.current is svc.current
        assert ctrl.current is None
        result = ctrl.save(_draft(), base_revision=None)
        assert result.kind is SettingsSaveOutcomeKind.SAVED
        assert ctrl.current is svc.current
        assert ctrl.current is not None

    def test_valid_draft_save_returns_saved_with_new_revision(self):
        _, ctrl = _make_ctrl()
        result = ctrl.save(_draft(), base_revision=None)
        assert result.kind is SettingsSaveOutcomeKind.SAVED
        assert result.base_revision is None
        assert result.new_revision == 1
        assert result.actual_revision is None
        assert "rev 1" in result.message

    def test_success_updates_service_and_controller_current(self):
        svc, ctrl = _make_ctrl()
        ctrl.save(_draft(), base_revision=None)
        assert svc.current is not None and svc.current.revision == 1
        assert ctrl.current is not None and ctrl.current.revision == 1
        ctrl.save(_draft(), base_revision=1)
        assert svc.current.revision == 2
        assert ctrl.current.revision == 2

    def test_validation_failure_preserves_current(self):
        svc, ctrl = _make_ctrl()
        assert ctrl.save(_draft(), base_revision=None).kind is SettingsSaveOutcomeKind.SAVED
        before = svc.current
        result = ctrl.save(_draft(schema_version=999), base_revision=1)
        assert result.kind is SettingsSaveOutcomeKind.VALIDATION_ERROR
        assert "仍使用原配置" in result.message
        assert svc.current is before
        assert svc.current.revision == 1

    def test_cas_conflict_preserves_current(self):
        svc, ctrl = _make_ctrl()
        ctrl.save(_draft(), base_revision=None)
        before = svc.current
        result = ctrl.save(_draft(), base_revision=None)  # 过期 base_revision
        assert result.kind is SettingsSaveOutcomeKind.CONFLICT
        assert result.actual_revision == 1
        assert svc.current is before
        assert svc.current.revision == 1

    def test_commit_failure_preserves_current(self):
        store = FakeSettingsStore()
        store.fail_commit_generic = True
        svc = SettingsService(store)
        ctrl = SettingsSaveController(svc)
        result = ctrl.save(_draft(), base_revision=None)
        assert result.kind is SettingsSaveOutcomeKind.COMMIT_ERROR
        assert "仍使用原配置" in result.message
        assert svc.current is None


# ================================================================ MainWindow UI 流


class TestMainWindowSaveFlow:
    def test_initial_committed_baseline_from_controller(self, qapp):
        svc, ctrl = _make_ctrl(revision_seed=1)
        win = _win(ctrl)
        assert win.settings_page._committed is ctrl.current
        assert win.settings_page.base_revision == 1
        assert win.settings_page.dirty is False
        win.close()

    def test_ui_save_success_clears_dirty_and_updates_revision(self, qapp):
        svc, ctrl = _make_ctrl(revision_seed=1)
        win = _win(ctrl)
        page = win.settings_page
        _set_url_text(page, "http://10.0.0.7:9751")
        assert page.dirty is True
        page.save_requested.emit(page.build_draft(), page.base_revision)
        QApplication.processEvents()
        assert page.dirty is False
        assert page.base_revision == 2
        assert page._committed is ctrl.current
        assert "rev 2" in page._save_feedback.text()
        win.close()

    def test_ui_save_failure_preserves_draft_and_old_revision(self, qapp):
        svc, ctrl = _make_ctrl(revision_seed=1)
        win = _win(ctrl)
        page = win.settings_page
        _set_url_text(page, "http://10.0.0.77:9751")
        assert page.dirty is True
        edited_url = page.build_draft()["openchamber"]["url"]
        page.save_requested.emit(page.build_draft(), None)  # 过期 base → CONFLICT
        QApplication.processEvents()
        assert page.base_revision == 1
        assert page.dirty is True
        assert page.build_draft()["openchamber"]["url"] == edited_url
        assert "仍使用原配置" in page._save_feedback.text()
        win.close()

    def test_no_controller_shows_not_connected_and_keeps_draft(self, qapp):
        win = MainWindow(fake_snapshot(config_revision="7"))
        win.show()
        QApplication.processEvents()
        page = win.settings_page
        _set_url_text(page, "http://10.0.0.9:9751")
        edited_url = page.build_draft()["openchamber"]["url"]
        page.save_requested.emit(page.build_draft(), page.base_revision)
        QApplication.processEvents()
        assert "尚未接入" in page._save_feedback.text()
        assert "仍使用原配置" in page._save_feedback.text()
        assert page.dirty is True
        assert page.build_draft()["openchamber"]["url"] == edited_url
        win.close()


class TestRunningTaskFreeze:
    def test_snapshot_and_active_task_frozen_after_save(self, qapp):
        svc, ctrl = _make_ctrl(revision_seed=1)
        snap = fake_snapshot(config_revision="7")
        win = MainWindow(snap, settings_save_controller=ctrl)
        win.show()
        QApplication.processEvents()
        snap_id = id(win.snapshot)
        old_active = win.snapshot.active_task
        page = win.settings_page
        page.save_requested.emit(page.build_draft(), page.base_revision)
        QApplication.processEvents()
        assert id(win.snapshot) == snap_id
        assert win.snapshot.active_task is old_active
        assert win.snapshot.active_task.config_revision == "7"
        assert page.base_revision == 2
        label = page._revision_label.text()
        assert "当前生效配置 rev 2" in label
        assert "仍使用 rev 7" in label
        assert "只影响之后接收/启动的任务" in label
        win.close()


class _MismatchFakeCtrl:
    """duck-typed 假控制器：SAVED 结果但 current.revision 与新版本不一致。"""

    def __init__(self, current, result) -> None:
        self._current = current
        self._result = result

    @property
    def current(self):
        return self._current

    def save(self, draft, *, base_revision):
        return self._result


def _snapshot_at(revision: int) -> SettingsSnapshot:
    store = FakeSettingsStore()
    svc = SettingsService(store)
    for _ in range(revision):
        svc.submit_draft(
            _draft(),
            base_revision=svc.current.revision if svc.current is not None else None,
        )
    return svc.current


class TestSaveStateVerification:
    def test_saved_result_but_current_mismatch_no_rebase_no_old_claim(self, qapp):
        current = _snapshot_at(3)
        result = SettingsSaveResult(
            kind=SettingsSaveOutcomeKind.SAVED,
            message="设置已保存，当前生效配置 rev 5",
            base_revision=3,
            new_revision=5,
        )
        win = MainWindow(
            fake_snapshot(config_revision="7"),
            settings_save_controller=_MismatchFakeCtrl(current, result),
        )
        win.show()
        QApplication.processEvents()
        page = win.settings_page
        _set_url_text(page, "http://10.0.0.6:9751")
        assert page.dirty is True
        old_base = page.base_revision
        edited_url = page.build_draft()["openchamber"]["url"]
        page.save_requested.emit(page.build_draft(), page.base_revision)
        QApplication.processEvents()
        assert page.dirty is True
        assert page.base_revision == old_base
        assert page._committed is current
        assert page.build_draft()["openchamber"]["url"] == edited_url
        feedback = page._save_feedback.text()
        assert "状态核验异常" in feedback
        assert "仍使用原配置" not in feedback
        win.close()