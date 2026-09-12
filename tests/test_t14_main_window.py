"""T14 主窗壳定向测试。

验收依据：
- UI-A01 首次打开工作台：主任务、接收、自动续接、连接分区清楚；无任务时空状态。
- UI-A05/UI-A06（壳层部分）在此文件断言结构与固定停止，窄屏/滚动在 test_t14_responsive。
- 固定顶部停止入口：在 headerBar，不在滚动内容区内，Fake 点击仅 emit stop_requested；
  点击不改变活动任务/快照/UI 状态（本阶段不真正停止任务）。
- L06 主题复用 T13：集中 token+QSS，无第二套样式、无组件级 setStyleSheet、无硬编码色值。
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.snapshots import empty_snapshot, fake_snapshot  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.navigation import PAGE_IDS  # noqa: E402
from ui.theme_tokens import render_qss  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    app.setStyleSheet("")


def _make(snapshot=None, mode="light", size=(1280, 820)):
    win = MainWindow(snapshot or fake_snapshot(), mode=mode)
    win.resize(*size)
    win.show()
    win.activateWindow()
    QApplication.processEvents()
    return win


# ------------------------------------------------------------- 结构


def test_ui_a01_structure_stop_outside_scroll_and_partitions(qapp):
    """主窗壳结构：固定头部含停止入口；内容区可滚动独立；五页容器就位。"""
    win = _make()
    # 停止入口位于固定顶部层，不在滚动内容区内
    assert win.stop_button.parent().objectName() == "headerBar"
    assert win.body_scroll.findChild(type(win.stop_button)) is None
    # 分区清楚：当前任务卡 + 正式三卡（模型/接口连接、原会话自动续接、等待任务数量）
    wb = win.workbench_page
    assert wb.task_card.title_label is not None
    assert wb.task_card.title_label.text() == "当前任务"
    assert wb.auto_card.title_label.text() == "原会话自动续接"
    assert wb.conn_card.title_label.text() == "模型/接口连接"
    assert wb.queue_card.title_label.text() == "等待任务数量"
    # 页面容器
    assert win.page_stack.count() == 5
    assert win.current_page == "PAGE01"
    assert win.navbar.isVisible()
    win.close()


def test_default_title_and_page(qapp):
    win = _make()
    assert win.windowTitle() == "AI Relay B V3.0"
    assert win.current_page == "PAGE01"
    assert win.page_stack.currentIndex() == 0
    win.close()


# ------------------------------------------------------------- UI-A01 空状态与分区


def test_ui_a01_empty_state_no_task(qapp):
    """没有任务时显示空状态：空标签可见、主任务卡隐藏、停止不可用却有可访问原因。"""
    win = _make(empty_snapshot())
    wb = win.workbench_page
    assert wb.task_card.isVisible() is False
    assert wb._empty_label.isVisible()  # noqa: SLF001
    assert not win.stop_button.isEnabled()
    assert "当前没有可停止的活动任务" in win.stop_button.accessibleDescription()
    win.close()


def test_ui_a01_task_partitions_reflect_fake_snapshot(qapp):
    snap = fake_snapshot(task_id="task-123", title="测试标题", state="ACTIVE")
    win = _make(snap)
    wb = win.workbench_page
    assert wb.task_card.isVisible()
    assert wb._task_id.text() == "task_id：task-123"  # noqa: SLF001
    assert wb._task_title.text() == "测试标题"  # noqa: SLF001
    assert wb._state_badge.text() == "✓ ACTIVE"
    assert win.stop_button.isEnabled()
    assert "task-123" in win.stop_button.accessibleDescription()
    win.close()


def test_ui_a01_receiving_connection_partitions(qapp):
    win = _make(fake_snapshot(receiving_enabled=True, connection_healthy=True))
    assert "接收 开启" in win._recv_badge.text()  # noqa: SLF001
    assert "连接 正常" in win._conn_badge.text()  # noqa: SLF001
    win.update_snapshot(fake_snapshot(receiving_enabled=False, connection_healthy=False))
    QApplication.processEvents()
    assert "接收 已暂停" in win._recv_badge.text()  # noqa: SLF001
    assert "连接 异常/未知" in win._conn_badge.text()  # noqa: SLF001
    win.close()


# ------------------------------------------------------------- 停止入口（Fake）


def test_stop_click_emits_request_and_never_mutates_business(qapp):
    """Fake 停止：只 emit stop_requested，不修改快照/活动任务/UI 状态（本阶段不真正停止）。"""
    snap = fake_snapshot(task_id="task-123", state="ACTIVE", stop_available=True)
    win = _make(snap)
    emitted = []
    win.stop_requested.connect(emitted.append)
    win.stop_button.click()
    QApplication.processEvents()
    assert emitted == ["PAGE01"]
    assert win.snapshot.active_task.task_id == "task-123"
    assert win.snapshot.active_task.state == "ACTIVE"
    assert win.workbench_page._state_badge.text() == "✓ ACTIVE"  # noqa: SLF001 不改为 STOPPED
    assert win.stop_button.isEnabled()
    assert win.current_page == "PAGE01"
    win.close()


def test_stop_click_each_page_no_business_change(qapp):
    """在每一页点击停止都不改变活动任务与快照身份。"""
    snap = fake_snapshot(task_id="task-123")
    win = _make(snap)
    snap_id = id(win.snapshot)
    emitted = []
    win.stop_requested.connect(emitted.append)
    for pid in PAGE_IDS:
        win.navigate(pid)
        QApplication.processEvents()
        win.stop_button.click()
        QApplication.processEvents()
        assert emitted[-1] == pid
        assert id(win.snapshot) == snap_id
        assert win.snapshot.active_task.task_id == "task-123"
    win.close()


def test_stop_disabled_when_not_available(qapp):
    """规格 15.1：禁用原因可访问，不只靠悬停。"""
    win = _make(fake_snapshot(stop_available=False))
    assert not win.stop_button.isEnabled()
    assert "当前没有可停止的活动任务" in win.stop_button.accessibleDescription()
    assert "当前没有可停止的活动任务" in win.stop_button.toolTip()
    win.close()


def test_update_snapshot_replaces_data(qapp):
    """update_snapshot 换发新快照：界面跟随，对象身份由调用方决定。"""
    win = _make(fake_snapshot(task_id="task-123"))
    new_snap = fake_snapshot(task_id="task-999", title="第二个任务", state="BLOCKED")
    win.update_snapshot(new_snap)
    QApplication.processEvents()
    assert win.snapshot is new_snap
    assert win.snapshot.active_task.task_id == "task-999"
    assert "task-999" in win.workbench_page._task_id.text()  # noqa: SLF001
    assert win.workbench_page._state_badge.text() == "⚠ BLOCKED"  # noqa: SLF001
    assert "task-999" in win.stop_button.accessibleDescription()
    win.close()


# ------------------------------------------------------------- 主题复用（L06）


def test_l06_theme_reused_from_t13(qapp):
    """主窗使用 T13 集中 QSS：渲染后含 primary 按钮选择器与 token 色值，无第二套样式。"""
    win = _make(mode="light")
    qss = qapp.styleSheet()
    assert qss
    assert 'QPushButton[variant="primary"]' in qss
    assert "#0F6CBD" in qss or "0F6CBD" in qss
    assert render_qss()  # 集中模板可完整渲染
    win.close()


def test_theme_no_component_scoped_stylesheet(qapp):
    """新文件不调用组件级 setStyleSheet（只在 theme_tokens 的 apply_theme 集中设置）。"""
    for rel in ("ui/main_window.py", "ui/navigation.py"):
        src = (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")
        assert "setStyleSheet(" not in src, f"{rel} 不应组件级设置样式"
    win = _make()
    win.close()


# ------------------------------------------------------------- 页面切换保持焦点


def test_page_switch_focus_never_none_across_pages(qapp):
    win = _make()
    for pid in PAGE_IDS:
        win.navigate(pid)
        QApplication.processEvents()
        assert QApplication.focusWidget() is not None, f"{pid} 后焦点丢失"
    win.close()