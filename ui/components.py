"""T13 基础控件。

只做统一基础组件：按钮（含正式七态）、状态徽标、卡片、输入框。
全部走 theme_tokens + theme.qss 一套视觉语言，不在本文件散落色值/
样式字符串。动态状态切换用 Qt 动态属性 + unoblish/polish，
不通过重复 setStyleSheet 临时拼接。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QLineEdit, QPushButton, QVBoxLayout

from .theme_tokens import refresh_style


# ============================== 按钮 ================================


class BaseButton(QPushButton):
    """统一按钮基类，variant ∈ primary/secondary/danger/text。

    正式七态：default、hover、pressed、focus 由 QSS 伪状态实现；
    disabled、loading(busy)、error 由本类属性切换。
    """

    def __init__(self, text: str = "", variant: str = "primary", parent=None) -> None:
        super().__init__(text, parent)
        if variant not in ("primary", "secondary", "danger", "text"):
            raise ValueError(f"未知按钮 variant: {variant!r}")
        self.setProperty("variant", variant)
        self.setProperty("busy", "false")
        self.setProperty("error", "false")
        self._busy = False
        refresh_style(self)

    @property
    def is_busy(self) -> bool:
        return self._busy

    def set_busy(self, busy: bool) -> None:
        """loading 态。与 disabled 独立：busy 时仍可读且不丢失焦点样式。"""
        self._busy = bool(busy)
        self.setProperty("busy", "true" if self._busy else "false")
        if self._busy:
            self.setCursor(Qt.WaitCursor)
            self.setAccessibleName(f"{self.text()}，处理中")
        else:
            self.unsetCursor()
            self.setAccessibleName(self.text())
        refresh_style(self)

    def set_error(self, has_error: bool) -> None:
        """error 态：危险环，须搭配错误文案/可访问说明，不只靠颜色。"""
        self.setProperty("error", "true" if has_error else "false")
        if has_error:
            self.setAccessibleDescription("操作失败，请核对后重试")
        else:
            self.setAccessibleDescription("")
        refresh_style(self)

    def set_disabled_reason(self, reason: str | None) -> None:
        """禁用 + 可访问的禁用原因说明（规格 15.1：禁用原因可见说明，不只靠 hover）。"""
        self.setEnabled(reason is None)
        if reason is not None:
            self.setToolTip(reason)
            self.setAccessibleDescription(reason)
        else:
            self.setToolTip("")
            self.setAccessibleDescription("")


class PrimaryButton(BaseButton):
    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, variant="primary", parent=parent)


class SecondaryButton(BaseButton):
    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, variant="secondary", parent=parent)


class DangerButton(BaseButton):
    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, variant="danger", parent=parent)


class TextButton(BaseButton):
    """低频动作文字按钮/更多菜单。"""

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, variant="text", parent=parent)


# =========================== 状态徽标 ==============================


class StatusBadge(QLabel):
    """状态徽标：文字 + 符号 + 颜色共同表达（UI-A14 可访问性）。

    tone ∈ success / recovering / danger / neutral（规格 13.2 / 13.1：
    恢复琥珀色、停止中性灰、危险红）。
    """

    SYMBOLS = {
        "success": "\u2713",       # ✓
        "recovering": "\u26a0",    # ⚠
        "danger": "\u2715",        # ✕
        "neutral": "\u25cf",       # ●
    }
    TONES = tuple(SYMBOLS)

    def __init__(self, text: str = "", tone: str = "neutral", parent=None) -> None:
        super().__init__(parent)
        if tone not in self.TONES:
            raise ValueError(f"未知 tone: {tone!r}")
        self._text = text
        self.setProperty("tone", tone)
        self.setWordWrap(True)
        refresh_style(self)
        self._render()

    def set_tone(self, tone: str) -> None:
        if tone not in self.TONES:
            raise ValueError(f"未知 tone: {tone!r}")
        self.setProperty("tone", tone)
        refresh_style(self)
        self._render()

    def set_value(self, text: str) -> None:
        self._text = text
        self._render()

    def _render(self) -> None:
        symbol = self.SYMBOLS[str(self.property("tone"))]
        self.setText(f"{symbol} {self._text}" if self._text else symbol)
        self.setAccessibleName(self._text or "状态")


# ============================= 卡片 ================================


class Card(QFrame):
    """统一卡片容器：背景 card、圆角、边框由 QSS 控制。"""

    def __init__(self, title: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setProperty("card", "true")
        refresh_style(self)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(20, 16, 20, 16)
        self._layout.setSpacing(12)
        self.title_label: QLabel | None = None
        if title:
            self.title_label = QLabel(title)
            self.title_label.setProperty("heading", "true")
            refresh_style(self.title_label)
            self._layout.addWidget(self.title_label)

    def add(self, widget) -> None:
        """把子控件加入卡片内容区。"""
        self._layout.addWidget(widget)

    def add_layout(self, layout) -> None:
        """把子布局加入卡片内容区。"""
        self._layout.addLayout(layout)


# ============================= 输入框 ================================


class TextInput(QLineEdit):
    """统一输入框。error 态用动态属性 input-error + 说明文字。"""

    def __init__(self, placeholder: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setProperty("input-error", "false")

    def set_error(self, has_error: bool) -> None:
        self.setProperty("input-error", "true" if has_error else "false")
        if has_error:
            self.setAccessibleDescription("输入不合法，请修正")
        else:
            self.setAccessibleDescription("")
        refresh_style(self)