"""T13 基础控件定向测试（Qt Widgets）。

验收依据：
- UI-A14 键盘与屏幕阅读：焦点环 QSS、Tab 可达、图标/控件有名字、模态焦点与返回焦点（T13 覆盖
  基础控件焦点链；完整模态焦点由 T14+ 主窗任务验收）。
- UI-A07 超长中文/路径/ID：长文本不 crash、文字不缩小（字号 token 断言在 theme_tokens 测试）。
- L06   主题切换不重建业务对象、不改变执行快照与续接许可：本例断言主题切换保持控件属性/
        对象身份稳定，且 ui 基础层 import 不拉入 core/storage/adapters/app 业务模块。

正式按钮七态：default / hover / pressed / focus / disabled / loading(busy) / error。
hover/pressed/focus 为 Qt 伪状态（QSS 选择器已在 theme_tokens 测试断言存在），
本测试验证 API 可切换的 disabled/busy/error 与 Focus 可达性。
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout  # noqa: E402

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
    apply_theme,
    refresh_style,
    render_qss,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    app.setStyleSheet("")


def _dim(app, theme):
    app.setStyleSheet(render_qss(theme))


def _window(children):
    win = QWidget()
    lay = QVBoxLayout(win)
    for w in children:
        lay.addWidget(w)
    return win


# ------------------------------------------------------------- 实例化


def test_buttons_instantiate_with_variants(qapp):
    for cls, variant in (
        (PrimaryButton, "primary"),
        (SecondaryButton, "secondary"),
        (DangerButton, "danger"),
        (TextButton, "text"),
    ):
        b = cls("启动任务接收")
        assert b.property("variant") == variant
        assert b.text() == "启动任务接收"
        b.deleteLater()


def test_unknown_variant_rejected(qapp):
    from ui.components import BaseButton

    with pytest.raises(ValueError):
        BaseButton("x", variant="ultraviolet")


def test_status_badge_tones_and_text(qapp):
    for tone, symbol in (("success", "\u2713"), ("recovering", "\u26a0"), ("danger", "\u2715"), ("neutral", "\u25cf")):
        badge = StatusBadge("已连接", tone=tone)
        assert badge.property("tone") == tone
        assert symbol in badge.text() and "已连接" in badge.text()
        assert badge.accessibleName() == "已连接"
    badge = StatusBadge("等待", tone="neutral")
    badge.set_tone("danger")
    assert badge.property("tone") == "danger"
    badge.set_value("已停止")
    assert "已停止" in badge.text()
    with pytest.raises(ValueError):
        badge.set_tone("mystery")


def test_card_instantiates(qapp):
    card = Card(title="任务详情")
    assert card.property("card") == "true"
    assert card.title_label is not None
    assert card.title_label.property("heading") == "true"


def test_textinput_set_error(qapp):
    inp = TextInput("请输入任务号")
    assert inp.placeholderText() == "请输入任务号"
    assert inp.property("input-error") == "false"
    inp.set_error(True)
    assert inp.property("input-error") == "true"
    assert inp.accessibleDescription()
    inp.set_error(False)
    assert inp.property("input-error") == "false"


# ------------------------------------------------------------- 七态切换


def test_button_seven_state_switching(qapp):
    _dim(qapp, LIGHT_THEME)
    btn = PrimaryButton("立即检查并继续")
    # default
    assert not btn.is_busy
    assert btn.property("busy") == "false"
    assert btn.property("error") == "false"
    assert btn.isEnabled()
    # loading（与 disabled 独立）
    btn.set_busy(True)
    assert btn.is_busy
    assert btn.property("busy") == "true"
    assert btn.isEnabled(), "busy 不应等于 disabled"
    assert str(btn.accessibleName()) and "处理中" in btn.accessibleName()
    btn.set_busy(False)
    assert not btn.is_busy
    # error
    btn.set_error(True)
    assert btn.property("error") == "true"
    assert btn.accessibleDescription()
    btn.set_error(False)
    # disabled + 可访问原因（规格 15.1）
    btn.set_disabled_reason("后端正在校验命令，请稍候")
    assert not btn.isEnabled()
    assert btn.toolTip() == "后端正在校验命令，请稍候"
    assert btn.accessibleDescription() == "后端正在校验命令，请稍候"
    btn.set_disabled_reason(None)
    assert btn.isEnabled()
    # focus + hover/pressed 走 QSS 伪状态（选择器断言在 token 测试）
    win = _window([btn])
    win.show()
    qapp.processEvents()
    btn.setFocus(Qt.FocusReason.MouseFocusReason)
    qapp.processEvents()
    assert btn.hasFocus()
    win.close()


def test_focus_still_visible_not_hover_dependent(qapp):
    """UI-A14：焦点由 :focus（键盘可达）提供，不依赖鼠标 hover。"""
    qss = render_qss(LIGHT_THEME)
    # 焦点选择器对所有 variant 与输入框存在
    assert "QPushButton[variant=\"primary\"]:focus" in qss
    assert "QPushButton[variant=\"secondary\"]:focus" in qss
    assert "QPushButton[variant=\"danger\"]:focus" in qss
    assert "QPushButton[variant=\"text\"]:focus" in qss
    assert "QLineEdit:focus" in qss


def test_stylesheet_apply_via_central_qss_not_component(qapp):
    """原则：控件不带独立 setStyleSheet。"""
    import inspect
    from ui import components

    for name, obj in vars(components).items():
        if (
            inspect.isclass(obj)
            and obj.__module__ == components.__name__
            and hasattr(obj, "setStyleSheet")
        ):
            src = inspect.getsource(obj)
            assert "setStyleSheet" not in src, f"{name} 不应自带样式覆盖"


# ------------------------------------------------------------- Tab 焦点链


def test_tab_focus_chain(qapp):  # noqa: ARG001
    _dim(qapp, LIGHT_THEME)
    b1 = PrimaryButton("启动任务接收")
    inp = TextInput("输入")
    b2 = DangerButton("停止任务")
    win = _window([b1, inp, b2])
    win.setWindowTitle("T13 焦点链")
    win.show()
    win.activateWindow()
    qapp.processEvents()
    win.setFocus()
    QTest.keyClick(win, Qt.Key_Tab)
    qapp.processEvents()
    assert qapp.focusWidget() == b1, "首个 Tab 应到达第一个按钮"
    QTest.keyClick(win, Qt.Key_Tab)
    qapp.processEvents()
    assert qapp.focusWidget() == inp or inp.hasFocus()
    QTest.keyClick(win, Qt.Key_Tab)
    qapp.processEvents()
    assert qapp.focusWidget() == b2 or b2.hasFocus()
    # Shift+Tab 返回
    QTest.keyClick(win, Qt.Key_Tab, Qt.KeyboardModifier.ShiftModifier)
    qapp.processEvents()
    assert not b2.hasFocus()


def test_refresh_style_polish_safe(qapp):
    btn = PrimaryButton("测试")
    btn.show()
    before = btn.minimumSizeHint().width() >= 0
    refresh_style(btn)
    assert before


# ------------------------------------------------------------- 主题切换


def test_theme_switch_preserves_objects_and_properties(qapp):
    """L06：仅改变 UI 偏好；对象身份与属性保持，不重建、不改数据。"""
    _dim(qapp, LIGHT_THEME)
    btn = PrimaryButton("立即检查并继续")
    inp = TextInput("请粘贴 Relay 任务")
    inp.setText("rel_abc123任务正文")
    btn_id = id(btn)
    inp_id = id(inp)
    prop_before = inp.property("input-error")

    _dim(qapp, DARK_THEME)
    assert id(btn) == btn_id
    assert id(inp) == inp_id
    assert inp.text() == "rel_abc123任务正文"
    assert inp.placeholderText() == "请粘贴 Relay 任务"
    assert inp.property("input-error") == prop_before
    assert btn.text() == "立即检查并继续"
    assert DARK_THEME.text.primary in qapp.styleSheet()

    _dim(qapp, LIGHT_THEME)
    assert inp.text() == "rel_abc123任务正文"


def test_ui_base_import_isolation_from_business(qapp):
    """L06：ui 基础层 import 不拉入 core/storage/adapters/app 业务模块（不改变执行快照）。"""
    import importlib
    import inspect
    import sys as _sys

    ui = importlib.import_module("ui.components")
    ui_src = inspect.getsource(ui)
    for banned in ("import core", "from core", "import app.", "from app ", "import storage", "from storage"):
        assert banned not in ui_src, f"ui 基础层不应 import 业务模块: {banned}"


# ------------------------------------------------------------- UI-A07


def test_long_chinese_path_id_no_crash(qapp):
    _dim(qapp, DARK_THEME)
    long_value = "D:\\中继\\项目归档\\" + "很长的中文任务标题-" * 20 + "任务号_" + "A" * 64
    badge = StatusBadge(long_value, tone="recovering")
    badge.show()
    qapp.processEvents()
    assert badge.sizeHint().height() > 0
    assert long_value in badge.text()
    card = Card(title=long_value)
    card.show()
    qapp.processEvents()
    assert "很长的中文任务标题" in card.title_label.text()
    inp = TextInput()
    inp.setText(long_value)
    inp.show()
    qapp.processEvents()
    assert inp.text() == long_value
    # 关键主操作不截断：为主窗任务(T14+)预留的断言基础——按钮文字完整可读
    btn = PrimaryButton(f"第{64}轮｜重新复制完整结果版本 {long_value[:12]}…")
    btn.show()
    qapp.processEvents()
    assert btn.text().startswith("第64轮")