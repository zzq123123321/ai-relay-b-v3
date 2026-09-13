"""AI Relay B V3.0：OpenChamber 只读 Transport 基座（T20-01）。

只读传输层：对外唯一 HTTP 能力是 GET，不实现 post/put/patch/delete/send/
create_session/compact/approve/stop/retry，也不暴露 request(method=...) 这类
可传任意方法的通用入口。使用标准库 urllib，不新增第三方 HTTP 依赖。

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

MAX_BODY_BYTES = 4 * 1024 * 1024

# 可自动携带本地 token 的精确主机白名单（不区分大小写，不扩展 127/8）
LOCAL_AUTH_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

KIND_INVALID_URL = "INVALID_URL"
KIND_UNSUPPORTED_SCHEME = "UNSUPPORTED_SCHEME"
KIND_NO_HOSTNAME = "NO_HOSTNAME"
KIND_CREDENTIALS_IN_URL = "CREDENTIALS_IN_URL"
KIND_TIMEOUT = "TIMEOUT"
KIND_CONNECTION = "CONNECTION"


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