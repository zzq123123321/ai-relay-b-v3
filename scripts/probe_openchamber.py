"""AI Relay B V3.0：OpenChamber 只读合同探测器（T19-B1）。

本脚本是只读证据采集器：对主规格 07.1 的候选端点做 GET 探测，输出脱敏证据。

边界（本脚本绝不越界）：
- 只允许 HTTP GET；源码中不存在任何写式 HTTP 方法调用，也不创建会话、不发送
  消息、不触发 /compact /stop /retry /approve。
- /health 与 /api/* 全部走同一个 ProbeTransport 实例（同一请求构建路径），
  只是认证策略不同：/health 不带 Authorization，loopback 上的 /api/* 由规则附加
  Bearer；探测客户端与后续 API 客户端必须共用这一 seam，禁止分裂成两个客户端后
  误判认证。
- Bearer token 仅当目标为 loopback（localhost / 127.0.0.1 / ::1）时自动附加；
  非 loopback 一律不读取本地 desktop token、不附加任何 token。
- 证据输出前递归脱敏（token/authorization/password/secret/cookie/api_key 等值
  替换为 <redacted>）；sample_hash 基于脱敏后的规范化 JSON 而非原始响应计算。
- service_reachable（health 可达）绝不自动推出 api_authenticated 或 session API
  支持；每个 endpoint 独立记录 capability_status / http_status / content_type /
  response_shape / pagination_evidence / missing_semantics / error_kind / sample_hash。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:57123"
DEFAULT_TIMEOUT_SECONDS = 3.0
MAX_BODY_BYTES = 4 * 1024 * 1024
REDACTED = "<redacted>"

# Bearer token 只在 loopback 目标上自动附加
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")

# 证据脱敏：值匹配以下 key（大小写与连字符归一后）一律替换为 <redacted>
_REDACT_KEYS = frozenset(
    {
        "token",
        "authorization",
        "password",
        "secret",
        "cookie",
        "api_key",
        "apikey",
        "clienttoken",
        "desktoplocalclienttoken",
        "desktopuipassword",
        "uipassword",
        "privatejwk",
        "publicjwk",
    }
)

_PAGINATION_KEYS = frozenset(
    {
        "page",
        "pages",
        "limit",
        "total",
        "count",
        "offset",
        "hasmore",
        "has_more",
        "cursor",
        "nextcursor",
        "next_cursor",
    }
)


def is_loopback_host(host: str | None) -> bool:
    """host 是否命中精确 loopback 白名单（localhost / 127.0.0.1 / ::1）。"""
    if not host:
        return False
    return host.strip().lower() in LOOPBACK_HOSTS


def is_loopback_base_url(base_url: str) -> bool:
    return is_loopback_host(urllib.parse.urlsplit(base_url).hostname)


def default_settings_path() -> Path:
    return Path.home() / ".config" / "openchamber" / "settings.json"


def resolve_auth_token(
    *,
    env_token: str | None,
    settings_path: Path | None = None,
    attach_allowed: bool,
) -> str | None:
    """按优先级解析 token。

    1. OPENCHAMBER_CLIENT_TOKEN 环境变量；
    2. %USERPROFILE%\\.config\\openchamber\\settings.json 的 desktopLocalClientToken。

    attach_allowed=False（非 loopback）时：不读取本地 desktop token，直接返回 None，
    保证非 loopback 请求绝不会携带本地凭据。
    返回的 token 仅用于在 transport 内构成 Bearer 头，绝不落日志/证据。
    """
    if not attach_allowed:
        return None
    if env_token and env_token.strip():
        return env_token.strip()
    path = settings_path if settings_path is not None else default_settings_path()
    if path is not None and path.is_file():
        try:
            raw = path.read_text(encoding="utf-8")
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


class ProbeTransportError(Exception):
    """探测传输错误：timeout / 连接失败等（结构化 kind，不做 HTTP 结果处理）。"""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}".rstrip(": "))
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True, slots=True)
class RawResponse:
    status: int
    content_type: str | None = None
    body: bytes | None = None
    truncated: bool = False

    @property
    def decoded(self) -> str:
        if self.body is None:
            return ""
        return self.body.decode("utf-8", "replace")


class ProbeTransport:
    """唯一 HTTP 传输 seam：/health 与 /api/* 共用同一个实例。

    request() 是全部请求的唯一构建路径；attach_auth 只控制本请求是否按规则附加
    Bearer。非 loopback 时即使有 token 也绝不附加（三重防护）。
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        opener=None,
    ) -> None:
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("base_url 不能为空")
        self.base_url = base
        self.timeout = timeout
        self._loopback = is_loopback_base_url(self.base_url)
        # 非 loopback：丢弃任何传入 token，从源头杜绝桌面凭据外泄
        self.token = token if self._loopback else None
        self._opener = opener
        self.n_calls = 0
        self.call_log: list[tuple[str, str, bool]] = list()

    @property
    def is_loopback(self) -> bool:
        return self._loopback

    def request(self, method: str, path: str, *, attach_auth: bool) -> RawResponse:
        """单条 HTTP 请求的构建与执行（仅 GET 路径使用；写式方法被本 seam 拒绝）。"""
        self.n_calls += 1
        self.call_log.append((method, path, attach_auth))
        headers: dict[str, str] = {}
        if attach_auth and self._loopback and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, method=method, headers=headers)
        try:
            if self._opener is not None:
                raw = self._opener(req)
            else:
                raw = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            body, truncated = self._read_bounded(exc, MAX_BODY_BYTES)
            return RawResponse(
                status=exc.code,
                content_type=_content_type_of(exc),
                body=body,
                truncated=truncated,
            )
        except socket.timeout as exc:
            raise ProbeTransportError("TIMEOUT", f"{type(exc).__name__} 请求超时")
        except urllib.error.URLError as exc:
            raise ProbeTransportError("CONNECTION", f"{type(exc).__name__}: {exc.reason}")
        except OSError as exc:
            raise ProbeTransportError("CONNECTION", f"{type(exc).__name__}: {exc}")
        return self._consume(raw)

    # ------------------------------------------------------------- internals

    def _consume(self, raw) -> RawResponse:
        body, truncated = self._read_bounded(raw, MAX_BODY_BYTES)
        status = int(getattr(raw, "status", raw.getcode() or 0))
        return RawResponse(
            status=status, content_type=_content_type_of(raw), body=body, truncated=truncated
        )

    @staticmethod
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
        ctype = getattr(raw, "content_type", None)
        return ctype
    return str(value)


# ---------------------------------------------------------------- evidence building


def redact_json(value: Any) -> Any:
    """递归脱敏：key 命中敏感集合的值替换为 <redacted>，保留结构与普通值。"""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            norm = str(key).lower().replace("-", "_")
            if norm in _REDACT_KEYS:
                out[str(key)] = REDACTED
            else:
                out[str(key)] = redact_json(item)
        return out
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    return value


def shape_of(value: Any) -> Any:
    """只记录结构、不保存完整正文的形状。

    dict  → {"type": "dict", "top_keys": [...]}
    list  → {"type": "list", "length": N, "item_type": <首项形状|"null">,
             "item_top_keys": [...|None]}（空数组 item_type="null"）
    原始值 → "str" / "number" / "bool" / "null"
    """
    if isinstance(value, dict):
        return {"type": "dict", "top_keys": [str(k) for k in value.keys()]}
    if isinstance(value, list):
        if not value:
            return {"type": "list", "length": 0, "item_type": "null", "item_top_keys": None}
        first = value[0]
        return {
            "type": "list",
            "length": len(value),
            "item_type": shape_of(first),
            "item_top_keys": [str(k) for k in first.keys()] if isinstance(first, dict) else None,
        }
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "str"
    return "null"


def pagination_evidence(value: Any, prefix: str = "") -> list[str]:
    found: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                full = f"{path}.{key}" if path else str(key)
                norm = str(key).lower().replace("-", "_")
                if norm in _PAGINATION_KEYS and not isinstance(item, (dict, list)):
                    found.append(f"{full}={item}")
                walk(item, full)
        elif isinstance(node, list) and node:
            walk(node[0], f"{path}[0]")

    walk(value, prefix)
    return found


def missing_semantics(value: Any, prefix: str = "") -> list[str]:
    found: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                full = f"{path}.{key}" if path else str(key)
                norm = str(key).lower().replace("-", "_")
                if item is None or "missing" in norm:
                    found.append(f"{full}={item}")
                walk(item, full)
        elif isinstance(node, list) and node:
            walk(node[0], f"{path}[0]")

    walk(value, prefix)
    return found


def sample_hash_of(redacted: Any) -> str:
    """sample_hash 基于脱敏后的规范化 JSON（sort_keys + 紧凑分隔符）。"""
    canonical = json.dumps(
        redacted, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EndpointPlan:
    name: str
    method: str
    path: str
    attach_auth: bool
    missing_input: bool = False


def build_endpoint_plan(directory: str, session_id: str | None) -> list[EndpointPlan]:
    """构造本轮只读探测计划。

    session_id 未提供时：message endpoint 标记 MISSING_INPUT，不发起请求，不猜 session。
    """
    encoded_dir = urllib.parse.quote(directory, safe="")
    plans = [
        EndpointPlan("health", "GET", "/health", attach_auth=False),
        EndpointPlan(
            "session_list", "GET", f"/api/session?directory={encoded_dir}", attach_auth=True
        ),
        EndpointPlan(
            "session_status",
            "GET",
            f"/api/session/status?directory={encoded_dir}",
            attach_auth=True,
        ),
        EndpointPlan(
            "permission", "GET", "/api/permission-auto-accept", attach_auth=True
        ),
    ]
    sid = (session_id or "").strip()
    if sid:
        encoded_sid = urllib.parse.quote(sid, safe="")
        plans.append(
            EndpointPlan(
                "session_messages",
                "GET",
                f"/api/session/{encoded_sid}/message?directory={encoded_dir}",
                attach_auth=True,
            )
        )
    else:
        plans.append(
            EndpointPlan(
                "session_messages",
                "GET",
                "",
                attach_auth=True,
                missing_input=True,
            )
        )
    return plans


def probe_once(transport: ProbeTransport, plan: EndpointPlan, *, max_bytes: int) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "endpoint": plan.name,
        "method": plan.method,
        "path": plan.path or "(not requested)",
        "capability_status": None,
        "http_status": None,
        "content_type": None,
        "response_shape": None,
        "pagination_evidence": [],
        "missing_semantics": [],
        "error_kind": None,
        "sample": None,
        "sample_hash": None,
    }
    if plan.missing_input:
        entry["capability_status"] = "UNVERIFIED"
        entry["error_kind"] = "MISSING_INPUT"
        return entry
    try:
        raw = transport.request(plan.method, plan.path, attach_auth=plan.attach_auth)
    except ProbeTransportError as exc:
        entry["capability_status"] = "UNVERIFIED"
        entry["error_kind"] = exc.kind
        return entry
    entry["http_status"] = raw.status
    if raw.content_type:
        entry["content_type"] = raw.content_type.split(";")[0].strip()
    if raw.truncated:
        entry["error_kind"] = "SIZE_OVER_LIMIT"

    parsed: Any = None
    text = raw.decoded
    if text.strip():
        try:
            parsed = json.loads(text)
        except ValueError:
            entry["error_kind"] = entry["error_kind"] or "PARSE"

    status = raw.status
    if parsed is not None and 200 <= status < 300:
        entry["capability_status"] = "SUPPORTED"
    elif status in (401, 403):
        entry["capability_status"] = "UNVERIFIED"
        entry["error_kind"] = entry["error_kind"] or "AUTH"
    elif status == 404:
        entry["capability_status"] = "UNVERIFIED"
        entry["error_kind"] = entry["error_kind"] or "NOT_FOUND_OR_UNSUPPORTED"
    elif status >= 400:
        entry["capability_status"] = "UNVERIFIED"
        entry["error_kind"] = entry["error_kind"] or f"HTTP_{status}"
    elif parsed is None:
        entry["capability_status"] = "UNVERIFIED"
        entry["error_kind"] = entry["error_kind"] or "PARSE"
    else:
        entry["capability_status"] = "UNVERIFIED"
        entry["error_kind"] = entry["error_kind"] or "UNKNOWN"

    if parsed is not None:
        redacted = redact_json(parsed)
        entry["response_shape"] = shape_of(parsed)
        entry["pagination_evidence"] = pagination_evidence(parsed)
        entry["missing_semantics"] = missing_semantics(parsed)
        entry["sample"] = redacted
        entry["sample_hash"] = sample_hash_of(redacted)
    return entry


def run_probe(
    transport: ProbeTransport,
    *,
    directory: str,
    session_id: str | None = None,
    max_bytes: int = MAX_BODY_BYTES,
) -> dict[str, Any]:
    """执行完整只读探测计划并组装证据。

    service_reachable 与 api_authenticated 严格分开：前者只由 /health 的 HTTP 状态
    决定，后者只由 /api/* 的 HTTP 状态决定，二者互不推出。
    """
    plans = build_endpoint_plan(directory, session_id)
    endpoints = [probe_once(transport, plan, max_bytes=max_bytes) for plan in plans]

    health = next((e for e in endpoints if e["endpoint"] == "health"), None)
    api_endpoints = [e for e in endpoints if e["endpoint"] != "health"]
    api_probed = [e for e in api_endpoints if e["error_kind"] != "MISSING_INPUT"]

    service_reachable = bool(health is not None and health["http_status"] is not None and 200 <= health["http_status"] < 300)
    api_2xx = any(
        e["http_status"] is not None and 200 <= e["http_status"] < 300 for e in api_probed
    )
    api_auth_refused = any(e["error_kind"] == "AUTH" for e in api_probed)
    if api_2xx:
        api_authenticated = True
    elif api_auth_refused:
        api_authenticated = False
    else:
        api_authenticated = None

    return {
        "tool": "probe_openchamber",
        "round": "T19-B1",
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "transport": {
            "base_url": transport.base_url,
            "loopback_target": transport.is_loopback,
            "same_transport_seam": True,
            "write_methods_used": [],
        },
        "directory": directory,
        "session_id_supplied": bool((session_id or "").strip()),
        "service_reachable": service_reachable,
        "api_authenticated": api_authenticated,
        "endpoints": endpoints,
        "notes": {
            "capability_values": ["SUPPORTED", "UNVERIFIED"],
            "message_endpoint_policy": (
                "session_id 未明确提供时不对 message endpoint 发起请求"
                if not (session_id or "").strip()
                else "session_id 已提供"
            ),
        },
    }


# ---------------------------------------------------------------- CLI


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="probe_openchamber",
        description="OpenChamber 只读合同探测器（仅 GET，输出脱敏证据）",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="OpenChamber 根地址")
    parser.add_argument("--directory", required=True, help="探测所用工作目录")
    parser.add_argument("--session-id", default=None, help="可选：明确已知的会话 ID")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="每请求超时秒数")
    parser.add_argument("--out", default=None, help="可选：证据输出文件路径，默认 stdout")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    loopback = is_loopback_base_url(args.base_url)
    token = resolve_auth_token(
        env_token=os.environ.get("OPENCHAMBER_CLIENT_TOKEN"),
        settings_path=default_settings_path(),
        attach_allowed=loopback,
    )
    transport = ProbeTransport(args.base_url, token=token, timeout=args.timeout)
    evidence = run_probe(
        transport, directory=args.directory, session_id=args.session_id,
    )
    text = json.dumps(evidence, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())