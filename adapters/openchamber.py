"""AI Relay B V3.0：OpenChamber Transport 基座（T20 只读 + T22 prompt 写）。

只读传输层：对外唯一 HTTP 能力是 GET，不实现 post/put/patch/delete/send/
create_session/compact/approve/stop/retry，也不暴露 request(method=...) 这类
可传任意方法的通用入口。使用标准库 urllib，不新增第三方 HTTP 依赖。

生产写能力（T22-03）由独立、用途限定的 OpenChamberPromptAsyncTransport 提供：
- 与只读类分离：read transport/client 保持 GET-only，不新增任何写方法；
- 构造期冻结配置（base_url/directory/provider_id/model_id/agent/variant/token/
  timeout），不动态回读 SettingsService；
- 写目标 fail-closed：仅允许精确 loopback 白名单 {localhost, 127.0.0.1, ::1}；
- 唯一 side-effect 路径 = POST /api/session/{sid}/prompt_async?directory=<frozen>；
- 发送前只读快照只做 GET 且只保存最小字段，绝不保存 message body / token。

与 T19 封板 seam（scripts/probe_openchamber.py）保持一致：
- 单条请求 = base_url + path 直接拼接（同一构造路径）；
- 本地 Bearer 认证边界为精确白名单 {localhost, 127.0.0.1, ::1}（忽略大小写、
  不扩展 127/8，绝不使用 ipaddress.is_loopback 的宽松判定）；非白名单主机
  即使构造时传入 token 也会在构造期被丢弃，绝无自动携带;
- token 只进入 Authorization 头，绝不落入日志、异常正文或响应 DTO；
- /health 等无认证端点由调用方以 attach_auth=False 走同一 get() 路径，禁止
  分裂成两个客户端后误判认证。

错误语义（结构化 kind）：
- HTTP 4xx/5xx → 保留 status/body 的 OpenChamberRawResponse，不自动重试；
- 超时     → OpenChamberReadError(kind="TIMEOUT")；
- 连接失败 → OpenChamberReadError(kind="CONNECTION")；
- Base URL 非法（构造期）→ OpenChamberReadError(kind="INVALID_URL" /
  "UNSUPPORTED_SCHEME" / "NO_HOSTNAME" / "CREDENTIALS_IN_URL")。

单次 get() 最多触发 1 次 opener/urlopen 调用（注入 opener 计数 + 不重试保证）。
响应体上限 MAX_BODY_BYTES，超出截断且 truncated=True，绝不无限读取。
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from core.dispatch import (
    SendAttempt,
    SendOutcome,
    SendTransportError,
    SnapshotFailure,
)

MAX_BODY_BYTES = 4 * 1024 * 1024

# T22：prompt_async 写传输常量
SESSION_ID_PREFIXES = ("sess_", "ses_")
PROMPT_ASYNC_PATH_TMPL = "/api/session/{sid}/prompt_async"

# 可自动携带本地 token 的精确主机白名单（不区分大小写，不扩展 127/8）
LOCAL_AUTH_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

KIND_INVALID_URL = "INVALID_URL"
KIND_UNSUPPORTED_SCHEME = "UNSUPPORTED_SCHEME"
KIND_NO_HOSTNAME = "NO_HOSTNAME"
KIND_CREDENTIALS_IN_URL = "CREDENTIALS_IN_URL"
KIND_TIMEOUT = "TIMEOUT"
KIND_CONNECTION = "CONNECTION"

# T20-02：Client 层稳定分类（transport 的 TIMEOUT/CONNECTION 原样上抛，不在此列表改写）
KIND_AUTH = "AUTH"
KIND_NOT_FOUND_OR_UNSUPPORTED = "NOT_FOUND_OR_UNSUPPORTED"
KIND_HTTP_ERROR = "HTTP_ERROR"
KIND_MALFORMED_JSON = "MALFORMED_JSON"
KIND_UNEXPECTED_SHAPE = "UNEXPECTED_SHAPE"
KIND_MISSING_INPUT = "MISSING_INPUT"

# 分类型 missing semantics（[] 与 {} 各自独立，禁止合并为一个布尔 missing 标记）
NO_SESSION_OBSERVED_IN_DIRECTORY = "NO_SESSION_OBSERVED_IN_DIRECTORY"
NO_STATUS_ENTRY_OBSERVED_IN_DIRECTORY = "NO_STATUS_ENTRY_OBSERVED_IN_DIRECTORY"


def is_loopback_host(host: str | None) -> bool:
    """host 是否命中精确 loopback 白名单（localhost / 127.0.0.1 / ::1，忽略大小写）。"""
    if not host:
        return False
    return host.strip().lower() in LOCAL_AUTH_HOSTS


def is_loopback_base_url(base_url: str) -> bool:
    return is_loopback_host(urllib.parse.urlsplit(base_url).hostname)


def default_settings_path() -> Path:
    """默认桌面配置路径定位（与 probe_openchamber.default_settings_path 一致）。"""
    return Path.home() / ".config" / "openchamber" / "settings.json"


def resolve_local_auth_token(
    base_url: str,
    *,
    env_token: str | None,
    settings_path: str | os.PathLike | None = None,
) -> str | None:
    """按优先级解析本地 Bearer token（仅精确白名单主机允许解析）。

    优先级：
    1. env_token（OPENCHAMBER_CLIENT_TOKEN 等价注入值）；
    2. settings.json 的 desktopLocalClientToken 字段（settings_path 为空时使用
       default_settings_path() 默认定位；测试传入临时路径）。

    关键安全规则：非精确白名单主机直接返回 None，且【不读取】任何 desktop
    设置文件，杜绝远程目标携带本地桌面凭据。返回的 token 仅用于在 transport
    内构成 Authorization: Bearer 头，绝不落日志、异常正文或响应 DTO。
    """
    if not is_loopback_base_url(base_url):
        return None
    if env_token and isinstance(env_token, str) and env_token.strip():
        return env_token.strip()
    path = settings_path if settings_path is not None else default_settings_path()
    if path is None or not os.path.isfile(path):
        return None
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    value = data.get("desktopLocalClientToken") if isinstance(data, dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


class OpenChamberReadError(Exception):
    """只读传输结构化错误：稳定 kind 属性区分失败类别，detail 仅作排查上下文。"""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}".rstrip(": "))
        self.kind = kind


@dataclass(frozen=True, slots=True)
class OpenChamberRawResponse:
    status: int
    content_type: str | None
    body: bytes
    truncated: bool = False


def _validate_base_url(base_url: str) -> str:
    """构造期校验：http/https、必须有 hostname、禁止 username/password、去末尾 /。

    非法一律抛结构化 OpenChamberReadError，绝不静默修复成别的地址。
    """
    raw = (base_url or "").strip()
    if not raw:
        raise OpenChamberReadError(KIND_INVALID_URL, "base_url 不能为空")
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError as exc:
        raise OpenChamberReadError(
            KIND_INVALID_URL, f"URL 无法解析：{exc}"
        ) from exc
    if parts.scheme not in ("http", "https"):
        raise OpenChamberReadError(
            KIND_UNSUPPORTED_SCHEME, f"只允许 http/https，实际 scheme={parts.scheme!r}"
        )
    if parts.username is not None or parts.password is not None:
        raise OpenChamberReadError(KIND_CREDENTIALS_IN_URL, "URL 内不允许携带用户名/密码")
    if not parts.hostname:
        raise OpenChamberReadError(KIND_NO_HOSTNAME, "缺少主机名")
    return raw.rstrip("/")


def _read_bounded(raw, max_bytes: int) -> tuple[bytes, bool]:
    try:
        chunk = raw.read(max_bytes + 1)
    except (OSError, ValueError):
        return b"", True
    return chunk[:max_bytes], len(chunk) > max_bytes


def _content_type_of(raw) -> str | None:
    value = None
    try:
        if hasattr(raw, "headers"):
            value = raw.headers.get("Content-Type") or raw.headers.get("content-type")
    except AttributeError:
        value = None
    if not value:
        return getattr(raw, "content_type", None)
    return str(value)


class OpenChamberReadTransport:
    """OpenChamber 只读 GET 传输：唯一对外 HTTP 能力是 get()。

    - 构造期校验 base_url（http/https、必须有 hostname、禁止 user:pass、去末尾 /）；
    - 非白名单主机在构造期丢弃任何传入 token，从源头杜绝桌面凭据外泄；
    - get() 单次请求调用 opener/urlopen 恰好一次；HTTP 4xx/5xx 保留 status/body
      返回给调用方，不自动改成功、不自动重试；timeout/连接失败抛结构化错误。
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 3.0,
        opener=None,
    ) -> None:
        self.base_url = _validate_base_url(base_url)
        self.timeout = timeout
        self._loopback = is_loopback_base_url(self.base_url)
        # 非精确白名单：丢弃任何传入 token，绝不自动携带
        if self._loopback and isinstance(token, str) and token.strip():
            self._token = token.strip()
        else:
            self._token = None
        self._opener = opener
        self.n_calls = 0

    def get(self, path: str, *, attach_auth: bool) -> OpenChamberRawResponse:
        """发起一次 GET；返回结构化响应，不把 HTTP 状态错误当异常抛出。

        attach_auth=True 且目标是精确白名单主机且 token 存在时才附加
        Authorization: Bearer <token>；其余情况绝无 Authorization 头。
        单次调用最多触发 1 次 opener 调用，遇错误不重试。
        """
        self.n_calls += 1
        headers: dict[str, str] = {}
        if attach_auth and self._loopback and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, method="GET", headers=headers)
        try:
            if self._opener is not None:
                raw = self._opener(req)
            else:
                raw = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            body, truncated = _read_bounded(exc, MAX_BODY_BYTES)
            return OpenChamberRawResponse(
                status=exc.code,
                content_type=_content_type_of(exc),
                body=body,
                truncated=truncated,
            )
        except socket.timeout as exc:
            raise OpenChamberReadError(
                KIND_TIMEOUT, f"{type(exc).__name__} 请求超时"
            ) from exc
        except urllib.error.URLError as exc:
            raise OpenChamberReadError(
                KIND_CONNECTION, f"{type(exc).__name__}: {exc.reason}"
            ) from exc
        except OSError as exc:
            raise OpenChamberReadError(
                KIND_CONNECTION, f"{type(exc).__name__}: {exc}"
            ) from exc
        body, truncated = _read_bounded(raw, MAX_BODY_BYTES)
        status = int(getattr(raw, "status", None) or raw.getcode() or 0)
        return OpenChamberRawResponse(
            status=status, content_type=_content_type_of(raw), body=body, truncated=truncated
        )


class OpenChamberApiError(OpenChamberReadError):
    """Client 层结构化错误：kind 为稳定分类；status 为 HTTP 状态（输入错误为 None）；endpoint 为调用入口名。

    异常正文只含分类与静态排查上下文，绝不携带完整 response body、token 或 Authorization。
    """

    def __init__(
        self, kind: str, endpoint: str, status: int | None = None, detail: str = ""
    ) -> None:
        super().__init__(kind, detail)
        self.status = status
        self.endpoint = endpoint


@dataclass(frozen=True, slots=True)
class OpenChamberObservation:
    """单次只读观察结果，不可变：

    endpoint 明确 + HTTP status 保留 + parsed payload 原样保留 + missing semantics
    分类型表达。[]（session list）/ {}（session status）分别用稳定字符串表达"本次
    目录未观察到"，绝不合成布尔 missing 标记或全局断言。
    """

    endpoint: str
    http_status: int
    payload: object
    missing_semantics: str | None = None


def _require_directory(directory: str, endpoint: str) -> str:
    """blank 目录输入错误：不发送任何 HTTP，直接抛稳定 client 输入错误。"""
    if not isinstance(directory, str) or not directory.strip():
        raise OpenChamberApiError(
            KIND_MISSING_INPUT, endpoint, detail="directory 不能为空（不发送任何 HTTP）"
        )
    return directory


def _raise_http_error(endpoint: str, resp: OpenChamberRawResponse) -> None:
    if resp.status in (401, 403):
        raise OpenChamberApiError(KIND_AUTH, endpoint, status=resp.status, detail="HTTP 认证失败")
    if resp.status == 404:
        raise OpenChamberApiError(
            KIND_NOT_FOUND_OR_UNSUPPORTED, endpoint, status=resp.status, detail="接口不存在或未支持"
        )
    raise OpenChamberApiError(
        KIND_HTTP_ERROR, endpoint, status=resp.status, detail="HTTP 非 2xx"
    )


class OpenChamberReadClient:
    """OpenChamber 只读业务 Client（T20-02）。

    HTTP 唯一 seam 是 T20-01 封板的 OpenChamberReadTransport（禁止再造第二套
    HTTP client / urllib opener / requests）。四个只读 endpoint 观察，仅表达
    endpoint-level observation，不做统一 connected/healthy/everything_ok 综合判定。
    公开面无任何 send/message/approve 等写能力（message 仍为 T19 UNVERIFIED）。

    endpoint 契约（与 contracts/openchamber_contract.md §3/§8 一致）：
    - health()          GET /health（attach_auth=False）
    - list_sessions(d)  GET /api/session?directory=...（attach_auth=True）
    - session_status(d) GET /api/session/status?directory=...（attach_auth=True）
    - permission_state()GET /api/permission-auto-accept（attach_auth=True）
    """

    def __init__(self, transport: OpenChamberReadTransport) -> None:
        self.transport = transport

    def health(self) -> OpenChamberObservation:
        return self._observe(
            "health", "/health", attach_auth=False, top_shape=dict, empty_semantics=None
        )

    def list_sessions(self, directory: str) -> OpenChamberObservation:
        directory = _require_directory(directory, "list_sessions")
        query = urllib.parse.urlencode({"directory": directory})
        return self._observe(
            "list_sessions",
            f"/api/session?{query}",
            attach_auth=True,
            top_shape=list,
            empty_semantics=NO_SESSION_OBSERVED_IN_DIRECTORY,
        )

    def session_status(self, directory: str) -> OpenChamberObservation:
        directory = _require_directory(directory, "session_status")
        query = urllib.parse.urlencode({"directory": directory})
        return self._observe(
            "session_status",
            f"/api/session/status?{query}",
            attach_auth=True,
            top_shape=dict,
            empty_semantics=NO_STATUS_ENTRY_OBSERVED_IN_DIRECTORY,
        )

    def permission_state(self) -> OpenChamberObservation:
        obs = self._observe(
            "permission_state",
            "/api/permission-auto-accept",
            attach_auth=True,
            top_shape=dict,
            empty_semantics=None,
        )
        payload = obs.payload
        sessions = payload.get("sessions")
        revision = payload.get("revision")
        valid = (
            isinstance(sessions, dict)
            and isinstance(revision, int)
            and not isinstance(revision, bool)
            and all(isinstance(v, bool) for v in sessions.values())
        )
        if not valid:
            raise OpenChamberApiError(
                KIND_UNEXPECTED_SHAPE,
                "permission_state",
                status=obs.http_status,
                detail="permission 结构不符：sessions 须为 dict 且值全为 bool、revision 须为 int（不解析具体 session ID）",
            )
        return obs

    def _observe(
        self,
        endpoint: str,
        path: str,
        *,
        attach_auth: bool,
        top_shape: type,
        empty_semantics: str | None,
    ) -> OpenChamberObservation:
        resp = self.transport.get(path, attach_auth=attach_auth)
        if resp.status < 200 or resp.status >= 300:
            _raise_http_error(endpoint, resp)
        try:
            payload = json.loads(resp.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise OpenChamberApiError(
                KIND_MALFORMED_JSON, endpoint, status=resp.status, detail="2xx 响应体不是合法 JSON"
            ) from None
        if not isinstance(payload, top_shape):
            raise OpenChamberApiError(
                KIND_UNEXPECTED_SHAPE,
                endpoint,
                status=resp.status,
                detail=f"顶层应为 {top_shape.__name__}",
            )
        missing = empty_semantics if len(payload) == 0 else None
        return OpenChamberObservation(
            endpoint=endpoint, http_status=resp.status, payload=payload, missing_semantics=missing
        )


# =====================================================================
# T22-03：生产 prompt_async 写 Transport（独立、用途限定）
# =====================================================================

def is_valid_session_id(value: object) -> bool:
    """合法 session id：字符串 + ses_/sess_ 前缀 + 前缀后仍有非空内容。

    与 T21 smoke 探测同一前缀语义（ACCEPTED_SESSION_ID_PREFIXES），精确前缀判定，
    不以 ipaddress/宽松网络判定冒充。
    """
    if not isinstance(value, str):
        return False
    for prefix in SESSION_ID_PREFIXES:
        if value.startswith(prefix) and len(value) > len(prefix):
            return True
    return False


def _endpoint_matches(frozen_base_url: str, endpoint: str) -> bool:
    """endpoint 是否等于构造冻结 base_url（统一 trailing-slash normalization）。

    只允许去掉末尾斜杠这一规范化；不做 host/port/path 的静默改写，不同即不匹配。
    """
    return isinstance(endpoint, str) and endpoint.rstrip("/") == frozen_base_url.rstrip("/")


def _non_redirecting_write_opener() -> urllib.request.OpenerDirector:
    """生产写 opener：仅 HTTP/HTTPS handler。

    不含 HTTPRedirectHandler（3xx 原样返回，绝不跟进 Location），也不含
    HTTPErrorProcessor（4xx/5xx 原样返回，由调用方分类）。连接层错误
    （超时/URLError/OSError）照常上抛。
    """
    opener = urllib.request.OpenerDirector()
    opener.add_handler(urllib.request.HTTPHandler())
    opener.add_handler(urllib.request.HTTPSHandler())
    return opener


def _make_urllib_write_call(timeout: float):
    opener = _non_redirecting_write_opener()
    return lambda req: opener.open(req, timeout=timeout)


class OpenChamberPromptTransportError(SendTransportError):
    """prompt_async 写传输错误：结果不明（可能已投递），adapter 内绝不重试。

    kind 为稳定分类（TIMEOUT / CONNECTION / ENDPOINT_MISMATCH /
    INVALID_SESSION_ID / MISSING_PLANNED_ID / INVALID_PLANNED_ID /
    NON_LOOPBACK_TARGET / MISSING_CONFIG）。detail 只含静态排查上下文，
    绝不携带远端 response body、token 或 Authorization。
    """

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}".rstrip(": "))
        self.kind = kind


class OpenChamberPromptAsyncTransport:
    """T22-03：生产 prompt_async 发送 Transport（仅本类公开写面）。

    - 构造期冻结全部配置（base_url/directory/provider_id/model_id/agent/variant/
      token/timeout），绝不动态回读 SettingsService；测试 seam 注入
      read_opener / write_opener（均为可调用，入参 urllib Request）；
    - 写目标 fail-closed：base_url 非精确 loopback 白名单
      {localhost,127.0.0.1,::1} → 构造期拒绝（0 HTTP），不扩展 127/8；
    - 唯一 side-effect：POST /api/session/{sid}/prompt_async?directory=<frozen>；
      body 严格仅 5 个已实证顶层 key（messageID/model/agent/variant/parts）；
    - send 前快照（capture_pre_send_snapshot）只 GET，只保存最小字段；
    - 不自动重试、不 redirect-follow、不做 reconciliation、不再生任何 identity。

    单次 send_once() 最多触发 1 次 write opener 调用；出错不重试。
    """

    def __init__(
        self,
        base_url: str,
        directory: str,
        *,
        provider_id: str,
        model_id: str,
        agent: str,
        variant: str = "default",
        token: str | None = None,
        timeout: float = 3.0,
        read_opener=None,
        write_opener=None,
    ) -> None:
        self.base_url = _validate_base_url(base_url)
        if not is_loopback_base_url(self.base_url):
            raise OpenChamberPromptTransportError(
                "NON_LOOPBACK_TARGET",
                "生产写目标仅允许精确本机白名单 {localhost,127.0.0.1,::1}：拒绝构造（0 HTTP）",
            )
        self.directory = _require_nonblank(directory, "directory")
        self.provider_id = _require_nonblank(provider_id, "provider_id")
        self.model_id = _require_nonblank(model_id, "model_id")
        self.agent = _require_nonblank(agent, "agent")
        self.variant = _require_nonblank(variant, "variant")
        self.timeout = float(timeout)
        self._token = token.strip() if isinstance(token, str) and token.strip() else None
        self._read = OpenChamberReadTransport(
            self.base_url, token=self._token, timeout=self.timeout, opener=read_opener
        )
        self._write_call = write_opener if write_opener is not None else _make_urllib_write_call(
            self.timeout
        )

    # ------------------------------------------------------ pre-send snapshot（GET-only）

    def capture_pre_send_snapshot(
        self, *, endpoint: str, session_id: str, task_key: str
    ) -> dict:
        """发送前只读快照：只 GET、只保存最小字段；任一必要观察失败 → SnapshotFailure。

        快照只保存 session_exists / message_count / message_ids / user_message_ids /
        status_entry_present，绝不保存 message body、assistant 正文、token、
        Authorization 或 cookie。失败一律张 SnapshotFailure（0 POST，明确不是 UNKNOWN）。
        """
        if not _endpoint_matches(self.base_url, endpoint):
            raise SnapshotFailure(
                f"endpoint={endpoint!r} 与构造冻结 base_url={self.base_url!r} "
                "不一致：拒绝快照（0 HTTP）"
            )
        if not is_valid_session_id(session_id):
            raise SnapshotFailure(
                f"非法 session_id={session_id!r}（只接受 ses_/sess_ 前缀）：拒绝快照（0 HTTP）"
            )
        del task_key  # send 前快照不依赖 task_key；仅保留签名兼容

        session_payload = self._snapshot_json(
            "session list", "/api/session?" + self._directory_query()
        )
        if not isinstance(session_payload, list):
            raise SnapshotFailure("session list 顶层不是数组：快照中止（0 POST）")
        found = False
        for entry in session_payload:
            if isinstance(entry, dict) and entry.get("id") == session_id:
                found = True
                break
        if not found:
            raise SnapshotFailure(
                f"session list 不存在预期会话 {session_id!r}：快照中止（0 POST）"
            )

        message_path = (
            f"/api/session/{urllib.parse.quote(session_id, safe='')}/message?"
            + self._directory_query()
        )
        message_payload = self._snapshot_json("message list", message_path)
        if not isinstance(message_payload, list):
            raise SnapshotFailure("message list 顶层不是数组：快照中止（0 POST）")
        message_ids: list[str] = []
        user_message_ids: list[str] = []
        for index, item in enumerate(message_payload):
            if not isinstance(item, dict):
                raise SnapshotFailure(f"message[{index}] 不是对象：快照中止（0 POST）")
            info = item.get("info")
            if not isinstance(info, dict):
                raise SnapshotFailure(
                    f"message[{index}] 缺少 info 对象，不符 T21 封板形状 "
                    "{{info:{{id,role,parentID}},parts:[...]}}：快照中止（0 POST）"
                )
            mid = info.get("id")
            role = info.get("role")
            if not isinstance(mid, str) or not mid:
                raise SnapshotFailure(f"message[{index}] info.id 缺失/非法：快照中止（0 POST）")
            if role is not None and not isinstance(role, str):
                raise SnapshotFailure(f"message[{index}] info.role 非法：快照中止（0 POST）")
            message_ids.append(mid)
            if role == "user":
                user_message_ids.append(mid)

        status_payload = self._snapshot_json(
            "session status", "/api/session/status?" + self._directory_query()
        )
        if not isinstance(status_payload, dict):
            raise SnapshotFailure("session status 顶层不是对象：快照中止（0 POST）")
        return {
            "session_exists": True,
            "message_count": len(message_ids),
            "message_ids": message_ids,
            "user_message_ids": user_message_ids,
            "status_entry_present": bool(status_payload),
        }

    # ------------------------------------------------------ send_once（唯一写入口）

    def send_once(
        self,
        *,
        endpoint: str,
        session_id: str,
        prompt_text: str,
        operation_id: str,
        planned_remote_user_id: str | None,
    ) -> SendAttempt:
        """执行单次 prompt_async POST（最多 1 次 opener 调用）。

        - planned_remote_user_id 只来自 ledger（nonblank + msg_ 前缀），绝不重新生成；
        - 204 → ACCEPTED（remote_user_id=planned）；400-499 → REJECTED；
          其他（5xx/3xx/unexpected 2xx）→ UNKNOWN；任何传输异常上抛
          OpenChamberPromptTransportError（Dispatch 视为结果不明）。
        - evidence 只保留 http_status + classification，绝不保存远端响应正文。
        """
        del operation_id  # body 不含独立 operationID 字段；identity 只来自 ledger planned id
        if not _endpoint_matches(self.base_url, endpoint):
            raise OpenChamberPromptTransportError(
                "ENDPOINT_MISMATCH",
                "send 目标与构造冻结 base_url 不一致：拒绝发送（0 HTTP）",
            )
        if not is_valid_session_id(session_id):
            raise OpenChamberPromptTransportError(
                "INVALID_SESSION_ID",
                f"非法 session_id={session_id!r}（只接受 ses_/sess_ 前缀）：拒绝发送（0 HTTP）",
            )
        if not isinstance(planned_remote_user_id, str) or not planned_remote_user_id.strip():
            raise OpenChamberPromptTransportError(
                "MISSING_PLANNED_ID", "planned_remote_user_id 为空：拒绝发送（0 HTTP）"
            )
        planned = planned_remote_user_id.strip()
        if not planned.startswith("msg_"):
            raise OpenChamberPromptTransportError(
                "INVALID_PLANNED_ID",
                "planned_remote_user_id 非 msg_ 前缀：拒绝发送（0 HTTP）",
            )

        path = PROMPT_ASYNC_PATH_TMPL.format(sid=urllib.parse.quote(session_id, safe=""))
        url = f"{self.base_url}{path}?{self._directory_query()}"
        body = {
            "messageID": planned,
            "model": {"providerID": self.provider_id, "modelID": self.model_id},
            "agent": self.agent,
            "variant": self.variant,
            "parts": [{"type": "text", "text": prompt_text}],
        }
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(
            url, method="POST", headers=headers,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        )
        status = self._post_status(req)
        return _send_attempt_from_status(status, planned)

    # ------------------------------------------------------ internals

    def _directory_query(self) -> str:
        return urllib.parse.urlencode({"directory": self.directory})

    def _snapshot_json(self, what: str, path: str) -> object:
        try:
            resp = self._read.get(path, attach_auth=True)
        except OpenChamberReadError as exc:
            raise SnapshotFailure(
                f"{what} GET 失败（{exc.kind}）：快照中止（0 POST）"
            ) from exc
        if not (200 <= resp.status < 300):
            raise SnapshotFailure(f"{what} GET HTTP {resp.status}：快照中止（0 POST）")
        try:
            return json.loads(resp.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise SnapshotFailure(
                f"{what} GET 响应非合法 JSON：快照中止（0 POST）"
            ) from exc

    def _post_status(self, req: urllib.request.Request) -> int:
        try:
            raw = self._write_call(req)
            status = int(getattr(raw, "status", None) or raw.getcode() or 0)
            _read_bounded(raw, MAX_BODY_BYTES)  # 有界读取并丢弃，绝不解析为完成结果
            return status
        except urllib.error.HTTPError as exc:
            _read_bounded(exc, MAX_BODY_BYTES)  # 有界读取并丢弃，绝不写远端正文进 evidence
            return exc.code
        except socket.timeout as exc:
            raise OpenChamberPromptTransportError(
                "TIMEOUT", "prompt_async POST 超时：结果不明，禁止重试"
            ) from exc
        except urllib.error.URLError as exc:
            raise OpenChamberPromptTransportError(
                "CONNECTION", f"{type(exc).__name__}: {exc.reason}"
            ) from exc
        except OSError as exc:
            raise OpenChamberPromptTransportError(
                "CONNECTION", f"{type(exc).__name__}: {exc}"
            ) from exc


def _send_attempt_from_status(status: int, planned_id: str) -> SendAttempt:
    """HTTP status → SendOutcome（204 唯一 ACCEPTED）。

    400-499 → REJECTED（evidence 只含 http_status+classification）；
    500-599 / 3xx / 除 204 外 unexpected 2xx / 其余 → UNKNOWN；
    REJECTED/UNKNOWN 一律 remote_user_id=None。
    """
    if status == 204:
        return SendAttempt(
            outcome=SendOutcome.ACCEPTED,
            remote_user_id=planned_id,
            evidence={"http_status": 204, "classification": "accepted"},
        )
    if 400 <= status < 500:
        return SendAttempt(
            outcome=SendOutcome.REJECTED,
            evidence={"http_status": status, "classification": "rejected"},
        )
    return SendAttempt(
        outcome=SendOutcome.UNKNOWN,
        evidence={"http_status": status, "classification": "unknown"},
    )


def _require_nonblank(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OpenChamberPromptTransportError(
            "MISSING_CONFIG", f"{name} 不能为空（任何 POST 之前拒绝）"
        )
    return value.strip()