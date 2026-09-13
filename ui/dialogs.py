"""T18-B1 确认框基础框架（DLG01–DLG04）。

职责（主规格 15.2–15.3 / 本卡）：
- 四个正式 Dialog 只做“确认意图并提交纯 UI 命令请求”：目标与后果明确、
  可显示 preview/summary、Confirm/Cancel、Esc=取消；
- 对外唯一出口 ``command_requested(object)``，payload 恒为构造时的
  同一个 ``UiCommandRequest`` 对象；Dialog 本身绝不执行业务：
  不包装、不停止、不建会话、不导出，不触碰 DB/网络/剪贴板；
- UI-A10/A11/A14：停止与新会话默认安全焦点（保留/取消）且保留按钮为默认
  Enter 目标，危险动作不接受默认回车误触；连续确认最多发一条命令，
  处理中 Confirm/Cancel 锁住并有“处理中…”可见反馈；
- ``finish_processing(message, *, success)`` 是纯 UI seam：只更新对话框内
  控件（成功不重发、失败可重试），业务动作由 B2 之后的接线层负责。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from app.commands import UiCommandKind, UiCommandRequest
from .components import DangerButton, PrimaryButton, SecondaryButton


def _hint(text: str) -> QLabel:
    """正文提示标签：自动换行、可被屏幕阅读器读到。"""
    lab = QLabel(text)
    lab.setWordWrap(True)
    return lab


class _ConfirmDialog(QDialog):
    """确认框共用骨架：单发确认、取消/Esc 安全、处理中锁 UI、焦点返回。

    子类通过类属性 ``_kind`` 声明动作种类，只需拼装 body 并调用
    ``_assemble``。本类不执行任何业务。
    """

    _kind: UiCommandKind

    command_requested = Signal(object)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        target_id: str | None = None,
        context: tuple[tuple[str, str], ...] = (),
        return_focus_widget: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._request = UiCommandRequest(
            kind=self._kind,
            target_id=target_id,
            context=context,
        )
        self._return_focus = return_focus_widget
        self._submitting = False
        self._status_label = QLabel()
        self._status_label.setAccessibleName("处理状态")
        self._status_label.setWordWrap(True)
        self._status_label.hide()
        self._confirm_button: PrimaryButton | DangerButton | None = None
        self._cancel_button: SecondaryButton | None = None

    # ------------------------------------------------------------------ API

    @property
    def request(self) -> UiCommandRequest:
        """构造时冻结的命令请求（Confirm 发出的就是同一个对象）。"""
        return self._request

    def finish_processing(self, message: str, *, success: bool) -> None:
        """处理结果反馈 seam：只更新 Dialog 内控件，绝不执行命令/再 emit。

        成功：命令已发出且仅一条，Confirm 保持锁定避免重复提交，取消恢复可用；
        失败：Confirm/Cancel 恢复可用并将状态重置，允许用户重试。
        """
        self._confirm_button.set_busy(False)
        if success:
            self._confirm_button.setEnabled(False)
            self._cancel_button.setEnabled(True)
        else:
            self._submitting = False
            self._confirm_button.setEnabled(True)
            self._cancel_button.setEnabled(True)
        self._show_status(message, error=not success)

    # ------------------------------------------------------- 子类拼装入口

    def _assemble(
        self,
        *,
        title: str,
        body_widgets: list[QLabel | QCheckBox],
        confirm_text: str,
        confirm_danger: bool,
        cancel_text: str = "取消",
    ) -> None:
        self.setWindowTitle(title)
        self.setModal(True)

        root = QVBoxLayout(self)
        title_lab = QLabel(title)
        title_lab.setAccessibleName(f"{title}，对话框")
        root.addWidget(title_lab)
        for widget in body_widgets:
            root.addWidget(widget)
        root.addWidget(self._status_label)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._cancel_button = SecondaryButton(cancel_text)
        self._cancel_button.setAccessibleName(cancel_text)
        btn_row.addWidget(self._cancel_button)
        if confirm_danger:
            self._confirm_button = DangerButton(confirm_text)
        else:
            self._confirm_button = PrimaryButton(confirm_text)
        self._confirm_button.setAccessibleName(confirm_text)
        btn_row.addWidget(self._confirm_button)
        root.addLayout(btn_row)

        self._confirm_button.clicked.connect(self._on_confirm)
        self._cancel_button.clicked.connect(self._on_cancel)

        # UI-A10 / 15.3：安全动作（保留/取消）为默认按钮与默认焦点，
        # 危险/确认动作不接受默认回车误触（Enter 永远落到安全动作）。
        self._confirm_button.setAutoDefault(False)
        self._cancel_button.setDefault(True)
        self._cancel_button.setAutoDefault(True)
        self._cancel_button.setFocus()

    def _can_submit(self) -> bool:
        return True

    # -------------------------------------------------------- 确认/取消

    def _on_confirm(self) -> None:
        if self._submitting or not self._can_submit():
            return
        self._submitting = True
        self._confirm_button.setEnabled(False)
        self._confirm_button.set_busy(True)
        self._cancel_button.setEnabled(False)
        self._show_status("处理中…", error=False)
        self.command_requested.emit(self._request)

    def _on_cancel(self) -> None:
        if self._submitting:
            return
        self.reject()

    def _show_status(self, message: str, *, error: bool) -> None:
        self._status_label.setText(message)
        self._status_label.setAccessibleDescription(message)
        self._status_label.show()

    # ------------------------------------------------- 键盘与焦点生命周期

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._submitting and event.key() in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter):
            event.accept()
            return
        super().keyPressEvent(event)

    def done(self, result: int) -> None:  # noqa: N802 - Qt 命名
        super().done(result)
        self._restore_focus()

    def _restore_focus(self) -> None:
        widget = self._return_focus
        if widget is None:
            return
        container = widget

        def _do_restore() -> None:
            try:
                if not container.isVisible():
                    return
                window = container.window()
                if window is not None:
                    window.activateWindow()
                    window.raise_()
                container.setFocus(Qt.OtherFocusReason)
            except RuntimeError:
                pass  # C++ 对象已析构：安全忽略

        # 延后到对话框窗口关闭之后，避免被关闭动作重置焦点（15.3）。
        QTimer.singleShot(0, _do_restore)


class ManualWrapDialog(_ConfirmDialog):
    """DLG01 人工包装：确认请求“人工包装”，不实际包装/复制 Result。"""

    _kind = UiCommandKind.MANUAL_WRAP

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        target_id: str | None = None,
        context: tuple[tuple[str, str], ...] = (),
        return_focus_widget: QWidget | None = None,
        target_title: str = "",
        body_preview: str = "",
        source: str = "",
        protocol_preview: str = "",
    ) -> None:
        super().__init__(
            parent,
            target_id=target_id,
            context=context,
            return_focus_widget=return_focus_widget,
        )
        self._target_title = target_title or "—"
        self._body_preview = body_preview or "—"
        self._source = source or "manual"
        self._protocol_preview = protocol_preview or "—"
        self._confirm_check = QCheckBox("已确认权威影响，提交人工包装")
        self._confirm_check.setAccessibleName("确认权威影响")
        self._confirm_check.toggled.connect(self._on_check_toggled)
        self._assemble(
            title="人工包装",
            body_widgets=[
                _hint(f"目标：{self._target_title}"),
                _hint(f"正文预览：{self._body_preview}"),
                _hint(f"来源：{self._source}（Result source=manual）"),
                _hint(f"协议预览：{self._protocol_preview}"),
                _hint("远端可能仍在执行；权威影响需显式确认后提交。"),
                self._confirm_check,
            ],
            confirm_text="提交人工包装",
            confirm_danger=False,
            cancel_text="取消",
        )
        self.setMinimumWidth(700)
        self.resize(760, 560)
        self.setSizeGripEnabled(True)
        self._confirm_button.setEnabled(False)

    def _on_check_toggled(self, checked: bool) -> None:
        if not self._submitting:
            self._confirm_button.setEnabled(checked)

    def _can_submit(self) -> bool:
        return self._confirm_check.isChecked()


class StopTaskDialog(_ConfirmDialog):
    """DLG02 停止任务：保留任务/停止本地任务；不真正触发停止。"""

    _kind = UiCommandKind.STOP_TASK

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        target_id: str | None = None,
        context: tuple[tuple[str, str], ...] = (),
        return_focus_widget: QWidget | None = None,
        target_title: str = "",
    ) -> None:
        super().__init__(
            parent,
            target_id=target_id,
            context=context,
            return_focus_widget=return_focus_widget,
        )
        self._target_title = target_title or "—"
        self._assemble(
            title="停止当前任务？",
            body_widgets=[
                _hint(f"目标任务：{self._target_title}"),
                _hint(
                    "停止本地等待及后续续接，不等于终止远端执行；"
                    "同项目可能继续隔离。已发出 ≠ 远端必然已经停止。"
                ),
            ],
            confirm_text="停止本地任务",
            confirm_danger=True,
            cancel_text="保留任务",
        )
        self.setMinimumWidth(480)
        self.resize(520, 0)


class NewSessionRetryDialog(_ConfirmDialog):
    """DLG03 新会话重试：明确“新会话”且不冒充“原会话续接”。"""

    _kind = UiCommandKind.NEW_SESSION_RETRY

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        target_id: str | None = None,
        context: tuple[tuple[str, str], ...] = (),
        return_focus_widget: QWidget | None = None,
        target_title: str = "",
    ) -> None:
        super().__init__(
            parent,
            target_id=target_id,
            context=context,
            return_focus_widget=return_focus_widget,
        )
        self._target_title = target_title or "—"
        self._assemble(
            title="新会话重试",
            body_widgets=[
                _hint(f"目标任务：{self._target_title}"),
                _hint("这是新建会话并重新提交原任务，不等同于原会话续接。"),
                _hint("已有文件不会回滚；会话与上下文不会完全继承。"),
            ],
            confirm_text="创建新会话并重试",
            confirm_danger=False,
            cancel_text="取消",
        )
        self.setMinimumWidth(520)
        self.resize(560, 0)


class DiagnosticExportDialog(_ConfirmDialog):
    """DLG04 诊断导出：只确认导出请求，不读取事件/生成文件/真实导出。"""

    _kind = UiCommandKind.DIAGNOSTIC_EXPORT

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        target_id: str | None = None,
        context: tuple[tuple[str, str], ...] = (),
        return_focus_widget: QWidget | None = None,
        target_title: str = "",
    ) -> None:
        super().__init__(
            parent,
            target_id=target_id,
            context=context,
            return_focus_widget=return_focus_widget,
        )
        self._target_title = target_title or "—"
        self._assemble(
            title="导出诊断信息",
            body_widgets=[
                _hint(f"范围：{self._target_title}"),
                _hint("时间范围、包含项与保存位置将在导出服务接入后提供。"),
                _hint("脱敏说明：默认不含全文与密钥；失败不会报成功。"),
            ],
            confirm_text="导出诊断信息",
            confirm_danger=False,
            cancel_text="取消",
        )
        self.setMinimumWidth(600)
        self.resize(640, 0)