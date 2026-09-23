"""OpenChamber 服务连接层（Lite）。

职责（随轮次扩展）：
  - probe()：探测服务可达性 + 服务延迟（"服务延迟"数据源）。
  - validate_session()：只读核实会话/directory 是否可用。
  - resolve_execution_config()：从当前会话解析可复用的执行配置
    （agent + providerID + modelID + variant），来源优先级：
    ① 会话历史中最近一条配置完整的 assistant 消息；
    ② 会话对象自身的 agent/model；
    都没有 → None（unavailable）。绝不猜默认模型。
  - send_text()：把 text 原样通过 prompt_async 发送（204=accepted，≠完成）。
  - compact_session()：走 UI 同路径 POST /summarize 压缩当前会话。

延迟口径：服务延迟 != 模型首响应延迟。模型首响应由真实任务
（prompt 发出 → 第一段模型响应）另行计算。
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


def is_loopback_base_url(base_url: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(base_url).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOOPBACK_HOSTS


def default_settings_path() -> Path:
    return Path.home() / ".config" / "openchamber" / "settings.json"


def resolve_local_token(
    base_url: str,
    *,
    settings_path: str | os.PathLike | None = None,
) -> str | None:
    """loopback 才解析本地 Bearer token（desktopLocalClientToken）。

    非 loopback 直接返回 None 且不读任何设置文件，杜绝远程目标
    携带本地桌面凭据。token 只用于组 Authorization 头，不落日志。
    """
    if not is_loopback_base_url(base_url):
        return None
    path = Path(settings_path) if settings_path is not None else default_settings_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("desktopLocalClientToken") if isinstance(data, dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """probe() 的稳定返回：永不抛异常，失败信息收敛到 error 字段。"""

    connected: bool
    latency_ms: int | None
    error: str | None


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """从当前会话解析出的可复用执行配置。

    provider_id/model_id/agent 必为非空；variant 可空。
    source 标记来源（"assistant"=历史消息，"session"=会话对象）。
    """

    agent: str
    provider_id: str
    model_id: str
    variant: str | None
    source: str


@dataclass(frozen=True, slots=True)
class SendResult:
    """send_text() 的稳定返回：2xx=accepted（≠任务完成）。"""

    accepted: bool
    error: str | None
    message_id: str | None


@dataclass(frozen=True, slots=True)
class CompactResult:
    """compact_session() 的稳定返回：200 且 body true 才算成功。"""

    success: bool
    error: str | None


@dataclass(frozen=True, slots=True)
class SessionStatusResult:
    """get_session_status() 只读返回：严格区分 成功拿到 status 与 404/transport/malformed。"""

    ok: bool
    status: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class TaskProgressResult:
    """get_task_progress() 只读返回：marker 是 user_message 之后内容的稳定指纹。

    read_ok=False 表示 messages GET 失败，绝不代表"没有进度"。
    """

    read_ok: bool
    marker: str | None
    user_message_found: bool
    error: str | None = None


class OpenChamberClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 5.0,
        settings_path: str | os.PathLike | None = None,
    ) -> None:
        base = base_url.rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise ValueError(f"base_url must be http(s): {base_url!r}")
        self.base_url = base
        self.timeout = timeout
        env_token = os.environ.get("OPENCHAMBER_CLIENT_TOKEN", "").strip()
        self._token = (token or env_token) or resolve_local_token(base, settings_path=settings_path)

    def probe(self) -> ProbeResult:
        url = self.base_url + "/health"
        request = urllib.request.Request(url)
        if self._token:
            request.add_header("Authorization", f"Bearer {self._token}")
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read(65536)
        except urllib.error.HTTPError as exc:
            latency = _elapsed_ms(started)
            return ProbeResult(False, latency, f"http: {exc.code}")
        except (TimeoutError, socket.timeout):
            latency = _elapsed_ms(started)
            return ProbeResult(False, latency, "timeout")
        except urllib.error.URLError as exc:
            latency = _elapsed_ms(started)
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                return ProbeResult(False, latency, "timeout")
            return ProbeResult(False, latency, f"connection: {reason}")
        except OSError as exc:
            latency = _elapsed_ms(started)
            return ProbeResult(False, latency, f"connection: {exc}")
        latency = _elapsed_ms(started)
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return ProbeResult(False, latency, "malformed: body is not JSON")
        if not isinstance(data, dict):
            return ProbeResult(False, latency, "malformed: health is not an object")
        return ProbeResult(True, latency, None)

    def _http(
        self, url: str, *, data: bytes | None = None
    ) -> tuple[int | None, bytes, str | None]:
        """极简只读/写传输：返回 (status, body, error)，永不抛异常。

        data 为 None 时是 GET；否则是 JSON POST。error 非 None 时
        status 为 None（传输层失败）或为 HTTP 错误码。
        """
        method = "POST" if data is not None else "GET"
        request = urllib.request.Request(url, data=data, method=method)
        if self._token:
            request.add_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read(), None
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read()
            except OSError:
                body = b""
            return exc.code, body, f"http: {exc.code}"
        except (TimeoutError, socket.timeout):
            return None, b"", "timeout"
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                return None, b"", "timeout"
            return None, b"", f"connection: {reason}"
        except OSError as exc:
            return None, b"", f"connection: {exc}"

    def validate_session(self, session_id: str, directory: str | None = None) -> bool:
        """只读核实：会话是否存在、directory 是否可用。

        用现有合同 `GET /api/session/{id}/message?directory=...`：
        2xx 表示该 session 存在且可取消息（directory 可用）；404/4xx/
        连接失败一律 False。永不抛异常。directory 为 None 时不带该参数。
        """
        if not session_id:
            return False
        status, _body, _error = self._http(self._messages_url(session_id, directory))
        return status is not None and status < 400

    def _messages_url(self, session_id: str, directory: str | None) -> str:
        url = (
            f"{self.base_url}/api/session/"
            f"{urllib.parse.quote(str(session_id), safe='')}/message"
        )
        if directory:
            url += f"?directory={urllib.parse.quote(str(directory), safe='/')}"
        return url

    def _new_message_id(self) -> str:
        return "msg_" + uuid.uuid4().hex

    def resolve_execution_config(
        self, session_id: str, directory: str | None = None
    ) -> ExecutionConfig | None:
        """解析当前会话可复用的执行配置（agent/providerID/modelID/variant）。

        优先：会话历史里最近一条配置完整的 assistant 消息（agent、
        providerID、modelID 均非空；variant 可空）。
        回退：会话对象自身的 agent + model（{id, providerID, variant}）。
        都没有 → None（unavailable）。绝不猜默认模型。
        """
        if not session_id:
            return None
        status, body, _error = self._http(self._messages_url(session_id, directory))
        if status is not None and status < 400:
            config = _config_from_messages(body)
            if config is not None:
                return config
        surl = f"{self.base_url}/api/session/{urllib.parse.quote(str(session_id), safe='')}"
        sstatus, sbody, _serr = self._http(surl)
        if sstatus is not None and sstatus < 400:
            return _config_from_session(sbody)
        return None

    def send_text(
        self, session_id: str, directory: str | None, text: str
    ) -> SendResult:
        """把 text 原样（不包装）经 prompt_async 发送到当前会话。

        执行配置来自 resolve_execution_config；配置不可用 →
        accepted=False（不猜模型）。2xx=accepted（≠任务完成）。
        永不抛异常。
        """
        config = self.resolve_execution_config(session_id, directory)
        if config is None:
            return SendResult(False, "unavailable: 无法取得当前会话的模型配置", None)
        message_id = self._new_message_id()
        body = {
            "messageID": message_id,
            "model": {"providerID": config.provider_id, "modelID": config.model_id},
            "agent": config.agent,
            "variant": config.variant,
            "parts": [{"type": "text", "text": text}],
        }
        url = (
            f"{self.base_url}/api/session/"
            f"{urllib.parse.quote(str(session_id), safe='')}/prompt_async"
        )
        if directory:
            url += f"?directory={urllib.parse.quote(str(directory), safe='/')}"
        status, _raw, error = self._http(url, data=json.dumps(body).encode("utf-8"))
        if error is not None and status is None:
            return SendResult(False, error, None)
        if status is not None and 200 <= status < 300:
            return SendResult(True, None, message_id)
        return SendResult(False, f"http: {status}", None)

    def compact_session(
        self, session_id: str, directory: str | None
    ) -> CompactResult:
        """压缩当前会话：走 OpenChamber UI 同路径 POST /summarize。

        body = {providerID, modelID}，来自 resolve_execution_config。
        成功 = HTTP 200 且 body 为 true。永不抛异常。
        """
        config = self.resolve_execution_config(session_id, directory)
        if config is None:
            return CompactResult(False, "unavailable: 无法取得当前会话的模型配置")
        body = {"providerID": config.provider_id, "modelID": config.model_id}
        url = (
            f"{self.base_url}/api/session/"
            f"{urllib.parse.quote(str(session_id), safe='')}/summarize"
        )
        if directory:
            url += f"?directory={urllib.parse.quote(str(directory), safe='/')}"
        status, raw, error = self._http(url, data=json.dumps(body).encode("utf-8"))
        if error is not None and status is None:
            return CompactResult(False, error)
        if status is not None and 200 <= status < 300:
            try:
                ok = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return CompactResult(False, "malformed: body is not JSON")
            if ok is True:
                return CompactResult(True, None)
            return CompactResult(False, f"body not true: {ok!r}")
        return CompactResult(False, f"http: {status}")

    def get_session_status(self, session_id: str) -> SessionStatusResult:
        """只读 GET /api/sessions/{id}/status。

        成功 = 2xx 且 body 有字符串 status 字段（busy/retry/idle...）。
        404 / transport / malformed 均返回 ok=False 且各自 error 不同，绝不伪装成 idle。
        """
        if not session_id:
            return SessionStatusResult(False, None, "empty: session_id 为空")
        surl = f"{self.base_url}/api/sessions/{urllib.parse.quote(str(session_id), safe='')}/status"
        status, body, error = self._http(surl)
        if error is not None and status is None:
            return SessionStatusResult(False, None, error)
        if status is not None and 200 <= status < 300:
            try:
                data = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return SessionStatusResult(False, None, "malformed: body is not JSON")
            if isinstance(data, dict) and isinstance(data.get("status"), str):
                return SessionStatusResult(True, data["status"], None)
            return SessionStatusResult(False, None, "malformed: missing 'status'")
        return SessionStatusResult(False, None, f"http: {status}")

    def get_task_progress(
        self, session_id: str, directory: str | None, user_message_id: str
    ) -> TaskProgressResult:
        """只读：判断"该 user message 之后的 session 内容是否发生了进展"。

        在消息列表里定位 info.id == user_message_id，取其到尾部的内容生成
        稳定 marker（canonical JSON → SHA-256）。assistant streaming 内容变化 /
        新 tool part / 新 assistant message / 新 continuation 都会改变 marker。
        不依赖 assistant parentID。messages GET 失败 → read_ok=False（≠没有进度）。
        """
        status, body, error = self._http(self._messages_url(session_id, directory))
        if error is not None and status is None:
            return TaskProgressResult(False, None, False, error)
        if status is None or not (200 <= status < 300):
            return TaskProgressResult(False, None, False, f"http: {status}")
        try:
            messages = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return TaskProgressResult(False, None, False, "malformed: body is not JSON")
        if not isinstance(messages, list):
            return TaskProgressResult(False, None, False, "malformed: messages not a list")
        anchor = None
        for i, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            info = message.get("info")
            if isinstance(info, dict) and info.get("id") == user_message_id:
                anchor = i
                break
        found = anchor is not None
        tail = messages[anchor:] if anchor is not None else messages
        canonical = json.dumps(_progress_projection(tail), ensure_ascii=False, sort_keys=True)
        marker = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return TaskProgressResult(True, marker, found, None)


def _nonempty_str(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _variant(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _created_ts(time_field) -> int:
    if isinstance(time_field, dict):
        created = time_field.get("created")
        if isinstance(created, (int, float)) and not isinstance(created, bool):
            return created
    return 0


def _config_from_messages(body: bytes) -> ExecutionConfig | None:
    """从会话消息里取最近一条配置完整的 assistant 消息。"""
    try:
        messages = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(messages, list):
        return None
    candidates = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        info = message.get("info")
        if not isinstance(info, dict) or info.get("role") != "assistant":
            continue
        agent = _nonempty_str(info.get("agent"))
        provider_id = _nonempty_str(info.get("providerID"))
        model_id = _nonempty_str(info.get("modelID"))
        if not (agent and provider_id and model_id):
            continue
        candidates.append(
            (_created_ts(info.get("time")), agent, provider_id, model_id, _variant(info.get("variant")))
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    _created, agent, provider_id, model_id, variant = candidates[-1]
    return ExecutionConfig(agent=agent, provider_id=provider_id, model_id=model_id, variant=variant, source="assistant")


def _config_from_session(body: bytes) -> ExecutionConfig | None:
    """回退来源：会话对象自身的 agent + model（{id, providerID, variant}）。"""
    try:
        session = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(session, dict):
        return None
    agent = _nonempty_str(session.get("agent"))
    model = session.get("model")
    provider_id = model_id = variant = None
    if isinstance(model, dict):
        provider_id = _nonempty_str(model.get("providerID"))
        model_id = _nonempty_str(model.get("id")) or _nonempty_str(model.get("modelID"))
        variant = _variant(model.get("variant"))
    if agent and provider_id and model_id:
        return ExecutionConfig(agent=agent, provider_id=provider_id, model_id=model_id, variant=variant, source="session")
    return None


def _progress_projection(tail) -> list:
    """把消息尾部投到稳定字段（id/role/各 part 的 type+text），供 marker 计算。

    只取会随真实进展变化的字段；忽略 time.updated 之类易变字段，避免误判"有进度"。
    """
    projection = []
    for message in tail:
        if not isinstance(message, dict):
            continue
        info = message.get("info") or {}
        parts = message.get("parts") or []
        projection.append(
            {
                "id": info.get("id"),
                "role": info.get("role"),
                "parts": [
                    {"type": p.get("type"), "text": p.get("text")}
                    for p in parts
                    if isinstance(p, dict)
                ],
            }
        )
    return projection


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:57123"
    result = OpenChamberClient(target).probe()
    if result.connected:
        print(f"connected latency_ms={result.latency_ms}")
    else:
        print(f"disconnected error={result.error}")
    raise SystemExit(0 if result.connected else 1)