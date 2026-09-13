"""T17-B3-R5：Settings 离页 Save / Discard / Stay 门禁测试。

覆盖：门禁只在 PAGE05→其它页且 needs_save=True 且 decider 非空时触发；
stay/unknown 原地保留草稿并回滚导航选中态；discard 还原后离页；
save 复用 R3 保存闭环（成功离页 / 失败保留）；legacy 无 decider 正常离页。
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

PAGE01 = "PAGE01"
PAGE02 = "PAGE02"
PAGE05 = "PAGE05"
EDIT_URL = "http://10.9.9.9:8080"


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


class _RecordDecider:
    """decider 替身：可安排决策序列并记录调用次数。"""

    def __init__(self, *decisions) -> None:
        self._seq = list(decisions)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self._seq:
            return str(self._seq.pop(0))
        return "stay"


class _MismatchFakeCtrl:
    """duck-typed 假保存控制器：SAVED 结果但 current.revision 与新版本不一致。"""

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


def _real_save_ctrl(revision_seed: int = 1, fail_commit=False) -> tuple[FakeSettingsStore, SettingsSaveController]:
    store = FakeSettingsStore()
    svc = SettingsService(store)
    for _ in range(revision_seed):
        svc.submit_draft(_draft(), base_revision=svc.current.revision if svc.current is not None else None)
    store.fail_commit_generic = fail_commit
    return store, SettingsSaveController(svc)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    app.setStyleSheet("")


def _make_win(snapshot=None, save_ctrl=None, decider=None) -> tuple[MainWindow, _RecordDecider | _RecordDecider | None]:
    if decider is None:
        d: _RecordDecider | None = None
    else:
        d = decider
    win = MainWindow(
        fake_snapshot() if snapshot is None else snapshot,
        settings_save_controller=save_ctrl,
        settings_leave_decider=d,
    )
    win.show()
    win.activateWindow()
    QApplication.processEvents()
    win.navigate(PAGE05)
    QApplication.processEvents()
    return win, d


def _set_url(page, text: str = EDIT_URL) -> None:
    page._widgets["openchamber.url"].setText(text)
    QApplication.processEvents()


def _menu_checked(win) -> dict[str, bool]:
    return {action.data(): action.isChecked() for action in win._nav_actions}


# ================================================================ 门禁基础


class TestGateBasics:
    def test_needs_save_false_leaves_without_decider(self, qapp):
        _, ctrl = _real_save_ctrl(1)
        d = _RecordDecider("stay")
        win = MainWindow(fake_snapshot(), settings_save_controller=ctrl, settings_leave_decider=d)
        win.show()
        QApplication.processEvents()
        win.navigate(PAGE05)
        QApplication.processEvents()
        assert win.settings_page.needs_save is False
        win.navigate(PAGE01)
        QApplication.processEvents()
        assert d.calls == 0
        assert win.current_page == PAGE01

    def test_no_decider_keeps_legacy_navigation(self, qapp):
        win = MainWindow(fake_snapshot())
        win.show()
        QApplication.processEvents()
        win.navigate(PAGE05)
        _set_url(win.settings_page)
        assert win.settings_page.needs_save is True
        win.navigate(PAGE02)
        QApplication.processEvents()
        assert win.current_page == PAGE02
        assert win.settings_page.dirty is True

    def test_same_page_navigation_does_not_call_decider(self, qapp):
        d = _RecordDecider("stay")
        win = MainWindow(fake_snapshot(), settings_leave_decider=d)
        win.show()
        QApplication.processEvents()
        win.navigate(PAGE05)
        _set_url(win.settings_page)
        assert win.settings_page.needs_save is True
        win.navigate(PAGE05)
        QApplication.processEvents()
        assert d.calls == 0
        assert win.current_page == PAGE05

    def test_entering_settings_no_gate(self, qapp):
        d = _RecordDecider("stay")
        win = MainWindow(fake_snapshot(), settings_leave_decider=d)
        win.show()
        QApplication.processEvents()
        assert win.current_page == PAGE01
        win.navigate(PAGE05)
        QApplication.processEvents()
        assert d.calls == 0
        assert win.current_page == PAGE05


# ================================================================ 决策分支


class TestDecisions:
    def test_stay_keeps_page_draft_and_dirty(self, qapp):
        d = _RecordDecider("stay")
        win = MainWindow(fake_snapshot(), settings_leave_decider=d)
        win.show()
        QApplication.processEvents()
        win.navigate(PAGE05)
        _set_url(win.settings_page)
        win.navigate(PAGE02)
        QApplication.processEvents()
        assert win.current_page == PAGE05
        assert win.settings_page.dirty is True
        assert win.settings_page.build_draft()["openchamber"]["url"] == EDIT_URL

    def test_stay_restores_navbar_selection(self, qapp):
        d = _RecordDecider("stay")
        win = MainWindow(fake_snapshot(), settings_leave_decider=d)
        win.show()
        QApplication.processEvents()
        win.navigate(PAGE05)
        _set_url(win.settings_page)
        win.navigate(PAGE02)
        QApplication.processEvents()
        assert win.navbar.current == PAGE05
        assert win.navbar.button_for(PAGE05).isChecked() is True
        assert win.navbar.button_for(PAGE02).isChecked() is False

    def test_stay_restores_menu_checked(self, qapp):
        d = _RecordDecider("stay")
        win = MainWindow(fake_snapshot(), settings_leave_decider=d)
        win.show()
        QApplication.processEvents()
        win.navigate(PAGE05)
        _set_url(win.settings_page)
        win.navigate(PAGE02)
        QApplication.processEvents()
        checked = _menu_checked(win)
        assert checked[PAGE05] is True
        assert checked[PAGE02] is False

    def test_discard_restores_and_leaves(self, qapp):
        _, ctrl = _real_save_ctrl(1)
        d = _RecordDecider("discard")
        win = MainWindow(fake_snapshot(), settings_save_controller=ctrl, settings_leave_decider=d)
        win.show()
        QApplication.processEvents()
        win.navigate(PAGE05)
        _set_url(win.settings_page)
        assert win.settings_page.dirty is True
        win.navigate(PAGE02)
        QApplication.processEvents()
        assert win.current_page == PAGE02
        assert win.settings_page.dirty is False
        assert win.settings_page.base_revision == 1
        assert win.settings_page.build_draft()["openchamber"]["url"] != EDIT_URL

    def test_discard_without_committed_allows_leave(self, qapp):
        d = _RecordDecider("discard")
        win = MainWindow(fake_snapshot(), settings_leave_decider=d)
        win.show()
        QApplication.processEvents()
        win.navigate(PAGE05)
        assert win.settings_page.needs_save is True  # committed None
        win.navigate(PAGE01)
        QApplication.processEvents()
        assert win.current_page == PAGE01
        assert win.settings_page.dirty is False

    def test_save_success_leaves_and_updates_revision(self, qapp):
        _, ctrl = _real_save_ctrl(1)
        d = _RecordDecider("save")
        win = MainWindow(fake_snapshot(), settings_save_controller=ctrl, settings_leave_decider=d)
        win.show()
        QApplication.processEvents()
        win.navigate(PAGE05)
        _set_url(win.settings_page)
        base = win.settings_page.base_revision
        win.navigate(PAGE02)
        QApplication.processEvents()
        assert win.current_page == PAGE02
        assert win.settings_page.dirty is False
        assert win.settings_page.base_revision == base + 1

    def test_save_failure_stays_and_preserves(self, qapp):
        _, ctrl = _real_save_ctrl(1, fail_commit=True)
        d = _RecordDecider("save")
        win = MainWindow(fake_snapshot(), settings_save_controller=ctrl, settings_leave_decider=d)
        win.show()
        QApplication.processEvents()
        win.navigate(PAGE05)
        _set_url(win.settings_page)
        win.navigate(PAGE02)
        QApplication.processEvents()
        assert win.current_page == PAGE05
        assert win.settings_page.dirty is True
        assert win.settings_page.base_revision == 1
        assert win.settings_page.build_draft()["openchamber"]["url"] == EDIT_URL

    def test_save_mismatch_stays_with_verification_feedback(self, qapp):
        current = _snapshot_at(3)
        result = SettingsSaveResult(
            kind=SettingsSaveOutcomeKind.SAVED,
            message="设置已保存，当前生效配置 rev 5",
            base_revision=3,
            new_revision=5,
        )
        ctrl = _MismatchFakeCtrl(current, result)
        d = _RecordDecider("save")
        win = MainWindow(fake_snapshot(), settings_save_controller=ctrl, settings_leave_decider=d)
        win.show()
        QApplication.processEvents()
        win.navigate(PAGE05)
        _set_url(win.settings_page)
        assert win.settings_page.dirty is True
        win.navigate(PAGE02)
        QApplication.processEvents()
        assert win.current_page == PAGE05
        assert win.settings_page.dirty is True
        assert win.settings_page.base_revision == 3
        assert "状态核验异常" in win.settings_page._save_feedback.text()

    def test_unknown_decision_safety_stays(self, qapp):
        d = _RecordDecider("接龙")
        win = MainWindow(fake_snapshot(), settings_leave_decider=d)
        win.show()
        QApplication.processEvents()
        win.navigate(PAGE05)
        _set_url(win.settings_page)
        win.navigate(PAGE02)
        QApplication.processEvents()
        assert win.current_page == PAGE05
        assert win.settings_page.dirty is True
        assert win.settings_page.build_draft()["openchamber"]["url"] == EDIT_URL


# ================================================================ 真实 GUI 入口


class TestRealEntry:
    def test_navbar_button_click_stay_restores(self, qapp):
        d = _RecordDecider("stay")
        win = MainWindow(fake_snapshot(), settings_leave_decider=d)
        win.show()
        win.activateWindow()
        QApplication.processEvents()
        win.navigate(PAGE05)
        _set_url(win.settings_page)
        win.navbar.button_for(PAGE01).click()
        QApplication.processEvents()
        assert win.current_page == PAGE05
        assert win.navbar.current == PAGE05
        assert d.calls >= 1

    def test_menu_action_trigger_stay_restores(self, qapp):
        d = _RecordDecider("stay")
        win = MainWindow(fake_snapshot(), settings_leave_decider=d)
        win.show()
        QApplication.processEvents()
        win.navigate(PAGE05)
        _set_url(win.settings_page)
        win._nav_actions[0].trigger()  # PAGE01 action
        QApplication.processEvents()
        assert win.current_page == PAGE05
        checked = _menu_checked(win)
        assert checked[PAGE05] is True
        assert win.navbar.current == PAGE05
        assert d.calls >= 1


# ================================================================ 无业务副作用


class TestNoSideEffects:
    def test_stay_and_discard_do_not_touch_snapshot(self, qapp):
        snap = fake_snapshot(config_revision="7")
        d = _RecordDecider("stay", "discard")
        win = MainWindow(snap, settings_leave_decider=d)
        win.show()
        QApplication.processEvents()
        win.navigate(PAGE05)
        _set_url(win.settings_page)
        before = win.snapshot
        active_before = win.snapshot.active_task
        win.navigate(PAGE01)
        QApplication.processEvents()
        assert win.current_page == PAGE05
        win.navigate(PAGE02)
        QApplication.processEvents()
        assert win.current_page == PAGE02
        assert win.snapshot is before
        assert win.snapshot.active_task is active_before
        assert win.snapshot.active_task.config_revision == "7"