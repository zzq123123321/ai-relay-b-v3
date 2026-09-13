"""AI Relay B V3.0：OpenChamber 写/send/session 合同只读侦察（T21-B1）。

本脚本只做【只读】探测，绝不调用任何可能改变会话状态的写式方法：
- 全部请求一律 GET；代码中不存在 POST/PUT/PATCH/DELETE 调用。
- 会话 ID 使用合成占位（sess_000... _000...，必不存在的格式），仅用于验证
  路由接线（route wiring），不会创建、写入或影响任何真实会话。
- GET 访问 /prompt /prompt_async /compact /wait /interrupt /message 这类路由
  只会得到 404/405/400 之类的无副作用响应，不触发 agent 执行。
- Bearer token 仅当目标为 loopback 时读取并附加（复用 T19 规则与 seam）。
- 证据脱敏与 sample_hash 复用 T19 实现；合成 session id 已是占位符。
- 文档侧证据来自本地安装包内 asar / web-dist / opencode 二进制字符串提取
  （只读文件分析），并记录来源 offset 供复核。

capability_status 只使用四类正式值：
  OBSERVED_READONLY_CONTRACT / DOCUMENTED_LOCAL_SOURCE / LEGACY_ONLY / UNVERIFIED；
  写能力一律禁止写成 SUPPORTED_WRITE（无真实 side-effect probe 前不渝）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scripts.probe_openchamber import (
        DEFAULT_BASE_URL,
        DEFAULT_TIMEOUT_SECONDS,
        MAX_BODY_BYTES,
        ProbeTransport,
        ProbeTransportError,
        default_settings_path,
        is_loopback_base_url,
        redact_json,
        resolve_auth_token,
        sample_hash_of,
        shape_of,
    )
except ImportError:  # pragma: no cover - 允许脚本独立以 ``python -m scripts.xxx`` 运行
    from probe_openchamber import (
        DEFAULT_BASE_URL,
        DEFAULT_TIMEOUT_SECONDS,
        MAX_BODY_BYTES,
        ProbeTransport,
        ProbeTransportError,
        default_settings_path,
        is_loopback_base_url,
        redact_json,
        resolve_auth_token,
        sample_hash_of,
        shape_of,
    )

# 合成且必不存在的会话 ID：仅用于路由接线探测（格式对齐 opencode 会话 ID）
SYNTHETIC_SESSION_ID = "sess_0000000000000000_0000000000000000"

CLIENT_TOKEN_ENV = "OPENCHAMBER_CLIENT_TOKEN"

_ID_PATTERNS = (
    re.compile(r"(?i)\bses_[a-z0-9_]{8,}\b"),
    re.compile(r"(?i)\bmsg_[a-z0-9_]{8,}\b"),
    re.compile(r"(?i)\bsession-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
    re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
)


def sanitize_sample(value: Any) -> Any:
    """把字符串中的真实 session/message ID 与 UUID 替换为 <redacted_id>。

    只影响 sample 字符串内容，不改写响应结构；token 等仍由 redact_json 处理。
    """
    if isinstance(value, dict):
        return {str(k): sanitize_sample(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_sample(v) for v in value]
    if isinstance(value, str):
        out = value
        for pat in _ID_PATTERNS:
            out = pat.sub("<redacted_id>", out)
        return out
    return value

# --------------------------------------------------------------------------- 路由探测


@dataclass(frozen=True, slots=True)
class RouteProbe:
    name: str
    method: str
    path: str
    attach_auth: bool = True
    note: str = ""


def build_route_probes(session_id: str) -> list[RouteProbe]:
    """构造只读（仅 GET）路由接线探测计划。

    全部为 GET：对以 POST 为主的写路由发起 GET 只会得到无副作用的 404/405/400，
    用于证明【路由是否接线】，绝不证明也不触发写操作。
    """
    sid = urllib.parse.quote(session_id, safe="")
    return [
        RouteProbe("health", "GET", "/health", attach_auth=False,
                   note="service reachability, no auth"),
        RouteProbe("api_guard_unauthzed", "GET", "/api", attach_auth=False,
                   note="auth guard: expect 401 without token"),
        RouteProbe("session_list", "GET", "/api/session",
                   note="session list (read contract baseline)"),
        RouteProbe("session_status", "GET", "/api/session/status",
                   note="session status map (read contract baseline)"),
        RouteProbe("session_active", "GET", "/api/session/active",
                   note="active sessions (read contract)"),
        RouteProbe("session_get", "GET", f"/api/session/{sid}",
                   note="route wiring for session.get"),
        RouteProbe("session_messages", "GET", f"/api/session/{sid}/message",
                   note="route wiring: classic messages GET"),
        RouteProbe("session_message_one", "GET", f"/api/session/{sid}/message/{sid}",
                   note="route wiring: single message GET"),
        RouteProbe("session_prompt", "GET", f"/api/session/{sid}/prompt",
                   note="route wiring: v2 prompt (POST-only; GET must never mutate)"),
        RouteProbe("session_prompt_async", "GET", f"/api/session/{sid}/prompt_async",
                   note="route wiring: classic prompt_async (POST-only)"),
        RouteProbe("session_compact", "GET", f"/api/session/{sid}/compact",
                   note="route wiring: compact (POST-only)"),
        RouteProbe("session_wait", "GET", f"/api/session/{sid}/wait",
                   note="route wiring: wait (POST-only)"),
        RouteProbe("session_interrupt", "GET", f"/api/session/{sid}/interrupt",
                   note="route wiring: interrupt (POST-only)"),
        RouteProbe("session_context", "GET", f"/api/session/{sid}/context",
                   note="route wiring: context GET"),
        RouteProbe("session_history", "GET", f"/api/session/{sid}/history?limit=1",
                   note="route wiring: history GET"),
    ]


def classify_route_wiring(status: int, content_type: str | None, text: str) -> dict[str, Any]:
    """把 404/405/400 的分类逻辑做成可测纯函数。

    规则（正式 status 只允许四值）：
    - 2xx 且 JSON 可解析 → OBSERVED_READONLY_CONTRACT（只读 GET 观察到的只读路由契约）
    - 400 → 路由接线（handler 到达，参数/ID 校验失败），写能力仍 UNVERIFIED
    - 404 → 可能是“无该路由”或“会话不存在”，统一 NOT_FOUND，写能力 UNVERIFIED
    - 405 → 路由接线（方法不允许），写能力 UNVERIFIED
    - 401/403 → 认证未通过，UNVERIFIED / AUTH
    - 其余 4xx/5xx → 按状态归类
    """
    try:
        json.loads(text)
        is_json = True
    except ValueError:
        is_json = False

    entry: dict[str, Any] = {
        "content_type": (content_type or "").split(";")[0].strip() or None,
        "is_json": is_json,
    }
    if 200 <= status < 300:
        if is_json:
            entry["capability_status"] = "OBSERVED_READONLY_CONTRACT"
        else:
            entry["capability_status"] = "UNVERIFIED"
            entry["error_kind"] = "PARSE"
    elif status == 400:
        entry["capability_status"] = "UNVERIFIED"
        entry["error_kind"] = "ROUTE_REACHED_PARAM_REJECTED"
    elif status in (401, 403):
        entry["capability_status"] = "UNVERIFIED"
        entry["error_kind"] = "AUTH"
    elif status == 404:
        entry["capability_status"] = "UNVERIFIED"
        entry["error_kind"] = "NOT_FOUND"
    elif status == 405:
        entry["capability_status"] = "UNVERIFIED"
        entry["error_kind"] = "ROUTE_REACHED_METHOD_NOT_ALLOWED"
    else:
        entry["capability_status"] = "UNVERIFIED"
        entry["error_kind"] = f"HTTP_{status}"
    return entry


def probe_route(transport: ProbeTransport, probe: RouteProbe) -> dict[str, Any]:
    """执行单条只读路由探测并返回证据条目。"""
    entry: dict[str, Any] = {
        "name": probe.name,
        "method": probe.method,
        "path": probe.path,
        "note": probe.note,
        "http_status": None,
        "classified": None,
        "sample_redacted": None,
        "sample_hash": None,
        "error_kind": None,
    }
    try:
        raw = transport.request(probe.method, probe.path, attach_auth=probe.attach_auth)
    except ProbeTransportError as exc:
        entry["error_kind"] = exc.kind
        return entry
    entry["http_status"] = raw.status
    text = raw.decoded
    classified = classify_route_wiring(raw.status, raw.content_type, text)
    entry["classified"] = classified
    if classified.get("error_kind"):
        entry["error_kind"] = classified["error_kind"]
    if classified.get("is_json") and text.strip():
        try:
            parsed = json.loads(text)
            redacted = redact_json(parsed)
            # 数组只保留前 3 项作为脱敏 sample，避免证据文件膨胀
            if isinstance(redacted, list) and len(redacted) > 3:
                redacted = redacted[:3]
            redacted = sanitize_sample(redacted)
            entry["sample_redacted"] = redacted
            entry["sample_hash"] = sample_hash_of(redacted)
        except ValueError:
            entry["sample_redacted"] = {"raw_prefix": text[:200]}
            entry["sample_hash"] = sample_hash_of({"raw_prefix": text[:200]})
    return entry


# --------------------------------------------------------------------------- 文档侧证据（本地安装包字符串提取，只读）


def probe_documented_contract(artifacts: dict[str, str]) -> dict[str, Any]:
    """从本地 artifacts（asar / web-dist / opencode 二进制全文）中提取合同锚点。

    都是只读文件分析；每条记录提取到的证据子串与来源 offset，供后续复核。
    """
    found: dict[str, Any] = {"sources": {}, "schema_anchors": {}}
    when_missing = {"present": False, "note": "本地安装包未命中（可能版本差异，交由实测与文档裁决）"}

    asar = artifacts.get("asar", "")
    binary = artifacts.get("binary", "")
    frontend = artifacts.get("frontend", "")

    for key, host, pat in [
        ("desktop_forwarder_doc", asar,
         'POST /api/session/:sessionId/message'),
        ("desktop_proxy_pathRewrite", asar,
         "pathRewrite: { '^/api': '' }"),
        ("desktop_api_use_proxy", asar, "app.use(\"/api\", apiProxy)"),
        ("sdk_desktop_prompt", frontend,
         'url:"/api/session/{sessionID}/prompt"'),
        ("sdk_desktop_create", frontend,
         'url:"/api/session"'),
        ("v2_prompt_route", binary,
         'identifier:"v2.session.prompt"'),
        ("v2_payload", binary,
         'payload:g.Struct({id:ir.ID.pipe(g.optional),prompt:i4.Prompt,delivery:Q0.Delivery.pipe(g.optional),res'),
        ("classic_prompt_route", binary,
         'prompt:`${yn}/:sessionID/message`'),
        ("classic_status_schema", binary,
         'q8=g.Record(g.String,Vg.Info)'),
        ("classic_create_payload", binary,
         'success:A(c.Info,"Successfully created session")'),
    ]:
        idx = host.find(pat)
        if idx < 0:
            found["schema_anchors"][key] = when_missing
            continue
        context = host[max(0, idx - 40): idx + len(pat) + 60].replace("\n", " ")
        found["schema_anchors"][key] = {
            "present": True,
            "pattern": pat,
            "offset": idx,
            "context": context[:220],
        }

    # 校验锚点必须至少命中一次/ascii 编码保护
    anchors = found["schema_anchors"]
    found["sources"]["overall"] = {
        "asar_len": len(asar),
        "binary_len": len(binary),
        "frontend_len": len(frontend),
        "classic_send_anchor_hit": anchors.get("classic_prompt_route", {}).get("present", False),
        "v2_prompt_anchor_hit": anchors.get("v2_prompt_route", {}).get("present", False),
    }
    return found


# --------------------------------------------------------------------------- 组装证据


def load_artifacts() -> dict[str, str]:
    """读取本地安装包全文（只读）。路径未找到则置空字符串并在证据中注明。"""
    base = r"C:\Users\LocalUser\AppData\Local\Programs\@openchamberelectron\resources"
    paths = {
        "asar": base + r"\app.asar",
        "binary": base + r"\opencode-cli\opencode.exe",
        "frontend": base + r"\web-dist\assets\index-CRf2xk51.js",
    }
    out: dict[str, str] = {}
    for key, path in paths.items():
        try:
            out[key] = Path(path).read_bytes().decode("utf-8", "replace")
        except OSError:
            out[key] = ""
    return out


def run_probe(base_url: str, token: str | None, timeout: float) -> dict[str, Any]:
    transport = ProbeTransport(base_url, token=token, timeout=timeout)
    probes = build_route_probes(SYNTHETIC_SESSION_ID)
    endpoints = [probe_route(transport, p) for p in probes]

    health = next((e for e in endpoints if e["name"] == "health"), {})
    unauth = next((e for e in endpoints if e["name"] == "api_guard_unauthzed"), {})
    api_probed = [e for e in endpoints if e["name"] not in ("health", "api_guard_unauthzed")]

    service_reachable = bool(
        200 <= (health.get("http_status") or -1) < 300
    )
    api_2xx = any(200 <= (e.get("http_status") or -1) < 300 for e in api_probed)
    api_auth_refused = any(e.get("error_kind") == "AUTH" for e in api_probed)
    api_authenticated = bool(api_2xx) if api_2xx or api_auth_refused else None

    artifacts = load_artifacts()
    documented = probe_documented_contract(artifacts)

    return {
        "tool": "probe_openchamber_write_contract",
        "round": "T21-B1",
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "transport": {
            "base_url": transport.base_url,
            "loopback_target": transport.is_loopback,
            "write_methods_used": [],
            "session_id": SYNTHETIC_SESSION_ID,
        },
        "service_reachable": service_reachable,
        "api_authenticated": api_authenticated,
        "unauthz_guard_401_observed": unauth.get("http_status") == 401,
        "endpoints": endpoints,
        "documented_contract": documented,
        "notes": {
            "write_capability_rule": (
                "所有写端点本轮只做路由接线/文档锚点侦察；capability 只能落在 "
                "OBSERVED_READONLY_CONTRACT / DOCUMENTED_LOCAL_SOURCE / LEGACY_ONLY / "
                "UNVERIFIED，禁止断言 SUPPORTED_WRITE"
            ),
            "synthetic_session_id": (
                "合成必不存在的会话 ID，仅用于以 GET 验证路由是否接线，不产生会话"
            ),
        },
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="probe_openchamber_write_contract",
        description="OpenChamber 写/send/session 合同只读侦察（仅 GET）",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="OpenChamber 根地址")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--out", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    loopback = is_loopback_base_url(args.base_url)
    token = resolve_auth_token(
        env_token=os.environ.get(CLIENT_TOKEN_ENV),
        settings_path=default_settings_path(),
        attach_allowed=loopback,
    )
    transport = ProbeTransport(args.base_url, token=token, timeout=args.timeout)
    evidence = run_probe(args.base_url, token, args.timeout)
    text = json.dumps(evidence, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())