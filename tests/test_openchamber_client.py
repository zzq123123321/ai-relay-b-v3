"""openchamber_client 单元测试（假接口，不打真实服务）。

覆盖：probe success / latency>=0 / connection failure / timeout /
HTTP error / malformed response / 任何情况下不抛异常；
resolve_execution_config / send_text 对内部摘要（summary/compaction）
配置的来源过滤与安全回退（v1.13 §14）。
"""

import io
import json
import socket
import urllib.error

import pytest

from openchamber_client import (
    ExecutionConfig,
    ModelTarget,
    OpenChamberClient,
    ProbeResult,
    is_loopback_base_url,
    resolve_local_token,
)

def test_modern_openchamber_send_and_bound_result(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    session_id = "ses_modern"
    directory = "D:/modern"
    prompt = "原文 保留\n换行"
    calls = []
    sent = False

    def fake_http(url, *, data=None):
        nonlocal sent
        if url.endswith("/api/session/ses_modern/message") or "/message?" in url:
            return 200, b'{"data":[]}', None
        if url.endswith("/api/session/ses_modern"):
            return 200, json.dumps({"data": {"agent": "build", "model": {"providerID": "local", "id": "qwen"}}}).encode(), None
        if url.endswith("/api/session/ses_modern/message/msg_answer?directory=D%3A/modern"):
            return 200, b'{"data":{"finish":"stop"}}', None
        assert url.endswith("/api/openchamber/control")
        request = json.loads(data)
        action, inputs = request["action"], request["input"]
        calls.append((action, inputs))
        if action == "session.status":
            return 200, b'{"sessionStatus":{"type":"idle"}}', None
        if action == "session.send":
            assert inputs["prompt"] == prompt
            assert inputs["model"] == "local/qwen"
            assert inputs["directory"] == directory
            sent = True
            return 200, b'{"promptDispatched":true}', None
        if inputs.get("all"):
            messages = [{"id": "msg_new", "role": "user", "text": prompt, "createdAt": 100},
                        {"id": "msg_answer", "role": "assistant", "text": "完成", "createdAt": 120, "completedAt": 160}]
            return 200, json.dumps({"messages": messages, "sessionStatus": {"type": "idle"}}).encode(), None
        users = [{"id": "msg_new", "text": prompt, "createdAt": 100}] if sent else [{"id": "msg_old", "text": "旧", "createdAt": 20}]
        return 200, json.dumps({"messages": users}).encode(), None

    monkeypatch.setattr(client, "_http", fake_http)
    config = client.resolve_execution_config(session_id, directory)
    assert config.model_id == "qwen" and client._modern_api is True
    result = client.send_text(session_id, directory, prompt)
    assert result.accepted and result.message_id == "msg_new"
    assert client.get_task_progress(session_id, directory, "msg_new").user_message_found
    final = client.get_task_result(session_id, directory, "msg_new")
    assert final.read_ok and final.complete and final.text == "完成"
    assert final.first_response_ms == 20
    assert [action for action, _ in calls].count("session.send") == 1

def test_modern_dispatch_uncertain_never_claims_accepted(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    monkeypatch.setattr("openchamber_client.time.sleep", lambda _: None)

    def fake_http(url, *, data=None):
        if "/api/session/ses_x/message?" in url:
            return 200, b'{"data":[]}', None
        if url.endswith("/api/session/ses_x"):
            return 200, b'{"data":{"agent":"build","model":{"providerID":"local","id":"qwen"}}}', None
        request = json.loads(data)
        action = request["action"]
        if action == "session.status":
            return 200, b'{"sessionStatus":{"type":"idle"}}', None
        if action == "session.messages":
            return 200, b'{"messages":[]}', None
        if action == "session.send":
            return None, b"", "timeout"
        raise AssertionError(url)

    monkeypatch.setattr(client, "_http", fake_http)
    result = client.send_text("ses_x", "D:/modern", "正文")
    assert result.accepted is False
    assert result.error.startswith("uncertain:")

@pytest.mark.parametrize("dispatch_error", [None, "timeout"])
def test_modern_send_confirms_late_user_message_without_resending(monkeypatch, tmp_path, dispatch_error):
    client = make_client(monkeypatch, tmp_path)
    monkeypatch.setattr("openchamber_client.time.sleep", lambda _: None)
    counts = {"send": 0, "read": 0}

    def control(action, inputs):
        if action == "session.status":
            return {"sessionStatus": {"type": "idle"}}, None
        if action == "session.send":
            counts["send"] += 1
            return ({"promptDispatched": True}, None) if dispatch_error is None else (None, dispatch_error)
        counts["read"] += 1
        if counts["read"] < 4:
            return {"messages": [{"id": "old", "text": "之前"}]}, None
        return {"messages": [{"id": "new", "text": "正文"}]}, None

    monkeypatch.setattr(client, "_modern_control", control)
    result = client._modern_send("ses_x", "D:/modern", "正文", ExecutionConfig("build", "local", "qwen", None, "session"))

    assert result.accepted and result.message_id == "new"
    assert counts == {"send": 1, "read": 4}

@pytest.mark.parametrize("users, expected", [
    ([{"id": "old", "role": "user", "text": "之前"},
      {"id": "new", "role": "user", "text": "正文"}], "new"),
    ([{"id": "old", "role": "user", "text": "之前"},
      {"id": "new", "role": "user", "text": "\r\n正文\r\n"}], "new"),
    ([{"id": "old", "role": "user", "text": "之前"},
      {"id": "other", "role": "user", "text": "别人的任务"},
      {"id": "new", "role": "user", "text": "正文"}], None),
    ([{"id": "old", "role": "user", "text": "之前"},
      {"id": "new", "role": "user", "text": "不匹配"}], None),
    ([{"id": "new", "role": "user", "text": "正文"}], None),
])
def test_confirm_uncertain_submission_requires_unique_user_after_baseline(monkeypatch, tmp_path, users, expected):
    client = make_client(monkeypatch, tmp_path)
    monkeypatch.setattr(client, "_modern_messages", lambda session_id, directory: (
        {"messages": users}, None
    ))
    assert client.confirm_uncertain_submission("ses_x", "D:/modern", "正文", "old") == expected

def test_modern_send_confirms_server_trimmed_crlf_without_changing_prompt(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    original = "\r\nSOURCE: CHATGPT\r\nCONTENT:\r\n正文 保留\r\n"
    calls = []

    def control(action, inputs):
        calls.append((action, inputs))
        if action == "session.status":
            return {"sessionStatus": {"type": "idle"}}, None
        if action == "session.send":
            return {"promptDispatched": True}, None
        if len([name for name, _ in calls if name == "session.messages"]) == 1:
            return {"messages": [{"id": "old", "text": "之前"}]}, None
        return {"messages": [{"id": "new", "text": original.strip().replace("\r\n", "\n")}]}, None

    monkeypatch.setattr(client, "_modern_control", control)
    result = client._modern_send("ses_x", "D:/modern", original, ExecutionConfig("build", "local", "qwen", None, "session"))
    assert result.accepted and result.message_id == "new"
    assert [inputs["prompt"] for name, inputs in calls if name == "session.send"] == [original]

@pytest.mark.parametrize("payload, expected", [
    ({"data": {"id": "ses_x", "location": {"directory": "D:/workspace"}}}, "D:/workspace"),
    ({"data": {"id": "other", "location": {"directory": "D:/workspace"}}}, None),
    ({"data": {"id": "ses_x", "location": {}}}, None),
    ({"data": {"id": "ses_x", "location": {"directory": "   "}}}, None),
])
def test_session_directory_only_from_matching_server_session(monkeypatch, tmp_path, payload, expected):
    client = make_client(monkeypatch, tmp_path)
    monkeypatch.setattr(client, "_http", lambda url, **kwargs: (
        (200, json.dumps(payload).encode(), None) if url.endswith("/api/session/ses_x")
        else pytest.fail("unexpected URL")
    ))
    assert client.resolve_session_directory("ses_x") == expected

def test_session_directory_read_failure_returns_none(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    monkeypatch.setattr(client, "_http", lambda *args, **kwargs: (400, b"{}", "http: 400"))
    assert client.resolve_session_directory("ses_x") is None

@pytest.mark.parametrize("state, baseline, landed, expected", [
    ({"sessionStatus": {"type": "unknown"}}, None, None, "unavailable:"),
    ({"sessionStatus": {"type": "idle"}}, {"messages": [None]}, None, "malformed:"),
    ({"sessionStatus": {"type": "idle"}}, {"messages": [{"id": "old"}]},
     {"messages": [{"id": "other", "text": "别人的消息"}]}, "uncertain:"),
    ({"sessionStatus": {"type": "idle"}}, {"messages": [{"id": "old"}]},
     {"messages": [None]}, "uncertain:"),
])
def test_modern_send_rejects_unverified_identity(monkeypatch, tmp_path, state, baseline, landed, expected):
    client = make_client(monkeypatch, tmp_path)
    calls = []

    def control(action, inputs):
        calls.append(action)
        if action == "session.status":
            return state, None
        if action == "session.messages":
            return (baseline if len(calls) == 2 else landed), None
        return {"promptDispatched": True}, None

    monkeypatch.setattr(client, "_modern_control", control)
    result = client._modern_send("ses_x", "D:/modern", "正文", ExecutionConfig("build", "local", "qwen", None, "session"))
    assert result.accepted is False and result.error.startswith(expected)
    assert calls.count("session.send") == (1 if landed is not None else 0)

@pytest.mark.parametrize("tail, status, expected", [
    ([{"id": "other", "role": "user"}, {"id": "answer", "role": "assistant", "text": "别人的答复", "completedAt": 160}], "idle", "ambiguous"),
    ([{"id": "answer", "role": "assistant", "text": "未完成", "completedAt": None}], "idle", "pending"),
    ([{"id": "answer", "role": "assistant", "text": "完成", "completedAt": 160}], "busy", "pending"),
])
def test_modern_result_never_claims_other_or_incomplete_answer(monkeypatch, tmp_path, tail, status, expected):
    client = make_client(monkeypatch, tmp_path)
    monkeypatch.setattr(client, "_modern_messages", lambda *args: (
        {"messages": [{"id": "ours", "role": "user", "createdAt": 100}, *tail], "sessionStatus": {"type": status}}, None
    ))
    monkeypatch.setattr(client, "_http", lambda *args, **kwargs: pytest.fail("不得读取其他任务或未完成答复"))
    result = client._modern_result("ses_x", "D:/modern", "ours")
    assert result.read_ok and not result.complete and result.text is None
    assert result.ambiguous == (expected == "ambiguous")

def test_modern_result_failed_finish_is_not_success(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path)
    monkeypatch.setattr(client, "_modern_messages", lambda *args: (
        {"messages": [
            {"id": "ours", "role": "user", "createdAt": 100},
            {"id": "answer", "role": "assistant", "text": "部分内容", "createdAt": 120, "completedAt": 160},
        ], "sessionStatus": {"type": "idle"}}, None
    ))
    monkeypatch.setattr(client, "_http", lambda *args, **kwargs: (200, b'{"data":{"finish":"error"}}', None))
    result = client._modern_result("ses_x", "D:/modern", "ours")
    assert result.read_ok and not result.complete and result.interrupted


class FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict | None = None) -> None:
        self.status = status
        self.body = body
        self.headers = headers or {"Content-Type": "application/json"}

    def read(self, size: int = -1) -> bytes:
        return self.body if size is None or size < 0 else self.body[:size]

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> None:
        return None


def make_client(monkeypatch, tmp_path, **kwargs) -> OpenChamberClient:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"desktopLocalClientToken": "tok-123"}), encoding="utf-8")
    kwargs.setdefault("settings_path", settings)
    kwargs.setdefault("token", None)
    return OpenChamberClient("http://127.0.0.1:57123", **kwargs)

@pytest.mark.parametrize("base_url, monitored_url, expected", [
    ("http://192.168.100.190:8080/v1/", "http://192.168.100.190:8080/v1", ModelTarget(True, "http://192.168.100.190:8080/v1")),
    ("http://127.0.0.1:18081/v1", "http://127.0.0.1:18081/v1", ModelTarget(True, "http://127.0.0.1:18081/v1")),
    ("http://localhost:9000/v1", "http://localhost:9000/v1", ModelTarget(True, "http://localhost:9000/v1")),
    ("http://192.168.100.190:20128/v1", "http://192.168.100.190:8080/v1", ModelTarget(False, "http://192.168.100.190:20128/v1")),
    ("http://127.0.0.1:18081/v1", "http://192.168.100.190:8080/v1", ModelTarget(False, "http://127.0.0.1:18081/v1")),
    ("https://api.example.com/v1", "http://192.168.100.190:8080/v1", ModelTarget(False, "https://api.example.com/v1")),
    (None, "http://192.168.100.190:8080/v1", ModelTarget(False, None)),
    ("file:///etc/passwd", "http://192.168.100.190:8080/v1", None),
    ("http://", "http://192.168.100.190:8080/v1", None),
])
def test_selected_model_target_uses_selected_provider_address(monkeypatch, tmp_path, base_url, monitored_url, expected):
    client = make_client(monkeypatch, tmp_path)
    calls = []
    def fake_http(url, **kwargs):
        calls.append(url)
        return 200, json.dumps({"data": [
            {"id": "other", "settings": {"baseURL": "http://127.0.0.1:9000"}},
            {"id": "chosen", "settings": {"baseURL": base_url}},
        ]}).encode(), None
    monkeypatch.setattr(client, "_http", fake_http)
    config = ExecutionConfig("build", "chosen", "model", None, "assistant")
    assert client.selected_model_target("session", "D:/work tree", monitored_url, config) == expected
    assert calls == ["http://127.0.0.1:57123/api/provider?directory=D%3A/work%20tree"]

@pytest.mark.parametrize("response", [(503, b""), (200, b"bad"), (200, b'{"data":[]}')])
def test_selected_model_target_unknown_does_not_guess(monkeypatch, tmp_path, response):
    client = make_client(monkeypatch, tmp_path)
    monkeypatch.setattr(client, "_http", lambda *args, **kwargs: (*response, None))
    config = ExecutionConfig("build", "missing", "model", None, "assistant")
    assert client.selected_model_target("session", None, "http://192.168.100.190:8080/v1", config) is None


def test_probe_success(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=None):
        assert request.full_url == "http://127.0.0.1:57123/health"
        assert request.get_header("Authorization") == "Bearer tok-123"
        return FakeResponse(200, json.dumps({"status": "ok", "openchamberVersion": "1.24.2"}).encode())

    monkeypatch.setattr("openchamber_client.urllib.request.urlopen", fake_urlopen)
    client = make_client(monkeypatch, tmp_path)
    result = client.probe()
    assert isinstance(result, ProbeResult)
    assert result.connected is True
    assert result.error is None
    assert result.latency_ms is not None
    assert result.latency_ms >= 0


def test_probe_connection_refused(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr("openchamber_client.urllib.request.urlopen", fake_urlopen)
    result = make_client(monkeypatch, tmp_path).probe()
    assert result.connected is False
    assert result.error.startswith("connection:")
    assert result.latency_ms >= 0


def test_probe_timeout_not_wrapped(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=None):
        raise socket.timeout("timed out")

    monkeypatch.setattr("openchamber_client.urllib.request.urlopen", fake_urlopen)
    result = make_client(monkeypatch, tmp_path).probe()
    assert result.connected is False
    assert result.error == "timeout"
    assert result.latency_ms >= 0


def test_probe_timeout_wrapped_in_urlerror(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(socket.timeout("timed out"))

    monkeypatch.setattr("openchamber_client.urllib.request.urlopen", fake_urlopen)
    result = make_client(monkeypatch, tmp_path).probe()
    assert result.connected is False
    assert result.error == "timeout"


def test_probe_http_error(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            "http://127.0.0.1:57123/health", 404, "not found", {}, io.BytesIO(b"")
        )

    monkeypatch.setattr("openchamber_client.urllib.request.urlopen", fake_urlopen)
    result = make_client(monkeypatch, tmp_path).probe()
    assert result.connected is False
    assert result.error == "http: 404"


def test_probe_malformed_json(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "openchamber_client.urllib.request.urlopen",
        lambda request, timeout=None: FakeResponse(200, b"not json at all"),
    )
    result = make_client(monkeypatch, tmp_path).probe()
    assert result.connected is False
    assert result.error.startswith("malformed:")


def test_probe_malformed_non_object(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "openchamber_client.urllib.request.urlopen",
        lambda request, timeout=None: FakeResponse(200, b"[1, 2, 3]"),
    )
    result = make_client(monkeypatch, tmp_path).probe()
    assert result.connected is False
    assert result.error.startswith("malformed:")


def test_probe_no_token_still_succeeds(monkeypatch, tmp_path):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["auth"] = request.get_header("Authorization")
        return FakeResponse(200, b'{"status": "ok"}')

    monkeypatch.setattr("openchamber_client.urllib.request.urlopen", fake_urlopen)
    # 无 settings 文件、无显式 token → 不带 Authorization 头
    client = OpenChamberClient("http://127.0.0.1:57123", settings_path=tmp_path / "missing.json")
    result = client.probe()
    assert result.connected is True
    assert seen["auth"] is None


def test_non_loopback_does_not_read_settings(monkeypatch, tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"desktopLocalClientToken": "tok-123"}), encoding="utf-8")
    assert resolve_local_token("http://10.0.0.5:57123", settings_path=settings) is None
    client = OpenChamberClient("http://10.0.0.5:57123", settings_path=settings)
    assert client._token is None


def test_base_url_validation():
    with pytest.raises(ValueError):
        OpenChamberClient("ftp://127.0.0.1")
    assert is_loopback_base_url("http://localhost:57123/")
    assert not is_loopback_base_url("http://10.0.0.5:57123")


# ---------- 执行配置解析回归：压缩后内部摘要配置不得成为执行配置来源（v1.13 §14） ----------

S1 = "ses_1"
MSG_URL = f"http://127.0.0.1:57123/api/session/{S1}/message"
SESS_URL = f"http://127.0.0.1:57123/api/session/{S1}"
PROMPT_URL = f"http://127.0.0.1:57123/api/session/{S1}/prompt_async"


def _msg(role, created, *, agent=None, provider=None, model=None, variant=None,
         synthetic=None, summary=None):
    info = {"role": role, "time": {"created": created}}
    if agent is not None:
        info["agent"] = agent
    if provider is not None:
        info["providerID"] = provider
    if model is not None:
        info["modelID"] = model
    if variant is not None:
        info["variant"] = variant
    if synthetic is not None:
        info["synthetic"] = synthetic
    if summary is not None:
        info["summary"] = summary
    return {"info": info, "parts": []}


def _normal(created, agent="build", provider="p1", model="m1", variant=None):
    return _msg("assistant", created, agent=agent, provider=provider,
                model=model, variant=variant)


def _patch_http(monkeypatch, routes):
    """routes: {url: (status, body_bytes)}；缺省 404。返回记录的 (url, data) 请求列表。"""
    calls = []

    def fake_urlopen(request, timeout=None):
        url = request.full_url
        calls.append((url, request.data))
        status, body = routes.get(url, (404, b"{}"))
        if status < 300:
            return FakeResponse(status, body)
        raise urllib.error.HTTPError(url, status, "err", {}, io.BytesIO(body))

    monkeypatch.setattr("openchamber_client.urllib.request.urlopen", fake_urlopen)
    return calls


def _messages_body(*messages):
    return json.dumps(list(messages)).encode("utf-8")


def test_resolve_ignores_latest_synthetic_summary(monkeypatch, tmp_path):
    """标记1：synthetic/summary 标记的内部摘要（agent 正常）不得压过较早的正常配置。"""
    body = _messages_body(
        _normal(100),
        _msg("assistant", 200, agent="build", provider="p2", model="m2", synthetic=True),
        _msg("assistant", 300, agent="build", provider="p3", model="m3", summary=True),
    )
    _patch_http(monkeypatch, {MSG_URL: (200, body)})
    cfg = make_client(monkeypatch, tmp_path).resolve_execution_config(S1)
    assert cfg is not None and cfg.source == "assistant"
    assert (cfg.agent, cfg.provider_id, cfg.model_id) == ("build", "p1", "m1")


def test_resolve_ignores_latest_compaction_agent(monkeypatch, tmp_path):
    """标记2：无 summary 标记但 agent=compaction 的内部代理同样不是执行配置来源。"""
    body = _messages_body(
        _normal(100),
        _msg("assistant", 200, agent="compaction", provider="p2", model="m2"),
    )
    _patch_http(monkeypatch, {MSG_URL: (200, body)})
    cfg = make_client(monkeypatch, tmp_path).resolve_execution_config(S1)
    assert cfg is not None
    assert (cfg.agent, cfg.provider_id, cfg.model_id) == ("build", "p1", "m1")
    assert cfg.variant is None


def test_resolve_picks_most_recent_normal(monkeypatch, tmp_path):
    """多个正常配置仍选最近一条（既有优先级不变）。"""
    body = _messages_body(_normal(100, model="m1"), _normal(300, model="m3"))
    _patch_http(monkeypatch, {MSG_URL: (200, body)})
    cfg = make_client(monkeypatch, tmp_path).resolve_execution_config(S1)
    assert cfg is not None and cfg.model_id == "m3"


def test_resolve_keeps_normal_nonbuild_agent_and_variant(monkeypatch, tmp_path):
    """正常非 build 代理与 variant 原样保留，不强制改成 build。"""
    body = _messages_body(_normal(100, agent="review", model="m1", variant="fast"))
    _patch_http(monkeypatch, {MSG_URL: (200, body)})
    cfg = make_client(monkeypatch, tmp_path).resolve_execution_config(S1)
    assert cfg is not None
    assert cfg.agent == "review" and cfg.variant == "fast"


def test_no_safe_config_send_does_not_post(monkeypatch, tmp_path):
    """仅内部摘要且会话回退 404 → 不可用；send_text 不得 POST prompt_async。"""
    body = _messages_body(
        _msg("assistant", 100, agent="compaction", provider="p1", model="m1", summary=True),
        _msg("assistant", 200, agent="build", provider="p2", model="m2", synthetic=True),
    )
    calls = _patch_http(monkeypatch, {MSG_URL: (200, body)})
    client = make_client(monkeypatch, tmp_path)
    assert client.resolve_execution_config(S1) is None
    result = client.send_text(S1, None, "task text")
    assert result.accepted is False and result.message_id is None
    assert result.error.startswith("unavailable")
    assert all(url != PROMPT_URL for url, _data in calls)


def test_session_fallback_normal_usable(monkeypatch, tmp_path):
    """消息仅内部摘要时，回退到会话对象正常配置（agent/provider/model/variant 保留）并正常发送。"""
    body = _messages_body(
        _msg("assistant", 100, agent="compaction", provider="p9", model="m9", summary=True)
    )
    session = json.dumps(
        {"agent": "build", "model": {"id": "m1", "providerID": "p1", "variant": "fast"}}
    ).encode("utf-8")
    calls = _patch_http(monkeypatch, {MSG_URL: (200, body), SESS_URL: (200, session),
                                      PROMPT_URL: (202, b"")})
    client = make_client(monkeypatch, tmp_path)
    cfg = client.resolve_execution_config(S1)
    assert cfg is not None and cfg.source == "session"
    assert (cfg.agent, cfg.provider_id, cfg.model_id, cfg.variant) == ("build", "p1", "m1", "fast")
    result = client.send_text(S1, None, "task text")
    assert result.accepted is True
    post = [data for url, data in calls if url == PROMPT_URL]
    assert len(post) == 1
    payload = json.loads(post[0].decode("utf-8"))
    assert payload["agent"] == "build"
    assert payload["variant"] == "fast"
    assert payload["model"] == {"providerID": "p1", "modelID": "m1"}


def test_session_fallback_internal_agent_rejected(monkeypatch, tmp_path):
    """会话对象回退同样拒绝：agent=compaction 的内部配置 → 不可用，不 POST。"""
    body = _messages_body()  # 空消息 2xx → 无消息配置，走会话回退
    session = json.dumps({"agent": "compaction", "model": {"id": "m1", "providerID": "p1"}}).encode("utf-8")
    calls = _patch_http(monkeypatch, {MSG_URL: (200, body), SESS_URL: (200, session)})
    client = make_client(monkeypatch, tmp_path)
    assert client.resolve_execution_config(S1) is None
    result = client.send_text(S1, None, "task text")
    assert result.accepted is False
    assert all(url != PROMPT_URL for url, _data in calls)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
