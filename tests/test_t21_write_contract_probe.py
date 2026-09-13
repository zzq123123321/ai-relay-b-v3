"""T21-01：OpenChamber 写/send/session 合同只读侦察探测器的自动测试。

全程 0 真实网络（注入 fake opener / 纯函数断言）：
- 路由接线分类纯函数（classify_route_wiring）的状态字面量语义；
- 探测计划只允许 GET、写动词路由以 GET 探测且属性表注明 POST-only；
- 证据脱敏：真实 session/message ID、UUID 被替换为 <redacted_id>；
- 文档锚点提取对缺失文件安全降级（present=False 而非抛错）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from scripts.probe_openchamber_write_contract import (  # noqa: E402
    SYNTHETIC_SESSION_ID,
    RouteProbe,
    build_route_probes,
    classify_route_wiring,
    probe_documented_contract,
    sanitize_sample,
)


class _FakeOpener:
    """注入用 fake opener：本探测计划唯一真实网络入口被彻底隔离。"""

    def __init__(self, status: int, body: str, content_type: str = "application/json"):
        self._status = status
        self._body = body.encode("utf-8")
        self._ct = content_type

    def __call__(self, req):
        frame = _FakeResponse(self._status, self._body, self._ct)
        if self._status >= 400:
            import urllib.error

            raise urllib.error.HTTPError(
                req.full_url, self._status, "fake", frame.headers, frame
            )
        return frame


class _FakeResponse:
    def __init__(self, status, body, content_type):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.content_type = content_type
        self._body = body

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._body)
        return self._body[:n]

    def getcode(self):
        return self.status


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


def test_plan_only_get_methods_and_marks_write_verbs():
    probes = build_route_probes(SYNTHETIC_SESSION_ID)
    assert probes
    assert all(p.method == "GET" for p in probes)
    write_named = {"session_prompt", "session_compact", "session_wait", "session_interrupt"}
    write_probes = [p for p in probes if p.name in write_named]
    assert write_probes
    assert all("POST-only" in p.note for p in write_probes)
    # 全部探测路径都必须落在 /api/*（桌面服务只读面），不访问裸 /session/*
    assert all(p.path == "/api" or p.path.startswith("/api/") or p.path == "/health" for p in probes)


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