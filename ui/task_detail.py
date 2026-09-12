"""T16-B2 PANEL01 TaskDetail 详情面板（正式，只读展示）。

职责边界（规格 14.1 PANEL01 / 14.3 / T16-B2 卡）：
- 纯展示：不持有 Provider/不查数据/不碰剪贴板；所有数据由 TaskRecordsPage
  读取后调用 render_* 注入。
- 完整值原则：全部身份字段/路径/原始消息/Result 正文长文本 wrap + 可选文本 +
  Tooltip 完整值；只允许视觉 elide，禁止“只显示截断且无全文入口”。
- 原始任务可折叠展示，但折叠是纯 UI 状态，完整值一直存在（raw_message_value()）。
- Attempt 续接链：active 只依据 detail.active_attempt_id 精确标记，绝不按
  “最后一行”猜测；损坏 Attempt 显示可读身份字段 + 数据不完整诊断，不整块消失。
- Result 版本按 Provider 顺序（revision DESC）展示；只用中性的「当前权威」/
  「历史版本」标记，历史版本不作为错误。
- 复制只发信号：copy_value_requested(kind, full_value) /
  copy_result_requested(result_id)，由 B3 的 ClipboardSink seam 接实际写入。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui.components import Card, SecondaryButton, TextButton
from ui.theme_tokens import refresh_style


def _label_value(value: object, fallback: str = "—") -> str:
    if value is None:
        return fallback
    text = str(value)
    return text if text else fallback


def _block_label(text: str, *, tone: str | None = None) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    label.setToolTip(text)
    if tone:
        label.setProperty("tone", tone)
        refresh_style(label)
    return label


def _copy_row(kind: str, title: str, value: str) -> tuple[QWidget, QLabel, TextButton]:
    """一行为单位展示一个完整长值 + 完整值复制按钮（kind 供 copy signal）。"""
    host = QWidget()
    lay = QHBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(8)
    name = QLabel(title)
    name.setFixedWidth(150)
    value_label = QLabel(value if value else "—")
    value_label.setWordWrap(True)
    value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    value_label.setToolTip(value)
    copy = TextButton("复制")
    copy.setAccessibleName(f"复制{title}")
    lay.addWidget(name)
    lay.addWidget(value_label, 1)
    lay.addWidget(copy)
    return host, value_label, copy


class TaskDetailPanel(QWidget):
    """PANEL01 任务详情。数据全部由外部 render_* 注入；自身只发复制/选择信号。"""

    copy_value_requested = Signal(str, str)
    copy_result_requested = Signal(str)
    result_selected = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._active_attempt_id: str | None = None
        self._selected_result_id: str | None = None
        self._result_readable = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        # ------------------------------------------------------------- 身份/状态
        self.identity_card = Card("任务身份 / 状态")
        self._identity_note = QLabel()
        self._identity_note.setWordWrap(True)
        self._identity_note.setProperty("tone", "danger")
        refresh_style(self._identity_note)
        self._identity_note.hide()
        self._identity_grid = QGridLayout()
        self._identity_grid.setContentsMargins(0, 0, 0, 0)
        self._identity_grid.setHorizontalSpacing(12)
        self._identity_grid.setVerticalSpacing(6)

        self.task_id_value, self.task_id_copy = self._identity_entry(
            0, "原 Task ID", "task_id"
        )
        self.task_key_value, self.task_key_copy = self._identity_entry(
            1, "task_key", "task_key"
        )
        # row 2..n 其余身份字段
        self._identity_labels: dict[str, QLabel] = {}

        def _kv_row(row: int, title: str) -> QLabel:
            name = QLabel(title)
            value = QLabel()
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self._identity_grid.addWidget(name, row, 0)
            self._identity_grid.addWidget(value, row, 1)
            self._identity_labels[title] = value
            return value

        self.sequence_value = _kv_row(2, "sequence")
        self.peer_id_value = _kv_row(3, "peer_id")
        self.protocol_value = _kv_row(4, "protocol_format")
        self.state_value = _kv_row(5, "state")
        self.blocked_value = _kv_row(6, "blocked_reason")
        self.received_value = _kv_row(7, "received_at")
        self.project_value = _kv_row(8, "project_key")
        self.directory_value, self.directory_copy = self._identity_entry(
            9, "directory", "directory"
        )
        self.requested_model_value = _kv_row(10, "requested_model")
        self.frozen_session_value = _kv_row(11, "frozen_session_id")
        self.config_revision_value = _kv_row(12, "config_revision")
        self.authority_epoch_value = _kv_row(13, "authority_epoch")
        self.active_attempt_value = _kv_row(14, "active_attempt_id")

        self.identity_card.add(self._identity_note)
        self.identity_card.add_layout(self._identity_grid)
        outer.addWidget(self.identity_card)

        # ------------------------------------------------------------- 原始任务
        self.raw_card = Card("原始任务")
        self._raw_collapsed = True
        self.raw_toggle = TextButton("显示原始消息")
        self.raw_toggle.setAccessibleName("切换原始消息显示")
        self.raw_toggle.clicked.connect(self._toggle_raw)
        self.body_value = _block_label("")
        self.body_value.setAccessibleName("任务正文")
        self.hash_value = QLabel()
        self.hash_value.setProperty("mono", "true")
        self.hash_value.setWordWrap(True)
        self.hash_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.hash_value.setAccessibleName("原始消息摘要")
        self.hash_value_copy = TextButton("复制")
        self.hash_value_copy.setAccessibleName("复制原始消息摘要")
        self._raw_area = QWidget()
        rayl = QVBoxLayout(self._raw_area)
        rayl.setContentsMargins(0, 0, 0, 0)
        rayl.setSpacing(8)
        self.raw_message_label = _block_label("")
        self.raw_message_label.setAccessibleName("原始消息完整内容")
        rayl.addWidget(self.raw_message_label)

        self.raw_card.add(self.raw_toggle)
        self.raw_card.add(self.body_value)

        self._hash_wrap = QWidget()
        hlay = QHBoxLayout(self._hash_wrap)
        hlay.setContentsMargins(0, 0, 0, 0)
        hlay.addWidget(self.hash_value, 1)
        hlay.addWidget(self.hash_value_copy)
        self.hash_value_copy.clicked.connect(
            lambda: self.copy_value_requested.emit(
                "canonical_hash", self.hash_value.text()
            )
        )
        self.raw_card.add(self._hash_wrap)
        self.raw_card.add(self._raw_area)
        self._raw_area.setVisible(False)
        outer.addWidget(self.raw_card)

        # ------------------------------------------------------------- Attempt 链
        self.attempt_card = Card("Attempt / 续接链")
        self._attempt_box = QVBoxLayout()
        self._attempt_box.setContentsMargins(0, 0, 0, 0)
        self._attempt_box.setSpacing(8)
        self._attempt_empty = QLabel("无 Attempt 记录")
        self._attempt_empty.setWordWrap(True)
        self.attempt_card.add_layout(self._attempt_box)
        outer.addWidget(self.attempt_card)

        # ------------------------------------------------------------- Result 版本
        self.version_card = Card("Result 版本")
        self.version_table = QTableWidget(0, 7)
        self.version_table.setHorizontalHeaderLabels(
            ("修订", "Result ID", "状态", "来源", "交付", "提交时间", "标记")
        )
        self.version_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.version_table.setSelectionBehavior(QTableWidget.SelectRows)
        header = self.version_table.horizontalHeader()
        for col in (0, 1, 2, 3, 4):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        self.version_table.setSortingEnabled(False)
        self.version_table.itemSelectionChanged.connect(self._on_version_selected)
        self.version_note = QLabel()
        self.version_note.setWordWrap(True)
        self.version_note.hide()
        self.version_card.add(self.version_table)
        self.version_card.add(self.version_note)
        outer.addWidget(self.version_card)

        # ------------------------------------------------------------- Result 精确
        self.result_card = Card("Result 精确查看")
        self._result_title = QLabel("未选择 Result 版本")
        self._result_title.setWordWrap(True)
        self._result_title.setAccessibleName("Result 精确查看标题")
        self._result_body = _block_label("")
        self._result_body.setAccessibleName("Result 最终正文")
        self._result_protocol = QLabel()
        self._result_protocol.setWordWrap(True)
        self._result_protocol.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._result_protocol.setAccessibleName("Result 协议文本")
        self._result_sha = QLabel()
        self._result_sha.setProperty("mono", "true")
        self._result_sha.setWordWrap(True)
        self._result_sha.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.copy_sha_button = TextButton("复制")
        self.copy_sha_button.setAccessibleName("复制 Result 摘要")
        self.copy_sha_button.clicked.connect(self._sha_forward)
        self._result_meta = QLabel()
        self._result_meta.setWordWrap(True)
        self._result_missing = QLabel()
        self._result_missing.setProperty("tone", "danger")
        self._result_missing.setWordWrap(True)
        refresh_style(self._result_missing)
        self._result_missing.hide()
        self.copy_reply_button = SecondaryButton("复制回复")
        self.copy_reply_button.setAccessibleName("复制回复")
        self.copy_reply_button.clicked.connect(self._on_copy_reply)

        self.result_card.add(self._result_title)
        self.result_card.add(self._result_missing)
        self.result_card.add(self._result_body)
        sha_row = QHBoxLayout()
        sha_row.addWidget(self._result_sha, 1)
        sha_row.addWidget(self.copy_sha_button)
        self.result_card.add_layout(sha_row)
        self.result_card.add(self._result_protocol)
        self.result_card.add(self._result_meta)
        self.result_card.add(self.copy_reply_button)
        outer.addWidget(self.result_card)
        outer.addStretch(1)

        self.clear()

    # ------------------------------------------------------------- 内部工具

    def _identity_entry(self, row: int, title: str, kind: str) -> tuple[QLabel, TextButton]:
        host, value, copy = _copy_row(kind, title, "")
        copy.clicked.connect(
            lambda _=False: self.copy_value_requested.emit(kind, value.text() or "")
        )
        self._identity_grid.addWidget(host, row, 0, 1, 2)
        return value, copy

    @property
    def raw_message_value(self) -> str:
        return self._raw_message_full

    @property
    def raw_message_visible(self) -> bool:
        return not self._raw_area.isHidden()

    @property
    def selected_result_id(self) -> str | None:
        return self._selected_result_id

    @property
    def result_readable(self) -> bool:
        return self._result_readable

    def _toggle_raw(self) -> None:
        self._raw_collapsed = not self._raw_collapsed
        self._apply_raw_visibility()

    def _apply_raw_visibility(self) -> None:
        self._raw_area.setVisible(not self._raw_collapsed)
        self.raw_toggle.setText("折叠原始消息" if not self._raw_collapsed else "显示原始消息")

    # ------------------------------------------------------------- 渲染入口

    def clear(self) -> None:
        """整体清空（切任务/搜索变化时由页面调用）。"""
        self._active_attempt_id = None
        self._selected_result_id = None
        self._result_readable = False
        self._raw_message_full = ""
        self._identity_note.hide()
        for v in self._identity_labels.values():
            v.setText("")
        self.task_id_value.setText("")
        self.task_key_value.setText("")
        self.directory_value.setText("")
        self.sequence_value.setText("")
        self.peer_id_value.setText("")
        self.protocol_value.setText("")
        self.state_value.setText("")
        self.blocked_value.setText("")
        self.received_value.setText("")
        self.project_value.setText("")
        self.requested_model_value.setText("")
        self.frozen_session_value.setText("")
        self.config_revision_value.setText("")
        self.authority_epoch_value.setText("")
        self.active_attempt_value.setText("")
        self.body_value.setText("")
        self.hash_value.setText("")
        self.raw_message_label.setText("")
        self._raw_collapsed = True
        self._apply_raw_visibility()
        self._clear_attempts()
        self.version_table.setRowCount(0)
        self._result_title.setText("未选择 Result 版本")
        self._result_body.setText("")
        self._result_protocol.setText("")
        self._result_sha.setText("")
        self._result_meta.setText("")
        self._result_missing.hide()
        self.copy_reply_button.setEnabled(False)
        self.copy_sha_button.setEnabled(False)

    def _clear_attempts(self) -> None:
        while self._attempt_box.count():
            item = self._attempt_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def render_detail(self, detail) -> None:
        """注入 TaskDetail（或其 None → 显示不存在提示，不残留旧值）。"""
        self._active_attempt_id = detail.active_attempt_id if detail else None
        self._raw_message_full = detail.raw_message if detail else ""
        if detail is None:
            self.clear()
            self._identity_note.setText("任务不存在")
            self._identity_note.show()
            return
        self._identity_note.hide()
        self.task_id_value.setText(_label_value(detail.task_id))
        self.task_id_value.setToolTip(_label_value(detail.task_id))
        self.task_key_value.setText(detail.task_key)
        self.task_key_value.setToolTip(detail.task_key)
        self.sequence_value.setText(str(detail.sequence))
        self.peer_id_value.setText(_label_value(detail.peer_id))
        self.protocol_value.setText(_label_value(detail.protocol_format))
        self.state_value.setText(detail.state or "未知")
        self.blocked_value.setText(_label_value(detail.blocked_reason))
        self.received_value.setText(_label_value(detail.received_at))
        self.project_value.setText(_label_value(detail.project_key))
        self.directory_value.setText(_label_value(detail.directory))
        self.directory_value.setToolTip(_label_value(detail.directory))
        self.requested_model_value.setText(_label_value(detail.requested_model))
        self.frozen_session_value.setText(_label_value(detail.frozen_session_id))
        self.config_revision_value.setText(
            "-" if detail.config_revision is None else str(detail.config_revision)
        )
        self.authority_epoch_value.setText(str(detail.authority_epoch))
        self.active_attempt_value.setText(_label_value(detail.active_attempt_id))

        if detail.corrupt_reasons:
            self._identity_note.setText(
                "数据不完整：" + "；".join(detail.corrupt_reasons)
            )
            self._identity_note.show()

        self.body_value.setText(detail.body if detail.body else "（无正文）")
        self.hash_value.setText(detail.canonical_hash or "")
        self.raw_message_label.setText(
            detail.raw_message if detail.raw_message else "（无原始消息）"
        )
        self._apply_raw_visibility()

        self._clear_attempts()
        for attempt in detail.attempts:
            self._attempt_box.addWidget(self._attempt_block(attempt))

    def _attempt_block(self, attempt) -> QLabel:
        active = self._active_attempt_id == attempt.attempt_id
        mark = "【当前进行中】" if active else "（历史）"
        corrupt = bool(attempt.corrupt_reasons)
        head = f"{mark} attempt_id：{attempt.attempt_id}  parent_attempt_id：{_label_value(attempt.parent_attempt_id)}"
        line2 = (
            f"kind：{_label_value(attempt.kind)}  state：{_label_value(attempt.state)}"
            f"  authority_epoch：{attempt.authority_epoch}"
            f"  remote_state：{_label_value(attempt.remote_state)}"
        )
        line3 = (
            f"started_at：{_label_value(attempt.started_at)}"
            f"  ended_at：{_label_value(attempt.ended_at)}"
        )
        line4 = f"resolved_session_id：{_label_value(attempt.resolved_session_id)}"
        text = "\n".join((head, line2, line3, line4))
        if corrupt:
            text += "\n⚠ 数据不完整：" + "；".join(attempt.corrupt_reasons)
        tone = "danger" if corrupt else ("success" if active else "neutral")
        label = _block_label(text, tone=tone)
        label.setAccessibleName(
            "Attempt 续接链，"
            + ("当前进行中，" if active else "历史，" )
            + ("数据不完整" if corrupt else "完整")
        )
        return label

    def render_versions(self, versions: tuple) -> None:
        """注入 list_task_result_versions 输出（顺序由 Provider，revision DESC）。"""
        rows = tuple(versions)
        self.version_table.setRowCount(len(rows))
        self.version_note.hide()
        for row, version in enumerate(rows):
            items = (
                str(version.revision),
                version.result_id,
                version.state or "—",
                version.source or "—",
                version.delivery_state or "—",
                version.committed_at or "—",
                "当前权威" if version.authoritative else "历史版本",
            )
            for col, text in enumerate(items):
                cell = QTableWidgetItem(text)
                cell.setToolTip(text)
                cell.setData(Qt.UserRole, version.result_id)
                self.version_table.setItem(row, col, cell)
        if not rows:
            self.version_note.setText("无 Result 版本")
            self.version_note.show()

    # ------------------------------------------------------------- Result 精确

    def _on_version_selected(self) -> None:
        selected = self.version_table.selectedItems()
        if not selected:
            return
        result_id = selected[0].data(Qt.UserRole)
        if result_id:
            self.result_selected.emit(str(result_id))

    def render_result(self, detail) -> None:
        """注入 get_result 的精确 ResultDetail；缺失时调用 render_result_missing。"""
        self._selected_result_id = detail.result_id
        self._result_readable = True
        self._result_missing.hide()
        self._result_title.setText(f"Result：{detail.result_id or '—'}")
        self._result_body.setText(detail.final_body if detail.final_body else "（无正文）")
        self._result_protocol.setText(
            detail.protocol_text if detail.protocol_text else "（无协议文本）"
        )
        self._result_sha.setText(detail.sha256 or "—")
        self._result_meta.setText(
            "revision {} · {} · source {} · delivery {}\ncommitted：{}\nremote ids：{}".format(
                detail.revision,
                ("当前权威" if detail.authoritative else "历史版本"),
                detail.source or "—",
                detail.delivery_state or "—",
                detail.committed_at or "—",
                ", ".join(detail.remote_message_ids) if detail.remote_message_ids else "—",
            )
        )
        if detail.corrupt_reasons:
            self._result_missing.setText(
                "数据不完整：" + "；".join(detail.corrupt_reasons)
            )
            self._result_missing.show()
        self.copy_reply_button.setEnabled(True)
        self.copy_sha_button.setEnabled(True)

    def _sha_forward(self) -> None:
        value = self._result_sha.text()
        if value and value != "—":
            self.copy_value_requested.emit("sha256", value)

    def render_result_missing(self, result_id: str) -> None:
        """exact result 不存在：保持 selected_result_id，结果区不可读、复制禁用。"""
        self._selected_result_id = result_id
        self._result_readable = False
        self._result_title.setText(f"Result：{result_id or '—'}")
        self._result_body.setText("")
        self._result_protocol.setText("")
        self._result_sha.setText("")
        self._result_meta.setText("")
        self._result_missing.setText("此版本无法读取")
        self._result_missing.show()
        self.copy_reply_button.setEnabled(False)
        self.copy_sha_button.setEnabled(False)

    def reset_result_section(self) -> None:
        """切任务时清空精确查看区（不触碰 identity/attempt/versions）。"""
        self._selected_result_id = None
        self._result_readable = False
        self._result_title.setText("未选择 Result 版本")
        self._result_body.setText("")
        self._result_protocol.setText("")
        self._result_sha.setText("")
        self._result_meta.setText("")
        self._result_missing.hide()
        self.copy_reply_button.setEnabled(False)
        self.copy_sha_button.setEnabled(False)

    def _on_copy_reply(self) -> None:
        if self._result_readable and self._selected_result_id:
            self.copy_result_requested.emit(self._selected_result_id)