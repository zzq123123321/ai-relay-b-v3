"""AI Relay B Lite 单页 UI 骨架。

只做布局、控件与 UI 内部行为；本轮不接真实网络、不发模型、不压缩真实会话、
不读 ClipLink，也不 import OpenChamberClient（Controller 接线留待下一轮）。
单窗口、无侧边栏 / Tab / 设置页。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

DEFAULT_WRAPPER_TEMPLATE = "{content}"

# 默认“项目总指挥模板”：完整保留开发文档中的原文，仅按语义分行以便阅读。
COMMANDER_TEMPLATE = """我有一个项目是：【在这里填写项目目标、功能需求和背景】

项目路径：【在这里填写项目路径】

需要你担任总指挥，与 B 端本地模型配合完成。B 端的回复由我原样转发给你。

请先理解项目目标并划分阶段。每个阶段拆成若干小任务，每轮只下发一个任务，最多包含 3 个具体交付项，原则上修改不超过 3 个主要文件。测试文件和必要配置文件不计入限制。

任务必须写清范围、要求、测试方法、验收标准、回传内容和停止点。B 端一轮任务后，会自动压缩上下文，必须开发时阅读开发文档。

你需要简要说明项目、项目路径、已完成内容和当前任务，不要依赖旧会话上下文。B 端报告只需包含修改文件、关键改动、测试结果、已知问题和未完成事项，不要输出完整代码或冗长日志。

你负责逐轮审核；发现问题就下发修正任务，审核通过后再进入下一任务。不要重复已完成工作，不要让 B 端自行进入下一阶段，也不要扩大项目范围。

落实无人化，有什么问题直接提交给你，不要问我，我也无法回答。接下来的所有对话回复都会直接传给 B 端程序进行操作。我不会去看你的内容，你的内容直接就是指令，B 端严格执行。

每次回复最后写明：
已完成内容：
下一步内容：
整体预计完成率：
文档地址：【在这里填写开发文档地址】

当你判断项目已完成并通过最终审核时，最后一条发给 B 端的内容必须严格只输出：
AI_RELAY_COMPLETE
不得带任何其他文字、标点、解释或前后缀。只有严格命中 AI_RELAY_COMPLETE 才触发自动联动停止。
"""

_CARD_STYLE = (
    "QFrame#card{border:1px solid #d0d0d0;border-radius:6px;"
    "background:#f7f7f7;padding:8px;}"
)
_BOX_STYLE = (
    "QPlainTextEdit{border:1px solid #d0d0d0;border-radius:4px;background:#fafafa;}"
)


def _status_color(text: str) -> str:
    """状态色语义：绿=已连接/正常，黄=连接中/恢复，红=失败/不可用，灰=未连接/暂停。"""
    s = text or ""
    if any(k in s for k in ("已连接", "正常")):
        return "#2e7d32"
    if any(k in s for k in ("连接中", "恢复")):
        return "#b8860b"
    if any(k in s for k in ("失败", "不可用", "断开")):
        return "#c62828"
    return "#808080"


class WrapperDialog(QDialog):
    """包装内容编辑弹窗（A端自动任务用，不作用于手动发送框）。仅本地 UI，不落 config。"""

    saved = Signal(str)

    def __init__(self, current_text: str, default_text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("wrapper_dialog")
        self.setWindowTitle("包装内容")
        self.resize(640, 420)
        self._default = default_text

        self._edit = QPlainTextEdit(self)
        self._edit.setObjectName("wrapper_edit")
        self._edit.setPlainText(current_text)
        note = QLabel("这是未来 A端自动任务包装使用，不作用于手动发送框。", self)

        btn_restore = QPushButton("恢复默认", self)
        btn_restore.setObjectName("wrapper_restore")
        btn_cancel = QPushButton("取消", self)
        btn_cancel.setObjectName("wrapper_cancel")
        btn_save = QPushButton("保存", self)
        btn_save.setObjectName("wrapper_save")
        btn_restore.clicked.connect(lambda: self._edit.setPlainText(self._default))
        btn_cancel.clicked.connect(self.close)
        btn_save.clicked.connect(self._on_save)

        form = QVBoxLayout(self)
        form.addWidget(note)
        form.addWidget(self._edit, 1)
        row = QHBoxLayout()
        row.addWidget(btn_restore)
        row.addStretch(1)
        row.addWidget(btn_cancel)
        row.addWidget(btn_save)
        form.addLayout(row)

    def _on_save(self) -> None:
        self.saved.emit(self._edit.toPlainText())
        self.close()


class CommanderDialog(QDialog):
    """项目总指挥模板编辑弹窗。填入发送框只写入手动框，不自动发送。"""

    filled = Signal(str)

    def __init__(self, default_text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("commander_dialog")
        self.setWindowTitle("项目总指挥模板")
        self.resize(680, 520)
        self._default = default_text

        self._edit = QPlainTextEdit(self)
        self._edit.setObjectName("commander_edit")
        self._edit.setPlainText(default_text)

        btn_restore = QPushButton("恢复默认模板", self)
        btn_restore.setObjectName("commander_restore")
        btn_cancel = QPushButton("取消", self)
        btn_cancel.setObjectName("commander_cancel")
        btn_fill = QPushButton("填入发送框", self)
        btn_fill.setObjectName("commander_fill")
        btn_restore.clicked.connect(lambda: self._edit.setPlainText(self._default))
        btn_cancel.clicked.connect(self.close)
        btn_fill.clicked.connect(self._on_fill)

        form = QVBoxLayout(self)
        form.addWidget(self._edit, 1)
        row = QHBoxLayout()
        row.addWidget(btn_restore)
        row.addStretch(1)
        row.addWidget(btn_cancel)
        row.addWidget(btn_fill)
        form.addLayout(row)

    def _on_fill(self) -> None:
        self.filled.emit(self._edit.toPlainText())
        self.close()


class MainWindow(QMainWindow):
    """单页主窗口：两张状态卡 + 大模型操作区 + 手动区 + 状态区 + 底部监听按钮。"""

    refresh_session_requested = Signal()
    test_model_requested = Signal()
    compact_requested = Signal()
    manual_send_requested = Signal(str)
    listening_changed = Signal(bool)
    auto_compact_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AI Relay B Lite")
        self.resize(900, 760)
        self._listening = False
        self._wrapper_template = DEFAULT_WRAPPER_TEMPLATE

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        root.addLayout(self._build_status_cards())
        root.addLayout(self._build_model_actions())
        root.addWidget(self._build_manual_area())
        root.addWidget(self._build_status_area(), 1)
        root.addLayout(self._build_bottom())
        self.setCentralWidget(central)

        # 两个最小弹窗（本地 UI，模式不阻塞，可重复打开）
        self._wrapper_dialog = WrapperDialog(self._wrapper_template, DEFAULT_WRAPPER_TEMPLATE, self)
        self._wrapper_dialog.saved.connect(self._on_wrapper_saved)
        self._commander_dialog = CommanderDialog(COMMANDER_TEMPLATE, self)
        self._commander_dialog.filled.connect(self._fill_commander)

    # ------------------------------------------------------------------ 构建
    def _card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("card")
        frame.setStyleSheet(_CARD_STYLE)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(4)
        title_lbl = QLabel(title, frame)
        title_lbl.setStyleSheet("font-weight:bold;font-size:14px;")
        lay.addWidget(title_lbl)
        return frame, lay

    def _status_label(self, lay: QVBoxLayout, text: str, object_name: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName(object_name)
        lbl.setStyleSheet(f"font-weight:bold;color:{_status_color(text)};")
        lay.addWidget(lbl)
        return lbl

    def _plain_label(self, lay: QVBoxLayout, text: str, object_name: str | None = None) -> QLabel:
        lbl = QLabel(text)
        if object_name:
            lbl.setObjectName(object_name)
        lay.addWidget(lbl)
        return lbl

    def _build_status_cards(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        a_card, a_lay = self._card("A端连接")
        a_card.setObjectName("a_card")
        self._a_status = self._status_label(a_lay, "● 未连接", "a_status")
        self._a_peer = self._plain_label(a_lay, "对端：--")
        self._a_latency = self._plain_label(a_lay, "网络延迟：-- ms")
        a_lay.addStretch(1)

        m_card, m_lay = self._card("大模型连接")
        m_card.setObjectName("llm_card")
        self._llm_status = self._status_label(m_lay, "● 未连接", "llm_status")
        self._llm_oc = self._plain_label(m_lay, "OpenChamber服务延迟：-- ms")
        self._llm_first = self._plain_label(m_lay, "模型首响应：-- ms")
        self._session_label = self._plain_label(m_lay, "当前会话：无", "session_label")
        m_lay.addStretch(1)

        row.addWidget(a_card, 1)
        row.addWidget(m_card, 1)
        return row

    def _build_model_actions(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setSpacing(8)
        self._btn_refresh = QPushButton("获取当前激活会话")
        self._btn_refresh.setObjectName("btn_refresh")
        self._btn_test = QPushButton("测试大模型")
        self._btn_test.setObjectName("btn_test")
        self._btn_compact = QPushButton("立即压缩当前会话")
        self._btn_compact.setObjectName("btn_compact")
        self._chk_auto = QCheckBox("自动压缩会话")
        self._chk_auto.setObjectName("chk_auto")
        grid.addWidget(self._btn_refresh, 0, 0)
        grid.addWidget(self._btn_test, 0, 1)
        grid.addWidget(self._btn_compact, 1, 0)
        grid.addWidget(self._chk_auto, 1, 1)
        self._btn_refresh.clicked.connect(self.refresh_session_requested)
        self._btn_test.clicked.connect(self.test_model_requested)
        self._btn_compact.clicked.connect(self.compact_requested)
        self._chk_auto.toggled.connect(self.auto_compact_changed)
        return grid

    def _build_manual_area(self) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setSpacing(6)

        row = QHBoxLayout()
        self._btn_commander = QPushButton("项目总指挥模板")
        self._btn_commander.setObjectName("btn_commander")
        self._btn_wrapper = QPushButton("包装内容")
        self._btn_wrapper.setObjectName("btn_wrapper")
        self._btn_clear = QPushButton("清空")
        self._btn_clear.setObjectName("btn_clear")
        row.addWidget(self._btn_commander)
        row.addWidget(self._btn_wrapper)
        row.addWidget(self._btn_clear)
        row.addStretch(1)
        lay.addLayout(row)

        lay.addWidget(QLabel("手动发送"))
        self._manual_edit = QPlainTextEdit()
        self._manual_edit.setObjectName("manual_input")
        self._manual_edit.setStyleSheet(_BOX_STYLE)
        self._manual_edit.setPlaceholderText("在这里输入要直接发给当前会话的内容")
        self._manual_edit.setMinimumHeight(120)
        self._manual_edit.installEventFilter(self)
        lay.addWidget(self._manual_edit)

        hint = QLabel("Ctrl+Enter 发送")
        hint.setAlignment(Qt.AlignRight)
        lay.addWidget(hint)

        btn_row = QHBoxLayout()
        self._btn_send = QPushButton("发送")
        self._btn_send.setObjectName("btn_send")
        btn_row.addStretch(1)
        btn_row.addWidget(self._btn_send)
        lay.addLayout(btn_row)

        self._btn_send.clicked.connect(self._send_from_ui)
        self._btn_clear.clicked.connect(lambda: self._manual_edit.setPlainText(""))
        self._btn_commander.clicked.connect(self._open_commander)
        self._btn_wrapper.clicked.connect(self._open_wrapper)
        return box

    def _read_box(self, text: str, object_name: str, fixed: bool) -> QPlainTextEdit:
        box = QPlainTextEdit()
        box.setObjectName(object_name)
        box.setReadOnly(True)
        box.setStyleSheet(_BOX_STYLE)
        if fixed:
            box.setFixedHeight(56)
        if text:
            box.setPlainText(text)
        return box

    def _build_status_area(self) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setSpacing(6)
        lay.addWidget(QLabel("当前任务"))
        self._task_box = self._read_box("空闲", "task_box", fixed=True)
        lay.addWidget(self._task_box)
        lay.addWidget(QLabel("最近结果"))
        self._result_box = self._read_box("暂无结果", "result_box", fixed=True)
        lay.addWidget(self._result_box)
        lay.addWidget(QLabel("简单日志"))
        self._log_box = self._read_box("", "log_box", fixed=False)
        self._log_box.setMinimumHeight(100)
        lay.addWidget(self._log_box, 1)
        return box

    def _build_bottom(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self._btn_listen = QPushButton("开始监听")
        self._btn_listen.setObjectName("btn_listen")
        self._btn_listen.setMinimumHeight(36)
        self._btn_listen.clicked.connect(self._toggle_listening)
        row.addStretch(1)
        row.addWidget(self._btn_listen)
        row.addStretch(1)
        return row

    # ------------------------------------------------------------------ 行为
    def _send_from_ui(self) -> None:
        """把手动框内容原样发出（不修改、不套包装），随后清空输入框。"""
        text = self._manual_edit.toPlainText()
        self.manual_send_requested.emit(text)
        self._manual_edit.setPlainText("")

    def _toggle_listening(self) -> None:
        self._listening = not self._listening
        self._btn_listen.setText("停止监听" if self._listening else "开始监听")
        self.listening_changed.emit(self._listening)

    def _open_wrapper(self) -> None:
        self._wrapper_dialog.show()
        self._wrapper_dialog.raise_()
        self._wrapper_dialog.activateWindow()

    def _open_commander(self) -> None:
        self._commander_dialog.show()
        self._commander_dialog.raise_()
        self._commander_dialog.activateWindow()

    def _fill_commander(self, text: str) -> None:
        self._manual_edit.setPlainText(text)  # 只写入手动框，不自动发送

    def _on_wrapper_saved(self, text: str) -> None:
        self._wrapper_template = text  # 仅更新内存值，不落 config
        self.append_log("包装内容已保存（仅内存）")

    # ------------------------------------------------------------------ 事件
    def eventFilter(self, obj, event) -> bool:
        if obj is self._manual_edit and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter) and (event.modifiers() & Qt.ControlModifier):
                self._send_from_ui()
                return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------- 下一轮接线用的简单 setter
    def set_a_connection(self, status: str = "未连接", peer: str = "--", latency_ms=None) -> None:
        self._a_status.setText("● " + status)
        self._a_status.setStyleSheet(f"font-weight:bold;color:{_status_color(status)};")
        self._a_peer.setText("对端：" + peer)
        self._a_latency.setText("网络延迟：" + (f"{latency_ms} ms" if latency_ms is not None else "-- ms"))

    def set_model_connection(self, status: str = "未连接", oc_ms=None, first_response_ms=None) -> None:
        self._llm_status.setText("● " + status)
        self._llm_status.setStyleSheet(f"font-weight:bold;color:{_status_color(status)};")
        self._llm_oc.setText("OpenChamber服务延迟：" + (f"{oc_ms} ms" if oc_ms is not None else "-- ms"))
        self._llm_first.setText("模型首响应：" + (f"{first_response_ms} ms" if first_response_ms is not None else "-- ms"))

    def set_current_session(self, text: str | None) -> None:
        self._session_label.setText("当前会话：" + (text if text else "无"))

    def set_current_task(self, text: str | None) -> None:
        self._task_box.setPlainText(text if text else "空闲")

    def set_recent_result(self, text: str | None) -> None:
        self._result_box.setPlainText(text if text else "暂无结果")

    def append_log(self, text: str) -> None:
        self._log_box.appendPlainText(text)