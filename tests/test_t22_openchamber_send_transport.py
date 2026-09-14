"""T22-03：生产 OpenChamberPromptAsyncTransport 自动测试（全程 fake opener，0 真实网络）。

覆盖 T22-03 卡（44+ 场景）：
- 构造冻结配置 + 输入校验（blank / 非 loopback fail-closed / ses_、sess_ 前缀）；
- pre-send snapshot：GET-only、最小字段、message body 不入快照、全部失败路径
  → SnapshotFailure（明确未 POST，不是 UNKNOWN）；
- send_once：唯一写入口、精确 POST contract（5 顶层 key / query directory）、
  HTTP→SendOutcome 映射（204=ACCEPTED、4xx=REJECTED、其余=UNKNOWN）、
  3xx 不跟随、单次 opener 调用、传输级不明上抛且不 retry、绝不做 reconciliation；
- secret hygiene：token / 远端响应正文绝不出现在 evidence / error / repr。

全部 fake opener，无任何真实网络。
"""

from __future__ import annotations

import io
import json
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from adapters.openchamber import (  # noqa: E402
    PROMPT_ASYNC_PATH_TMPL,
    OpenChamberPromptAsyncTransport,
    OpenChamberPromptTransportError,
    _non_redirecting_write_opener,
    is_valid_session_id,
)
from core.dispatch import (  # noqa: E402
    SendOutcome,
    SendTransportError,
    SnapshotFailure,
)

_BASE = "http://127.0.0.1:57123"
_DIR = r"D:\AIwork\proj dir"
_SID = "ses_abc123"
_PLANNED = "msg_0123456789abcdef0123456789abcdef"
_SECRET_TOKEN = "tok-TopSecret-9f8e"
_MESSAGE_BODY_SECRET = "UNIQUE-SECRET-MESSAGE-BODY-TEXT"
_SECRET_RAW_BODY = "SECRET-RAW-REMOTE-BODY-xyz"


# ---------------------------------------------------------------------- fake harness

class FakeResponse:
    def __init__(self, status: int, body: str | bytes, content_type: str = "application/json",
                 headers: dict | None = None):
        self.status = status
        self._body = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        self.content_type = content_type
        self.headers = dict(headers or {})

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
    """一次性脚本式 fake opener：按序消费响应（可抛异常），记录全部请求并计数。

    script 耗尽且未提供 default 时再调用 → AssertionError（防止测试误解为 0 HTTP）。
    """

    def __init__(self, script=None, default=None):
        self.script = list(script or [])
        self.default = default  # None → 越界即失败
        self.calls: list[dict] = []
        self.call_count = 0

    def __call__(self, req):
        self.call_count += 1
        self.calls.append({
            "method": req.get_method(),
            "url": req.full_url,
            "headers": {k: v for k, v in req.header_items()},
            "body": req.data.decode("utf-8") if req.data is not None else None,
        })
        if self.script:
            item = self.script.pop(0)
        elif self.default is not None:
            item = self.default
        else:
            raise AssertionError("fake opener 超出预设响应")
        if isinstance(item, Exception):
            raise item
        if isinstance(item, FakeResponse):
            return item
        if isinstance(item, (tuple, list)) and len(item) == 3:
            status, body, content_type = item
            return FakeResponse(status, body, content_type=content_type)
        status, body = item
        return FakeResponse(status, body)


def _session_payload(sid: str = _SID) -> list:
    return [{"id": sid, "title": "t"}]


def _message_payload(secret: bool = True) -> list:
    parts = [{"type": "text", "text": _MESSAGE_BODY_SECRET}] if secret else []
    return [{"info": {"id": _PLANNED, "role": "user", "parentID": None}, "parts": parts}]


def _status_payload() -> dict:
    return {_SID: {"active": True}}


def _ok_read_opener(session_payload=None, message_payload=None, status_payload=None,
                    secret: bool = True) -> FakeOpener:
    script = [
        (200, json.dumps(session_payload if session_payload is not None else _session_payload())),
        (200, json.dumps(message_payload if message_payload is not None else _message_payload(secret))),
        (200, json.dumps(status_payload if status_payload is not None else _status_payload())),
    ]
    return FakeOpener(script=script)


def _empty_opener() -> FakeOpener:
    return FakeOpener(script=[])


def make_transport(*, base_url: str = _BASE, directory: str = _DIR,
                   provider_id: str = "opencode", model_id: str = "big-pickle",
                   agent: str = "build", variant: str = "default",
                   token: str = _SECRET_TOKEN, timeout: float = 1.5,
                   read_opener=None, write_opener=None) -> OpenChamberPromptAsyncTransport:
    return OpenChamberPromptAsyncTransport(
        base_url, directory,
        provider_id=provider_id, model_id=model_id, agent=agent, variant=variant,
        token=token, timeout=timeout,
        read_opener=read_opener, write_opener=write_opener,
    )


def _send(t, *, session_id: str = _SID, prompt_text: str = "继续任务的报告正文",
          operation_id: str = "op-derive123", planned: str = _PLANNED):
    return t.send_once(endpoint=_BASE, session_id=session_id, prompt_text=prompt_text,
                       operation_id=operation_id, planned_remote_user_id=planned)


# =====================================================================
# 01-08：构造期冻结配置与输入校验（全部 0 HTTP）
# =====================================================================

def test_constructor_normalizes_and_freezes_config():
    t = make_transport(base_url="http://127.0.0.1:57123/", timeout=2)
    assert t.base_url == "http://127.0.0.1:57123"
    assert t.directory == _DIR
    assert t.provider_id == "opencode"
    assert t.model_id == "big-pickle"
    assert t.agent == "build"
    assert t.variant == "default"
    assert t.timeout == 2.0


@pytest.mark.parametrize("field", ["directory", "provider_id", "model_id", "agent", "variant"])
def test_constructor_rejects_blank_config_before_any_http(field):
    kwargs = {"directory": _DIR, "provider_id": "opencode", "model_id": "big-pickle",
              "agent": "build", "variant": "default"}
    kwargs[field] = "   "
    with pytest.raises(OpenChamberPromptTransportError) as ctx:
        OpenChamberPromptAsyncTransport(_BASE, **kwargs)
    assert ctx.value.kind == "MISSING_CONFIG"


@pytest.mark.parametrize("url", [
    "http://192.0.2.10:57123",
    "http://127.0.0.2:57123",
    "http://127.1.2.3:57123",
    "http://example.com/",
    "https://openchamber.example.com",
])
def test_constructor_fail_closed_non_loopback_target(url):
    with pytest.raises(OpenChamberPromptTransportError) as ctx:
        make_transport(base_url=url, token="tok")
    assert ctx.value.kind == "NON_LOOPBACK_TARGET"


@pytest.mark.parametrize("url", [
    "http://localhost:57123",
    "http://127.0.0.1:57123",
    "http://[::1]:57123",
])
def test_constructor_accepts_exact_loopback_hosts(url):
    t = make_transport(base_url=url, read_opener=_empty_opener(), write_opener=_empty_opener())
    assert t.base_url == url.rstrip("/")


def test_is_valid_session_id_prefix_semantics():
    assert is_valid_session_id("ses_1") is True
    assert is_valid_session_id("sess_012345") is True
    assert is_valid_session_id("ses_") is False     # 前缀后无内容 = malformed
    assert is_valid_session_id("bad123") is False
    assert is_valid_session_id("msg_x") is False
    assert is_valid_session_id("") is False
    assert is_valid_session_id(123) is False


# =====================================================================
# 09-27：pre-send snapshot（GET-only，最小字段，失败 → SnapshotFailure）
# =====================================================================

@pytest.mark.parametrize("sid", ["bad123", "sigma_1", "ses_", "msg_x", ""])
def test_snapshot_invalid_session_id_fails_0_http(sid):
    ro = _empty_opener()
    wo = _empty_opener()
    t = make_transport(read_opener=ro, write_opener=wo)
    with pytest.raises(SnapshotFailure):
        t.capture_pre_send_snapshot(endpoint=_BASE, session_id=sid, task_key="tk")
    assert ro.calls == []
    assert wo.calls == []


def test_snapshot_accepts_ses_and_sess_prefixes():
    for sid in ("ses_abc123", "sess_0123456789abc"):
        ro = _ok_read_opener(session_payload=[{"id": sid}])
        t = make_transport(read_opener=ro)
        snap = t.capture_pre_send_snapshot(endpoint=_BASE, session_id=sid, task_key="tk")
        assert snap["session_exists"] is True


def test_snapshot_success_minimal_fields():
    ro = _ok_read_opener()
    wo = _empty_opener()
    t = make_transport(read_opener=ro, write_opener=wo)
    snap = t.capture_pre_send_snapshot(endpoint=_BASE, session_id=_SID, task_key="tk")
    assert set(snap) == {"session_exists", "message_count", "message_ids",
                         "user_message_ids", "status_entry_present"}
    assert snap == {
        "session_exists": True,
        "message_count": 1,
        "message_ids": [_PLANNED],
        "user_message_ids": [_PLANNED],
        "status_entry_present": True,
    }
    assert wo.calls == []  # 快照不触发任何 POST


def test_snapshot_session_absent_fails():
    ro = _ok_read_opener(session_payload=[{"id": "ses_other"}])
    wo = _empty_opener()
    t = make_transport(read_opener=ro, write_opener=wo)
    with pytest.raises(SnapshotFailure):
        t.capture_pre_send_snapshot(endpoint=_BASE, session_id=_SID, task_key="tk")
    assert len(ro.calls) == 1
    assert wo.calls == []


def test_snapshot_session_list_malformed_json_fails():
    ro = FakeOpener(script=[(200, "{not json")])
    t = make_transport(read_opener=ro)
    with pytest.raises(SnapshotFailure):
        t.capture_pre_send_snapshot(endpoint=_BASE, session_id=_SID, task_key="tk")


def test_snapshot_session_list_non_array_fails():
    ro = FakeOpener(script=[(200, '{"sessions": []}')])
    t = make_transport(read_opener=ro)
    with pytest.raises(SnapshotFailure):
        t.capture_pre_send_snapshot(endpoint=_BASE, session_id=_SID, task_key="tk")


def test_snapshot_excludes_message_body_text():
    ro = _ok_read_opener(secret=True)
    t = make_transport(read_opener=ro)
    snap = t.capture_pre_send_snapshot(endpoint=_BASE, session_id=_SID, task_key="tk")
    text = json.dumps(snap, ensure_ascii=False)
    assert _MESSAGE_BODY_SECRET not in text
    assert "parts" not in snap          # parts 一律不进快照
    assert "body" not in snap
    assert "text" not in snap


def test_snapshot_message_list_malformed_json_fails():
    ro = FakeOpener(script=[
        (200, json.dumps(_session_payload())),
        (200, "{oops"),
    ])
    t = make_transport(read_opener=ro)
    with pytest.raises(SnapshotFailure):
        t.capture_pre_send_snapshot(endpoint=_BASE, session_id=_SID, task_key="tk")


def test_snapshot_message_shape_unparseable_fails():
    bad = [{"id": "flat", "role": "user"}]  # 缺 info 嵌套 = 不符 T21 封板形状
    ro = FakeOpener(script=[
        (200, json.dumps(_session_payload())),
        (200, json.dumps(bad)),
    ])
    t = make_transport(read_opener=ro)
    with pytest.raises(SnapshotFailure):
        t.capture_pre_send_snapshot(endpoint=_BASE, session_id=_SID, task_key="tk")


def test_snapshot_message_id_nonstring_fails():
    bad = [{"info": {"id": 42, "role": "user"}, "parts": []}]
    ro = FakeOpener(script=[
        (200, json.dumps(_session_payload())),
        (200, json.dumps(bad)),
    ])
    t = make_transport(read_opener=ro)
    with pytest.raises(SnapshotFailure):
        t.capture_pre_send_snapshot(endpoint=_BASE, session_id=_SID, task_key="tk")


def test_snapshot_message_role_nonstring_fails():
    bad = [{"info": {"id": "msg_1", "role": 7}, "parts": []}]
    ro = FakeOpener(script=[
        (200, json.dumps(_session_payload())),
        (200, json.dumps(bad)),
    ])
    t = make_transport(read_opener=ro)
    with pytest.raises(SnapshotFailure):
        t.capture_pre_send_snapshot(endpoint=_BASE, session_id=_SID, task_key="tk")


def test_snapshot_extracts_only_info_id_and_role():
    payload = [
        {"info": {"id": "msg_u1", "role": "user", "parentID": None}, "parts": [{"type": "text", "text": "a"}]},
        {"info": {"id": "msg_a1", "role": "assistant", "parentID": "msg_u1"}, "parts": [{"type": "text", "text": "b"}]},
        {"info": {"id": "msg_u2", "role": "user"}, "parts": []},
    ]
    t = make_transport(read_opener=_ok_read_opener(message_payload=payload))
    snap = t.capture_pre_send_snapshot(endpoint=_BASE, session_id=_SID, task_key="tk")
    assert snap["message_count"] == 3
    assert snap["message_ids"] == ["msg_u1", "msg_a1", "msg_u2"]
    assert snap["user_message_ids"] == ["msg_u1", "msg_u2"]
    assert "parentID" not in snap


def test_snapshot_status_malformed_json_fails():
    ro = FakeOpener(script=[
        (200, json.dumps(_session_payload())),
        (200, json.dumps(_message_payload())),
        (200, "{oops"),
    ])
    t = make_transport(read_opener=ro)
    with pytest.raises(SnapshotFailure):
        t.capture_pre_send_snapshot(endpoint=_BASE, session_id=_SID, task_key="tk")


def test_snapshot_status_empty_dict_not_failure():
    t = make_transport(read_opener=_ok_read_opener(status_payload={}))
    snap = t.capture_pre_send_snapshot(endpoint=_BASE, session_id=_SID, task_key="tk")
    assert snap["status_entry_present"] is False   # 可读但无条目 = 缺省语义，不算失败


def test_snapshot_status_non_dict_fails():
    t = make_transport(read_opener=_ok_read_opener(status_payload=["not", "dict"]))
    with pytest.raises(SnapshotFailure):
        t.capture_pre_send_snapshot(endpoint=_BASE, session_id=_SID, task_key="tk")


def test_snapshot_401_auth_error_fails():
    ro = FakeOpener(script=[_http_error(401, '{"error":"unauth"}')])
    t = make_transport(read_opener=ro)
    with pytest.raises(SnapshotFailure):
        t.capture_pre_send_snapshot(endpoint=_BASE, session_id=_SID, task_key="tk")
    assert ro.call_count == 1


def test_snapshot_403_auth_error_fails():
    ro = FakeOpener(script=[_http_error(403, '{"error":"forbidden"}')])
    t = make_transport(read_opener=ro)
    with pytest.raises(SnapshotFailure):
        t.capture_pre_send_snapshot(endpoint=_BASE, session_id=_SID, task_key="tk")


def test_snapshot_timeout_fails():
    ro = FakeOpener(script=[socket.timeout("t")])
    t = make_transport(read_opener=ro)
    with pytest.raises(SnapshotFailure):
        t.capture_pre_send_snapshot(endpoint=_BASE, session_id=_SID, task_key="tk")


def test_snapshot_connection_error_fails():
    ro = FakeOpener(script=[ConnectionRefusedError("refused")])
    t = make_transport(read_opener=ro)
    with pytest.raises(SnapshotFailure):
        t.capture_pre_send_snapshot(endpoint=_BASE, session_id=_SID, task_key="tk")


def test_snapshot_stores_no_token_or_auth_but_uses_bearer_on_wire():
    ro = _ok_read_opener()
    t = make_transport(token=_SECRET_TOKEN, read_opener=ro)
    snap = t.capture_pre_send_snapshot(endpoint=_BASE, session_id=_SID, task_key="tk")
    text = json.dumps(snap, ensure_ascii=False)
    assert _SECRET_TOKEN not in text
    assert "Authorization" not in text
    assert "Bearer" not in text
    lower_headers = {k.lower(): v for k, v in ro.calls[0]["headers"].items()}
    assert lower_headers["authorization"] == f"Bearer {_SECRET_TOKEN}"


def test_snapshot_endpoint_mismatch_fails_0_http():
    ro = _empty_opener()
    wo = _empty_opener()
    t = make_transport(read_opener=ro, write_opener=wo)
    with pytest.raises(SnapshotFailure):
        t.capture_pre_send_snapshot(endpoint="http://127.0.0.1:57124", session_id=_SID,
                                    task_key="tk")
    assert ro.calls == []
    assert wo.calls == []


def test_snapshot_endpoint_trailing_slash_normalized():
    t = make_transport(read_opener=_ok_read_opener())
    snap = t.capture_pre_send_snapshot(endpoint=_BASE + "/", session_id=_SID, task_key="tk")
    assert snap["session_exists"] is True


def test_snapshot_three_gets_in_order_using_frozen_directory():
    ro = _ok_read_opener()
    t = make_transport(directory=_DIR, read_opener=ro)
    t.capture_pre_send_snapshot(endpoint=_BASE, session_id=_SID, task_key="tk")
    assert len(ro.calls) == 3
    assert [c["method"] for c in ro.calls] == ["GET", "GET", "GET"]
    paths = [urllib.parse.urlsplit(c["url"]).path for c in ro.calls]
    assert paths[0] == "/api/session"
    assert paths[1] == f"/api/session/{_SID}/message"
    assert paths[2] == "/api/session/status"
    assert [urllib.parse.parse_qs(urllib.parse.urlsplit(c["url"]).query)
            for c in ro.calls] == [{"directory": [_DIR]}] * 3


# =====================================================================
# 28-52：send_once 精确写 contract 与 HTTP → SendOutcome
# =====================================================================

def test_send_once_exact_prompt_async_contract():
    wo = FakeOpener(script=[(204, "")])
    t = make_transport(write_opener=wo)
    prompt = "继续任务的报告正文"
    attempt = t.send_once(endpoint=_BASE, session_id=_SID, prompt_text=prompt,
                          operation_id="op-derive123", planned_remote_user_id=_PLANNED)
    assert len(wo.calls) == 1
    call = wo.calls[0]
    assert call["method"] == "POST"
    parts = urllib.parse.urlsplit(call["url"])
    assert parts.scheme == "http" and parts.netloc == "127.0.0.1:57123"
    assert parts.path == f"/api/session/{_SID}/prompt_async"
    assert PROMPT_ASYNC_PATH_TMPL == "/api/session/{sid}/prompt_async"
    assert urllib.parse.parse_qs(parts.query) == {"directory": [_DIR]}
    body = json.loads(call["body"])
    assert set(body) == {"messageID", "model", "agent", "variant", "parts"}  # 唯一 5 个顶层 key
    assert body["messageID"] == _PLANNED                      # identity 只来自 ledger，绝不重生成
    assert body["model"] == {"providerID": "opencode", "modelID": "big-pickle"}
    assert body["agent"] == "build"
    assert body["variant"] == "default"
    assert body["parts"] == [{"type": "text", "text": prompt}]
    lower_headers = {k.lower(): v for k, v in call["headers"].items()}
    assert lower_headers["content-type"] == "application/json"
    for forbidden in ("delivery", "resume", "operationID", "taskID"):
        assert forbidden not in body
    assert attempt.outcome is SendOutcome.ACCEPTED
    assert attempt.remote_user_id == _PLANNED


def test_send_204_accepted_not_completed_no_readback():
    wo = FakeOpener(script=[(204, "")])
    ro = _empty_opener()
    t = make_transport(read_opener=ro, write_opener=wo)
    attempt = _send(t)
    assert attempt.outcome is SendOutcome.ACCEPTED
    assert attempt.remote_user_id == _PLANNED   # ACCEPTED != completed，且无任何 GET readback
    assert attempt.evidence == {"http_status": 204, "classification": "accepted"}
    assert len(wo.calls) == 1
    assert ro.calls == []


@pytest.mark.parametrize("code", [400, 401, 403, 404, 409, 429])
def test_send_4xx_rejected_with_minimal_evidence(code):
    wo = FakeOpener(script=[(code, '{"error":"remote"}')])
    t = make_transport(write_opener=wo)
    attempt = _send(t)
    assert attempt.outcome is SendOutcome.REJECTED
    assert attempt.remote_user_id is None
    assert attempt.evidence == {"http_status": code, "classification": "rejected"}
    assert len(wo.calls) == 1


def test_send_5xx_unknown():
    wo = FakeOpener(script=[(500, "server exploded")])
    t = make_transport(write_opener=wo)
    attempt = _send(t)
    assert attempt.outcome is SendOutcome.UNKNOWN
    assert attempt.remote_user_id is None
    assert attempt.evidence == {"http_status": 500, "classification": "unknown"}
    assert len(wo.calls) == 1


def test_send_3xx_not_followed_single_request():
    wo = FakeOpener(script=[FakeResponse(302, "", headers={"Location": "/somewhere-else"})])
    t = make_transport(write_opener=wo)
    attempt = _send(t)
    assert attempt.outcome is SendOutcome.UNKNOWN
    assert attempt.remote_user_id is None
    assert attempt.evidence == {"http_status": 302, "classification": "unknown"}
    assert len(wo.calls) == 1                     # 绝不产生第二个 HTTP 请求、不 follow Location


def test_production_write_opener_has_no_redirect_or_error_handlers():
    opener = _non_redirecting_write_opener()
    handler_types = [type(h).__name__ for h in opener.handlers]
    assert "HTTPRedirectHandler" not in handler_types
    assert "HTTPErrorProcessor" not in handler_types
    assert "HTTPCookieProcessor" not in handler_types


def test_send_unexpected_2xx_unknown():
    wo = FakeOpener(script=[(200, '{"ok":true}')])
    t = make_transport(write_opener=wo)
    attempt = _send(t)
    assert attempt.outcome is SendOutcome.UNKNOWN      # 除 204 外 unexpected 2xx → UNKNOWN
    assert attempt.remote_user_id is None
    assert attempt.evidence == {"http_status": 200, "classification": "unknown"}
    assert len(wo.calls) == 1


def test_send_timeout_raises_transport_error_single_call():
    wo = FakeOpener(script=[socket.timeout("t")])
    t = make_transport(write_opener=wo)
    with pytest.raises(OpenChamberPromptTransportError) as ctx:
        _send(t)
    assert ctx.value.kind == "TIMEOUT"
    assert isinstance(ctx.value, SendTransportError)
    assert len(wo.calls) == 1                     # 不明 => 上抛且不重试


def test_send_connection_refused_raises():
    wo = FakeOpener(script=[ConnectionRefusedError("refused")])
    t = make_transport(write_opener=wo)
    with pytest.raises(OpenChamberPromptTransportError) as ctx:
        _send(t)
    assert ctx.value.kind == "CONNECTION"
    assert len(wo.calls) == 1


def test_send_urlerror_raises():
    wo = FakeOpener(script=[urllib.error.URLError(OSError("conn failed"))])
    t = make_transport(write_opener=wo)
    with pytest.raises(OpenChamberPromptTransportError) as ctx:
        _send(t)
    assert ctx.value.kind == "CONNECTION"


def test_send_http_error_raised_path_classified():
    wo = FakeOpener(script=[_http_error(403, '{"error":"nope"}')])
    t = make_transport(write_opener=wo)
    attempt = _send(t)
    assert attempt.outcome is SendOutcome.REJECTED
    assert attempt.evidence == {"http_status": 403, "classification": "rejected"}
    wo2 = FakeOpener(script=[_http_error(503, "boom")])
    t2 = make_transport(write_opener=wo2)
    attempt2 = _send(t2)
    assert attempt2.outcome is SendOutcome.UNKNOWN
    assert attempt2.evidence == {"http_status": 503, "classification": "unknown"}


def test_send_blank_planned_id_fail_closed_0_http():
    wo = _empty_opener()
    t = make_transport(write_opener=wo)
    with pytest.raises(OpenChamberPromptTransportError) as ctx:
        _send(t, planned="   ")
    assert ctx.value.kind == "MISSING_PLANNED_ID"
    assert wo.calls == []


def test_send_planned_not_msg_prefix_fail_closed_0_http():
    wo = _empty_opener()
    t = make_transport(write_opener=wo)
    with pytest.raises(OpenChamberPromptTransportError) as ctx:
        _send(t, planned="user-abc")
    assert ctx.value.kind == "INVALID_PLANNED_ID"
    assert wo.calls == []


def test_send_invalid_session_id_fail_closed_0_http():
    wo = _empty_opener()
    t = make_transport(write_opener=wo)
    with pytest.raises(OpenChamberPromptTransportError) as ctx:
        _send(t, session_id="bad123")
    assert ctx.value.kind == "INVALID_SESSION_ID"
    assert wo.calls == []


def test_send_endpoint_mismatch_fail_closed_0_http():
    wo = _empty_opener()
    t = make_transport(write_opener=wo)
    with pytest.raises(OpenChamberPromptTransportError) as ctx:
        t.send_once(endpoint="http://127.0.0.1:57124", session_id=_SID,
                    prompt_text="p", operation_id="op", planned_remote_user_id=_PLANNED)
    assert ctx.value.kind == "ENDPOINT_MISMATCH"
    assert wo.calls == []


def test_send_endpoint_trailing_slash_matches():
    wo = FakeOpener(script=[(204, "")])
    t = make_transport(write_opener=wo)
    attempt = t.send_once(endpoint=_BASE + "/", session_id=_SID, prompt_text="p",
                          operation_id="op", planned_remote_user_id=_PLANNED)
    assert attempt.outcome is SendOutcome.ACCEPTED


def test_send_bearer_header_on_loopback_only():
    wo = FakeOpener(script=[(204, "")])
    t = make_transport(token=_SECRET_TOKEN, write_opener=wo)
    _send(t)
    lower_headers = {k.lower(): v for k, v in wo.calls[0]["headers"].items()}
    assert lower_headers["authorization"] == f"Bearer {_SECRET_TOKEN}"


def test_send_no_reconciliation_get_after_unknown():
    wo = FakeOpener(script=[(500, "boom")])
    ro = _empty_opener()
    t = make_transport(read_opener=ro, write_opener=wo)
    attempt = _send(t)
    assert attempt.outcome is SendOutcome.UNKNOWN
    assert [c["method"] for c in wo.calls] == ["POST"]
    assert len(wo.calls) == 1
    assert ro.calls == []          # UNKNOWN 交还 Dispatch/Ledger，adapter 不做自动 GET/重发


def test_send_no_internal_retry_per_call():
    wo = FakeOpener(default=(503, "boom"))     # 可重复消费：证明每次调用都只发 1 次
    t = make_transport(write_opener=wo)
    for _ in range(3):
        attempt = _send(t)
        assert attempt.outcome is SendOutcome.UNKNOWN
    assert len(wo.calls) == 3                  # 每次 send_once 恰好 1 次，adapter 绝不内建重试


def test_send_planned_message_id_exactly_used_not_regenerated():
    wo = FakeOpener(script=[(204, "")])
    t = make_transport(write_opener=wo)
    different_planned = "msg_ffffffffffffffffffffffffffffffff"
    _send(t, planned=different_planned)
    body = json.loads(wo.calls[0]["body"])
    assert body["messageID"] == different_planned     # 绝不重新生成 messageID


# =====================================================================
# 43-44 及补充：secret hygiene
# =====================================================================

def test_token_not_in_evidence_error_repr():
    wo = FakeOpener(script=[(500, "server exploded")])
    t = make_transport(token=_SECRET_TOKEN, write_opener=wo)
    attempt = _send(t)
    evidence_text = json.dumps(attempt.evidence, ensure_ascii=False)
    assert _SECRET_TOKEN not in evidence_text
    assert "Authorization" not in evidence_text
    assert "Bearer" not in evidence_text
    with pytest.raises(OpenChamberPromptTransportError) as ctx:
        t.send_once(endpoint="http://127.0.0.1:57124", session_id=_SID, prompt_text="p",
                    operation_id="op", planned_remote_user_id=_PLANNED)
    assert _SECRET_TOKEN not in str(ctx.value)
    assert _SECRET_TOKEN not in repr(ctx.value)
    assert _SECRET_TOKEN not in repr(t)
    assert _SECRET_TOKEN not in str(t)


def test_error_raw_body_not_in_evidence():
    wo = FakeOpener(script=[(503, f'{{"message": "{_SECRET_RAW_BODY}"}}')])
    t = make_transport(write_opener=wo)
    attempt = _send(t)
    assert _SECRET_RAW_BODY not in json.dumps(attempt.evidence)
    assert attempt.evidence == {"http_status": 503, "classification": "unknown"}
    wo2 = FakeOpener(script=[(400, f'{{"error": "{_SECRET_RAW_BODY}"}}')])
    t2 = make_transport(write_opener=wo2)
    attempt2 = _send(t2)
    assert _SECRET_RAW_BODY not in json.dumps(attempt2.evidence)
    assert attempt2.evidence == {"http_status": 400, "classification": "rejected"}


def test_secrets_not_in_snapshot_failure_detail():
    ro = FakeOpener(script=[_http_error(401, f'{{"token":"{_SECRET_TOKEN}", "body":"{_SECRET_RAW_BODY}"}}')])
    t = make_transport(token=_SECRET_TOKEN, read_opener=ro)
    with pytest.raises(SnapshotFailure) as ctx:
        t.capture_pre_send_snapshot(endpoint=_BASE, session_id=_SID, task_key="tk")
    assert _SECRET_TOKEN not in str(ctx.value)
    assert _SECRET_RAW_BODY not in str(ctx.value)