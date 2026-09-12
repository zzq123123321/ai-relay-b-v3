"""T16-B2 PAGE02 任务记录（正式，只读 + Provider seam）。

职责边界（规格 14.1 PAGE02 / T16-B2 卡）：
- 不创建 Database、不执行 SQL、不实例化 TaskQueries；数据一律经构造传入的
  Provider（duck-type：list_tasks/get_task_detail/list_task_result_versions/get_result）。
- 本页只维护页面本地状态：selected_task_key / selected_result_id / current_cursor /
  cursor_history / search_text / state_filter；绝不写进 ApplicationSnapshot。
- render(snapshot) 只更新「当前真实活动任务」banner；活跃快照更新不自动改变
  历史选择；只有用户改搜索/过滤/翻页/选择才会变化（UI-A12）。
- keyset 分页：首次 cursor=None；下一页 = Provider.next_cursor；上一页回溯
  cursor_history 栈；搜索/过滤变化 → cursor reset + history clear + 选择清除。
- 查询异常：页内错误条 + 「重试查询」（仅重播相同只读 query，绝不重跑任务）；
  保留已展示的旧数据与选择，禁止 modal 红框/清空旧数据/静默吞错。
- 复制只发信号 copy_value_requested(kind, full_value) / copy_result_requested(result_id)；
  实际 ClipboardSink 写剪贴板留给 T16-B3。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.snapshots import ApplicationSnapshot
from ui.components import Card, SecondaryButton, StatusBadge, TextInput, TextButton
from ui.task_detail import TaskDetailPanel
from ui.theme_tokens import refresh_style

_PAGE_SIZE = 20

STATE_FILTER_LABELS = (
    ("全部", None),
    ("QUEUED", "QUEUED"),
    ("ACTIVE", "ACTIVE"),
    ("BLOCKED", "BLOCKED"),
    ("COMPLETED", "COMPLETED"),
    ("FAILED", "FAILED"),
    ("STOPPED_BY_USER", "STOPPED_BY_USER"),
)

_TABLE_COLS = ("原 Task ID", "项目", "状态", "交付", "接收时间")


class TaskRecordsPage(QWidget):
    """PAGE02 任务记录：列表 + 详情 + exact result，只读 Provider seam。"""

    copy_value_requested = Signal(str, str)
    copy_result_requested = Signal(str)

    def __init__(self, provider, snapshot: ApplicationSnapshot | None = None) -> None:
        super().__init__()
        self._provider = provider
        self._snapshot = snapshot
        self._single = False
        self._split_in_row = True
        self._last_failed: tuple | None = None

        # ---- 页面本地状态（绝不进入 ApplicationSnapshot） ----
        self.selected_task_key: str | None = None
        self.selected_result_id: str | None = None
        self.current_cursor: int | None = None
        self.cursor_history: list[int] = []
        self.search_text: str = ""
        self.state_filter: str | None = None

        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(24, 20, 24, 24)
        self._outer.setSpacing(12)

        heading = QLabel("任务记录")
        heading.setProperty("heading", "true")
        self._outer.addWidget(heading)

        # ---- 当前真实活动任务 banner（UI-A12：只读快照驱动，不参与页面选择） ----
        self.banner = StatusBadge("当前真实活动任务：未知", tone="neutral")
        self._outer.addWidget(self.banner)

        # ---- 错误条（页内 + 重试查询，保留旧数据） ----
        self._error_bar = QWidget()
        err_lay = QHBoxLayout(self._error_bar)
        err_lay.setContentsMargins(0, 0, 0, 0)
        err_lay.setSpacing(8)
        self._error_label = QLabel()
        self._error_label.setWordWrap(True)
        self._error_label.setProperty("tone", "danger")
        refresh_style(self._error_label)
        self.retry_button = SecondaryButton("重试查询")
        self.retry_button.setAccessibleName("重试查询")
        self.retry_button.clicked.connect(self.retry_query)
        err_lay.addWidget(self._error_label, 1)
        err_lay.addWidget(self.retry_button)
        self._error_bar.hide()
        self._outer.addWidget(self._error_bar)

        # ---- 复制反馈（B3：只展示，不碰剪贴板/Outbox；tone 走主题角色） ----
        self._copy_feedback = QLabel()
        self._copy_feedback.setWordWrap(True)
        self._copy_feedback.setProperty("tone", "neutral")
        self._copy_feedback.setAccessibleName("复制反馈")
        refresh_style(self._copy_feedback)
        self._copy_feedback.hide()
        self._outer.addWidget(self._copy_feedback)

        self._split_row = QWidget()
        split = QHBoxLayout(self._split_row)
        split.setContentsMargins(0, 0, 0, 0)
        split.setSpacing(12)

        # ---- 任务记录区 ----
        self.list_card = Card("任务记录")
        self.list_card.setMinimumWidth(0)
        self.search_input = TextInput("搜索 task_id / project / result_id")
        self.search_input.setAccessibleName("搜索任务")
        self.search_input.textChanged.connect(self._on_search_changed)
        self.filter_combo = QComboBox()
        self.filter_combo.setAccessibleName("状态过滤")
        for label, _ in STATE_FILTER_LABELS:
            self.filter_combo.addItem(label)
        self.filter_combo.currentIndexChanged.connect(self._on_filter_changed)

        toolbar = QHBoxLayout()
        toolbar.addWidget(self.search_input, 1)
        toolbar.addWidget(self.filter_combo)
        self.list_card.add_layout(toolbar)

        self.tasks_table = QTableWidget(0, len(_TABLE_COLS))
        self.tasks_table.setHorizontalHeaderLabels(_TABLE_COLS)
        self.tasks_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tasks_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.tasks_table.setSelectionMode(QTableWidget.SingleSelection)
        header = self.tasks_table.horizontalHeader()
        for col in range(len(_TABLE_COLS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        header.setStretchLastSection(False)
        self.tasks_table.setSortingEnabled(False)
        self.tasks_table.cellClicked.connect(self._on_row_clicked)
        self.list_card.add(self.tasks_table)

        self._empty_label = QLabel("无匹配任务")
        self._empty_label.setWordWrap(True)
        self._empty_label.hide()
        self.list_card.add(self._empty_label)

        self.prev_button = SecondaryButton("上一页")
        self.prev_button.setAccessibleName("上一页")
        self.prev_button.clicked.connect(self.previous_page)
        self.next_button = SecondaryButton("下一页")
        self.next_button.setAccessibleName("下一页")
        self.next_button.clicked.connect(self.next_page)
        self.result_count_label = QLabel()
        self.result_count_label.setAccessibleName("当前结果数")

        pager = QHBoxLayout()
        pager.addWidget(self.prev_button)
        pager.addWidget(self.next_button)
        pager.addStretch(1)
        pager.addWidget(self.result_count_label)
        self.list_card.add_layout(pager)

        split.addWidget(self.list_card)
        split.setStretch(0, 3)

        # ---- 详情面板 ----
        self.detail_panel = TaskDetailPanel()
        self.detail_panel.copy_value_requested.connect(self.copy_value_requested)
        self.detail_panel.copy_result_requested.connect(self.copy_result_requested)
        self.detail_panel.result_selected.connect(self.select_result)
        split.addWidget(self.detail_panel)
        split.setStretch(1, 4)

        self._outer.addWidget(self._split_row, 1)
        self._outer.addStretch(1)

        self.set_single_column(False)
        if snapshot is not None:
            self.render(snapshot)
        self._load_list()

    # ------------------------------------------------------------- Provider seam

    def show_copy_feedback(self, message: str, tone: str = "neutral") -> None:
        """复制结果只读展示（success/missing/failed/unavailable → 主题 tone）。
        本页不写剪贴板、不改 Outbox；写入由上层 HistoryCopyService 完成。"""
        self._copy_feedback.setProperty("tone", tone)
        refresh_style(self._copy_feedback)
        self._copy_feedback.setText(message)
        self._copy_feedback.show()

    def _with_error(
        self,
        exc: Exception,
        kind: str,
        *,
        cursor: int | None = None,
        text: str | None = None,
        states: tuple[str, ...] | None = None,
        task_key: str | None = None,
        result_id: str | None = None,
        action: str = "",
    ) -> None:
        """冻结最近一次失败查询的原始参数，供重试时重播同 query。"""
        self._last_failed = (kind, cursor, text, states, task_key, result_id, action)
        self._error_label.setText(f"查询失败：{type(exc).__name__}: {exc}")
        self._error_bar.show()

    def _clear_error(self) -> None:
        self._error_bar.hide()
        self._last_failed = None

    def retry_query(self) -> None:
        """重播最近一次相同的只读查询（冻结 cursor/search/filter 等原参数，
        不是任务重跑、不重读控件当前值）。"""
        if self._last_failed is None:
            return
        kind, cursor, text, states, task_key, result_id, action = self._last_failed
        if kind == "list":
            page = self._fetch_page(cursor, text, states, action)
            if page is None:
                return
            if action == "list_fresh":
                self._commit_fresh(page)
            elif action == "list_next":
                self.cursor_history.append(self.current_cursor)
                self.current_cursor = cursor
                self._render_page(page)
            else:  # list_prev
                self.cursor_history.pop()
                self.current_cursor = cursor
                self._render_page(page)
        elif kind == "detail":
            self.load_task_detail(task_key)
        elif kind == "result":
            self.select_result(result_id)

    # ------------------------------------------------------------- 列表与分页

    def _filters(self) -> tuple[str | None, tuple[str, ...] | None]:
        text = self.search_input.text().strip()
        state = None
        for label, value in STATE_FILTER_LABELS:
            if label == self.filter_combo.currentText():
                state = value
                break
        return (text or None), ((state,) if state else None)

    def _load_list(self) -> None:
        text, states = self._filters()
        page = self._fetch_page(None, text, states, "list_fresh")
        if page is None:
            return
        self._commit_fresh(page)

    def _fetch_page(self, cursor: int | None, text: str | None,
                    states: tuple[str, ...] | None, action: str):
        """查询目标页；失败返回 None 并冻结失败参数，成功才 commit search/filter。"""
        try:
            page = self._provider.list_tasks(
                limit=_PAGE_SIZE,
                cursor_sequence=cursor,
                search_text=text,
                state_filter=states,
            )
        except Exception as exc:  # noqa: BLE001 页内错误条保留旧数据，不吞错
            self._with_error(
                exc, "list", cursor=cursor, text=text, states=states, action=action
            )
            return None
        self._clear_error()
        self.search_text = text or ""
        self.state_filter = states[0] if states else None
        return page

    def _commit_fresh(self, page) -> None:
        """成功的新查询（搜索/过滤/首查）一次性提交重置分页与历史选择。"""
        self.selected_task_key = None
        self.selected_result_id = None
        self.detail_panel.clear()
        self.current_cursor = None
        self.cursor_history.clear()
        self._render_page(page)

    def _render_page(self, page) -> None:
        self._has_more = bool(page.has_more)
        self._next_cursor = page.next_cursor
        self._render_table(page.items)
        self.result_count_label.setText(
            "当前结果 {} / 总数 {}".format(
                len(page.items), page.total_count if page.total_count is not None else "?"
            )
        )
        self._update_pager()

    def _render_table(self, items: tuple) -> None:
        rows = tuple(items)
        self.tasks_table.setRowCount(len(rows))
        self._empty_label.setVisible(len(rows) == 0)
        for row, item in enumerate(rows):
            values = (
                item.task_id,
                item.project_key if item.project_key else "—",
                item.state or "—",
                item.delivery_state if item.delivery_state else "—",
                item.received_at if item.received_at else "—",
            )
            for col, text in enumerate(values):
                cell = QTableWidgetItem(text)
                cell.setToolTip(text)
                cell.setData(Qt.UserRole, item.task_key)
                self.tasks_table.setItem(row, col, cell)

    def _update_pager(self) -> None:
        self.prev_button.setEnabled(bool(self.cursor_history))
        self.next_button.setEnabled(self._has_more)

    def next_page(self) -> None:
        if not self._has_more:
            return
        target = self._next_cursor
        text, states = self._filters()
        page = self._fetch_page(target, text, states, "list_next")
        if page is None:
            return
        self.cursor_history.append(self.current_cursor)
        self.current_cursor = target
        self._render_page(page)

    def previous_page(self) -> None:
        if not self.cursor_history:
            return
        target = self.cursor_history[-1]
        text, states = self._filters()
        page = self._fetch_page(target, text, states, "list_prev")
        if page is None:
            return
        self.cursor_history.pop()
        self.current_cursor = target
        self._render_page(page)

    def _on_search_changed(self, text: str) -> None:
        del text
        self._reset_filters()

    def _on_filter_changed(self, index: int) -> None:
        del index
        self._reset_filters()

    def _reset_filters(self) -> None:
        """显式用户搜索/过滤变化：新查询成功才重置分页并清除历史选择；
        失败则保持旧表、旧 viewed 选择、旧分页状态，只显示错误条。"""
        text, states = self._filters()
        page = self._fetch_page(None, text, states, "list_fresh")
        if page is None:
            return
        self._commit_fresh(page)

    def _on_row_clicked(self, row: int, col: int) -> None:
        del col
        item = self.tasks_table.item(row, 0)
        if item is None:
            return
        task_key = item.data(Qt.UserRole)
        if task_key:
            self.load_task_detail(str(task_key))

    # ------------------------------------------------------------- 详情与 Result

    def load_task_detail(self, task_key: str) -> None:
        """详情 bundle（detail + versions）全部成功才一次性切换；
        任一查询失败 → 保持旧 bundle（旧选择/旧详情/旧版本/旧 Result），只显错误条。"""
        try:
            detail = self._provider.get_task_detail(task_key)
        except Exception as exc:  # noqa: BLE001 页内错误条，保留旧详情与旧选择
            self._with_error(exc, "detail", task_key=task_key)
            return
        if detail is None:
            self.selected_task_key = task_key
            self.selected_result_id = None
            self.detail_panel.render_detail(None)
            self._clear_error()
            return
        try:
            versions = self._provider.list_task_result_versions(task_key)
        except Exception as exc:  # noqa: BLE001 版本失败 → 不切换到新任务，避免跨任务污染
            self._with_error(exc, "detail", task_key=task_key)
            return
        self.selected_task_key = task_key
        self.selected_result_id = None
        self.detail_panel.render_detail(detail)
        self.detail_panel.render_versions(versions)
        self.detail_panel.reset_result_section()
        self._clear_error()

    def select_result(self, result_id: str) -> None:
        """exact result 精确读取；query 成功（含 None 缺失）才 commit 选择，
        失败保持旧 selected_result_id / 旧 Result 内容 / 旧 copy target。"""
        try:
            detail = self._provider.get_result(result_id)
        except Exception as exc:  # noqa: BLE001
            self._with_error(exc, "result", result_id=result_id)
            return
        self._clear_error()
        self.selected_result_id = result_id
        if detail is None:
            self.detail_panel.render_result_missing(result_id)
        else:
            self.detail_panel.render_result(detail)

    # ------------------------------------------------------------- 响应式

    @property
    def is_single_column(self) -> bool:
        return not self._split_in_row

    def set_single_column(self, flag: bool) -> None:
        """narrow：列表在上、详情在下单列；否则并排。只改布局，不动数据。"""
        if flag == self.is_single_column:
            return
        split = self._split_row.layout()
        if self._split_in_row:
            split.removeWidget(self.detail_panel)
            self._outer.insertWidget(self._outer.count() - 1, self.detail_panel)
        else:
            self._outer.removeWidget(self.detail_panel)
            split.addWidget(self.detail_panel)
            split.setStretch(1, 4)
        self._split_in_row = not self._split_in_row

    # ------------------------------------------------------------- Snapshot / 焦点

    def render(self, snapshot: ApplicationSnapshot) -> None:
        """只更新当前真实活动任务 banner；不改变任何历史选择（UI-A12）。"""
        self._snapshot = snapshot
        active = snapshot.active_task
        if active is None:
            self.banner.set_tone("neutral")
            self.banner.set_value("当前真实活动任务：无")
        else:
            self.banner.set_tone("success")
            self.banner.set_value(
                "当前真实活动任务：{} · {}".format(
                    active.task_id or "未知", active.state or "未知"
                )
            )

    @property
    def focus_target(self) -> QLineEdit:
        return self.search_input