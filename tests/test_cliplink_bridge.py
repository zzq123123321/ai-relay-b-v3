"""cliplink_bridge 全 fake 测试：remote event 读取/去重、结果回传/pending、状态文件。

覆盖 19 个场景，全 tmp_path + FakeClipboardWriter，不碰真实 %LOCALAPPDATA%。
"""

import json
from pathlib import Path

import pytest

from cliplink_bridge import ClipLinkBridge, RemoteTask, default_remote_event_path, default_status_file_path
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


def test_04_listening_baseline_skips_current_event(tmp_path):
    p = tmp_path / "remote_clipboard.json"
    _write_remote_event(p, event_id="evt_base")
    b = _bridge(tmp_path)
    b.set_listening(True)
    assert b.poll() is None


def test_05_same_event_id_not_retriggered(tmp_path):
    p = tmp_path / "remote_clipboard.json"
    _write_remote_event(p, event_id="evt1", text="first")
    b = _bridge(tmp_path)
    b.set_listening(True)
    _write_remote_event(p, event_id="evt1", text="second")
    assert b.poll() is None


def test_06_new_event_id_triggers_once(tmp_path):
    p = tmp_path / "remote_clipboard.json"
    _write_remote_event(p, event_id="evt1")
    b = _bridge(tmp_path)
    b.set_listening(True)
    _write_remote_event(p, event_id="evt2", text="new")
    task = b.poll()
    assert task is not None and task.event_id == "evt2"
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


def test_08b_relistening_rebaselines(tmp_path):
    p = tmp_path / "remote_clipboard.json"
    _write_remote_event(p, event_id="evt1")
    b = _bridge(tmp_path)
    b.set_listening(True)
    b.set_listening(False)
    _write_remote_event(p, event_id="evt2")
    b.set_listening(True)
    assert b.poll() is None


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


# ── 路径 ─────────────────────────────────────────────────────────


def test_default_remote_event_path(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
    assert str(default_remote_event_path()) == r"C:\Users\test\AppData\Local\ClipLink\remote_clipboard.json"


def test_default_status_file_path(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
    assert str(default_status_file_path()) == r"C:\Users\test\AppData\Local\AIRelayLite\status.json"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))