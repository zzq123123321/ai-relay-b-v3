"""AI Relay B Lite 三页 UI（阶段 2C）：主控 / 模型配置 / 模板与日志。

只做布局、控件与 UI 内部行为；“A端连接”卡由 main 层轮询 ClipLink 状态文件后
调用 set_a_connection 刷新（本模块不直接读文件、不发网络、不 import OpenChamberClient）。
页面只创建一次并保留实例；切页不丢输入/草稿/开关，不重复接线、不重复建 worker/QTimer。
首页与配置页共用同一探测结果与生效地址刷新（镜像控件用不同 objectName）。
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
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

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

# ---- §6 深色主题（生产 UI 自带，截图脚本不另套主题）----
# 语义：蓝=主要操作，绿=连接成功，橙=等待/待生效，红=错误，灰=未连接/未检测。
_BG = "#1e1f22"
_CARD_BG = "#26282c"
_BORDER = "#3a3d42"
_TEXT = "#e6e6e6"
_INPUT_BG = "#1a1b1e"
_BTN_BG = "#33363b"
_BTN_HOVER = "#3d4148"
_PRIMARY = "#2f6fed"
_PRIMARY_HOVER = "#4a83f7"
_OK = "#4caf50"        # 绿=已连接/正常
_WAIT = "#f5a623"      # 橙=连接中/待生效
_ERR = "#ef5350"       # 红=失败/断开
_IDLE = "#9aa0a6"      # 灰=未连接/未检测

_DARK_STYLE = f"""
QWidget {{ background:transparent; color:{_TEXT}; }}
QMainWindow, QDialog {{ background:{_BG}; }}
QLabel {{ color:{_TEXT}; background:transparent; }}
QTabWidget {{ background:{_BG}; }}
QTabWidget::pane {{ border:1px solid {_BORDER}; border-radius:4px; background:{_BG}; top:-1px; }}
QTabBar::tab {{ background:{_CARD_BG}; border:1px solid {_BORDER}; border-bottom:none;
    border-radius:4px; padding:5px 16px; color:{_TEXT}; }}
QTabBar::tab:selected {{ background:{_BG}; color:#ffffff; }}
QFrame#card {{ background:{_CARD_BG}; border:1px solid {_BORDER}; border-radius:6px; }}
QLineEdit {{ background:{_INPUT_BG}; border:1px solid {_BORDER}; border-radius:4px;
    color:{_TEXT}; padding:3px 6px; selection-background-color:{_PRIMARY}; }}
QLineEdit:focus {{ border:1px solid {_PRIMARY}; }}
QPlainTextEdit {{ background:{_INPUT_BG}; border:1px solid {_BORDER}; border-radius:4px;
    color:{_TEXT}; padding:2px 6px; selection-background-color:{_PRIMARY}; }}
QPushButton {{ background:{_BTN_BG}; color:{_TEXT}; border:1px solid {_BORDER};
    border-radius:4px; padding:4px 10px; }}
QPushButton:hover {{ background:{_BTN_HOVER}; }}
QPushButton:pressed {{ background:#2a2d31; }}
QPushButton#btn_send, QPushButton#btn_listen {{ background:{_PRIMARY}; border:1px solid {_PRIMARY}; color:#ffffff; }}
QPushButton#btn_send:hover, QPushButton#btn_listen:hover {{ background:{_PRIMARY_HOVER}; }}
QCheckBox {{ color:{_TEXT}; spacing:6px; }}
QCheckBox::indicator {{ width:14px; height:14px; border:1px solid {_BORDER};
    border-radius:3px; background:{_INPUT_BG}; }}
QCheckBox::indicator:checked {{ background:{_PRIMARY}; border:1px solid {_PRIMARY}; }}
QScrollBar:vertical {{ background:{_BG}; width:10px; margin:0; }}
QScrollBar::handle:vertical {{ background:{_BORDER}; border-radius:4px; min-height:24px; }}
QScrollBar:horizontal {{ background:{_BG}; height:10px; margin:0; }}
QScrollBar::handle:horizontal {{ background:{_BORDER}; border-radius:4px; min-width:24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width:0; height:0; }}
"""

_CARD_STYLE = (
    f"QFrame#card{{border:1px solid {_BORDER};border-radius:6px;"
    f"background:{_CARD_BG};padding:8px;}}"
)
_BOX_STYLE = (
    f"QPlainTextEdit{{border:1px solid {_BORDER};border-radius:4px;"
    f"background:{_INPUT_BG};color:{_TEXT};}}"
)


def _status_color(text: str) -> str:
    """状态色语义：绿=已连接/正常，橙=连接中/恢复，红=失败/不可用，灰=未连接/暂停。"""
    s = text or ""
    if any(k in s for k in ("已连接", "正常")):
        return _OK
    if any(k in s for k in ("连接中", "恢复")):
        return _WAIT
    if any(k in s for k in ("失败", "不可用", "断开")):
        return _ERR
    return _IDLE


class ResultDetailDialog(QDialog):
    """最近结果完整详情弹窗：只读，展示完整结果文本（不静默截断、不依赖悬停）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("result_detail_dialog")
        self.setWindowTitle("最近结果详情")
        self.resize(560, 420)

        self._edit = QPlainTextEdit(self)
        self._edit.setObjectName("result_detail")
        self._edit.setReadOnly(True)
        self._edit.setStyleSheet(_BOX_STYLE)

        btn_close = QPushButton("关闭", self)
        btn_close.setObjectName("result_detail_close")
        btn_close.clicked.connect(self.close)

        form = QVBoxLayout(self)
        form.addWidget(self._edit, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(btn_close)
        form.addLayout(row)

    def set_detail(self, text: str) -> None:
        self._edit.setPlainText(text)


class ClickableLabel(QLabel):
    """可点击标签：长内容（生效/待生效地址、错误提示）换行有界显示、不撑宽窗口，
    完整原文左点击开只读详情弹窗（不只依赖悬停 tooltip，不截断数据）。"""

    clicked = Signal()

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class FullTextDialog(QDialog):
    """全文详情弹窗：只读可滚动区域展示完整原文（长地址/长错误提示），不静默截断。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("full_text_dialog")
        self.setWindowTitle("全文详情")
        self.resize(640, 420)

        self._edit = QPlainTextEdit(self)
        self._edit.setObjectName("full_text")
        self._edit.setReadOnly(True)
        self._edit.setStyleSheet(_BOX_STYLE)

        btn_close = QPushButton("关闭", self)
        btn_close.setObjectName("full_text_close")
        btn_close.clicked.connect(self.close)

        form = QVBoxLayout(self)
        form.addWidget(self._edit, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(btn_close)
        form.addLayout(row)

    def set_full_text(self, text: str) -> None:
        self._edit.setPlainText(text)


class MainWindow(QMainWindow):
    """三页主窗口：主控（默认页）/ 模型配置 / 模板与日志（内分项目模板、运行日志）。

    大模型卡（主控页，只读镜像）：已连接/未连接 + 服务延迟 + 生效地址；
    地址编辑、保存、待生效/失败提示集中在“模型配置”页（同一批真实控件）。
    连接显示以 main 层 probe 轮询为准，首页与配置页用同一结果刷新，不增加 HTTP。
    """

    refresh_session_requested = Signal()
    test_model_requested = Signal()
    compact_requested = Signal()
    manual_send_requested = Signal(str)
    wrap_clipboard_requested = Signal()
    listening_changed = Signal(bool)
    auto_compact_changed = Signal(bool)
    model_address_changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AI Relay B Lite")
        self.setStyleSheet(_DARK_STYLE)  # §6 深色主题随生产 UI 生效，不依赖截图脚本
        self.resize(720, 640)  # 2C 默认尺寸；680×600 必须可用
        self._listening = False
        self._active_model_url: str | None = None
        self._model_state: dict = {"status": "未检测", "oc_ms": None, "first_response_ms": None}

        self._main_tabs = QTabWidget()
        self._main_tabs.setObjectName("main_tabs")
        self._page_main = self._build_main_page()
        self._page_config = self._build_config_page()
        self._page_templates = self._build_template_page()
        self._main_tabs.addTab(self._page_main, "主控")
        self._main_tabs.addTab(self._page_config, "模型配置")
        self._main_tabs.addTab(self._page_templates, "模板与日志")
        self.setCentralWidget(self._main_tabs)

        # 本地详情弹窗
        self._result_detail = ResultDetailDialog(self)
        self._full_text = FullTextDialog(self)

    # ------------------------------------------------------------------ 构建
    def _card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("card")
        frame.setStyleSheet(_CARD_STYLE)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(2)
        title_lbl = QLabel(title, frame)
        title_lbl.setStyleSheet(f"font-weight:bold;font-size:14px;color:#ffffff;")
        lay.addWidget(title_lbl)
        return frame, lay

    def _status_label(self, lay: QVBoxLayout, text: str, object_name: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName(object_name)
        lbl.setStyleSheet(f"font-weight:bold;color:{_status_color(text)};")
        lay.addWidget(lbl)
        return lbl

    def _plain_label(
        self,
        lay: QVBoxLayout,
        text: str,
        object_name: str | None = None,
        bounded: bool = False,
    ) -> QLabel:
        """bounded=True：长内容换行有界（不撑宽窗口/隐藏页最小宽），点击开全文详情弹窗。"""
        cls = ClickableLabel if bounded else QLabel
        lbl = cls(text)
        if bounded:
            lbl.setWordWrap(True)
            lbl.clicked.connect(lambda: self._open_full_text(lbl.text()))
        if object_name:
            lbl.setObjectName(object_name)
        lay.addWidget(lbl)
        return lbl

    def _build_main_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("page_main")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(12)

        lay.addLayout(self._build_status_cards())
        lay.addLayout(self._build_session_ops())
        lay.addWidget(self._build_task_result())
        lay.addWidget(self._build_manual_area(), 1)
        lay.addLayout(self._build_bottom())
        return page

    def _build_status_cards(self) -> QHBoxLayout:
        """主控页顶部两列紧凑连接卡（只读镜像；地址编辑在“模型配置”页）。"""
        row = QHBoxLayout()
        row.setSpacing(12)

        a_card, a_lay = self._card("A端连接")
        a_card.setObjectName("a_card")
        self._a_status = self._status_label(a_lay, "● 未连接", "a_status")
        self._a_peer = self._plain_label(a_lay, "对端：--", "a_peer")
        self._a_latency = self._plain_label(a_lay, "网络延迟：-- ms", "a_latency")

        m_card, m_lay = self._card("大模型连接")
        m_card.setObjectName("llm_card")
        self._llm_status = self._status_label(m_lay, "● 未检测", "llm_status")
        self._llm_oc = self._plain_label(m_lay, "服务延迟：-- ms", "llm_oc")
        self._llm_first = self._plain_label(m_lay, "模型首响应：-- ms", "llm_first")
        self._llm_first.setVisible(False)  # 首响应时间不再上屏（保留内部数据与断言入口）
        self._session_label = self._plain_label(m_lay, "当前会话：无", "session_label")
        self._session_label.setVisible(False)  # 当前会话 ID 不再上屏（内部识别逻辑保留）
        self._llm_active = self._plain_label(m_lay, "生效地址：--", "llm_active", bounded=True)

        row.addWidget(a_card, 1)
        row.addWidget(m_card, 1)
        return row

    def _build_session_ops(self) -> QVBoxLayout:
        """会话操作栏常驻：获取当前激活会话 / 立即压缩当前会话 / 自动压缩会话。"""
        layout = QVBoxLayout()
        layout.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(8)
        self._btn_refresh = QPushButton("获取当前激活会话")
        self._btn_refresh.setObjectName("btn_refresh")
        self._btn_compact = QPushButton("立即压缩当前会话")
        self._btn_compact.setObjectName("btn_compact")
        self._chk_auto = QCheckBox("自动压缩会话")
        self._chk_auto.setObjectName("chk_auto")
        row.addWidget(self._btn_refresh)
        row.addWidget(self._btn_compact)
        row.addWidget(self._chk_auto)
        row.addStretch(1)
        self._btn_refresh.clicked.connect(self.refresh_session_requested)
        self._btn_compact.clicked.connect(self.compact_requested)
        self._chk_auto.toggled.connect(self.auto_compact_changed)
        layout.addLayout(row)
        self._session_notice = self._plain_label(layout, "", "session_notice", bounded=True)
        self._session_notice.setVisible(False)
        return layout

    def _build_task_result(self) -> QWidget:
        """当前任务 / 最近结果：紧凑只读摘要区 + 可点击的完整详情（不只依赖悬停）。"""
        box = QWidget()
        grid = QGridLayout(box)
        grid.setSpacing(6)
        grid.addWidget(QLabel("当前任务"), 0, 0)
        self._task_box = self._read_box("空闲", "task_box", fixed=True)
        grid.addWidget(self._task_box, 0, 1)
        grid.addWidget(QLabel("最近结果"), 1, 0)
        self._result_box = self._read_box("暂无结果", "result_box", fixed=True)
        grid.addWidget(self._result_box, 1, 1)
        self._btn_result_detail = QPushButton("详情")
        self._btn_result_detail.setObjectName("btn_result_detail")
        self._btn_result_detail.clicked.connect(self._open_result_detail)
        grid.addWidget(self._btn_result_detail, 1, 2)
        grid.setColumnStretch(1, 1)
        return box

    def _build_manual_area(self) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._btn_commander = QPushButton("项目总指挥模板")
        self._btn_commander.setObjectName("btn_commander")
        self._btn_wrapper = QPushButton("包装内容")
        self._btn_wrapper.setObjectName("btn_wrapper")
        self._btn_wrapper.setToolTip("人工优先：立即包装当前剪贴板文本并写回；不等待自动任务完成")
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
        self._manual_edit.setPlaceholderText("输入内容后点「发送」或 Ctrl+Enter：立即包装并复制到剪贴板")
        self._manual_edit.setMinimumHeight(56)
        self._manual_edit.installEventFilter(self)
        lay.addWidget(self._manual_edit, 1)

        btn_row = QHBoxLayout()
        hint = QLabel("Ctrl+Enter 发送")
        btn_row.addWidget(hint)
        btn_row.addStretch(1)
        self._btn_send = QPushButton("发送")
        self._btn_send.setObjectName("btn_send")
        self._btn_send.setToolTip("把输入框内容立即包装并复制到剪贴板（Ctrl+Enter）")
        btn_row.addWidget(self._btn_send)
        lay.addLayout(btn_row)

        self._btn_send.clicked.connect(self._send_from_ui)
        self._btn_clear.clicked.connect(lambda: self._manual_edit.setPlainText(""))
        self._btn_commander.clicked.connect(self._open_commander)
        self._btn_wrapper.clicked.connect(self.wrap_clipboard_requested)
        return box

    def _build_bottom(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self._btn_view_log = QPushButton("查看完整日志")
        self._btn_view_log.setObjectName("btn_view_log")
        self._btn_view_log.clicked.connect(self.show_log_page)
        self._btn_listen = QPushButton("开始监听")
        self._btn_listen.setObjectName("btn_listen")
        self._btn_listen.setMinimumHeight(28)
        self._btn_listen.clicked.connect(self._toggle_listening)
        row.addWidget(self._btn_view_log)
        row.addStretch(1)
        row.addWidget(self._btn_listen)
        row.addStretch(1)
        return row

    def _build_config_page(self) -> QWidget:
        """模型配置页：生效地址/实时状态镜像 + 地址编辑与保存 + 待生效/失败提示集中显示。"""
        page = QWidget()
        page.setObjectName("page_config")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(12)

        card, cl = self._card("模型配置")
        card.setObjectName("cfg_card")
        self._cfg_llm_status = self._status_label(cl, "● 未检测", "cfg_llm_status")
        self._cfg_llm_oc = self._plain_label(cl, "服务延迟：-- ms", "cfg_llm_oc")
        self._cfg_llm_active = self._plain_label(cl, "生效地址：--", "cfg_llm_active", bounded=True)
        self._plain_label(cl, "此处检测填写的大模型地址；自动任务通过本机执行服务处理。新地址重启后生效。")
        addr_row = QHBoxLayout()
        self._llm_address_edit = QLineEdit()
        self._llm_address_edit.setObjectName("model_address_edit")
        self._llm_address_edit.setPlaceholderText("本地模型地址，如 http://192.168.100.190:8080/v1")
        self._btn_save_address = QPushButton("保存")
        self._btn_save_address.setObjectName("btn_save_address")
        self._btn_save_address.clicked.connect(self._on_save_address)
        addr_row.addWidget(self._llm_address_edit, 1)
        addr_row.addWidget(self._btn_save_address)
        cl.addLayout(addr_row)
        self._llm_pending = self._plain_label(cl, "", "llm_pending", bounded=True)
        self._llm_pending.setVisible(False)  # 仅在保存新地址/失败/无效时显示

        lay.addWidget(card)
        lay.addStretch(1)
        return page

    def _build_template_page(self) -> QWidget:
        """模板与日志页：内分“项目模板 / 运行日志”两个内容切换区。"""
        page = QWidget()
        page.setObjectName("page_templates")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(12)

        self._tpl_tabs = QTabWidget(page)
        self._tpl_tabs.setObjectName("tpl_tabs")
        self._tpl_tabs.addTab(self._build_commander_panel(), "项目模板")
        self._tpl_tabs.addTab(self._build_log_panel(), "运行日志")
        lay.addWidget(self._tpl_tabs, 1)
        return page

    def _build_commander_panel(self) -> QWidget:
        """项目总指挥模板编辑区：编辑/恢复默认/取消返回/填入发送框；仅内存，不落盘。"""
        panel = QWidget()
        panel.setObjectName("commander_panel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        lay.addWidget(QLabel("项目总指挥模板：编辑仅内存、不落盘；填入发送框后回主控，不自动发送。"))
        self._commander_edit = QPlainTextEdit()
        self._commander_edit.setObjectName("commander_edit")
        self._commander_edit.setStyleSheet(_BOX_STYLE)
        self._commander_edit.setPlainText(COMMANDER_TEMPLATE)
        lay.addWidget(self._commander_edit, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._btn_commander_restore = QPushButton("恢复默认模板")
        self._btn_commander_restore.setObjectName("commander_restore")
        self._btn_commander_cancel = QPushButton("取消/返回")
        self._btn_commander_cancel.setObjectName("commander_cancel")
        self._btn_commander_fill = QPushButton("填入发送框")
        self._btn_commander_fill.setObjectName("commander_fill")
        row.addWidget(self._btn_commander_restore)
        row.addStretch(1)
        row.addWidget(self._btn_commander_cancel)
        row.addWidget(self._btn_commander_fill)
        lay.addLayout(row)

        self._btn_commander_restore.clicked.connect(
            lambda: self._commander_edit.setPlainText(COMMANDER_TEMPLATE)
        )
        self._btn_commander_cancel.clicked.connect(self.show_main_page)
        self._btn_commander_fill.clicked.connect(
            lambda: self._fill_commander(self._commander_edit.toPlainText())
        )
        return panel

    def _build_log_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("log_panel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)
        lay.addWidget(QLabel("运行日志"))
        self._log_box = self._read_box("", "log_box", fixed=False)
        self._log_box.setMinimumHeight(48)
        lay.addWidget(self._log_box, 1)
        return panel

    def _read_box(self, text: str, object_name: str, fixed: bool) -> QPlainTextEdit:
        box = QPlainTextEdit()
        box.setObjectName(object_name)
        box.setReadOnly(True)
        box.setStyleSheet(_BOX_STYLE)
        if fixed:
            box.setFixedHeight(32)
        if text:
            box.setPlainText(text)
        return box

    # ------------------------------------------------------------------ 页间导航
    def show_main_page(self) -> None:
        self._main_tabs.setCurrentWidget(self._page_main)

    def show_template_page(self) -> None:
        self._main_tabs.setCurrentWidget(self._page_templates)
        self._tpl_tabs.setCurrentIndex(0)

    def show_log_page(self) -> None:
        self._main_tabs.setCurrentWidget(self._page_templates)
        self._tpl_tabs.setCurrentIndex(1)

    # ------------------------------------------------------------------ 行为
    def _send_from_ui(self) -> None:
        """把手动输入框正文发出（原样、不修改），由接线侧立即包装并复制到剪贴板。

        阶段 4A：信号只携带输入框文本；包装与剪贴板写入在 main 层完成。
        输入框保留原内容，由用户通过 [清空] 自行决定是否清除。
        """
        self.manual_send_requested.emit(self._manual_edit.toPlainText())

    def set_listening(self, enabled: bool, emit: bool = True) -> None:
        """公开监听开关：统一维护 _listening / 按钮文字 / listening_changed。

        enabled=True → 按钮“停止监听”；enabled=False → “开始监听”。
        emit=True 时发 listening_changed（现有接线会继续调 bridge.set_listening）。
        """
        self._listening = enabled
        self._btn_listen.setText("停止监听" if enabled else "开始监听")
        if emit:
            self.listening_changed.emit(enabled)

    def set_auto_compact(self, enabled: bool) -> None:
        self._chk_auto.setChecked(enabled)

    def _toggle_listening(self) -> None:
        self.set_listening(not self._listening)

    def _open_commander(self) -> None:
        """首页快捷入口：跳转第三页“项目模板”编辑区（不再开弹窗）。"""
        self.show_template_page()

    def _fill_commander(self, text: str) -> None:
        self._manual_edit.setPlainText(text)  # 只写入手动框，不自动发送
        self.show_main_page()  # 填入后自动回主控

    def _open_result_detail(self) -> None:
        self._result_detail.set_detail(self._result_box.toPlainText())
        self._result_detail.show()
        self._result_detail.raise_()
        self._result_detail.activateWindow()

    def _open_full_text(self, text: str) -> None:
        """点击有界标签 → 只读弹窗显示完整原文（长地址/长错误提示）。"""
        self._full_text.set_full_text(text)
        self._full_text.show()
        self._full_text.raise_()
        self._full_text.activateWindow()

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

    def _on_save_address(self) -> None:
        self.model_address_changed.emit(self._llm_address_edit.text())

    def set_model_base_url(self, url: str) -> None:
        """启动注入本次生效地址：配置页输入框 + 主控/配置两页生效地址显示，并清掉“重启后生效”提示。"""
        self._active_model_url = url
        self._llm_address_edit.setText(url)
        self._llm_active.setText("生效地址：" + url)
        self._llm_active.setToolTip(url)
        self._cfg_llm_active.setText("生效地址：" + url)
        self._cfg_llm_active.setToolTip(url)
        self.set_model_notice("")

    def current_model_base_url(self) -> str | None:
        """本次启动的生效地址（保存新地址不会改变它，直到重启）。"""
        return self._active_model_url

    def set_model_notice(self, text: str) -> None:
        """地址保存/校验提示区（配置页）；空文本隐藏。用于区分“重启后生效/无效/失败/已一致”等状态。

        颜色语义：待生效=橙，无效/失败=红，其他=默认浅色。
        """
        if text:
            self._llm_pending.setText(text)
            self._llm_pending.setVisible(True)
            if "重启后生效" in text or "待生效" in text:
                color = _WAIT
            elif "失败" in text or "无效" in text:
                color = _ERR
            else:
                color = _TEXT
            self._llm_pending.setStyleSheet(f"font-weight:bold;color:{color};")
        else:
            self._llm_pending.setText("")
            self._llm_pending.setVisible(False)

    def set_model_pending(self, new_url: str) -> None:
        self.set_model_notice(f"新地址已保存，重启后生效：{new_url}")

    def set_model_connection(self, status: str = "未检测", latency_ms=None) -> None:
        """probe 轮询更新连接显示的唯一入口；手动测试/首响应/任务等旧事件不走这里。

        只允许“已连接/未连接”两态 + 服务延迟；未连接/未检测时延迟恒为 -- ms
        （断开探测的耗时不冒充服务延迟）。主控页与配置页用同一结果刷新（不增加 HTTP）。
        """
        status_text = "● " + status
        self._llm_status.setText(status_text)
        self._llm_status.setStyleSheet(f"font-weight:bold;color:{_status_color(status)};")
        self._cfg_llm_status.setText(status_text)
        self._cfg_llm_status.setStyleSheet(f"font-weight:bold;color:{_status_color(status)};")
        if status == "已连接" and latency_ms is not None:
            oc_text = f"服务延迟：{latency_ms} ms"
        else:
            oc_text = "服务延迟：-- ms"
        self._llm_oc.setText(oc_text)
        self._cfg_llm_oc.setText(oc_text)

    def set_model_first_response_display(self, latency_ms) -> None:
        """首响应显示入口（隐藏 label，不上屏）；绝不触碰 连接状态/服务延迟 显示。"""
        self._llm_first.setText("模型首响应：" + (f"{latency_ms} ms" if latency_ms is not None else "-- ms"))

    def set_current_session(self, text: str | None) -> None:
        self._session_label.setText("当前会话：" + (text if text else "无"))

    def set_session_notice(self, text: str, success: bool) -> None:
        self._session_notice.setText(text)
        self._session_notice.setStyleSheet(f"color:{_TEXT if success else _ERR};")
        self._session_notice.setVisible(bool(text))

    def set_current_task(self, text: str | None) -> None:
        self._task_box.setPlainText(text if text else "空闲")

    def set_recent_result(self, text: str | None) -> None:
        self._result_box.setPlainText(text if text else "暂无结果")

    def append_log(self, text: str) -> None:
        self._log_box.appendPlainText(text)
