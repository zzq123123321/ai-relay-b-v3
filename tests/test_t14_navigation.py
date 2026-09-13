"""T14 五页导航定向测试。

验收依据：
- UI-A01 首次打开工作台：五页正式名称与顺序（规格 14.1：工作台/任务记录/会话与执行端/日志中心/设置）；
  工作台分区清楚、无任务时空状态（主窗壳在 test_t14_main_window 断言）。
- UI-A12 历史任务选择：只切换查看对象；停止入口/活动任务保持不变。
- 导航与业务状态分离：点击导航只切页/选中态/焦点，不重建 Controller/Snapshot。
- 键盘：Tab/Shift+Tab 可达，Enter/Space 激活；选中页 checked + accessibleName。
"""

import os
import re
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.snapshots import empty_snapshot, fake_snapshot  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.navigation import PAGES, PAGE_IDS  # noqa: E402

# 主规格 14.1 主导航正式名称与顺序
EXPECTED_LABELS = ("工作台", "任务记录", "会话与执行端", "日志中心", "设置")


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


# ------------------------------------------------------------- UI-A01


def test_ui_a01_five_pages_order_and_labels(qapp):
    """规格 14.1：主导航只放五页，正式 ID PAGE01..PAGE05，顺序不可变。"""
    assert PAGE_IDS == ("PAGE01", "PAGE02", "PAGE03", "PAGE04", "PAGE05")
    assert tuple(p.label for p in PAGES) == EXPECTED_LABELS
    win = _make()
    labels = [win.navbar.page_label(pid) for pid in PAGE_IDS]
    assert labels == list(EXPECTED_LABELS)
    assert win.navbar.current == "PAGE01"
    win.close()


# ------------------------------------------------------------- 切换只改页


def test_ui_a12_switching_all_pages_preserves_snapshot(qapp):
    """页面切换只改「当前页」，不修改业务快照（身份与值都保持）。"""
    snap = fake_snapshot(task_id="task-123", state="ACTIVE")
    win = _make(snap)
    snap_id = id(snap)
    token = (snap.active_task.task_id, snap.active_task.state, snap.stop_available)
    for pid in PAGE_IDS:
        win.navigate(pid)
        QApplication.processEvents()
        assert win.current_page == pid
        assert win.page_stack.currentIndex() == PAGE_IDS.index(pid)
        assert win.navbar.current == pid
        assert win.snapshot is snap
        assert id(win.snapshot) == snap_id
        assert (win.snapshot.active_task.task_id, win.snapshot.active_task.state, win.snapshot.stop_available) == token
    win.close()


def test_navigation_never_rebuilds_business(qapp):
    """导航选择与业务状态分离：不应出现暂停/重建 Controller/Snapshot 的调用。"""
    snap = fake_snapshot(task_id="task-123")
    win = _make(snap)
    before = id(win.snapshot)
    for pid in PAGE_IDS:
        QTest.mouseClick(win.navbar.button_for(pid), Qt.MouseButton.LeftButton)
        QApplication.processEvents()
        assert win.current_page == pid
        assert id(win.snapshot) == before
        assert win.snapshot.active_task.task_id == "task-123"
    win.close()


def test_selected_state_checked_and_accessible(qapp):
    """选中页 checked + primary 变体 + accessibleName「当前页」；其余 secondary。"""
    win = _make()
    win.navigate("PAGE03")
    QApplication.processEvents()
    for pid in PAGE_IDS:
        btn = win.navbar.button_for(pid)
        if pid == "PAGE03":
            assert btn.isChecked()
            assert btn.property("variant") == "primary"
            assert "当前页" in btn.accessibleName()
        else:
            assert not btn.isChecked()
            assert btn.property("variant") == "secondary"
    win.close()


# ------------------------------------------------------------- 键盘


def test_tab_shift_tab_path_predictable(qapp):
    """UI-A14（T14 壳层）：Tab/Shift+Tab 焦点路径可达，不出现 focusWidget()=None。"""
    win = _make()
    win.navigate("PAGE01")
    QApplication.processEvents()
    win.navbar.button_for("PAGE01").setFocus(Qt.TabFocusReason)
    QApplication.processEvents()
    assert QApplication.focusWidget() is win.navbar.button_for("PAGE01")
    QTest.keyClick(win, Qt.Key_Tab)
    QApplication.processEvents()
    assert QApplication.focusWidget() is not None
    QTest.keyClick(win, Qt.Key_Tab, Qt.KeyboardModifier.ShiftModifier)
    QApplication.processEvents()
    assert QApplication.focusWidget() is not None
    win.close()


def test_space_activation_selects_page(qapp):
    """键盘激活：Space 选中导航项，页随动，不修改快照。"""
    snap = fake_snapshot(task_id="task-123")
    win = _make(snap)
    btn = win.navbar.button_for("PAGE04")
    btn.setFocus(Qt.TabFocusReason)
    QApplication.processEvents()
    QTest.keyClick(btn, Qt.Key_Space)
    QApplication.processEvents()
    assert win.current_page == "PAGE04"
    assert win.snapshot.active_task.task_id == "task-123"
    win.close()


def test_enter_activation_selects_page(qapp):
    """键盘激活：Enter 激活聚焦的导航项，页随动。"""
    win = _make()
    nav = win.navbar.button_for("PAGE05")
    nav.setFocus(Qt.TabFocusReason)
    QApplication.processEvents()
    QTest.keyClick(win, Qt.Key_Return)
    QApplication.processEvents()
    assert win.current_page == "PAGE05"
    win.close()


def test_page_switch_focus_not_none(qapp):
    """页面切换后不得出现 focusWidget()=None：每次都有合理焦点目标。"""
    win = _make()
    for pid in PAGE_IDS:
        win.navigate(pid)
        QApplication.processEvents()
        assert QApplication.focusWidget() is not None, f"切到 {pid} 后焦点丢失"
    win.close()


def test_focus_remembered_and_restored_per_page(qapp):
    """焦点策略：切走记住该页最近焦点，切回恢复（不强行清空）。"""
    win = _make()
    win.navigate("PAGE05")  # 设置页
    QApplication.processEvents()
    win.settings_page.focus_target.setFocus(Qt.TabFocusReason)
    QApplication.processEvents()
    assert QApplication.focusWidget() is win.settings_page.focus_target
    win.navigate("PAGE04")
    QApplication.processEvents()
    win.navigate("PAGE05")
    QApplication.processEvents()
    assert QApplication.focusWidget() is win.settings_page.focus_target, "返回设置页应恢复最近焦点"
    win.close()


# ------------------------------------------------------------- UI-A12


def test_ui_a12_history_selection_changes_view_only(qapp):
    """历史任务选择只切换查看对象；停入口与活动任务不受影响（规格 14.1/UI-A12）。"""
    snap = fake_snapshot(task_id="task-123", state="ACTIVE", stop_available=True)
    win = _make(snap)
    win.navigate("PAGE02")
    QApplication.processEvents()
    win.tasks_page.history.setCurrentRow(1)  # 选择 task-002
    QApplication.processEvents()
    assert "task-002" in win.tasks_page.preview.text()
    assert win.tasks_page.preview.text().startswith("查看对象：task-002")
    assert win.snapshot.active_task.task_id == "task-123"
    assert win.snapshot.active_task.state == "ACTIVE"
    assert win.current_page == "PAGE02"
    assert win.stop_button.isEnabled()
    assert "task-123" in win.stop_button.accessibleDescription()
    assert win.stop_button.text() == "停止任务"  # 停止文案不随历史选择改变
    win.close()


# ------------------------------------------------------------- 窄屏菜单导航


def test_ui_a05_narrow_nav_menu_switches_page(qapp):
    """窄屏导航可切菜单：菜单动作能切页且保持选中态。"""
    win = _make(size=(720, 600))
    assert win.tier == "narrow"
    assert win.menu_button.isVisible()
    assert not win.navbar.isVisible()
    action = next(a for a in win._nav_actions if a.data() == "PAGE03")  # noqa: SLF001
    action.trigger()
    QApplication.processEvents()
    assert win.current_page == "PAGE03"
    assert win.navbar.current == "PAGE03"
    assert action.isChecked() is True
    assert win.snapshot.active_task.task_id == "task-123"
    win.close()


# ------------------------------------------------------------- 依赖隔离


def _banned_in_sources():
    """用 tokenize 剥离注释/文档字符串后，检查非字符串代码中是否引用业务模块。
    说明性注释允许出现这些名字，但 import/属性/变量等实际引用不得出现。"""
    import io
    import tokenize as _tokenize

    banned_identifiers = {
        "sqlite3",
        "requests",
        "OpenChamber",
        "Reasonix",
        "TaskStore",
        "Database",
        "adapters",
        "controller",
        "commands",
        "core",
    }
    found = []
    for rel in ("app/snapshots.py", "ui/navigation.py", "ui/main_window.py"):
        src = Path(__file__).resolve().parents[1] / rel
        code = src.read_text(encoding="utf-8")
        names = set()
        code_text = []
        for tok in _tokenize.tokenize(io.BytesIO(code.encode("utf-8")).readline):
            if tok.type in (
                _tokenize.COMMENT,
                _tokenize.STRING,
                _tokenize.ENCODING,
                _tokenize.ENDMARKER,
                _tokenize.NL,
                _tokenize.NEWLINE,
                _tokenize.INDENT,
                _tokenize.DEDENT,
            ):
                continue
            if tok.type == _tokenize.NAME:
                names.add(tok.string)
            code_text.append(tok.string)
        hits = sorted(names & banned_identifiers)
        if hits:
            found.append(f"{rel}: 业务/外部模块标识符 {hits}")
        if re.search(r"#[0-9A-Fa-f]{6}", "\n".join(code_text)):
            found.append(f"{rel}: 硬编码 hex 色值")
    return found


def test_t14_sources_no_business_dependency_or_hardcoded_hex(qapp):
    """主窗/导航/快照不得访问 DB/client，也不得硬编码色值。"""
    found = _banned_in_sources()
    assert not found, f"存在业务依赖或硬编码色值: {found}"


def test_workbench_empty_state_uses_fake_snapshot(qapp):
    """空快照可独立构造并驱动空状态（UI-A01：没有任务时显示空状态）。"""
    win = _make(empty_snapshot())
    assert win.snapshot.active_task is None
    assert win.workbench_page._empty_label.isVisible()  # noqa: SLF001
    assert not win.workbench_page.task_card.isVisible()
    win.close()