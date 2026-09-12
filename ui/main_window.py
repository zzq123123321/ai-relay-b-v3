"""T14 主窗壳、导航与响应式布局。

结构（规格 13.4 / 14.1 / 14.2）：

    MainWindow
     ├─ headerBar（固定，不随内容滚动）
     │    ├─ 应用标题
     │    ├─ 窄屏导航菜单入口（可切菜单）
     │    ├─ 接收 / 连接 状态
     │    └─ 固定顶部停止入口（红描边，Fake：仅 emit stop_requested）
     └─ body
          ├─ 左导航 NavigationBar（wide：206 图文；medium：72 图标栏；narrow：隐藏改菜单）
          └─ QScrollArea（内容区，只纵向滚动，禁止整页横向滚动）
               └─ QStackedWidget（五页 Fake 页面）

原则：
- 主窗不直接访问数据库或 client；数据单向流动 DB/Controller → Snapshot → UI；
- 页面切换只改 UI 状态（当前页 + 导航选中态 + 焦点），不修改不可变 Snapshot；
- 停止入口在固定顶部层，页面滚动/宽窄变化都不影响可用性；
- 不使用巨大 minimumSize/setFixed*** 强撑最小尺寸（960×640 仅为设计目标）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.snapshots import ApplicationSnapshot, empty_snapshot, fake_snapshot
from .components import (
    Card,
    DangerButton,
    PrimaryButton,
    SecondaryButton,
    StatusBadge,
    TextButton,
    TextInput,
)
from .dashboard import Dashboard
from .navigation import NavigationBar, PAGES, PAGE_IDS
from .status_presenter import (
    connection_presentation,
    connection_short_label,
    is_superseded_update,
)
from .theme_tokens import ThemeMode, apply_theme, theme_for_mode

# ------------------------------------------------------------------ 断点
# 主规格 13.4：宽≥1180 左导航约206图标+文字；940–1179 导航折叠 64–74 图标栏；
# 宽<940 右栏移入主区、内容单列、导航可切菜单。以下阈值逐字采用规格数值。
BREAKPOINT_MEDIUM_MIN = 940
BREAKPOINT_WIDE_MIN = 1180
NAV_WIDTH_WIDE = 206
NAV_WIDTH_COMPACT = 72
TIER_WIDE = "wide"
TIER_MEDIUM = "medium"
TIER_NARROW = "narrow"

_TIER_NAMES = (TIER_WIDE, TIER_MEDIUM, TIER_NARROW)


def tier_for_width(width: int) -> str:
    """按规格 13.4 断点把窗口/内容宽度解析为三档。"""
    if width >= BREAKPOINT_WIDE_MIN:
        return TIER_WIDE
    if width >= BREAKPOINT_MEDIUM_MIN:
        return TIER_MEDIUM
    return TIER_NARROW


# ================================================================ Fake 页面


class _TasksPage(QWidget):
    """PAGE02 任务记录（Fake）。历史查看只切查看对象，不切活动任务（UI-A12）。"""

    HISTORY = ("task-001", "task-002", "task-003")

    def __init__(self, snapshot: ApplicationSnapshot) -> None:
        super().__init__()
        self._active_tid: str | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 24)
        outer.setSpacing(16)

        heading = QLabel("任务记录")
        heading.setProperty("heading", "true")
        outer.addWidget(heading)

        self._active_label = QLabel()
        self._active_label.setWordWrap(True)
        outer.addWidget(self._active_label)

        self.search = TextInput("搜索任务（Fake，T16 接分页表格）")
        self.focus_target = self.search
        outer.addWidget(self.search)

        card = Card("历史任务（Fake）")
        self.history = QListWidget()
        self.history.setAccessibleName("历史任务列表（Fake）")
        for tid in self.HISTORY:
            self.history.addItem(tid)
        self.history.currentItemChanged.connect(self._on_history_selected)
        self.preview = QLabel("查看对象：—（仅切换查看对象，不影响活动任务）")
        self.preview.setWordWrap(True)
        card.add(self.history)
        card.add(self.preview)
        outer.addWidget(card)
        outer.addStretch(1)

        self.render(snapshot)

    def render(self, snapshot: ApplicationSnapshot) -> None:
        active = snapshot.active_task
        self._active_tid = active.task_id if active else None
        self._active_label.setText(
            f"当前活动任务：{self._active_tid or '（无）'}｜状态：{active.state if active else '—'}"
        )
        self.preview.setText(f"查看对象：—（仅切换查看对象，不影响活动任务 {self._active_tid or '无'}）")

    def _on_history_selected(self, current, _previous) -> None:
        if current is None:
            return
        self.preview.setText(
            f"查看对象：{current.text()}（仅切换查看对象，不影响活动任务 {self._active_tid or '无'}）"
        )


class _SimplePage(QWidget):
    """通用占位页（PAGE03 等）。"""

    def __init__(self, title: str, note: str, button_text: str = "刷新占位") -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 24)
        outer.setSpacing(16)
        heading = QLabel(title)
        heading.setProperty("heading", "true")
        outer.addWidget(heading)
        card = Card(title)
        label = QLabel(note)
        label.setWordWrap(True)
        card.add(label)
        outer.addWidget(card)
        outer.addStretch(1)
        self.refresh_button = SecondaryButton(button_text)
        self.focus_target = self.refresh_button
        outer.addWidget(self.refresh_button)


class _LogsPage(QWidget):
    """PAGE04 日志中心（Fake）：长内容用于滚动测试（UI-A06）。"""

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 24)
        outer.setSpacing(16)
        heading = QLabel("日志中心")
        heading.setProperty("heading", "true")
        outer.addWidget(heading)

        self.filter = TextInput("筛选日志（Fake，T16 分页/详情）")
        self.focus_target = self.filter
        outer.addWidget(self.filter)

        card = Card("事件时间线（Fake，长内容）")
        for i in range(50):
            row = QLabel(f"{i + 1:>3}  2026-09-12 10:00:{i % 60:02d}  EVENT_CODE_{i + 1}  第{i + 1}条中文示例日志记录，用于滚动测试")
            row.setWordWrap(True)
            card.add(row)
        follow = TextButton("暂停跟随（占位）")
        card.add(follow)
        outer.addWidget(card)
        outer.addStretch(1)


class _SettingsPage(QWidget):
    """PAGE05 设置（Fake）：底部保存可达（UI-A06）。"""

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 24)
        outer.setSpacing(16)
        heading = QLabel("设置（Fake，T17 接草稿）")
        heading.setProperty("heading", "true")
        outer.addWidget(heading)

        self.draft = TextInput("草稿名（Fake）")
        self.focus_target = self.draft
        outer.addWidget(self.draft)

        for title, note in (
            ("执行端", "OC / Reasonix 绑定与能力合同（Fake 占位）"),
            ("续接策略", "恢复许可、轮换与继承（Fake 占位）"),
            ("轮换", "目标目录与计数重置范围（Fake 占位）"),
            ("外观", "主题 / 密度 / 窗口选择（Fake 占位）"),
            ("日志诊断", "导出范围与脱敏（Fake 占位）"),
        ):
            card = Card(title)
            label = QLabel(note)
            label.setWordWrap(True)
            card.add(label)
            outer.addWidget(card)

        outer.addStretch(1)

        bottom = QWidget()
        bottom_lay = QHBoxLayout(bottom)
        bottom_lay.setContentsMargins(0, 0, 0, 0)
        bottom_lay.setSpacing(8)
        self.discard_button = SecondaryButton("放弃更改（占位）")
        self.save_button = PrimaryButton("保存设置（占位）")
        bottom_lay.addStretch(1)
        bottom_lay.addWidget(self.discard_button)
        bottom_lay.addWidget(self.save_button)
        outer.addWidget(bottom)


# ================================================================ 主窗口


class MainWindow(QMainWindow):
    """主窗壳。T14 停止入口为 Fake：点击仅 emit stop_requested，不执行任何停止。"""

    stop_requested = Signal(str)  # 参数：当前页面 ID（审计用），不做业务停止

    def __init__(
        self,
        snapshot: ApplicationSnapshot | None = None,
        mode: ThemeMode | str = ThemeMode.LIGHT,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._snapshot = snapshot or empty_snapshot()
        self._mode = mode
        self._tier: str = TIER_WIDE
        self._current_page: str = PAGE_IDS[0]
        self._page_focus: dict[str, QWidget | None] = {}
        self._widget_page: dict[QWidget, str] = {}

        self._build()
        self._refresh_header(self._snapshot)
        self.setWindowTitle("AI Relay B V3.0")
        self.resize(1280, 820)  # 规格 13.4 建议初始 1280×820，可缩放

        qapp = QApplication.instance()
        if qapp is not None:
            apply_theme(qapp, theme_for_mode(self._mode))

        self._apply_tier(tier_for_width(self.width()))
        self.navigate(PAGE_IDS[0])

    # ------------------------------------------------------------ 构建

    def _build(self) -> None:
        central = QWidget()
        central.setObjectName("pageRoot")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 顶部固定区（不在滚轮内容区内）
        header = QWidget()
        header.setObjectName("headerBar")
        self.header_bar = header
        header_lay = QHBoxLayout(header)
        header_lay.setContentsMargins(16, 8, 12, 8)
        header_lay.setSpacing(8)

        self._title_label = QLabel("AI Relay B V3.0")
        header_lay.addWidget(self._title_label)

        self._nav_menu = QMenu(self)
        self._nav_actions: list = []
        for ref in PAGES:
            action = self._nav_menu.addAction(ref.label)
            action.setData(ref.page_id)
            action.setCheckable(True)
            action.triggered.connect(lambda _checked=False, pid=ref.page_id: self.navigate(pid))
            self._nav_actions.append(action)
        self.menu_button = QPushButton("菜单 ▾")
        self.menu_button.setAccessibleName("导航菜单（五页）")
        self.menu_button.setMenu(self._nav_menu)
        self.menu_button.setVisible(False)  # 仅窄屏
        header_lay.addWidget(self.menu_button)

        header_lay.addStretch(1)

        self._recv_badge = StatusBadge("接收 开启", tone="success")
        self._conn_badge = StatusBadge("连接 正常", tone="success")
        header_lay.addWidget(self._recv_badge)
        header_lay.addWidget(self._conn_badge)

        self.stop_button = DangerButton("停止任务")
        self.stop_button.setAccessibleName("停止当前任务")
        self.stop_button.clicked.connect(self._on_stop_clicked)
        header_lay.addWidget(self.stop_button)

        root.addWidget(header)

        # ---- 主体：左导航 + 滚动内容区
        body = QWidget()
        body_lay = QHBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)

        self.navbar = NavigationBar()
        self.navbar.page_selected.connect(self.navigate)
        body_lay.addWidget(self.navbar)

        self.body_scroll = QScrollArea()
        self.body_scroll.setObjectName("pageScroll")
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # 禁止整页横向滚动
        self.body_scroll.viewport().setObjectName("pageRoot")

        self.page_stack = QStackedWidget()
        self.workbench_page = Dashboard(self._snapshot)
        self.tasks_page = _TasksPage(self._snapshot)
        self.sessions_page = _SimplePage("会话与执行端", "OC 与 Reasonix 分卡、自检、监控（T15+ 实现）")
        self.logs_page = _LogsPage()
        self.settings_page = _SettingsPage()
        for page in (
            self.workbench_page,
            self.tasks_page,
            self.sessions_page,
            self.logs_page,
            self.settings_page,
        ):
            self.page_stack.addWidget(page)
            self._widget_page[page] = PAGE_IDS[len(self._widget_page)]

        self._page_index = {page_id: i for i, page_id in enumerate(PAGE_IDS)}
        self.body_scroll.setWidget(self.page_stack)
        body_lay.addWidget(self.body_scroll, stretch=1)
        root.addWidget(body, stretch=1)

        self._refresh_stop()

    # ------------------------------------------------------------ 对外状态

    @property
    def snapshot(self) -> ApplicationSnapshot:
        return self._snapshot

    @property
    def current_page(self) -> str:
        return self._current_page

    @property
    def tier(self) -> str:
        return self._tier

    @property
    def tier_name(self) -> str:
        return self._tier

    # ------------------------------------------------------------ 导航

    def navigate(self, page_id: str) -> None:
        """页面切换：只改「当前页 + 导航选中态 + 焦点」，不改业务 Snapshot。"""
        if page_id not in self._page_index:
            raise ValueError(f"未知页面: {page_id!r}")
        old = self.page_stack.currentWidget()
        if old is not None:
            focus_widget = old.focusWidget()
            if focus_widget is not None and focus_widget.isVisible():
                self._page_focus[self._widget_page[old]] = focus_widget
        self.page_stack.setCurrentIndex(self._page_index[page_id])
        self._current_page = page_id
        # 切页回到滚动区顶部：跨页滚动位置不残留（否则可能带出当前页全部内容）
        self.body_scroll.verticalScrollBar().setValue(0)
        self.navbar.select(page_id)
        for action in self._nav_actions:
            action.setChecked(action.data() == page_id)
        self._restore_focus(self.page_stack.currentWidget())

    def _restore_focus(self, page: QWidget) -> None:
        """焦点策略：恢复该页最近焦点；否则落到该页焦点目标；绝不出现 focusWidget()=None。"""
        pid = self._widget_page[page]
        previous = self._page_focus.get(pid)
        target: QWidget | None = None
        if previous is not None and previous.isVisible() and previous.isEnabled():
            target = previous
        elif getattr(page, "focus_target", None) is not None:
            candidate = page.focus_target
            if candidate.isEnabled():
                target = candidate
        if target is not None:
            target.setFocus(Qt.TabFocusReason)
        else:
            self.navbar.setFocus(Qt.TabFocusReason)

    # ------------------------------------------------------------ 快照更新

    def update_snapshot(self, snapshot: ApplicationSnapshot) -> None:
        """换发新快照并刷新界面（不改对象身份，只换新值）。

        迟到/过期业务快照在入口被身份守卫拦截（T15-B2 接缝）：当候选快照与
        当前快照都能提供持久 sequence、且判定为已过期时，直接拒绝，保持
        当前 Dashboard 与 self._snapshot 均为最新任务。
        """
        if self._clearly_superseded(snapshot):
            return
        self._snapshot = snapshot
        self.workbench_page.render(snapshot)
        self.tasks_page.render(snapshot)
        self._refresh_header(snapshot)
        self._refresh_stop()

    def _clearly_superseded(self, candidate: ApplicationSnapshot) -> bool:
        """Snapshot 级 gate：仅在双方都有权威 sequence 时判定过期。

        legacy（无 sequence）快照沿用 T14 更新契约直接接受，避免破坏
        旧调用；身份算法本身全部复用 presenter.is_superseded_update。
        """
        cand = candidate.active_task
        curr = self._snapshot.active_task
        if cand is None or curr is None:
            return False
        if cand.sequence is None or curr.sequence is None:
            return False
        return is_superseded_update(candidate, self._snapshot)

    def _refresh_header(self, snapshot: ApplicationSnapshot) -> None:
        self._recv_badge.set_tone("success" if snapshot.receiving_enabled else "neutral")
        self._recv_badge.set_value("接收 开启" if snapshot.receiving_enabled else "接收 已暂停")
        conn = connection_presentation(snapshot)
        self._conn_badge.set_tone(conn.tone)
        self._conn_badge.set_value(connection_short_label(conn.headline))

    # ------------------------------------------------------------ 停止入口

    def _refresh_stop(self) -> None:
        active = self._snapshot.active_task
        if self._snapshot.stop_available and active is not None:
            self.stop_button.set_disabled_reason(None)
            target = f"作用于当前活动任务：{active.task_id}"
            self.stop_button.setToolTip(target)
            self.stop_button.setAccessibleDescription(target)
        else:
            self.stop_button.set_disabled_reason("当前没有可停止的活动任务")

    def _on_stop_clicked(self) -> None:
        """Fake 停止：仅广播请求，绝不修改任务/快照/UI 状态。真正命令接线在后续任务。"""
        if not self._snapshot.stop_available:
            return
        self.stop_requested.emit(self.current_page)

    def keyPressEvent(self, event) -> None:
        """键盘导航：焦点在导航项时按 Enter/Return 激活该项（Tab/Shift+Tab 由 QPushButton 原生支持）。"""
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            focus = self.focusWidget()
            if focus is not None and isinstance(focus, QPushButton):
                for ref in PAGES:
                    if self.navbar.button_for(ref.page_id) is focus:
                        focus.click()
                        event.accept()
                        return
        super().keyPressEvent(event)

    # ------------------------------------------------------------ 响应式

    def _apply_tier(self, tier: str) -> None:
        self._tier = tier
        if tier == TIER_WIDE:
            self.navbar.setVisible(True)
            self.navbar.set_show_text(True)
            self.navbar.setFixedWidth(NAV_WIDTH_WIDE)
            self.menu_button.setVisible(False)
        elif tier == TIER_MEDIUM:
            self.navbar.setVisible(True)
            self.navbar.set_show_text(False)
            self.navbar.setFixedWidth(NAV_WIDTH_COMPACT)
            self.menu_button.setVisible(False)
        else:  # narrow：导航切菜单，内容单列
            self.navbar.setVisible(False)
            self.menu_button.setVisible(True)
        self.workbench_page.set_single_column(tier == TIER_NARROW)
        self.navbar.select(self.current_page)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        new_tier = tier_for_width(self.width())
        if new_tier != self._tier:
            self._apply_tier(new_tier)