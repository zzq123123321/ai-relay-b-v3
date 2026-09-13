"""AI Relay B V3.0：T21-02 Disposable Session 单次真实写冒烟（ARM 专用）。

本轮是 T21 系列【首次允许真实 side-effect POST】，但严格限 2 次：
- POST #1  /api/session                                  -> 创建 1 个 disposable probe session
- POST #2  /api/session/{sid}/prompt_async               -> 发送 1 个 synthetic prompt
任何失败 / timeout / reset 都【不得自动重发 POST】；绝不调用 /message /prompt /compact
/interrupt /approve /reject /delete /archive /stop /cancel；删除与归档能力【不实现】。

与只读探测器（probe_openchamber，GET-only seam）不同，本脚本为了 ARM 必须携带写结构，
因此采用【最窄写面】设计，把写行为锁死在两个私有、用途明确的函数里：

- `_create_probe_session_once(...)`  内有且仅有一次 POST /api/session（模块常量 CREATE_PATH）；
- `_send_probe_prompt_once(...)`      内有且仅有一次 POST /api/session/{sid}/prompt_async（模板常量）；
- 二者都通过模块内私有 `_post_json(...)` 执行【单次】POST；
  模块【不暴露】任何公共的 post()/request() 通用入口；
- 每次 POST 前先在 durable journal（.recovery/t21_write_smoke_state.json）原子落盘 intent；
- journal 存在且 create_attempt_count>0 时默认 REFUSE NEW WRITES，只允许只读 reconciliation。
- 进程内硬上限：_post_json 只允许发出总计 2 次 POST（第 3 次直接拒绝）。

T21-02R（UNKNOWN_CREATE 只读恢复 + 单次 Prompt 续测）：
- 前缀裁决：真实运行时 session id 前缀 = "ses_"（OBSERVED runtime fact），"sess_" 仅为
  本地静态源/文档兼容线索。二者都被接受为合法前缀，但 session identity 决定性条件是：
  GET /api/session?directory=<existing_probe_directory> 恰好 1 条会话 且 id 合法。
- --resume-once：仅当 phase==UNKNOWN_CREATE && create_attempt_count==1 &&
  send_attempt_count==0 时进入；执行只读 reconciliation -> SESSION_CREATED -> WRITE #2
  prompt once -> GET-only completion observation。它【绝不调用 create】。
- 严禁 reset journal / 新 RUN_ID / 新 probe directory / 第二次 create POST。
- 真实 id 只落私有 journal；公开证据/控制台/回传报告只允许 redacted + hash。

fail-closed：真实写目标只允许 `http://127.0.0.1:57123`（精确匹配，任何偏差 -> 拒绝 armed write）。
token 仅本机 loopback 解析（复用 T19 规则），绝不打日志 / 落证据。

完成判定（四条件同时成立）：
  A) 本轮 user message（id == PROBE_MESSAGE_ID）可定位；
  B) assistant response role == "assistant"；
  C) assistant text 精确 == "T21_PROBE_OK_<RUN_NONCE>"；
  D) assistant 与本轮 user message 有显式 parent/causal 关系（assistant.parentID == 本轮 messageID）。
禁止"列表最后一条 assistant 就算"，也禁止单靠时间最近 / 内容相似判归属。

数据模型区分 execute_accepted（204=已接纳）与 completed&attributed（四条件齐全），
204 本身绝不推出 completed。

本脚本默认无副作用：不带 --arm-once 时只做 dry-run（打印计划，绝不发 POST）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scripts.probe_openchamber import (
        DEFAULT_TIMEOUT_SECONDS,
        MAX_BODY_BYTES,
        ProbeTransport as _ProbeTransport,
        ProbeTransportError as _ProbeTransportError,
        default_settings_path,
        is_loopback_base_url,
        resolve_auth_token,
    )
except ImportError:  # pragma: no cover - 允许脚本独立以 ``python -m scripts.xxx`` 运行
    from probe_openchamber import (
        DEFAULT_TIMEOUT_SECONDS,
        MAX_BODY_BYTES,
        ProbeTransport as _ProbeTransport,
        ProbeTransportError as _ProbeTransportError,
        default_settings_path,
        is_loopback_base_url,
        resolve_auth_token,
    )

# --------------------------------------------------------------------------- 不变的守口边界

ALLOWED_BASE_URL = "http://127.0.0.1:57123"
CLIENT_TOKEN_ENV = "OPENCHAMBER_CLIENT_TOKEN"

# 两条被允许的真实写端点（精确常量，代码内不得出现其他写路径）
CREATE_PATH = "/api/session"
PROMPT_ASYNC_PATH_TMPL = "/api/session/{sid}/prompt_async"

MAX_CREATE_POSTS = 1
MAX_PROMPT_POSTS = 1
MAX_WRITE_POSTS = 2

MARKER_PREFIX = "T21_PROBE_OK_"
SESS_ID_PREFIX = "sess_"
MSG_ID_PREFIX = "msg_"

# 前缀裁决（A 端裁决，遵守执行）：
# - "ses_" = OBSERVED runtime fact（真实服务端实际使用）；
# - "sess_" = DOCUMENTED_LOCAL_SOURCE / static-source compatibility clue；
# 运行时两者都接受为合法前缀；但 reconciliation 决定性条件仍是
# directory 中恰好 1 条会话 + 非空合法 id。
ACCEPTED_SESSION_ID_PREFIXES = ("sess_", "ses_")

PHASES = (
    "PREPARED",
    "CREATE_ATTEMPTED",
    "SESSION_CREATED",
    "UNKNOWN_CREATE",
    "SEND_ATTEMPTED",
    "SEND_ACCEPTED",
    "UNKNOWN_SEND",
    "RESULT_OBSERVED",
    "FAILED",
)

# 进程内硬上限：即使 journal 被误删，_post_json 也不允许发出第 3 次 POST。
_WRITE_FIRE_COUNT = 0


class ProbeWriteRefused(Exception):
    """journal 或单进程计数显示不得再写（防重复的强制落点）。"""


class ProbeClosedError(Exception):
    """fail-closed 拒绝：目标非精确 loopback / preflight 非空 / state 损坏等。"""


@dataclass(frozen=True, slots=True)
class _PostResult:
    status: int | None
    content_type: str | None = None
    body: bytes | None = None
    truncated: bool = False

    @property
    def decoded(self) -> str:
        if self.body is None:
            return ""
        return self.body.decode("utf-8", "replace")


# --------------------------------------------------------------------------- fail-closed 目标校验

def normalize_base_url(base_url: str) -> str | None:
    """只接受 http + 127.0.0.1 + 57123 + 无子路径的精确目标；其余返回 None（fail-closed）。"""
    u = urllib.parse.urlsplit((base_url or "").strip().rstrip("/"))
    host = (u.hostname or "").lower()
    if u.scheme not in ("http",):
        return None
    if host != "127.0.0.1":
        return None
    if u.port not in (None, 57123):
        return None
    if u.path not in ("", "/"):
        return None
    return f"{u.scheme}://{host}:57123"


def base_url_allowed(base_url: str) -> bool:
    # 精确值语义：与规范 URL 逐字相等才放行；任何多余字符/斜杠/参数一律拒绝。
    return (base_url or "").strip() == ALLOWED_BASE_URL


# --------------------------------------------------------------------------- 随机身份 / 消息格式

def make_run_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"t21_{stamp}_{secrets.token_hex(4)}"


def make_message_id() -> str:
    # 与本地 SDK 的 ra("msg") 生成器同族：以 "msg" 开头 + 十六进制串；
    # 本地 schema 只证明约束为"必须以 msg 开头"。
    return f"{MSG_ID_PREFIX}{secrets.token_hex(12)}"


def make_run_nonce() -> str:
    return "b" + secrets.token_hex(8)


def marker_for(nonce: str) -> str:
    return f"{MARKER_PREFIX}{nonce}"


def synthetic_prompt(nonce: str) -> str:
    return (
        "这是 AI Relay B T21 写通路探针。\n"
        "不要调用任何工具。\n"
        "不要读取、创建、修改或删除任何文件。\n"
        "不要运行命令。\n"
        "只回复下面这一行，除此之外不要输出其他内容：\n"
        "\n"
        f"{marker_for(nonce)}"
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def redact_id(full: str | None) -> str | None:
    """真实 ID 只允许出现前缀 + 摘要，绝不出现完整值。"""
    if not full:
        return None
    return f"{full[:8]}#{sha256_hex(full)[:16]}"


_ID_TOKEN_RE = re.compile(r"(?i)\b((?:ses_|sess_|msg_)[a-z0-9_]+)")


def scrub_ids(value: Any) -> Any:
    """递归把疑似真实 session/message ID 字符串替换为脱敏形式（证据防御）。"""
    if isinstance(value, dict):
        return {str(k): scrub_ids(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_ids(v) for v in value]
    if isinstance(value, str):

        def _sub(m: re.Match) -> str:
            full = m.group(1)
            return redact_id(full) or full
        return _ID_TOKEN_RE.sub(_sub, value)
    return value


# --------------------------------------------------------------------------- durable probe journal

def default_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_state_path(repo_root: Path | None = None) -> Path:
    return (repo_root or default_repo_root()) / ".recovery" / "t21_write_smoke_state.json"


def default_probe_root(repo_root: Path | None = None) -> Path:
    return (repo_root or default_repo_root()) / ".recovery" / "t21_write_smoke"


def initial_state(run_id: str, probe_directory: str, *, nonce: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "probe_directory": probe_directory,
        "run_nonce": nonce,
        "phase": "PREPARED",
        "create_attempt_count": 0,
        "send_attempt_count": 0,
        "create_attempted_at": None,
        "session_confirmed_at": None,
        "send_attempted_at": None,
        "send_http_status": None,
        "send_accepted_at": None,
        "first_result_observed_at": None,
        "completion_at": None,
        "explicit_session_id": None,
        "explicit_probe_message_id": None,
        "completion_verdict": None,
        "reconciliation_attempted_at": None,
        "reconciliation_result": None,
        "recovered_from_unknown_create": False,
    }


def _atomic_write_json(path: Path, obj: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def load_state(path: Path) -> dict[str, Any] | None:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ProbeClosedError(f"state json 损坏: {p} ({exc})") from exc
    if not isinstance(data, dict):
        raise ProbeClosedError(f"state json 非对象: {p}")
    return data


def journal(state_path: Path, state: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """把所有写意图先原子落盘：任何 HTTP POST 之前都必须经过此函数。"""
    merged = dict(state)
    merged.update(changes)
    _atomic_write_json(state_path, merged)
    return merged


# --------------------------------------------------------------------------- 私有单次 POST（唯一写通道）

def _read_bounded(raw, max_bytes: int) -> tuple[bytes, bool]:
    try:
        chunk = raw.read(max_bytes + 1)
    except (OSError, ValueError):
        return b"", True
    return chunk[:max_bytes], len(chunk) > max_bytes


def _content_type_of(raw) -> str | None:
    try:
        value = raw.headers.get("Content-Type") or raw.headers.get("content-type")
    except AttributeError:
        value = getattr(raw, "content_type", None)
    return str(value) if value else None


def _post_json(
    base_url: str,
    path: str,
    *,
    params: dict[str, str] | None,
    body: dict[str, Any] | None,
    token: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> _PostResult:
    """私有 POST 单发函数：只被 `_create_probe_session_once` / `_send_probe_prompt_once` 调用。

    非 loopback 时即使有 token 也绝不附加（与只读 seam 同一策略）。
    进程内硬上限 MAX_WRITE_POSTS：只允许 2 次 POST，第 3 次直接拒绝。
    """
    global _WRITE_FIRE_COUNT
    _WRITE_FIRE_COUNT += 1
    if _WRITE_FIRE_COUNT > MAX_WRITE_POSTS:
        _WRITE_FIRE_COUNT -= 1
        raise ProbeWriteRefused(
            f"进程内 POST 已到上限 {MAX_WRITE_POSTS}：REFUSE NEW WRITES (count={_WRITE_FIRE_COUNT})"
        )
    url = base_url + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Content-Type": "application/json"}
    if token and is_loopback_base_url(base_url):
        headers["Authorization"] = f"Bearer {token}"
    payload = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(url, method="POST", headers=headers, data=payload)
    try:
        raw = urllib.request.urlopen(req, timeout=timeout)
        body_bytes, truncated = _read_bounded(raw, MAX_BODY_BYTES)
        status = getattr(raw, "status", None)
        if status is None:
            getcode = getattr(raw, "getcode", None)
            status = getcode() if callable(getcode) else 0
        return _PostResult(status=int(status),
                           content_type=_content_type_of(raw),
                           body=body_bytes, truncated=truncated)
    except urllib.error.HTTPError as exc:
        body_bytes, truncated = _read_bounded(exc, MAX_BODY_BYTES)
        return _PostResult(status=exc.code, content_type=_content_type_of(exc),
                           body=body_bytes, truncated=truncated)
    except socket.timeout as exc:
        raise _ProbeTransportError("TIMEOUT", f"{type(exc).__name__} POST 超时") from exc
    except urllib.error.URLError as exc:
        raise _ProbeTransportError("CONNECTION", f"{type(exc).__name__}: {exc.reason}") from exc
    except OSError as exc:
        raise _ProbeTransportError("CONNECTION", f"{type(exc).__name__}: {exc}") from exc


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


# --------------------------------------------------------------------------- WRITE #1：创建 disposable session

def _create_probe_session_once(
    transport: Any,
    directory: str,
    state_path: Path,
    *,
    token: str | None = None,
    title: str = "AI Relay B T21 write smoke (disposable)",
    timeout: float = 20.0,
    base_url: str = ALLOWED_BASE_URL,
    now_fn=None,
) -> dict[str, Any]:
    """锁死内部 endpoint 的 create 单发函数：本函数内有且仅有一次 POST /api/session。

    - 调用前先 journal phase=CREATE_ATTEMPTED / create_attempt_count=1；
    - 任何情况下都不自动重发；UNKNOWN_CREATE 只能由调用方走只读 reconciliation；
    - body 只使用本地源码明确证明的最小字段（SDK 的 create body key 中 title 为可选
      顶层 key，本地桌面 SDK 真实 create 也只发 parentID/title/metadata + directory query）。
    """
    if not base_url_allowed(base_url):
        raise ProbeClosedError(f"armed write 拒绝：base_url={base_url!r} 非精确 {ALLOWED_BASE_URL}")
    state = load_state(state_path)
    if state is None:
        raise ProbeClosedError("state 不存在：必须先完成 PREPARED 初始化")
    if state.get("create_attempt_count", 0) > 0:
        raise ProbeWriteRefused(f"create_attempt_count={state['create_attempt_count']}>0：REFUSE NEW WRITES")
    if state.get("phase") not in ("PREPARED", "SESSION_CREATED"):
        raise ProbeWriteRefused(f"phase={state['phase']} 不允许再发 create POST")

    now = (now_fn() if now_fn else _now_iso())
    state = journal(state_path, state, phase="CREATE_ATTEMPTED",
                    create_attempt_count=1, create_attempted_at=now)

    body: dict[str, Any] = {"title": title}
    params: dict[str, str] = {"directory": directory}
    try:
        result = _post_json(base_url, CREATE_PATH, params=params, body=body,
                            token=token, timeout=timeout)
    except _ProbeTransportError as exc:
        journal(state_path, state, phase="UNKNOWN_CREATE")
        return {"phase": "UNKNOWN_CREATE", "session_id": None,
                "http_status": None, "kind": "TRANSPORT_EXC", "detail": str(exc)}

    outcome: dict[str, Any] = {
        "phase": None,
        "session_id": None,
        "http_status": result.status,
        "kind": None,
        "detail": None,
    }
    if result.status is not None and 200 <= result.status < 300:
        parsed: Any = None
        text = result.decoded
        if text.strip():
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
        sid = None
        if isinstance(parsed, dict):
            sid = parsed.get("id")
        if isinstance(sid, str) and is_valid_session_id(sid):
            outcome["phase"] = "SESSION_CREATED"
            outcome["session_id"] = sid
            outcome["kind"] = "CONFIRMED"
            state = journal(state_path, state, phase="SESSION_CREATED",
                            session_confirmed_at=(now_fn() if now_fn else _now_iso()),
                            explicit_session_id=sid)
        else:
            outcome["phase"] = "UNKNOWN_CREATE"
            outcome["kind"] = "NO_UNIQUE_ID"
            outcome["detail"] = f"2xx 但无合法 {ACCEPTED_SESSION_ID_PREFIXES} 前缀 id (body={result.decoded[:200]!r})"
            state = journal(state_path, state, phase="UNKNOWN_CREATE")
    elif result.status is not None and result.status >= 400:
        outcome["phase"] = "FAILED"
        outcome["kind"] = "REJECTED"
        outcome["detail"] = f"HTTP {result.status}: {result.decoded[:200]!r}"
        state = journal(state_path, state, phase="FAILED")
    else:
        outcome["phase"] = "UNKNOWN_CREATE"
        outcome["kind"] = "NO_STATUS"
    return outcome


# --------------------------------------------------------------------------- WRITE #2：发送 synthetic prompt

def _send_probe_prompt_once(
    transport: Any,
    session_id: str,
    directory: str,
    message_id: str,
    prompt_text: str,
    state_path: Path,
    *,
    token: str | None = None,
    agent: str = "build",
    provider_id: str = "opencode",
    model_id: str = "big-pickle",
    variant: str = "default",
    timeout: float = 20.0,
    base_url: str = ALLOWED_BASE_URL,
    now_fn=None,
) -> dict[str, Any]:
    """锁死内部 endpoint 的 prompt_async 单发函数：本函数内有且仅有一次 POST prompt_async。

    - body 只使用本地源码明确证明的字段（SDK promptAsync 的 body key +
      FE 真实发送形状：model 为 {providerID, modelID}、agent 字符串、variant 字符串、
      parts [{type:"text", text}]，messageID 必须 msg 前缀；directory 走 query；
      delivery 不在当前 SDK 的 promptAsync body key 集内，不发送）。
    - 204 = request accepted != model execution completed；绝不因 204 判完成。
    """
    if not base_url_allowed(base_url):
        raise ProbeClosedError(f"armed write 拒绝：base_url={base_url!r} 非精确 {ALLOWED_BASE_URL}")
    state = load_state(state_path)
    if state is None:
        raise ProbeClosedError("state 不存在：必须先完成 create")
    if state.get("phase") != "SESSION_CREATED":
        raise ProbeWriteRefused(f"phase={state.get('phase')} != SESSION_CREATED：拒绝 prompt POST")
    if state.get("send_attempt_count", 0) > 0:
        raise ProbeWriteRefused(f"send_attempt_count={state['send_attempt_count']}>0：REFUSE NEW WRITES")

    now = (now_fn() if now_fn else _now_iso())
    state = journal(state_path, state, phase="SEND_ATTEMPTED",
                    send_attempt_count=1, send_attempted_at=now,
                    explicit_probe_message_id=message_id)

    path = PROMPT_ASYNC_PATH_TMPL.format(sid=urllib.parse.quote(session_id, safe=""))
    body: dict[str, Any] = {
        "messageID": message_id,
        "model": {"providerID": provider_id, "modelID": model_id},
        "agent": agent,
        "variant": variant,
        "parts": [{"type": "text", "text": prompt_text}],
    }
    params: dict[str, str] = {"directory": directory}
    try:
        result = _post_json(base_url, path, params=params, body=body,
                            token=token, timeout=timeout)
    except _ProbeTransportError as exc:
        journal(state_path, state, phase="UNKNOWN_SEND")
        return {"phase": "UNKNOWN_SEND", "http_status": None,
                "kind": "TRANSPORT_EXC", "detail": str(exc)}

    outcome: dict[str, Any] = {
        "phase": None,
        "http_status": result.status,
        "kind": None,
        "detail": None,
    }
    if result.status is not None and 200 <= result.status < 300:
        outcome["phase"] = "SEND_ACCEPTED"
        outcome["kind"] = "ACCEPTED"
        state = journal(state_path, state, phase="SEND_ACCEPTED",
                        send_http_status=result.status,
                        send_accepted_at=(now_fn() if now_fn else _now_iso()))
    elif result.status is not None and result.status >= 400:
        outcome["phase"] = "FAILED"
        outcome["kind"] = "REJECTED"
        outcome["detail"] = f"HTTP {result.status}: {result.decoded[:200]!r}"
        state = journal(state_path, state, phase="FAILED",
                        send_http_status=result.status)
    else:
        outcome["phase"] = "UNKNOWN_SEND"
        outcome["kind"] = "NO_STATUS"
    return outcome


# --------------------------------------------------------------------------- 只读 reconciliation / 观察

def session_list(transport: Any, directory: str) -> list[dict[str, Any]]:
    encoded = urllib.parse.urlencode({"directory": directory})
    raw = transport.request("GET", f"/api/session?{encoded}", attach_auth=True)
    if not (200 <= raw.status < 300):
        raise ProbeClosedError(f"GET /api/session 非 2xx: {raw.status}")
    try:
        parsed = json.loads(raw.decoded)
    except ValueError as exc:
        raise ProbeClosedError(f"GET /api/session 非 JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ProbeClosedError("GET /api/session 返回非数组")
    return parsed


def session_id_prefix_of(value: str) -> str | None:
    """返回匹配到的合法前缀（sess_ / ses_），否则 None。"""
    for prefix in ACCEPTED_SESSION_ID_PREFIXES:
        if value.startswith(prefix):
            return prefix
    return None


def is_valid_session_id(value: Any) -> bool:
    """合法 session id：字符串 + 合法前缀 + 前缀后仍有非空内容（无前缀尾 = malformed）。"""
    if not isinstance(value, str):
        return False
    prefix = session_id_prefix_of(value)
    return prefix is not None and len(value) > len(prefix)


def reconcile_sessions(transport: Any, directory: str) -> list[str]:
    """只读 reconciliation：GET-only，绝不产生写。返回该 directory 下全部【合法】session id。"""
    return [s["id"] for s in session_list(transport, directory)
            if is_valid_session_id(s.get("id"))]


def reconcile_exactly_one(transport: Any, directory: str) -> dict[str, Any]:
    """UNKNOWN_CREATE 恢复的决定性判定（三条件同立）：

    1) GET /api/session?directory=<existing_probe_directory> 恰好 1 条会话；
    2) 该会话含非空合法 session id（sess_ / ses_）；
    否则（0 个 / >1 个 / 缺 id / id malformed）一律 declined，绝不发起写。
    """
    all_sessions = session_list(transport, directory)
    valid_ids = [s["id"] for s in all_sessions if is_valid_session_id(s.get("id"))]
    if len(all_sessions) == 1 and len(valid_ids) == 1:
        return {"recovered": True, "session_id": valid_ids[0],
                "directory_count": 1, "reason": None}
    if len(all_sessions) != 1:
        reason = "ZERO" if len(all_sessions) == 0 else "MULTIPLE"
        return {"recovered": False, "session_id": None,
                "directory_count": len(all_sessions), "reason": reason}
    return {"recovered": False, "session_id": None,
            "directory_count": 1, "reason": "NO_VALID_ID"}


def fetch_messages(transport: Any, session_id: str, directory: str) -> list[dict[str, Any]]:
    encoded = urllib.parse.urlencode({"directory": directory})
    sid = urllib.parse.quote(session_id, safe="")
    raw = transport.request("GET", f"/api/session/{sid}/message?{encoded}", attach_auth=True)
    if not (200 <= raw.status < 300):
        raise ProbeClosedError(f"GET messages 非 2xx: {raw.status}")
    try:
        parsed = json.loads(raw.decoded)
    except ValueError as exc:
        raise ProbeClosedError(f"GET messages 非 JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ProbeClosedError("GET messages 返回非数组")
    return parsed


def message_text(msg: dict[str, Any]) -> str:
    """统一提取文本：优先 parts[].text（type=text），降级顶层 text。"""
    out: list[str] = []
    parts = msg.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                out.append(part["text"])
    if not out and isinstance(msg.get("text"), str):
        out.append(msg["text"])
    return "".join(out)


def _msg_field(msg: dict[str, Any], key: str) -> Any:
    """消息身份字段兼容两种形状：
    - 文档/本地源形状：{"id", "role", "parentID", "parts"}（扁平）；
    - 真实运行时形状：{"info": {"id", "role", "parentID", ...}, "parts"}（嵌套）。
    """
    info = msg.get("info")
    if isinstance(info, dict) and key in info:
        return info.get(key)
    return msg.get(key)


def message_id_of(msg: dict[str, Any]) -> Any:
    return _msg_field(msg, "id")


def message_role(msg: dict[str, Any]) -> Any:
    return _msg_field(msg, "role")


def message_parent_of(msg: dict[str, Any]) -> Any:
    return _msg_field(msg, "parentID")


def evaluate_completion(
    messages: list[dict[str, Any]], probe_message_id: str, marker: str
) -> dict[str, Any]:
    """完成判定纯函数：四条件（A/B/C/D）同立才算 completed。

    D 只接受显式 parent/causal：assistant.parentID == probe_message_id。
    禁止"最后一条 assistant 就算"；若 marker 文本出现在 assistant 但无 parent 关系，
    一律 ATTRIBUTION_AMBIGUOUS，不得 completed。
    """
    user_matches = [m for m in messages
                    if isinstance(message_id_of(m), str) and message_id_of(m) == probe_message_id]
    assistant_with_parent = [
        m for m in messages
        if message_role(m) == "assistant"
        and isinstance(message_parent_of(m), str)
        and message_parent_of(m) == probe_message_id
    ]
    assistant_text_any = [
        m for m in messages
        if message_role(m) == "assistant" and message_text(m).strip() == marker
    ]
    attrs: list[dict[str, Any]] = []

    cond_a = len(user_matches) == 1
    cond_b = len(assistant_with_parent) >= 1
    cond_c = any(message_text(m).strip() == marker for m in assistant_with_parent)
    cond_d_parent = cond_b

    if cond_a and cond_b and cond_c:
        attribution = "VALID"
        completed = True
        attrs = [
            {
                "message_id": redact_id(m.get("id")),
                "role": m.get("role"),
                "text_ok": message_text(m).strip() == marker,
            }
            for m in assistant_with_parent
        ]
    elif assistant_text_any and not cond_b:
        # marker 出现在 assistant，但没有任何 assistant 挂在 probe user 之下：
        # 无法建立显式 parent/causal 关系 -> ATTRIBUTION_AMBIGUOUS。
        attribution = "AMBIGUOUS"
        completed = False
    elif cond_b:
        # 有显式 parent 关系的 assistant（归属可辨），但文本不符/尚未完成。
        attribution = "VALID"
        completed = False
    else:
        attribution = "UNVERIFIED"
        completed = False

    return {
        "conditions": {
            "A_user_identity_located": cond_a,
            "B_assistant_role": cond_b,
            "C_exact_marker": cond_c,
            "D_explicit_parent_relation": cond_d_parent,
        },
        "attribution": attribution,
        "completed": completed,
        "user_match_count": len(user_matches),
        "assistant_parent_match_count": len(assistant_with_parent),
        "assistant_text_match_any": len(assistant_text_any) > 0,
        "message_count": len(messages),
        "attributed_assistants": attrs,
    }


def observe_completion(
    transport: Any,
    session_id: str,
    directory: str,
    probe_message_id: str,
    marker: str,
    *,
    max_wait: float = 90.0,
    interval: float = 3.0,
    sleep_fn=time.sleep,
    clock_fn=time.monotonic,
    now_fn=None,
) -> dict[str, Any]:
    """只读完成观察：只允许 GET，poll interval>=1s、total<=max_wait。"""
    interval = max(1.0, interval)
    deadline = clock_fn() + max_wait
    polls: list[dict[str, Any]] = []
    first_result_observed_at = None
    completion_at = None
    verdict: dict[str, Any] | None = None
    last_messages: list[dict[str, Any]] = []

    while True:
        msgs = fetch_messages(transport, session_id, directory)
        last_messages = msgs
        verdict = evaluate_completion(msgs, probe_message_id, marker)
        now = now_fn() if now_fn else _now_iso()
        if verdict["assistant_parent_match_count"] > 0 and first_result_observed_at is None:
            first_result_observed_at = now
        polls.append({"t": now, "verdict_summary": {
            "completed": verdict["completed"],
            "conditions": verdict["conditions"],
            "attribution": verdict["attribution"],
            "assistant_parent_match_count": verdict["assistant_parent_match_count"],
        }})
        if verdict["completed"]:
            completion_at = now
            break
        if clock_fn() >= deadline:
            break
        sleep_fn(interval)

    return {
        "polls": polls,
        "first_result_observed_at": first_result_observed_at,
        "completion_at": completion_at,
        "verdict": verdict,
        "last_messages_redacted": [{
            "id": redact_id(message_id_of(m)),
            "role": message_role(m),
            "parentID": redact_id(message_parent_of(m)),
            "text": message_text(m),
        } for m in last_messages],
    }


# --------------------------------------------------------------------------- WRITE #2R：UNKNOWN_CREATE 恢复 + prompt once

def resume_write_once(
    transport: Any,
    state_path: Path,
    *,
    token: str | None = None,
    timeout: float = 20.0,
    base_url: str = ALLOWED_BASE_URL,
    max_wait: float = 90.0,
    sleep_fn=time.sleep,
    clock_fn=time.monotonic,
    now_fn=None,
) -> dict[str, Any]:
    """T21-02R：UNKNOWN_CREATE -> 只读 reconciliation -> WRITE #2 prompt once -> GET-only 观察。

    硬约束：
    - 仅当 phase==UNKNOWN_CREATE && create_attempt_count==1 && send_attempt_count==0 才进入；
    - 【绝不调用 create】；只可能发出 1 次 prompt POST；
    - reconciliation 决定性条件 = directory 恰好 1 条会话且 id 合法（sess_ / ses_）；
    - 真实 session id 只写私有 journal；返回/公开面只给 redacted 依据。
    """
    if not base_url_allowed(base_url):
        raise ProbeClosedError(f"resume 拒绝：base_url={base_url!r} 非精确 {ALLOWED_BASE_URL}")
    state = load_state(state_path)
    if state is None:
        raise ProbeClosedError("state 不存在：无 journal 可恢复")
    if not (state.get("phase") == "UNKNOWN_CREATE"
            and state.get("create_attempt_count") == 1
            and state.get("send_attempt_count") == 0):
        raise ProbeWriteRefused(
            f"resume 前置不满足：phase={state.get('phase')!r} "
            f"create_attempt_count={state.get('create_attempt_count')} "
            f"send_attempt_count={state.get('send_attempt_count')}"
        )
    directory = state.get("probe_directory")
    run_nonce = str(state.get("run_nonce") or "")
    now = (now_fn() if now_fn else _now_iso())
    state = journal(state_path, state, reconciliation_attempted_at=now,
                    reconciliation_result="PENDING",
                    recovered_from_unknown_create=False)

    rec = reconcile_exactly_one(transport, directory)
    if not rec["recovered"]:
        state = journal(state_path, state, reconciliation_result=f"DECLINED_{rec['reason']}")
        return {"phase": "UNKNOWN_CREATE", "reconciliation": rec,
                "send_outcome": None, "observation": None,
                "declined": True,
                "reason": f"directory 恰 1 条且 id 合法才可恢复，实际：{rec['reason']}"}

    sid = rec["session_id"]
    state = journal(state_path, state,
                    reconciliation_result="SINGLE_RECOVERED",
                    recovered_from_unknown_create=True,
                    phase="SESSION_CREATED",
                    explicit_session_id=sid,
                    session_confirmed_at=(now_fn() if now_fn else _now_iso()))

    message_id = make_message_id()
    prompt_text = synthetic_prompt(run_nonce)
    send_out = _send_probe_prompt_once(
        transport, sid, directory, message_id, prompt_text, state_path,
        token=token, timeout=timeout, base_url=base_url, now_fn=now_fn,
    )
    outcome: dict[str, Any] = {
        "phase": send_out["phase"], "reconciliation": rec,
        "send_outcome": send_out, "observation": None,
        "declined": False,
    }
    state = load_state(state_path) or state
    if send_out["phase"] == "SEND_ACCEPTED":
        observation = observe_completion(
            transport, sid, directory, message_id, marker_for(run_nonce),
            max_wait=max_wait, sleep_fn=sleep_fn, clock_fn=clock_fn, now_fn=now_fn,
        )
        outcome["observation"] = observation
        outcome["message_id_redacted"] = redact_id(message_id)
        state = journal(state_path, state,
                        first_result_observed_at=observation.get("first_result_observed_at"),
                        completion_at=observation.get("completion_at"),
                        completion_verdict=observation.get("verdict"),
                        phase="RESULT_OBSERVED")
        outcome["phase"] = "RESULT_OBSERVED"
    elif send_out["phase"] == "UNKNOWN_SEND":
        outcome["phase"] = "UNKNOWN_SEND"
    elif send_out["phase"] == "FAILED":
        outcome["phase"] = "FAILED"
    return outcome


# --------------------------------------------------------------------------- 七层证据

def build_evidence(
    *,
    run_id: str,
    probe_directory: str,
    base_url: str,
    phase: str,
    session_id: str | None,
    session_confirmed_at: str | None,
    create_attempted_at: str | None,
    send_attempted_at: str | None,
    send_http_status: int | None,
    send_accepted_at: str | None,
    first_result_observed_at: str | None,
    completion_at: str | None,
    observation: dict[str, Any] | None,
    create_attempt_count: int,
    send_attempt_count: int,
    marker: str,
    run_nonce: str,
    service_reachable: bool,
    api_authenticated: bool,
    recovery_history: dict[str, Any] | None = None,
    extra_notes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """组装脱敏证据：完整 session/message ID 一律禁止落入本文件。"""
    completed = bool(observation and observation.get("verdict", {}).get("completed"))
    executing = bool(first_result_observed_at) or completed
    verdict = (observation or {}).get("verdict") or {
        "attribution": "UNVERIFIED", "completed": False,
    }
    return {
        "task_id": "5b28ce68-d741-4720-8ed2-013f129f2102",
        "round": "T21-02-B",
        "run_id": run_id,
        "generated_at": _now_iso(),
        "transport": {
            "base_url": base_url,
            "loopback_target": is_loopback_base_url(base_url),
        },
        "probe": {
            "probe_directory": probe_directory,
            "session_id_redacted": redact_id(session_id),
            "session_id_hash": sha256_hex(session_id or ""),
            "marker": marker,
            "marker_hash": sha256_hex(marker),
            "run_nonce_hash": sha256_hex(run_nonce),
        },
        "phase": phase,
        "recovery_history": scrub_ids(recovery_history) if recovery_history else {},
        "write_counters": {
            "create_post_count": create_attempt_count,
            "prompt_post_count": send_attempt_count,
            "other_write_count": 0,
            "create_retries": max(0, create_attempt_count - 1),
            "send_retries": max(0, send_attempt_count - 1),
        },
        "seven_layers": {
            "service_reachable": service_reachable,
            "api_authenticated": api_authenticated,
            "capability_available": {
                "session_create": "OBSERVED" if session_id else ("REJECTED" if phase == "FAILED" else "UNVERIFIED"),
                "prompt_async": "OBSERVED_ACCEPT" if send_http_status and 200 <= send_http_status < 300 else "UNVERIFIED",
            },
            "session_exists": bool(session_id),
            "session_attribution_valid": verdict.get("attribution", "UNVERIFIED"),
            "execute_accepted": bool(send_http_status and 200 <= send_http_status < 300),
            "execution_progressing": executing,
        },
        "data_model": {
            "execute_accepted": bool(send_http_status and 200 <= send_http_status < 300),
            "completed_and_attributed": completed,
            "note_204_is_not_completed": (
                "POST 204 / 2xx 仅表示请求被接纳（request accepted），"
                "即使首轮 GET 已见结果，也不改变'204 本身不含 completion result'的事实"
            ),
        },
        "timeline": {
            "create_attempted_at": create_attempted_at,
            "session_confirmed_at": session_confirmed_at,
            "send_attempted_at": send_attempted_at,
            "send_http_status": send_http_status,
            "send_accepted_at": send_accepted_at,
            "first_result_observed_at": first_result_observed_at,
            "completion_at": completion_at,
        },
        "completion": {
            "completed": completed,
            "attribution": verdict.get("attribution", "UNVERIFIED"),
            "conditions": verdict.get("conditions"),
            "explicit_parent_relation_used": True,
            "last_message_heuristic_used": False,
        },
        "poll_summary": {
            "poll_count": len((observation or {}).get("polls", [])),
            "last_poll": ((observation or {}).get("polls") or [None])[-1],
        },
        "notes": {
            "keep": "disposable session 留在 .recovery probe directory，本轮不做服务端 cleanup 写",
            "cleanup_write_capability": False,
            **scrub_ids(extra_notes or {}),
        },
    }


# --------------------------------------------------------------------------- CLI

def _default_evidence_path(repo_root: Path | None = None) -> Path:
    return (repo_root or default_repo_root()) / ".recovery" / "t21_write_smoke_evidence.json"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="probe_openchamber_write_smoke",
        description="T21-02 disposable session 单次真实写冒烟（严格 2 POST，dry-run/arm-once）",
    )
    parser.add_argument("--base-url", default=ALLOWED_BASE_URL, help="真实写目标（必须精确 loopback）")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--state", default=None, help="durable journal 路径")
    parser.add_argument("--probe-root", default=None, help="probe directory 根目录")
    parser.add_argument("--out", default=None, help="evidence 输出路径")
    parser.add_argument("--max-wait", type=float, default=90.0, help="完成观察总时长上限（秒）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只打印执行计划，绝不发 POST")
    mode.add_argument("--arm-once", action="store_true", help="执行严格 2 次真实 POST")
    mode.add_argument("--resume-once", action="store_true",
                      help="UNKNOWN_CREATE 只读恢复 + 唯一 1 次 prompt POST（绝不 create）")
    mode.add_argument("--observe-only", action="store_true",
                      help="POST 之后只读完成观察 + 终版证据（绝不发 POST）")
    return parser.parse_args(argv)


def _plan(run_id: str, probe_directory: str, message_id: str, nonce: str) -> dict[str, Any]:
    return {
        "mode": "dry-run",
        "run_id": run_id,
        "run_nonce": nonce,
        "probe_directory": probe_directory,
        "target_base": ALLOWED_BASE_URL,
        "base_url_allowed": base_url_allowed(ALLOWED_BASE_URL),
        "create_path": f"POST {CREATE_PATH}?directory=<probe_directory>",
        "prompt_path_template": "POST /api/session/{sid}/prompt_async?directory=<probe_directory>",
        "create_body_keys": ["title"],
        "prompt_body_keys": ["messageID", "model", "agent", "variant", "parts"],
        "message_id_prefix": MSG_ID_PREFIX,
        "message_id_shape": "msg_<hex>（本地 schema 只证明'必须以 msg 开头'）",
        "parts_format": [{"type": "text", "text": "<synthetic prompt>"}],
        "agent_or_model_required_on_create": False,
        "delivery_optional": "不在当前 SDK promptAsync body key 集内，本轮不发送",
        "synthetic_marker_hash": sha256_hex(marker_for(nonce)),
        "maximum_writes": MAX_WRITE_POSTS,
        "token": "<not-printed>",
    }


def _main_resume(args: argparse.Namespace, state_path: Path, out_path: Path) -> int:
    """--resume-once：UNKNOWN_CREATE 只读恢复 + 唯一 1 次 prompt POST。绝不调用 create。"""
    if not base_url_allowed(args.base_url):
        print(f"RESUME 拒绝：base_url={args.base_url!r} != {ALLOWED_BASE_URL}", file=sys.stderr)
        return 2
    existing = load_state(state_path)
    if existing is None:
        print("RESUME 拒绝：无 durable journal。", file=sys.stderr)
        return 3
    if not (existing.get("phase") == "UNKNOWN_CREATE"
            and existing.get("create_attempt_count") == 1
            and existing.get("send_attempt_count") == 0):
        print("RESUME 拒绝：前置不满足（需 phase==UNKNOWN_CREATE && "
              "create_attempt_count==1 && send_attempt_count==0）。", file=sys.stderr)
        print(json.dumps({"run_id": existing.get("run_id"), "phase": existing.get("phase"),
                          "create_attempt_count": existing.get("create_attempt_count"),
                          "send_attempt_count": existing.get("send_attempt_count")},
                         ensure_ascii=False))
        return 3

    token = resolve_auth_token(
        env_token=os.environ.get(CLIENT_TOKEN_ENV),
        settings_path=default_settings_path(),
        attach_allowed=is_loopback_base_url(args.base_url),
    )
    transport = _ProbeTransport(args.base_url, token=token, timeout=args.timeout)
    out = resume_write_once(transport, state_path, token=token, timeout=args.timeout,
                            base_url=args.base_url, max_wait=args.max_wait)
    state = load_state(state_path) or {}
    rec = out["reconciliation"]
    send_out = out.get("send_outcome")
    obs = out.get("observation")

    if out.get("declined"):
        print(f"reconciliation 未恢复（STOP，不发 POST）：{out['reason']}", file=sys.stderr)
        print(json.dumps({"directory_count": rec["directory_count"],
                          "recovered": False, "reason": rec["reason"]}))
        return 7
    sid = rec["session_id"]
    print(f"reconciliation: directory_count={rec['directory_count']} "
          f"recovered={rec['recovered']} session_id={redact_id(sid)}")
    print(f"send: phase={send_out['phase']} http={send_out['http_status']}")
    if obs is not None:
        print(f"completion: completed={obs['verdict']['completed']} "
              f"attribution={obs['verdict']['attribution']}")

    service_reachable, api_authenticated = _service_readiness(transport)

    evidence = _compose_resume_evidence(args, state, sid=sid, obs=obs,
                                        send_http_status=(send_out or {}).get("http_status"),
                                        reconciliation_count=rec["directory_count"],
                                        recovered=rec["recovered"],
                                        phase=state.get("phase") or out["phase"],
                                        service_reachable=service_reachable,
                                        api_authenticated=api_authenticated)
    _atomic_write_json(out_path, evidence)
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _compose_resume_evidence(
    args: argparse.Namespace,
    state: dict[str, Any],
    *,
    sid: str | None,
    obs: dict[str, Any] | None,
    send_http_status: int | None,
    reconciliation_count: int,
    recovered: bool,
    phase: str,
    service_reachable: bool,
    api_authenticated: bool,
) -> dict[str, Any]:
    """统一组装 T21-02R 证据（历史保留 + 脱敏）。绝不包含完整真实 id。"""
    recovery_history = {
        "initial_create_post": 1,
        "initial_create_http": 200,
        "initial_classification": "UNKNOWN_CREATE",
        "reason": "local prefix contract mismatch (probe expected sess_; runtime session id prefix is ses_)",
        "reconciliation_get_directory_count": reconciliation_count,
        "recovered_session": "YES" if recovered else "NO",
        "prompt_post": int(state.get("send_attempt_count") or 0),
        "other_writes": 0,
        "retries": 0,
    }
    return build_evidence(
        run_id=state.get("run_id") or "",
        probe_directory=state.get("probe_directory") or "",
        base_url=args.base_url,
        phase=phase,
        session_id=sid,
        session_confirmed_at=state.get("session_confirmed_at"),
        create_attempted_at=state.get("create_attempted_at"),
        send_attempted_at=state.get("send_attempted_at"),
        send_http_status=send_http_status,
        send_accepted_at=state.get("send_accepted_at"),
        first_result_observed_at=state.get("first_result_observed_at"),
        completion_at=state.get("completion_at"),
        observation=obs,
        create_attempt_count=int(state.get("create_attempt_count") or 1),
        send_attempt_count=int(state.get("send_attempt_count") or 0),
        marker=marker_for(str(state.get("run_nonce") or "")),
        run_nonce=str(state.get("run_nonce") or ""),
        service_reachable=service_reachable,
        api_authenticated=api_authenticated,
        recovery_history=recovery_history,
        extra_notes={
            "disposable_session_left_for_audit": "YES",
            "cleanup_write_capability": False,
        },
    )


def _main_observe_only(args: argparse.Namespace, state_path: Path, out_path: Path) -> int:
    """--observe-only：POST 之后的只读完成观察 + 终版证据。绝不发任何 POST。"""
    if not base_url_allowed(args.base_url):
        print(f"OBSERVE 拒绝：base_url={args.base_url!r} != {ALLOWED_BASE_URL}", file=sys.stderr)
        return 2
    state = load_state(state_path)
    if state is None:
        print("OBSERVE 拒绝：无 durable journal。", file=sys.stderr)
        return 3
    if state.get("send_attempt_count", 0) < 1:
        print("OBSERVE 拒绝：尚无 prompt POST（send_attempt_count<1）。", file=sys.stderr)
        return 3
    sid = state.get("explicit_session_id")
    mid = state.get("explicit_probe_message_id")
    if not sid or not mid:
        print("OBSERVE 拒绝：journal 缺 explicit_session_id / explicit_probe_message_id。",
              file=sys.stderr)
        return 3

    token = resolve_auth_token(
        env_token=os.environ.get(CLIENT_TOKEN_ENV),
        settings_path=default_settings_path(),
        attach_allowed=is_loopback_base_url(args.base_url),
    )
    transport = _ProbeTransport(args.base_url, token=token, timeout=args.timeout)
    obs = observe_completion(
        transport, sid, state["probe_directory"], mid,
        marker_for(str(state.get("run_nonce") or "")), max_wait=args.max_wait,
    )
    state = load_state(state_path) or state
    state = journal(state_path, state,
                    first_result_observed_at=obs.get("first_result_observed_at"),
                    completion_at=obs.get("completion_at"),
                    completion_verdict=obs.get("verdict"),
                    phase="RESULT_OBSERVED")

    print(f"completion: completed={obs['verdict']['completed']} "
          f"attribution={obs['verdict']['attribution']}")
    service_reachable, api_authenticated = _service_readiness(transport)
    evidence = _compose_resume_evidence(args, state, sid=sid, obs=obs,
                                        send_http_status=state.get("send_http_status"),
                                        reconciliation_count=1, recovered=True,
                                        phase="RESULT_OBSERVED",
                                        service_reachable=service_reachable,
                                        api_authenticated=api_authenticated)
    _atomic_write_json(out_path, evidence)
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = default_repo_root()
    state_path = Path(args.state) if args.state else default_state_path(repo_root)
    probe_root = Path(args.probe_root) if args.probe_root else default_probe_root(repo_root)
    out_path = Path(args.out) if args.out else _default_evidence_path(repo_root)

    if args.resume_once:
        return _main_resume(args, state_path, out_path)
    if args.observe_only:
        return _main_observe_only(args, state_path, out_path)

    run_id = make_run_id()
    nonce = make_run_nonce()
    probe_directory = str(probe_root / run_id)
    message_id = make_message_id()
    prompt_text = synthetic_prompt(nonce)

    if args.dry_run or not args.arm_once:
        plan = _plan(run_id, probe_directory, message_id, nonce)
        _atomic_write_json(out_path.with_name("t21_write_smoke_plan.json"), plan)
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        print(f"RUN_NONCE={nonce}")
        return 0

    # ---- arm-once 路径
    if not base_url_allowed(args.base_url):
        print(f"ARM 拒绝：base_url={args.base_url!r} != {ALLOWED_BASE_URL}", file=sys.stderr)
        return 2

    existing = load_state(state_path)
    if existing and existing.get("create_attempt_count", 0) > 0:
        print("REFUSE NEW WRITES：journal 已存在且 create_attempt_count>0，"
              "只允许只读 reconciliation。", file=sys.stderr)
        print(json.dumps({"run_id": existing.get("run_id"), "phase": existing.get("phase"),
                          "create_attempt_count": existing.get("create_attempt_count")},
                         ensure_ascii=False))
        return 3

    token = resolve_auth_token(
        env_token=os.environ.get(CLIENT_TOKEN_ENV),
        settings_path=default_settings_path(),
        attach_allowed=is_loopback_base_url(args.base_url),
    )
    transport = _ProbeTransport(args.base_url, token=token, timeout=args.timeout)

    # 1) probe 隔离：先建本地空目录；创建前 preflight 必须观察到 []
    probe_dir_path = Path(probe_directory)
    probe_dir_path.mkdir(parents=True, exist_ok=True)
    state = initial_state(run_id, probe_directory, nonce=nonce)
    state = journal(state_path, state)

    try:
        pre = session_list(transport, probe_directory)
    except (ProbeClosedError, _ProbeTransportError) as exc:
        print(f"preflight 失败：{exc}", file=sys.stderr)
        journal(state_path, state, phase="FAILED")
        return 4
    if len(pre) != 0:
        print(f"preflight 非空（{len(pre)} 会话）：不 POST，换唯一目录再 preflight。", file=sys.stderr)
        journal(state_path, state, phase="FAILED")
        return 5
    print(f"preflight /api/session?directory=<probe> = [] OK (run_id={run_id})")

    # 3) WRITE #1 create
    create_out = _create_probe_session_once(
        transport, probe_directory, state_path,
        token=token, timeout=args.timeout, base_url=args.base_url,
    )
    print(f"create result: phase={create_out['phase']} http={create_out['http_status']}")
    if create_out["phase"] == "SESSION_CREATED":
        session_id = create_out["session_id"]
        print(f"session confirmed (redacted): {redact_id(session_id)}")
    elif create_out["phase"] == "UNKNOWN_CREATE":
        # 只读 reconciliation：恰好 1 个 session 才可恢复；0 或 >1 -> STOP
        try:
            found = reconcile_sessions(transport, probe_directory)
        except (ProbeClosedError, _ProbeTransportError) as exc:
            print(f"reconciliation 失败：{exc}", file=sys.stderr)
            return 6
        print(f"reconciliation sessions={len(found)}")
        if len(found) == 1:
            session_id = found[0]
            journal(state_path, state, phase="SESSION_CREATED",
                    session_confirmed_at=_now_iso(), explicit_session_id=session_id)
            create_out["session_id"] = session_id
            create_out["phase"] = "SESSION_CREATED"
            create_out["kind"] = "RECONCILED"
        else:
            print(f"UNKNOWN_CREATE：reconciliation 得 {len(found)} 会话（需要恰好 1），STOP 不重发。",
                  file=sys.stderr)
            return 7
    else:
        print(f"create 未确认（phase={create_out['phase']}）：不重发，STOP。", file=sys.stderr)
        return 8

    # 4) WRITE #2 prompt_async（严格 1 次）
    send_out = _send_probe_prompt_once(
        transport, session_id, probe_directory, message_id, prompt_text, state_path,
        token=token, timeout=args.timeout, base_url=args.base_url,
    )
    print(f"send result: phase={send_out['phase']} http={send_out['http_status']}")
    observation: dict[str, Any] | None = None
    if send_out["phase"] == "SEND_ACCEPTED":
        # 5) 只读完成观察
        try:
            observation = observe_completion(
                transport, session_id, probe_directory, message_id, marker_for(nonce),
                max_wait=args.max_wait,
            )
        except (ProbeClosedError, _ProbeTransportError) as exc:
            print(f"观察阶段异常（只读）：{exc}", file=sys.stderr)
            observation = {"polls": [], "verdict": None, "first_result_observed_at": None,
                           "completion_at": None, "last_messages_redacted": []}
    elif send_out["phase"] == "UNKNOWN_SEND":
        print("UNKNOWN_SEND：绝不再次发送 prompt；后续只能 GET reconciliation。", file=sys.stderr)
    else:
        print(f"send 未接受（phase={send_out['phase']}）：不重发，STOP。", file=sys.stderr)

    state = load_state(state_path) or state
    final = journal(state_path, state,
                    first_result_observed_at=(observation or {}).get("first_result_observed_at"),
                    completion_at=(observation or {}).get("completion_at"),
                    completion_verdict=(observation or {}).get("verdict"),
                    phase="RESULT_OBSERVED" if observation is not None else state.get("phase"))

    service_reachable, api_authenticated = _service_readiness(transport)

    evidence = build_evidence(
        run_id=run_id,
        probe_directory=probe_directory,
        base_url=args.base_url,
        phase=final["phase"],
        session_id=session_id,
        session_confirmed_at=final.get("session_confirmed_at"),
        create_attempted_at=final.get("create_attempted_at"),
        send_attempted_at=final.get("send_attempted_at"),
        send_http_status=final.get("send_http_status"),
        send_accepted_at=final.get("send_accepted_at"),
        first_result_observed_at=final.get("first_result_observed_at"),
        completion_at=final.get("completion_at"),
        observation=observation,
        create_attempt_count=final.get("create_attempt_count", 0),
        send_attempt_count=final.get("send_attempt_count", 0),
        marker=marker_for(nonce),
        run_nonce=nonce,
        service_reachable=service_reachable,
        api_authenticated=api_authenticated,
        extra_notes={"send_result_phase": send_out["phase"]},
    )
    _atomic_write_json(out_path, evidence)
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _service_readiness(transport: Any) -> tuple[bool, bool | None]:
    try:
        raw = transport.request("GET", "/health", attach_auth=False)
        health_ok = 200 <= raw.status < 300
    except (_ProbeTransportError, OSError):
        health_ok = False
    try:
        raw = transport.request("GET", "/api/session/status", attach_auth=True)
        api_ok = bool(200 <= raw.status < 300) if health_ok else None
    except (_ProbeTransportError, OSError):
        api_ok = None
    return health_ok, api_ok


if __name__ == "__main__":
    raise SystemExit(main())