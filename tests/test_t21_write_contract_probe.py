"""T21-01F：OpenChamber 写/send/session 合同只读侦察探测器（GET-only by construction）。

全程 0 真实网络（注入 fake transport / 纯函数断言）：
- 路由接线分类纯函数（classify_route_wiring）的状态字面量语义；
- 探测计划没有 method 入口：RouteProbe 无 method 字段、构造传 method= 必须 TypeError；
- probe_route 内部把 HTTP 动词硬编码为 "GET"，fake transport 捕获记实证明无其他方法；
- 模块公共 namespace 不暴露通用 ProbeTransport / ProbeTransportError（仅私有别名）；
- 静态 AST 检查：执行路径不存在 .method 属性访问、request() 首个实参恒为 "GET"；
- 证据脱敏 / 文档锚点 / 四值分类全部保持。
"""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import fields
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

import scripts.probe_openchamber_write_contract as mx  # noqa: E402
from scripts.probe_openchamber_write_contract import (  # noqa: E402
    SYNTHETIC_SESSION_ID,
    RouteProbe,
    build_route_probes,
    classify_route_wiring,
    probe_documented_contract,
    probe_route,
    sanitize_sample,
)


class _FakeProbeTransport:
    """注入用 probe transport 替身：记录 request 调用参数，绝不发起真实网络。"""

    def __init__(self, status: int = 200, text: str = '{"ok": true}', content_type: str = "application/json"):
        self.status = status
        self.text = text
        self.content_type = content_type
        self.calls: list[tuple[str, str, bool]] = []

    def request(self, method: str, path: str, *, attach_auth: bool):
        self.calls.append((method, path, attach_auth))
        return _RawLike(self.status, self.text, self.content_type)


class _RawLike:
    def __init__(self, status, text, content_type):
        self.status = status
        self.content_type = content_type
        self._text = text

    @property
    def decoded(self) -> str:
        return self._text


# ---------------------------------------------------------------------- 静态保护（AST）

def _script_source() -> str:
    return (Path(_REPO) / "scripts" / "probe_openchamber_write_contract.py").read_text(encoding="utf-8")


def _method_attributes_in_exec_path() -> list[ast.Attribute]:
    tree = ast.parse(_script_source())
    return [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "method"]


def _non_get_request_calls() -> list[tuple[int, str]]:
    tree = ast.parse(_script_source())
    bad: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else None
        if name != "request":
            continue
        first = node.args[0] if node.args else None
        if not (isinstance(first, ast.Constant) and first.value == "GET"):
            bad.append((node.lineno, "request() 首个位置实参不是 GET"))
        for kw in node.keywords:
            if kw.arg == "method":
                bad.append((node.lineno, "request() 携带 method= 关键字"))
    return bad


def _method_keyword_calls() -> list[tuple[int, str]]:
    tree = ast.parse(_script_source())
    return [
        (n.lineno, ast.unparse(kw.value))
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        for kw in n.keywords
        if kw.arg == "method"
    ]


def test_static_exec_path_has_no_method_seam():
    assert _method_attributes_in_exec_path() == []
    assert _non_get_request_calls() == []
    assert _method_keyword_calls() == []


# ---------------------------------------------------------------------- 结构性 GET-only


def test_routeprobe_dataclass_has_no_method_field():
    names = [f.name for f in fields(RouteProbe)]
    assert "method" not in names
    assert names == ["name", "path", "attach_auth", "note"]


def test_routeprobe_rejects_method_keyword():
    with pytest.raises(TypeError):
        RouteProbe("x", "/x", method="POST")


def test_plan_probes_have_no_method_and_marks_write_verbs():
    probes = build_route_probes(SYNTHETIC_SESSION_ID)
    assert probes
    assert all(not hasattr(p, "method") for p in probes)
    assert all(getattr(p, "method", None) is None for p in probes)
    write_named = {"session_prompt", "session_compact", "session_wait", "session_interrupt"}
    write_probes = [p for p in probes if p.name in write_named]
    assert write_probes
    assert all("POST-only" in p.note for p in write_probes)
    # 全部探测路径都必须落在 /api/*（桌面服务只读面），不访问裸 /session/*
    assert all(p.path == "/api" or p.path.startswith("/api/") or p.path == "/health" for p in probes)


def test_probe_route_always_sends_get():
    ft = _FakeProbeTransport()
    probe_route(ft, RouteProbe("session_list", "/api/session"))
    probe_route(ft, RouteProbe("health", "/health", attach_auth=False))
    assert len(ft.calls) == 2
    assert [m for m, _p, _a in ft.calls] == ["GET", "GET"]
    assert ft.calls[0][1] == "/api/session"
    assert ft.calls[0][2] is True
    assert ft.calls[1][1] == "/health"
    assert ft.calls[1][2] is False


def test_probe_route_entry_records_get():
    ft = _FakeProbeTransport()
    entry = probe_route(ft, RouteProbe("session_list", "/api/session"))
    assert entry["method"] == "GET"
    assert entry["http_status"] == 200
    assert entry["classified"]["capability_status"] == "OBSERVED_READONLY_CONTRACT"


def test_probe_route_transports_error_kind():
    class _ErrTransport:
        def request(self, method, path, *, attach_auth):
            raise mx._ProbeTransportError("TIMEOUT", "fake")

    entry = probe_route(_ErrTransport(), RouteProbe("session_list", "/api/session"))
    assert entry["http_status"] is None
    assert entry["error_kind"] == "TIMEOUT"


def test_public_namespace_hides_generic_transport():
    assert not hasattr(mx, "ProbeTransport")
    assert not hasattr(mx, "ProbeTransportError")
    assert hasattr(mx, "_ProbeTransport")
    assert hasattr(mx, "_ProbeTransportError")


# ---------------------------------------------------------------------- 行为保持（分类/脱敏/文档锚点）


def test_classify_readable_2xx_json_is_observed_readonly_contract():
    got = classify_route_wiring(200, "application/json", '{"ok": true}')
    assert got["capability_status"] == "OBSERVED_READONLY_CONTRACT"
    assert got["is_json"] is True


def test_classify_401_is_auth_not_reachable():
    got = classify_route_wiring(401, "text/plain", "unauthorized")
    assert got["capability_status"] == "UNVERIFIED"
    assert got["error_kind"] == "AUTH"


def test_classify_400_is_param_rejection_reaching_handler():
    got = classify_route_wiring(400, "application/json", '{"message": "bad"}')
    assert got["error_kind"] == "ROUTE_REACHED_PARAM_REJECTED"


def test_classify_405_is_method_not_allowed():
    got = classify_route_wiring(405, "text/plain", "x")
    assert got["error_kind"] == "ROUTE_REACHED_METHOD_NOT_ALLOWED"


def test_classify_404_is_not_found():
    got = classify_route_wiring(404, "application/json", '{"a": 1}')
    assert got["error_kind"] == "NOT_FOUND"


def test_sanitize_sample_removes_real_ids_and_uuids():
    sample = {
        "id": "ses_f666c6850ffeOGZidm6B3B5xF",
        "message": "Error for msg_1789282981807_0000 next",
        "path": "chats/2026-09-13/session-5bd8b331-3fdb-43cd-95b8-14b21236a981",
        "uuid": "3fdb-43cd-95b8-14b21236a981",
        "nested": [{"mid": "msg_abc123_xyz", "ok": 1}],
    }
    out = sanitize_sample(sample)
    text = json.dumps(out)
    assert "<redacted_id>" in text
    assert "ses_f666c6850ffeOGZidm6B3B5xF" not in text
    assert "msg_1789282981807_0000" not in text
    assert "session-5bd8b331-3fdb-43cd-95b8-14b21236a981" not in text


def test_documented_contract_present_for_extracted_anchors():
    got = probe_documented_contract({"asar": "", "binary": "", "frontend": ""})
    anchors = got["schema_anchors"]
    assert anchors["desktop_forwarder_doc"]["present"] is False
    assert anchors["v2_prompt_route"]["present"] is False
    assert got["sources"]["overall"]["classic_send_anchor_hit"] is False


def test_documented_contract_anchor_hits_when_present():
    binary = 'q8=g.Record(g.String,Vg.Info)  prompt:`${yn}/:sessionID/message`'
    got = probe_documented_contract({"asar": "", "binary": binary, "frontend": ""})
    anchors = got["schema_anchors"]
    assert anchors["classic_status_schema"]["present"] is True
    assert anchors["classic_prompt_route"]["present"] is True
    assert got["sources"]["overall"]["classic_send_anchor_hit"] is True