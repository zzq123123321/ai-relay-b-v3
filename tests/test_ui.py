"""AI Relay B Lite 单页 UI 骨架测试（offscreen，不起真实服务、不 import OpenChamberClient）。

覆盖任务要求 12 项：
 1 窗口可构造（标题/默认尺寸）
 2 页面不存在 Agent/Model/工作目录 等禁止控件
 3 A端连接、大模型连接两个状态区存在
 4 当前会话控件存在
 5 自动压缩是可勾选控件
 6 手动输入框存在
 7 发送按钮触发 manual_send_requested 且文本不修改
 8 Ctrl+Enter 触发同一 signal
 9 开始监听 ↔ 停止监听切换正确
 10 包装内容弹窗可打开
 11 总指挥模板弹窗可打开（含关键内容）
 12 总指挥模板“填入发送框”不自动发送
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QLabel,
    QPlainTextEdit,
    QPushButton,
)

from ui.main_window import CommanderDialog, MainWindow, WrapperDialog

FORBIDDEN = ["Agent", "Model", "工作目录", "Reasonix", "FIXED_SESSION", "PROJECT_ROTATING"]


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def win(qapp):
    w = MainWindow()
    w.show()
    yield w
    w.close()


def _visible_texts(w) -> str:
    out = [w.windowTitle()]
    for cls in (QLabel, QPushButton, QCheckBox):
        for it in w.findChildren(cls):
            out.append(it.text())
    for it in w.findChildren(QPlainTextEdit):
        out.append(it.toPlainText())
        out.append(it.placeholderText())
    return " ".join(out)


def test_window_constructs(qapp):
    w = MainWindow()
    assert w.windowTitle() == "AI Relay B Lite"
    assert w.width() == 900
    assert w.height() == 760
    assert issubclass(type(w), MainWindow)
    w.close()


def test_forbidden_fields_absent(win):
    text = _visible_texts(win).upper()
    for tok in FORBIDDEN:
        assert tok.upper() not in text, f"出现禁止字段: {tok}"


def test_connection_cards_exist(win):
    assert win.findChild(QLabel, "a_status") is not None
    assert win.findChild(QLabel, "llm_status") is not None
    visible = _visible_texts(win)
    assert "A端连接" in visible
    assert "大模型连接" in visible


def test_current_session_control(win):
    lbl = win.findChild(QLabel, "session_label")
    assert lbl is not None
    assert "当前会话" in lbl.text()


def test_auto_compact_is_checkable(win):
    chk = win.findChild(QCheckBox, "chk_auto")
    assert chk is not None
    assert chk.isCheckable()
    assert chk.text() == "自动压缩会话"
    fired = []
    win.auto_compact_changed.connect(fired.append)
    assert not chk.isChecked()
    chk.toggle()
    assert fired == [True]
    chk.toggle()
    assert fired == [True, False]


def test_manual_input_exists(win):
    assert win.findChild(QPlainTextEdit, "manual_input") is not None


def test_send_button_emits_unmodified(win):
    edit = win.findChild(QPlainTextEdit, "manual_input")
    edit.setPlainText("hello 世界\n第二行 内容")
    captured = []
    win.manual_send_requested.connect(captured.append)
    win.findChild(QPushButton, "btn_send").click()
    assert captured == ["hello 世界\n第二行 内容"]


def test_ctrl_enter_emits_same_signal(win):
    edit = win.findChild(QPlainTextEdit, "manual_input")
    edit.setPlainText("via ctrl+enter")
    edit.setFocus()
    captured = []
    win.manual_send_requested.connect(captured.append)
    from PySide6.QtCore import Qt

    QTest.keyClick(edit, Qt.Key_Return, Qt.ControlModifier)
    assert captured == ["via ctrl+enter"]


def test_listening_toggle(win):
    btn = win.findChild(QPushButton, "btn_listen")
    fired = []
    win.listening_changed.connect(fired.append)
    assert btn.text() == "开始监听"
    btn.click()
    assert btn.text() == "停止监听"
    assert fired == [True]
    btn.click()
    assert btn.text() == "开始监听"
    assert fired == [True, False]


def test_wrapper_dialog_opens(win):
    dlg = win.findChild(QDialog, "wrapper_dialog")
    assert isinstance(dlg, WrapperDialog)
    assert dlg.isHidden()
    win.findChild(QPushButton, "btn_wrapper").click()
    assert not dlg.isHidden()


def test_commander_dialog_opens(win):
    dlg = win.findChild(QDialog, "commander_dialog")
    assert isinstance(dlg, CommanderDialog)
    assert dlg.isHidden()
    win.findChild(QPushButton, "btn_commander").click()
    assert not dlg.isHidden()
    txt = dlg.findChild(QPlainTextEdit, "commander_edit").toPlainText()
    assert "总指挥" in txt
    assert "AI_RELAY_COMPLETE" in txt


def test_commander_fill_no_autosend(win):
    dlg = win.findChild(QDialog, "commander_dialog")
    edit = dlg.findChild(QPlainTextEdit, "commander_edit")
    edit.setPlainText("自定义总指挥指令 123")
    sends = []
    win.manual_send_requested.connect(sends.append)
    dlg.findChild(QPushButton, "commander_fill").click()
    assert win.findChild(QPlainTextEdit, "manual_input").toPlainText() == "自定义总指挥指令 123"
    assert sends == []  # 只写入手动框，未自动发送