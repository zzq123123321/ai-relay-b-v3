"""T20-01：OpenChamber 只读 Transport 基座的自动测试。

全程使用注入 fake opener（0 真实网络）：构造期只读校验、GET-only 公开面、
精确本地认证边界、token 解析优先级、结构化错误、body 上限截断、单次请求计数。

测试风格对齐现有 tests 目录（pytest 函数式 + 断言习惯），覆盖卡第 11 节 24 项
并额外补充认证卫生/截断/计数等安全断言。
"""

from __future__ import annotations

import io
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from adapters.openchamber import (  # noqa: E402
    KIND_CONNECTION,
    KIND_CREDENTIALS_IN_URL,
    KIND_INVALID_URL,
    KIND_NO_HOSTNAME,
    KIND_TIMEOUT,
    KIND_UNSUPPORTED_SCHEME,
    LOCAL_AUTH_HOSTS,
    MAX_BODY_BYTES,
    OpenChamberRawResponse,
    OpenChamberReadError,
    OpenChamberReadTransport,
    is_loopback_host,
    resolve_local_auth_token,
)


class FakeResponse:
    def __init__(self, status: int, body: str | bytes, content_type: str = "application/json"):
        self.status = status
        self._body = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        self.headers = {"Content-Type": content_type}
        self.content_type = content_type

    def getcode(self) -> int:
        return self.status

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = len(self._body)
        return self._body[:n]


def _http_error(code: int, body: str, content_type: str = "application/json"):
    return urllib.error.HTTPError(
        "http://fake.local/",
        code,
        "mock http error",
        {"Content-Type": content_type},
        io.BytesIO(body.encode("utf-8")),
    )


class FakeOpener:
    """本地假 HTTP 入口：按 path 路由（响应或异常），记录全部请求并计数。

    route 值可以是 (status, body) 或可抛出的 Exception 实例（含 HTTPError）。
    """

    def __init__(self, routes: dict[str, object], default_status: int = 200,
                 default_body: str = "{}"):
        self.routes = {("/" + str(k).strip("/")): v for k, v in routes.items()}
        self.default_status = default_status
        self.default_body = default_body
        self.requests: list[dict] = []
        self.call_count = 0

    def __call__(self, req):
        self.call_count += 1
        method = req.get_method()
        headers = {k: v for k, v in req.header_items()}
        self.requests.append({"method": method, "url": req.full_url, "headers": headers})
        parts = urllib.parse.urlsplit(req.full_url)
        path_query = (parts.path or "").rstrip("/") or "/"
        if parts.query:
            path_query += "?" + parts.query
        if path_query in self.routes:
            entry = self.routes[path_query]
            if isinstance(entry, Exception):
                raise entry
            if isinstance(entry, (tuple, list)) and len(entry) == 3:
                status, body, content_type = entry
                return FakeResponse(status, body, content_type=content_type)
            status, body = entry
            return FakeResponse(status, body)
        return FakeResponse(self.default_status, self.default_body)


# =====================================================================
# 01-05：Base URL 构造期校验
# =====================================================================

def test_valid_http_base_url():
    t = OpenChamberReadTransport("http://localhost:57123", opener=FakeOpener({}))
    assert t.base_url == "http://localhost:57123"


def test_valid_https_base_url():
    t = OpenChamberReadTransport("https://127.0.0.1:57123", opener=FakeOpener({}))
    assert t.base_url == "https://127.0.0.1:57123"


@pytest.mark.parametrize(
    "bad",
    ["ftp://localhost/x", "file:///etc/passwd", "ws://localhost/", "localhost:57123"],
)
def test_invalid_scheme_rejected(bad):
    with pytest.raises(OpenChamberReadError) as ctx:
        OpenChamberReadTransport(bad)
    assert ctx.value.kind == KIND_UNSUPPORTED_SCHEME


@pytest.mark.parametrize("bad", ["http:///path", "https://:57123/", "http://", "http:/x", ""])
def test_missing_hostname_or_empty_rejected(bad):
    with pytest.raises(OpenChamberReadError) as ctx:
        OpenChamberReadTransport(bad)
    expected = KIND_NO_HOSTNAME if bad else KIND_INVALID_URL
    assert ctx.value.kind == expected


@pytest.mark.parametrize("bad", ["http://user:pw@localhost:57123/", "http://user@localhost/", "http://:pw@localhost/"])
def test_url_credentials_rejected(bad):
    with pytest.raises(OpenChamberReadError) as ctx:
        OpenChamberReadTransport(bad)
    assert ctx.value.kind == KIND_CREDENTIALS_IN_URL


# =====================================================================
# 06-12：精确本地认证边界
# =====================================================================

def test_localhost_auth_allowed():
    opener = FakeOpener({"/api/x": (200, "{}")})
    t = OpenChamberReadTransport("http://localhost:57123", token="tok-local", opener=opener)
    assert t._token == "tok-local"
    t.get("/api/x", attach_auth=True)
    assert opener.requests[0]["headers"]["Authorization"] == "Bearer tok-local"


def test_127_0_0_1_auth_allowed():
    opener = FakeOpener({"/api/x": (200, "{}")})
    t = OpenChamberReadTransport("http://127.0.0.1:57123", token="tok-127", opener=opener)
    t.get("/api/x", attach_auth=True)
    assert opener.requests[0]["headers"]["Authorization"] == "Bearer tok-127"


def test_ipv6_loopback_auth_allowed():
    opener = FakeOpener({"/api/x": (200, "{}")})
    t = OpenChamberReadTransport("http://[::1]:57123", token="tok-v6", opener=opener)
    t.get("/api/x", attach_auth=True)
    assert opener.requests[0]["headers"]["Authorization"] == "Bearer tok-v6"


def test_localhost_uppercase_normalized(tmp_path):
    opener = FakeOpener({"/api/x": (200, "{}")})
    t = OpenChamberReadTransport("http://LOCALHOST:57123", token="tok-u", opener=opener)
    assert t._token == "tok-u"
    t.get("/api/x", attach_auth=True)
    assert opener.requests[0]["headers"]["Authorization"] == "Bearer tok-u"
    settings = tmp_path / "s.json"
    settings.write_text(json.dumps({"desktopLocalClientToken": "tok-d"}), encoding="utf-8")
    assert resolve_local_auth_token("http://LOCALHOST:57123", env_token=None, settings_path=settings) == "tok-d"


@pytest.mark.parametrize("host", ["127.0.0.2", "127.1.2.3"])
def test_127_octet_non_loopback_auth_rejected(host):
    opener = FakeOpener({"/api/x": (200, "{}")})
    t = OpenChamberReadTransport(f"http://{host}:57123", token="tok", opener=opener)
    assert t._token is None
    t.get("/api/x", attach_auth=True)
    assert "Authorization" not in opener.requests[0]["headers"]
    assert resolve_local_auth_token(f"http://{host}:57123", env_token="tok", settings_path="ignored") is None


@pytest.mark.parametrize("base", ["http://192.0.2.10:57123", "https://example.com/"])
def test_remote_host_auth_rejected(base):
    opener = FakeOpener({"/api/x": (200, "{}")})
    t = OpenChamberReadTransport(base, token="tok", opener=opener)
    assert t._token is None
    t.get("/api/x", attach_auth=True)
    assert "Authorization" not in opener.requests[0]["headers"]
    assert resolve_local_auth_token(base, env_token="tok", settings_path="ignored") is None


def test_local_auth_hosts_exact_whitelist():
    assert LOCAL_AUTH_HOSTS == frozenset({"localhost", "127.0.0.1", "::1"})
    assert is_loopback_host("localhost") is True
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("::1") is True
    assert is_loopback_host("LOCALHOST") is True
    assert is_loopback_host("127.0.0.2") is False
    assert is_loopback_host("127.1.2.3") is False
    assert is_loopback_host("192.0.2.10") is False


# =====================================================================
# 13-15：token 解析优先级与设置文件读取边界
# =====================================================================

def test_env_token_priority(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"desktopLocalClientToken": "tok-desktop"}), encoding="utf-8")
    got = resolve_local_auth_token("http://localhost:57123", env_token="tok-env", settings_path=settings)
    assert got == "tok-env"
    opener = FakeOpener({})
    t = OpenChamberReadTransport("http://localhost:57123", token=got, opener=opener)
    t.get("/api/x", attach_auth=True)
    assert opener.requests[0]["headers"]["Authorization"] == "Bearer tok-env"


def test_desktop_token_fallback(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"desktopLocalClientToken": "tok-desktop"}), encoding="utf-8")
    got = resolve_local_auth_token("http://127.0.0.1:57123", env_token=None, settings_path=settings)
    assert got == "tok-desktop"


def test_desktop_fallback_malformed_or_missing(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert resolve_local_auth_token("http://localhost:1", env_token=None, settings_path=bad) is None
    missing_key = tmp_path / "missing.json"
    missing_key.write_text(json.dumps({"other": 1}), encoding="utf-8")
    assert resolve_local_auth_token("http://localhost:1", env_token=None, settings_path=missing_key) is None
    assert resolve_local_auth_token("http://localhost:1", env_token=None, settings_path=tmp_path / "none.json") is None


def test_remote_target_does_not_read_settings_file(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"desktopLocalClientToken": "tok-desktop"}), encoding="utf-8")

    def _fail(_p):
        raise AssertionError("非精确白名单主机不得读取 desktop settings 文件")

    monkeypatch.setattr(os.path, "isfile", _fail)
    for base in ("http://192.0.2.10:57123", "http://127.0.0.2:57123", "http://127.1.2.3:57123"):
        assert resolve_local_auth_token(base, env_token=None, settings_path=settings) is None


# =====================================================================
# 16-18：attach_auth 与 Bearer 具体行为
# =====================================================================

def test_attach_auth_false_never_sends_authorization():
    opener = FakeOpener({})
    t = OpenChamberReadTransport("http://localhost:57123", token="tok", opener=opener)
    t.get("/health", attach_auth=False)
    t.get("/api/session?directory=d", attach_auth=False)
    assert opener.requests
    assert all("Authorization" not in r["headers"] for r in opener.requests)


def test_attach_auth_true_local_sends_bearer():
    opener = FakeOpener({})
    t = OpenChamberReadTransport("http://localhost:57123", token="tok", opener=opener)
    t.get("/api/x", attach_auth=True)
    t.get("/health", attach_auth=False)
    assert opener.requests[0]["headers"]["Authorization"] == "Bearer tok"
    assert "Authorization" not in opener.requests[1]["headers"]


def test_remote_never_sends_bearer():
    for base in ("http://192.0.2.10:57123", "http://127.0.0.2:57123", "http://127.1.2.3:57123", "https://example.com"):
        opener = FakeOpener({})
        t = OpenChamberReadTransport(base, token="tok", opener=opener)
        t.get("/api/x", attach_auth=True)
        assert "Authorization" not in opener.requests[0]["headers"]


def test_all_wire_requests_are_get():
    opener = FakeOpener({"/api/x": (200, "{}"), "/h": (200, "{}")})
    t = OpenChamberReadTransport("http://localhost:57123", token="tok", opener=opener)
    t.get("/api/x", attach_auth=True)
    t.get("/h", attach_auth=False)
    assert all(r["method"] == "GET" for r in opener.requests)


# =====================================================================
# 19-21：HTTP 错误保留与结构化网络错误
# =====================================================================

def test_http_error_status_and_body_preserved():
    opener = FakeOpener({"/api/x": _http_error(404, '{"error":"not found"}')})
    t = OpenChamberReadTransport("http://localhost:57123", opener=opener)
    resp = t.get("/api/x", attach_auth=False)
    assert isinstance(resp, OpenChamberRawResponse)
    assert resp.status == 404
    assert resp.body == b'{"error":"not found"}'
    assert resp.truncated is False


def test_http_error_5xx_preserved_and_does_not_raise():
    opener = FakeOpener({"/api/x": _http_error(503, '{"error":"boom"}')})
    t = OpenChamberReadTransport("http://localhost:57123", opener=opener)
    resp = t.get("/api/x", attach_auth=False)
    assert resp.status == 503
    assert resp.body == b'{"error":"boom"}'


def test_http_error_still_applies_bearer_when_attach_auth():
    opener = FakeOpener({"/api/x": _http_error(403, '{"error":"forbidden"}')})
    t = OpenChamberReadTransport("http://localhost:57123", token="tok", opener=opener)
    t.get("/api/x", attach_auth=True)
    assert opener.requests[0]["headers"]["Authorization"] == "Bearer tok"


def test_timeout_structured():
    opener = FakeOpener({"/api/x": socket.timeout("timed out")})
    t = OpenChamberReadTransport("http://localhost:57123", opener=opener)
    with pytest.raises(OpenChamberReadError) as ctx:
        t.get("/api/x", attach_auth=False)
    assert ctx.value.kind == KIND_TIMEOUT


@pytest.mark.parametrize(
    "exc",
    [urllib.error.URLError(OSError("connection refused")), ConnectionRefusedError("refused")],
)
def test_connection_failure_structured(exc):
    opener = FakeOpener({"/api/x": exc})
    t = OpenChamberReadTransport("http://localhost:57123", opener=opener)
    with pytest.raises(OpenChamberReadError) as ctx:
        t.get("/api/x", attach_auth=False)
    assert ctx.value.kind == KIND_CONNECTION


# =====================================================================
# 22-23：单次计数不重试 + body 上限截断
# =====================================================================

def test_no_automatic_retry_and_single_opener_call():
    opener = FakeOpener({"/api/x": _http_error(500, '{"error":"boom"}')})
    t = OpenChamberReadTransport("http://localhost:57123", opener=opener)
    resp = t.get("/api/x", attach_auth=False)
    assert resp.status == 500
    assert opener.call_count == 1
    assert t.n_calls == 1


def test_timeout_does_not_retry_and_single_opener_call():
    opener = FakeOpener({"/api/x": socket.timeout("t")})
    t = OpenChamberReadTransport("http://localhost:57123", opener=opener)
    with pytest.raises(OpenChamberReadError):
        t.get("/api/x", attach_auth=False)
    assert opener.call_count == 1
    assert t.n_calls == 1


def test_ok_response_single_opener_call():
    opener = FakeOpener({"/ok": (200, '{"status":"idle"}')})
    t = OpenChamberReadTransport("http://localhost:57123", opener=opener)
    resp = t.get("/ok", attach_auth=False)
    assert resp.status == 200
    assert opener.call_count == 1
    assert t.n_calls == 1


def test_body_bounded_and_truncated():
    big = "x" * (MAX_BODY_BYTES + 17)
    opener = FakeOpener({"/big": (200, big)})
    t = OpenChamberReadTransport("http://localhost:57123", opener=opener)
    resp = t.get("/big", attach_auth=False)
    assert resp.truncated is True
    assert len(resp.body) == MAX_BODY_BYTES


def test_small_body_not_truncated():
    opener = FakeOpener({"/ok": (200, '{"x":1}')})
    t = OpenChamberReadTransport("http://localhost:57123", opener=opener)
    resp = t.get("/ok", attach_auth=False)
    assert resp.truncated is False
    assert resp.body == b'{"x":1}'


# =====================================================================
# 24：公开面只读（GET-only by construction）
# =====================================================================

def test_public_surface_has_no_write_methods():
    t = OpenChamberReadTransport("http://localhost:57123", opener=FakeOpener({}))
    for name in (
        "post", "put", "patch", "delete", "request", "send", "send_once",
        "create_session", "compact", "approve", "stop", "retry",
    ):
        assert not hasattr(t, name), f"公开面不应存在写方法 {name}"
        assert not hasattr(OpenChamberReadTransport, name), f"类公开面不应存在写方法 {name}"
    assert callable(t.get)
    assert "get" in dir(t)


# =====================================================================
# 附加：认证卫生 / 构造细嗅 / 集成
# =====================================================================

def test_token_not_leaked_in_error_or_response_dto():
    token = "TokTopSecret!"
    opener = FakeOpener({"/timeout": socket.timeout("t"), "/conn": ConnectionRefusedError("x")})
    t = OpenChamberReadTransport("http://127.0.0.1:57123", token=token, opener=opener)
    with pytest.raises(OpenChamberReadError) as ctx:
        t.get("/timeout", attach_auth=True)
    assert token not in str(ctx.value)
    with pytest.raises(OpenChamberReadError) as ctx:
        t.get("/conn", attach_auth=True)
    assert token not in str(ctx.value)
    ok_opener = FakeOpener({"/ok": (200, '{"x":1}')})
    t2 = OpenChamberReadTransport("http://127.0.0.1:57123", token=token, opener=ok_opener)
    resp = t2.get("/ok", attach_auth=True)
    assert ok_opener.requests[0]["headers"]["Authorization"] == f"Bearer {token}"
    assert getattr(resp, "token", None) is None


def test_base_url_trailing_slash_stripped():
    before = OpenChamberReadTransport("http://localhost:57123/", opener=FakeOpener({}))
    assert before.base_url == "http://localhost:57123"
    after = OpenChamberReadTransport("http://localhost:57123//", opener=FakeOpener({}))
    assert after.base_url == "http://localhost:57123"


@pytest.mark.parametrize("base", ["http://127.0.0.2:57123", "https://192.0.2.10:443", "http://example.com:80"])
def test_constructor_discards_token_for_non_whitelist(base):
    t = OpenChamberReadTransport(base, token="should-drop", opener=FakeOpener({}))
    assert t._token is None


def test_transport_uses_resolved_token_on_loopback(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"desktopLocalClientToken": "tok-derived"}), encoding="utf-8")
    token = resolve_local_auth_token("http://localhost:57123", env_token=None, settings_path=settings)
    opener = FakeOpener({})
    t = OpenChamberReadTransport("http://localhost:57123", token=token, opener=opener)
    t.get("/api/x", attach_auth=True)
    assert opener.requests[0]["headers"]["Authorization"] == "Bearer tok-derived"


def test_content_type_captured():
    opener = FakeOpener({"/health": (200, '{"status":"ok"}', "text/plain")})
    t = OpenChamberReadTransport("http://localhost:57123", opener=opener)
    resp = t.get("/health", attach_auth=False)
    assert resp.status == 200
    assert resp.content_type == "text/plain"