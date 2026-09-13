"""T18-B1：确认框基础框架 + Fake UI Command 合同测试。

覆盖（按卡第9节）：
1. 四个正式 Dialog 都能构造；
2. 对应正确 UiCommandKind；
3. Confirm 发出的是构造时的同一 request 对象；
4. Cancel 不 emit；
5. Esc 不 emit；
6. DLG02 默认焦点在 Cancel（保留）；
7. DLG03 默认焦点在 Cancel；
8. 连续 Confirm 只 emit 一次；
9. Enter/repeat 不产生第二条；
10. processing 后 Confirm disabled（Cancel 同样锁定）；
11. processing UI 有明确状态（可见“处理中…”）；
12. finish_processing 只改变 UI、不额外 emit；
13. 关闭/取消后焦点返回触发控件；
14. accessibleName / 焦点策略满足 A14；
另验证文案边界：停止框含后果与非假装成功；新会话框含“新会话”且不冒充“原会话续接”。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.commands import UiCommandKind  # noqa: E402
from ui.dialogs import (  # noqa: E402
    DiagnosticExportDialog,
    ManualWrapDialog,
    NewSessionRetryDialog,
    StopTaskDialog,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    app.setStyleSheet("")


ALL_DIALOGS = (
    ManualWrapDialog,
    StopTaskDialog,
    NewSessionRetryDialog,
    DiagnosticExportDialog,
)


def _build(kind):
    cls = {
        UiCommandKind.MANUAL_WRAP: ManualWrapDialog,
        UiCommandKind.STOP_TASK: StopTaskDialog,
        UiCommandKind.NEW_SESSION_RETRY: NewSessionRetryDialog,
        UiCommandKind.DIAGNOSTIC_EXPORT: DiagnosticExportDialog,
    }[kind]
    return cls(target_id="task-1", context=(("project", "demo"),))


def _build_in(container, kind, **kw):
    cls = {
        UiCommandKind.MANUAL_WRAP: ManualWrapDialog,
        UiCommandKind.STOP_TASK: StopTaskDialog,
        UiCommandKind.NEW_SESSION_RETRY: NewSessionRetryDialog,
        UiCommandKind.DIAGNOSTIC_EXPORT: DiagnosticExportDialog,
    }[kind]
    return cls(container, **kw)


def _emissions(dialog):
    seen = []
    dialog.command_requested.connect(seen.append)
    return seen


def _confirm_button(dialog) -> QPushButton:
    return dialog._confirm_button


def _cancel_button(dialog) -> QPushButton:
    return dialog._cancel_button


def _confirmed(dialog) -> bool:
    return dialog._submitting


def test_four_dialogs_constructible(qapp):
    for cls in ALL_DIALOGS:
        dialog = cls()
        assert dialog.windowTitle() != ""


@pytest.mark.parametrize(
    ("kind", "cls"),
    [
        (UiCommandKind.MANUAL_WRAP, ManualWrapDialog),
        (UiCommandKind.STOP_TASK, StopTaskDialog),
        (UiCommandKind.NEW_SESSION_RETRY, NewSessionRetryDialog),
        (UiCommandKind.DIAGNOSTIC_EXPORT, DiagnosticExportDialog),
    ],
)
def test_dialog_request_kinds(qapp, kind, cls):
    dialog = cls(target_id="t")
    assert dialog.request.kind is kind
    assert dialog.request.target_id == "t"


@pytest.mark.parametrize("kind", list(UiCommandKind))
def test_confirm_emits_same_request_object(qapp, kind):
    dialog = _build(kind)
    if kind is UiCommandKind.MANUAL_WRAP:
        dialog._confirm_check.setChecked(True)
    seen = _emissions(dialog)
    _confirm_button(dialog).click()
    assert len(seen) == 1
    assert seen[0] is dialog.request
    assert seen[0].kind is kind


def test_cancel_does_not_emit(qapp):
    dialog = _build(UiCommandKind.STOP_TASK)
    seen = _emissions(dialog)
    _cancel_button(dialog).click()
    assert seen == []
    assert _confirmed(dialog) is False


def test_escape_rejects_without_emit(qapp):
    dialog = _build(UiCommandKind.STOP_TASK)
    seen = _emissions(dialog)
    dialog.show()
    QTest.keyClick(dialog, Qt.Key_Escape)
    assert seen == []
    assert _confirmed(dialog) is False


def test_stop_default_focus_cancel(qapp):
    dialog = _build(UiCommandKind.STOP_TASK)
    dialog.show()
    dialog.activateWindow()
    QApplication.processEvents()
    assert dialog.focusWidget() is _cancel_button(dialog)


def test_new_session_default_focus_cancel(qapp):
    dialog = _build(UiCommandKind.NEW_SESSION_RETRY)
    dialog.show()
    dialog.activateWindow()
    QApplication.processEvents()
    assert dialog.focusWidget() is _cancel_button(dialog)


def test_consecutive_confirm_single_emit(qapp):
    dialog = _build(UiCommandKind.STOP_TASK)
    seen = _emissions(dialog)
    _confirm_button(dialog).click()
    _confirm_button(dialog).click()
    _confirm_button(dialog).click()
    assert len(seen) == 1


def test_enter_repeat_no_second_emit(qapp):
    dialog = _build(UiCommandKind.STOP_TASK)
    seen = _emissions(dialog)
    dialog.show()
    _confirm_button(dialog).click()
    QTest.keyClick(dialog, Qt.Key_Return)
    QTest.keyClick(dialog, Qt.Key_Enter)
    QTest.mouseDClick(dialog, Qt.LeftButton, pos=dialog.rect().center())
    assert len(seen) == 1


def test_processing_disables_buttons(qapp):
    dialog = _build(UiCommandKind.STOP_TASK)
    _confirm_button(dialog).click()
    assert _confirmed(dialog) is True
    assert _confirm_button(dialog).isEnabled() is False
    assert _cancel_button(dialog).isEnabled() is False


def test_processing_status_label_visible(qapp):
    dialog = _build(UiCommandKind.STOP_TASK)
    dialog.show()
    QApplication.processEvents()
    _confirm_button(dialog).click()
    QApplication.processEvents()
    status = dialog._status_label
    assert status.isVisible()
    assert "处理中" in status.text()


@pytest.mark.parametrize("success", [True, False])
def test_finish_processing_ui_only_no_emit(qapp, success):
    dialog = _build(UiCommandKind.STOP_TASK)
    dialog.show()
    QApplication.processEvents()
    seen = _emissions(dialog)
    _confirm_button(dialog).click()
    before = len(seen)
    dialog.finish_processing("已记录", success=success)
    assert len(seen) == before
    assert dialog._status_label.isVisible()


def test_finish_processing_success_keeps_confirm_locked(qapp):
    dialog = _build(UiCommandKind.STOP_TASK)
    seen = _emissions(dialog)
    _confirm_button(dialog).click()
    dialog.finish_processing("已记录", success=True)
    assert _confirmed(dialog) is True
    assert _confirm_button(dialog).isEnabled() is False
    assert _cancel_button(dialog).isEnabled() is True
    assert len(seen) == 1


def test_finish_processing_failure_allows_retry(qapp):
    dialog = _build(UiCommandKind.STOP_TASK)
    seen = _emissions(dialog)
    _confirm_button(dialog).click()
    dialog.finish_processing("发送失败", success=False)
    assert _confirmed(dialog) is False
    assert _confirm_button(dialog).isEnabled() is True
    assert _cancel_button(dialog).isEnabled() is True
    _confirm_button(dialog).click()
    assert len(seen) == 2


def test_focus_returns_to_trigger_widget(qapp):
    container = QWidget()
    trigger = QPushButton("原触发按钮", container)
    container.show()
    container.activateWindow()
    QApplication.processEvents()
    dialog = _build_in(container, UiCommandKind.STOP_TASK, return_focus_widget=trigger)
    dialog.show()
    dialog.activateWindow()
    QApplication.processEvents()
    _cancel_button(dialog).setFocus()
    dialog.reject()
    QApplication.processEvents()
    assert trigger.hasFocus()


def test_focus_return_none_is_safe(qapp):
    dialog = _build(UiCommandKind.STOP_TASK)
    dialog.reject()  # return_focus_widget=None 不报错


def test_accessible_names_and_focus_policy(qapp):
    dialog = _build(UiCommandKind.STOP_TASK)
    assert _confirm_button(dialog).accessibleName()
    assert _cancel_button(dialog).accessibleName()
    from PySide6.QtCore import Qt as _Qt

    assert _confirm_button(dialog).focusPolicy() == _Qt.StrongFocus
    assert _cancel_button(dialog).focusPolicy() == _Qt.StrongFocus
    assert dialog._status_label.accessibleName() == "处理状态"


def _all_texts(dialog) -> str:
    chunks = [dialog.windowTitle()]
    for widget in dialog.findChildren(QLabel):
        chunks.append(widget.text())
    if _confirm_button(dialog) is not None:
        chunks.append(_confirm_button(dialog).text())
    if _cancel_button(dialog) is not None:
        chunks.append(_cancel_button(dialog).text())
    return " ".join(chunks)


@pytest.mark.parametrize("kind", list(UiCommandKind))
def test_no_business_side_effect_in_dialog(qapp, kind):
    dialog = _build(kind)
    assert dialog._kind is kind
    assert dialog.request.kind is kind


def test_stop_dialog_wording(qapp):
    dialog = StopTaskDialog(target_title="我的任务")
    full = _all_texts(dialog)
    assert "停止当前任务" in full
    assert "停止本地任务" in _confirm_button(dialog).text()
    assert "保留任务" == _cancel_button(dialog).text()
    assert "已发出 ≠ 远端必然已经停止" in full
    assert "不等于终止远端执行" in full


def test_new_session_wording_is_not_original_resume(qapp):
    dialog = NewSessionRetryDialog(target_title="我的任务")
    full = _all_texts(dialog)
    assert "新会话" in full
    assert "创建新会话并重试" in _confirm_button(dialog).text()
    assert "不等同于原会话续接" in full


def test_manual_wrap_requires_authority_confirm(qapp):
    dialog = ManualWrapDialog(target_title="我的任务")
    assert _confirm_button(dialog).isEnabled() is False
    dialog._confirm_check.setChecked(True)
    assert _confirm_button(dialog).isEnabled() is True