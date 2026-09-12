"""T14 响应式布局定向测试。

验收依据（主规格 13.4 断点逐字采用）：
- 宽 ≥1180 = WIDE：导航≈206 宽图文条，内容右栏并行。
- 940–1179 = MEDIUM：导航折叠为 64–74 宽图标栏。
- <940 = NARROW：右栏下移进主区单列，导航可切（菜单）。
- 断点临界：940 属 MEDIUM、1179 属 MEDIUM、1180 属 WIDE、939 属 NARROW。
- UI-A05 窄窗：内容可垂直滚动、无整页横向滚动、导航折叠可用。
- UI-A06 滚到底：顶部停止仍可见、设置保存按钮可达。
- 不强制超大最小尺寸（无 setMinimumWidth(1200)/setFixedWidth/setFixedSize）。

截图（程序性证据）：artifacts/t14/{wide,medium,narrow}.png + dark_wide.png（gitignored）。
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.snapshots import fake_snapshot  # noqa: E402
from ui.main_window import (  # noqa: E402
    BREAKPOINT_MEDIUM_MIN,
    BREAKPOINT_WIDE_MIN,
    MainWindow,
    tier_for_width,
)

ARTIFACT_DIR = Path(__file__).resolve().parents[1] / "artifacts" / "t14"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    app.setStyleSheet("")


def _make(size=(1280, 820), mode="light"):
    win = MainWindow(fake_snapshot(), mode=mode)
    win.resize(*size)
    win.show()
    win.activateWindow()
    QApplication.processEvents()
    return win


def _within_window(win, widget):
    """widget 完整位于 win 可视矩形内（含 0..w-1/0..h-1）。"""
    tl = widget.mapTo(win, QPoint(0, 0))
    br = widget.mapTo(win, QPoint(widget.width() - 1, widget.height() - 1))
    return 0 <= tl.x() <= br.x() < win.width() and 0 <= tl.y() <= br.y() < win.height()


def _reachable_while_scrolling(win, widget):
    """沿纵向滚动采样：任一位姿下 widget 完整可见即视为「可达」。"""
    vbar = win.body_scroll.verticalScrollBar()
    for v in (0, vbar.maximum() // 2, vbar.maximum()):
        vbar.setValue(v)
        QApplication.processEvents()
        if _within_window(win, widget):
            return True
    return False


# ------------------------------------------------------------- 断点换算


def test_ui_a05_tier_for_width_boundaries(qapp):
    assert tier_for_width(1400) == "wide"
    assert tier_for_width(BREAKPOINT_WIDE_MIN) == "wide"          # 1180 → wide
    assert tier_for_width(BREAKPOINT_WIDE_MIN - 1) == "medium"    # 1179 → medium
    assert tier_for_width(BREAKPOINT_MEDIUM_MIN) == "medium"      # 940 → medium
    assert tier_for_width(BREAKPOINT_MEDIUM_MIN - 1) == "narrow"  # 939 → narrow
    assert tier_for_width(360) == "narrow"


# ------------------------------------------------------------- 三档布局


def test_ui_a05_wide_layout_full_text_nav(qapp):
    """WIDE：导航 206 宽图文条，内容区与导航并行。"""
    win = _make((1280, 820))
    assert win.tier == "wide"
    assert win.navbar.isVisible()
    assert win.navbar.width() == 206
    assert win.navbar.show_text is True
    assert "工作台" in win.navbar.page_label("PAGE01")
    assert not win.menu_button.isVisible()
    assert not win.workbench_page.is_single_column
    win.close()


def test_ui_a05_medium_layout_icon_bar(qapp):
    """MEDIUM：导航折叠为 72 宽图标栏，仅字形、隐藏文字。"""
    win = _make((1000, 700))
    assert win.tier == "medium"
    assert win.navbar.isVisible()
    assert win.navbar.width() == 72
    assert win.navbar.show_text is False
    btn = win.navbar.button_for("PAGE02")
    assert btn.text() == "记"  # 折叠图标栏仅字形
    assert "工作" not in btn.text() and "任务记录" not in btn.text()
    assert win.navbar.page_label("PAGE02") == "任务记录"  # 全名在系统可访问名/提示中保留
    assert not win.menu_button.isVisible()
    win.close()


def test_ui_a05_narrow_layout_single_column_menu(qapp):
    """NARROW：右栏下移主区单列、导航可切菜单、无整页横向滚动。"""
    win = _make((720, 600))
    assert win.tier == "narrow"
    assert not win.navbar.isVisible()
    assert win.menu_button.isVisible()
    assert win.workbench_page.is_single_column is True
    assert win.body_scroll.horizontalScrollBar().maximum() == 0
    win.close()


def test_no_huge_minimum_size(qapp):
    """未强制超大最小尺寸（禁止 setMinimumWidth(1200)/setFixedWidth/setFixedSize）；
    窗口可缩至窄屏使用菜单，内容无整页横向滚动。"""
    win = _make((1280, 820))
    assert win.minimumWidth() < 940, "不得用 setMinimumWidth 强制超宽（960 仅为设计目标）"
    win.resize(480, 360)
    QApplication.processEvents()
    assert win.width() < 940
    assert win.tier == "narrow"
    assert win.menu_button.isVisible()
    assert win.body_scroll.horizontalScrollBar().maximum() == 0
    win.close()


# ------------------------------------------------------------- UI-A06 固定停止 / 可达



def test_ui_a06_stop_visible_bottom_scroll_all_tiers(qapp):
    """内容滚到底后，固定顶部停止任务与导航仍可见（每档各一次）。"""
    for size in ((1280, 820), (1000, 700), (720, 600)):
        win = _make(size)
        win.navigate("PAGE04")  # 日志页最长
        QApplication.processEvents()
        vbar = win.body_scroll.verticalScrollBar()
        vbar.setValue(vbar.maximum())
        QApplication.processEvents()
        assert _within_window(win, win.stop_button), f"tier={win.tier} 停止任务不可见"
        assert _within_window(win, win.navbar.button_for("PAGE04")) or not win.navbar.isVisible()
        assert win.body_scroll.horizontalScrollBar().maximum() == 0
        win.close()


def test_ui_a06_settings_save_reachable_scrolled(qapp):
    """纵向滚动采样下，设置页保存按钮始终可达（尤其窄屏）。"""
    win = _make((720, 600))
    win.navigate("PAGE05")
    QApplication.processEvents()
    assert _reachable_while_scrolling(win, win.settings_page.save_button)
    assert win.settings_page.save_button.isVisible()
    assert win.settings_page.save_button.isEnabled()
    assert _within_window(win, win.stop_button)
    win.close()


def test_scroll_keeps_header_stop_static(qapp):
    """headerBar 不在滚动区内：任何滚动静止锚点不变。"""
    win = _make((1280, 820))
    win.navigate("PAGE04")
    QApplication.processEvents()
    before = win.header_bar.mapTo(win, QPoint(0, 0))
    vbar = win.body_scroll.verticalScrollBar()
    vbar.setValue(vbar.maximum())
    QApplication.processEvents()
    after = win.header_bar.mapTo(win, QPoint(0, 0))
    assert before == after
    win.close()


# ------------------------------------------------------------- 截图（程序性证据）


def test_visual_smoke_screenshots_all_tiers(qapp):
    """截三档布局截图并做程序性检查（面积、停止可见、导航/菜单、无横向溢出）。"""
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    cases = (
        ((1280, 820), "wide", "wide.png", True, "nav"),
        ((1000, 700), "medium", "medium.png", True, "nav"),
        ((720, 600), "narrow", "narrow.png", False, "menu"),
    )
    for size, tier, name, nav_visible, control in cases:
        win = _make(size, mode="light")
        assert win.tier == tier
        img = win.grab().toImage()
        path = ARTIFACT_DIR / name
        assert img.save(str(path)), f"截图保存失败: {path}"
        assert path.exists() and path.stat().st_size > 0
        assert img.width() == size[0] and img.height() == size[1]
        assert img.width() > 0 and img.height() > 0
        assert _within_window(win, win.stop_button)
        assert win.navbar.isVisible() == nav_visible
        control_widget = win.navbar if control == "nav" else win.menu_button
        assert control_widget.isVisible()
        assert win.body_scroll.horizontalScrollBar().maximum() == 0
        win.close()


def test_theme_dark_wide_screenshot_differs_from_light(qapp):
    """深色宽屏截图：两主题像素内容不同（Dark Primary 冲突按 accepted 已知问题）。"""
    light = _make((1280, 820), mode="light")
    img_light = light.grab().toImage()
    light.close()

    dark = _make((1280, 820), mode="dark")
    img_dark = dark.grab().toImage()
    path = ARTIFACT_DIR / "dark_wide.png"
    assert img_dark.save(str(path)), f"深色截图保存失败: {path}"
    assert path.exists() and path.stat().st_size > 0
    dark.close()

    assert img_light.size() == img_dark.size()
    a = img_light.constBits()
    b = img_dark.constBits()
    diff = 0
    for x in range(img_light.width()):
        for y in range(img_light.height()):
            if img_light.pixel(x, y) != img_dark.pixel(x, y):
                diff += 1
    assert diff > 0, "深色与浅色截图应可见不同"


def test_screenshots_recorded_evidence(qapp):
    """四种截图均已落盘且非空（程序性证据，非人工视觉验收）。"""
    assert (ARTIFACT_DIR / "wide.png").stat().st_size > 0
    assert (ARTIFACT_DIR / "medium.png").stat().st_size > 0
    assert (ARTIFACT_DIR / "narrow.png").stat().st_size > 0
    assert (ARTIFACT_DIR / "dark_wide.png").stat().st_size > 0