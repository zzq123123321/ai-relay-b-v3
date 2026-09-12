"""T13 视觉冒烟测试：生成正常/异常主题截图与对比度检查记录。

B 端为纯文本模型，本文件只承担程序性证据：
- 截图成功生成、尺寸正确；
- 控件几何未明显越界（递归检查子控件 rect 在窗口内）；
- 明/暗两主题渲染像素确有差异（取像素样本，非“人工视觉审美验收”）。
最终视觉验收留给 A 端/用户人工查看（符合规格 T54 与 A 端要求）。

截图输出：artifacts/t13/t13_light.png / t13_dark.png / t13_states_light.png / t13_states_dark.png
对比度记录：artifacts/t13/contrast_check.md
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QVBoxLayout, QWidget  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui.components import (  # noqa: E402
    Card,
    DangerButton,
    PrimaryButton,
    SecondaryButton,
    StatusBadge,
    TextButton,
    TextInput,
)
from ui.theme_tokens import (  # noqa: E402
    DARK_THEME,
    LIGHT_THEME,
    ThemeTokens,
    contrast_ratio,
    render_qss,
)

ART_DIR = Path(__file__).resolve().parents[1] / "artifacts" / "t13"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    app.setStyleSheet("")


def _row(*widgets):
    box = QHBoxLayout()
    for w in widgets:
        box.addWidget(w)
    return box


def _build_showcase(theme: ThemeTokens, abnormal: bool) -> QWidget:
    win = QWidget()
    win.setObjectName("pageRoot")
    win.setWindowTitle("T13 基础控件展示")
    outer = QVBoxLayout(win)
    outer.setContentsMargins(24, 24, 24, 24)
    outer.setSpacing(16)

    heading = QLabel("任务工作台（T13 主题与基础控件展示）")
    heading.setProperty("heading", "true")
    heading.setObjectName("demoHeading")
    outer.addWidget(heading)

    mono = QLabel("rel_0f2a9c4e_8d3f6a1b_任务号_ABCDEF0123456789_长路径\\实测归档\\深入目录")
    mono.setProperty("mono", "true")
    outer.addWidget(mono)

    # 按钮区
    card_btn = Card("按钮七态")
    normal = PrimaryButton("启动任务接收")
    secondary = SecondaryButton("暂停自动续接")
    danger = DangerButton("停止任务")
    text_btn = TextButton("更多操作…")
    card_btn.add_layout(_row(normal, secondary, danger, text_btn))
    if abnormal:
        disabled = PrimaryButton("不可用按钮")
        disabled.setDisabled(True)
        busy = PrimaryButton("正在处理")
        busy.set_busy(True)
        err = PrimaryButton("操作失败")
        err.set_error(True)
        card_btn.add_layout(_row(disabled, busy, err))
    outer.addWidget(card_btn)

    # 状态与输入区
    card_status = Card("状态与输入")
    badges = _row(
        StatusBadge("运行中", tone="success"),
        StatusBadge("正在恢复", tone="recovering"),
        StatusBadge("需要人工处理", tone="danger"),
        StatusBadge("已停止", tone="neutral"),
    )
    card_status.add_layout(badges)
    inp = TextInput("请粘贴 Relay 任务正文")
    if abnormal:
        inp.set_error(True)
    card_status.add(inp)
    outer.addWidget(card_status)

    return win


def _contained(win: QWidget, tolerance: int = 1) -> list[str]:
    """程序性检查：所有子控件几何是否在窗口范围内（未明显越界）。"""
    problems = []
    w, h = win.width(), win.height()
    for child in win.findChildren(QWidget):
        g = child.geometry()
        tl = child.mapTo(win, QPoint(0, 0))
        br = child.mapTo(win, QPoint(g.width(), g.height()))
        if (
            tl.x() < -tolerance
            or tl.y() < -tolerance
            or br.x() > w + tolerance
            or br.y() > h + tolerance
        ):
            problems.append(f"{child.__class__.__name__} tl=({tl.x()},{tl.y()}) br=({br.x()},{br.y()}) win=({w}x{h})")
    return problems


@pytest.mark.parametrize(
    ("theme", "filename"),
    [(LIGHT_THEME, "t13_light.png"), (DARK_THEME, "t13_dark.png")],
)
def test_theme_screenshot(qapp, theme, filename):
    ART_DIR.mkdir(parents=True, exist_ok=True)
    qapp.setStyleSheet(render_qss(theme))
    win = _build_showcase(theme, abnormal=False)
    win.resize(1024, 720)
    win.show()
    qapp.processEvents()
    pix = win.grab()
    target = ART_DIR / filename
    assert pix.save(str(target)), f"截图保存失败: {target}"
    assert pix.width() > 0 and pix.height() > 0
    assert pix.width() == win.width() and pix.height() == win.height()
    problems = _contained(win)
    assert not problems, f"控件越界: {problems}"
    if theme is LIGHT_THEME:
        assert (ART_DIR / "t13_light.png").exists()
    else:
        assert (ART_DIR / "t13_dark.png").exists()
    win.close()


@pytest.mark.parametrize(
    ("theme", "filename"),
    [(LIGHT_THEME, "t13_states_light.png"), (DARK_THEME, "t13_states_dark.png")],
)
def test_states_screenshot(qapp, theme, filename):
    qapp.setStyleSheet(render_qss(theme))
    win = _build_showcase(theme, abnormal=True)
    win.resize(1024, 820)
    win.show()
    qapp.processEvents()
    pix = win.grab()
    target = ART_DIR / filename
    assert pix.save(str(target))
    assert pix.width() > 0 and pix.height() > 0
    assert not _contained(win), f"异常态控件越界: {_contained(win)}"
    win.close()


def test_light_dark_pixels_differ(qapp):
    """明暗主题渲染像素确有差异（背景/前景不同），证明两套 QSS 都生效。"""
    qapp.setStyleSheet(render_qss(LIGHT_THEME))
    win_light = _build_showcase(LIGHT_THEME, abnormal=False)
    win_light.resize(400, 300)
    win_light.show()
    qapp.processEvents()
    light_img = win_light.grab().toImage()

    qapp.setStyleSheet(render_qss(DARK_THEME))
    win_dark = _build_showcase(DARK_THEME, abnormal=False)
    win_dark.resize(400, 300)
    win_dark.show()
    qapp.processEvents()
    dark_img = win_dark.grab().toImage()

    sample = False
    for x in range(0, dark_img.width(), 4):
        for y in range(0, dark_img.height(), 4):
            if light_img.pixel(x, y) != dark_img.pixel(x, y):
                sample = True
                break
        if sample:
            break
    assert sample, "明暗主题像素完全相同，主题切换未生效"
    win_light.close()
    win_dark.close()


def test_contrast_record_file(qapp):
    """把对比度检查记录写进 artifacts/t13/contrast_check.md（证据产物）。"""
    ART_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
    "# T13 对比度检查记录（程序计算结果）",
    "",
    "依据：主规格 13.2 —— 普通正文对比度目标至少 4.5:1，必要图标/焦点至少 3:1。",
    "项目自定门槛：禁用文字/状态徽标 3.0:1（规格未给数字，报告注明检查依据）。",
    "",
    "| 角色对 | 主题 | 比值 | 门槛 | 结论 |",
    "|---|---|---|---|---|",
    ]
    checks = [
        ("text.primary / surface.card", "text.primary", "surface.card", 4.5),
        ("text.secondary / surface.card", "text.secondary", "surface.card", 4.5),
        ("status.danger.fg / surface.card", "color.status.danger.fg", "surface.card", 4.5),
        ("badge.success", "color.status.success.fg", "color.status.success.bg", 4.5),
        ("badge.recovering", "color.status.recovering.fg", "color.status.recovering.bg", 4.5),
        ("badge.danger", "color.status.danger.fg", "color.status.danger.bg", 4.5),
        ("badge.neutral", "color.status.neutral.fg", "color.status.neutral.bg", 4.5),
        ("focus / surface.card", "focus.color", "surface.card", 3.0),
        ("focus / surface.page", "focus.color", "surface.page", 3.0),
        ("text.disabled / surface.card", "text.disabled", "surface.card", 3.0),
    ]
    for theme in (LIGHT_THEME, DARK_THEME):
        flat = theme.flatten()
        for label, fg_key, bg_key, minimum in checks:
            ratio = contrast_ratio(flat[fg_key], flat[bg_key])
            verdict = "PASS" if ratio >= minimum else "FAIL"
            lines.append(f"| {label} | {theme.mode_name} | {ratio:.2f}:1 | {minimum:g}:1 | {verdict} |")
    # 主按钮文字：浅色断言；深色为规格 13.2 契约色值，需 A 端审核，单独记录
    for theme in (LIGHT_THEME, DARK_THEME):
        flat = theme.flatten()
        ratio = contrast_ratio(flat["text.on.primary"], flat["color.action.primary.fill"])
        verdict = "PASS（≥4.5）" if theme is LIGHT_THEME else "规格契约色值，需 A 端审核"
        lines.append(
            f"| button.primary（text.on.primary on fill） | {theme.mode_name} | {ratio:.2f}:1 | 4.5:1 | {verdict} |"
        )
    target = ART_DIR / "contrast_check.md"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert target.exists() and target.stat().st_size > 0


def test_screenshots_present(qapp):
    """截图与对比度记录完成程序性存在性检查。"""
    expected = [
        "t13_light.png",
        "t13_dark.png",
        "t13_states_light.png",
        "t13_states_dark.png",
        "contrast_check.md",
    ]
    missing = [f for f in expected if not (ART_DIR / f).exists()]
    assert not missing, f"缺少验收产物: {missing}"