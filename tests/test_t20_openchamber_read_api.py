"""T20-02：OpenChamber 只读业务 Client 自动测试（全程 fake，0 真实网络）。

注入 Fake opener 到 T20-01 封板的 OpenChamberReadTransport，覆盖卡列 33 项：
endpoint 路径 / attach_auth 精确性、成功 shape 与分类型 missing 语义（[] 与 {}）、
blank directory 输入错误不发送 HTTP、HTTP / JSON 错误分类、transport TIMEOUT /
CONNECTION 原样上抛、公开面无 messages / 写 / 控制方法、无统一 connected / healthy
判定、单次 client 调用 = 单次 transport.get。测试风格对齐 tests 目录既有惯例。
"""

from __future__ import annotations

import io
import json
import socket
import sys
import urllib.error
import urllib.parse
from dataclasses import fields
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from adapters.openchamber import (  # noqa: E402
    KIND_AUTH,
    KIND_CONNECTION,
    KIND_HTTP_ERROR,
    KIND_MALFORMED_JSON,
    KIND_MISSING_INPUT,
    KIND_NOT_FOUND_OR_UNSUPPORTED,
    KIND_TIMEOUT,
    KIND_UNEXPECTED_SHAPE,
    NO_SESSION_OBSERVED_IN_DIRECTORY,
    NO_STATUS_ENTRY_OBSERVED_IN_DIRECTORY,
    OpenChamberApiError,
    OpenChamberObservation,
    OpenChamberReadClient,
    OpenChamberReadError,
    OpenChamberReadTransport,
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


def _http_error(code: int, body: str):
    return urllib.error.HTTPError(
        "http://fake.local/",
        code,
        "mock http error",
        {"Content-Type": "application/json"},
        io.BytesIO(body.encode("utf-8")),
    )


class FakeOpener:
    """本地 fake HTTP 入口：按 path+query 路由，记录每次请求 URL/headers 并计数。

    route 值可为 (status, body) / (status, body, content_type) 或可抛出的 Exception
    （含 HTTPError、socket.timeout、URLError）。
    """

    def __init__(self, routes: dict[str, object], default_status: int = 200, default_body: str = "{}"):
        self.routes = {("/" + str(k).strip("/")): v for k, v in routes.items()}
        self.default_status = default_status
        self.default_body = default_body
        self.requests: list[dict] = []
        self.call_count = 0

    def __call__(self, req):
        self.call_count += 1
        parts = urllib.parse.urlsplit(req.full_url)
        path_query = (parts.path or "").rstrip("/") or "/"
        if parts.query:
            path_query += "?" + parts.query
        self.requests.append(
            {"url": req.full_url, "method": req.get_method(), "headers": {k: v for k, v in req.header_items()}}
        )
        entry = self.routes.get(path_query)
        if entry is None:
            return FakeResponse(self.default_status, self.default_body)
        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, (tuple, list)) and len(entry) == 3:
            status, body, content_type = entry
            return FakeResponse(status, body, content_type=content_type)
        status, body = entry
        return FakeResponse(status, body)


def _make_client(opener: FakeOpener, token: str = "tok"):
    transport = OpenChamberReadTransport("http://127.0.0.1:57123", token=token, opener=opener)
    return OpenChamberReadClient(transport), transport


# =====================================================================
# 01-04：health endpoint
# =====================================================================

def test_health_path_exact():
    opener = FakeOpener({"/health": (200, '{"status":"ok"}')})
    client, transport = _make_client(opener)
    client.health()
    assert opener.requests[0]["url"] == "http://127.0.0.1:57123/health"
    assert opener.requests[0]["method"] == "GET"
    assert transport.n_calls == 1


def test_health_attach_auth_false():
    opener = FakeOpener({"/health": (200, '{"status":"ok"}')})
    client, _ = _make_client(opener)
    client.health()
    assert "Authorization" not in opener.requests[0]["headers"]


def test_health_dict_success():
    opener = FakeOpener({"/health": (200, '{"status":"ok","openchamberVersion":"1.23.0"}')})
    client, _ = _make_client(opener)
    obs = client.health()
    assert isinstance(obs, OpenChamberObservation)
    assert obs.endpoint == "health"
    assert obs.http_status == 200
    assert obs.payload == {"status": "ok", "openchamberVersion": "1.23.0"}
    assert obs.missing_semantics is None


@pytest.mark.parametrize("body", ["[1,2]", '"ok"', "42", "null"])
def test_health_non_dict_shape_rejected(body):
    opener = FakeOpener({"/health": (200, body)})
    client, _ = _make_client(opener)
    with pytest.raises(OpenChamberApiError) as ctx:
        client.health()
    assert ctx.value.kind == KIND_UNEXPECTED_SHAPE
    assert ctx.value.endpoint == "health"


# =====================================================================
# 05-10：list_sessions endpoint
# =====================================================================

def test_list_sessions_path_url_encoded():
    directory = "D:\\AIwork\\ai relay b&v3"
    query = urllib.parse.urlencode({"directory": directory})
    opener = FakeOpener({f"/api/session?{query}": (200, "[]")})
    client, _ = _make_client(opener)
    client.list_sessions(directory)
    assert opener.call_count == 1
    parts = urllib.parse.urlsplit(opener.requests[0]["url"])
    assert parts.path == "/api/session"
    assert parts.query == query
    assert " " not in parts.query  # 空格被 urlencode 编码为 '+'
    assert "%26" in parts.query  # 原始 '&' 必须被编码，禁止手工字符串替换
    assert directory not in opener.requests[0]["url"]


def test_list_sessions_attach_auth_true():
    query = urllib.parse.urlencode({"directory": "d"})
    opener = FakeOpener({f"/api/session?{query}": (200, "[]")})
    client, _ = _make_client(opener)
    client.list_sessions("d")
    assert opener.requests[0]["headers"]["Authorization"] == "Bearer tok"


def test_list_sessions_empty_array_scoped_missing_semantics():
    query = urllib.parse.urlencode({"directory": "d"})
    opener = FakeOpener({f"/api/session?{query}": (200, "[]")})
    client, _ = _make_client(opener)
    obs = client.list_sessions("d")
    assert obs.payload == []
    assert obs.missing_semantics == NO_SESSION_OBSERVED_IN_DIRECTORY


def test_list_sessions_nonempty_preserved():
    query = urllib.parse.urlencode({"directory": "d"})
    payload = [{"sessionId": "s-1", "name": "x"}]
    opener = FakeOpener({f"/api/session?{query}": (200, json.dumps(payload))})
    client, _ = _make_client(opener)
    obs = client.list_sessions("d")
    assert obs.payload == payload
    assert obs.missing_semantics is None


@pytest.mark.parametrize("body", ['{"nope":1}', '{"sessions":[]}'])
def test_list_sessions_non_list_rejected(body):
    query = urllib.parse.urlencode({"directory": "d"})
    opener = FakeOpener({f"/api/session?{query}": (200, body)})
    client, _ = _make_client(opener)
    with pytest.raises(OpenChamberApiError) as ctx:
        client.list_sessions("d")
    assert ctx.value.kind == KIND_UNEXPECTED_SHAPE


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_list_sessions_blank_directory_missing_input_no_http(blank):
    opener = FakeOpener({})
    client, _ = _make_client(opener)
    with pytest.raises(OpenChamberApiError) as ctx:
        client.list_sessions(blank)
    assert ctx.value.kind == KIND_MISSING_INPUT
    assert ctx.value.endpoint == "list_sessions"
    assert opener.call_count == 0


# =====================================================================
# 11-16：session_status endpoint
# =====================================================================

def test_session_status_path_url_encoded():
    directory = "D:\\AIwork\\ai relay b&v3"
    query = urllib.parse.urlencode({"directory": directory})
    opener = FakeOpener({f"/api/session/status?{query}": (200, "{}")})
    client, _ = _make_client(opener)
    client.session_status(directory)
    parts = urllib.parse.urlsplit(opener.requests[0]["url"])
    assert parts.path == "/api/session/status"
    assert parts.query == query
    assert directory not in opener.requests[0]["url"]


def test_session_status_attach_auth_true():
    query = urllib.parse.urlencode({"directory": "d"})
    opener = FakeOpener({f"/api/session/status?{query}": (200, "{}")})
    client, _ = _make_client(opener)
    client.session_status("d")
    assert opener.requests[0]["headers"]["Authorization"] == "Bearer tok"


def test_session_status_empty_dict_scoped_missing_semantics():
    query = urllib.parse.urlencode({"directory": "d"})
    opener = FakeOpener({f"/api/session/status?{query}": (200, "{}")})
    client, _ = _make_client(opener)
    obs = client.session_status("d")
    assert obs.payload == {}
    assert obs.missing_semantics == NO_STATUS_ENTRY_OBSERVED_IN_DIRECTORY


def test_session_status_nonempty_dict_preserved():
    query = urllib.parse.urlencode({"directory": "d"})
    payload = {"status": "running", "sessionId": "s-1"}
    opener = FakeOpener({f"/api/session/status?{query}": (200, json.dumps(payload))})
    client, _ = _make_client(opener)
    obs = client.session_status("d")
    assert obs.payload == payload
    assert obs.missing_semantics is None


def test_session_status_non_dict_rejected():
    query = urllib.parse.urlencode({"directory": "d"})
    opener = FakeOpener({f"/api/session/status?{query}": (200, "[1]")})
    client, _ = _make_client(opener)
    with pytest.raises(OpenChamberApiError) as ctx:
        client.session_status("d")
    assert ctx.value.kind == KIND_UNEXPECTED_SHAPE


@pytest.mark.parametrize("blank", ["", "  "])
def test_session_status_blank_directory_no_http(blank):
    opener = FakeOpener({})
    client, transport = _make_client(opener)
    with pytest.raises(OpenChamberApiError) as ctx:
        client.session_status(blank)
    assert ctx.value.kind == KIND_MISSING_INPUT
    assert ctx.value.endpoint == "session_status"
    assert opener.call_count == 0
    assert transport.n_calls == 0


# =====================================================================
# 17-22：permission_state endpoint
# =====================================================================

def test_permission_exact_path():
    opener = FakeOpener({"/api/permission-auto-accept": (200, '{"sessions":{},"revision":1}')})
    client, _ = _make_client(opener)
    client.permission_state()
    assert opener.requests[0]["url"] == "http://127.0.0.1:57123/api/permission-auto-accept"
    assert opener.requests[0]["method"] == "GET"


def test_permission_attach_auth_true():
    opener = FakeOpener({"/api/permission-auto-accept": (200, '{"sessions":{},"revision":1}')})
    client, _ = _make_client(opener)
    client.permission_state()
    assert opener.requests[0]["headers"]["Authorization"] == "Bearer tok"


def test_permission_sessions_dict_and_int_revision_accepted():
    payload = {"sessions": {"fake-id-1": True, "fake-id-2": False}, "revision": 3}
    opener = FakeOpener({"/api/permission-auto-accept": (200, json.dumps(payload))})
    client, _ = _make_client(opener)
    obs = client.permission_state()
    assert obs.payload == payload
    assert obs.http_status == 200
    assert obs.missing_semantics is None


def test_permission_non_bool_value_rejected():
    payload = {"sessions": {"fake-id-1": "yes"}, "revision": 1}
    opener = FakeOpener({"/api/permission-auto-accept": (200, json.dumps(payload))})
    client, _ = _make_client(opener)
    with pytest.raises(OpenChamberApiError) as ctx:
        client.permission_state()
    assert ctx.value.kind == KIND_UNEXPECTED_SHAPE
    assert ctx.value.endpoint == "permission_state"
    assert "fake-id-1" not in str(ctx.value)  # 异常正文不得暴露 session ID


@pytest.mark.parametrize("body", ['{"revision":1}', '{"sessions":"x","revision":1}'])
def test_permission_missing_or_non_dict_sessions_rejected(body):
    opener = FakeOpener({"/api/permission-auto-accept": (200, body)})
    client, _ = _make_client(opener)
    with pytest.raises(OpenChamberApiError) as ctx:
        client.permission_state()
    assert ctx.value.kind == KIND_UNEXPECTED_SHAPE


@pytest.mark.parametrize("body", ['{"sessions":{}}', '{"sessions":{},"revision":1.5}', '{"sessions":{},"revision":"1"}', '{"sessions":{},"revision":true}'])
def test_permission_missing_or_invalid_revision_rejected(body):
    opener = FakeOpener({"/api/permission-auto-accept": (200, body)})
    client, _ = _make_client(opener)
    with pytest.raises(OpenChamberApiError) as ctx:
        client.permission_state()
    assert ctx.value.kind == KIND_UNEXPECTED_SHAPE


# =====================================================================
# 23-27：HTTP / JSON 错误分类
# =====================================================================

@pytest.mark.parametrize("code", [401, 403])
def test_http_401_403_map_auth(code):
    query = urllib.parse.urlencode({"directory": "d"})
    opener = FakeOpener({f"/api/session?{query}": _http_error(code, '{"error":"no auth"}')})
    client, _ = _make_client(opener)
    with pytest.raises(OpenChamberApiError) as ctx:
        client.list_sessions("d")
    assert ctx.value.kind == KIND_AUTH
    assert ctx.value.status == code
    assert '{"error":"no auth"}' not in str(ctx.value)  # 不带 response body


def test_http_404_maps_not_found_or_unsupported():
    query = urllib.parse.urlencode({"directory": "d"})
    opener = FakeOpener({f"/api/session?{query}": _http_error(404, '{"error":"nf"}')})
    client, _ = _make_client(opener)
    with pytest.raises(OpenChamberApiError) as ctx:
        client.list_sessions("d")
    assert ctx.value.kind == KIND_NOT_FOUND_OR_UNSUPPORTED
    assert ctx.value.status == 404


def test_http_500_maps_http_error():
    opener = FakeOpener({"/health": _http_error(500, '{"error":"boom"}')})
    client, _ = _make_client(opener)
    with pytest.raises(OpenChamberApiError) as ctx:
        client.health()
    assert ctx.value.kind == KIND_HTTP_ERROR
    assert ctx.value.status == 500


def test_malformed_json_maps_malformed_json():
    opener = FakeOpener({"/health": (200, "{not json")})
    client, _ = _make_client(opener)
    with pytest.raises(OpenChamberApiError) as ctx:
        client.health()
    assert ctx.value.kind == KIND_MALFORMED_JSON
    assert ctx.value.status == 200
    assert ctx.value.endpoint == "health"
    assert "not json" not in str(ctx.value)


@pytest.mark.parametrize("body", ["", "   ", "<html></html>"])
def test_malformed_json_various_bodies(body):
    opener = FakeOpener({"/health": (200, body)})
    client, _ = _make_client(opener)
    with pytest.raises(OpenChamberApiError) as ctx:
        client.health()
    assert ctx.value.kind == KIND_MALFORMED_JSON


# =====================================================================
# 28-29：transport 错误原样上抛（不改写成无 session / 不吞掉）
# =====================================================================

def test_transport_timeout_preserved():
    opener = FakeOpener({"/health": socket.timeout("timed out")})
    client, _ = _make_client(opener)
    with pytest.raises(OpenChamberReadError) as ctx:
        client.health()
    assert ctx.value.kind == KIND_TIMEOUT
    assert not isinstance(ctx.value, OpenChamberApiError)


def test_transport_connection_preserved():
    opener = FakeOpener({"/health": urllib.error.URLError(OSError("refused"))})
    client, _ = _make_client(opener)
    with pytest.raises(OpenChamberReadError) as ctx:
        client.health()
    assert ctx.value.kind == KIND_CONNECTION
    assert not isinstance(ctx.value, OpenChamberApiError)


@pytest.mark.parametrize("exc", [socket.timeout("t"), urllib.error.URLError(OSError("refused"))])
def test_transport_error_not_rewritten_into_empty_observation(exc):
    query = urllib.parse.urlencode({"directory": "d"})
    opener = FakeOpener({f"/api/session?{query}": exc})
    client, _ = _make_client(opener)
    with pytest.raises(OpenChamberReadError) as ctx:
        client.list_sessions("d")
    assert ctx.value.kind in (KIND_TIMEOUT, KIND_CONNECTION)


# =====================================================================
# 30-33：公开面安全 / 计数 / 无综合判定
# =====================================================================

def test_no_messages_public_method():
    assert not hasattr(OpenChamberReadClient, "messages")
    client, _ = _make_client(FakeOpener({}))
    for name in ("messages", "list_messages", "session_messages", "get_messages"):
        assert not hasattr(client, name), f"公开面不应存在 messages 方法 {name}"


def test_no_write_or_control_public_methods():
    client, _ = _make_client(FakeOpener({}))
    for name in (
        "send", "send_once", "post", "create_session", "rotate_session",
        "stop", "retry", "approve", "reject", "compact",
    ):
        assert not hasattr(OpenChamberReadClient, name), f"类公开面不应存在 {name}"
        assert not hasattr(client, name), f"公开面不应存在写/控制方法 {name}"


def test_no_unified_connected_healthy_result():
    assert {f.name for f in fields(OpenChamberObservation)} == {
        "endpoint", "http_status", "payload", "missing_semantics",
    }
    client, _ = _make_client(FakeOpener({}))
    for name in (
        "connected", "healthy", "everything_ok",
        "api_authenticated", "session_exists", "execution_progressing",
    ):
        assert not hasattr(client, name), f"客户端不应输出综合判定属性 {name}"


def test_one_client_call_one_transport_get():
    opener = FakeOpener({"/health": (200, '{"status":"ok"}')})
    client, transport = _make_client(opener)
    obs = client.health()
    assert obs.http_status == 200
    assert transport.n_calls == 1
    assert opener.call_count == 1