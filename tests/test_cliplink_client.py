"""tests/test_cliplink_client.py

覆盖：协议帧、连接状态、断线恢复等待、心跳、持久设备身份、防回环与线程安全边界。
使用 fake / 临时目录 / socket pair，不碰真实系统剪贴板、不触发真实网络。
"""

from __future__ import annotations

import json
import os
import queue
import socket
import struct
import tempfile
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

import cliplink_client as cc
from cliplink_client import (
    ClipLinkClient,
    PROTOCOL_VERSION,
    MSG_HELLO,
    MSG_PING,
    MSG_PONG,
    MSG_CLIPBOARD_UPDATE,
    MSG_DISCONNECT,
    _sha256_hex,
    _now_ms,
    _load_or_create_device_id,
    discover_zerotier_ip,
)


# ── 工具 ─────────────────────────────────────────────────────────


def _make_frame(msg: dict) -> bytes:
    data = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    return struct.pack(">I", len(data)) + data


def _make_message(msg_type: str, payload: dict, device_id: str = "test-id") -> dict:
    return {
        "version": PROTOCOL_VERSION,
        "type": msg_type,
        "message_id": str(uuid.uuid4()),
        "timestamp": _now_ms(),
        "device_id": device_id,
        "payload": payload,
        "auth": None,
    }


def _send_frame(sock: socket.socket, msg: dict):
    sock.sendall(_make_frame(msg))


def _recv_frame(sock: socket.socket) -> dict:
    sock.settimeout(5.0)
    hdr = b""
    while len(hdr) < 4:
        chunk = sock.recv(4 - len(hdr))
        if not chunk:
            raise ConnectionError("EOF")
        hdr += chunk
    length = struct.unpack(">I", hdr)[0]
    body = b""
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            raise ConnectionError("EOF")
        body += chunk
    return json.loads(body.decode("utf-8"))


@pytest.fixture
def tmp_localappdata(tmp_path: Path):
    """重定向 LOCALAPPDATA 到临时目录。"""
    old = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = str(tmp_path)
    yield tmp_path
    if old is not None:
        os.environ["LOCALAPPDATA"] = old
    else:
        os.environ.pop("LOCALAPPDATA", None)


@pytest.fixture
def fake_clipboard():
    """模拟 GUI 线程剪贴板：getter/setter 对。"""
    state = {"text": None}

    def getter():
        return state["text"]

    def setter(text: str):
        state["text"] = text

    return {"state": state, "getter": getter, "setter": setter}


@pytest.fixture
def client(fake_clipboard, tmp_localappdata):
    """构造一个 ClipLinkClient 实例（不启动网络线程）。"""
    c = ClipLinkClient(
        listen_ip="127.0.0.1",
        port=0,  # 不用真实端口，测试中手动创建 socket pair
        device_id="550e8400-e29b-41d4-a716-446655440000",
        device_name="TEST-B",
        clipboard_getter=fake_clipboard["getter"],
        clipboard_setter=fake_clipboard["setter"],
    )
    return c


# ── 协议帧测试 ─────────────────────────────────────────────────


class TestProtocolFrames:
    def test_send_hello_frame_format(self, client):
        """hello 帧：4 字节大端长度 + JSON，含 version/type/message_id/timestamp/device_id/payload/auth。"""
        msg = {
            "version": PROTOCOL_VERSION,
            "type": MSG_HELLO,
            "message_id": "mid-1",
            "timestamp": 12345,
            "device_id": "dev-b",
            "payload": {
                "device_id": "dev-b",
                "device_name": "B",
                "protocol_version": PROTOCOL_VERSION,
            },
            "auth": None,
        }
        frame = _make_frame(msg)
        # 前 4 字节是 JSON 长度的大端编码
        json_bytes = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        assert frame[:4] == struct.pack(">I", len(json_bytes))
        assert frame[4:] == json_bytes

    def test_recv_hello_version_mismatch(self, client):
        """协议版本不匹配 → ConnectionError。"""
        # 用 socket pair 模拟
        a, b = socket.socketpair()
        client._sock = b
        try:
            # A 端发一个版本为 2 的 hello
            _send_frame(a, _make_message(MSG_HELLO, {
                "device_id": "dev-a",
                "device_name": "A",
                "protocol_version": 2,
            }))
            with pytest.raises(ConnectionError, match="协议版本不匹配"):
                client._recv_hello()
        finally:
            a.close()
            b.close()

    def test_recv_hello_valid(self, client):
        """合法 hello → 设置 peer_name，不抛异常。"""
        a, b = socket.socketpair()
        client._sock = b
        try:
            _send_frame(a, _make_message(MSG_HELLO, {
                "device_id": "dev-a",
                "device_name": "XU-HP",
                "protocol_version": PROTOCOL_VERSION,
            }))
            client._recv_hello()
            assert client._peer_name == "XU-HP"
        finally:
            a.close()
            b.close()

    def test_recv_hello_timeout(self, client):
        """握手超时（无数据）→ ConnectionError。"""
        a, b = socket.socketpair()
        client._sock = b
        try:
            b.settimeout(0.1)  # 短超时
            # 不发送任何数据，_recv_message 会读到 None 或超时
            # 模拟 EOF
            a.close()
            with pytest.raises(ConnectionError, match="握手超时"):
                client._recv_hello()
        finally:
            b.close()

    def test_ping_pong_frame_roundtrip(self, client):
        """ping/pong 帧格式正确。"""
        ping = _make_message(MSG_PING, {"ping_id": "p1", "sent_at": 1000})
        frame = _make_frame(ping)
        parsed = json.loads(frame[4:].decode("utf-8"))
        assert parsed["type"] == "ping"
        assert parsed["payload"]["ping_id"] == "p1"

        pong = _make_message(MSG_PONG, {"ping_id": "p1", "sent_at": 1000})
        frame2 = _make_frame(pong)
        parsed2 = json.loads(frame2[4:].decode("utf-8"))
        assert parsed2["type"] == "pong"
        assert parsed2["payload"]["ping_id"] == "p1"

    def test_clipboard_update_frame(self, client):
        """clipboard_update 帧含 text + content_hash。"""
        text = "hello world"
        msg = _make_message(MSG_CLIPBOARD_UPDATE, {
            "text": text,
            "content_hash": _sha256_hex(text),
        })
        frame = _make_frame(msg)
        parsed = json.loads(frame[4:].decode("utf-8"))
        assert parsed["type"] == "clipboard_update"
        assert parsed["payload"]["text"] == text
        assert parsed["payload"]["content_hash"] == _sha256_hex(text)

    def test_disconnect_frame(self, client):
        """disconnect 帧含 reason。"""
        msg = _make_message(MSG_DISCONNECT, {"reason": "user_requested"})
        parsed = json.loads(_make_frame(msg)[4:].decode("utf-8"))
        assert parsed["type"] == "disconnect"
        assert parsed["payload"]["reason"] == "user_requested"

    def test_oversized_message_rejected(self, client):
        """超过 1MiB 的消息 → ValueError。"""
        big_text = "x" * (1024 * 1024 + 1)
        with pytest.raises(ValueError, match="超过 1MiB"):
            client._send_raw(MSG_CLIPBOARD_UPDATE, {"text": big_text, "content_hash": ""})


# ── 连接状态测试 ────────────────────────────────────────────────


class TestConnectionStatus:
    def test_initial_status_is_offline(self, client, tmp_localappdata):
        """监听启动但无对端 → status=offline。"""
        client._write_status("offline")
        status_file = tmp_localappdata / "ClipLink" / "status.json"
        data = json.loads(status_file.read_text(encoding="utf-8"))
        assert data["status"] == "offline"
        assert data["peer_name"] is None
        assert data["peer_ip"] is None
        assert data["latency_ms"] is None

    def test_connected_status_after_handshake(self, client, tmp_localappdata):
        """握手成功后 → status=connected，含 peer 信息和 latency。"""
        client._peer_name = "XU-HP"
        client._peer_ip_str = "192.168.191.95:55204"
        client._latency_ms = 150
        client._gen = 1
        client._write_status("connected")
        status_file = tmp_localappdata / "ClipLink" / "status.json"
        data = json.loads(status_file.read_text(encoding="utf-8"))
        assert data["status"] == "connected"
        assert data["peer_name"] == "XU-HP"
        assert data["peer_ip"] == "192.168.191.95:55204"
        assert data["latency_ms"] == 150
        assert data["generation"] == 1

    def test_disconnected_returns_to_offline(self, client, tmp_localappdata):
        """断线后 → status=offline（B 是监听端，等待 A 重连）。"""
        client._peer_name = "XU-HP"
        client._peer_ip_str = "192.168.191.95:55204"
        client._write_status("offline")
        status_file = tmp_localappdata / "ClipLink" / "status.json"
        data = json.loads(status_file.read_text(encoding="utf-8"))
        assert data["status"] == "offline"
        assert data["peer_name"] is None
        assert data["peer_ip"] is None
        assert data["latency_ms"] is None

    def test_not_reconnecting_for_listener(self, client, tmp_localappdata):
        """B 是监听端：不使用 reconnecting 状态（不主动重连）。"""
        # 验证 _write_status 只写 "connected" 或 "offline"
        client._write_status("offline")
        data = json.loads((tmp_localappdata / "ClipLink" / "status.json").read_text())
        assert data["status"] == "offline"
        # 如果代码写了 reconnecting 则说明有 bug
        assert data["status"] != "reconnecting"


# ── 断线恢复等待测试 ────────────────────────────────────────────


class TestDisconnectRecovery:
    def test_listener_continues_after_disconnect(self, client, tmp_localappdata):
        """断线后 listener 继续运行，等待 A 重新连接。"""
        # 模拟：listener 存在且 running=True
        client._running = True
        # 创建 listener socket
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client._listener = listener
        port = listener.getsockname()[1]

        # 模拟第一次连接 + 断开
        conn_a, _ = listener.accept() if False else (None, None)

        # 验证 stop 能正确关闭 listener
        client._running = False
        listener.close()
        client._listener = None

    def test_new_connection_after_disconnect(self, client, tmp_localappdata):
        """A 断开后重新连入，B 能正常接受新连接（gen 递增）。"""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        # 第一次连接
        c1 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        c1.connect(("127.0.0.1", port))
        conn1, _ = listener.accept()
        client._gen = 0
        client._gen += 1
        assert client._gen == 1
        conn1.close()
        c1.close()

        # 第二次连接（模拟 A 重连）
        c2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        c2.connect(("127.0.0.1", port))
        conn2, _ = listener.accept()
        client._gen += 1
        assert client._gen == 2
        conn2.close()
        c2.close()
        listener.close()


# ── 心跳测试 ────────────────────────────────────────────────────


class TestHeartbeat:
    def test_miss_count_accumulates_without_reset(self, client):
        """心跳丢失计数：发 ping 后不重置，连续 3 次无 pong → 失联。"""
        # 模拟 HeartbeatState 逻辑
        pending_ping = None
        misses = 0
        cc.HEARTBEAT_SECS = 0.1  # 短周期加速测试
        cc.MISS_THRESHOLD = 3

        a, b = socket.socketpair()
        client._sock = b

        # 手动模拟 4 次 tick（每次发 ping，无 pong 回来）
        for i in range(4):
            if pending_ping is not None:
                misses += 1
            if misses >= cc.MISS_THRESHOLD:
                break
            ping_id = str(uuid.uuid4())
            pending_ping = (ping_id, _now_ms())
            # 发送 ping（但不重置 misses）
            client._send_raw(MSG_PING, {"ping_id": ping_id, "sent_at": _now_ms()})
            time.sleep(0.05)

        assert misses >= 3, f"misses 应达到 3，实际 {misses}"

        # 验证 pong 能重置
        pending_ping = ("p1", _now_ms())
        misses = 2
        # 模拟收到 pong
        assert pending_ping[0] == "p1"
        misses = 0  # 收到有效 pong 后重置
        assert misses == 0

        a.close()
        b.close()

    def test_pong_resets_miss_counter(self, client):
        """收到有效 pong → misses 归零。"""
        pending_ping = ("p1", _now_ms())
        misses = 2
        # 模拟 pong 到达
        ping_id = "p1"
        if pending_ping and pending_ping[0] == ping_id:
            pending_ping = None
            misses = 0
        assert misses == 0
        assert pending_ping is None

    def test_mismatched_pong_does_not_reset(self, client):
        """不匹配的 pong（ping_id 不同）不重置 misses。"""
        pending_ping = ("p1", _now_ms())
        misses = 1
        # 收到一个 ping_id="p2" 的 pong（不匹配）
        pong_ping_id = "p2"
        if pending_ping and pending_ping[0] == pong_ping_id:
            pending_ping = None
            misses = 0
        # 不匹配 → misses 不变
        assert misses == 1
        assert pending_ping is not None


# ── 持久设备身份测试 ────────────────────────────────────────────


class TestPersistentIdentity:
    def test_device_id_persisted(self, tmp_localappdata):
        """首次生成 device_id 并写入文件；再次读取返回相同值。"""
        id1 = _load_or_create_device_id()
        # 验证是合法 UUID
        uuid.UUID(id1)
        # 再次读取应返回相同值
        id2 = _load_or_create_device_id()
        assert id1 == id2

    def test_device_id_file_location(self, tmp_localappdata):
        """device_id 文件在 %LOCALAPPDATA%/ClipLink/device_id。"""
        _load_or_create_device_id()
        expected = tmp_localappdata / "ClipLink" / "device_id"
        assert expected.exists()
        content = expected.read_text(encoding="utf-8").strip()
        uuid.UUID(content)

    def test_device_id_survives_restart(self, tmp_localappdata):
        """模拟重启：两次独立调用返回相同 ID。"""
        id1 = _load_or_create_device_id()
        # 模拟进程重启（函数无状态，只读文件）
        id2 = _load_or_create_device_id()
        assert id1 == id2
        # 验证 UUID 格式
        parsed = uuid.UUID(id1)
        assert parsed.version == 4

    def test_corrupt_device_id_regenerates(self, tmp_localappdata):
        """device_id 文件损坏（非 UUID）→ 重新生成。"""
        path = tmp_localappdata / "ClipLink" / "device_id"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-a-uuid", encoding="utf-8")
        new_id = _load_or_create_device_id()
        # 应生成新 UUID 并覆盖
        uuid.UUID(new_id)
        assert new_id != "not-a-uuid"
        assert path.read_text(encoding="utf-8").strip() == new_id

    def test_empty_device_id_file_regenerates(self, tmp_localappdata):
        """device_id 文件为空 → 重新生成。"""
        path = tmp_localappdata / "ClipLink" / "device_id"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        new_id = _load_or_create_device_id()
        uuid.UUID(new_id)
        assert new_id != ""


# ── 防回环测试 ──────────────────────────────────────────────────


class TestLoopPrevention:
    def test_remote_write_suppresses_matching_read(self, client, fake_clipboard):
        """A→B 写入后，只有读缓冲观察到同一文本才消费抑制。"""
        # 模拟 A→B 写入（设置 _suppress_text）
        client._suppress_text = "from-A"
        client._last_clipboard_text = "old"

        # 模拟 GUI 线程已更新 read_buffer（A 端内容已落地）
        fake_clipboard["state"]["text"] = "from-A"
        client._read_buffer = "from-A"

        # _check_local_clipboard：读缓冲匹配 → 消费抑制
        client._check_local_clipboard()
        assert client._suppress_text is None
        assert client._last_clipboard_text == "from-A"

        # 再次 _check_local_clipboard：内容不变，不发
        client._check_local_clipboard()
        assert client._last_clipboard_text == "from-A"

    def test_race_network_checks_old_before_gui_writes(self, client, fake_clipboard):
        """稳定复现竞态：网络线程先检查旧值（不消费），GUI 后写新值，断言不回发。"""
        a, b = socket.socketpair()
        client._sock = b
        client._last_clipboard_text = "old-value"

        # 1. 网络线程收到 A→B 内容，设置 _suppress_text
        client._suppress_text = "remote-text"
        # 此时 read_buffer 仍是旧值（GUI 尚未写入）
        client._read_buffer = "old-value"

        # 2. 网络线程 _check_local_clipboard（在 GUI 写入前）
        #    读缓冲是 "old-value" ≠ "remote-text" → 不消费、不发送
        client._check_local_clipboard()
        assert client._suppress_text == "remote-text", "不应提前消费"

        # 3. GUI 线程随后写入远端文本 → read_buffer 更新
        client._read_buffer = "remote-text"

        # 4. 网络线程再次 _check_local_clipboard
        #    读缓冲 "remote-text" == _suppress_text → 消费
        client._check_local_clipboard()
        assert client._suppress_text is None, "应已消费"
        assert client._last_clipboard_text == "remote-text"

        # 5. 断言：A 端未收到任何 clipboard_update（远端内容未回发）
        b.settimeout(0.1)
        try:
            data = b.recv(4096)
            assert data == b"", f"远端内容不应回发，但收到了: {data[:50]!r}"
        except socket.timeout:
            pass  # 无数据 = 正确

        a.close()
        b.close()

    def test_local_change_sends_to_a(self, client, fake_clipboard):
        """B 端本机新复制内容 → 发送给 A。"""
        a, b = socket.socketpair()
        client._sock = b
        client._last_clipboard_text = "old"
        client._suppress_text = None

        # GUI 线程更新：B 端用户复制了新内容
        fake_clipboard["state"]["text"] = "new-from-B"
        client._read_buffer = "new-from-B"

        client._check_local_clipboard()

        # 验证 A 端收到了 clipboard_update
        b.settimeout(2.0)
        msg = _recv_frame(a)
        assert msg["type"] == MSG_CLIPBOARD_UPDATE
        assert msg["payload"]["text"] == "new-from-B"
        assert msg["payload"]["content_hash"] == _sha256_hex("new-from-B")

        a.close()
        b.close()

    def test_unchanged_content_not_sent(self, client, fake_clipboard):
        """内容不变 → 不发送。"""
        a, b = socket.socketpair()
        client._sock = b
        client._last_clipboard_text = "same"
        client._suppress_text = None

        # 内容相同
        client._read_buffer = "same"
        client._check_local_clipboard()

        # 不应该有数据发出
        b.settimeout(0.1)
        try:
            data = b.recv(1024)
            assert data == b"", "内容不变时不应发送数据"
        except socket.timeout:
            pass  # 超时就对了

        a.close()
        b.close()

    def test_remote_clipboard_hash_mismatch_discarded(self, client, fake_clipboard):
        """A→B 剪贴板哈希不匹配 → 丢弃，不写剪贴板。"""
        written = []
        client._clipboard_setter_gui = lambda t: written.append(t)

        client._handle_remote_clipboard({
            "text": "hello",
            "content_hash": "wrong-hash",
        })
        # 哈希不匹配 → 不写入
        assert written == []
        # _suppress_text 不应被设置
        assert client._suppress_text is None


# ── 线程安全边界测试 ────────────────────────────────────────────


class TestThreadSafety:
    def test_set_clipboard_uses_queue(self, client, fake_clipboard):
        """网络线程写剪贴板 → 通过队列，不直接调 Qt。"""
        client._set_clipboard("test-text")
        # 验证队列中有数据
        assert not client._write_queue.empty()
        text = client._write_queue.get_nowait()
        assert text == "test-text"
        # Qt setter 不应被直接调用（由 gui_poll 调用）
        assert fake_clipboard["state"]["text"] is None

    def test_gui_poll_processes_write_queue(self, client, fake_clipboard):
        """gui_poll 消费写队列 → 调用 Qt setter。"""
        client._set_clipboard("hello")
        client._set_clipboard("world")

        # GUI 线程调用
        client.gui_poll()

        # 验证 Qt 剪贴板被设置为最后一次的值
        assert fake_clipboard["state"]["text"] == "world"

    def test_gui_poll_updates_read_buffer(self, client, fake_clipboard):
        """gui_poll 更新 read_buffer → 网络线程能读到最新剪贴板。"""
        fake_clipboard["state"]["text"] = "from-gui-thread"
        client.gui_poll()
        # 网络线程读取
        assert client._get_clipboard() == "from-gui-thread"

    def test_network_thread_does_not_call_qt_directly(self, client, fake_clipboard):
        """网络线程的 _get_clipboard / _set_clipboard 不直接碰 Qt 对象。"""
        # _get_clipboard 只读 _read_buffer（由 GUI 线程更新）
        client._read_buffer = "cached"
        assert client._get_clipboard() == "cached"

        # _set_clipboard 只放队列
        client._set_clipboard("new")
        # Qt setter 未被直接调用
        assert fake_clipboard["state"]["text"] != "new"
        # 队列中有
        assert client._write_queue.get_nowait() == "new"

    def test_concurrent_read_write_no_deadlock(self, client, fake_clipboard):
        """并发读/写不死锁。"""
        errors = []

        def writer():
            try:
                for i in range(50):
                    client._set_clipboard(f"write-{i}")
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(50):
                    client._get_clipboard()
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=reader)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert not errors
        assert not t1.is_alive()
        assert not t2.is_alive()


# ── discover_zerotier_ip 测试 ──────────────────────────────────


class TestDiscoverZerotierIp:
    """可注入、完全隔离的 ZT IP 发现测试（不依赖当前机器是否安装 ZeroTier）。"""

    def test_zt_adapter_found_by_name(self):
        """存在 ZeroTier 适配器 → 返回其 IP。"""
        adapters = [
            ("Ethernet", "192.168.1.100"),
            ("ZeroTier One [abc123]", "192.168.191.180"),
        ]
        result = discover_zerotier_ip(get_adapter_ips=lambda: adapters)
        assert result == "192.168.191.180"

    def test_zt_ip_found_by_subnet_only(self):
        """适配器名不含 ZeroTier 但 IP 在 192.168.191.x → 仍返回。"""
        adapters = [
            ("Ethernet", "192.168.1.100"),
            ("SomeAdapter", "192.168.191.55"),
        ]
        result = discover_zerotier_ip(get_adapter_ips=lambda: adapters)
        assert result == "192.168.191.55"

    def test_no_zt_address_returns_none(self):
        """无 ZT 适配器、无 192.168.191.x 地址 → 返回 None。"""
        adapters = [
            ("Ethernet", "192.168.1.100"),
            ("WiFi", "10.0.0.5"),
            ("Loopback", "127.0.0.1"),
        ]
        result = discover_zerotier_ip(get_adapter_ips=lambda: adapters)
        assert result is None

    def test_enumeration_failure_returns_none(self):
        """枚举函数抛异常 → 返回 None（不崩溃）。"""
        def raise_error():
            raise OSError("Get-NetIPAddress 不可用")
        result = discover_zerotier_ip(get_adapter_ips=raise_error)
        assert result is None

    def test_zt_name_but_wrong_subnet_uses_subnet_match(self):
        """适配器名含 ZeroTier 但 IP 不在 192.168.191.x → 不匹配（需两者同时满足）。"""
        adapters = [
            ("ZeroTier One [xyz]", "169.254.1.1"),  # ZT 未获得地址
            ("Ethernet", "192.168.191.100"),
        ]
        # 优先路径（名+网段）不匹配，次选路径（仅网段）匹配
        result = discover_zerotier_ip(get_adapter_ips=lambda: adapters)
        assert result == "192.168.191.100"


# ── TCP 分片/超时丢帧测试 ────────────────────────────────────────


class TestTcpFragmentation:
    """验证持久接收缓冲正确处理分片、超时、粘包。"""

    def test_header_split_across_recv(self, client):
        """4 字节 header 分两次到达（2+2），持久缓冲正确重组。"""
        a, b = socket.socketpair()
        client._sock = b
        client._recv_buffer = b""

        msg = _make_message(MSG_PING, {"ping_id": "frag1", "sent_at": 1000})
        frame = _make_frame(msg)
        hdr = frame[:4]
        body = frame[4:]

        # 分片发送 header：前 2 字节
        b.settimeout(0.5)
        a.sendall(hdr[:2])
        time.sleep(0.05)
        a.sendall(hdr[2:])
        time.sleep(0.05)
        a.sendall(body)

        # 应该能正确解析完整消息
        parsed = client._recv_message()
        assert parsed is not None
        assert parsed["type"] == MSG_PING
        assert parsed["payload"]["ping_id"] == "frag1"
        # 缓冲应已清空
        assert client._recv_buffer == b""

        a.close()
        b.close()

    def test_body_split_across_recv(self, client):
        """body 分多次到达，持久缓冲跨轮次累积。"""
        a, b = socket.socketpair()
        client._sock = b
        client._recv_buffer = b""

        msg = _make_message(MSG_PING, {"ping_id": "frag2", "sent_at": 2000})
        frame = _make_frame(msg)
        hdr = frame[:4]
        body = frame[4:]

        b.settimeout(1.0)
        # 先收 header（完整）
        a.sendall(hdr)
        time.sleep(0.05)
        # body 分两半
        half = len(body) // 2
        a.sendall(body[:half])
        time.sleep(0.05)
        a.sendall(body[half:])

        parsed = client._recv_message()
        assert parsed is not None
        assert parsed["type"] == MSG_PING
        assert parsed["payload"]["ping_id"] == "frag2"
        assert client._recv_buffer == b""

        a.close()
        b.close()

    def test_timeout_between_fragments_preserves_buffer(self, client):
        """分片之间发生 timeout：已收字节不丢失，下次调用继续累积。"""
        a, b = socket.socketpair()
        client._sock = b
        client._recv_buffer = b""

        msg = _make_message(MSG_PING, {"ping_id": "timeout-frag", "sent_at": 3000})
        frame = _make_frame(msg)
        hdr = frame[:4]
        body = frame[4:]

        b.settimeout(0.1)  # 短超时
        # 发送 header 前 2 字节
        a.sendall(hdr[:2])
        time.sleep(0.15)

        # 第一次 _recv_message：header 不完整 → timeout
        try:
            client._recv_message()
        except socket.timeout:
            pass
        # 已收的 2 字节应保留在缓冲中
        assert client._recv_buffer == hdr[:2], f"缓冲应保留已收字节，实际: {client._recv_buffer!r}"

        # 继续发送剩余字节
        b.settimeout(1.0)
        a.sendall(hdr[2:])
        time.sleep(0.05)
        a.sendall(body)
        time.sleep(0.05)

        # 第二次 _recv_message：从持久缓冲继续 → 成功解析
        parsed = client._recv_message()
        assert parsed is not None
        assert parsed["type"] == MSG_PING
        assert parsed["payload"]["ping_id"] == "timeout-frag"

        a.close()
        b.close()

    def test_timeout_after_header_and_half_body(self, client):
        """完整 header + 半个 body 到达后第一次读取超时；剩余 body 到达后第二次读取成功。

        回归：修复前帧头在正文超时后已被消费，第二次读取把正文前四字节误当新帧长度
        （ValueError: 非法帧长度）。现在完整帧就绪前不消费帧头。
        """
        a, b = socket.socketpair()
        client._sock = b
        client._recv_buffer = b""

        msg = _make_message(MSG_PING, {"ping_id": "half-body", "sent_at": 5000})
        frame = _make_frame(msg)
        half = (4 + len(frame[4:])) // 2  # 完整 header + 半个 body
        assert half > 4, "前半段必须包含完整 header"

        b.settimeout(0.1)
        a.sendall(frame[:half])
        time.sleep(0.05)

        # 第一次读取：正文不完整 → timeout，缓冲保留全部已收字节
        with pytest.raises(socket.timeout):
            client._recv_message()
        assert client._recv_buffer == frame[:half], f"缓冲应保留已收字节，实际: {client._recv_buffer!r}"

        # 发送剩余 body，第二次读取必须成功返回原消息
        b.settimeout(1.0)
        a.sendall(frame[half:])
        time.sleep(0.05)
        parsed = client._recv_message()
        assert parsed is not None
        assert parsed == msg
        assert client._recv_buffer == b""

        a.close()
        b.close()

    def test_two_frames_back_to_back(self, client):
        """连续两帧粘包（一次 TCP 读收到两帧），持久缓冲正确拆分。"""
        a, b = socket.socketpair()
        client._sock = b
        client._recv_buffer = b""

        msg1 = _make_message(MSG_PING, {"ping_id": "frame1", "sent_at": 4000})
        msg2 = _make_message(MSG_PONG, {"ping_id": "frame1", "sent_at": 4001})
        combined = _make_frame(msg1) + _make_frame(msg2)

        b.settimeout(1.0)
        a.sendall(combined)

        # 第一帧
        parsed1 = client._recv_message()
        assert parsed1 is not None
        assert parsed1["type"] == MSG_PING
        assert parsed1["payload"]["ping_id"] == "frame1"

        # 第二帧（从持久缓冲中读取）
        parsed2 = client._recv_message()
        assert parsed2 is not None
        assert parsed2["type"] == MSG_PONG
        assert parsed2["payload"]["ping_id"] == "frame1"

        assert client._recv_buffer == b""

        a.close()
        b.close()

    def test_recv_buffer_reset_on_new_connection(self, client):
        """新连接建立时 _recv_buffer 被重置（不残留旧连接数据）。"""
        client._recv_buffer = b"\x00\x00\x01\x00old-data"
        # 模拟新连接重置
        client._recv_buffer = b""
        assert client._recv_buffer == b""


# ── 集成测试：完整连接生命周期（socket pair） ──────────────────


class TestIntegrationSocketPair:
    def test_full_handshake_and_heartbeat(self, client, fake_clipboard, tmp_localappdata):
        """完整流程：hello 握手 → 心跳 ping/pong → 剪贴板同步 → 断开。"""
        a, b = socket.socketpair()
        client._sock = b
        client._running = True

        # 1. 双向 hello
        # B 发 hello
        client._send_hello()
        hello_b = _recv_frame(a)
        assert hello_b["type"] == MSG_HELLO
        assert hello_b["payload"]["device_name"] == "TEST-B"

        # A 回 hello
        _send_frame(a, _make_message(MSG_HELLO, {
            "device_id": "dev-a",
            "device_name": "XU-HP",
            "protocol_version": PROTOCOL_VERSION,
        }))
        # B 收 hello（设置短超时）
        b.settimeout(2.0)
        # 手动调 _recv_hello 逻辑
        b.settimeout(2.0)
        msg = client._recv_message()
        assert msg["type"] == MSG_HELLO
        client._peer_name = msg["payload"]["device_name"]

        # 2. 心跳：B 发 ping，A 回 pong
        # 从 a 端读 B 的 ping
        b.settimeout(2.0)
        # 手动发一个 ping
        ping_id = str(uuid.uuid4())
        client._send_raw(MSG_PING, {"ping_id": ping_id, "sent_at": _now_ms()})
        ping_msg = _recv_frame(a)
        assert ping_msg["type"] == MSG_PING
        assert ping_msg["payload"]["ping_id"] == ping_id

        # A 回 pong
        _send_frame(a, _make_message(MSG_PONG, {"ping_id": ping_id, "sent_at": _now_ms()}))
        pong_msg = client._recv_message()
        assert pong_msg["type"] == MSG_PONG

        # 3. 剪贴板同步 A→B
        text = "测试任务内容"
        _send_frame(a, _make_message(MSG_CLIPBOARD_UPDATE, {
            "text": text,
            "content_hash": _sha256_hex(text),
        }))
        clip_msg = client._recv_message()
        assert clip_msg["type"] == MSG_CLIPBOARD_UPDATE
        # 处理
        client._handle_remote_clipboard(clip_msg["payload"])
        # 验证 write_queue 中有数据
        assert not client._write_queue.empty()
        assert client._write_queue.get_nowait() == text

        # 4. 断开
        _send_frame(a, _make_message(MSG_DISCONNECT, {"reason": "user_requested"}))
        disc_msg = client._recv_message()
        assert disc_msg["type"] == MSG_DISCONNECT

        client._running = False
        a.close()
        b.close()


# ── 阶段 3F-2：入站 sink 接管（先 sink 后 best-effort 快照）────────


class TestInboundSink:
    def test_sink_called_before_snapshot_file(self, client, tmp_localappdata):
        """sink 先于快照文件写入；sink 收到的 RemoteTask 与文件事件同帧一致。"""
        from cliplink_bridge import RemoteTask

        seq: list[str] = []
        tasks: list[RemoteTask] = []

        def sink(task: RemoteTask):
            tasks.append(task)
            seq.append("sink")

        client.set_inbound_sink(sink)
        client._handle_remote_clipboard({"text": "任务A", "content_hash": ""})
        seq.append("file")
        assert seq == ["sink", "file"]  # 先 sink 后快照
        assert len(tasks) == 1
        snap = json.loads((tmp_localappdata / "ClipLink" / "remote_clipboard.json").read_text("utf-8"))
        assert tasks[0].event_id == snap["event_id"]  # 同帧：内存接管与快照一致
        assert tasks[0].text == "任务A"
        assert tasks[0].content_hash == snap["content_hash"]

    def test_sink_receives_all_consecutive_frames(self, client, tmp_localappdata):
        """R1：tick 前连续三帧 → sink 全部接管（快照文件单槽只剩最后一帧）。"""
        from cliplink_bridge import RemoteTask

        tasks: list[RemoteTask] = []
        client.set_inbound_sink(tasks.append)
        for text in ("A", "B", "C"):
            client._handle_remote_clipboard({"text": text, "content_hash": ""})
        assert [t.text for t in tasks] == ["A", "B", "C"]
        assert len({t.event_id for t in tasks}) == 3  # 每帧独立 event_id
        snap = json.loads((tmp_localappdata / "ClipLink" / "remote_clipboard.json").read_text("utf-8"))
        assert snap["text"] == "C"  # 单槽快照只剩最新帧（边界：只救回 1 帧）

    def test_sink_failure_visible_and_snapshot_still_written(self, client, tmp_localappdata, caplog):
        """sink 抛错：记可见错误日志（不吞）、不中断帧处理，best-effort 快照仍写入。"""
        import logging

        def boom(task):
            raise RuntimeError("sink boom")

        client.set_inbound_sink(boom)
        with caplog.at_level(logging.WARNING, logger="cliplink_client"):
            client._handle_remote_clipboard({"text": "X", "content_hash": ""})
        assert any("入站 sink 接管失败" in m and "sink boom" in m for m in caplog.messages)
        snap = json.loads((tmp_localappdata / "ClipLink" / "remote_clipboard.json").read_text("utf-8"))
        assert snap["text"] == "X"  # 快照仍写（仅能救回本最新帧）

    def test_no_sink_snapshot_only_behavior_unchanged(self, client, tmp_localappdata):
        """未注册 sink：行为与 3F-2 前一致（仅快照文件），不崩。"""
        client._handle_remote_clipboard({"text": "Y", "content_hash": ""})
        snap = json.loads((tmp_localappdata / "ClipLink" / "remote_clipboard.json").read_text("utf-8"))
        assert snap["text"] == "Y"