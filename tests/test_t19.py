"""T19-B1：OpenChamber 只读合同探测器的自动测试（不依赖真实 OpenChamber）。

覆盖卡第 11 节 18 项 + O01 无写副作用 + permission 无 approve 副作用。
探测脚本经 importlib 动态加载（scripts/ 无 __init__.py，保持 tracked 仅两文件）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import socket
import sys
import urllib.parse
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_PROBE_PATH = _REPO / "scripts" / "probe_openchamber.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("probe_openchamber", _PROBE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


probe = _load_probe()


class FakeResponse:
    def __init__(self, status: int, body: str, content_type: str = "application/json"):
        self.status = status
        self._body = body.encode("utf-8")
        self.headers = {"Content-Type": content_type}
        self.content_type = content_type

    def getcode(self) -> int:
        return self.status

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = len(self._body)
        return self._body[:n]


class FakeOpener:
    """本地假 HTTP 入口：按 path 返回路由后的响应并记录全部请求。

    route 值可以是 (status, body) 或可抛出的 Exception 实例。
    """

    def __init__(self, routes: dict[str, object], default_status: int = 200,
                 default_body: str = "{}"):
        self.routes = {("/" + str(k).strip("/")): v for k, v in routes.items()}
        self.default_status = default_status
        self.default_body = default_body
        self.requests: list[dict] = []

    def __call__(self, req):
        url = req.full_url
        method = req.get_method()
        headers = {k: v for k, v in req.header_items()}
        self.requests.append({"method": method, "url": url, "headers": headers})
        parts = urllib.parse.urlsplit(url)
        path_query = (parts.path or "").rstrip("/")
        if parts.query:
            path_query += "?" + parts.query
        if path_query in self.routes:
            entry = self.routes[path_query]
            if isinstance(entry, Exception):
                raise entry
            status, body = entry
            return FakeResponse(status, body)
        return FakeResponse(self.default_status, self.default_body)


def _encoded_dir(directory: str) -> str:
    import urllib.parse

    return urllib.parse.quote(directory, safe="")


def _run(base_url, *, directory="D:\\AIwork\\ai_relay_b_v3", session_id=None,
         routes=None, env_token=None, settings_text="{}", default_status=200,
         default_body="{}"):
    routes = routes or {}
    opener = FakeOpener(routes, default_status=default_status, default_body=default_body)
    settings_path = Path(__file__).resolve().parent / ".t19_settings_tmp.json"
    settings_path.write_text(settings_text, encoding="utf-8")
    try:
        token = probe.resolve_auth_token(
            env_token=env_token, settings_path=settings_path,
            attach_allowed=probe.is_loopback_base_url(base_url),
        )
        transport = probe.ProbeTransport(base_url, token=token, timeout=1.0, opener=opener)
        evidence = probe.run_probe(transport, directory=directory, session_id=session_id)
        return evidence, opener, transport
    finally:
        settings_path.unlink(missing_ok=True)


def _ok_routes(directory: str) -> dict:
    enc = _encoded_dir(directory)
    routes = {
        "/health": (200, '{"status":"ok","openchamberVersion":"1.23.0"}'),
        f"/api/session?directory={enc}": (
            200, '{"sessions":[{"id":"ses_a","title":"t"}],"page":1,"total":1}'
        ),
        f"/api/session/status?directory={enc}": (200, '{"status":"idle","sessionId":null}'),
        "/api/permission-auto-accept": (
            200, '{"sessions":{"ses_a":true},"revision":1}'
        ),
    }
    return routes


def test_health_200_service_reachable():
    ev, _, _ = _run("http://127.0.0.1:57123", routes=_ok_routes("d"))
    assert ev["service_reachable"] is True


def test_health_200_does_not_imply_api_authenticated():
    ev, _, _ = _run(
        "http://127.0.0.1:57123",
        routes={
            "/health": (200, '{"status":"ok"}'),
            "/api/session?directory=d": (401, '{"error":"unauthorized"}'),
            "/api/session/status?directory=d": (401, '{"error":"unauthorized"}'),
            "/api/permission-auto-accept": (401, '{"error":"unauthorized"}'),
        },
        directory="d",
    )
    assert ev["service_reachable"] is True
    assert ev["api_authenticated"] is False


def test_api_200_api_authenticated():
    ev, _, _ = _run("http://127.0.0.1:57123", directory="d", routes=_ok_routes("d"))
    assert ev["api_authenticated"] is True


def test_api_401_api_authenticated_false():
    ev, _, _ = _run(
        "http://127.0.0.1:57123",
        directory="d",
        routes={
            "/health": (200, '{"status":"ok"}'),
            "/api/session?directory=d": (401, '{"error":"unauthorized"}'),
            "/api/session/status?directory=d": (401, '{"error":"unauthorized"}'),
            "/api/permission-auto-accept": (401, '{"error":"unauthorized"}'),
        },
    )
    for ep in ev["endpoints"]:
        if ep["endpoint"] != "health" and not ep["path"].startswith("(not"):
            assert ep["capability_status"] == "UNVERIFIED"
            assert ep["error_kind"] == "AUTH"
    assert ev["api_authenticated"] is False


def test_api_403_api_authenticated_false():
    ev, _, _ = _run(
        "http://127.0.0.1:57123",
        directory="d",
        routes={
            "/health": (200, '{"status":"ok"}'),
            "/api/session?directory=d": (403, '{"error":"forbidden"}'),
            "/api/session/status?directory=d": (200, '{"status":"idle"}'),
            "/api/permission-auto-accept": (403, '{"error":"forbidden"}'),
        },
    )
    assert ev["api_authenticated"] is True  # status 端 200
    status_ep = next(e for e in ev["endpoints"] if e["endpoint"] == "session_status")
    assert status_ep["capability_status"] == "SUPPORTED"
    perm_ep = next(e for e in ev["endpoints"] if e["endpoint"] == "permission")
    assert perm_ep["capability_status"] == "UNVERIFIED"
    assert perm_ep["error_kind"] == "AUTH"


def test_api_404_endpoint_unverified():
    ev, _, _ = _run(
        "http://127.0.0.1:57123",
        directory="d",
        routes={
            "/health": (200, '{"status":"ok"}'),
            "/api/session?directory=d": (200, '{"sessions":[]}'),
            "/api/session/status?directory=d": (404, '{"error":"not found"}'),
            "/api/permission-auto-accept": (404, '{"error":"not found"}'),
        },
    )
    status_ep = next(e for e in ev["endpoints"] if e["endpoint"] == "session_status")
    assert status_ep["capability_status"] == "UNVERIFIED"
    assert status_ep["error_kind"] == "NOT_FOUND_OR_UNSUPPORTED"


def test_timeout_unverified():
    ev, _, _ = _run(
        "http://127.0.0.1:57123",
        directory="d",
        routes={
            "/health": (200, '{"status":"ok"}'),
            "/api/session?directory=d": socket.timeout("timeout"),
            "/api/session/status?directory=d": (200, '{"status":"idle"}'),
            "/api/permission-auto-accept": (200, '{"sessions":{}}'),
        },
    )
    session_ep = next(e for e in ev["endpoints"] if e["endpoint"] == "session_list")
    assert session_ep["capability_status"] == "UNVERIFIED"
    assert session_ep["error_kind"] == "TIMEOUT"
    assert ev["api_authenticated"] is True


def test_missing_session_id_does_not_request_message_endpoint():
    ev, opener, transport = _run("http://127.0.0.1:57123", directory="d", routes=_ok_routes("d"))
    msg = next(e for e in ev["endpoints"] if e["endpoint"] == "session_messages")
    assert msg["capability_status"] == "UNVERIFIED"
    assert msg["error_kind"] == "MISSING_INPUT"
    assert msg["http_status"] is None
    assert not any("message" in r["url"] for r in opener.requests)
    assert transport.n_calls == 4  # 仅 health + 3 个 API 端点在无 session-id 时被请求


def test_directory_url_encoded():
    directory = "D:\\AIwork\\ai_relay_b v3 & x"
    ev, opener, _ = _run("http://127.0.0.1:57123", directory=directory, routes=_ok_routes(directory))
    enc = _encoded_dir(directory)
    requested = [r["url"] for r in opener.requests if "/api/session?" in r["url"]]
    assert requested
    assert requested[0] == (
        f"http://127.0.0.1:57123/api/session?directory={enc}"
    )
    assert " " not in requested[0]


def test_loopback_127_attaches_bearer():
    token = "tok-127"
    _, opener, _ = _run(
        "http://127.0.0.1:57123", directory="d", routes=_ok_routes("d"),
        env_token=token,
    )
    api_reqs = [r for r in opener.requests if "/api/" in r["url"]]
    assert api_reqs
    assert all(r["headers"].get("Authorization") == f"Bearer {token}" for r in api_reqs)


def test_localhost_attaches_bearer():
    token = "tok-localhost"
    _, opener, _ = _run(
        "http://localhost:57123", directory="d", routes=_ok_routes("d"),
        env_token=token,
    )
    api_reqs = [r for r in opener.requests if "/api/" in r["url"]]
    assert api_reqs
    assert all(r["headers"].get("Authorization") == f"Bearer {token}" for r in api_reqs)


def test_loopback_ipv6_attaches_bearer():
    token = "tok-v6"
    _, opener, _ = _run(
        "http://[::1]:57123", directory="d", routes=_ok_routes("d"),
        env_token=token,
    )
    api_reqs = [r for r in opener.requests if "/api/" in r["url"]]
    assert api_reqs
    assert all(r["headers"].get("Authorization") == f"Bearer {token}" for r in api_reqs)


def test_non_loopback_never_attaches_desktop_token():
    settings = '{"desktopLocalClientToken":"SECRET-DESKTOP","desktopUiPassword":"p"}'
    _, opener, _ = _run(
        "http://192.0.2.10:57123", directory="d", routes=_ok_routes("d"),
        env_token="SECRET-ENV", settings_text=settings,
    )
    assert opener.requests
    assert all("Authorization" not in r["headers"] for r in opener.requests)


def test_loopback_127_0_0_2_never_attaches_any_token():
    settings = '{"desktopLocalClientToken":"SECRET-DESKTOP-2"}'
    _, opener, transport = _run(
        "http://127.0.0.2:57123", directory="d", routes=_ok_routes("d"),
        env_token="SECRET-ENV-2", settings_text=settings,
    )
    assert transport.is_loopback is False
    assert opener.requests
    assert all("Authorization" not in r["headers"] for r in opener.requests)


def test_loopback_127_1_2_3_never_attaches_any_token():
    settings = '{"desktopLocalClientToken":"SECRET-DESKTOP-3"}'
    _, opener, transport = _run(
        "http://127.1.2.3:57123", directory="d", routes=_ok_routes("d"),
        env_token="SECRET-ENV-3", settings_text=settings,
    )
    assert transport.is_loopback is False
    assert opener.requests
    assert all("Authorization" not in r["headers"] for r in opener.requests)


def test_is_loopback_host_exact_whitelist():
    assert probe.is_loopback_host("127.0.0.1") is True
    assert probe.is_loopback_host("localhost") is True
    assert probe.is_loopback_host("::1") is True
    assert probe.is_loopback_host("127.0.0.2") is False
    assert probe.is_loopback_host("127.1.2.3") is False
    assert probe.is_loopback_host("LOCALHOST") is True


def test_authorization_not_in_evidence():
    ev, _, _ = _run(
        "http://127.0.0.1:57123", directory="d", routes=_ok_routes("d"),
        env_token="tok-secret",
    )
    text = json.dumps(ev)
    assert "Authorization" not in text
    assert "Bearer" not in text
    assert "tok-secret" not in text


def test_token_password_recursive_redaction():
    body = json.dumps({
        "token": "t", "nested": {"password": "p", "api_key": "k"},
        "data": {"clientToken": "c", "desktopLocalClientToken": "d"},
        "ui": {"desktopUiPassword": "up", "apiKey": "ak"},
        "keep": "visible", "n": [{"cookie": "x"}],
    })
    routes = {
        "/health": (200, '{"status":"ok"}'),
        "/api/session?directory=d": (200, body),
        "/api/session/status?directory=d": (200, '{"status":"idle"}'),
        "/api/permission-auto-accept": (200, '{"sessions":{}}'),
    }
    ev, _, _ = _run("http://127.0.0.1:57123", directory="d", routes=routes)
    session_ep = next(e for e in ev["endpoints"] if e["endpoint"] == "session_list")
    sample = session_ep["sample"]
    assert sample["token"] == "<redacted>"
    assert sample["nested"]["password"] == "<redacted>"
    assert sample["nested"]["api_key"] == "<redacted>"
    assert sample["data"]["clientToken"] == "<redacted>"
    assert sample["data"]["desktopLocalClientToken"] == "<redacted>"
    assert sample["ui"]["desktopUiPassword"] == "<redacted>"
    assert sample["ui"]["apiKey"] == "<redacted>"
    assert sample["keep"] == "visible"
    assert sample["n"][0]["cookie"] == "<redacted>"
    assert "tok-secret" not in json.dumps(ev)


def test_sample_hash_stable_and_redacted_based():
    body_a = '{"token":"AAA","data":{"x":1}}'
    body_b = '{"token":"BBB","data":{"x":1}}'
    routes_a = {
        "/health": (200, '{"status":"ok"}'),
        "/api/session?directory=d": (200, body_a),
        "/api/session/status?directory=d": (200, '{"status":"idle"}'),
        "/api/permission-auto-accept": (200, '{"sessions":{}}'),
    }
    routes_b = dict(routes_a)
    routes_b["/api/session?directory=d"] = (200, body_b)
    ev_a, _, _ = _run("http://127.0.0.1:57123", directory="d", routes=routes_a)
    ev_b, _, _ = _run("http://127.0.0.1:57123", directory="d", routes=routes_b)
    sa = next(e for e in ev_a["endpoints"] if e["endpoint"] == "session_list")
    sb = next(e for e in ev_b["endpoints"] if e["endpoint"] == "session_list")
    assert sa["sample_hash"] == sb["sample_hash"]
    assert len(sa["sample_hash"]) == 64
    canonical_a = json.dumps(sa["sample"], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert sa["sample_hash"] == hashlib.sha256(canonical_a.encode("utf-8")).hexdigest()


def test_health_and_api_share_same_transport_seam():
    _, _, transport = _run("http://127.0.0.1:57123", directory="d", routes=_ok_routes("d"))
    paths = [p for _, p, _ in transport.call_log]
    assert "/health" in paths
    assert any(p.startswith("/api/") for p in paths)
    assert len(paths) >= 4
    assert all(method == "GET" for method, _, _ in transport.call_log)


def test_no_write_methods_in_source():
    import re

    src = _PROBE_PATH.read_text(encoding="utf-8")
    hits = re.findall(r"\b(?:POST|PUT|PATCH|DELETE)\b", src)
    assert hits == [], f"探测源码不得出现写式 HTTP 方法：{hits}（注意 MISSING_INPUT 含 PUT 子串，需整词匹配）"


def test_o01_missing_semantics_no_write_operations():
    routes = {
        "/health": (200, '{"status":"ok"}'),
        "/api/session?directory=d": (200, '{"sessions":[],"missing":true}'),
        "/api/session/status?directory=d": (200, '{"status":"missing","sessionId":null}'),
        "/api/permission-auto-accept": (200, '{"sessions":{}}'),
    }
    ev, opener, _ = _run("http://127.0.0.1:57123", directory="d", routes=routes)
    status_ep = next(e for e in ev["endpoints"] if e["endpoint"] == "session_status")
    assert status_ep["missing_semantics"]
    assert opener.requests
    assert all(r["method"] == "GET" for r in opener.requests)
    assert all(e["method"] == "GET" or e["method"] == "" for e in ev["endpoints"])
    for ep in ev["endpoints"]:
        if ep["method"]:
            assert ep["method"] == "GET"


def test_permission_probe_no_approve_side_effect():
    routes = _ok_routes("d")
    ev, opener, _ = _run("http://127.0.0.1:57123", directory="d", routes=routes)
    perm = next(e for e in ev["endpoints"] if e["endpoint"] == "permission")
    assert perm["capability_status"] == "SUPPORTED"
    assert ev["api_authenticated"] is True
    assert all(r["method"] == "GET" for r in opener.requests)
    assert all("permission-auto-accept" in r["url"] for r in opener.requests
               if "permission-auto-accept" in r["url"])
    assert not any(r["method"] != "GET" for r in opener.requests)


def test_health_request_carries_no_authorization():
    token = "tok-nonauth-health"
    _, opener, _ = _run(
        "http://127.0.0.1:57123", directory="d", routes=_ok_routes("d"),
        env_token=token,
    )
    health_reqs = [r for r in opener.requests if "/health" in r["url"]]
    assert health_reqs
    assert all("Authorization" not in r["headers"] for r in health_reqs)