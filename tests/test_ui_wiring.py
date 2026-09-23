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
from controller import AutoTaskIntake, CurrentSession, LiteController, ModelConnectionResult
from main import (
    apply_cliplink_status,
    process_monitor_events,
    start_bridge_poll,
    start_cliplink_poll,
    start_monitor_poll,
    wire_ui,
)
from openchamber_client import CompactResult, SendResult
from ui.main_window import MainWindow


class FakeController:
    """LiteController 的替身：记录调用、按预设返回，绝不起真实 HTTP。"""

    def __init__(self, session=None, model_result=None, send=None, compact=None, auto=None) -> None:
        self._session = session if session is not None else CurrentSession(None, None, None, False, "当前激活会话不可用")
        self._model = model_result if model_result is not None else ModelConnectionResult(False, None, False, "timeout")
        self._send = send if send is not None else SendResult(True, None, "msg_1")
        self._compact = compact if compact is not None else CompactResult(True, None)
        self._auto = auto  # 预设 AutoTaskIntake；None 时按 model 推导
        self.refresh_calls = 0
        self.model_calls = 0
        self.sent_texts = []
        self.compact_calls = 0
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


class FakeBridge:
    """ClipLinkBridge 的替身：记录调用，不读写真实文件。"""

    def __init__(self) -> None:
        self.listening_calls: list[bool] = []
        self.session_ids: list[str | None] = []
        self.model_statuses: list[tuple] = []
        self.first_response_calls: list = []
        self.delivered: list[str] = []
        self.on_remote_task = None

    def set_listening(self, enabled: bool) -> None:
        self.listening_calls.append(enabled)

    def set_session_id(self, session_id: str | None) -> None:
        self.session_ids.append(session_id)

    def set_model_status(self, status: str, latency_ms: int | None = None) -> None:
        self.model_statuses.append((status, latency_ms))

    def set_model_first_response(self, latency_ms) -> None:
        self.first_response_calls.append(latency_ms)

    def deliver_result(self, text: str) -> None:
        self.delivered.append(text)

    def tick(self) -> None:
        pass


class FakeMonitor:
    """AutoMonitor 替身：记录 submit/ack，可手动 push 事件供 process_monitor_events drain。"""

    def __init__(self) -> None:
        self.submit_calls: list[tuple[str, str, str]] = []
        self.ack_calls: list[str] = []
        self.stop_calls = 0
        self._events: list[dict] = []

    def submit_remote_task(self, event_id: str, text: str, wrapper_template: str) -> None:
        self.submit_calls.append((event_id, text, wrapper_template))

    def acknowledge_result(self, event_id: str) -> None:
        self.ack_calls.append(event_id)

    def stop(self) -> None:
        self.stop_calls += 1

    def drain_events(self) -> list[dict]:
        out = self._events
        self._events = []
        return out

    def push(self, ev: dict) -> None:
        self._events.append(ev)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _wired(qapp, controller, bridge=None, monitor=None):
    w = MainWindow()
    w.show()
    wire_ui(w, controller, bridge if bridge is not None else FakeBridge(), monitor)
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


def test_remote_task_uses_wrapper_template(qapp):
    # 17: on_remote_task 只 submit 给 monitor（读 window.wrapper_template()），
    # 不再直接调 controller.receive_auto_task；主线程立即显示"正在处理"（0 HTTP）
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    w._wrapper_template = "前{content}后"
    bridge.on_remote_task(RemoteTask("evt_new", "A端任务内容", "hash1", 12345))
    assert mon.submit_calls == [("evt_new", "A端任务内容", "前{content}后")]
    assert fake.auto_intakes == []  # 主线程不再直接调 controller
    assert _box(w, "task_box") == "已收到A端任务，正在处理"


def test_remote_task_ready_ui(qapp):
    # 18: 主线程立即"正在处理"；monitor 回 intake_ready → "已包装，等待大模型恢复"
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    bridge.on_remote_task(RemoteTask("evt_new", "A端任务内容", "hash1", 12345))
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
    bridge.on_remote_task(RemoteTask("evt_new", "A端任务内容", "hash1", 12345))
    mon.push({"type": "intake_running"})
    process_monitor_events(mon, w, bridge)
    assert _box(w, "task_box") == "已提交到大模型，等待执行"
    assert "收到 A端新任务，已包装并提交到当前会话" in _log(w)


def test_remote_task_busy_logs(qapp):
    # 20: intake_busy → 只记日志，不改当前任务框（保持 on_remote_task 的立即文案）
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    bridge.on_remote_task(RemoteTask("evt_new", "A端任务内容", "hash1", 12345))
    assert _box(w, "task_box") == "已收到A端任务，正在处理"
    mon.push({"type": "intake_busy"})
    process_monitor_events(mon, w, bridge)
    assert "自动任务未接收：已有任务正在处理" in _log(w)
    assert _box(w, "task_box") == "已收到A端任务，正在处理"  # busy 不改任务框


def test_remote_task_independent_of_a_connection(qapp):
    # 21: A端断连也不影响 submit（A 端只影响最终回传，不影响包装/发送）
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    w.set_a_connection("已断开", "--", None)  # A 端不可用
    bridge.on_remote_task(RemoteTask("evt_new", "A端任务内容", "hash1", 12345))
    assert mon.submit_calls == [("evt_new", "A端任务内容", "{content}")]
    mon.push({"type": "intake_running"})
    process_monitor_events(mon, w, bridge)
    assert _box(w, "task_box") == "已提交到大模型，等待执行"


# --- 自动任务最终结果 / 首响应 的 UI 回传接线（monitor 事件驱动）-----------


def test_result_complete_deliver_before_ack(qapp):
    # 22: result_complete 时 bridge.deliver_result 必须先于 monitor.acknowledge_result
    seq: list[str] = []

    class OrderBridge(FakeBridge):
        def deliver_result(self, text):
            super().deliver_result(text)
            seq.append("deliver")

    class OrderMonitor(FakeMonitor):
        def acknowledge_result(self, eid):
            super().acknowledge_result(eid)
            seq.append("ack")

    fake = FakeController()
    bridge = OrderBridge()
    mon = OrderMonitor()
    w = _wired(qapp, fake, bridge, mon)
    mon.push({"type": "result_complete", "event_id": "evt_1", "text": "R", "first_response_ms": None})
    process_monitor_events(mon, w, bridge)
    assert seq == ["deliver", "ack"]  # 先交 Bridge，再让 monitor 释放 Controller 任务


def test_result_complete_updates_recent_result(qapp):
    # 23: result_complete → 更新最近结果 + 任务文案 + 交 Bridge + ack
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    assert _box(w, "result_box") == "暂无结果"
    mon.push({"type": "result_complete", "event_id": "evt_1", "text": "最终答案", "first_response_ms": None})
    process_monitor_events(mon, w, bridge)
    assert _box(w, "result_box") == "最终答案"
    assert _box(w, "task_box") == "自动任务已完成，结果已进入回传链路"
    assert "大模型任务完成" in _log(w)
    assert bridge.delivered == ["最终答案"]
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


def test_first_response_preserves_oc_latency(qapp):
    # 25: 首响应 UI 更新时保留已有 OC 服务延迟（不被清成 --）
    fake = FakeController(model_result=ModelConnectionResult(True, 15, True, None))
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    w.test_model_requested.emit()  # status=正常, OC 服务延迟=15ms
    assert "15 ms" in _label(w, "llm_oc")
    mon.push({"type": "first_response", "first_response_ms": 420})
    process_monitor_events(mon, w, bridge)
    assert "15 ms" in _label(w, "llm_oc")  # OC 延迟保留
    assert "420 ms" in _label(w, "llm_first")  # 首响应已显示


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
    # 5: 严格命中 → 不读取/不消费 wrapper 模板；普通任务则会带上包装模板
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    w._wrapper_template = "前{content}后"
    bridge.on_remote_task(RemoteTask("evt_c", "AI_RELAY_COMPLETE", "h", 1))
    assert mon.submit_calls == []
    bridge.on_remote_task(RemoteTask("evt_n", "normal", "h", 2))
    assert mon.submit_calls == [("evt_n", "normal", "前{content}后")]


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
    # 8+9+10: 任何非严格相等 → 不命中，作为普通 RemoteTask submit，监听状态不变
    fake = FakeController()
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    bridge.on_remote_task(RemoteTask("evt_x", text, "h", 1))
    assert mon.submit_calls == [("evt_x", text, "{content}")]
    assert w._listening is False  # 非命中不触发 COMPLETE 停止逻辑


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


def test_new_task_resets_first_response_keeps_oc(qapp):
    # 11: 普通新任务开始 → 首响应清为 None（UI 显示 -- ms），保留 OC 服务延迟
    fake = FakeController(model_result=ModelConnectionResult(True, 18, True, None))
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, fake, bridge, mon)
    w.test_model_requested.emit()  # OC 服务延迟 18ms
    mon.push({"type": "first_response", "first_response_ms": 820})
    process_monitor_events(mon, w, bridge)
    assert "820 ms" in _label(w, "llm_first")
    assert bridge.first_response_calls[-1] == 820
    # 新任务：清首响应，保留 OC 服务延迟
    bridge.on_remote_task(RemoteTask("evt_n", "new task", "h", 2))
    assert "18 ms" in _label(w, "llm_oc")
    assert "-- ms" in _label(w, "llm_first")
    assert w._model_state["first_response_ms"] is None
    assert bridge.first_response_calls[-1] is None
    assert mon.submit_calls == [("evt_n", "new task", "{content}")]


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