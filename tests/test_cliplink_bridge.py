"""cliplink_bridge 全 fake 测试：remote event 读取/去重、结果回传/pending、状态文件。

覆盖 19 个场景，全 tmp_path + FakeClipboardWriter，不碰真实 %LOCALAPPDATA%。
"""

import json
from pathlib import Path

import pytest

from cliplink_bridge import (
    AI_RELAY_BEGIN_MARKER,
    AI_RELAY_END_MARKER,
    ClipLinkBridge,
    RemoteTask,
    default_remote_event_path,
    default_status_file_path,
    extract_envelope,
    is_return_envelope,
    envelope_route,
    is_nested_return,
    task_fingerprint,
)
from cliplink_status import STALE_MS, now_millis


class FakeClipboardWriter:
    def __init__(self, fail: bool = False) -> None:
        self.written: list[str] = []
        self.fail = fail

    def __call__(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("clipboard write failed")
        self.written.append(text)


def _write_remote_event(path: Path, **fields) -> Path:
    base = {"version": 1, "event_id": "evt1", "text": "hello", "content_hash": "abc123", "updated_at": 1000}
    base.update(fields)
    path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    return path


def _write_cliplink_status(path: Path, **fields) -> Path:
    base = {
        "version": 1,
        "status": "connected",
        "peer_name": None,
        "peer_ip": None,
        "latency_ms": None,
        "generation": 0,
        "updated_at": now_millis(),
    }
    base.update(fields)
    path.write_text(json.dumps(base), encoding="utf-8")
    return path


def _bridge(tmp_path: Path, **kwargs) -> ClipLinkBridge:
    defaults = dict(
        remote_event_path=tmp_path / "remote_clipboard.json",
        cliplink_status_path=tmp_path / "status.json",
        status_file_path=tmp_path / "AIRelayLite" / "status.json",
        clipboard_writer=FakeClipboardWriter(),
    )
    defaults.update(kwargs)
    return ClipLinkBridge(**defaults)


# ── 1-3: remote event 读取 ────────────────────────────────────────


def test_01_read_valid_remote_event(tmp_path):
    p = tmp_path / "remote_clipboard.json"
    _write_remote_event(p, event_id="evt_x", text="  你好\n世界  ", content_hash="h1", updated_at=42)
    b = _bridge(tmp_path)
    task = b._read_remote_event()
    assert task is not None
    assert task.event_id == "evt_x"
    assert task.text == "  你好\n世界  "
    assert task.content_hash == "h1"
    assert task.updated_at == 42


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        "[1,2,3]",
        json.dumps({"version": 1, "text": "x", "content_hash": "h", "updated_at": 1}),
        json.dumps({"version": 1, "event_id": "e", "content_hash": "h", "updated_at": 1}),
        json.dumps({"version": 1, "event_id": "e", "text": "x", "updated_at": 1}),
        json.dumps({"version": 1, "event_id": "e", "text": "x", "content_hash": "h"}),
        json.dumps({"version": 1, "event_id": "", "text": "x", "content_hash": "h", "updated_at": 1}),
        json.dumps({"version": 1, "event_id": 123, "text": "x", "content_hash": "h", "updated_at": 1}),
        json.dumps({"version": 1, "event_id": "e", "text": 42, "content_hash": "h", "updated_at": 1}),
    ],
)
def test_02_corrupt_or_missing_fields_returns_none(tmp_path, raw):
    p = tmp_path / "remote_clipboard.json"
    p.write_text(raw, encoding="utf-8")
    b = _bridge(tmp_path)
    assert b._read_remote_event() is None


def test_03_text_preserved_verbatim(tmp_path):
    text = "  leading\ntrailing  \t tab\r\n  "
    _write_remote_event(tmp_path / "remote_clipboard.json", text=text)
    b = _bridge(tmp_path)
    task = b._read_remote_event()
    assert task is not None
    assert task.text == text


# ── 4-8: listening / baseline / 去重 ──────────────────────────────


def test_04_existing_event_triggers_once_after_listening(tmp_path):
    """已存在未消费事件，B 开监听后必须触发（回归③验收①）；消费后绝不重复触发。"""
    p = tmp_path / "remote_clipboard.json"
    _write_remote_event(p, event_id="evt1")
    b = _bridge(tmp_path)
    b.set_listening(True)
    task = b.poll()
    assert task is not None and task.event_id == "evt1"
    # poll 纯检测不标 consumed：交付前反复 poll 都拿到同一未消费事件
    assert b.poll() is not None
    b.on_remote_task = lambda t: None
    b.tick()
    assert b.poll() is None


def test_05_same_event_id_not_retriggered(tmp_path):
    """同一 event_id 已消费后，重写该事件绝不重复触发（回归③验收②）。"""
    p = tmp_path / "remote_clipboard.json"
    _write_remote_event(p, event_id="evt1", text="first")
    b = _bridge(tmp_path)
    b.set_listening(True)
    b.on_remote_task = lambda t: None
    b.tick()  # 首次消费
    _write_remote_event(p, event_id="evt1", text="second")
    assert b.poll() is None
    b.tick()  # 重写后仍不重复触发


def test_06_new_event_id_triggers_once(tmp_path):
    # 阶段 3F-2 语义变化：文件不再是运行时事件源；新帧经 sink 入队（模拟 client 收帧）
    p = tmp_path / "remote_clipboard.json"
    _write_remote_event(p, event_id="evt1")
    b = _bridge(tmp_path)
    b.set_listening(True)
    b.on_remote_task = lambda t: "ignored"
    b.tick()  # evt1 经启动快照导入后投递（终态 ignored）
    _write_remote_event(p, event_id="evt2", text="new")
    b.enqueue_remote_task(RemoteTask("evt2", "new", "h", 2000))  # sink 帧
    b.tick()
    assert b.poll() is None


def test_07_same_text_new_event_id_still_triggers(tmp_path):
    p = tmp_path / "remote_clipboard.json"
    _write_remote_event(p, event_id="evt1", text="same", content_hash="h")
    b = _bridge(tmp_path)
    b.set_listening(True)
    _write_remote_event(p, event_id="evt2", text="same", content_hash="h")
    task = b.poll()
    assert task is not None and task.event_id == "evt2"


def test_08_not_listening_no_trigger(tmp_path):
    p = tmp_path / "remote_clipboard.json"
    _write_remote_event(p, event_id="evt1")
    b = _bridge(tmp_path)
    assert b.poll() is None


def test_08b_relistening_does_not_rebaseline_unconsumed_triggers(tmp_path):
    """重新监听不去重化：未消费的新事件（evt2）仍会触发一次（回归③验收④）。"""
    p = tmp_path / "remote_clipboard.json"
    _write_remote_event(p, event_id="evt1")
    b = _bridge(tmp_path)
    b.set_listening(True)
    b.set_listening(False)
    _write_remote_event(p, event_id="evt2")
    b.set_listening(True)
    task = b.poll()
    assert task is not None and task.event_id == "evt2"


# ── 9: A 掉线不影响 inbound ───────────────────────────────────────


def test_09_a_offline_after_event_inbound_still_works(tmp_path):
    p = tmp_path / "remote_clipboard.json"
    sp = tmp_path / "status.json"
    _write_cliplink_status(sp, status="connected")
    _write_remote_event(p, event_id="evt1")
    b = _bridge(tmp_path)
    b.set_listening(True)
    _write_remote_event(p, event_id="evt2", text="arrived")
    _write_cliplink_status(sp, status="offline")
    task = b.poll()
    assert task is not None and task.event_id == "evt2" and task.text == "arrived"


# ── 10-15: 结果回传 + pending ────────────────────────────────────


def _make_available(tmp_path: Path) -> None:
    _write_cliplink_status(tmp_path / "status.json", status="connected", updated_at=now_millis())


def _make_unavailable(tmp_path: Path, status: str = "paused") -> None:
    _write_cliplink_status(tmp_path / "status.json", status=status)


def test_10_deliver_result_a_available_writes_clipboard(tmp_path):
    writer = FakeClipboardWriter()
    b = _bridge(tmp_path, clipboard_writer=writer)
    _make_available(tmp_path)
    b.deliver_result("result_text")
    assert writer.written == ["result_text"]
    assert b._pending_result is None


def test_11_deliver_result_paused_goes_pending(tmp_path):
    writer = FakeClipboardWriter()
    b = _bridge(tmp_path, clipboard_writer=writer)
    _make_unavailable(tmp_path, "paused")
    b.deliver_result("pending_text")
    assert writer.written == []
    assert b._pending_result == "pending_text"


@pytest.mark.parametrize("status", ["offline", "reconnecting", "connecting", "error"])
def test_12_deliver_result_other_states_goes_pending(tmp_path, status):
    writer = FakeClipboardWriter()
    b = _bridge(tmp_path, clipboard_writer=writer)
    _make_unavailable(tmp_path, status)
    b.deliver_result("x")
    assert writer.written == []
    assert b._pending_result == "x"


def test_13_deliver_result_connected_stale_goes_pending(tmp_path):
    writer = FakeClipboardWriter()
    b = _bridge(tmp_path, clipboard_writer=writer)
    _write_cliplink_status(
        tmp_path / "status.json", status="connected", updated_at=now_millis() - STALE_MS - 5000
    )
    b.deliver_result("stale_result")
    assert writer.written == []
    assert b._pending_result == "stale_result"


def test_14_flush_pending_on_recovery_writes_and_clears(tmp_path):
    writer = FakeClipboardWriter()
    b = _bridge(tmp_path, clipboard_writer=writer)
    _make_unavailable(tmp_path, "offline")
    b.deliver_result("was_pending")
    assert b._pending_result == "was_pending"
    _make_available(tmp_path)
    b.flush_pending_result()
    assert writer.written == ["was_pending"]
    assert b._pending_result is None


def test_15_clipboard_writer_failure_keeps_pending(tmp_path):
    writer = FakeClipboardWriter(fail=True)
    b = _bridge(tmp_path, clipboard_writer=writer)
    _make_available(tmp_path)
    b.deliver_result("will_fail")
    assert writer.written == []
    assert b._pending_result == "will_fail"


def test_15b_flush_writer_failure_keeps_pending(tmp_path):
    writer = FakeClipboardWriter()
    b = _bridge(tmp_path, clipboard_writer=writer)
    b._pending_result = "stuck"
    _make_unavailable(tmp_path, "offline")
    _write_cliplink_status(tmp_path / "status.json", status="connected", updated_at=now_millis())
    writer.fail = True
    b.flush_pending_result()
    assert b._pending_result == "stuck"


# ── 16-19: AIRelayLite 状态文件 ──────────────────────────────────


def test_16_status_file_schema(tmp_path):
    b = _bridge(tmp_path)
    b.write_status_file()
    raw = (tmp_path / "AIRelayLite" / "status.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["version"] == 1
    assert data["relay_status"] == "idle"
    assert data["model_status"] == "unknown"
    assert data["openchamber_latency_ms"] is None
    assert data["model_first_response_ms"] is None
    assert data["session_id"] is None
    assert isinstance(data["updated_at"], int)


def test_17_status_file_atomic_write_valid_json_no_tmp_remains(tmp_path):
    b = _bridge(tmp_path)
    b.write_status_file()
    sf = tmp_path / "AIRelayLite" / "status.json"
    assert sf.exists()
    json.loads(sf.read_text(encoding="utf-8"))
    assert not (tmp_path / "AIRelayLite" / "status.json.tmp").exists()


def test_18_relay_status_transitions(tmp_path):
    b = _bridge(tmp_path)
    assert b._relay_status == "idle"
    b.set_listening(True)
    assert b._relay_status == "listening"
    _make_available(tmp_path)
    b.deliver_result("x")
    assert b._relay_status == "listening"  # write succeeded, pending cleared
    b.set_listening(False)
    assert b._relay_status == "idle"


def test_19_model_session_latency_status_write(tmp_path):
    b = _bridge(tmp_path)
    b.set_model_status("ready", 42)
    b.set_session_id("ses_abc")
    b.set_listening(True)
    b.write_status_file()
    data = json.loads((tmp_path / "AIRelayLite" / "status.json").read_text(encoding="utf-8"))
    assert data["model_status"] == "ready"
    assert data["openchamber_latency_ms"] == 42
    assert data["session_id"] == "ses_abc"
    assert data["relay_status"] == "listening"
    assert data["model_first_response_ms"] is None


def test_19b_disconnected_clears_latency(tmp_path):
    b = _bridge(tmp_path)
    b.set_model_status("ready", 42)
    b.set_model_status("disconnected")
    b.write_status_file()
    data = json.loads((tmp_path / "AIRelayLite" / "status.json").read_text(encoding="utf-8"))
    assert data["model_status"] == "disconnected"
    assert data["openchamber_latency_ms"] is None


# ── L05-05: 真实模型首响应写状态文件 ────────────────────────────────


def test_l0505_model_first_response_int_written(tmp_path):
    b = _bridge(tmp_path)
    b.set_model_first_response(820)
    data = json.loads((tmp_path / "AIRelayLite" / "status.json").read_text(encoding="utf-8"))
    assert data["model_first_response_ms"] == 820


def test_l0505_model_first_response_none_writes_null(tmp_path):
    b = _bridge(tmp_path)
    b.set_model_first_response(820)
    b.set_model_first_response(None)
    data = json.loads((tmp_path / "AIRelayLite" / "status.json").read_text(encoding="utf-8"))
    assert data["model_first_response_ms"] is None


# ── FIX1: relay_status 生命周期 + status 写盘频率 ─────────────────


def test_fix1_01_listening_true_immediate_delivery_stays_listening(tmp_path):
    b = _bridge(tmp_path)
    b.set_listening(True)
    _make_available(tmp_path)
    b.deliver_result("r")
    assert b._pending_result is None
    assert b._relay_status == "listening"


def test_fix1_02_listening_false_immediate_delivery_goes_idle(tmp_path):
    b = _bridge(tmp_path)
    _make_available(tmp_path)
    b.deliver_result("r")
    assert b._pending_result is None
    assert b._relay_status == "idle"


def test_fix1_03_a_offline_pending_wait_return(tmp_path):
    b = _bridge(tmp_path)
    b.set_listening(True)
    _make_unavailable(tmp_path, "offline")
    b.deliver_result("r")
    assert b._pending_result == "r"
    assert b._relay_status == "wait_return"


def test_fix1_04_stop_listening_with_pending_stays_wait_return(tmp_path):
    b = _bridge(tmp_path)
    b.set_listening(True)
    _make_unavailable(tmp_path, "offline")
    b.deliver_result("r")
    assert b._relay_status == "wait_return"
    b.set_listening(False)
    assert b._relay_status == "wait_return"
    assert b._pending_result == "r"


def test_fix1_05_flush_success_listening_true_restores_listening(tmp_path):
    b = _bridge(tmp_path)
    b.set_listening(True)
    _make_unavailable(tmp_path, "offline")
    b.deliver_result("r")
    assert b._relay_status == "wait_return"
    _make_available(tmp_path)
    b.flush_pending_result()
    assert b._pending_result is None
    assert b._relay_status == "listening"


def test_fix1_06_flush_success_listening_false_restores_idle(tmp_path):
    b = _bridge(tmp_path)
    b.set_listening(True)
    _make_unavailable(tmp_path, "offline")
    b.deliver_result("r")
    b.set_listening(False)
    assert b._relay_status == "wait_return"
    _make_available(tmp_path)
    b.flush_pending_result()
    assert b._pending_result is None
    assert b._relay_status == "idle"


def test_fix1_07_writer_failure_pending_retained_wait_return(tmp_path):
    writer = FakeClipboardWriter(fail=True)
    b = _bridge(tmp_path, clipboard_writer=writer)
    b.set_listening(True)
    _make_available(tmp_path)
    b.deliver_result("r")
    assert b._pending_result == "r"
    assert b._relay_status == "wait_return"


def test_fix1_08_ticks_do_not_write_every_tick(tmp_path, monkeypatch):
    b = _bridge(tmp_path)
    fake_now = [10_000]
    monkeypatch.setattr("cliplink_bridge.now_millis", lambda: fake_now[0])
    b.tick()
    assert b._last_status_write_ms == 10_000
    fake_now[0] = 10_400
    b.tick()
    assert b._last_status_write_ms == 10_000
    fake_now[0] = 10_800
    b.tick()
    assert b._last_status_write_ms == 10_000
    fake_now[0] = 11_200
    b.tick()
    assert b._last_status_write_ms == 10_000


def test_fix1_09_heartbeat_refreshes_at_2s(tmp_path, monkeypatch):
    b = _bridge(tmp_path)
    fake_now = [10_000]
    monkeypatch.setattr("cliplink_bridge.now_millis", lambda: fake_now[0])
    b.tick()
    first = json.loads(b._status_file.read_text(encoding="utf-8"))["updated_at"]
    assert first == 10_000
    fake_now[0] = 12_000
    b.tick()
    second = json.loads(b._status_file.read_text(encoding="utf-8"))["updated_at"]
    assert second == 12_000
    assert second != first


# ── FIX2 (E2E-01-INBOUND-CONSUME-FIX1): consumed 时机 + 跨进程持久化 ──


def test_fix2_01_poll_does_not_mark_consumed(tmp_path):
    """poll 纯检测：交付完成前，反复 poll 都拿到同一未消费事件。"""
    p = tmp_path / "remote_clipboard.json"
    _write_remote_event(p, event_id="evt1")
    b = _bridge(tmp_path)
    b.set_listening(True)
    assert b.poll() is not None
    assert b.poll() is not None
    assert b.poll() is not None


def test_fix2_02_tick_marks_consumed_after_callback_success(tmp_path):
    """on_remote_task 正常返回后才标 consumed；同事件绝不重复投递。"""
    p = tmp_path / "remote_clipboard.json"
    _write_remote_event(p, event_id="evt1")
    b = _bridge(tmp_path)
    b.set_listening(True)
    seen: list[str] = []
    b.on_remote_task = lambda t: seen.append(t.event_id)
    b.tick()
    assert seen == ["evt1"]
    assert b.poll() is None
    b.tick()
    assert seen == ["evt1"]


def test_fix2_03_tick_callback_failure_keeps_unconsumed(tmp_path):
    """回调抛异常不吞、不标 consumed；下一 tick 同一事件再次投递。"""
    p = tmp_path / "remote_clipboard.json"
    _write_remote_event(p, event_id="evt1")
    b = _bridge(tmp_path)
    b.set_listening(True)

    def boom(t):
        raise RuntimeError("callback failed")

    b.on_remote_task = boom
    with pytest.raises(RuntimeError):
        b.tick()
    assert b.poll() is not None  # 仍未消费
    b.on_remote_task = lambda t: None
    b.tick()  # 重试成功
    assert b.poll() is None


def test_fix2_04_last_consumed_event_id_persisted_to_status_file(tmp_path):
    """消费前后 last_consumed_event_id 都写入 AIRelayLite status.json。

    阶段 3F-2 语义变化：文件在 Bridge 创建后写入，须经 sink 入队才参与投递
    （生产时序：帧由网络线程经 sink 到达，文件只是启动快照）。
    """
    b = _bridge(tmp_path)
    b.write_status_file()
    data = json.loads((tmp_path / "AIRelayLite" / "status.json").read_text(encoding="utf-8"))
    assert data["last_consumed_event_id"] is None
    p = tmp_path / "remote_clipboard.json"
    _write_remote_event(p, event_id="evt1")
    b.set_listening(True)
    b.on_remote_task = lambda t: None
    b.enqueue_remote_task(b._read_remote_event())  # 模拟 client sink 收帧
    b.tick()
    data = json.loads((tmp_path / "AIRelayLite" / "status.json").read_text(encoding="utf-8"))
    assert data["last_consumed_event_id"] == "evt1"


def test_fix2_05_restart_restores_consumed_state(tmp_path):
    """重启（新实例）恢复 last_consumed_event_id：
    已消费事件绝不重复执行；未消费事件重启后仍会执行一次。"""
    p = tmp_path / "remote_clipboard.json"
    _write_remote_event(p, event_id="evt1")
    b1 = _bridge(tmp_path)
    b1.set_listening(True)
    # b1 不 tick：evt1 保持未消费
    b2 = _bridge(tmp_path)  # “重启”
    b2.set_listening(True)
    task = b2.poll()
    assert task is not None and task.event_id == "evt1"  # 未消费仍触发
    b2.on_remote_task = lambda t: None
    b2.tick()  # 消费并持久化
    b3 = _bridge(tmp_path)  # 再次“重启”
    b3.set_listening(True)
    assert b3.poll() is None  # 已消费恢复，不重复执行
    _write_remote_event(p, event_id="evt2", text="new")
    assert b3.poll() is not None  # 新事件仍正常工作


def test_fix2_06_restore_missing_or_corrupt_status_file(tmp_path):
    """状态文件缺失 / 损坏 / 非 str → 恢复为 None（按未消费），不崩。"""
    sf = tmp_path / "AIRelayLite" / "status.json"
    sf.parent.mkdir(parents=True, exist_ok=True)
    assert _bridge(tmp_path)._last_consumed_event_id is None  # 文件缺失
    sf.write_text("{not json", encoding="utf-8")
    assert _bridge(tmp_path)._last_consumed_event_id is None
    sf.write_text(json.dumps({"last_consumed_event_id": 42}), encoding="utf-8")
    assert _bridge(tmp_path)._last_consumed_event_id is None
    sf.write_text(json.dumps({"last_consumed_event_id": "evt9"}), encoding="utf-8")
    assert _bridge(tmp_path)._last_consumed_event_id == "evt9"


# ── 路径 ─────────────────────────────────────────────────────────


def test_default_remote_event_path(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
    assert str(default_remote_event_path()) == r"C:\Users\test\AppData\Local\ClipLink\remote_clipboard.json"


def test_default_status_file_path(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
    assert str(default_status_file_path()) == r"C:\Users\test\AppData\Local\AIRelayLite\status.json"


# ── A-REPLY-WRAPPER-FIX1: AI_RELAY envelope extraction ────────────


def test_wrap_01_plain_envelope_extracts_inner():
    inner = "\nSOURCE: CHATGPT\nTARGET: EXECUTOR\nTYPE: TASK\nCONTENT:\nhello\n"
    text = AI_RELAY_BEGIN_MARKER + inner + AI_RELAY_END_MARKER
    assert extract_envelope(text) == inner


def test_wrap_02_leading_and_trailing_junk_ignored():
    inner = "\nCONTENT:\n带前缀后记的包\n"
    text = "ChatGPT 前言 ```text\n" + AI_RELAY_BEGIN_MARKER + inner + AI_RELAY_END_MARKER + "\n``` 后记"
    assert extract_envelope(text) == inner


def test_wrap_03_half_package_only_begin_returns_none():
    text = "废话\n" + AI_RELAY_BEGIN_MARKER + "\nSOURCE: CHATGPT\n"
    assert extract_envelope(text) is None


def test_wrap_04_no_markers_returns_none():
    assert extract_envelope("普通剪贴板文本，没有协议标记") is None


def test_wrap_05_begin_after_end_returns_none():
    text = AI_RELAY_END_MARKER + "\n" + AI_RELAY_BEGIN_MARKER + "\nCONTENT:\nx"
    assert extract_envelope(text) is None


def test_wrap_06_only_begin_marker_returns_none():
    assert extract_envelope(AI_RELAY_BEGIN_MARKER) is None


def test_wrap_07_complete_marker_preserved_in_case_insensitive_wrap():
    text = "log line\n" + AI_RELAY_BEGIN_MARKER + "\nAI_RELAY_COMPLETE" + AI_RELAY_END_MARKER
    assert extract_envelope(text) == "\nAI_RELAY_COMPLETE"

def test_wrap_08_nested_task_preserved_through_outer_end():
    inner = AI_RELAY_BEGIN_MARKER + "\nTASK_ID: previous\nCONTENT:\n继续执行\n" + AI_RELAY_END_MARKER
    payload = "\nTASK_ID: current\nCONTENT:\n" + inner + "\n"
    assert extract_envelope(AI_RELAY_BEGIN_MARKER + payload + AI_RELAY_END_MARKER) == payload

def test_wrap_09_unclosed_outer_package_is_not_executed():
    inner = AI_RELAY_BEGIN_MARKER + "\nCONTENT:\n任务\n" + AI_RELAY_END_MARKER
    assert extract_envelope(AI_RELAY_BEGIN_MARKER + "\nCONTENT:\n" + inner) is None

def test_return_envelope_ignores_body_headers():
    assert is_return_envelope("\nSOURCE: EXECUTOR\nTARGET: CHATGPT\nTYPE: RESPONSE\nCONTENT:\n结果")
    assert not is_return_envelope("\nSOURCE: CHATGPT\nTARGET: EXECUTOR\nTYPE: TASK\nCONTENT:\n"
                                  "SOURCE: EXECUTOR\nTARGET: CHATGPT\nTYPE: RESPONSE")

def _routed_frame(task_id="one", round_number=1, content="work", version="legacy", time="today"):
    prefix = "AI_RELAY/1" if version == "v1" else AI_RELAY_BEGIN_MARKER
    return (f"{prefix}\nSOURCE: CHATGPT\nTARGET: EXECUTOR\nTYPE: TASK\n"
            f"TASK_ID: {task_id}\nROUND: {round_number}\nMAX_ROUNDS: 100\n"
            f"TIME: {time}\nCONTENT:\n{content}\n{AI_RELAY_END_MARKER}")

def test_routing_legacy_v1_and_nested_return():
    for version in ("legacy", "v1"):
        payload = extract_envelope(_routed_frame(version=version))
        assert envelope_route(payload) == ("CHATGPT", "EXECUTOR", "TASK")
        assert task_fingerprint(_routed_frame(version=version))
    response = (f"{AI_RELAY_BEGIN_MARKER}\nSOURCE: EXECUTOR\nTARGET: CHATGPT\n"
                f"TYPE: RESPONSE\nTASK_ID: old\nCONTENT:\nanswer\n{AI_RELAY_END_MARKER}")
    assert is_nested_return(extract_envelope(_routed_frame(content=response)))
    assert not is_nested_return(extract_envelope(_routed_frame(content="analyze " + response)))
    assert task_fingerprint(response) is None

def test_duplicate_frames_do_not_reenter_queue_or_restart(tmp_path):
    bridge = _bridge(tmp_path)
    bridge.set_listening(True)
    seen = []
    bridge.on_remote_task = lambda task: seen.append(task.event_id) or "submitted"
    first = _routed_frame(time="first")
    duplicate = _routed_frame(time="second").replace("\n", "\r\n")
    assert task_fingerprint(first) == task_fingerprint(duplicate)
    bridge.enqueue_remote_task(RemoteTask("first", first, "", 1))
    bridge.enqueue_remote_task(RemoteTask("same", duplicate, "", 2))
    assert bridge.queue_size() == 1
    bridge.tick()
    bridge.enqueue_remote_task(RemoteTask("while-running", duplicate, "", 3))
    assert bridge.queue_size() == 0
    assert bridge.resolve_inflight("first", True)
    bridge.enqueue_remote_task(RemoteTask("after-accepted", duplicate, "", 4))
    assert bridge.queue_size() == 0
    assert seen == ["first"]
    restored = _bridge(tmp_path)
    restored.enqueue_remote_task(RemoteTask("after-restart", duplicate, "", 5))
    assert restored.queue_size() == 0
    restored.enqueue_remote_task(RemoteTask("next-round", _routed_frame(round_number=2), "", 6))
    restored.enqueue_remote_task(RemoteTask("new-body", _routed_frame(content="different"), "", 7))
    assert restored.queue_size() == 2


# ── 阶段 3F-1：回传单槽占用保护 + 本地写入完成通知 ─────────────────


def _bridge_with_calls(tmp_path, **kwargs):
    """Bridge + 记录 on_local_write 调用的桩。"""
    calls: list[str | None] = []
    b = _bridge(tmp_path, **kwargs)
    b.on_local_write = calls.append
    return b, calls


def test_3f1_slot_rejects_different_event_id_keeps_old(tmp_path):
    writer = FakeClipboardWriter()
    b, calls = _bridge_with_calls(tmp_path, clipboard_writer=writer)
    _make_unavailable(tmp_path, "offline")
    assert b.deliver_result("result-A", event_id="evt-1") == "pending"
    assert b.deliver_result("result-B", event_id="evt-2") == "rejected"
    assert b._pending_result == "result-A"  # 旧记录保留，未静默覆盖
    assert b._pending_event_id == "evt-1"
    assert writer.written == []
    assert calls == []  # 没有写入成功：无通知


def test_3f1_slot_rejects_conflicting_text_same_event_id(tmp_path):
    writer = FakeClipboardWriter()
    b, calls = _bridge_with_calls(tmp_path, clipboard_writer=writer)
    _make_unavailable(tmp_path, "offline")
    assert b.deliver_result("text-A", event_id="evt-1") == "pending"
    assert b.deliver_result("text-B", event_id="evt-1") == "rejected"  # 冲突文本不得替换
    assert b._pending_result == "text-A"
    assert calls == []


def test_3f1_duplicate_same_event_same_text_no_rewrite_no_renotify(tmp_path):
    writer = FakeClipboardWriter()
    b, calls = _bridge_with_calls(tmp_path, clipboard_writer=writer)
    _make_unavailable(tmp_path, "offline")
    assert b.deliver_result("same", event_id="evt-1") == "pending"
    assert b.deliver_result("same", event_id="evt-1") == "duplicate"  # 不重写/不重复确认
    assert writer.written == []
    assert calls == []
    _make_available(tmp_path)
    b.flush_pending_result()
    assert writer.written == ["same"]  # 恢复后只写一次
    assert calls == ["evt-1"]  # 一次、带正确 event_id
    assert b._pending_result is None


def test_3f1_flush_failure_keeps_packet_and_retries_same_tick_loop(tmp_path):
    writer = FakeClipboardWriter(fail=True)
    b, calls = _bridge_with_calls(tmp_path, clipboard_writer=writer)
    _make_unavailable(tmp_path, "offline")
    packet = "包装回包 TIME 固定\nTASK_ID: task-1"
    assert b.deliver_result(packet, event_id="evt-9") == "pending"
    _make_available(tmp_path)
    b.flush_pending_result()  # 写失败
    assert writer.written == []
    assert b._pending_result == packet  # 完全相同的文本/TIME
    assert b._pending_event_id == "evt-9"  # 关联 id 保留
    assert calls == []  # 无写入成功：无通知
    writer.fail = False
    b.flush_pending_result()  # 下次既有 tick 重试成功
    assert writer.written == [packet]
    assert b._pending_result is None
    assert calls == ["evt-9"]


def test_3f1_no_write_success_no_notification(tmp_path):
    writer = FakeClipboardWriter(fail=True)
    b, calls = _bridge_with_calls(tmp_path, clipboard_writer=writer)
    _make_available(tmp_path)
    assert b.deliver_result("x", event_id="evt-1") == "pending"  # 在线但写失败
    assert calls == []


def test_3f1_online_write_success_notifies_with_event_id_and_clears(tmp_path):
    writer = FakeClipboardWriter()
    b, calls = _bridge_with_calls(tmp_path, clipboard_writer=writer)
    _make_available(tmp_path)
    assert b.deliver_result("online-r", event_id="evt-7") == "flushed"
    assert writer.written == ["online-r"]
    assert b._pending_result is None
    assert calls == ["evt-7"]


def test_3f1_event_id_none_standalone_flush_notifies_none(tmp_path):
    # 单独测试 Bridge 的无 event_id 调用：可接管/写出，通知 event_id=None（GUI 不得据此自动 ack）
    writer = FakeClipboardWriter()
    b, calls = _bridge_with_calls(tmp_path, clipboard_writer=writer)
    _make_available(tmp_path)
    assert b.deliver_result("standalone") == "flushed"
    assert calls == [None]
    _make_unavailable(tmp_path, "offline")
    assert b.deliver_result("standalone-2") == "pending"
    assert b.deliver_result("other") == "rejected"  # 无关联记录不得覆盖占用槽
    assert calls == [None]


def _rt(event_id: str, text: str = "x") -> RemoteTask:
    return RemoteTask(event_id, text, "h", 1000)


def test_3f2_frames_before_tick_all_taken_over_fifo(tmp_path):
    """R1：tick 前连续多帧经 sink 到达 → 全部入队、按序投递、不丢。"""
    b = _bridge(tmp_path)
    b.set_listening(True)
    seen: list[str] = []
    b.on_remote_task = lambda t: seen.append(t.event_id) or "ignored"
    for eid in ("A", "B", "C"):
        b.enqueue_remote_task(_rt(eid))
    assert b.queue_size() == 3  # 三帧全部接管（旧实现单槽文件只见 C）
    b.tick()  # 在途闸门：每 tick 只投一项
    assert seen == ["A"]
    b.tick()
    b.tick()
    assert seen == ["A", "B", "C"]
    assert b.queue_size() == 0


def test_3f2_busy_requeues_head_no_overtake(tmp_path):
    """R2：busy 回队首、不标 consumed、不丢正文；后项不得越过队首。"""
    b = _bridge(tmp_path)
    b.set_listening(True)
    dispatched: list[str] = []

    def handler(t):
        dispatched.append(t.event_id)
        return "submitted" if t.event_id != "B" else "submitted"  # B 提交后 worker 判 busy

    b.on_remote_task = handler
    for eid in ("A", "B", "C"):
        b.enqueue_remote_task(_rt(eid))
    b.tick()  # A 提交 → 在途
    assert dispatched == ["A"]
    assert b.resolve_inflight("A", True)  # A accepted → 终态
    b.tick()  # B 提交
    assert dispatched == ["A", "B"]
    b.tick()  # 在途闸门：C 不得越过
    assert dispatched == ["A", "B"]
    assert b.resolve_inflight("B", False)  # B busy → 回队首
    assert b.queue_size() == 2  # [B, C]：B 仍在队首，正文保留
    b.tick()  # B 再次队首投递（重复投递允许）
    b.tick()
    b.resolve_inflight("B", True)  # B 本轮 accepted
    b.tick()  # C 才轮到
    assert dispatched == ["A", "B", "B", "C"]


def test_3f2_consumed_only_on_terminal(tmp_path):
    """busy 不标 consumed（重启/重放语义）；accepted/ignored 才标。"""
    p = tmp_path / "remote_clipboard.json"
    b = _bridge(tmp_path)
    b.set_listening(True)
    b.on_remote_task = lambda t: "submitted"
    b.enqueue_remote_task(_rt("E1"))
    b.tick()
    b.resolve_inflight("E1", False)  # busy → 回队首
    assert b._last_consumed_event_id is None  # busy 未终态
    b.tick()  # 重新投递（accepted 只匹配在途项）
    b.resolve_inflight("E1", True)  # accepted
    assert b._last_consumed_event_id == "E1"
    data = json.loads((tmp_path / "AIRelayLite" / "status.json").read_text(encoding="utf-8"))
    assert data["last_consumed_event_id"] == "E1"


def test_3f2_stale_attempt_receipt_ignored(tmp_path):
    """跨 busy 重试的旧 attempt 回执不得误配新一轮在途项。"""
    b = _bridge(tmp_path)
    b.set_listening(True)
    b.on_remote_task = lambda t: "submitted"
    b.enqueue_remote_task(_rt("E1"))
    b.tick()  # 投递 #1，attempt=1
    assert b.resolve_inflight("E1", False, attempt=1)  # 第 1 轮 busy
    b.tick()  # 投递 #2，attempt=2
    assert b.resolve_inflight("E1", False, attempt=1) is False  # 旧 attempt 迟到 → 忽略
    assert b.inflight_attempt == 2  # 在途未被误清
    assert b.queue_size() == 0
    assert b.resolve_inflight("E1", True, attempt=2)  # 本轮 accepted
    assert b._last_consumed_event_id == "E1"


def test_3f2_wrong_or_late_receipt_ignored(tmp_path):
    """错 id / 无在途（迟到）回执：忽略，不重复释放、不误清。"""
    b = _bridge(tmp_path)
    b.set_listening(True)
    b.on_remote_task = lambda t: "submitted"
    b.enqueue_remote_task(_rt("E1"))
    b.tick()
    assert b.resolve_inflight("other", True) is False
    assert b.inflight_attempt is not None  # 在途未动
    assert b.resolve_inflight("E1", True)
    assert b.resolve_inflight("E1", True) is False  # 迟到重复 → 忽略


def test_3f2_runtime_stale_snapshot_not_reinjected(tmp_path):
    """运行时陈旧快照（已消费事件仍驻文件）不再重注入/重执行。"""
    p = tmp_path / "remote_clipboard.json"
    _write_remote_event(p, event_id="F1", text="t1")
    b = _bridge(tmp_path)  # 启动导入 F1
    b.set_listening(True)
    seen: list[str] = []
    b.on_remote_task = lambda t: seen.append(t.event_id) or "submitted"
    b.tick()  # F1 投递
    assert seen == ["F1"]
    b.resolve_inflight("F1", True)  # 终态；文件里仍是 F1（陈旧快照）
    b.enqueue_remote_task(_rt("F2", "t2"))
    for _ in range(5):  # 连续多 tick：陈旧 F1 不得重入
        b.tick()
    assert seen == ["F1", "F2"]  # F2 只投递一次，F1 不重放
    b.resolve_inflight("F2", True)


def test_3f2_startup_snapshot_before_new_frame(tmp_path):
    """启动旧快照先于新帧：旧 F0 排在新 F1 之前，先投递。"""
    p = tmp_path / "remote_clipboard.json"
    _write_remote_event(p, event_id="F0", text="old")
    b = _bridge(tmp_path)  # __init__ 导入 F0
    b.set_listening(True)
    seen: list[str] = []
    b.on_remote_task = lambda t: seen.append(t.event_id) or "ignored"
    b.enqueue_remote_task(_rt("F1", "new"))  # start() 后新帧经 sink 到达
    b.tick()
    assert seen == ["F0"]  # 旧快照先投递
    b.tick()
    assert seen == ["F0", "F1"]


def test_3f2_complete_bypass_inflight_and_keeps_queue(tmp_path):
    """COMPLETE 控制事件旁路在途闸门：B 在途（busy 循环）时 C 立即被处理停监听；
    B 在途不被中断；D 保留等待；重开监听后从队首继续，COMPLETE 不重投。"""
    b = _bridge(tmp_path)
    b.set_listening(True)
    seen: list[str] = []

    def handler(t):
        seen.append(t.event_id)
        if t.text == "AI_RELAY_COMPLETE":
            b.set_listening(False)
            return "ignored"
        return "submitted"

    b.on_remote_task = handler
    b.enqueue_remote_task(_rt("B"))
    b.tick()  # B 提交 → 在途（attempt=1）
    assert seen == ["B"]
    b.enqueue_remote_task(RemoteTask("C", "AI_RELAY_COMPLETE", "h", 1))
    b.enqueue_remote_task(_rt("D"))
    b.tick()  # 控制旁路：B 在途期间 C 被立即处理（不被 busy 循环长期挡住）
    assert seen == ["B", "C"]  # C 越过在途 B 先行处理
    assert b._listening is False
    assert b.inflight_attempt is not None  # B 在途不被 COMPLETE 中断
    assert b.queue_size() == 1  # D 保留等待
    b.resolve_inflight("B", True)  # B 终态（活动任务结果回传不被 COMPLETE 中断）
    b.set_listening(True)
    b.tick()  # 重新监听：D 从队首继续（COMPLETE 不重投）
    assert seen == ["B", "C", "D"]
    assert b._last_consumed_event_id == "B"  # 末次终态=B；C 的 consumed 标记已在旁路时持久化


def test_3f2_precommit_exception_requeues_and_raises(tmp_path):
    """提交点前异常：回队首、下一 tick 重试，异常不吞（保持 3F-1 前契约）。"""
    b = _bridge(tmp_path)
    b.set_listening(True)
    seen: list[str] = []

    def flaky(t):
        seen.append(t.event_id)
        if len(seen) == 1:
            raise RuntimeError("pre-commit boom")

    b.on_remote_task = flaky
    b.enqueue_remote_task(_rt("E1"))
    with pytest.raises(RuntimeError):
        b.tick()
    assert b.queue_size() == 1  # 回队首

    def ok(t):
        seen.append(t.event_id)
        return "ignored"

    b.on_remote_task = ok
    b.tick()
    assert seen == ["E1", "E1"]
    assert b._last_consumed_event_id == "E1"


def test_3f2_not_listening_keeps_queue_resume_dispatches(tmp_path):
    """监听关闭期间队列保留（含 sink 新帧）；重新监听后从队首继续。"""
    b = _bridge(tmp_path)
    b.set_listening(True)
    seen: list[str] = []
    b.on_remote_task = lambda t: seen.append(t.event_id) or "ignored"
    b.enqueue_remote_task(_rt("A"))
    b.set_listening(False)
    b.enqueue_remote_task(_rt("B"))  # 关闭期间新帧仍入队
    b.tick()
    assert seen == []  # 未监听：不投递
    b.set_listening(True)
    b.tick()
    b.tick()
    assert seen == ["A", "B"]


def test_3f2_enqueued_by_worker_thread_is_thread_safe(tmp_path):
    """sink 来自网络线程：并发入队与 GUI tick 不竞争（锁保护队列/在途）。"""
    import threading

    b = _bridge(tmp_path)
    b.set_listening(True)
    b.on_remote_task = lambda t: "ignored"
    n = 50
    def producer():
        for i in range(n):
            b.enqueue_remote_task(_rt(f"E{i}"))
    t = threading.Thread(target=producer)
    t.start()
    t.join(timeout=5)
    for _ in range(n + 5):
        b.tick()
    assert b.queue_size() == 0  # 全部投递、无丢失/死锁


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
