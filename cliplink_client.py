"""ClipLink TCP 客户端（B 端）：B 端监听 45888，A 端主动连入，替代独立 ClipLink Tauri 应用。

协议：4 字节大端长度前缀 + UTF-8 JSON 帧，端口 45888。
功能：
  - A→B：收到 clipboard_update → 写系统剪贴板 + remote_clipboard.json
  - B→A：监控本机剪贴板变化 → 发 clipboard_update 给 A
  - 心跳：10s ping/pong，连续 3 次丢失判失联
  - 断线后 B 端继续监听，等待 A 端重新连接（B 是被动监听端，不主动重连）
  - 状态文件：写 %LOCALAPPDATA%/ClipLink/status.json 供 GUI 读取
  - 设备 ID：持久化到 %LOCALAPPDATA%/ClipLink/device_id，重启后不变
  - 线程安全：剪贴板读写通过队列派发到 GUI 主线程
  - 防回环：基于具体文本的抑制（非布尔标志），只有读缓冲观察到同一远端内容才消费
  - TCP framing：持久接收缓冲，跨轮次保留已接收字节，不丢半帧
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import socket
import struct
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from cliplink_bridge import RemoteTask

log = logging.getLogger("cliplink_client")

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 1024 * 1024
HEARTBEAT_SECS = 10
MISS_THRESHOLD = 3
CONNECT_TIMEOUT = 5
CLIP_POLL_INTERVAL = 0.3  # 剪贴板轮询间隔（秒），与 Rust 端 300ms 一致

MSG_HELLO = "hello"
MSG_PING = "ping"
MSG_PONG = "pong"
MSG_DISCONNECT = "disconnect"
MSG_ERROR = "error"
MSG_CLIPBOARD_UPDATE = "clipboard_update"


def _local_appdata() -> Path:
    base = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    return Path(base)


def _status_path() -> Path:
    return _local_appdata() / "ClipLink" / "status.json"


def _remote_event_path() -> Path:
    return _local_appdata() / "ClipLink" / "remote_clipboard.json"


def _device_id_path() -> Path:
    return _local_appdata() / "ClipLink" / "device_id"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def discover_zerotier_ip(
    get_adapter_ips: Callable[[], list[tuple[str, str]]] | None = None,
) -> str | None:
    """发现本机 ZeroTier 适配器 IPv4 地址。

    优先匹配适配器名含 "ZeroTier"（不区分大小写）；
    其次匹配 192.168.191.x 网段。
    找不到返回 None（生产路径不应回退 127.0.0.1）。

    Args:
        get_adapter_ips: 可注入的枚举函数，返回 [(适配器名, IPv4地址), ...]。
                         为 None 时使用默认 Windows 枚举实现。
    """
    if get_adapter_ips is None:
        get_adapter_ips = _default_get_adapter_ips

    try:
        adapters = get_adapter_ips()
    except Exception as e:
        log.warning("ZeroTier 适配器枚举失败: %s", e)
        return None

    # 优先：适配器名含 ZeroTier
    for name, ip in adapters:
        if "zerotier" in name.lower() and ip.startswith("192.168.191."):
            log.info("发现 ZeroTier 适配器: %s → %s", name, ip)
            return ip
    # 次选：192.168.191.x 网段（可能适配器名不含 ZeroTier）
    for name, ip in adapters:
        if ip.startswith("192.168.191."):
            log.info("发现 ZT 网段地址: %s → %s", name, ip)
            return ip

    return None


def _default_get_adapter_ips() -> list[tuple[str, str]]:
    """Windows 默认实现：通过 PowerShell Get-NetIPAddress 枚举所有 IPv4 适配器地址。"""
    result = subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            "Get-NetIPAddress -AddressFamily IPv4 | "
            "Select-Object IPAddress, InterfaceAlias | "
            "ConvertTo-Json -Compress",
        ],
        capture_output=True, text=True, timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise OSError(f"Get-NetIPAddress 失败: {result.stderr.strip()}")

    data = json.loads(result.stdout.strip())
    # ConvertTo-Json 对单条返回 dict，多条返回 list
    if isinstance(data, dict):
        data = [data]
    return [(entry.get("InterfaceAlias", ""), entry.get("IPAddress", "")) for entry in data]


def _load_or_create_device_id() -> str:
    """读取持久化 device_id；不存在则生成 UUID v4 并写入文件。"""
    path = _device_id_path()
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if raw:
            uuid.UUID(raw)  # 验证是合法 UUID
            return raw
    except (OSError, ValueError):
        pass
    new_id = str(uuid.uuid4())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_id, encoding="utf-8")
    except OSError as e:
        log.warning("写入 device_id 文件失败: %s", e)
    return new_id


class ClipLinkClient:
    """B 端 ClipLink TCP 监听客户端（守护线程运行）。

    B 端是被动监听端：bind ZT_IP:45888 等 A 端连入。
    断线后继续监听，等待 A 端重新连接（不主动外连）。
    """

    def __init__(
        self,
        listen_ip: str,
        port: int = 45888,
        device_id: str | None = None,
        device_name: str | None = None,
        clipboard_getter=None,
        clipboard_setter=None,
    ):
        """
        Args:
            listen_ip: 监听地址（必须传入，由调用方通过 discover_zerotier_ip() 获取）
            port: 监听端口（默认 45888）
            device_id: 设备 ID（None 时自动持久化生成）
            device_name: 设备名（默认 "AIRelayLite-B"）
            clipboard_getter: 可选，GUI 线程的剪贴板读取函数（用于线程安全桥接）
            clipboard_setter: 可选，GUI 线程的剪贴板写入函数（用于线程安全桥接）
        """
        self._listen_ip = listen_ip
        self._port = port
        self._device_id = device_id or _load_or_create_device_id()
        self._device_name = device_name or "AIRelayLite-B"

        # 线程安全剪贴板桥：网络线程写入队列，GUI 线程消费
        self._write_queue: queue.Queue[str] = queue.Queue()
        self._read_lock = threading.Lock()
        self._read_buffer: str | None = None  # GUI 线程定期更新
        self._clipboard_getter_gui = clipboard_getter  # GUI 线程调用
        self._clipboard_setter_gui = clipboard_setter  # GUI 线程调用

        self._sock: socket.socket | None = None
        self._listener: socket.socket | None = None
        self._peer_ip_str: str | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._gen = 0
        self._status = "offline"
        self._peer_name: str | None = None
        self._latency_ms: int | None = None
        self._last_clipboard_text: str | None = None
        self._suppress_text: str | None = None  # 待抑制的远端文本（内容关联，非布尔）
        self._recv_buffer: bytes = b""  # 持久 TCP 接收缓冲（跨轮次保留已收字节）
        self._inbound_sink: Callable[[RemoteTask], None] | None = None  # 入站接管 sink（start 前注册）

    # ── 生命周期 ─────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="cliplink-client")
        self._thread.start()
        log.info("ClipLinkClient started: listening on %s:%d", self._listen_ip, self._port)

    def set_inbound_sink(self, sink: Callable[[RemoteTask], None]) -> None:
        """注册入站接管 sink（收帧构造 RemoteTask 后由网络线程同步调用）。

        必须在 start() 之前注册；sink 须线程安全且不碰 Qt（Bridge 队列回调满足）。
        sink 抛错只记可见错误日志，不中断帧处理，best-effort 快照文件仍会写入。
        """
        self._inbound_sink = sink

    def stop(self):
        self._running = False
        self._write_status("offline")
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._listener:
            try:
                self._listener.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        log.info("ClipLinkClient stopped")

    # ── GUI 主线程调用（线程安全桥接） ──────────────────────────

    def gui_poll(self) -> None:
        """GUI 主线程定时调用（QTimer 300ms）：

        1. 读取队列中待写入的剪贴板内容 → 调用 Qt clipboard.setText()
        2. 读取当前 Qt 剪贴板 → 更新 _read_buffer 供网络线程消费
        """
        # 1. 消费写队列
        while True:
            try:
                text = self._write_queue.get_nowait()
            except queue.Empty:
                break
            if self._clipboard_setter_gui:
                try:
                    self._clipboard_setter_gui(text)
                except Exception as e:
                    log.warning("GUI 线程写剪贴板失败: %s", e)

        # 2. 更新读缓冲
        if self._clipboard_getter_gui:
            try:
                with self._read_lock:
                    self._read_buffer = self._clipboard_getter_gui()
            except Exception:
                pass

    # ── 网络线程使用的剪贴板访问（线程安全） ──────────────────

    def _get_clipboard(self) -> str | None:
        """网络线程读取剪贴板：从 GUI 线程定期更新的缓冲读取。"""
        with self._read_lock:
            return self._read_buffer

    def _set_clipboard(self, text: str) -> None:
        """网络线程请求写剪贴板：放入队列，由 GUI 线程实际执行。"""
        self._write_queue.put(text)

    # ── 主循环（监听 + 接受连接） ──────────────────────────────────

    def _run_loop(self):
        # 初始状态：监听中但无对端 → offline
        self._write_status("offline")
        while self._running:
            try:
                self._listen_and_accept()
            except OSError as e:
                log.warning("监听异常: %s", e)
                self._write_status("offline")
                if not self._running:
                    break
                time.sleep(1)

    def _listen_and_accept(self):
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((self._listen_ip, self._port))
        self._listener.listen(1)
        self._listener.settimeout(1.0)
        self._write_status("offline")
        log.info("监听中 %s:%d，等待 A 端连接", self._listen_ip, self._port)

        while self._running:
            try:
                self._sock, addr = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            self._sock.settimeout(None)
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._recv_buffer = b""  # 新连接重置接收缓冲
            self._gen += 1
            self._peer_ip_str = f"{addr[0]}:{addr[1]}"
            log.info("收到 A 端连接: %s (gen=%d)", self._peer_ip_str, self._gen)

            try:
                self._handshake_and_serve()
            except OSError as e:
                log.info("连接断开: %s", e)
            except Exception as e:
                log.error("连接异常: %s", e, exc_info=True)
            finally:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
                # B 是监听端：断线后继续等待 A 重新连接 → offline
                self._write_status("offline")

    def _handshake_and_serve(self):
        # 双向 hello
        self._send_hello()
        self._recv_hello()
        # 握手成功后才写 connected
        self._write_status("connected")
        self._last_clipboard_text = self._get_clipboard()
        log.info("握手完成 gen=%d peer=%s", self._gen, self._peer_name)
        self._serve_loop()

    def _serve_loop(self):
        heartbeat_due = 0.0
        clip_check_due = 0.0
        pending_ping: tuple[str, int] | None = None
        misses = 0

        while self._running:
            now = time.time()

            # 心跳
            if now >= heartbeat_due:
                heartbeat_due = now + HEARTBEAT_SECS
                if pending_ping is not None:
                    misses += 1
                    if misses >= MISS_THRESHOLD:
                        log.warning("心跳丢失 %d 次，判定失联", misses)
                        self._write_status("offline")
                        return
                ping_id = str(uuid.uuid4())
                sent_at = _now_ms()
                pending_ping = (ping_id, sent_at)
                # 注意：不在此处重置 misses，只有收到有效 pong 才重置
                self._send_raw(MSG_PING, {"ping_id": ping_id, "sent_at": sent_at})

            # 剪贴板监控（B→A）
            if now >= clip_check_due:
                clip_check_due = now + CLIP_POLL_INTERVAL
                self._check_local_clipboard()

            # 非阻塞读
            self._sock.settimeout(0.1)
            try:
                msg = self._recv_message()
            except socket.timeout:
                continue
            except OSError:
                log.info("连接断开")
                self._write_status("offline")
                return

            if msg is None:
                continue

            mtype = msg.get("type")
            payload = msg.get("payload", {})

            if mtype == MSG_PONG:
                ping_id = payload.get("ping_id", "")
                if pending_ping and pending_ping[0] == ping_id:
                    rtt = _now_ms() - pending_ping[1]
                    self._latency_ms = rtt
                    pending_ping = None
                    misses = 0
                    self._write_status("connected")
                    log.debug("pong RTT=%dms", rtt)

            elif mtype == MSG_PING:
                ping_id = payload.get("ping_id", "")
                self._send_raw(MSG_PONG, {"ping_id": ping_id, "sent_at": _now_ms()})

            elif mtype == MSG_CLIPBOARD_UPDATE:
                self._handle_remote_clipboard(payload)

            elif mtype == MSG_DISCONNECT:
                log.info("对方主动断开: %s", payload.get("reason"))
                self._write_status("offline")
                return

            elif mtype == MSG_ERROR:
                log.warning("对方报错: %s", payload.get("reason"))

    # ── 协议收发 ─────────────────────────────────────────────────

    def _send_raw(self, msg_type: str, payload: dict):
        msg = {
            "version": PROTOCOL_VERSION,
            "type": msg_type,
            "message_id": str(uuid.uuid4()),
            "timestamp": _now_ms(),
            "device_id": self._device_id,
            "payload": payload,
            "auth": None,
        }
        data = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        if len(data) > MAX_MESSAGE_BYTES:
            raise ValueError("消息超过 1MiB 上限")
        frame = struct.pack(">I", len(data)) + data
        with self._write_lock:
            self._sock.sendall(frame)

    def _recv_message(self) -> dict | None:
        # 完整帧（4 字节头 + 全部正文）就绪前不消费帧头，超时后帧状态不丢失
        if self._recv_exact(4) is None:
            return None
        length = struct.unpack(">I", self._recv_buffer[:4])[0]
        if length == 0 or length > MAX_MESSAGE_BYTES:
            raise ValueError(f"非法帧长度: {length}")
        if self._recv_exact(4 + length) is None:
            return None
        body = self._recv_buffer[4:4 + length]
        self._recv_buffer = self._recv_buffer[4 + length:]
        msg = json.loads(body.decode("utf-8"))
        if not isinstance(msg, dict):
            return None
        return msg

    def _recv_exact(self, n: int) -> bytes | None:
        """等待持久缓冲至少 n 字节。只填充不消费；超时保留已收字节，EOF 返回 None。"""
        while len(self._recv_buffer) < n:
            chunk = self._sock.recv(n - len(self._recv_buffer))
            if not chunk:
                return None  # 连接关闭
            self._recv_buffer += chunk
        return self._recv_buffer

    def _send_hello(self):
        self._send_raw(MSG_HELLO, {
            "device_id": self._device_id,
            "device_name": self._device_name,
            "protocol_version": PROTOCOL_VERSION,
        })

    def _recv_hello(self):
        self._sock.settimeout(CONNECT_TIMEOUT)
        while True:
            msg = self._recv_message()
            if msg is None:
                raise ConnectionError("握手超时：未收到 hello")
            if msg.get("type") == MSG_HELLO:
                p = msg.get("payload", {})
                if p.get("protocol_version") != PROTOCOL_VERSION:
                    raise ConnectionError(f"协议版本不匹配: {p.get('protocol_version')}")
                self._peer_name = p.get("device_name", "unknown")
                return
            # 忽略非 hello 消息（如对方先发 ping）

    # ── 剪贴板同步 ───────────────────────────────────────────────

    def _handle_remote_clipboard(self, payload: dict):
        text = payload.get("text", "")
        content_hash = payload.get("content_hash", "")
        if not text:
            return
        # 校验 SHA
        if content_hash and _sha256_hex(text) != content_hash:
            log.warning("剪贴板内容哈希不匹配，丢弃")
            return

        log.info("收到 A→B 剪贴板 (%d 字符)", len(text))
        # 基于具体文本的抑制：只有读缓冲观察到同一文本时才消费
        self._suppress_text = text
        # 写系统剪贴板（通过队列派发到 GUI 线程）
        self._set_clipboard(text)
        # 阶段 3F-2：先 sink 接管（Bridge 内存队列唯一持有），后 best-effort 快照文件；
        # 快照仅供启动时导入一次/显式读取，不再是运行时第二事件源。
        event = {
            "version": 1,
            "event_id": str(uuid.uuid4()),
            "text": text,
            "content_hash": content_hash or _sha256_hex(text),
            "updated_at": _now_ms(),
        }
        if self._inbound_sink is not None:
            try:
                self._inbound_sink(
                    RemoteTask(event["event_id"], text, event["content_hash"], event["updated_at"])
                )
            except Exception as e:
                # sink 失败：可见错误日志（不吞）；本帧内存副本丢失，快照文件仅能救回
                # 最新 1 帧，连续失败不保证全数恢复（边界见总纲 §21）。
                log.warning("入站 sink 接管失败（event_id=%s）: %s", event["event_id"], e, exc_info=True)
        self._atomic_write_json(_remote_event_path(), event)
        self._last_clipboard_text = text

    def _check_local_clipboard(self):
        text = self._get_clipboard()
        if text is None:
            return

        # 基于具体文本的抑制：只有读缓冲已观察到同一远端内容时才消费
        # 这确保 GUI 线程已实际写入剪贴板（读缓冲更新后才匹配）
        if self._suppress_text is not None:
            if text == self._suppress_text:
                # GUI 已写入且读缓冲确认 → 消费抑制
                self._suppress_text = None
                self._last_clipboard_text = text
                return
            # 读缓冲尚未更新（仍是旧值）→ 不消费，等待下一轮
            # 同时不发送旧值（因为抑制已挂起）
            return

        if text == self._last_clipboard_text:
            return

        self._last_clipboard_text = text
        if not text.strip():
            return

        log.info("发送 B→A 剪贴板 (%d 字符)", len(text))
        try:
            self._send_raw(MSG_CLIPBOARD_UPDATE, {
                "text": text,
                "content_hash": _sha256_hex(text),
            })
        except (OSError, ValueError) as e:
            log.warning("B→A 发送失败: %s", e)

    # ── 状态文件 ─────────────────────────────────────────────────

    def _write_status(self, status: str):
        self._status = status
        is_connected = status == "connected"
        peer_ip = self._peer_ip_str if is_connected else None
        data = {
            "version": 1,
            "status": status,
            "peer_name": self._peer_name if is_connected else None,
            "peer_ip": peer_ip,
            "latency_ms": self._latency_ms if status == "connected" else None,
            "generation": self._gen,
            "updated_at": _now_ms(),
        }
        self._atomic_write_json(_status_path(), data)

    @staticmethod
    def _atomic_write_json(path: Path, data: dict):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(path))
        except OSError as e:
            log.warning("写文件失败 %s: %s", path, e)