"""UI ↔ LiteController + ClipLinkBridge 接线测试（全 fake，offscreen，禁止真实 HTTP）。

用 FakeController + FakeBridge 覆盖 main.wire_ui 的全部接线分支：
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
 11 listening_changed → bridge.set_listening() 被调用 + 日志
 12 auto_compact_changed → 不调用 compact_session
 13 remote task → UI 更新 + 不调用模型发送
 14 refresh session → bridge.set_session_id() 被调用
 15 test model → bridge.set_model_status() 被调用
"""

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QPlainTextEdit

from cliplink_bridge import ClipLinkBridge, RemoteTask
from cliplink_status import now_millis
from controller import CurrentSession, LiteController, ModelConnectionResult
from main import apply_cliplink_status, start_bridge_poll, start_cliplink_poll, wire_ui
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


class FakeBridge:
    """ClipLinkBridge 的替身：记录调用，不读写真实文件。"""

    def __init__(self) -> None:
        self.listening_calls: list[bool] = []
        self.session_ids: list[str | None] = []
        self.model_statuses: list[tuple] = []
        self.on_remote_task = None

    def set_listening(self, enabled: bool) -> None:
        self.listening_calls.append(enabled)

    def set_session_id(self, session_id: str | None) -> None:
        self.session_ids.append(session_id)

    def set_model_status(self, status: str, latency_ms: int | None = None) -> None:
        self.model_statuses.append((status, latency_ms))

    def tick(self) -> None:
        pass


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _wired(qapp, controller, bridge=None):
    w = MainWindow()
    w.show()
    wire_ui(w, controller, bridge if bridge is not None else FakeBridge())
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


def test_listening_changed_calls_bridge_and_logs(qapp):
    fake = FakeController()
    bridge = FakeBridge()
    w = _wired(qapp, fake, bridge)
    w.listening_changed.emit(True)
    assert bridge.listening_calls == [True]
    assert "自动监听已开始" in _log(w)
    assert fake.compact_calls == 0
    assert fake.sent_texts == []
    w.listening_changed.emit(False)
    assert bridge.listening_calls == [True, False]
    assert "自动监听已停止" in _log(w)


def test_auto_compact_does_not_call_compact(qapp):
    fake = FakeController()
    w = _wired(qapp, fake)
    w.auto_compact_changed.emit(True)
    w.auto_compact_changed.emit(False)
    assert fake.compact_calls == 0  # 只是切换 UI 状态，不真正压缩
    assert "自动压缩已设置为开，后台策略将在后续阶段接入" in _log(w)


def test_remote_task_updates_ui_without_model(qapp):
    fake = FakeController()
    bridge = FakeBridge()
    w = _wired(qapp, fake, bridge)
    w.listening_changed.emit(True)
    task = RemoteTask("evt_new", "A端任务内容", "hash1", 12345)
    bridge.on_remote_task(task)
    assert _box(w, "task_box") == "已收到A端任务，等待自动处理"
    assert "收到 A端新任务" in _log(w)
    assert fake.sent_texts == []
    assert fake.compact_calls == 0


def test_refresh_session_calls_bridge_set_session_id(qapp):
    fake = FakeController(session=CurrentSession("ses_abc", r"C:\w", "src", True, None))
    bridge = FakeBridge()
    w = _wired(qapp, fake, bridge)
    w.refresh_session_requested.emit()
    assert bridge.session_ids == ["ses_abc"]


def test_refresh_session_failure_sets_session_id_none(qapp):
    fake = FakeController(session=CurrentSession(None, None, None, False, "不可用"))
    bridge = FakeBridge()
    w = _wired(qapp, fake, bridge)
    w.refresh_session_requested.emit()
    assert bridge.session_ids == [None]


def test_model_test_calls_bridge_set_model_status(qapp):
    fake = FakeController(model_result=ModelConnectionResult(True, 15, True, None))
    bridge = FakeBridge()
    w = _wired(qapp, fake, bridge)
    w.test_model_requested.emit()
    assert bridge.model_statuses == [("ready", 15)]


def test_model_disconnected_calls_bridge(qapp):
    fake = FakeController(model_result=ModelConnectionResult(False, None, False, "timeout"))
    bridge = FakeBridge()
    w = _wired(qapp, fake, bridge)
    w.test_model_requested.emit()
    assert bridge.model_statuses == [("disconnected", None)]


# --- A端连接卡 ← ClipLink status.json 接线（全 tmp 文件，offscreen）-----


def _write_cliplink_status(path: Path, **fields) -> Path:
    base = {
        "version": 1,
        "status": "connected",
        "peer_name": None,
        "peer_ip": None,
        "latency_ms": None,
        "generation": 0,
        "updated_at": 1000,
    }
    base.update(fields)
    path.write_text(json.dumps(base), encoding="utf-8")
    return path


def test_cliplink_connected_shows_peer_and_latency(qapp, tmp_path):
    w = MainWindow()
    w.show()
    p = _write_cliplink_status(
        tmp_path / "status.json",
        status="connected",
        peer_name="A端PC",
        peer_ip="192.168.1.5",
        latency_ms=42,
        updated_at=1000,
    )
    apply_cliplink_status(w, path=p, now_ms=2000)  # 距今 1s，未过旧
    assert "已连接" in _label(w, "a_status")
    assert _label(w, "a_peer") == "对端：A端PC (192.168.1.5)"
    assert "42 ms" in _label(w, "a_latency")


def test_cliplink_stale_connection_shows_disconnected(qapp, tmp_path):
    w = MainWindow()
    w.show()
    p = _write_cliplink_status(
        tmp_path / "status.json", status="connected", peer_name="A端PC", latency_ms=42, updated_at=1000
    )
    apply_cliplink_status(w, path=p, now_ms=1000 + 31_000)  # 31s 无更新 → 假连接
    assert "已断开(过旧)" in _label(w, "a_status")
    assert "-- ms" in _label(w, "a_latency")


def test_cliplink_missing_file_shows_offline(qapp, tmp_path):
    w = MainWindow()
    w.show()
    apply_cliplink_status(w, path=tmp_path / "absent.json", now_ms=5_000)
    assert "未连接" in _label(w, "a_status")
    assert _label(w, "a_peer") == "对端：--"
    assert "-- ms" in _label(w, "a_latency")


def test_start_cliplink_poll_refreshes_first_frame(qapp, tmp_path):
    w = MainWindow()
    w.show()
    p = _write_cliplink_status(
        tmp_path / "status.json",
        status="connected",
        peer_name="A端PC",
        latency_ms=15,
        updated_at=now_millis(),  # 与真实时钟同步 → 首帧未过旧
    )
    timer = start_cliplink_poll(w, interval_ms=60_000, path=p)  # 长间隔，避免测试期间再触发
    assert "已连接" in _label(w, "a_status")
    assert "15 ms" in _label(w, "a_latency")
    assert timer.isActive()
    timer.stop()