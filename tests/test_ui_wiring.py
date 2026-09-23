"""UI ↔ LiteController 接线测试（全 fake，offscreen，禁止真实 HTTP）。

用 FakeController 覆盖 main.wire_ui 的全部接线分支：
 1 refresh session 成功 → 当前会话 label 更新
 2 refresh session 失败 → 显示"无"
 3 model test ready → "正常" + 服务延迟
 4 model 在线但不可 ready → "服务正常" + 说明日志
 5 model 服务断开 → "已断开" + 服务延迟 --
 6 manual send → controller 收到完全原样 text
 7 manual accepted → 日志正确且不更新最近结果
 8 manual failed → 日志显示错误
 9 compact success → 日志成功
10 compact failure → 日志错误
11 listening_changed → 不启动任何真实后台服务（占位日志）
12 auto_compact_changed → 不调用 compact_session
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QPlainTextEdit

from controller import CurrentSession, LiteController, ModelConnectionResult
from main import wire_ui
from openchamber_client import CompactResult, SendResult
from ui.main_window import MainWindow


class FakeController:
    """LiteController 的替身：记录调用、按预设返回，绝不起真实 HTTP。"""

    def __init__(self, session=None, model_result=None, send=None, compact=None) -> None:
        self._session = session if session is not None else CurrentSession(None, None, None, False, "当前激活会话不可用")
        self._model = model_result if model_result is not None else ModelConnectionResult(False, None, False, "timeout")
        self._send = send if send is not None else SendResult(True, None, "msg_1")
        self._compact = compact if compact is not None else CompactResult(True, None)
        self.refresh_calls = 0
        self.model_calls = 0
        self.sent_texts = []
        self.compact_calls = 0

    def get_current_session(self):
        self.refresh_calls += 1
        return self._session

    def test_model_connection(self):
        self.model_calls += 1
        return self._model

    def manual_send(self, text):
        self.sent_texts.append(text)
        return self._send

    def compact_current_session(self):
        self.compact_calls += 1
        return self._compact


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _wired(qapp, controller):
    w = MainWindow()
    w.show()
    wire_ui(w, controller)
    return w


def _log(w) -> str:
    return w.findChild(QPlainTextEdit, "log_box").toPlainText()


def _label(w, name: str) -> str:
    return w.findChild(QLabel, name).text()


def _box(w, name: str) -> str:
    return w.findChild(QPlainTextEdit, name).toPlainText()


def test_refresh_success_updates_label(qapp):
    fake = FakeController(session=CurrentSession("ses_abc", r"C:\w", "src", True, None))
    w = _wired(qapp, fake)
    w.refresh_session_requested.emit()
    assert fake.refresh_calls == 1
    assert _label(w, "session_label") == "当前会话：ses_abc"
    assert "已获取当前激活会话" in _log(w)


def test_refresh_failure_shows_none(qapp):
    fake = FakeController(session=CurrentSession(None, None, None, False, "当前激活会话不可用"))
    w = _wired(qapp, fake)
    w.refresh_session_requested.emit()
    assert _label(w, "session_label") == "当前会话：无"
    assert "当前激活会话不可用" in _log(w)


def test_model_ready_normal_with_latency(qapp):
    fake = FakeController(model_result=ModelConnectionResult(True, 12, True, None))
    w = _wired(qapp, fake)
    w.test_model_requested.emit()
    assert "正常" in _label(w, "llm_status")
    assert "12 ms" in _label(w, "llm_oc")
    assert "-- ms" in _label(w, "llm_first")


def test_model_online_not_ready(qapp):
    fake = FakeController(model_result=ModelConnectionResult(True, 30, False, None))
    w = _wired(qapp, fake)
    w.test_model_requested.emit()
    assert "服务正常" in _label(w, "llm_status")
    assert "OpenChamber 服务正常，但当前会话模型配置不可用" in _log(w)


def test_model_disconnected(qapp):
    fake = FakeController(model_result=ModelConnectionResult(False, None, False, "timeout"))
    w = _wired(qapp, fake)
    w.test_model_requested.emit()
    assert "已断开" in _label(w, "llm_status")
    assert "-- ms" in _label(w, "llm_oc")
    assert "-- ms" in _label(w, "llm_first")


def test_manual_send_text_preserved(qapp):
    fake = FakeController(send=SendResult(True, None, "m_ok"))
    w = _wired(qapp, fake)
    text = "你好 世界\n第二行 保持原样"
    w.manual_send_requested.emit(text)
    assert fake.sent_texts == [text]


def test_manual_accepted_no_recent_result_update(qapp):
    fake = FakeController(send=SendResult(True, None, "m_ok"))
    w = _wired(qapp, fake)
    assert _box(w, "result_box") == "暂无结果"
    w.manual_send_requested.emit("x")
    assert "手动内容已提交到当前会话" in _log(w)
    assert _box(w, "task_box") == "已提交，等待模型处理"
    assert _box(w, "result_box") == "暂无结果"  # accepted != 完成，不更新最近结果


def test_manual_failed_logs_error(qapp):
    fake = FakeController(send=SendResult(False, "http: 500", None))
    w = _wired(qapp, fake)
    w.manual_send_requested.emit("x")
    assert "http: 500" in _log(w)


def test_compact_success_logs(qapp):
    fake = FakeController(compact=CompactResult(True, None))
    w = _wired(qapp, fake)
    w.compact_requested.emit()
    assert fake.compact_calls == 1
    assert "已提交当前会话压缩" in _log(w)


def test_compact_failure_logs(qapp):
    fake = FakeController(compact=CompactResult(False, "http: 404"))
    w = _wired(qapp, fake)
    w.compact_requested.emit()
    assert "压缩失败" in _log(w)
    assert "http: 404" in _log(w)


def test_listening_changed_no_background_service(qapp):
    fake = FakeController()
    w = _wired(qapp, fake)
    w.listening_changed.emit(True)
    assert "自动监听将在后续阶段接入" in _log(w)
    assert fake.compact_calls == 0  # 未启动任何后台 worker，未触发压缩
    assert fake.sent_texts == []


def test_auto_compact_does_not_call_compact(qapp):
    fake = FakeController()
    w = _wired(qapp, fake)
    w.auto_compact_changed.emit(True)
    w.auto_compact_changed.emit(False)
    assert fake.compact_calls == 0  # 只是切换 UI 状态，不真正压缩
    assert "自动压缩已设置为开，后台策略将在后续阶段接入" in _log(w)