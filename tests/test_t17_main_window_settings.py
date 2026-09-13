"""T17-B3-R2：MainWindow PAGE05 接入正式 SettingsPage 的集成测试。

覆盖 R2 验收点：
1. PAGE05 是正式 SettingsPage（而非 old Fake）；
2. 五页数量 / PAGE05 索引 / 导航保持正常；
3. SettingsPage 初始拿到 MainWindow 当前 ApplicationSnapshot；
4. MainWindow.update_snapshot() 会把新快照 render 给 SettingsPage；
5. 草稿修改后 update_snapshot() 不得清 dirty、不得覆盖草稿（本轮核心边界）；
6. narrow / wide 切换会驱动 SettingsPage.set_single_column()（T14 响应式不退化）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QFormLayout  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.snapshots import fake_snapshot  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.navigation import PAGE_IDS  # noqa: E402
from ui.settings_page import SettingsPage  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    app.setStyleSheet("")


def _make(snapshot=None, size=(1280, 820)):
    win = MainWindow(snapshot or fake_snapshot())
    win.resize(*size)
    win.show()
    win.activateWindow()
    QApplication.processEvents()
    return win


def _form_policy(win) -> QFormLayout.WrapPolicy:
    return win.settings_page._forms[0].rowWrapPolicy()


# ------------------------------------------------ 1. 正式 SettingsPage


def test_p05_is_formal_settings_page(qapp):
    win = _make()
    assert type(win.settings_page) is SettingsPage
    win.close()


# ------------------------------------------------ 2. 五页 / PAGE05 导航


def test_five_pages_and_p05_index_and_navigation(qapp):
    win = _make()
    assert win.page_stack.count() == 5
    assert PAGE_IDS.index("PAGE05") == 4
    assert win.page_stack.indexOf(win.settings_page) == 4
    win.navigate("PAGE05")
    QApplication.processEvents()
    assert win.current_page == "PAGE05"
    assert win.page_stack.currentWidget() is win.settings_page
    assert win.navbar.current == "PAGE05"
    win.close()


# ------------------------------------------------ 3. 初始快照传入


def test_settings_page_receives_window_snapshot(qapp):
    snap = fake_snapshot(task_id="task-777")
    win = MainWindow(snap)
    assert win.settings_page._snapshot is snap
    win.close()


# ------------------------------------------------ 4. update_snapshot 渲染


def test_update_snapshot_renders_to_settings_page(qapp):
    win = _make(fake_snapshot(task_id="task-old", title="标题A"))
    previous = win.settings_page._snapshot
    new_snap = fake_snapshot(task_id="task-new", title="标题B")
    win.update_snapshot(new_snap)
    assert win.settings_page._snapshot is new_snap
    assert previous is not new_snap
    win.close()


# ------------------------------------------------ 5. dirty / 草稿不被快照更新冲掉


def test_dirty_and_draft_survive_snapshot_update(qapp):
    win = _make(fake_snapshot(task_id="task-old"))
    page = win.settings_page
    url_widget = page._widgets["openchamber.url"]
    base_url = url_widget.text()
    edited_url = "http://10.20.30.40:9751"
    assert edited_url != base_url

    url_widget.setText(edited_url)
    QApplication.processEvents()
    assert page.dirty is True
    assert page.build_draft()["openchamber"]["url"] == edited_url
    draft_before = page.build_draft()

    win.update_snapshot(fake_snapshot(task_id="task-new", title="新任务"))
    assert page._snapshot.active_task.task_id == "task-new"
    assert page.dirty is True
    assert page.build_draft()["openchamber"]["url"] == edited_url
    assert page.build_draft() == draft_before
    win.close()


# ------------------------------------------------ 6. 响应式 set_single_column


def test_responsive_wide_wraps_off(qapp):
    win = _make(size=(1280, 820))
    assert win.tier == "wide"
    assert _form_policy(win) == QFormLayout.DontWrapRows
    win.close()


def test_responsive_narrow_wraps_on(qapp):
    win = _make(size=(1280, 820))
    assert win.tier == "wide"
    assert _form_policy(win) == QFormLayout.DontWrapRows
    win.resize(720, 600)
    QApplication.processEvents()
    assert win.tier == "narrow"
    assert _form_policy(win) == QFormLayout.WrapAllRows
    win.close()


def test_responsive_narrow_to_wide_wraps_off(qapp):
    win = _make(size=(720, 600))
    assert win.tier == "narrow"
    assert _form_policy(win) == QFormLayout.WrapAllRows
    win.resize(1280, 820)
    QApplication.processEvents()
    assert win.tier == "wide"
    assert _form_policy(win) == QFormLayout.DontWrapRows
    win.close()