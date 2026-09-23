"""ClipLink 远程剪贴板事件桥：A→B 事件读取（event_id 去重）、B→A 结果回传（单槽 pending）、AIRelayLite 状态文件。

本轮只做 ClipLink Bridge 本身，不做自动包装 / 模型发送 / watchdog / 中断续接 / 自动 compact / AI_RELAY_COMPLETE。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from cliplink_status import is_stale, now_millis, read_cliplink_status


@dataclass(frozen=True, slots=True)
class RemoteTask:
    event_id: str
    text: str
    content_hash: str
    updated_at: int


def default_remote_event_path() -> Path:
    base = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    return Path(base) / "ClipLink" / "remote_clipboard.json"


def default_status_file_path() -> Path:
    base = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    return Path(base) / "AIRelayLite" / "status.json"


class ClipLinkBridge:
    _STATUS_HEARTBEAT_MS = 2000

    def __init__(
        self,
        remote_event_path=None,
        cliplink_status_path=None,
        status_file_path=None,
        clipboard_writer=None,
    ) -> None:
        self._remote_path = Path(remote_event_path) if remote_event_path else default_remote_event_path()
        self._cliplink_status_path = cliplink_status_path
        self._status_file = Path(status_file_path) if status_file_path else default_status_file_path()
        self._clipboard_writer = clipboard_writer
        self._listening = False
        self._baseline_event_id: str | None = None
        self._pending_result: str | None = None
        self._relay_status = "idle"
        self._model_status = "unknown"
        self._session_id: str | None = None
        self._openchamber_latency_ms: int | None = None
        self._last_status_write_ms = 0
        self.on_remote_task = None

    # ── A→B inbound ──────────────────────────────────────────────

    def _read_remote_event(self) -> RemoteTask | None:
        try:
            raw = self._remote_path.read_text(encoding="utf-8-sig")
        except OSError:
            return None
        try:
            obj = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        event_id = obj.get("event_id")
        text = obj.get("text")
        content_hash = obj.get("content_hash")
        updated_at = obj.get("updated_at")
        if not isinstance(event_id, str) or not event_id:
            return None
        if not isinstance(text, str):
            return None
        if not isinstance(content_hash, str):
            return None
        if not isinstance(updated_at, (int, float)):
            return None
        return RemoteTask(event_id, text, content_hash, int(updated_at))

    def set_listening(self, enabled: bool) -> None:
        self._listening = enabled
        if enabled:
            task = self._read_remote_event()
            if task:
                self._baseline_event_id = task.event_id
        self._refresh_relay_status()
        self.write_status_file()

    def poll(self) -> RemoteTask | None:
        if not self._listening:
            return None
        task = self._read_remote_event()
        if task is None or task.event_id == self._baseline_event_id:
            return None
        self._baseline_event_id = task.event_id
        return task

    # ── B→A outbound ─────────────────────────────────────────────

    def _a_available(self) -> bool:
        st = read_cliplink_status(self._cliplink_status_path)
        return st is not None and st.status == "connected" and not is_stale(st, now_millis())

    def deliver_result(self, text: str) -> None:
        if self._a_available():
            try:
                self._clipboard_writer(text)
                self._pending_result = None
            except Exception:
                self._pending_result = text
        else:
            self._pending_result = text
        self._refresh_relay_status()
        self.write_status_file()

    def flush_pending_result(self) -> None:
        if self._pending_result is None or not self._a_available():
            return
        try:
            self._clipboard_writer(self._pending_result)
            self._pending_result = None
        except Exception:
            pass
        self._refresh_relay_status()
        self.write_status_file()

    # ── AIRelayLite 状态文件 ─────────────────────────────────────

    def _refresh_relay_status(self) -> None:
        if self._pending_result is not None:
            self._relay_status = "wait_return"
        elif self._listening:
            self._relay_status = "listening"
        else:
            self._relay_status = "idle"

    def set_model_status(self, status: str, latency_ms: int | None = None) -> None:
        self._model_status = status
        self._openchamber_latency_ms = latency_ms
        self.write_status_file()

    def set_session_id(self, session_id: str | None) -> None:
        self._session_id = session_id
        self.write_status_file()

    def write_status_file(self) -> None:
        data = {
            "version": 1,
            "relay_status": self._relay_status,
            "model_status": self._model_status,
            "openchamber_latency_ms": self._openchamber_latency_ms,
            "model_first_response_ms": None,
            "session_id": self._session_id,
            "updated_at": now_millis(),
        }
        try:
            self._status_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._status_file.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(self._status_file))
            self._last_status_write_ms = now_millis()
        except OSError:
            pass

    # ── QTimer tick ──────────────────────────────────────────────

    def tick(self) -> None:
        task = self.poll()
        if task is not None and self.on_remote_task is not None:
            self.on_remote_task(task)
        self.flush_pending_result()
        if now_millis() - self._last_status_write_ms >= self._STATUS_HEARTBEAT_MS:
            self.write_status_file()