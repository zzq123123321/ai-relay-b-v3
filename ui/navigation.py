"""T14 五页导航（规格 14.1：主导航只放工作台/任务记录/会话与执行端/日志中心/设置）。

职责边界：
- 只负责「切换页面 / 更新导航选中态 / 管理焦点」；
- 不暂停任务、不改变当前 Task、不重建 Controller / Snapshot；
- 稳定 ID 使用规格 14.1 界面 ID（PAGE01..PAGE05），业务逻辑不得依赖中文按钮文字；
- 键盘：QPushButton 可勾选 + Tab/Shift+Tab + Enter/Space 激活；
  当前页同时表达 checked/selected + accessibleName（+ 中文字形图标，不引入 icon framework）。

T14 不引入大型 icon framework，折叠图标栏使用统一单字图形（glyph）：
工作台=台、任务记录=记、会话与执行端=端、日志中心=志、设置=设。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QVBoxLayout, QWidget

from .components import BaseButton
from .theme_tokens import refresh_style


@dataclass(frozen=True)
class PageRef:
    """页面身份。page_id 为规格 14.1 界面 ID，业务逻辑只依赖它。"""

    page_id: str
    label: str
    glyph: str  # 图标栏单字图形（不引入 icon framework）


PAGES: tuple[PageRef, ...] = (
    PageRef("PAGE01", "工作台", "台"),
    PageRef("PAGE02", "任务记录", "记"),
    PageRef("PAGE03", "会话与执行端", "端"),
    PageRef("PAGE04", "日志中心", "志"),
    PageRef("PAGE05", "设置", "设"),
)
PAGE_IDS: tuple[str, ...] = tuple(p.page_id for p in PAGES)


class NavigationBar(QWidget):
    """左侧主导航。选中页以 primary 变体高亮 + checked + accessible 表达。

    只发射 page_selected(page_id)，不触碰业务状态。
    """

    page_selected = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("navigationBar")
        self._buttons: dict[str, BaseButton] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._current: str = PAGE_IDS[0]
        self._show_text = True  # 宽屏 true；中屏折叠字形栏 false（见 set_show_text）

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 12, 6, 12)
        layout.setSpacing(4)

        for ref in PAGES:
            btn = BaseButton(ref.label, variant="secondary", parent=self)
            btn.setCheckable(True)
            btn.setProperty("nav", "true")
            btn.setToolTip(ref.label)
            btn.setAccessibleName(ref.label)
            btn.setMinimumHeight(36)
            btn.clicked.connect(lambda _checked=False, pid=ref.page_id: self._on_clicked(pid))
            self._buttons[ref.page_id] = btn
            self._group.addButton(btn)
            layout.addWidget(btn)
        layout.addStretch(1)
        self.select(PAGE_IDS[0])

    # ------------------------------------------------------------- 状态

    @property
    def current(self) -> str:
        return self._current

    def select(self, page_id: str) -> None:
        """程序化选中（不发射信号）。页面切换只改选中态，不改业务状态。"""
        if page_id not in self._buttons:
            raise ValueError(f"未知页面: {page_id!r}")
        self._current = page_id
        for pid, btn in self._buttons.items():
            selected = pid == page_id
            btn.setChecked(selected)
            btn.setProperty("variant", "primary" if selected else "secondary")
            label = self._label_for(pid)
            btn.setAccessibleName(f"{label}，当前页" if selected else label)
            refresh_style(btn)

    def set_show_text(self, show: bool) -> None:
        """宽屏显示「字形+文字」、中断（940-1179）只显示字形图标栏。"""
        self._show_text = bool(show)
        for ref in PAGES:
            btn = self._buttons[ref.page_id]
            btn.setText(f"{ref.glyph} {ref.label}" if show else ref.glyph)

    @property
    def show_text(self) -> bool:
        """当前是否显示「字形+文字」；medium 折叠为字形时应为 False。"""
        return self._show_text

    def button_for(self, page_id: str) -> BaseButton:
        return self._buttons[page_id]

    def page_label(self, page_id: str) -> str:
        return self._label_for(page_id)

    # ------------------------------------------------------------- 内部

    def _label_for(self, page_id: str) -> str:
        return next((p.label for p in PAGES if p.page_id == page_id), page_id)

    def _on_clicked(self, page_id: str) -> None:
        self.select(page_id)
        self.page_selected.emit(page_id)


def nav_focus_policy_hint() -> Qt.FocusPolicy:
    """导航按钮均可 Tab 聚焦；返回 Qt 默认强焦点。"""
    return Qt.StrongFocus