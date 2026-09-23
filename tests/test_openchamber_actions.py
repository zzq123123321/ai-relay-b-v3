"""openchamber_client 会话动作测试（全内存假接口，不打真实服务）。

覆盖交付1 resolve_execution_config + 交付2 send_text + 交付3 compact_session：
  1.  最近完整 assistant 配置正确解析（按 time.created 取最新）
  2.  跳过不完整 assistant，找到前一个完整配置
  3.  variant 缺失 → None（正常）
  4.  无可用配置（消息 + 会话对象都无）→ None（unavailable）
  5.  send_text body 精确
  6.  send_text 204/200 → accepted
  7.  send_text HTTP/连接失败 → 稳定错误，不抛异常
  8.  compact body 精确
  9.  compact 200 + true → success
  10. compact false / HTTP error → failure
  11. 回退来源：会话对象 agent/model → source="session"
  12. send_text 配置不可用 → accepted=False（不猜模型）
"""

import io
import json
import socket
import urllib.error

import pytest

from openchamber_client import (
    CompactResult,
    ExecutionConfig,
    OpenChamberClient,
    SendResult,
    SessionStatusResult,
    TaskProgressResult,
)


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self.body = body

    def read(self, size: int = -1) -> bytes:
        return self.body if size is None or size < 0 else self.body[:size]

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> None:
        return None


def _msg(role: str, **info) -> dict:
    """构造一条消息；info 字段按需给（agent/providerID/modelID/variant/time）。"""
    full = {"id": "m", "sessionID": "ses", "role": role}
    full.update(info)
    return {"info": full, "parts": [{"type": "text", "text": "x"}]}


def _assistant(agent="build", provider="4090", model="qwen3.8-27b", variant=None, created=None) -> dict:
    info = {}
    if created is not None:
        info["time"] = {"created": created}
    if agent is not None:
        info["agent"] = agent
    if provider is not None:
        info["providerID"] = provider
    if model is not None:
        info["modelID"] = model
    if variant is not None:
        info["variant"] = variant
    return _msg("assistant", **info)


def _client(monkeypatch, tmp_path, routes):
    """routes: 按顺序 (url 子串, handler(request))；handler 返回 FakeResponse 或 raise。"""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"desktopLocalClientToken": "tok"}), encoding="utf-8")
    client = OpenChamberClient("http://127.0.0.1:57123", settings_path=settings, token=None)
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append((request.get_method(), request.full_url, request.data))
        for sub, handler in routes:
            if sub in request.full_url:
                return handler(request)
        raise AssertionError(f"unrouted: {request.full_url}")

    monkeypatch.setattr("openchamber_client.urllib.request.urlopen", fake_urlopen)
    return client, calls


def _json_body(body) -> bytes:
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


# --- 交付1：resolve_execution_config -------------------------------------


def test_latest_complete_assistant_parsed(monkeypatch, tmp_path):
    messages = [
        _msg("user"),
        _assistant(model=None, created=50),  # 不完整（缺 model）
        _assistant(model="m1", created=100),  # 完整，variant 缺失
        _assistant(model="m2", variant="平均", created=200),  # 最新完整
    ]
    client, _ = _client(monkeypatch, tmp_path, [("/message", lambda r: FakeResponse(200, _json_body(messages)))])
    cfg = client.resolve_execution_config("ses_x", None)
    assert isinstance(cfg, ExecutionConfig)
    assert cfg.source == "assistant"
    assert cfg.agent == "build"
    assert cfg.provider_id == "4090"
    assert cfg.model_id == "m2"
    assert cfg.variant == "平均"


def test_skip_incomplete_to_earlier_complete(monkeypatch, tmp_path):
    messages = [
        _assistant(model="m1", created=100),  # 完整
        _assistant(model=None, created=200),  # 最新但不完整
    ]
    client, _ = _client(monkeypatch, tmp_path, [("/message", lambda r: FakeResponse(200, _json_body(messages)))])
    cfg = client.resolve_execution_config("ses_x", None)
    assert cfg is not None
    assert cfg.model_id == "m1"
    assert cfg.variant is None


def test_variant_absent_is_none(monkeypatch, tmp_path):
    messages = [_assistant(model="qwen3.8-27b", created=100)]  # 无 variant 键
    client, _ = _client(monkeypatch, tmp_path, [("/message", lambda r: FakeResponse(200, _json_body(messages)))])
    cfg = client.resolve_execution_config("ses_x", None)
    assert cfg is not None
    assert cfg.variant is None
    assert cfg.source == "assistant"


def test_no_config_returns_none(monkeypatch, tmp_path):
    # 消息只有 user，会话对象也无 agent/model → unavailable
    routes = [
        ("/message", lambda r: FakeResponse(200, _json_body([_msg("user")]))),
        ("/api/session/ses_x", lambda r: FakeResponse(200, _json_body({}))),
    ]
    client, _ = _client(monkeypatch, tmp_path, routes)
    assert client.resolve_execution_config("ses_x", None) is None


def test_fallback_to_session_object(monkeypatch, tmp_path):
    # 消息无完整 assistant，但会话对象提供 agent + model{id, providerID, variant}
    routes = [
        ("/message", lambda r: FakeResponse(200, _json_body([_msg("user")]))),
        ("/api/session/ses_x", lambda r: FakeResponse(200, _json_body(
            {"agent": "plan", "model": {"id": "m9", "providerID": "4090", "variant": "高"}}
        ))),
    ]
    client, _ = _client(monkeypatch, tmp_path, routes)
    cfg = client.resolve_execution_config("ses_x", None)
    assert isinstance(cfg, ExecutionConfig)
    assert cfg.source == "session"
    assert cfg.agent == "plan"
    assert cfg.provider_id == "4090"
    assert cfg.model_id == "m9"
    assert cfg.variant == "高"


# --- 交付2：send_text -----------------------------------------------------


def _messages_route(variant=None, model="qwen3.8-27b"):
    return ("/message", lambda r: FakeResponse(200, _json_body([_assistant(model=model, variant=variant, created=1)])))


def test_send_text_body_exact(monkeypatch, tmp_path):
    monkeypatch.setattr(OpenChamberClient, "_new_message_id", lambda self: "msg_fixed")
    client, calls = _client(monkeypatch, tmp_path, [
        _messages_route(),
        ("/prompt_async", lambda r: FakeResponse(204, b"")),
    ])
    result = client.send_text("ses_x", "D:/work", "hello 世界")
    assert isinstance(result, SendResult)
    assert result.accepted is True
    assert result.error is None
    assert result.message_id == "msg_fixed"
    # 定位 POST 请求，校验 body 精确
    post = next(c for c in calls if c[0] == "POST" and "/prompt_async" in c[1])
    assert "directory=D%3A/work" in post[1]
    body = json.loads(post[2].decode("utf-8"))
    assert body == {
        "messageID": "msg_fixed",
        "model": {"providerID": "4090", "modelID": "qwen3.8-27b"},
        "agent": "build",
        "variant": None,
        "parts": [{"type": "text", "text": "hello 世界"}],
    }


def test_send_text_2xx_accepted(monkeypatch, tmp_path):
    for status in (204, 200, 202):
        client, _ = _client(monkeypatch, tmp_path, [
            _messages_route(),
            ("/prompt_async", lambda r, s=status: FakeResponse(s, b"")),
        ])
        result = client.send_text("ses_x", None, "ping")
        assert result.accepted is True
        assert result.error is None


def test_send_text_http_error_stable(monkeypatch, tmp_path):
    def raise_400(request):
        raise urllib.error.HTTPError(request.full_url, 400, "bad", {}, io.BytesIO(b""))

    client, _ = _client(monkeypatch, tmp_path, [_messages_route(), ("/prompt_async", raise_400)])
    result = client.send_text("ses_x", None, "ping")
    assert result.accepted is False
    assert result.error == "http: 400"
    assert result.message_id is None


def test_send_text_connection_failure_stable(monkeypatch, tmp_path):
    def raise_conn(request):
        raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

    client, _ = _client(monkeypatch, tmp_path, [_messages_route(), ("/prompt_async", raise_conn)])
    result = client.send_text("ses_x", None, "ping")
    assert result.accepted is False
    assert result.error.startswith("connection:")


def test_send_text_timeout_stable(monkeypatch, tmp_path):
    def raise_timeout(request):
        raise socket.timeout("timed out")

    client, _ = _client(monkeypatch, tmp_path, [_messages_route(), ("/prompt_async", raise_timeout)])
    result = client.send_text("ses_x", None, "ping")
    assert result.accepted is False
    assert result.error == "timeout"


def test_send_text_unavailable_config(monkeypatch, tmp_path):
    routes = [
        ("/message", lambda r: FakeResponse(200, _json_body([_msg("user")]))),
        ("/api/session/ses_x", lambda r: FakeResponse(200, _json_body({}))),
        ("/prompt_async", lambda r: FakeResponse(204, b"")),  # 不应被调用
    ]
    client, calls = _client(monkeypatch, tmp_path, routes)
    result = client.send_text("ses_x", None, "ping")
    assert result.accepted is False
    assert result.error is not None and "unavailable" in result.error
    assert not any("/prompt_async" in url for _m, url, _d in calls)


# --- 交付3：compact_session ----------------------------------------------


def test_compact_body_exact(monkeypatch, tmp_path):
    client, calls = _client(monkeypatch, tmp_path, [
        _messages_route(variant="平均"),
        ("/summarize", lambda r: FakeResponse(200, b"true")),
    ])
    result = client.compact_session("ses_x", "D:/work")
    assert isinstance(result, CompactResult)
    assert result.success is True
    assert result.error is None
    post = next(c for c in calls if c[0] == "POST" and "/summarize" in c[1])
    assert "directory=D%3A/work" in post[1]
    body = json.loads(post[2].decode("utf-8"))
    assert body == {"providerID": "4090", "modelID": "qwen3.8-27b"}


def test_compact_200_true_success(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, [
        _messages_route(),
        ("/summarize", lambda r: FakeResponse(200, b"true")),
    ])
    assert client.compact_session("ses_x", None).success is True


def test_compact_200_false_failure(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, [
        _messages_route(),
        ("/summarize", lambda r: FakeResponse(200, b"false")),
    ])
    result = client.compact_session("ses_x", None)
    assert result.success is False
    assert result.error.startswith("body not true")


def test_compact_http_error_failure(monkeypatch, tmp_path):
    def raise_500(request):
        raise urllib.error.HTTPError(request.full_url, 500, "ise", {}, io.BytesIO(b""))

    client, _ = _client(monkeypatch, tmp_path, [_messages_route(), ("/summarize", raise_500)])
    result = client.compact_session("ses_x", None)
    assert result.success is False
    assert result.error == "http: 500"


def test_compact_malformed_body_failure(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, [
        _messages_route(),
        ("/summarize", lambda r: FakeResponse(200, b"not-json")),
    ])
    result = client.compact_session("ses_x", None)
    assert result.success is False
    assert result.error.startswith("malformed:")


# --- 交付4：只读 watchdog API --------------------------------------------


# --- A. session status ---
def test_session_status_values(monkeypatch, tmp_path):
    for status in ("busy", "retry", "idle"):
        client, _ = _client(
            monkeypatch, tmp_path,
            [("/status", lambda r, s=status: FakeResponse(200, _json_body({"status": s})))],
        )
        result = client.get_session_status("ses_x")
        assert isinstance(result, SessionStatusResult)
        assert result.ok is True
        assert result.status == status
        assert result.error is None


def test_session_status_404_not_idle(monkeypatch, tmp_path):
    def raise_404(request):
        raise urllib.error.HTTPError(request.full_url, 404, "nf", {}, io.BytesIO(b""))

    client, _ = _client(monkeypatch, tmp_path, [("/status", raise_404)])
    result = client.get_session_status("ses_x")
    assert result.ok is False
    assert result.status is None
    assert result.error == "http: 404"


def test_session_status_transport_failure_not_idle(monkeypatch, tmp_path):
    def raise_conn(request):
        raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

    client, _ = _client(monkeypatch, tmp_path, [("/status", raise_conn)])
    result = client.get_session_status("ses_x")
    assert result.ok is False
    assert result.status is None
    assert result.error.startswith("connection:")


def test_session_status_malformed(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, [("/status", lambda r: FakeResponse(200, b"{not-json"))])
    result = client.get_session_status("ses_x")
    assert result.ok is False
    assert result.status is None
    assert result.error.startswith("malformed:")


# --- B. task progress marker ---
def _progress_messages(assistant_text="hello"):
    return [
        {"info": {"id": "u1", "role": "user"}, "parts": [{"type": "text", "text": "q"}]},
        {"info": {"id": "a1", "role": "assistant"}, "parts": [{"type": "text", "text": assistant_text}]},
    ]


def test_task_progress_marker_found(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, [("/message", lambda r: FakeResponse(200, _json_body(_progress_messages())))])
    result = client.get_task_progress("ses_x", None, "u1")
    assert isinstance(result, TaskProgressResult)
    assert result.read_ok is True
    assert result.user_message_found is True
    assert result.marker


def test_task_progress_marker_changes_on_content(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path, [("/message", lambda r: FakeResponse(200, _json_body(_progress_messages("v1"))))])
    m1 = client.get_task_progress("ses_x", None, "u1").marker
    client, _ = _client(monkeypatch, tmp_path, [("/message", lambda r: FakeResponse(200, _json_body(_progress_messages("v2"))))])
    m2 = client.get_task_progress("ses_x", None, "u1").marker
    assert m1 != m2


def test_task_progress_get_failure_read_ok_false(monkeypatch, tmp_path):
    def raise_conn(request):
        raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

    client, _ = _client(monkeypatch, tmp_path, [("/message", raise_conn)])
    result = client.get_task_progress("ses_x", None, "u1")
    assert result.read_ok is False
    assert result.marker is None
    assert result.user_message_found is False
    assert result.error.startswith("connection:")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))