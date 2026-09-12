"""T15 工作台 Dashboard。

数据单向流动：ApplicationSnapshot（app.snapshots）
    → StatusPresenter 纯函数（ui.status_presenter）
    → 本页只读渲染。

职责边界（规格 14.2 / 14.3，T15-A 审核定稿）：
- 只做展示：不查询 DB/Store/Client，不 send/continue/timer，不启动任何业务；
- 一律复用 presenter 的输出（7 节点恢复条、4 段阶段条、计数器、连接、事件、
  队列摘要），不在本页再写第二套 phase/stage 映射；
- 进度/恢复/连接/事件都只由 Snapshot 驱动，禁止假百分比、禁止自造 TTL/时间；
- 空状态（active_task is None）不残留上一任务标题/session/模型/恢复相位；
- BLOCKED 只给文字与 action_hint 建议，不提供业务按钮；
- focus_target() 返回只读标签作为首个可聚焦目标，不添加假业务按钮。

对 T14 兼容：保留 task_card/_empty_label/_state_badge/_task_title/_task_id/
task_card.title 等既有分区属性与 render/set_single_column/is_single_column/
focus_target 用法；卡片标题按 T15 正式名称（T14 受影响断言已同步更新）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.snapshots import ApplicationSnapshot
from ui.status_presenter import (
    BLOCKED_REASON_LABELS,
    RECOVERY_STEP_LABELS,
    PROGRESS_STEP_LABELS,
    connection_presentation,
    counters,
    event_rows,
    format_runtime,
    present_status,
    progress_presentation,
    queue_brief_rows,
    recovery_steps,
)
from ui.components import Card, StatusBadge
from ui.theme_tokens import refresh_style

_STATE_MARK = {"done": "✓", "current": "●", "todo": "○"}


def _tone_label(text: str, tone: str) -> QLabel:
    """统一 tone 标签（复用 QSS QLabel[tone=...] 视觉，无组件级样式）。"""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setProperty("tone", tone)
    refresh_style(label)
    return label


def _mono_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("mono", "true")
    label.setWordWrap(True)
    return label


class Dashboard(QWidget):
    """PAGE01 工作台（正式）。保持 T14 MainWindow 接口合同。"""

    def __init__(self, snapshot: ApplicationSnapshot) -> None:
        super().__init__()
        self._snapshot = snapshot
        self._single = False
        self._rail_in_row = True

        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(24, 20, 24, 24)
        self._outer.setSpacing(16)

        heading = QLabel("工作台")
        heading.setProperty("heading", "true")
        self._outer.addWidget(heading)

        self._grid_row = QWidget()
        grid = QHBoxLayout(self._grid_row)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(16)

        self._main = QWidget()
        main_lay = QVBoxLayout(self._main)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(12)

        # ---- 当前任务卡（T14 属性兼容：task_card/_state_badge/_task_title/_task_id/_task_meta）
        self.task_card = Card("当前任务")
        self._state_badge = StatusBadge("ACTIVE", tone="success")
        self._task_title = QLabel()
        self._task_title.setWordWrap(True)
        self._task_title.setFocusPolicy(Qt.StrongFocus)
        self._task_title.setAccessibleName("当前任务标题")
        self._task_id = QLabel()
        self._task_id.setProperty("mono", "true")
        self._task_id.setWordWrap(True)
        self._task_meta = QLabel()
        self._task_meta.setWordWrap(True)
        self._models_label = QLabel()
        self._models_label.setWordWrap(True)
        self._times_label = QLabel()
        self._times_label.setWordWrap(True)
        self._config_label = QLabel()
        self._config_label.setWordWrap(True)
        self.task_card.add(self._state_badge)
        self.task_card.add(self._task_title)
        self.task_card.add(self._task_id)
        self.task_card.add(self._task_meta)
        self.task_card.add(self._models_label)
        self.task_card.add(self._times_label)
        self.task_card.add(self._config_label)

        # ---- 空状态（UI-A01：没有任务时不再残留上一任务信息）
        self._empty_card = Card("空状态")
        self._empty_label = QLabel("等待 A 端任务")
        self._empty_label.setWordWrap(True)
        self._empty_label.setFocusPolicy(Qt.StrongFocus)
        self._empty_label.setAccessibleName("空状态标题")
        self._recv_empty_badge = StatusBadge("接收 未知", tone="neutral")
        self._conn_empty_line = _tone_label("连接状态 未知", "neutral")
        self._queue_empty_line = QLabel()
        self._queue_empty_line.setWordWrap(True)
        self._empty_card.add(self._empty_label)
        self._empty_card.add(self._recv_empty_badge)
        self._empty_card.add(self._conn_empty_line)
        self._empty_card.add(self._queue_empty_line)

        # ---- 三张状态卡（独立表达，不揉成系统正常）
        self.conn_card = Card("模型/接口连接")
        self._conn_badge = StatusBadge("连接 未知", tone="neutral")
        self._conn_detail = QLabel()
        self._conn_detail.setWordWrap(True)
        self.conn_card.add(self._conn_badge)
        self.conn_card.add(self._conn_detail)

        self.auto_card = Card("原会话自动续接")
        self._auto_badge = StatusBadge("自动续接 未知", tone="neutral")
        self._auto_detail = QLabel()
        self._auto_detail.setWordWrap(True)
        self.auto_card.add(self._auto_badge)
        self.auto_card.add(self._auto_detail)

        self.queue_card = Card("等待任务数量")
        self._queue_count = QLabel("0")
        self._queue_count.setProperty("heading", "true")
        self.queue_card.add(self._queue_count)
        self._queue_brief = QLabel()
        self._queue_brief.setWordWrap(True)
        self.queue_card.add(self._queue_brief)

        self._status_row = QWidget()
        self._status_grid = QGridLayout(self._status_row)
        self._status_grid.setContentsMargins(0, 0, 0, 0)
        self._status_grid.setSpacing(12)
        for i, card in enumerate((self.conn_card, self.auto_card, self.queue_card)):
            self._status_grid.addWidget(card, 0, i)
        for col in range(3):
            self._status_grid.setColumnStretch(col, 1)

        # ---- 恢复状态条（7 节点，只渲染 Presenter）
        self.recovery_card = Card("恢复状态")
        self._recovery_headline = StatusBadge("—", tone="neutral")
        self._recovery_detail = QLabel()
        self._recovery_detail.setWordWrap(True)
        self._recovery_nodes = QWidget()
        node_lay = QHBoxLayout(self._recovery_nodes)
        node_lay.setContentsMargins(0, 0, 0, 0)
        node_lay.setSpacing(8)
        self._recovery_node_labels: list[QLabel] = []
        for label in RECOVERY_STEP_LABELS:
            node = QLabel()
            node.setWordWrap(True)
            node.setAlignment(Qt.AlignTop)
            node.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            node_lay.addWidget(node)
            self._recovery_node_labels.append(node)
        self.recovery_card.add(self._recovery_headline)
        self.recovery_card.add(self._recovery_detail)
        self.recovery_card.add(self._recovery_nodes)

        # ---- 四段工作阶段（只读 progress_stage / progress_stage_completed）
        self.stage_card = Card("工作阶段")
        stage_lay = QHBoxLayout()
        stage_lay.setContentsMargins(0, 0, 0, 0)
        stage_lay.setSpacing(8)
        self._stage_labels: list[QLabel] = []
        for code in PROGRESS_STEP_LABELS:
            stage = QLabel()
            stage.setWordWrap(True)
            stage.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            stage_lay.addWidget(stage)
            self._stage_labels.append(stage)
        self.stage_card.add_layout(stage_lay)

        # ---- 右栏（累计/连续/下一动作/最近事件）
        self.rail = Card("进度与事件")
        self._counter_total = _tone_label("累计续接 0 次", "neutral")
        self._counter_consecutive = _tone_label("连续未恢复 0 次", "neutral")
        self._next_action = QLabel("下一动作：—")
        self._next_action.setWordWrap(True)
        self.rail.add(self._counter_total)
        self.rail.add(self._counter_consecutive)
        self.rail.add(self._next_action)
        self._events_box = QVBoxLayout()
        self._events_box.setContentsMargins(0, 0, 0, 0)
        self._events_box.setSpacing(6)
        self.rail.add_layout(self._events_box)
        self._events_hint = QLabel("最近事件：—")
        self._events_hint.setWordWrap(True)

        for w in (
            self._empty_card,
            self.task_card,
            self._status_row,
            self.recovery_card,
            self.stage_card,
        ):
            main_lay.addWidget(w)
        main_lay.addStretch(1)

        grid.addWidget(self._main)
        grid.addWidget(self.rail)
        self._outer.addWidget(self._grid_row)
        self._outer.addStretch(1)

        self.set_single_column(False)
        self.render(snapshot)

    # ------------------------------------------------------------ 渲染

    def render(self, snapshot: ApplicationSnapshot) -> None:
        self._snapshot = snapshot
        active = snapshot.active_task
        empty = active is None

        self._empty_card.setVisible(empty)
        self.task_card.setVisible(not empty)
        self.recovery_card.setVisible(not empty)
        self.stage_card.setVisible(not empty)
        self.rail.setVisible(True)
        self._status_row.setVisible(not empty or snapshot.waiting_task_count is not None)

        if empty:
            self._render_empty(snapshot)
            self._render_status_cards(snapshot)   # 连接/续接/队列仍可见
            self._render_rail(snapshot)
            return

        self._render_task(snapshot, active)
        self._render_status_cards(snapshot)
        self._render_recovery(snapshot)
        self._render_stages(snapshot)
        self._render_rail(snapshot)

    def _render_empty(self, snapshot: ApplicationSnapshot) -> None:
        # 空态不清零：直接展示本次快照的连接/队列，不存在“上一任务”残留字段
        self._empty_label.setText("等待 A 端任务")
        recv_on = bool(snapshot.receiving_enabled)
        self._recv_empty_badge.set_tone("success" if recv_on else "neutral")
        self._recv_empty_badge.set_value("接收 开启" if recv_on else "接收 已暂停")
        conn = connection_presentation(snapshot)
        self._conn_empty_line.setProperty("tone", conn.tone)
        self._conn_empty_line.setText(f"{conn.headline} · {conn.detail}")
        refresh_style(self._conn_empty_line)
        if snapshot.waiting_task_count is not None:
            self._queue_empty_line.setText(
                f"等待任务：{snapshot.waiting_task_count}\n" + "\n".join(queue_brief_rows(snapshot))
            )
        else:
            self._queue_empty_line.setText("等待任务：—")

    def _render_task(self, snapshot: ApplicationSnapshot, active) -> None:
        status = present_status(snapshot)
        self._state_badge.set_tone(status.tone)
        self._state_badge.set_value(active.state or "未知")
        self._task_title.setText(active.title or "未指定")
        self._task_id.setText(f"task_id：{active.task_id or '未指定'}")
        self._task_meta.setText(
            f"project：{active.project or '未指定'}｜session：{active.session or '未指定'}"
        )
        self._models_label.setText(
            "请求模型：{}｜解析模型：{}｜实际模型：{}".format(
                active.requested_model or "未指定",
                active.parsed_model or "未指定",
                active.actual_model or "未知",
            )
        )
        begin = active.received_at or active.running_since
        times = f"开始/接收：{begin.strftime('%Y-%m-%d %H:%M:%S')}" if begin else "开始/接收：未知"
        if active.running_since is not None and active.running_since != begin:
            times += f"｜开始运行：{active.running_since.strftime('%H:%M:%S')}"
        times += f"｜运行时长：{format_runtime(active.runtime_seconds)}"
        self._times_label.setText(times)
        self._config_label.setText(f"config revision:{active.config_revision or '未知'}")

    def _render_status_cards(self, snapshot: ApplicationSnapshot) -> None:
        conn = connection_presentation(snapshot)
        self._conn_badge.set_tone(conn.tone)
        self._conn_badge.set_value(conn.headline)
        self._conn_detail.setText(conn.detail)

        auto = snapshot.auto_resume
        if auto is None:
            self._auto_badge.set_tone("neutral")
            self._auto_badge.set_value("自动续接 未知")
            self._auto_detail.setText("未提供续接状态")
        elif auto.paused_by_user:
            self._auto_badge.set_tone("neutral")
            self._auto_badge.set_value("自动续接 已暂停")
            self._auto_detail.setText("由用户暂停；累计计数不丢失")
        elif auto.enabled:
            self._auto_badge.set_tone("success")
            self._auto_badge.set_value("自动续接 已开启")
            self._auto_detail.setText("接收 A 端任务后可自动续接")
        else:
            self._auto_badge.set_tone("neutral")
            self._auto_badge.set_value("自动续接 已关闭")
            self._auto_detail.setText("未启用自动续接")

        count = snapshot.waiting_task_count if snapshot.waiting_task_count is not None else 0
        self._queue_count.setText(f"等待任务：{count}")
        rows = queue_brief_rows(snapshot)
        self._queue_brief.setText("队列摘要：\n" + "\n".join(rows) if rows else "队列摘要：暂无")

    def _render_recovery(self, snapshot: ApplicationSnapshot) -> None:
        status = present_status(snapshot)
        self._recovery_headline.set_tone(status.tone)
        self._recovery_headline.set_value(status.headline)
        self._recovery_detail.setText(status.detail)
        for node, step in zip(self._recovery_node_labels, recovery_steps(snapshot)):
            mark = _STATE_MARK[step.state]
            tone = {"done": "success", "current": "recovering", "todo": "neutral"}[step.state]
            node.setText(f"{mark} {step.label}")
            node.setProperty("tone", tone)
            node.setAccessibleName(f"{step.label}，{'已完成' if step.state == 'done' else '进行中' if step.state == 'current' else '待处理'}")
            refresh_style(node)

    def _render_stages(self, snapshot: ApplicationSnapshot) -> None:
        progress = progress_presentation(snapshot)
        states = {"done": "success", "current": "recovering", "todo": "neutral"}
        for label, stage in zip(self._stage_labels, progress.stages):
            mark = _STATE_MARK[stage.state]
            label.setText(f"{mark} {stage.label}")
            label.setProperty("tone", states[stage.state])
            label.setAccessibleName(
                f"{stage.label}，{'已完成' if stage.state == 'done' else '进行中' if stage.state == 'current' else '待处理'}"
            )
            refresh_style(label)

    def _render_rail(self, snapshot: ApplicationSnapshot) -> None:
        c = counters(snapshot)
        self._counter_total.setProperty("tone", "recovering" if c.resume_total else "neutral")
        self._counter_total.setText(c.total_text)
        self._counter_consecutive.setProperty(
            "tone", "recovering" if c.consecutive_no_progress else "success"
        )
        self._counter_consecutive.setText(c.consecutive_text)
        refresh_style(self._counter_total)
        refresh_style(self._counter_consecutive)

        status = present_status(snapshot)
        self._next_action.setText(
            f"下一动作：{status.next_action_text if status.next_action_text else '等待上游状态'}"
        )

        # 事件由 Presenter 决定排序/上限/脱敏；UI 只展示
        while self._events_box.count():
            item = self._events_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        rows = event_rows(snapshot)
        if not rows:
            self._events_hint.setText("最近事件：—")
            self._events_box.addWidget(self._events_hint)
            return
        for row in rows:
            text = f"{row.time_text}  {row.summary or '（空摘要）'}"
            lab = _tone_label(text, row.tone)
            lab.setAccessibleName(text)
            self._events_box.addWidget(lab)

    # ------------------------------------------------------------ 响应式

    @property
    def is_single_column(self) -> bool:
        return not self._rail_in_row

    def set_single_column(self, flag: bool) -> None:
        """narrow：右栏下移为单列 + 状态卡改单列；否则恢复并排。只改布局，不动数据。"""
        if flag == self.is_single_column:
            self._apply_single_only(flag)
            return
        grid = self._grid_row.layout()
        if self._rail_in_row:
            grid.removeWidget(self.rail)
            self._outer.insertWidget(self._outer.count() - 1, self.rail)
        else:
            self._outer.removeWidget(self.rail)
            grid.addWidget(self.rail)
        self._rail_in_row = not self._rail_in_row
        self._apply_single_only(flag)

    def _apply_single_only(self, flag: bool) -> None:
        # 状态卡：单列时竖直排布，宽/中时三列
        for i, card in enumerate((self.conn_card, self.auto_card, self.queue_card)):
            self._status_grid.removeWidget(card)
        if flag:
            for i, card in enumerate((self.conn_card, self.auto_card, self.queue_card)):
                self._status_grid.addWidget(card, i, 0)
            self._status_grid.setColumnStretch(0, 1)
            for col in (1, 2):
                self._status_grid.setColumnStretch(col, 0)
        else:
            for i, card in enumerate((self.conn_card, self.auto_card, self.queue_card)):
                self._status_grid.addWidget(card, 0, i)
            for col in range(3):
                self._status_grid.setColumnStretch(col, 1)
        # 窄屏右栏放宽到整行，避免撑出横向滚动
        if flag:
            self.rail.setMinimumWidth(0)
            self.rail.setMaximumWidth(16777215)
        else:
            self.rail.setMinimumWidth(294)
            self.rail.setMaximumWidth(330)

    # ------------------------------------------------------------ 焦点

    @property
    def focus_target(self) -> QWidget:
        """首个可聚焦目标：当前任务卡标题或空态标题（只读标签，无业务按钮）。"""
        if self._snapshot.active_task is not None:
            return self._task_title
        return self._empty_label

    # ------------------------------------------------------------ 只读访问
    # 供 T15 验收测试断言展示值（数据来源仍只有 Snapshot + Presenter）

    def recovery_headline_text(self) -> str:
        return self._recovery_headline.text()

    def stage_texts(self) -> tuple[str, ...]:
        return tuple(lab.text() for lab in self._stage_labels)

    def recovery_node_texts(self) -> tuple[str, ...]:
        return tuple(lab.text() for lab in self._recovery_node_labels)

    def counter_texts(self) -> tuple[str, str]:
        return self._counter_total.text(), self._counter_consecutive.text()

    def rail_next_action_text(self) -> str:
        return self._next_action.text()

    def blocked_hint(self) -> str | None:
        rec = self._snapshot.recovery
        if rec is None or rec.blocked_reason is None:
            return None
        return BLOCKED_REASON_LABELS.get(rec.blocked_reason, "请检查任务状态后处理")