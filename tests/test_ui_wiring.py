"""UI ↔ LiteController + ClipLinkBridge 接线测试（全 fake，offscreen，禁止真实 HTTP）。

用 FakeController + FakeBridge 覆盖 main.wire_ui 的全部接线分支：
  1 refresh session 成功 → 内部会话/日志更新
  2 refresh session 失败 → 内部会话置空
  3 model test ready → bridge ready + 日志（不覆盖 probe 连接显示）
  4 model 在线但不可 ready → bridge service_only + 说明日志
  5 model 服务断开 → bridge disconnected + 日志（连接显示归 probe 管）
  6 manual send → 立即包装+写剪贴板（阶段 4A：0 HTTP、不走 manual_send）
  7 manual 空白/自动任务/单槽占用 → 不写剪贴板、明确提示
  8 wrap 内容按钮 → 读剪贴板写回、不弹窗、零模型/HTTP
  9 compact success → 日志成功
 10 compact failure → 日志错误
 11 listening_changed → bridge.set_listening() 被调用 + 日志
 12 auto_compact_changed → 不调用 compact_session
 13 remote task → 完整包提交给 monitor；普通文本/半包/空白包忽略；严格 COMPLETE 停监听
 14 refresh session → bridge.set_session_id() 被调用
 15 test model → bridge.set_model_status() 被调用
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QPlainTextEdit, QPushButton

from cliplink_bridge import ClipLinkBridge, RemoteTask, extract_envelope
from cliplink_status import now_millis
from controller import (
    AutoTaskIdentity,
    AutoTaskIntake,
    CurrentSession,
    LiteController,
    ModelConnectionResult,
)
from main import (
    apply_cliplink_status,
    initialize_startup,
    process_monitor_events,
    start_bridge_poll,
    start_cliplink_poll,
    start_monitor_poll,
    wire_ui,
)
from openchamber_client import CompactResult, SendResult
from ui.main_window import MainWindow

VALID_ID = AutoTaskIdentity("task-1", 0, 3, True, None)
INVALID_ID = AutoTaskIdentity("", 0, 0, False, "缺少头部：TASK_ID")


class FakeController:
    """LiteController 的替身：记录调用、按预设返回，绝不起真实 HTTP。"""

    def __init__(self, session=None, model_result=None, send=None, compact=None, auto=None) -> None:
        self._session = session if session is not None else CurrentSession(None, None, None, False, "当前激活会话不可用")
        self._model = model_result if model_result is not None else ModelConnectionResult(False, None, False, "timeout")
        self._send = send if send is not None else SendResult(True, None, "msg_1")
        self._compact = compact if compact is not None else CompactResult(True, None)
        self._auto = auto  # 预设 AutoTaskIntake；None 时按 model 推导
        self._auto_task = None  # 测试可注入 SimpleNamespace(event_id=...) 模拟活动任务
        self.auto_active = False  # 阶段 4A：auto_task_active() 返回值可预设
        self.refresh_calls = 0
        self.model_calls = 0
        self.sent_texts = []
        self.compact_calls = 0
        self.auto_compact_calls: list[str] = []
        self.finish_calls: list[str] = []
        self.auto_intakes: list[tuple[str, str, str]] = []

    def get_current_session(self):
        self.refresh_calls += 1
        return self._session

    def test_model_connection(self):
        self.model_calls += 1
        return self._model

    def manual_send(self, text):
        self.sent_texts.append(text)
        return self._send

    def auto_task_active(self):
        return self.auto_active

    def compact_current_session(self):
        self.compact_calls += 1
        return self._compact

    def receive_auto_task(self, event_id, raw_text, wrapper_template):
        self.auto_intakes.append((event_id, raw_text, wrapper_template))
        if self._auto is not None:
            return self._auto
        # 默认：模型 ready 则视为已提交，否则仅包装等待
        if self._model.connected and self._model.ready:
            return AutoTaskIntake(True, True)
        return AutoTaskIntake(True, False)

    def finish_auto_task(self, event_id) -> bool:
        self.finish_calls.append(event_id)
        return True

    def compact_auto_task(self, event_id):
        self.auto_compact_calls.append(event_id)
        return self._compact


class FakeBridge:
    """ClipLinkBridge 的替身：记录调用，不读写真实文件。"""

    def __init__(self) -> None:
        self.listening_calls: list[bool] = []
        self.session_ids: list[str | None] = []
        self.model_statuses: list[tuple] = []
        self.first_response_calls: list = []
        self.delivered: list[str] = []
        self.deliver_calls: list[tuple[str, str | None]] = []
        self.resolve_calls: list[tuple] = []  # (event_id, accepted, attempt)
        self.inflight_attempt = None  # 测试可设置，模拟 Bridge 在途轮次
        self.on_remote_task = None
        self.on_local_write = None
        self.outcome = "flushed"  # 测试可改为 "pending"/"rejected"/"duplicate"
        self.result_occupied = False  # 阶段 4A：result_slot_occupied() 返回值可预设

    def result_slot_occupied(self) -> bool:
        return self.result_occupied

    def set_listening(self, enabled: bool) -> None:
        self.listening_calls.append(enabled)

    def set_session_id(self, session_id: str | None) -> None:
        self.session_ids.append(session_id)

    def set_model_status(self, status: str, latency_ms: int | None = None) -> None:
        self.model_statuses.append((status, latency_ms))

    def set_model_first_response(self, latency_ms) -> None:
        self.first_response_calls.append(latency_ms)

    def deliver_result(self, text: str, event_id: str | None = None) -> str:
        self.delivered.append(text)
        self.deliver_calls.append((text, event_id))
        if self.outcome == "flushed" and self.on_local_write is not None:
            self.on_local_write(event_id)
        return self.outcome

    def resolve_inflight(self, event_id, accepted: bool, attempt=None) -> bool:
        self.resolve_calls.append((event_id, accepted, attempt))
        return True

    def tick(self) -> None:
        pass


class FakeMonitor:
    """AutoMonitor 替身：记录 submit/ack，可手动 push 事件供 process_monitor_events drain。"""

    def __init__(self, *args, **kwargs) -> None:
        self.submit_calls: list[tuple[str, str, str]] = []
        self.submit_attempts: list = []  # 阶段 3F-2：内部 attempt 令牌（None=无令牌兼容）
        self.ack_calls: list[str] = []
        self.stop_calls = 0
        self.auto_compact_calls: list[bool] = []
        self._events: list[dict] = []

    def submit_remote_task(self, event_id: str, text: str, wrapper_template: str,
                           attempt=None) -> None:
        self.submit_calls.append((event_id, text, wrapper_template))
        self.submit_attempts.append(attempt)

    def set_auto_compact(self, enabled: bool) -> None:
        self.auto_compact_calls.append(bool(enabled))

    def acknowledge_result(self, event_id: str) -> None:
        self.ack_calls.append(event_id)

    def stop(self) -> None:
        self.stop_calls += 1

    def start(self) -> None:
        pass  # 真实 AutoMonitor 的后台轮询由 fake 事件 push 代替

    def drain_events(self) -> list[dict]:
        out = self._events
        self._events = []
        return out

    def push(self, ev: dict) -> None:
        self._events.append(ev)


class FakeClipboard:
    """阶段 4A 可注入内存剪贴板读写器：记录读/写次数，可预设读取/写入异常。"""

    def __init__(self, text: str | None = None, read_error=None, write_error=None) -> None:
        self._text = text
        self.reads = 0
        self.writes: list[str] = []
        self._read_error = read_error
        self._write_error = write_error

    def text(self) -> str | None:
        self.reads += 1
        if self._read_error is not None:
            raise self._read_error
        return self._text

    def setText(self, value: str) -> None:
        if self._write_error is not None:
            raise self._write_error
        self._text = value
        self.writes.append(value)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _wired(qapp, controller, bridge=None, monitor=None, clipboard=None):
    w = MainWindow()
    w.show()
    wire_ui(w, controller, bridge if bridge is not None else FakeBridge(), monitor,
            clipboard=clipboard)
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
    w.findChild(QPushButton, "btn_refresh").click()
    assert fake.refresh_calls == 1
    assert _label(w, "session_label") == "当前会话：ses_abc"
    notice = w.findChild(QLabel, "session_notice")
    assert notice.isVisible()
    assert notice.text() == "已获取当前激活会话：ses_abc"
    assert "已获取当前激活会话" in _log(w)


def test_refresh_failure_shows_none(qapp):
    fake = FakeController(session=CurrentSession(None, None, None, False, "当前激活会话不可用"))
    w = _wired(qapp, fake)
    w.findChild(QPushButton, "btn_refresh").click()
    assert _label(w, "session_label") == "当前会话：无"
    notice = w.findChild(QLabel, "session_notice")
    assert notice.isVisible()
    assert notice.text() == "未获取当前激活会话：当前激活会话不可用"
    assert "当前激活会话不可用" in _log(w)

def test_refresh_error_is_visible_without_overwriting_task(qapp, monkeypatch):
    fake = FakeController()
    bridge = FakeBridge()
    w = _wired(qapp, fake, bridge)
    w.set_current_task("自动任务正在处理")

    def failed_read():
        raise OSError("会话读取失败")

    monkeypatch.setattr(fake, "get_current_session", failed_read)
    w.findChild(QPushButton, "btn_refresh").click()
    assert w.findChild(QLabel, "session_notice").text() == "获取当前激活会话失败：会话读取失败"
    assert w.findChild(QLabel, "session_notice").isVisible()
    assert _box(w, "task_box") == "自动任务正在处理"
    assert bridge.session_ids == []
    assert "会话读取失败" in _log(w)


def test_model_ready_normal_with_latency(qapp):
    fake = FakeController(model_result=ModelConnectionResult(True, 12, True, None))
    bridge = FakeBridge()
    w = _wired(qapp, fake, bridge)
    w.test_model_requested.emit()
    assert bridge.model_statuses == [("ready", 12)]
    assert "大模型连接正常" in _log(w)
    # 手动测试不再覆盖 probe 驱动的“已连接/未连接”显示（旧测试入口已移除）
    assert "未检测" in _label(w, "llm_status")
    assert "-- ms" in _label(w, "llm_oc")
    assert "-- ms" in _label(w, "llm_first")


def test_model_online_not_ready(qapp):
    fake = FakeController(model_result=ModelConnectionResult(True, 30, False, None))
    bridge = FakeBridge()
    w = _wired(qapp, fake, bridge)
    w.test_model_requested.emit()
    assert bridge.model_statuses == [("service_only", 30)]
    assert "OpenChamber 服务正常，但当前会话模型配置不可用" in _log(w)
    assert "未检测" in _label(w, "llm_status")


def test_model_disconnected(qapp):
    fake = FakeController(model_result=ModelConnectionResult(False, None, False, "timeout"))
    bridge = FakeBridge()
    w = _wired(qapp, fake, bridge)
    w.test_model_requested.emit()
    assert bridge.model_statuses == [("disconnected", None)]
    assert "OpenChamber 服务不可达（timeout）" in _log(w)
    assert "未检测" in _label(w, "llm_status")
    assert "-- ms" in _label(w, "llm_oc")
    assert "-- ms" in _label(w, "llm_first")


# --- 阶段 4A：发送/包装内容 = 立即包装 + 写本机剪贴板（全 fake，0 HTTP） ---


def _assert_official_wrap(out: str, body: str) -> None:
    """断言 out 是正式 legacy 回包：固定头 + 独立 manual 事件 ID + 默认轮次 + 正文逐字。"""
    lines = out.split("\n")
    assert lines[0] == "----- AI_RELAY_BEGIN -----"
    assert lines[1] == "SOURCE: EXECUTOR"
    assert lines[2] == "TARGET: CHATGPT"
    assert lines[3] == "TYPE: RESPONSE"
    assert lines[4].startswith("TASK_ID: manual-")  # 独立 manual 事件 ID，不冒用自动任务身份
    assert lines[5] == "ROUND: 0"
    assert lines[6] == "MAX_ROUNDS: 3"
    assert len(lines[7]) > len("TIME: ") and lines[7].startswith("TIME: ")
    assert lines[8] == "CONTENT:"
    assert "\n".join(lines[9:-1]) == body  # 正文逐字保留（不 strip、不丢换行/协议文本）
    assert lines[-1] == "----- AI_RELAY_END -----"


def test_manual_send_wraps_input_and_copies_once(qapp):
    # 发送：读输入框正文（不读剪贴板）→ 正式包装 → 恰一次写剪贴板；
    # 零模型/HTTP（不 manual_send、不查会话）；输入框保留原文；成功提示可见。
    fake = FakeController()
    clip = FakeClipboard(text="剪贴板里已有的旧内容")
    w = _wired(qapp, fake, FakeBridge(), None, clipboard=clip)
    text = "你好 世界\n第二行 保持原样"
    w.findChild(QPlainTextEdit, "manual_input").setPlainText(text)
    w.findChild(QPushButton, "btn_send").click()  # 真实按钮点击
    assert fake.sent_texts == [] and fake.refresh_calls == 0 and fake.model_calls == 0
    assert len(clip.writes) == 1
    _assert_official_wrap(clip.writes[0], text)
    assert _box(w, "manual_input") == text  # 输入框保留原文
    assert _box(w, "task_box") == "已包装并复制到剪贴板"
    assert "已包装并复制到剪贴板" in _log(w)


def test_manual_send_empty_input_no_clipboard_write(qapp):
    # 空白输入：不写剪贴板（保留原剪贴板），当前页可见提示，不虚报成功
    fake = FakeController()
    clip = FakeClipboard(text="保留的旧剪贴板")
    w = _wired(qapp, fake, FakeBridge(), None, clipboard=clip)
    w.manual_send_requested.emit("   ")
    assert clip.writes == []
    assert _box(w, "task_box") == "输入内容为空，未包装"
    assert "输入内容为空，未包装" in _log(w)
    assert fake.sent_texts == []


def test_manual_send_prioritized_while_auto_task_active(qapp, monkeypatch):
    fake = FakeController()
    fake.auto_active = True
    clip = FakeClipboard(text="旧内容")
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon, clipboard=clip)
    w.set_listening(True)
    monkeypatch.setattr(fake, "auto_task_active", lambda: pytest.fail("人工入口不检查自动任务"))
    monkeypatch.setattr(bridge, "result_slot_occupied", lambda: pytest.fail("人工入口不检查回传单槽"))
    w.manual_send_requested.emit("内容")
    assert len(clip.writes) == 1
    _assert_official_wrap(clip.writes[0], "内容")
    assert _box(w, "task_box") == "已包装并复制到剪贴板"
    assert mon.ack_calls == [] and mon.submit_calls == []
    assert bridge.deliver_calls == []
    assert w._listening is True


def test_manual_send_prioritized_while_result_slot_occupied(qapp):
    fake = FakeController()
    clip = FakeClipboard(text="旧内容")
    bridge = FakeBridge()
    bridge.result_occupied = True
    w = _wired(qapp, fake, bridge, None, clipboard=clip)
    w.manual_send_requested.emit("内容")
    assert len(clip.writes) == 1
    _assert_official_wrap(clip.writes[0], "内容")
    assert bridge.result_slot_occupied() is True
    assert bridge.deliver_calls == []
    assert _box(w, "task_box") == "已包装并复制到剪贴板"


def test_wrap_clipboard_button_wraps_clipboard_in_place(qapp):
    # 包装内容：真实按钮点击 → 读剪贴板 → 同一正式包装 → 写回剪贴板；
    # 不要求先填输入框、零模型/HTTP
    fake = FakeController()
    clip = FakeClipboard(text="剪贴板原文\n第二行")
    w = _wired(qapp, fake, FakeBridge(), None, clipboard=clip)
    assert w.findChild(QPushButton, "btn_wrapper_edit") is None
    w.findChild(QPushButton, "btn_wrapper").click()
    assert len(clip.writes) == 1
    _assert_official_wrap(clip.writes[0], "剪贴板原文\n第二行")
    assert fake.sent_texts == [] and fake.model_calls == 0 and fake.refresh_calls == 0
    assert _box(w, "task_box") == "已包装并复制到剪贴板"
    assert "已包装并复制到剪贴板" in _log(w)


def test_wrap_clipboard_empty_no_write(qapp):
    fake = FakeController()
    clip = FakeClipboard(text="")
    w = _wired(qapp, fake, FakeBridge(), None, clipboard=clip)
    w.findChild(QPushButton, "btn_wrapper").click()
    assert clip.writes == []
    assert "剪贴板无文本" in _box(w, "task_box")
    assert "剪贴板无文本" in _log(w)


def test_wrap_clipboard_read_error_visible(qapp):
    # 读取剪贴板异常：可见错误、不写入、不虚报成功、不崩溃
    fake = FakeController()
    clip = FakeClipboard(read_error=OSError("read fail"))
    w = _wired(qapp, fake, FakeBridge(), None, clipboard=clip)
    w.findChild(QPushButton, "btn_wrapper").click()
    assert clip.writes == []
    assert "读取剪贴板失败" in _box(w, "task_box")
    assert "读取剪贴板失败" in _log(w)
    assert "已包装并复制到剪贴板" not in _log(w)


def test_wrap_clipboard_write_error_visible_no_false_success(qapp):
    # 写入剪贴板异常：可见错误、不虚报成功
    fake = FakeController()
    clip = FakeClipboard(text="内容", write_error=OSError("write fail"))
    w = _wired(qapp, fake, FakeBridge(), None, clipboard=clip)
    w.findChild(QPushButton, "btn_wrapper").click()
    assert clip.writes == []
    assert "写入剪贴板失败" in _box(w, "task_box")
    assert "写入剪贴板失败" in _log(w)
    assert "已包装并复制到剪贴板" not in _log(w)


def test_wrap_clipboard_prioritized_while_auto_task_active(qapp, monkeypatch):
    fake = FakeController()
    fake.auto_active = True
    clip = FakeClipboard(text="旧内容")
    mon = FakeMonitor()
    bridge = FakeBridge()
    w = _wired(qapp, fake, bridge, mon, clipboard=clip)
    w.set_listening(True)
    monkeypatch.setattr(fake, "auto_task_active", lambda: pytest.fail("人工入口不检查自动任务"))
    monkeypatch.setattr(bridge, "result_slot_occupied", lambda: pytest.fail("人工入口不检查回传单槽"))
    w.findChild(QPushButton, "btn_wrapper").click()
    assert len(clip.writes) == 1
    _assert_official_wrap(clip.writes[0], "旧内容")
    assert _box(w, "task_box") == "已包装并复制到剪贴板"
    assert mon.ack_calls == [] and mon.submit_calls == []
    assert bridge.deliver_calls == []
    assert w._listening is True
    assert fake.auto_active is True


def test_wrap_clipboard_prioritized_while_result_slot_occupied(qapp):
    fake = FakeController()
    clip = FakeClipboard(text="旧内容")
    bridge = FakeBridge()
    bridge.result_occupied = True
    w = _wired(qapp, fake, bridge, None, clipboard=clip)
    w.findChild(QPushButton, "btn_wrapper").click()
    assert len(clip.writes) == 1
    _assert_official_wrap(clip.writes[0], "旧内容")
    assert bridge.result_slot_occupied() is True
    assert bridge.deliver_calls == []
    assert _box(w, "task_box") == "已包装并复制到剪贴板"


def test_send_reads_input_wrap_reads_clipboard(qapp):
    # 两入口正文来源不同：发送读输入框（不读剪贴板）；包装内容读剪贴板（不用输入框）
    fake = FakeController()
    clip_a = FakeClipboard(text="剪贴板里有别的文本")
    w = _wired(qapp, fake, FakeBridge(), None, clipboard=clip_a)
    w.findChild(QPlainTextEdit, "manual_input").setPlainText("输入框正文")
    w.findChild(QPushButton, "btn_send").click()  # 真实按钮点击
    assert len(clip_a.writes) == 1
    assert "输入框正文" in clip_a.writes[0]
    assert "剪贴板里有别的文本" not in clip_a.writes[0]

    fake2 = FakeController()
    clip_b = FakeClipboard(text="剪贴板原文")
    w2 = _wired(qapp, fake2, FakeBridge(), None, clipboard=clip_b)
    w2.findChild(QPlainTextEdit, "manual_input").setPlainText("不该被用到的输入框")
    w2.findChild(QPushButton, "btn_wrapper").click()
    assert len(clip_b.writes) == 1
    assert "剪贴板原文" in clip_b.writes[0]
    assert "不该被用到的输入框" not in clip_b.writes[0]


def test_ctrl_enter_wraps_and_copies(qapp):
    # 既有 Ctrl+Enter 快捷键走同一立即包装+复制行为（真实按键事件，非直接调 handler）
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    fake = FakeController()
    clip = FakeClipboard()
    w = _wired(qapp, fake, FakeBridge(), None, clipboard=clip)
    edit = w.findChild(QPlainTextEdit, "manual_input")
    edit.setPlainText("ctrl+enter 正文")
    edit.setFocus()
    QTest.keyClick(edit, Qt.Key_Return, Qt.ControlModifier)
    assert len(clip.writes) == 1
    _assert_official_wrap(clip.writes[0], "ctrl+enter 正文")
    assert fake.sent_texts == []  # 不再走 controller.manual_send
    assert _box(w, "manual_input") == "ctrl+enter 正文"  # 输入框保留原文


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

def test_pending_inbound_message_explains_listening_is_off(qapp):
    window = MainWindow()
    waiting = [1]
    ticks = []
    bridge = SimpleNamespace(tick=lambda: ticks.append(True), queue_size=lambda: waiting[0])
    timer = start_bridge_poll(window, bridge, interval_ms=60000)
    try:
        assert len(ticks) == 1
        assert "自动监听未开启（等待 1 条）" in _box(window, "task_box")
        assert _log(window).count("自动监听未开启") == 1
        timer.timeout.emit()
        assert _log(window).count("自动监听未开启") == 1
        waiting[0] = 2
        timer.timeout.emit()
        assert "自动监听未开启（等待 2 条）" in _box(window, "task_box")
    finally:
        timer.stop()


def test_auto_compact_does_not_call_compact(qapp):
    # monitor=None 的测试兼容：on_auto_compact 不崩，也不触发任何压缩
    fake = FakeController()
    w = _wired(qapp, fake)  # monitor=None
    w.auto_compact_changed.emit(True)
    w.auto_compact_changed.emit(False)
    assert fake.compact_calls == 0  # 不触发手动压缩
    assert fake.auto_compact_calls == []  # monitor=None → 不触发自动压缩
    assert "自动压缩已开启" in _log(w)
    assert "自动压缩已关闭" in _log(w)


def test_remote_task_uses_fixed_passthrough_template(qapp):
    # 17: on_remote_task 只 submit 给 monitor（使用固定透传模板），
    # 不再直接调 controller.receive_auto_task；主线程立即显示"正在处理"（0 HTTP）
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    bridge.on_remote_task(RemoteTask("evt_new", _task_envelope("test-task", "sample"), "hash1", 12345))
    assert mon.submit_calls == [("evt_new", extract_envelope(_task_envelope("test-task", "sample")), "{content}")]
    assert fake.auto_intakes == []  # 主线程不再直接调 controller
    assert _box(w, "task_box") == "已收到A端任务，正在处理"


def test_remote_task_ready_ui(qapp):
    # 18: 主线程立即"正在处理"；monitor 回 intake_ready → "已包装，等待大模型恢复"
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    bridge.on_remote_task(RemoteTask("evt_new", _task_envelope("test-task", "sample"), "hash1", 12345))
    assert _box(w, "task_box") == "已收到A端任务，正在处理"
    mon.push({"type": "intake_ready"})
    process_monitor_events(mon, w, bridge)
    assert _box(w, "task_box") == "已包装，等待大模型恢复"
    assert "收到 A端新任务，已完成包装，等待大模型" in _log(w)


def test_remote_task_running_ui(qapp):
    # 19: monitor 回 intake_running → "已提交到大模型，等待执行"
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    bridge.on_remote_task(RemoteTask("evt_new", _task_envelope("test-task", "sample"), "hash1", 12345))
    mon.push({"type": "intake_running"})
    process_monitor_events(mon, w, bridge)
    assert _box(w, "task_box") == "已提交到大模型，等待执行"
    assert "收到 A端新任务，已包装并提交到当前会话" in _log(w)


def test_remote_task_busy_logs(qapp):
    # 20: intake_busy → 显示排队，重复 busy 不刷屏或覆盖排队提示。
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    bridge.on_remote_task(RemoteTask("evt_new", _task_envelope("test-task", "sample"), "hash1", 12345))
    assert _box(w, "task_box") == "已收到A端任务，正在处理"
    mon.push({"type": "intake_busy", "event_id": "evt_new"})
    process_monitor_events(mon, w, bridge)
    assert "自动任务未接收：已有任务正在处理" in _log(w)
    assert _box(w, "task_box") == "已有自动任务等待处理；A端新消息已排队，尚未执行"
    bridge.on_remote_task(RemoteTask("evt_new", _task_envelope("test-task", "sample"), "hash1", 12345))
    mon.push({"type": "intake_busy", "event_id": "evt_new"})
    process_monitor_events(mon, w, bridge)
    assert _box(w, "task_box") == "已有自动任务等待处理；A端新消息已排队，尚未执行"
    assert _log(w).count("新消息排队等待") == 1


def test_remote_task_independent_of_a_connection(qapp):
    # 21: A端断连也不影响 submit（A 端只影响最终回传，不影响包装/发送）
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    w.set_a_connection("已断开", "--", None)  # A 端不可用
    bridge.on_remote_task(RemoteTask("evt_new", _task_envelope("test-task", "sample"), "hash1", 12345))
    assert mon.submit_calls == [("evt_new", extract_envelope(_task_envelope("test-task", "sample")), "{content}")]
    mon.push({"type": "intake_running"})
    process_monitor_events(mon, w, bridge)
    assert _box(w, "task_box") == "已提交到大模型，等待执行"


# --- 自动任务最终结果 / 首响应 的 UI 回传接线（monitor 事件驱动）-----------


def test_result_complete_deliver_before_ack(qapp):
    # 22: result_complete 时 bridge.deliver_result 必须先于 monitor.acknowledge_result
    # （ack 由本机写入成功后的 on_local_write 触发，写入发生在 deliver_result 内部）
    seq: list[str] = []

    class OrderBridge(FakeBridge):
        def deliver_result(self, text, event_id=None):
            seq.append("deliver")
            super().deliver_result(text, event_id)

    class OrderMonitor(FakeMonitor):
        def acknowledge_result(self, eid):
            super().acknowledge_result(eid)
            seq.append("ack")

    fake = FakeController()
    fake._auto_task = SimpleNamespace(event_id="evt_1")
    bridge = OrderBridge()
    mon = OrderMonitor()
    w = _wired(qapp, fake, bridge, mon)
    mon.push({"type": "result_complete", "event_id": "evt_1", "text": "R", "first_response_ms": None, "identity": VALID_ID})
    process_monitor_events(mon, w, bridge)
    assert seq == ["deliver", "ack"]  # 先交 Bridge（本机写入），再经 on_local_write 释放


def test_result_complete_updates_recent_result(qapp):
    # 23: result_complete（合法冻结身份）→ 原始结果展示 + 包装回包交 Bridge + 本机写成功后 ack
    fake = FakeController()
    fake._auto_task = SimpleNamespace(event_id="evt_1")
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    assert _box(w, "result_box") == "暂无结果"
    mon.push({"type": "result_complete", "event_id": "evt_1", "text": "最终答案", "first_response_ms": None, "identity": VALID_ID})
    process_monitor_events(mon, w, bridge)
    assert _box(w, "result_box") == "最终答案"  # 原始结果逐字保留展示
    assert _box(w, "task_box") == "自动任务已完成，结果已进入回传链路"
    assert "大模型任务完成" in _log(w)
    assert len(bridge.delivered) == 1  # 交付的是按原任务身份包装后的 legacy 回包
    sent = bridge.delivered[0]
    assert sent.startswith("----- AI_RELAY_BEGIN -----\nSOURCE: EXECUTOR\nTARGET: CHATGPT\nTYPE: RESPONSE\n")
    assert "TASK_ID: task-1\n" in sent  # 冻结的原 id，不是 event_id/随机 id
    assert "ROUND: 0\nMAX_ROUNDS: 3\n" in sent
    assert "CONTENT:\n最终答案\n" in sent
    assert sent.endswith("----- AI_RELAY_END -----")
    assert mon.ack_calls == ["evt_1"]


def test_result_ambiguous_pauses_ui(qapp):
    # 24: result_ambiguous → 暂停自动回传文案 + 日志，不清任务、不交 Bridge
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    mon.push({"type": "result_ambiguous", "event_id": "evt_1"})
    process_monitor_events(mon, w, bridge)
    assert _box(w, "task_box") == "结果识别存在冲突，已暂停自动回传"
    assert "检测到同一会话存在额外用户消息，未自动回传" in _log(w)
    assert bridge.delivered == []  # ambiguous 绝不回传
    assert mon.ack_calls == []  # 不释放任务


def test_first_response_preserves_probe_display(qapp):
    # 25: 首响应事件不覆盖 probe 驱动的连接显示（服务延迟保留、状态不变），只写隐藏首响应
    fake = FakeController(model_result=ModelConnectionResult(True, 15, True, None))
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    w.set_model_connection("已连接", 15)  # 模拟 probe 轮询写入的连接显示
    mon.push({"type": "first_response", "first_response_ms": 420})
    process_monitor_events(mon, w, bridge)
    assert "已连接" in _label(w, "llm_status")  # 不被首响应事件覆盖成复杂状态
    assert "15 ms" in _label(w, "llm_oc")  # probe 服务延迟保留
    assert "420 ms" in _label(w, "llm_first")  # 首响应写入隐藏显示
    assert w._model_state["first_response_ms"] == 420


def test_monitor_poll_timer_does_no_http(qapp):
    # 26: monitor 事件处理路径（QTimer 调用的同一函数）不做 OpenChamber HTTP
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    timer = start_monitor_poll(w, mon, bridge, interval_ms=60_000)  # 长间隔，测试期间不触发
    assert timer.isActive()
    mon.push({"type": "offline"})
    process_monitor_events(mon, w, bridge)  # 走 QTimer 的同一处理路径
    assert fake.model_calls == 0  # drain/处理事件绝不发 OpenChamber HTTP
    assert _box(w, "task_box") == "大模型连接中断，等待恢复"
    assert bridge.model_statuses == [("disconnected", None)]
    timer.stop()


# --- L05-05: AI_RELAY_COMPLETE 严格联动停止 + 首响应状态回写 -----------


def test_complete_exact_stops_inbound_listening(qapp):
    # 4+6+16+17: 严格命中 → 停 inbound 监听；不 stop monitor 线程/不清 Controller 任务/不发 OpenChamber
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    w.set_listening(True)  # 先开，验证命中后自动关闭
    bridge.on_remote_task(RemoteTask("evt_c", "AI_RELAY_COMPLETE", "h", 1))
    assert mon.submit_calls == []          # 4: 不 submit
    assert mon.stop_calls == 0             # 16: 不停 monitor 线程
    assert fake.finish_calls == []         # 17: 不清 Controller 任务
    assert fake.model_calls == 0           # 不发 OpenChamber
    assert w._listening is False           # 6: inbound 监听关闭
    assert bridge.listening_calls == [True, False]  # 手动开 + COMPLETE 自动停


def test_complete_wrapper_not_read(qapp):
    # 5: 严格命中 → 不提交任务；普通任务使用固定透传模板
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    bridge.on_remote_task(RemoteTask("evt_c", "AI_RELAY_COMPLETE", "h", 1))
    assert mon.submit_calls == []
    bridge.on_remote_task(RemoteTask("evt_n", _task_envelope("normal-task", "normal"), "h", 2))
    assert mon.submit_calls == [("evt_n", extract_envelope(_task_envelope("normal-task", "normal")), "{content}")]


def test_complete_updates_ui(qapp):
    # 7: 严格命中 → UI 当前任务/日志正确 + 监听按钮变 [开始监听]
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    bridge.on_remote_task(RemoteTask("evt_c", "AI_RELAY_COMPLETE", "h", 1))
    assert _box(w, "task_box") == "项目已完成，自动联动已停止"
    assert "收到 AI_RELAY_COMPLETE，自动联动已停止" in _log(w)
    assert w._btn_listen.text() == "开始监听"


@pytest.mark.parametrize(
    "text",
    [
        " AI_RELAY_COMPLETE",       # 前空格
        "AI_RELAY_COMPLETE ",       # 后空格
        "AI_RELAY_COMPLETE\n",      # 换行
        "AI_RELAY_COMPLETE\r\n",    # CRLF
        "ai_relay_complete",        # 大小写不同
        "AI_RELAY_COMPLETE。",      # 尾部标点
        "完成：AI_RELAY_COMPLETE",  # 含前缀
    ],
)
def test_complete_non_exact_not_matched(qapp, text):
    # 8+9+10: 任何非严格相等 → 不命中 COMPLETE；普通文本直接忽略，不 submit、监听状态不变
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    w.set_listening(True)
    bridge.on_remote_task(RemoteTask("evt_x", text, "h", 1))
    assert mon.submit_calls == []
    assert w._listening is True  # 非命中不触发 COMPLETE 停止逻辑，忽略也不停监听


# ── A-REPLY-WRAPPER-FIX1: 外层包装中提取 AI_RELAY 包 ──────────────


def test_envelope_wrapped_plain_task_submits_inner(qapp):
    # 带 ChatGPT 前言/后记的整段文本 → 只提交 BEGIN..END 内部内容
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    text = (
        "ChatGPT 写了一段前言\n```text\n"
        "----- AI_RELAY_BEGIN -----\nSOURCE: CHATGPT\nTARGET: EXECUTOR\nTYPE: TASK\nCONTENT:\n你好\n"
        "----- AI_RELAY_END -----\n```\n"
    )
    bridge.on_remote_task(RemoteTask("evt_1", text, "h", 1))
    assert mon.submit_calls == [
        ("evt_1", "\nSOURCE: CHATGPT\nTARGET: EXECUTOR\nTYPE: TASK\nCONTENT:\n你好\n", "{content}")
    ]
    assert w._listening is False

def test_returned_b_result_is_not_submitted_as_new_a_task(qapp):
    bridge = FakeBridge()
    monitor = FakeMonitor()
    window = _wired(qapp, FakeController(), bridge, monitor)
    text = (
        "----- AI_RELAY_BEGIN -----\nSOURCE: EXECUTOR\nTARGET: CHATGPT\nTYPE: RESPONSE\n"
        "TASK_ID: old-task\nCONTENT:\n处理结果\n----- AI_RELAY_END -----"
    )
    assert bridge.on_remote_task(RemoteTask("echo", text, "h", 1)) == "ignored"
    assert monitor.submit_calls == []
    assert "已忽略 B端回传的结果" in _log(window)

def test_nested_b_result_is_not_executed_even_when_outer_frame_is_a_task(qapp):
    bridge = FakeBridge()
    monitor = FakeMonitor()
    _wired(qapp, FakeController(), bridge, monitor)
    returned = (
        "----- AI_RELAY_BEGIN -----\nSOURCE: EXECUTOR\nTARGET: CHATGPT\n"
        "TYPE: RESPONSE\nTASK_ID: previous\nCONTENT:\nanswer\n----- AI_RELAY_END -----"
    )
    assert bridge.on_remote_task(RemoteTask("echo", _task_envelope("new-id", returned), "h", 1)) == "ignored"
    v1_return = (
        "AI_RELAY/1\nMESSAGE_ID: response-1\nIN_REPLY_TO: previous\n"
        "SOURCE: EXECUTOR\nTARGET: CHATGPT\nTYPE: RESPONSE\nROUND: 1\nMAX_ROUNDS: 3\n\nanswer"
    )
    assert bridge.on_remote_task(RemoteTask("v1-echo", _task_envelope("another-id", v1_return), "h", 2)) == "ignored"
    assert monitor.submit_calls == []

def test_only_a_to_b_task_route_is_accepted(qapp):
    bridge = FakeBridge()
    monitor = FakeMonitor()
    _wired(qapp, FakeController(), bridge, monitor)
    for text in (
        "----- AI_RELAY_BEGIN -----\nSOURCE: EXECUTOR\nTARGET: CHATGPT\nTYPE: TASK\nCONTENT:\nanswer\n----- AI_RELAY_END -----",
        "----- AI_RELAY_BEGIN -----\nSOURCE: CHATGPT\nTARGET: EXECUTOR\nTYPE: RESPONSE\nCONTENT:\nanswer\n----- AI_RELAY_END -----",
        "----- AI_RELAY_BEGIN -----\nCONTENT:\nmissing routing\n----- AI_RELAY_END -----",
        "AI_RELAY/1\nSOURCE: EXECUTOR\nTARGET: CHATGPT\nTYPE: RESPONSE\n\nanswer",
    ):
        assert bridge.on_remote_task(RemoteTask("not-task", text, "h", 1)) == "ignored"
    assert monitor.submit_calls == []
    v1_task = "AI_RELAY/1\nSOURCE: CHATGPT\nTARGET: EXECUTOR\nTYPE: TASK\nTASK_ID: v1-task\nROUND: 1\nMAX_ROUNDS: 3\nCONTENT:\nwork\n----- AI_RELAY_END -----"
    assert bridge.on_remote_task(RemoteTask("v1", v1_task, "h", 1)) == "submitted"
    assert monitor.submit_calls == [("v1", extract_envelope(v1_task), "{content}")]

def test_nested_b_result_is_not_executed_even_when_outer_frame_is_a_task(qapp):
    bridge = FakeBridge()
    monitor = FakeMonitor()
    window = _wired(qapp, FakeController(), bridge, monitor)
    returned = (
        "----- AI_RELAY_BEGIN -----\nSOURCE: EXECUTOR\nTARGET: CHATGPT\n"
        "TYPE: RESPONSE\nTASK_ID: previous\nCONTENT:\nanswer\n----- AI_RELAY_END -----"
    )
    assert bridge.on_remote_task(RemoteTask("echo", _task_envelope("new-id", returned), "h", 1)) == "ignored"
    assert monitor.submit_calls == []

def test_only_a_to_b_task_route_is_accepted(qapp):
    bridge = FakeBridge()
    monitor = FakeMonitor()
    _wired(qapp, FakeController(), bridge, monitor)
    for text in (
        "----- AI_RELAY_BEGIN -----\nSOURCE: EXECUTOR\nTARGET: CHATGPT\nTYPE: TASK\nCONTENT:\nanswer\n----- AI_RELAY_END -----",
        "----- AI_RELAY_BEGIN -----\nSOURCE: CHATGPT\nTARGET: EXECUTOR\nTYPE: RESPONSE\nCONTENT:\nanswer\n----- AI_RELAY_END -----",
        "----- AI_RELAY_BEGIN -----\nCONTENT:\nmissing routing\n----- AI_RELAY_END -----",
        "AI_RELAY/1\nSOURCE: EXECUTOR\nTARGET: CHATGPT\nTYPE: RESPONSE\n\nanswer",
    ):
        assert bridge.on_remote_task(RemoteTask("not-task", text, "h", 1)) == "ignored"
    assert monitor.submit_calls == []
    v1_task = "AI_RELAY/1\nSOURCE: CHATGPT\nTARGET: EXECUTOR\nTYPE: TASK\nTASK_ID: v1-task\nROUND: 1\nMAX_ROUNDS: 3\nCONTENT:\nwork\n----- AI_RELAY_END -----"
    assert bridge.on_remote_task(RemoteTask("v1", v1_task, "h", 1)) == "submitted"
    assert monitor.submit_calls == [("v1", extract_envelope(v1_task), "{content}")]


def test_envelope_wrapped_complete_stops_inbound(qapp):
    # 完整包内 AI_RELAY_COMPLETE → 触发联动停止（绝不能发给 OpenChamber）
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    w.set_listening(True)
    text = "前言 " + "----- AI_RELAY_BEGIN -----\nAI_RELAY_COMPLETE\n----- AI_RELAY_END -----" + " 后记"
    bridge.on_remote_task(RemoteTask("evt_2", text, "h", 1))
    assert mon.submit_calls == []
    assert w._listening is False
    assert "收到 AI_RELAY_COMPLETE，自动联动已停止" in _log(w)


def test_starts_with_begin_marker_no_end_is_ignored(qapp):
    # 半包（只有 BEGIN 无 END）→ 非完整包、非任务，忽略不执行
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    w.set_listening(True)
    text = "----- AI_RELAY_BEGIN -----\nSOURCE: CHATGPT\nCONTENT:\n只有半包"
    bridge.on_remote_task(RemoteTask("evt_3", text, "h", 1))
    assert mon.submit_calls == []
    assert w._listening is True  # 忽略不误停监听


def test_first_response_writes_bridge_status(qapp):
    # 12+13: first_response 事件 → UI 显示真实 ms 且 bridge.set_model_first_response(ms)
    fake = FakeController(model_result=ModelConnectionResult(True, 9, True, None))
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    mon.push({"type": "first_response", "first_response_ms": 333})
    process_monitor_events(mon, w, bridge)
    assert "333 ms" in _label(w, "llm_first")
    assert bridge.first_response_calls == [333]


def test_new_task_resets_first_response_keeps_probe_display(qapp):
    # 11: 普通新任务开始 → 首响应清为 None（隐藏显示 -- ms），probe 连接显示不被覆盖
    fake = FakeController(model_result=ModelConnectionResult(True, 18, True, None))
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    w.set_model_connection("已连接", 18)  # 模拟 probe 轮询写入
    mon.push({"type": "first_response", "first_response_ms": 820})
    process_monitor_events(mon, w, bridge)
    assert "820 ms" in _label(w, "llm_first")
    assert bridge.first_response_calls[-1] == 820
    # 新任务：清首响应，probe 连接显示保持
    bridge.on_remote_task(RemoteTask("evt_n", _task_envelope("next-task", "new task"), "h", 2))
    assert "已连接" in _label(w, "llm_status")
    assert "18 ms" in _label(w, "llm_oc")
    assert "-- ms" in _label(w, "llm_first")
    assert w._model_state["first_response_ms"] is None
    assert bridge.first_response_calls[-1] is None
    assert mon.submit_calls == [("evt_n", extract_envelope(_task_envelope("next-task", "new task")), "{content}")]


def test_remote_task_end_only_half_envelope_ignored(qapp):
    # 半包（只有 END 无 BEGIN）→ 非完整包、非任务，忽略不执行，监听开时不误停
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    w.set_listening(True)
    text = "只有半包的内容\n----- AI_RELAY_END -----"
    bridge.on_remote_task(RemoteTask("evt_e", text, "h", 1))
    assert mon.submit_calls == []
    assert w._listening is True


def test_remote_task_blank_envelope_ignored(qapp):
    # 空白包（BEGIN/END 间无有效内容）→ 非任务忽略：不 submit、不清首响应、
    # 不覆盖 probe 连接显示、监听状态不变
    fake = FakeController(model_result=ModelConnectionResult(True, 9, True, None))
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    w.set_listening(True)
    w.set_model_connection("已连接", 9)
    mon.push({"type": "first_response", "first_response_ms": 333})
    process_monitor_events(mon, w, bridge)
    bridge.on_remote_task(RemoteTask("evt_b", "----- AI_RELAY_BEGIN -----\n----- AI_RELAY_END -----", "h", 1))
    assert mon.submit_calls == []
    assert w._listening is True
    assert "已连接" in _label(w, "llm_status")
    assert "9 ms" in _label(w, "llm_oc")
    assert "333 ms" in _label(w, "llm_first")  # 首响应未被忽略分支清除
    assert w._model_state["first_response_ms"] == 333


def test_remote_task_plain_text_ignored_keeps_state(qapp):
    # 纯普通文本（无标记）→ 忽略：不 submit、不清首响应、不改当前任务/结果/监听
    fake = FakeController(model_result=ModelConnectionResult(True, 18, True, None))
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    w.set_listening(True)
    w.set_model_connection("已连接", 18)
    mon.push({"type": "first_response", "first_response_ms": 820})
    process_monitor_events(mon, w, bridge)
    mon.push({"type": "result_complete", "event_id": "evt_0", "text": "先前结果", "first_response_ms": None, "identity": VALID_ID})
    process_monitor_events(mon, w, bridge)
    bridge.on_remote_task(RemoteTask("evt_p", "普通复制的文本，没有任何 AI_RELAY 标记", "h", 1))
    assert mon.submit_calls == []
    assert w._listening is True
    assert "已连接" in _label(w, "llm_status")
    assert "18 ms" in _label(w, "llm_oc")
    assert "820 ms" in _label(w, "llm_first")  # 忽略不清首响应
    assert w._model_state["first_response_ms"] == 820
    assert _box(w, "result_box") == "先前结果"  # 最近结果不被覆盖
    assert "普通复制的文本" not in _log(w)  # 日志不记录复制原文


def test_remote_task_ignore_keeps_state_when_window_hidden(qapp):
    # 窗口隐藏（非当前页）时忽略普通文本：跨页状态（监听/首响应/连接显示）全部保留
    fake = FakeController(model_result=ModelConnectionResult(True, 21, True, None))
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    w.set_listening(True)
    w.set_model_connection("已连接", 21)
    mon.push({"type": "first_response", "first_response_ms": 555})
    process_monitor_events(mon, w, bridge)
    w.hide()
    bridge.on_remote_task(RemoteTask("evt_h", "隐藏窗口时的普通文本", "h", 1))
    assert w.isHidden()
    assert mon.submit_calls == []
    assert w._listening is True
    assert "已连接" in _label(w, "llm_status")
    assert "555 ms" in _label(w, "llm_first")
    assert w._model_state["first_response_ms"] == 555


def test_remote_task_ignore_when_not_listening(qapp):
    # 监听关闭状态下普通文本同样忽略：不 submit、监听状态保持关闭
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    assert w._listening is False
    bridge.on_remote_task(RemoteTask("evt_o", "监听关闭时的普通文本", "h", 1))
    assert mon.submit_calls == []
    assert w._listening is False


def test_remote_filter_does_not_affect_manual_send(qapp):
    # 自动入口忽略普通文本后，手动入口不受影响（阶段 4A：立即包装+写剪贴板，
    # 不走 controller.manual_send，也不进 monitor 自动链）
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    clip = FakeClipboard()
    w = _wired(qapp, fake, bridge, mon, clipboard=clip)
    bridge.on_remote_task(RemoteTask("evt_f", "自动入口应忽略的普通文本", "h", 1))
    assert mon.submit_calls == []
    w.manual_send_requested.emit("手动内容 保持原样")
    assert fake.sent_texts == []  # 不再发往 OpenChamber
    assert mon.submit_calls == []  # 手动发送不走 monitor 自动链
    assert len(clip.writes) == 1  # 包装回包写入本机剪贴板
    assert "手动内容 保持原样" in clip.writes[0]


def test_startup_enables_session_listening_and_auto_compact(qapp):
    fake = FakeController(session=CurrentSession("ses_abc", r"C:\w", "src", True, None))
    bridge = FakeBridge()
    monitor = FakeMonitor()
    window = _wired(qapp, fake, bridge, monitor)

    initialize_startup(window)

    assert fake.refresh_calls == 1
    assert bridge.session_ids == ["ses_abc"]
    assert bridge.listening_calls == [True]
    assert window.findChild(QPushButton, "btn_listen").text() == "停止监听"
    assert window._chk_auto.isChecked()
    assert monitor.auto_compact_calls == [True]
    assert fake.compact_calls == 0
    assert fake.auto_compact_calls == []

def test_startup_keeps_listening_and_auto_compact_on_without_session(qapp):
    fake = FakeController()
    bridge = FakeBridge()
    monitor = FakeMonitor()
    window = _wired(qapp, fake, bridge, monitor)

    initialize_startup(window)

    assert bridge.session_ids == [None]
    assert "未获取当前激活会话" in window.findChild(QLabel, "session_notice").text()
    assert bridge.listening_calls == [True]
    assert window._chk_auto.isChecked()
    assert monitor.auto_compact_calls == [True]

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


# --- L05-06: 自动压缩接线（checkbox / compact 事件 / 手动 compact 不回归）----


def test_ai_relay_complete_no_auto_compact(qapp):
    # 19: AI_RELAY_COMPLETE 不 submit → 无任务 → 即使 auto_compact 开也不 compact / 不 ack
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    bridge.on_remote_task(RemoteTask("evt_c", "AI_RELAY_COMPLETE", "h", 1))
    assert mon.submit_calls == []  # 不 submit → 无 result_complete → 无 ack
    assert mon.ack_calls == []
    assert fake.auto_compact_calls == []


def test_auto_compact_checkbox_on_calls_monitor(qapp):
    # 20: 勾选 checkbox → monitor.set_auto_compact(True) + 日志
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    w.auto_compact_changed.emit(True)
    assert mon.auto_compact_calls == [True]
    assert "自动压缩已开启" in _log(w)


def test_auto_compact_checkbox_off_calls_monitor(qapp):
    # 21: 取消 checkbox → monitor.set_auto_compact(False) + 日志
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    w.auto_compact_changed.emit(False)
    assert mon.auto_compact_calls == [False]
    assert "自动压缩已关闭" in _log(w)


def test_auto_compact_checkbox_no_direct_compact(qapp):
    # 22: checkbox signal 本身不直接调 controller.compact*（手动/自动压缩都不触发）
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    w.auto_compact_changed.emit(True)
    w.auto_compact_changed.emit(False)
    assert fake.compact_calls == 0  # 手动压缩未触发
    assert fake.auto_compact_calls == []  # 自动压缩未直接触发（只让 monitor 入队）
    assert mon.auto_compact_calls == [True, False]


def test_compact_success_logs(qapp):
    # 23: compact_success 事件 → 日志"自动任务会话压缩完成"
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    mon.push({"type": "compact_success", "event_id": "evt_1"})
    process_monitor_events(mon, w, bridge)
    assert "自动任务会话压缩完成" in _log(w)


def test_compact_failed_logs(qapp):
    # 24: compact_failed 事件 → 日志"自动压缩失败：<error>"
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    mon.push({"type": "compact_failed", "event_id": "evt_1", "error": "http: 500"})
    process_monitor_events(mon, w, bridge)
    assert "自动压缩失败：http: 500" in _log(w)


def test_compact_failed_keeps_completed_task(qapp):
    # 25: compact 失败 != 任务失败：不把"结果已进入回传链路"改成失败
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    mon.push({"type": "result_complete", "event_id": "evt_1", "text": "R", "first_response_ms": None, "identity": VALID_ID})
    process_monitor_events(mon, w, bridge)
    assert _box(w, "task_box") == "自动任务已完成，结果已进入回传链路"
    mon.push({"type": "compact_failed", "event_id": "evt_1", "error": "http: 500"})
    process_monitor_events(mon, w, bridge)
    assert _box(w, "task_box") == "自动任务已完成，结果已进入回传链路"  # 文案不变
    assert "自动压缩失败：http: 500" in _log(w)


def test_manual_compact_still_uses_current_session(qapp):
    # 26: 手动"立即压缩当前会话" → 仍走 compact_current_session（不回归）
    fake = FakeController(compact=CompactResult(True, None))
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    w.compact_requested.emit()
    assert fake.compact_calls == 1  # compact_current_session 被调用
    assert fake.auto_compact_calls == []  # 不走自动压缩
    assert "已提交当前会话压缩" in _log(w)


def test_result_complete_order_preserved_with_auto_compact(qapp):
    # 27: 原有顺序仍保持：bridge.deliver_result 先于 monitor.acknowledge_result
    seq: list[str] = []

    class OrderBridge(FakeBridge):
        def deliver_result(self, text, event_id=None):
            seq.append("deliver")
            super().deliver_result(text, event_id)

    class OrderMonitor(FakeMonitor):
        def acknowledge_result(self, eid):
            super().acknowledge_result(eid)
            seq.append("ack")

    fake = FakeController()
    fake._auto_task = SimpleNamespace(event_id="evt_1")
    bridge = OrderBridge()
    mon = OrderMonitor()
    w = _wired(qapp, fake, bridge, mon)
    mon.push({"type": "result_complete", "event_id": "evt_1", "text": "R", "first_response_ms": None, "identity": VALID_ID})
    process_monitor_events(mon, w, bridge)
    assert seq == ["deliver", "ack"]  # 先交 Bridge（本机写入），再经 on_local_write 释放（compact 在 worker ack 内）


# --- 阶段 3D：结果回包按原任务身份包装（GUI 单点包装/安全分支）-----------


def test_result_complete_wraps_legacy_response(qapp):
    # 合法身份 + 非空结果：deliver 完整 legacy 回包（TIME 行存在），本机写成功后 ack
    fake = FakeController()
    fake._auto_task = SimpleNamespace(event_id="evt_9")
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    mon.push(
        {
            "type": "result_complete",
            "event_id": "evt_9",
            "text": "第一行\n第二行",
            "first_response_ms": 40,
            "identity": AutoTaskIdentity("task-77", 2, 9, True, None),
        }
    )
    process_monitor_events(mon, w, bridge)
    assert _box(w, "result_box") == "第一行\n第二行"  # 原始结果逐字展示
    assert len(bridge.delivered) == 1
    sent = bridge.delivered[0]
    lines = sent.split("\n")
    assert lines[0] == "----- AI_RELAY_BEGIN -----"
    assert lines[1:5] == ["SOURCE: EXECUTOR", "TARGET: CHATGPT", "TYPE: RESPONSE", "TASK_ID: task-77"]
    assert lines[5] == "ROUND: 2"
    assert lines[6] == "MAX_ROUNDS: 9"
    assert lines[7].startswith("TIME: ")
    assert lines[8] == "CONTENT:"
    assert lines[9:11] == ["第一行", "第二行"]
    assert lines[11] == "----- AI_RELAY_END -----"
    assert mon.ack_calls == ["evt_9"]


def test_result_complete_invalid_identity_blocks_deliver_and_ack(qapp):
    # 身份无效：零 deliver/零 ack（任务保持占用），可见状态 + 简短原因，原结果仍可查看
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    mon.push(
        {
            "type": "result_complete",
            "event_id": "evt_bad",
            "text": "有结果但没有原任务id",
            "first_response_ms": None,
            "identity": INVALID_ID,
        }
    )
    process_monitor_events(mon, w, bridge)
    assert bridge.delivered == []
    assert mon.ack_calls == []
    assert _box(w, "result_box") == "有结果但没有原任务id"  # 原结果保留展示
    assert _box(w, "task_box") == "结果已就绪，但原任务身份无效，未自动回传"
    assert "缺少头部：TASK_ID" in _log(w)


def test_result_complete_missing_identity_blocks_deliver_and_ack(qapp):
    # 事件缺身份快照：同样零 deliver/零 ack，可见反馈
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    mon.push({"type": "result_complete", "event_id": "evt_x", "text": "R", "first_response_ms": None})
    process_monitor_events(mon, w, bridge)
    assert bridge.delivered == []
    assert mon.ack_calls == []
    assert _box(w, "task_box") == "结果已就绪，但原任务身份无效，未自动回传"
    assert "原任务身份缺失" in _log(w)


def test_result_complete_empty_text_blocks_deliver_and_ack(qapp):
    # 身份有效但结果空：零 deliver/零 ack，原因"结果正文为空"
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    mon.push(
        {"type": "result_complete", "event_id": "evt_e", "text": "   ", "first_response_ms": None, "identity": VALID_ID}
    )
    process_monitor_events(mon, w, bridge)
    assert bridge.delivered == []
    assert mon.ack_calls == []
    assert "结果正文为空" in _log(w)


# --- 阶段 3D 集成链：真实 LiteController + AutoMonitor + wire_ui + ClipLinkBridge ---


class _ChainClient:
    """最小 fake client：脚本化 probe/发送/结果，全部内存。"""

    def __init__(self) -> None:
        from openchamber_client import TaskResultResult

        self.sent = []
        self.compacted = []
        self.result = TaskResultResult(True, False, None, None, False, False, None)

    def probe(self):
        from openchamber_client import ProbeResult

        return ProbeResult(True, 5, None)

    def validate_session(self, session_id, directory=None):
        return True

    def resolve_execution_config(self, session_id, directory=None):
        return object()

    def send_text(self, session_id, directory, text):
        from openchamber_client import SendResult

        self.sent.append((session_id, directory, text))
        return SendResult(True, None, f"m_{len(self.sent)}")

    def get_task_result(self, session_id, directory, message_id, allowed_followup_user_ids=None):
        return self.result

    def get_task_progress(self, session_id, directory, user_message_id):
        from openchamber_client import TaskProgressResult

        return TaskProgressResult(True, "mk", True)

    def get_session_status(self, session_id):
        from openchamber_client import SessionStatusResult

        return SessionStatusResult(True, "idle", None)

    def compact_session(self, session_id, directory):
        from openchamber_client import CompactResult

        self.compacted.append((session_id, directory))
        return CompactResult(True, None)


def _push_remote(bridge, path: Path, event_id: str, text: str) -> None:
    # 阶段 3F-2：运行时帧经 sink 入队（生产时序：client 网络线程收帧 → sink）；
    # 快照文件仅启动时导入一次，不再是运行时事件源。文件同步写一份保持快照语义。
    path.write_text(
        json.dumps({"event_id": event_id, "text": text, "content_hash": "h", "updated_at": now_millis()}),
        encoding="utf-8",
    )
    bridge.enqueue_remote_task(RemoteTask(event_id, text, "h", now_millis()))
    bridge.tick()


def test_real_chain_identity_wraps_result_online_and_pending(qapp, tmp_path):
    # 完整链：remote envelope(含原 TASK_ID) → wire_ui → 真实 AutoMonitor →
    # 真实 ClipLinkBridge（内存剪贴板/临时文件）。验证：原 id 一路到回包、
    # 原始结果展示、先交付后 ack、在线直写与离线 pending 恢复 flush 同字节（TIME 不重建）。
    from active_session_reader import ActiveSession
    from auto_monitor import AutoMonitor
    from openchamber_client import TaskResultResult

    remote_path = tmp_path / "remote_clipboard.json"
    clip_status = tmp_path / "clip_status.json"
    bridge_status = tmp_path / "bridge_status.json"
    clipboard: list[str] = []

    window = MainWindow()
    window.show()
    bridge = ClipLinkBridge(
        remote_event_path=remote_path,
        cliplink_status_path=clip_status,
        status_file_path=bridge_status,
        clipboard_writer=lambda text: clipboard.append(text),
    )
    session = ActiveSession(session_id="ses_z", directory=r"C:\t", source="fake")
    client = _ChainClient()
    controller = LiteController(client, active_session_reader=lambda: session)
    monitor = AutoMonitor(controller, interval_seconds=1.0)
    wire_ui(window, controller, bridge, monitor)
    bridge.set_listening(True)

    # 任务1：A 在线 → 结果按原任务身份包装后直写剪贴板
    _write_cliplink_status(clip_status, status="connected", updated_at=now_millis())
    task1 = (
        "外层杂文\n----- AI_RELAY_BEGIN -----\nSOURCE: CHATGPT\nTARGET: EXECUTOR\nTYPE: TASK\n"
        "TASK_ID: task-77\nROUND: 2\nMAX_ROUNDS: 9\n"
        "CONTENT:\n修复登录\nTASK_ID: fake-999\n----- AI_RELAY_END -----\n尾巴"
    )
    _push_remote(bridge, remote_path, "evt-77", task1)
    monitor.run_once()  # intake(解析冻结身份) + 首发送 + 结果识别 → result_complete(paused)
    ident = controller.identity_snapshot("evt-77")
    assert ident is not None and ident.valid
    assert (ident.task_id, ident.round_number, ident.max_rounds) == ("task-77", 2, 9)  # 头区原 id，非正文 fake-999
    client.result = TaskResultResult(
        True, True, "最终结果\n----- AI_RELAY_BEGIN -----\n嵌套标记\n----- AI_RELAY_END -----",
        120, False, False, None,
    )
    monitor.run_once()
    process_monitor_events(monitor, window, bridge)
    assert len(clipboard) == 1
    out = clipboard[0]
    assert "TASK_ID: task-77\nROUND: 2\nMAX_ROUNDS: 9\n" in out
    assert "SOURCE: EXECUTOR\nTARGET: CHATGPT\nTYPE: RESPONSE\n" in out
    assert out.startswith("----- AI_RELAY_BEGIN -----\n")
    assert out.endswith("----- AI_RELAY_END -----\n----- AI_RELAY_END -----")  # 结果自带标记原样保留 + 回包 END
    assert _box(window, "result_box") == (
        "最终结果\n----- AI_RELAY_BEGIN -----\n嵌套标记\n----- AI_RELAY_END -----"
    )  # 原始结果逐字展示
    assert _box(window, "task_box") == "自动任务已完成，结果已进入回传链路"
    monitor.run_once()  # ack → finish（auto_compact 关 → 无 compact）
    assert controller._auto_task is None
    assert client.compacted == []

    # 任务2：A 离线 → 同一包装字符串进 pending；恢复在线后 flush 同字节（TIME 不重建）
    client.result = TaskResultResult(True, True, "第二个结果", 66, False, False, None)
    clip_status.unlink()
    task2 = (
        "----- AI_RELAY_BEGIN -----\nSOURCE: CHATGPT\nTARGET: EXECUTOR\nTYPE: TASK\n"
        "TASK_ID: task-88\nCONTENT:\n第二个任务\n----- AI_RELAY_END -----"
    )
    _push_remote(bridge, remote_path, "evt-88", task2)
    monitor.run_once()
    monitor.run_once()
    process_monitor_events(monitor, window, bridge)
    assert clipboard == [out]  # 离线：不直写剪贴板
    assert bridge._pending_result is not None and "TASK_ID: task-88\n" in bridge._pending_result
    assert "ROUND: 0\nMAX_ROUNDS: 3\n" in bridge._pending_result  # 缺省轮次
    assert controller._auto_task is not None  # 3F-1：离线接管 pending 保持占用，不提前 ack/finish
    pending = bridge._pending_result
    _write_cliplink_status(clip_status, status="connected", updated_at=now_millis())
    bridge.tick()  # flush pending → 本机写入成功 → on_local_write 唯一 ack
    assert clipboard[-1] == pending  # 同一个已包装字符串，TIME 不重建、不二次包装
    assert controller._auto_task is not None  # ack 仅入队，释放由 worker 完成
    monitor.run_once()
    assert controller._auto_task is None

# ===================== 阶段 3D-2：非法字段表驱动故障回归 =====================
# 每个错误 result_complete 后紧跟一个正常控制事件（合法身份+非空结果的
# result_complete）：断言错误任务零 deliver/零 ack + 可见反馈，且同批后续
# 正常事件实际被处理（先交付后 ack）。

FAULT_CASES = [
    # (名称, identity 值, text 值, 日志应含的原因)
    ("identity_empty_dict", {}, "结果A", "类型或字段非法"),
    ("identity_missing", None, "结果A", "原任务身份缺失"),
    ("identity_valid_nonbool_true", AutoTaskIdentity("t1", 0, 3, "yes"), "结果A", "类型或字段非法"),
    ("identity_valid_nonbool_false", AutoTaskIdentity("t1", 0, 3, "no"), "结果A", "类型或字段非法"),
    ("identity_round_bool", AutoTaskIdentity("t1", True, 3, True, None), "结果A", "类型或字段非法"),
    ("identity_max_rounds_bool", AutoTaskIdentity("t1", 0, False, True, None), "结果A", "类型或字段非法"),
    ("identity_round_nonint", AutoTaskIdentity("t1", "1", 3, True, None), "结果A", "类型或字段非法"),
    ("identity_max_zero", AutoTaskIdentity("t1", 0, 0, True, None), "结果A", "类型或字段非法"),
    ("identity_round_gt_max", AutoTaskIdentity("t1", 5, 3, True, None), "结果A", "类型或字段非法"),
    ("identity_empty_task_id", AutoTaskIdentity("", 0, 3, True, None), "结果A", "类型或字段非法"),
    ("identity_task_id_newline", AutoTaskIdentity("t1\nSOURCE: HIJACK", 0, 3, True, None), "结果A", "封装失败"),
    ("text_nonstr_int", VALID_ID, 123, "不是字符串"),
    ("text_none", VALID_ID, None, "不是字符串"),
    ("text_empty", VALID_ID, "   ", "结果正文为空"),
    ("invalid_identity_snapshot", INVALID_ID, "结果A", "缺少头部：TASK_ID"),
]


@pytest.mark.parametrize("name,identity,text,reason", FAULT_CASES, ids=[c[0] for c in FAULT_CASES])
def test_fault_event_then_next_normal_event_processed(qapp, name, identity, text, reason):
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    fake._auto_task = SimpleNamespace(event_id="evt_ok")
    # 错误完成事件 + 同批后续正常控制事件（合法身份/非空文本）
    mon.push(
        {
            "type": "result_complete",
            "event_id": "evt_fault",
            "text": text,
            "first_response_ms": None,
            "identity": identity,
        }
    )
    mon.push(
        {
            "type": "result_complete",
            "event_id": "evt_ok",
            "text": "正常结果",
            "first_response_ms": None,
            "identity": VALID_ID,
        }
    )
    process_monitor_events(mon, w, bridge)
    # 错误任务：零 deliver/零 ack（保持占用）；可见反馈含原因
    delivered = bridge.delivered
    assert len(delivered) == 1  # 仅控制事件的一次交付
    assert "TASK_ID: task-1\n" in delivered[0]  # 交付的是控制事件的包装回包，不是错误任务的
    assert mon.ack_calls == ["evt_ok"]  # 错误任务未 ack
    assert reason in _log(w)
    assert _box(w, "task_box") == "自动任务已完成，结果已进入回传链路"  # 后续事件实际处理
    assert _box(w, "result_box") == "正常结果"  # 控制事件结果展示（非字符串结果未交 Qt）


def test_fault_identity_dict_keeps_previous_result_display(qapp):
    # 非法身份但文本是安全字符串：原文照常展示；后续非字符串结果不覆盖、不抛异常
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    mon.push(
        {"type": "result_complete", "event_id": "e1", "text": "先前的安全结果", "first_response_ms": None, "identity": {}}
    )
    mon.push({"type": "result_complete", "event_id": "e2", "text": 123, "first_response_ms": None, "identity": VALID_ID})
    process_monitor_events(mon, w, bridge)
    assert bridge.delivered == []
    assert mon.ack_calls == []
    assert _box(w, "result_box") == "先前的安全结果"  # 非字符串未覆盖，也未把 123 交给 Qt
    assert "不是字符串" in _log(w)


def test_fault_wrap_failure_releases_nothing_and_next_event_ok(qapp):
    # 封装故障（手工构造含换行的 id）：零 deliver/ack + 可见原因；同批正常事件照常交付
    fake = FakeController()
    fake._auto_task = SimpleNamespace(event_id="e_ok")
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    mon.push(
        {
            "type": "result_complete",
            "event_id": "e_bad_wrap",
            "text": "R",
            "first_response_ms": None,
            "identity": AutoTaskIdentity("t\nX: 1", 0, 3, True, None),
        }
    )
    mon.push(
        {
            "type": "result_complete",
            "event_id": "e_ok",
            "text": "ok结果",
            "first_response_ms": None,
            "identity": VALID_ID,
        }
    )
    process_monitor_events(mon, w, bridge)
    assert [t for t in bridge.delivered if "TASK_ID: t\n" in t] == []
    assert bridge.delivered == [t for t in bridge.delivered if "TASK_ID: task-1\n" in t]
    assert mon.ack_calls == ["e_ok"]
    assert "封装失败" in _log(w)


# ===================== 阶段 3F-1：回传单槽保护与本地写入后释放 =====================


def _real_chain(tmp_path: Path):
    """真实 MainWindow + LiteController + AutoMonitor + ClipLinkBridge 链（fake client/内存剪贴板/临时文件）。"""
    from active_session_reader import ActiveSession
    from auto_monitor import AutoMonitor

    remote = tmp_path / "remote_clipboard.json"
    clip_status = tmp_path / "clip_status.json"
    bridge_status = tmp_path / "bridge_status.json"
    clipboard: list[str] = []
    window = MainWindow()
    window.show()
    bridge = ClipLinkBridge(
        remote_event_path=remote,
        cliplink_status_path=clip_status,
        status_file_path=bridge_status,
        clipboard_writer=lambda text: clipboard.append(text),
    )
    session = ActiveSession(session_id="ses_z", directory=r"C:\t", source="fake")
    client = _ChainClient()
    controller = LiteController(client, active_session_reader=lambda: session)
    monitor = AutoMonitor(controller, interval_seconds=1.0)
    wire_ui(window, controller, bridge, monitor)
    return dict(
        remote=remote,
        clip_status=clip_status,
        clipboard=clipboard,
        window=window,
        bridge=bridge,
        client=client,
        controller=controller,
        monitor=monitor,
    )


def _offline_pending_state(tmp_path: Path, auto_compact: bool = False) -> dict:
    """跑到“A 离线、第一结果已接管 pending、任务保持占用”的确定状态。"""
    from openchamber_client import TaskResultResult

    ch = _real_chain(tmp_path)
    ch["bridge"].set_listening(True)
    task = (
        "----- AI_RELAY_BEGIN -----\nSOURCE: CHATGPT\nTARGET: EXECUTOR\nTYPE: TASK\n"
        "TASK_ID: task-1\nCONTENT:\n任务一\n----- AI_RELAY_END -----"
    )
    _push_remote(ch["bridge"], ch["remote"], "evt-1", task)
    if auto_compact:
        ch["monitor"].set_auto_compact(True)
    ch["monitor"].run_once()  # 接管 + 首发送
    ch["client"].result = TaskResultResult(True, True, "结果一", 50, False, False, None)
    ch["monitor"].run_once()  # 结果识别 → result_complete（A 离线：无状态文件）
    process_monitor_events(ch["monitor"], ch["window"], ch["bridge"])
    return ch


def test_new_a_turn_takes_over_stale_uncertain_task_without_business_queue(qapp, tmp_path):
    from openchamber_client import SendResult, TaskResultResult

    chain = _real_chain(tmp_path)
    chain["bridge"].set_listening(True)
    _write_cliplink_status(chain["clip_status"], status="connected", updated_at=now_millis())
    sends = []

    def send_text(session_id, directory, text):
        sends.append(text)
        if len(sends) == 1:
            return SendResult(False, "uncertain: 提交回执未确认", None)
        return SendResult(True, None, "new-user-message")

    chain["client"].send_text = send_text
    first = _task_envelope("old", "上一轮")
    second = _task_envelope("new", "下一轮")
    _push_remote(chain["bridge"], chain["remote"], "evt-old", first)
    chain["monitor"].run_once()
    process_monitor_events(chain["monitor"], chain["window"], chain["bridge"])
    assert chain["controller"].auto_task_submit_unknown()

    _push_remote(chain["bridge"], chain["remote"], "evt-new", second)
    chain["monitor"].run_once()
    process_monitor_events(chain["monitor"], chain["window"], chain["bridge"])
    assert len(sends) == 2
    assert chain["controller"]._auto_task.event_id == "evt-new"
    assert chain["bridge"].queue_size() == 0
    assert "新一轮到达后已释放旧状态" in _log(chain["window"])

    chain["client"].result = TaskResultResult(True, True, "下一轮答复", 20, False, False, None)
    chain["monitor"].run_once()
    process_monitor_events(chain["monitor"], chain["window"], chain["bridge"])
    assert len(chain["clipboard"]) == 1
    assert "TASK_ID: new\n" in chain["clipboard"][0]
    assert "TASK_ID: old\n" not in chain["clipboard"][0]

def test_uncertain_submit_later_confirmed_returns_and_compacts(qapp, tmp_path):
    from openchamber_client import SendResult, TaskResultResult

    chain = _real_chain(tmp_path)
    chain["bridge"].set_listening(True)
    _write_cliplink_status(chain["clip_status"], status="connected", updated_at=now_millis())
    chain["monitor"].set_auto_compact(True)
    chain["client"].send_text = lambda session_id, directory, text: SendResult(
        False, "uncertain: 提交回执未确认", None, "previous", True
    )
    confirmed = {"message_id": None}
    chain["client"].confirm_uncertain_submission = (
        lambda session_id, directory, text, baseline_user_id: confirmed["message_id"]
    )
    task = (
        "----- AI_RELAY_BEGIN -----\nSOURCE: CHATGPT\nTARGET: EXECUTOR\nTYPE: TASK\n"
        "TASK_ID: task-recovered\nCONTENT:\n修复\n----- AI_RELAY_END -----"
    )
    _push_remote(chain["bridge"], chain["remote"], "evt-recovered", task)
    chain["monitor"].run_once()
    process_monitor_events(chain["monitor"], chain["window"], chain["bridge"])
    assert chain["controller"].auto_task_submit_unknown()
    assert chain["clipboard"] == []
    assert chain["client"].compacted == []

    confirmed["message_id"] = "message-recovered"
    chain["client"].result = TaskResultResult(True, True, "已完成", 20, False, False, None)
    chain["monitor"].run_once()
    process_monitor_events(chain["monitor"], chain["window"], chain["bridge"])
    assert len(chain["clipboard"]) == 1
    assert "TASK_ID: task-recovered\n" in chain["clipboard"][0]
    assert "已在原会话核对到提交的任务" in _log(chain["window"])

    chain["monitor"].run_once()
    assert chain["client"].compacted == [("ses_z", r"C:\t")]
    assert chain["controller"]._auto_task is None

def test_3f1_offline_result_holds_task_until_local_write_success(qapp, tmp_path):
    # 离线：接管 pending 保持占用（零 compact/finish/本地写入/通知）；
    # 恢复在线 flush 写成功 → 冻结原会话 compact 一次再 finish（真实类，非 FakeMonitor 计数）
    ch = _offline_pending_state(tmp_path, auto_compact=True)
    assert ch["clipboard"] == []  # 本机写入未成功：无写入
    assert ch["bridge"]._pending_result is not None and "TASK_ID: task-1\n" in ch["bridge"]._pending_result
    assert ch["controller"]._auto_task is not None  # 任务保持占用：不提前 ack/finish/compact
    assert ch["client"].compacted == []
    # 决定性观察：本机写入未成功 → 尚未发出 ack，单跑一轮 worker 也不释放任务
    # （旧实现“接管 pending 即 ack”会在此处立即释放，被本断言抓住）
    ch["monitor"].run_once()
    assert ch["controller"]._auto_task is not None
    assert ch["client"].compacted == []
    _write_cliplink_status(ch["clip_status"], status="connected", updated_at=now_millis())
    ch["bridge"].tick()  # flush → 本机写入成功 → on_local_write → 唯一 ack 入队
    assert len(ch["clipboard"]) == 1 and "TASK_ID: task-1\n" in ch["clipboard"][0]
    assert ch["controller"]._auto_task is not None  # ack 已入队但 worker 尚未处理
    ch["monitor"].run_once()  # worker：compact 冻结原会话一次 + finish
    assert ch["client"].compacted == [("ses_z", r"C:\t")]  # 恰好一次
    assert ch["controller"]._auto_task is None
    assert ch["clipboard"] and ch["clipboard"][0].startswith("----- AI_RELAY_BEGIN -----\nSOURCE: EXECUTOR\n")


def test_3f1_second_result_rejected_keeps_first_and_task(qapp, tmp_path):
    # 单槽被第一结果占用：不同 event_id 的第二结果被拒绝、旧记录逐字保留、任务不释放
    ch = _offline_pending_state(tmp_path)
    first = ch["bridge"]._pending_result
    assert ch["bridge"].deliver_result("另一条异常结果", event_id="evt-other") == "rejected"
    assert ch["bridge"]._pending_result == first  # 旧记录保留，未静默覆盖
    assert ch["controller"]._auto_task is not None  # 被拒事件不 ack
    assert ch["clipboard"] == []
    assert ch["client"].compacted == []


def test_3f1_duplicate_submit_and_wrong_id_notification_no_release(qapp, tmp_path):
    # 相同事件相同文本重复提交不重写/不重复确认；错 id / 无关联通知不释放任务
    ch = _offline_pending_state(tmp_path)
    first = ch["bridge"]._pending_result
    assert ch["bridge"].deliver_result(first, event_id="evt-1") == "duplicate"
    assert ch["bridge"]._pending_result == first
    assert ch["clipboard"] == []
    ch["bridge"].on_local_write("evt-wrong")  # 错 id 通知
    ch["bridge"].on_local_write(None)  # 无关联通知
    assert ch["controller"]._auto_task is not None
    assert ch["client"].compacted == []
    # 正确的本机写入成功：恰好释放一次
    _write_cliplink_status(ch["clip_status"], status="connected", updated_at=now_millis())
    ch["bridge"].tick()
    assert len(ch["clipboard"]) == 1
    ch["monitor"].run_once()
    assert ch["controller"]._auto_task is None


def test_3f1_rejected_result_visible_no_ack_via_dispatch(qapp):
    # GUI 分发层：单槽被占用（rejected）→ 简洁可见状态 + 零 ack
    fake = FakeController()
    fake._auto_task = SimpleNamespace(event_id="evt_busy")
    bridge = FakeBridge()
    bridge.outcome = "rejected"
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    mon.push({"type": "result_complete", "event_id": "evt_busy", "text": "R", "first_response_ms": None, "identity": VALID_ID})
    process_monitor_events(mon, w, bridge)
    assert len(bridge.delivered) == 1  # 已交 Bridge（被其拒绝）
    assert mon.ack_calls == []  # 拒绝：不 ack
    assert _box(w, "task_box") == "结果已就绪，回传单槽被占用，未回传"
    assert "回传单槽被占用（rejected）" in _log(w)


def test_3f1_pending_result_visible_no_ack_via_dispatch(qapp):
    # GUI 分发层：接管 pending（离线/写失败）→ 可见等待状态 + 零 ack
    fake = FakeController()
    fake._auto_task = SimpleNamespace(event_id="evt_wait")
    bridge = FakeBridge()
    bridge.outcome = "pending"
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    mon.push({"type": "result_complete", "event_id": "evt_wait", "text": "R", "first_response_ms": None, "identity": VALID_ID})
    process_monitor_events(mon, w, bridge)
    assert len(bridge.delivered) == 1
    assert mon.ack_calls == []  # 接管 pending ≠ 本机写入成功：不 ack
    assert _box(w, "task_box") == "结果已就绪，等待A端在线后写回本机剪贴板"
    assert "结果暂存待回传" in _log(w)


# ===================== 阶段 3F-2：入站 FIFO 链式测试（真实 client/bridge/monitor 链）====


def _task_envelope(task_id: str, content: str) -> str:
    return (
        "----- AI_RELAY_BEGIN -----\nSOURCE: CHATGPT\nTARGET: EXECUTOR\nTYPE: TASK\n"
        f"TASK_ID: {task_id}\nCONTENT:\n{content}\n----- AI_RELAY_END -----"
    )


def test_3f2_real_client_frames_before_tick_all_taken_over_accept_in_order(qapp, tmp_path, monkeypatch):
    # R1 根因回归：真实 ClipLinkClient 连收 A/B/C（都在 tick 前）→ sink 全接管、
    # 无丢失；最终接受序 A,B,C（busy/在途闸门不越序）。
    import os

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    from cliplink_client import ClipLinkClient

    ch = _real_chain(tmp_path)
    ch["bridge"].set_listening(True)
    _write_cliplink_status(ch["clip_status"], status="connected", updated_at=now_millis())

    client = ClipLinkClient(
        listen_ip="127.0.0.1",
        port=0,
        device_id="550e8400-e29b-41d4-a716-446655440000",
        device_name="TEST-B",
        clipboard_getter=lambda: None,
        clipboard_setter=lambda t: None,
    )
    client.set_inbound_sink(ch["bridge"].enqueue_remote_task)
    for i, (tid, content) in enumerate((("task-A", "任务A"), ("task-B", "任务B"), ("task-C", "任务C"))):
        client._handle_remote_clipboard({"text": _task_envelope(tid, content), "content_hash": ""})
    assert ch["bridge"].queue_size() == 3  # 三帧全部接管（旧实现单槽文件只见 C）

    accepted: list[str] = []
    for i, (tid, content) in enumerate((("task-A", "任务A"), ("task-B", "任务B"), ("task-C", "任务C"))):
        ch["bridge"].tick()  # 队首投递（在途闸门）
        ch["monitor"].run_once()  # worker：接管+首发送
        ch["client"].result = type(ch["client"].result)(
            True, True, f"结果{content[-1]}", 10, False, False, None
        )
        ch["monitor"].run_once()  # 结果识别 → result_complete
        process_monitor_events(ch["monitor"], ch["window"], ch["bridge"])  # intake 回执+交付+ack 入队
        ch["monitor"].run_once()  # ack → finish（释放，下一项可接管）
        accepted.append(tid)
        assert ch["controller"]._auto_task is None  # 每项干净释放
    assert accepted == ["task-A", "task-B", "task-C"]
    sent_texts = [s[2] for s in ch["client"].sent]
    assert "任务A" in sent_texts[0] and "任务B" in sent_texts[1] and "任务C" in sent_texts[2]
    assert ch["bridge"].queue_size() == 0
    assert ch["bridge"].inflight_attempt is None


def test_3f2_offline_pending_keeps_queue_then_releases_on_recovery(qapp, tmp_path):
    # 离线 pending 期间：后续任务 B/C 持续入队但 busy 不越序、不被接受；
    # A 恢复写出成功释放后，队列从队首 B 继续。
    from openchamber_client import TaskResultResult

    ch = _real_chain(tmp_path)
    ch["bridge"].set_listening(True)
    # A 离线：不写 clip 状态
    _push_remote(ch["bridge"], ch["remote"], "evt-A", _task_envelope("task-A", "任务A"))
    ch["monitor"].run_once()
    process_monitor_events(ch["monitor"], ch["window"], ch["bridge"])  # A accepted 回执
    ch["client"].result = TaskResultResult(True, True, "结果A", 10, False, False, None)
    ch["monitor"].run_once()
    process_monitor_events(ch["monitor"], ch["window"], ch["bridge"])  # A 结果 → pending（离线）
    assert ch["bridge"]._pending_result is not None
    assert ch["controller"]._auto_task is not None  # 3F-1：pending 保持占用

    # B、C 在 A 未释放期间到达
    _push_remote(ch["bridge"], ch["remote"], "evt-B", _task_envelope("task-B", "任务B"))
    ch["monitor"].run_once()  # B 提交 → busy（A 活动）
    process_monitor_events(ch["monitor"], ch["window"], ch["bridge"])  # busy → B 回队首
    _push_remote(ch["bridge"], ch["remote"], "evt-C", _task_envelope("task-C", "任务C"))
    ch["bridge"].tick()  # B 在途 → C 不得越过
    assert ch["client"].sent and "任务A" in ch["client"].sent[-1][2]  # 仍只有 A 被接受

    # A 恢复在线 → flush 写成功 → 唯一 ack → A 释放（同轮还处理 B 的在途 busy 回执）
    _write_cliplink_status(ch["clip_status"], status="connected", updated_at=now_millis())
    ch["bridge"].tick()
    assert len(ch["clipboard"]) == 1 and "TASK_ID: task-A\n" in ch["clipboard"][0]
    ch["monitor"].run_once()  # B 在途 busy + ack A → A finish
    process_monitor_events(ch["monitor"], ch["window"], ch["bridge"])  # B busy → 回队首
    assert ch["controller"]._auto_task is None

    # 队列从队首 B 继续（不越序）
    ch["bridge"].tick()
    ch["monitor"].run_once()  # B accepted
    assert "任务B" in ch["client"].sent[-1][2]
    process_monitor_events(ch["monitor"], ch["window"], ch["bridge"])
    ch["client"].result = TaskResultResult(True, True, "结果B", 10, False, False, None)
    ch["monitor"].run_once()
    process_monitor_events(ch["monitor"], ch["window"], ch["bridge"])  # B 写成功 → ack
    ch["monitor"].run_once()  # B finish
    assert ch["controller"]._auto_task is None
    ch["bridge"].tick()  # C 才轮到
    ch["monitor"].run_once()
    assert "任务C" in ch["client"].sent[-1][2]
    process_monitor_events(ch["monitor"], ch["window"], ch["bridge"])
    sent_texts = [s[2] for s in ch["client"].sent]
    assert "任务A" in sent_texts[0] and "任务B" in sent_texts[1] and "任务C" in sent_texts[2]
    assert ch["bridge"].queue_size() == 0


def test_3f2_submit_then_ui_error_no_double_submit(qapp, tmp_path):
    # 提交点（monitor 命令入队）之后的 UI 抛错：本地日志收敛、不重投；
    # 在途保持，命令队列不出现重复 submit。
    from auto_monitor import AutoMonitor
    from cliplink_bridge import ClipLinkBridge
    from cliplink_status import now_millis as _now

    class BoomWindow(MainWindow):
        def set_current_task(self, text):
            raise RuntimeError("UI boom after commit")

    remote = tmp_path / "remote_clipboard.json"
    bridge = ClipLinkBridge(
        remote_event_path=remote,
        cliplink_status_path=tmp_path / "clip_status.json",
        status_file_path=tmp_path / "bridge_status.json",
        clipboard_writer=lambda t: None,
    )
    from active_session_reader import ActiveSession

    session = ActiveSession(session_id="ses_z", directory=r"C:\t", source="fake")
    client = _ChainClient()
    controller = LiteController(client, active_session_reader=lambda: session)
    monitor = AutoMonitor(controller, interval_seconds=1.0)
    w = BoomWindow()
    w.show()
    wire_ui(w, controller, bridge, monitor)
    bridge.set_listening(True)

    bridge.enqueue_remote_task(
        RemoteTask("evt-1", _task_envelope("task-1", "任务1"), "h", _now())
    )
    bridge.tick()  # 提交点已过（命令入队）；其后 set_current_task 抛错被本地收敛
    assert monitor._cmd.qsize() == 1  # 恰好 1 条 submit，无重复入命令
    assert bridge.inflight_attempt is not None  # 在途保留，等回执
    bridge.tick()  # 在途闸门：不重投、不产生第 2 条命令
    assert monitor._cmd.qsize() == 1
    assert "不影响接管" in _log(w)  # 可见日志收敛
    monitor.run_once()  # worker 处理唯一 submit → 首发送
    assert len(client.sent) == 1


def test_3f2_complete_bypass_busy_and_queue_continues_on_relisten(qapp, tmp_path):
    # COMPLETE 不被 busy 普通任务长期挡住：B 反复 busy 时，C=COMPLETE
    # 经控制旁路立即停监听（B 在途保留、D 排队）；A 活动任务结果回传不被中断；
    # 重新监听后队列从队首继续，COMPLETE 不重投。
    from openchamber_client import TaskResultResult

    ch = _real_chain(tmp_path)
    from openchamber_client import SessionStatusResult
    ch["client"].get_session_status = lambda session_id: SessionStatusResult(True, "busy", None)
    w = ch["window"]
    bridge = ch["bridge"]
    w.set_listening(True)
    _write_cliplink_status(ch["clip_status"], status="connected", updated_at=now_millis())

    seen_ids: list[str] = []
    orig_handler = bridge.on_remote_task

    def recording_handler(t):
        seen_ids.append(t.event_id)
        return orig_handler(t)

    bridge.on_remote_task = recording_handler

    # A 接管并活动（accepted）
    _push_remote(bridge, ch["remote"], "evt-A", _task_envelope("task-A", "任务A"))
    ch["monitor"].run_once()
    process_monitor_events(ch["monitor"], w, bridge)  # A accepted → 在途释放
    # B 提交 → busy（A 活动）→ 回队首
    _push_remote(bridge, ch["remote"], "evt-B", _task_envelope("task-B", "任务B"))
    ch["monitor"].run_once()
    process_monitor_events(ch["monitor"], w, bridge)  # B busy → 回队首
    # C=COMPLETE、D=普通任务入队（_push_remote 的 tick 会先投队首 B 的新一轮在途）
    _push_remote(bridge, ch["remote"], "evt-C", "AI_RELAY_COMPLETE")  # tick：B#2 投出（在途）
    _push_remote(bridge, ch["remote"], "evt-D", _task_envelope("task-D", "任务D"))  # 在途 → 不投

    bridge.tick()  # 控制旁路：C 越过在途 B 立即处理 → 停监听
    assert seen_ids[-1] == "evt-C"  # COMPLETE 未被队首 busy 的 B 长期挡住
    assert w._listening is False
    assert bridge._last_consumed_event_id == "evt-C"  # 控制事件 consumed 不污染快照恢复

    # A 活动任务的结果回传不受 COMPLETE 影响（同轮 B#2 busy 回执也处理）
    ch["client"].result = TaskResultResult(True, True, "结果A", 10, False, False, None)
    ch["monitor"].run_once()
    process_monitor_events(ch["monitor"], w, bridge)  # B#2 busy → 回队首；A 写成功 → ack 入队
    assert len(ch["clipboard"]) == 1 and "TASK_ID: task-A\n" in ch["clipboard"][0]
    ch["monitor"].run_once()  # A finish
    assert ch["controller"]._auto_task is None
    assert bridge.queue_size() == 2  # B、D 保留等待（停止监听不清队列）

    # 重新监听：队列从队首 B 继续；COMPLETE 不重投
    w.set_listening(True)
    bridge.tick()
    ch["monitor"].run_once()  # B accepted
    assert "任务B" in ch["client"].sent[-1][2]
    process_monitor_events(ch["monitor"], w, bridge)
    assert "evt-C" not in seen_ids[seen_ids.index("evt-C") + 1:]  # C 不重投
    ch["client"].result = TaskResultResult(True, True, "结果B", 10, False, False, None)
    ch["monitor"].run_once()
    process_monitor_events(ch["monitor"], w, bridge)  # B 写成功 → ack
    ch["monitor"].run_once()  # B finish
    bridge.tick()
    ch["monitor"].run_once()  # D 才轮到
    assert "任务D" in ch["client"].sent[-1][2]
    sent_texts = [s[2] for s in ch["client"].sent]
    assert "任务A" in sent_texts[0] and "任务B" in sent_texts[1] and "任务D" in sent_texts[2]
    assert ch["bridge"].queue_size() == 0
