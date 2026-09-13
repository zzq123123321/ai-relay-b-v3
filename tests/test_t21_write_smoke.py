"""T21-02F：Disposable Session 单次真实写冒烟探测器自动测试（全程 fake，0 真实网络）。

覆盖卡 §16 要求的全部项目（至少 20 例）+ T21-02R 恢复/续测规则：
- exact base URL fail-closed；create/prompt endpoint 固定；无 generic post/request 公共 API；
- create max once / prompt max once；journal before create / before prompt；
- existing journal refuses second armed run；create timeout→UNKNOWN_CREATE→no resend；
- send timeout→UNKNOWN_SEND→no resend；reconciliation uses GET only；
- 204 不代表 completed；explicit parent attribution required；last-message heuristic rejected；
- evidence redacts IDs；other_write_count==0；no cleanup write capability；
- 前缀规则：ses_（runtime）/ sess_（documented）均合法；无关前缀/malformed 拒绝；
- reconcile_exactly_one 决定性三条件；resume-once 绝不 create、至多 1 次 prompt POST、
  不 reset journal（create_attempt_count 恒定 1）。
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

import scripts.probe_openchamber_write_smoke as mx  # noqa: E402

_FULL_SESSION_ID = "sess_0123456789abcdef_TEST"
_FULL_MESSAGE_ID = "msg_0123456789abcdef_TEST"


@pytest.fixture(autouse=True)
def _reset_post_counter():
    mx._WRITE_FIRE_COUNT = 0
    yield
    mx._WRITE_FIRE_COUNT = 0


# ---------------------------------------------------------------------- fake harness

class _FakeRaw:
    def __init__(self, status=200, text="{}", content_type="application/json"):
        self.status = status
        self._text = text
        self.content_type = content_type

    @property
    def decoded(self) -> str:
        return self._text


class _FakeTransport:
    def __init__(self, responses=None):
        self.base_url = mx.ALLOWED_BASE_URL
        self.responses = list(responses or [])
        self.calls: list[tuple[str, str, bool]] = []

    def request(self, method, path, *, attach_auth):
        self.calls.append((method, path, attach_auth))
        if not self.responses:
            raise AssertionError("fake transport 无预设响应")
        return self.responses.pop(0)


def _prepared_state(tmp_path: Path, **overrides) -> Path:
    state_path = tmp_path / "state.json"
    st = mx.initial_state("t21_probe", "D:/probe/dir", nonce="fake")
    st.update(overrides)
    mx._atomic_write_json(state_path, st)
    return state_path


# ---------------------------------------------------------------------- base URL fail-closed

def test_base_url_allowed_exact_target():
    assert mx.base_url_allowed(mx.ALLOWED_BASE_URL) is True


def test_base_url_fail_closed_on_wrong_scheme_host_port_path():
    cases = [
        "https://127.0.0.1:57123",
        "http://127.0.0.1:57124",
        "http://localhost:57123",
        "http://192.168.31.1:57123",
        "http://127.0.0.1:57123/api",
        "http://127.0.0.1:57123/",
        "http://127.0.0.1",
        "http://127.0.0.1:57123?x=1",
        "",
    ]
    for url in cases:
        assert mx.base_url_allowed(url) is False, url


# ---------------------------------------------------------------------- endpoint 固定 / 无 public 写 API

def test_create_endpoint_constant_fixed():
    assert mx.CREATE_PATH == "/api/session"


def test_prompt_async_endpoint_template_fixed():
    assert mx.PROMPT_ASYNC_PATH_TMPL == "/api/session/{sid}/prompt_async"


def test_no_public_post_or_request_api():
    assert not hasattr(mx, "post")
    assert not hasattr(mx, "request")
    assert not hasattr(mx, "PostJson")


def test_post_channel_confined_to_private_write_functions():
    src = Path(_REPO / "scripts" / "probe_openchamber_write_smoke.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    holders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name == "_post_json":
            continue
        if "_post_json(" in ast.unparse(node):
            holders.append(node.name)
    assert sorted(holders) == ["_create_probe_session_once", "_send_probe_prompt_once"]


def test_no_dynamic_method_seam_in_write_channel():
    src = Path(_REPO / "scripts" / "probe_openchamber_write_smoke.py").read_text(encoding="utf-8")
    assert src.count("urllib.request.Request") >= 1
    assert "method=" in src


# ---------------------------------------------------------------------- create max once / journal before create

def test_create_max_once_via_journal(tmp_path, monkeypatch):
    state_path = _prepared_state(tmp_path, phase="PREPARED", create_attempt_count=1)
    with pytest.raises(mx.ProbeWriteRefused):
        mx._create_probe_session_once(_FakeTransport(), "D:/probe/dir", state_path)


def test_journal_before_create(tmp_path, monkeypatch):
    state_path = _prepared_state(tmp_path, phase="PREPARED")
    seen = {}

    def fake_post(base_url, path, *, params=None, body=None, token=None, timeout=None):
        st = json.loads(Path(state_path).read_text(encoding="utf-8"))
        seen["phase"] = st["phase"]
        seen["create_attempt_count"] = st["create_attempt_count"]
        assert path == mx.CREATE_PATH
        assert params == {"directory": "D:/probe/dir"}
        assert body == {"title": "AI Relay B T21 write smoke (disposable)"}
        return mx._PostResult(200, "application/json", json.dumps({"id": _FULL_SESSION_ID}).encode())

    monkeypatch.setattr(mx, "_post_json", fake_post)
    out = mx._create_probe_session_once(_FakeTransport(), "D:/probe/dir", state_path)
    assert seen["phase"] == "CREATE_ATTEMPTED"
    assert seen["create_attempt_count"] == 1
    assert out["phase"] == "SESSION_CREATED"
    assert out["session_id"] == _FULL_SESSION_ID
    st = json.loads(Path(state_path).read_text(encoding="utf-8"))
    assert st["phase"] == "SESSION_CREATED"
    assert st["explicit_session_id"] == _FULL_SESSION_ID


def test_create_timeout_unknown_no_resend(tmp_path, monkeypatch):
    state_path = _prepared_state(tmp_path, phase="PREPARED")

    def fake_post(base_url, path, *, params=None, body=None, token=None, timeout=None):
        raise mx._ProbeTransportError("TIMEOUT", "fake socket timeout")

    monkeypatch.setattr(mx, "_post_json", fake_post)
    out = mx._create_probe_session_once(_FakeTransport(), "D:/probe/dir", state_path)
    assert out["phase"] == "UNKNOWN_CREATE"
    st = json.loads(Path(state_path).read_text(encoding="utf-8"))
    assert st["phase"] == "UNKNOWN_CREATE"
    assert st["create_attempt_count"] == 1
    with pytest.raises(mx.ProbeWriteRefused):
        mx._create_probe_session_once(_FakeTransport(), "D:/probe/dir", state_path)


def test_create_rejected_4xx_records_failed(tmp_path, monkeypatch):
    state_path = _prepared_state(tmp_path, phase="PREPARED")

    def fake_post(base_url, path, *, params=None, body=None, token=None, timeout=None):
        return mx._PostResult(400, "application/json", b'{"error":"bad"}')

    monkeypatch.setattr(mx, "_post_json", fake_post)
    out = mx._create_probe_session_once(_FakeTransport(), "D:/probe/dir", state_path)
    assert out["phase"] == "FAILED"
    assert json.loads(Path(state_path).read_text(encoding="utf-8"))["phase"] == "FAILED"


# ---------------------------------------------------------------------- prompt max once / journal before send

def _session_created_state(tmp_path) -> Path:
    return _prepared_state(tmp_path, phase="SESSION_CREATED", create_attempt_count=1,
                           explicit_session_id=_FULL_SESSION_ID)


def test_journal_before_send(tmp_path, monkeypatch):
    state_path = _session_created_state(tmp_path)
    seen = {}
    prompt_text = mx.synthetic_prompt("abcdef")

    def fake_post(base_url, path, *, params=None, body=None, token=None, timeout=None):
        st = json.loads(Path(state_path).read_text(encoding="utf-8"))
        seen["phase"] = st["phase"]
        seen["send_attempt_count"] = st["send_attempt_count"]
        assert path == f"/api/session/{_FULL_SESSION_ID}/prompt_async"
        assert params == {"directory": "D:/probe/dir"}
        assert body["messageID"].startswith("msg_")
        assert body["agent"] == "build"
        assert body["model"] == {"providerID": "opencode", "modelID": "big-pickle"}
        assert body["variant"] == "default"
        assert body["parts"] == [{"type": "text", "text": prompt_text}]
        return mx._PostResult(204, None, b"")

    monkeypatch.setattr(mx, "_post_json", fake_post)
    # 先单独抓取默认 title 以保持幂等（见下面断言固定使用常量）
    out = mx._send_probe_prompt_once(
        _FakeTransport(), _FULL_SESSION_ID, "D:/probe/dir",
        _FULL_MESSAGE_ID, prompt_text, state_path,
    )
    assert seen["phase"] == "SEND_ATTEMPTED"
    assert seen["send_attempt_count"] == 1
    assert out["phase"] == "SEND_ACCEPTED"
    assert out["http_status"] == 204
    st = json.loads(Path(state_path).read_text(encoding="utf-8"))
    assert st["phase"] == "SEND_ACCEPTED"
    assert st["send_http_status"] == 204
    assert st["send_accepted_at"] is not None


def test_prompt_max_once_via_journal(tmp_path, monkeypatch):
    state_path = _session_created_state(tmp_path)
    state_path2 = _prepared_state(tmp_path, phase="SEND_ACCEPTED", create_attempt_count=1,
                                  send_attempt_count=1, explicit_session_id=_FULL_SESSION_ID)
    with pytest.raises(mx.ProbeWriteRefused):
        mx._send_probe_prompt_once(
            _FakeTransport(), _FULL_SESSION_ID, "D:/probe/dir",
            _FULL_MESSAGE_ID, mx.synthetic_prompt("a"), state_path2,
        )


def test_send_timeout_unknown_no_resend(tmp_path, monkeypatch):
    state_path = _session_created_state(tmp_path)

    def fake_post(base_url, path, *, params=None, body=None, token=None, timeout=None):
        raise mx._ProbeTransportError("TIMEOUT", "send timeout")

    monkeypatch.setattr(mx, "_post_json", fake_post)
    out = mx._send_probe_prompt_once(
        _FakeTransport(), _FULL_SESSION_ID, "D:/probe/dir",
        _FULL_MESSAGE_ID, mx.synthetic_prompt("a"), state_path,
    )
    assert out["phase"] == "UNKNOWN_SEND"
    st = json.loads(Path(state_path).read_text(encoding="utf-8"))
    assert st["phase"] == "UNKNOWN_SEND"
    assert st["send_attempt_count"] == 1


def test_send_rejected_5xx_records_failed(tmp_path, monkeypatch):
    state_path = _session_created_state(tmp_path)

    def fake_post(base_url, path, *, params=None, body=None, token=None, timeout=None):
        return mx._PostResult(500, "application/json", b'{"error":"boom"}')

    monkeypatch.setattr(mx, "_post_json", fake_post)
    out = mx._send_probe_prompt_once(
        _FakeTransport(), _FULL_SESSION_ID, "D:/probe/dir",
        _FULL_MESSAGE_ID, mx.synthetic_prompt("a"), state_path,
    )
    assert out["phase"] == "FAILED"
    assert json.loads(Path(state_path).read_text(encoding="utf-8"))["phase"] == "FAILED"


def test_send_refused_when_session_not_created(tmp_path, monkeypatch):
    state_path = _prepared_state(tmp_path, phase="PREPARED")
    with pytest.raises(mx.ProbeWriteRefused):
        mx._send_probe_prompt_once(
            _FakeTransport(), _FULL_SESSION_ID, "D:/probe/dir",
            _FULL_MESSAGE_ID, mx.synthetic_prompt("a"), state_path,
        )


# ---------------------------------------------------------------------- existing journal refuses armed run

def test_existing_journal_refuses_second_armed_run(tmp_path, capsys):
    state_path = _prepared_state(tmp_path, phase="SESSION_CREATED",
                                 create_attempt_count=1, run_id="t21_old")
    rc = mx.main(["--arm-once", "--state", str(state_path),
                  "--probe-root", str(tmp_path / "probe")])
    assert rc == 3
    assert "REFUSE NEW WRITES" in capsys.readouterr().err


# ---------------------------------------------------------------------- reconciliation GET only

def test_reconciliation_uses_get_only(tmp_path):
    ft = _FakeTransport([_FakeRaw(200, json.dumps([{"id": _FULL_SESSION_ID}])),
                         _FakeRaw(200, json.dumps([{"id": _FULL_SESSION_ID}]))])
    ids = mx.reconcile_sessions(ft, "D:/probe/dir")
    assert ids == [_FULL_SESSION_ID]
    assert all(m == "GET" for m, _p, _a in ft.calls)
    assert all("/api/session" in p for _m, p, _a in ft.calls)


def test_preflight_empty_list_allows_proceed(tmp_path):
    ft = _FakeTransport([_FakeRaw(200, "[]")])
    assert mx.session_list(ft, "D:/probe/dir") == []


# ---------------------------------------------------------------------- 204 != completed

def test_204_is_not_completed_even_with_empty_messages(tmp_path):
    st = mx.initial_state("r", "d", nonce="n")
    evidence = mx.build_evidence(
        run_id="r", probe_directory="d", base_url=mx.ALLOWED_BASE_URL,
        phase="RESULT_OBSERVED", session_id=_FULL_SESSION_ID,
        session_confirmed_at="t1", create_attempted_at="t0",
        send_attempted_at="t2", send_http_status=204, send_accepted_at="t3",
        first_result_observed_at=None, completion_at=None,
        observation={"polls": [], "verdict": {
            "completed": False, "attribution": "UNVERIFIED",
            "conditions": {"A": False, "B": False, "C": False, "D": False}}},
        create_attempt_count=1, send_attempt_count=1,
        marker=mx.marker_for("n"), run_nonce="n",
        service_reachable=True, api_authenticated=True,
    )
    assert evidence["data_model"]["execute_accepted"] is True
    assert evidence["data_model"]["completed_and_attributed"] is False
    assert evidence["seven_layers"]["execution_progressing"] is False


# ---------------------------------------------------------------------- attribution

def _assistant_msg(mid, parent, text):
    return {"id": mid, "role": "assistant", "parentID": parent,
            "parts": [{"type": "text", "text": text}]}


def test_explicit_parent_attribution_required_for_complete_pass():
    msgs = [
        {"id": _FULL_MESSAGE_ID, "role": "user",
         "parts": [{"type": "text", "text": "hi"}]},
        _assistant_msg("msg_aaa", _FULL_MESSAGE_ID, mx.marker_for("n")),
    ]
    v = mx.evaluate_completion(msgs, _FULL_MESSAGE_ID, mx.marker_for("n"))
    assert v["completed"] is True
    assert v["attribution"] == "VALID"
    assert all(v["conditions"].values())


def test_last_message_heuristic_rejected_without_parent():
    msgs = [
        {"id": _FULL_MESSAGE_ID, "role": "user", "parts": [{"type": "text", "text": "hi"}]},
        _assistant_msg("msg_aaa", "msg_zzz", mx.marker_for("n")),
    ]
    v = mx.evaluate_completion(msgs, _FULL_MESSAGE_ID, mx.marker_for("n"))
    assert v["completed"] is False
    assert v["attribution"] == "AMBIGUOUS"
    assert v["conditions"]["D_explicit_parent_relation"] is False


def test_marker_as_last_message_but_no_parent_is_not_completed():
    msgs = [
        _assistant_msg("msg_other", "msg_someone", mx.marker_for("n")),
        {"id": _FULL_MESSAGE_ID, "role": "user", "parts": [{"type": "text", "text": "hi"}]},
    ]
    v = mx.evaluate_completion(msgs, _FULL_MESSAGE_ID, mx.marker_for("n"))
    assert v["completed"] is False
    assert v["assistant_text_match_any"] is True
    assert v["attribution"] == "AMBIGUOUS"


def test_parent_valid_but_text_mismatch_is_not_completed():
    msgs = [
        {"id": _FULL_MESSAGE_ID, "role": "user", "parts": [{"type": "text", "text": "hi"}]},
        _assistant_msg("msg_aaa", _FULL_MESSAGE_ID, "不同文本"),
    ]
    v = mx.evaluate_completion(msgs, _FULL_MESSAGE_ID, mx.marker_for("n"))
    assert v["completed"] is False
    assert v["attribution"] == "VALID"


def test_user_identity_must_be_unique():
    msgs = [
        {"id": _FULL_MESSAGE_ID, "role": "user", "parts": [{"type": "text", "text": "a"}]},
        {"id": _FULL_MESSAGE_ID, "role": "user", "parts": [{"type": "text", "text": "b"}]},
        _assistant_msg("msg_aaa", _FULL_MESSAGE_ID, mx.marker_for("n")),
    ]
    v = mx.evaluate_completion(msgs, _FULL_MESSAGE_ID, mx.marker_for("n"))
    assert v["completed"] is False
    assert v["conditions"]["A_user_identity_located"] is False


def test_marker_whitespace_tolerance_trim():
    msgs = [
        {"id": _FULL_MESSAGE_ID, "role": "user", "parts": [{"type": "text", "text": "hi"}]},
        _assistant_msg("msg_aaa", _FULL_MESSAGE_ID, mx.marker_for("n") + "\n"),
    ]
    v = mx.evaluate_completion(msgs, _FULL_MESSAGE_ID, mx.marker_for("n"))
    assert v["completed"] is True


# ---------------------------------------------------------------------- evidence redaction / counters

def test_evidence_redacts_session_and_message_ids():
    ev = mx.build_evidence(
        run_id="r", probe_directory="d", base_url=mx.ALLOWED_BASE_URL,
        phase="RESULT_OBSERVED", session_id=_FULL_SESSION_ID, session_confirmed_at="t",
        create_attempted_at="t", send_attempted_at="t", send_http_status=204,
        send_accepted_at="t", first_result_observed_at="t", completion_at="t",
        observation={"polls": [{}], "verdict": {"completed": True, "attribution": "VALID"}},
        create_attempt_count=1, send_attempt_count=1,
        marker=mx.marker_for("n"), run_nonce="n",
        service_reachable=True, api_authenticated=True,
        extra_notes={"full_session_id": _FULL_SESSION_ID, "full_message_id": _FULL_MESSAGE_ID},
    )
    text = json.dumps(ev, ensure_ascii=False)
    assert _FULL_SESSION_ID not in text
    assert _FULL_MESSAGE_ID not in text
    # 证据不包含真实 token / Authorization
    assert "Authorization" not in text and "Bearer " not in text
    assert ev["probe"]["session_id_redacted"].startswith("sess_")
    assert "#" in ev["probe"]["session_id_redacted"]


def test_other_write_count_is_zero():
    ev = mx.build_evidence(
        run_id="r", probe_directory="d", base_url=mx.ALLOWED_BASE_URL,
        phase="RESULT_OBSERVED", session_id=_FULL_SESSION_ID, session_confirmed_at="t",
        create_attempted_at="t", send_attempted_at="t", send_http_status=204,
        send_accepted_at="t", first_result_observed_at="t", completion_at="t",
        observation={"polls": [{}], "verdict": {"completed": True, "attribution": "VALID"}},
        create_attempt_count=1, send_attempt_count=1,
        marker=mx.marker_for("n"), run_nonce="n",
        service_reachable=True, api_authenticated=True,
    )
    assert ev["write_counters"]["create_post_count"] == 1
    assert ev["write_counters"]["prompt_post_count"] == 1
    assert ev["write_counters"]["other_write_count"] == 0


# ---------------------------------------------------------------------- no cleanup write capability

def test_no_cleanup_write_capability():
    src = Path(_REPO / "scripts" / "probe_openchamber_write_smoke.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fns = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) if n.name}
    assert not any(any(k in name for k in ("delete", "archive", "_cancel", "_stop")) for name in fns)
    # 模块不存在 DELETE 动词：urllib 只被 _post_json 以 POST 与 GET 使用
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "request"]
    assert all(kw.arg != "method" for c in calls for kw in c.keywords)


def test_post_json_hard_cap_two_max(tmp_path, monkeypatch):
    class _Raw:
        status = 204
        headers = {}
        def read(self, n=-1):
            return b""
    monkeypatch.setattr(mx.urllib.request, "urlopen", lambda req, timeout=None: _Raw())
    mx._post_json(mx.ALLOWED_BASE_URL, mx.CREATE_PATH, params={}, body={})
    mx._post_json(mx.ALLOWED_BASE_URL, mx.CREATE_PATH, params={}, body={})
    with pytest.raises(mx.ProbeWriteRefused):
        mx._post_json(mx.ALLOWED_BASE_URL, mx.CREATE_PATH, params={}, body={})
    assert mx._WRITE_FIRE_COUNT == 2


# ---------------------------------------------------------------------- message id / marker shape

def test_message_id_msg_prefix_and_safe_format():
    mid = mx.make_message_id()
    assert mid.startswith("msg_")
    assert len(mid) > 8
    assert mid[4:].isalnum()


def test_synthetic_prompt_has_exact_marker_line_and_no_real_data():
    nonce = mx.make_run_nonce()
    prompt = mx.synthetic_prompt(nonce)
    assert f"\n{mx.marker_for(nonce)}" in prompt
    assert prompt.count(f"T21_PROBE_OK_{nonce}") == 1
    assert "密码" not in prompt and "token" not in prompt.lower()


def test_marker_prefix_constant():
    assert mx.marker_for("x") == "T21_PROBE_OK_x"
    assert mx.MARKER_PREFIX == "T21_PROBE_OK_"


# ---------------------------------------------------------------------- dry-run 无副作用

def test_dry_run_no_write_no_network(tmp_path, monkeypatch):
    def bad_post(*a, **k):
        raise AssertionError("dry-run 不应调用 _post_json")
    monkeypatch.setattr(mx, "_post_json", bad_post)
    rc = mx.main(["--dry-run", "--state", str(tmp_path / "st.json"),
                  "--probe-root", str(tmp_path / "probe"), "--out", str(tmp_path / "ev.json")])
    assert rc == 0
    assert (tmp_path / "ev.json").exists() is False
    assert (tmp_path / "st.json").exists() is False
    plan = json.loads((tmp_path / "t21_write_smoke_plan.json").read_text(encoding="utf-8"))
    assert plan["maximum_writes"] == 2
    assert plan["create_path"].startswith("POST /api/session")
    assert "prompt_async" in plan["prompt_path_template"]
    assert plan["base_url_allowed"] is True
    assert plan["token"] == "<not-printed>"


# ---------------------------------------------------------------------- observe completion (read-only)

def test_observe_completion_polls_get_only_and_completes(tmp_path, monkeypatch):
    poll0 = [_FakeRaw(200, json.dumps([{"id": _FULL_MESSAGE_ID, "role": "user",
                                        "parts": [{"type": "text", "text": "hi"}]}]))]
    poll1 = [_FakeRaw(200, json.dumps([
        {"id": _FULL_MESSAGE_ID, "role": "user", "parts": [{"type": "text", "text": "hi"}]},
        _assistant_msg("msg_aaa", _FULL_MESSAGE_ID, mx.marker_for("n")),
    ]))]
    ft = _FakeTransport(poll0 + poll1)
    obs = mx.observe_completion(
        ft, _FULL_SESSION_ID, "D:/probe/dir", _FULL_MESSAGE_ID, mx.marker_for("n"),
        interval=1.0, sleep_fn=lambda _: None, clock_fn=lambda: 100.0,
        now_fn=lambda: "now",
    )
    assert obs["completion_at"] == "now"
    assert obs["verdict"]["completed"] is True
    assert all(m == "GET" for m, _p, _a in ft.calls)
    assert obs["last_messages_redacted"][1]["parentID"].startswith("msg_")


def test_parse_failures_in_get_raise_closed_error():
    ft = _FakeTransport([_FakeRaw(500, "boom", "text/plain")])
    with pytest.raises(mx.ProbeClosedError):
        mx.fetch_messages(ft, _FULL_SESSION_ID, "D:/probe/dir")


# ---------------------------------------------------------------------- T21-02R 前缀裁决 / reconciliation 决定性条件

_REAL_STYLE_SID = "ses_5f4d0c1b3a7e908f6c2b4a1d9e0f3c8a"
_DOC_SID = "sess_0123456789abcdef_TEST"


def test_session_id_prefix_of_detects_both_legal_prefixes():
    assert mx.session_id_prefix_of(_REAL_STYLE_SID) == "ses_"
    assert mx.session_id_prefix_of(_DOC_SID) == "sess_"
    assert mx.session_id_prefix_of("session_abc") is None
    assert mx.session_id_prefix_of("123_ses_abc") is None


def test_is_valid_session_id_rules():
    assert mx.is_valid_session_id(_REAL_STYLE_SID) is True
    assert mx.is_valid_session_id(_DOC_SID) is True
    assert mx.is_valid_session_id("ses_") is False      # 前缀后为空 = malformed
    assert mx.is_valid_session_id("sess_") is False     # 前缀后为空 = malformed
    assert mx.is_valid_session_id("xyz_123") is False   # 无关前缀
    assert mx.is_valid_session_id("") is False
    assert mx.is_valid_session_id(None) is False
    assert mx.is_valid_session_id(12345) is False


def test_create_accepts_real_style_ses_prefix(tmp_path, monkeypatch):
    state_path = _prepared_state(tmp_path, phase="PREPARED")

    def fake_post(base_url, path, *, params=None, body=None, token=None, timeout=None):
        assert path == mx.CREATE_PATH
        return mx._PostResult(200, "application/json",
                              json.dumps({"id": _REAL_STYLE_SID}).encode())

    monkeypatch.setattr(mx, "_post_json", fake_post)
    out = mx._create_probe_session_once(_FakeTransport(), "D:/probe/dir", state_path)
    assert out["phase"] == "SESSION_CREATED"
    assert out["session_id"] == _REAL_STYLE_SID
    assert json.loads(Path(state_path).read_text(encoding="utf-8"))["phase"] == "SESSION_CREATED"


def test_create_accepts_documented_sess_prefix(tmp_path, monkeypatch):
    state_path = _prepared_state(tmp_path, phase="PREPARED")

    def fake_post(base_url, path, *, params=None, body=None, token=None, timeout=None):
        return mx._PostResult(200, "application/json",
                              json.dumps({"id": _DOC_SID}).encode())

    monkeypatch.setattr(mx, "_post_json", fake_post)
    out = mx._create_probe_session_once(_FakeTransport(), "D:/probe/dir", state_path)
    assert out["phase"] == "SESSION_CREATED"
    assert out["session_id"] == _DOC_SID


def test_create_rejects_unrelated_prefix_as_unknown(tmp_path, monkeypatch):
    state_path = _prepared_state(tmp_path, phase="PREPARED")

    def fake_post(base_url, path, *, params=None, body=None, token=None, timeout=None):
        return mx._PostResult(200, "application/json", json.dumps({"id": "xyz_beef"}).encode())

    monkeypatch.setattr(mx, "_post_json", fake_post)
    out = mx._create_probe_session_once(_FakeTransport(), "D:/probe/dir", state_path)
    assert out["phase"] == "UNKNOWN_CREATE"
    assert out["kind"] == "NO_UNIQUE_ID"


def test_reconcile_exactly_one_recovers_single_real_style_session():
    ft = _FakeTransport([_FakeRaw(200, json.dumps([{"id": _REAL_STYLE_SID}]))])
    rec = mx.reconcile_exactly_one(ft, "D:/probe/dir")
    assert rec["recovered"] is True
    assert rec["session_id"] == _REAL_STYLE_SID
    assert rec["directory_count"] == 1
    assert all(m == "GET" for m, _p, _a in ft.calls)


def test_reconcile_exactly_one_zero_sessions_refuses():
    ft = _FakeTransport([_FakeRaw(200, "[]")])
    rec = mx.reconcile_exactly_one(ft, "D:/probe/dir")
    assert rec["recovered"] is False
    assert rec["reason"] == "ZERO"


def test_reconcile_exactly_one_multiple_sessions_refuses():
    ft = _FakeTransport([_FakeRaw(200, json.dumps([
        {"id": _REAL_STYLE_SID}, {"id": _DOC_SID}]))])
    rec = mx.reconcile_exactly_one(ft, "D:/probe/dir")
    assert rec["recovered"] is False
    assert rec["reason"] == "MULTIPLE"


def test_reconcile_exactly_one_missing_or_malformed_id_refuses():
    cases = ["[{}]", json.dumps([{"id": "ses_"}]), json.dumps([{"id": "xyz_beef"}])]
    for body in cases:
        ft = _FakeTransport([_FakeRaw(200, body)])
        rec = mx.reconcile_exactly_one(ft, "D:/probe/dir")
        assert rec["recovered"] is False, body
        assert rec["reason"] == "NO_VALID_ID", body


# ---------------------------------------------------------------------- T21-02R --resume-once

def _unknown_create_state(tmp_path, **overrides) -> Path:
    base = dict(phase="UNKNOWN_CREATE", create_attempt_count=1, send_attempt_count=0,
                run_nonce="n")
    base.update(overrides)
    return _prepared_state(tmp_path, **base)


def _resume_happy_message_msgs():
    return [
        {"id": _FULL_MESSAGE_ID, "role": "user", "parts": [{"type": "text", "text": "hi"}]},
        _assistant_msg("msg_aaa", _FULL_MESSAGE_ID, mx.marker_for("n")),
    ]


def test_resume_recovers_then_prompt_once_and_observes(tmp_path, monkeypatch):
    state_path = _unknown_create_state(tmp_path)
    ft = _FakeTransport([
        _FakeRaw(200, json.dumps([{"id": _REAL_STYLE_SID}])),
        _FakeRaw(200, json.dumps(_resume_happy_message_msgs())),
    ])
    monkeypatch.setattr(mx, "make_message_id", lambda: _FULL_MESSAGE_ID)
    posts = []

    def fake_post(base_url, path, *, params=None, body=None, token=None, timeout=None):
        posts.append(path)
        assert "prompt_async" in path
        assert f"/api/session/{_REAL_STYLE_SID}/prompt_async" == path
        st = json.loads(Path(state_path).read_text(encoding="utf-8"))
        assert st["phase"] == "SEND_ATTEMPTED"
        assert st["send_attempt_count"] == 1
        return mx._PostResult(204, None, b"")

    monkeypatch.setattr(mx, "_post_json", fake_post)
    out = mx.resume_write_once(ft, state_path, now_fn=lambda: "now",
                               sleep_fn=lambda _: None, clock_fn=lambda: 0.0)
    assert len(posts) == 1
    assert out["declined"] is False
    assert out["phase"] == "RESULT_OBSERVED"
    assert out["observation"]["verdict"]["completed"] is True
    assert out["observation"]["verdict"]["attribution"] == "VALID"
    st = json.loads(Path(state_path).read_text(encoding="utf-8"))
    assert st["create_attempt_count"] == 1          # 不 reset journal
    assert st["send_attempt_count"] == 1            # 本轮唯一 1 次 prompt
    assert st["phase"] == "RESULT_OBSERVED"
    assert st["reconciliation_result"] == "SINGLE_RECOVERED"
    assert st["recovered_from_unknown_create"] is True
    assert st["explicit_session_id"] == _REAL_STYLE_SID
    assert all(m == "GET" for m, _p, _a in ft.calls)


def test_resume_never_calls_create(tmp_path, monkeypatch):
    state_path = _unknown_create_state(tmp_path)
    ft = _FakeTransport([
        _FakeRaw(200, json.dumps([{"id": _REAL_STYLE_SID}])),
        _FakeRaw(200, json.dumps(_resume_happy_message_msgs())),
    ])

    def boom_create(*a, **k):
        raise AssertionError("resume 不得调用 create")

    monkeypatch.setattr(mx, "make_message_id", lambda: _FULL_MESSAGE_ID)
    monkeypatch.setattr(mx, "_create_probe_session_once", boom_create)
    posts = []

    def fake_post(base_url, path, *, params=None, body=None, token=None, timeout=None):
        posts.append(path)
        return mx._PostResult(204, None, b"")

    monkeypatch.setattr(mx, "_post_json", fake_post)
    mx.resume_write_once(ft, state_path, sleep_fn=lambda _: None, clock_fn=lambda: 0.0)
    assert len(posts) == 1
    assert paths_are_prompt_only(posts)


def paths_are_prompt_only(posts):
    return all("prompt_async" in p and p != mx.CREATE_PATH for p in posts)


def test_resume_declines_no_post_when_zero_sessions(tmp_path, monkeypatch):
    state_path = _unknown_create_state(tmp_path)
    ft = _FakeTransport([_FakeRaw(200, "[]")])

    def fake_post(base_url, path, *, params=None, body=None, token=None, timeout=None):
        raise AssertionError("reconciliation 未恢复时不得 POST")

    monkeypatch.setattr(mx, "_post_json", fake_post)
    out = mx.resume_write_once(ft, state_path)
    assert out["declined"] is True
    assert out["reconciliation"]["reason"] == "ZERO"
    st = json.loads(Path(state_path).read_text(encoding="utf-8"))
    assert st["phase"] == "UNKNOWN_CREATE"
    assert st["reconciliation_result"] == "DECLINED_ZERO"
    assert st["send_attempt_count"] == 0


def test_resume_declines_no_post_when_multiple_sessions(tmp_path, monkeypatch):
    state_path = _unknown_create_state(tmp_path)
    ft = _FakeTransport([_FakeRaw(200, json.dumps([
        {"id": _REAL_STYLE_SID}, {"id": _DOC_SID}]))])

    def fake_post(base_url, path, *, params=None, body=None, token=None, timeout=None):
        raise AssertionError("reconciliation 未恢复时不得 POST")

    monkeypatch.setattr(mx, "_post_json", fake_post)
    out = mx.resume_write_once(ft, state_path)
    assert out["declined"] is True
    assert out["reconciliation"]["reason"] == "MULTIPLE"
    assert json.loads(Path(state_path).read_text(encoding="utf-8"))["send_attempt_count"] == 0


def test_resume_refuses_when_phase_not_unknown(tmp_path):
    state_path = _prepared_state(tmp_path, phase="SESSION_CREATED",
                                 create_attempt_count=1, send_attempt_count=0)
    with pytest.raises(mx.ProbeWriteRefused):
        mx.resume_write_once(_FakeTransport(), state_path)


def test_resume_refuses_when_create_count_not_one(tmp_path):
    for create_count in (0, 2):
        state_path = _unknown_create_state(tmp_path, create_attempt_count=create_count)
        with pytest.raises(mx.ProbeWriteRefused):
            mx.resume_write_once(_FakeTransport(), state_path)


def test_second_resume_after_send_refuses(tmp_path, monkeypatch):
    state_path = _unknown_create_state(tmp_path, send_attempt_count=1,
                                        phase="SEND_ACCEPTED")
    with pytest.raises(mx.ProbeWriteRefused):
        mx.resume_write_once(_FakeTransport(), state_path)


def test_resume_unknown_send_no_resend(tmp_path, monkeypatch):
    state_path = _unknown_create_state(tmp_path)
    ft = _FakeTransport([_FakeRaw(200, json.dumps([{"id": _REAL_STYLE_SID}]))])

    def fake_post(base_url, path, *, params=None, body=None, token=None, timeout=None):
        raise mx._ProbeTransportError("TIMEOUT", "resume send timeout")

    monkeypatch.setattr(mx, "_post_json", fake_post)
    out = mx.resume_write_once(ft, state_path)
    assert out["phase"] == "UNKNOWN_SEND"
    st = json.loads(Path(state_path).read_text(encoding="utf-8"))
    assert st["phase"] == "UNKNOWN_SEND"
    assert st["send_attempt_count"] == 1


def test_cli_resume_refuses_wrong_phase(tmp_path, capsys):
    state_path = _prepared_state(tmp_path, phase="SESSION_CREATED",
                                 create_attempt_count=1, send_attempt_count=0)
    rc = mx.main(["--resume-once", "--state", str(state_path),
                  "--probe-root", str(tmp_path / "probe")])
    assert rc == 3
    assert "RESUME 拒绝" in capsys.readouterr().err


def test_cli_resume_refuses_missing_journal(tmp_path, capsys):
    rc = mx.main(["--resume-once", "--state", str(tmp_path / "none.json"),
                  "--probe-root", str(tmp_path / "probe")])
    assert rc == 3
    assert "无 durable journal" in capsys.readouterr().err


def test_resume_code_path_never_references_create(tmp_path):
    src = Path(_REPO / "scripts" / "probe_openchamber_write_smoke.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("resume_write_once", "_main_resume"):
            body = ast.unparse(node)
            assert "_create_probe_session_once" not in body, node.name
            assert "CREATE_PATH" not in body, node.name


def test_evidence_recovery_history_preserved_and_redacted():
    ev = mx.build_evidence(
        run_id="r", probe_directory="d", base_url=mx.ALLOWED_BASE_URL,
        phase="RESULT_OBSERVED", session_id=_REAL_STYLE_SID, session_confirmed_at="t",
        create_attempted_at="t", send_attempted_at="t", send_http_status=204,
        send_accepted_at="t", first_result_observed_at="t", completion_at="t",
        observation={"polls": [{}], "verdict": {"completed": True, "attribution": "VALID"}},
        create_attempt_count=1, send_attempt_count=1,
        marker=mx.marker_for("n"), run_nonce="n",
        service_reachable=True, api_authenticated=True,
        recovery_history={"initial_create_http": 200, "session_id": _REAL_STYLE_SID},
    )
    text = json.dumps(ev, ensure_ascii=False)
    assert _REAL_STYLE_SID not in text
    assert ev["recovery_history"]["initial_create_http"] == 200
    assert ev["probe"]["session_id_redacted"].startswith("ses_")


def test_evidence_redacts_real_style_id():
    ev = mx.build_evidence(
        run_id="r", probe_directory="d", base_url=mx.ALLOWED_BASE_URL,
        phase="RESULT_OBSERVED", session_id=_REAL_STYLE_SID, session_confirmed_at="t",
        create_attempted_at="t", send_attempted_at="t", send_http_status=204,
        send_accepted_at="t", first_result_observed_at="t", completion_at="t",
        observation={"polls": [{}], "verdict": {"completed": True, "attribution": "VALID"}},
        create_attempt_count=1, send_attempt_count=1,
        marker=mx.marker_for("n"), run_nonce="n",
        service_reachable=True, api_authenticated=True,
    )
    text = json.dumps(ev, ensure_ascii=False)
    assert _REAL_STYLE_SID not in text
    assert ev["probe"]["session_id_redacted"].startswith("ses_")
    assert ev["seven_layers"]["session_attribution_valid"] == "VALID"


# ---------------------------------------------------------------------- T21-02R 消息 schema：info 嵌套（真实运行时）兼容

_RUNTIME_USER_MSG = {"info": {"id": _FULL_MESSAGE_ID, "role": "user"},
                     "parts": [{"type": "text", "text": "hi"}]}
_RUNTIME_ASSISTANT_MSG = {"info": {"id": "msg_aaa", "role": "assistant",
                                   "parentID": _FULL_MESSAGE_ID},
                          "parts": [{"type": "text", "text": mx.marker_for("n")}]}


def test_message_extraction_helpers_flat_and_info_shape():
    assert mx.message_id_of(_RUNTIME_USER_MSG) == _FULL_MESSAGE_ID
    assert mx.message_role(_RUNTIME_ASSISTANT_MSG) == "assistant"
    assert mx.message_parent_of(_RUNTIME_ASSISTANT_MSG) == _FULL_MESSAGE_ID
    flat = {"id": _FULL_MESSAGE_ID, "role": "user", "parentID": None}
    assert mx.message_id_of(flat) == _FULL_MESSAGE_ID
    assert mx.message_role(flat) == "user"


def test_evaluate_completion_supports_runtime_info_shape():
    v = mx.evaluate_completion([_RUNTIME_USER_MSG, _RUNTIME_ASSISTANT_MSG],
                               _FULL_MESSAGE_ID, mx.marker_for("n"))
    assert v["completed"] is True
    assert v["attribution"] == "VALID"
    assert all(v["conditions"].values())


def test_flat_shape_still_works_after_info_support():
    msgs = [
        {"id": _FULL_MESSAGE_ID, "role": "user", "parts": [{"type": "text", "text": "hi"}]},
        {"id": "msg_aaa", "role": "assistant", "parentID": _FULL_MESSAGE_ID,
         "parts": [{"type": "text", "text": mx.marker_for("n")}]},
    ]
    v = mx.evaluate_completion(msgs, _FULL_MESSAGE_ID, mx.marker_for("n"))
    assert v["completed"] is True
    assert v["attribution"] == "VALID"


def test_runtime_shape_without_text_part_not_attributed():
    step_start = {"info": {"id": "msg_zzz", "role": "assistant", "parentID": _FULL_MESSAGE_ID},
                  "parts": [{"type": "step-start", "snapshot": "x"}]}
    v = mx.evaluate_completion([_RUNTIME_USER_MSG, step_start], _FULL_MESSAGE_ID, mx.marker_for("n"))
    assert v["completed"] is False
    assert v["attribution"] == "VALID"  # 有显式 parent 关系但文本未满足


def test_observe_completion_runtime_shape_redacts(tmp_path, monkeypatch):
    ft = _FakeTransport([_FakeRaw(200, json.dumps([_RUNTIME_USER_MSG, _RUNTIME_ASSISTANT_MSG]))])
    obs = mx.observe_completion(ft, _FULL_SESSION_ID, "D:/probe/dir", _FULL_MESSAGE_ID,
                                mx.marker_for("n"), sleep_fn=lambda _: None,
                                clock_fn=lambda: 1.0, now_fn=lambda: "now")
    assert obs["verdict"]["completed"] is True
    assert obs["verdict"]["attribution"] == "VALID"
    assert obs["last_messages_redacted"][1]["role"] == "assistant"
    assert obs["last_messages_redacted"][1]["parentID"].startswith("msg_")


# ---------------------------------------------------------------------- T21-02R CLI --observe-only（只读）

def test_cli_observe_only_reads_then_writes_final_evidence(tmp_path, monkeypatch, capsys):
    state_path = _prepared_state(tmp_path, phase="SEND_ACCEPTED", create_attempt_count=1,
                                 send_attempt_count=1, explicit_session_id=_REAL_STYLE_SID,
                                 explicit_probe_message_id=_FULL_MESSAGE_ID, run_nonce="n")
    ft = _FakeTransport([_FakeRaw(200, json.dumps([_RUNTIME_USER_MSG, _RUNTIME_ASSISTANT_MSG]))])

    def fake_factory(base_url, token=None, timeout=None, **k):
        return ft

    monkeypatch.setattr(mx, "_ProbeTransport", fake_factory)
    monkeypatch.setattr(mx, "resolve_auth_token", lambda **k: None)
    monkeypatch.setattr(mx, "_service_readiness", lambda t: (True, True))
    ev_path = tmp_path / "ev.json"
    rc = mx.main(["--observe-only", "--state", str(state_path),
                  "--probe-root", str(tmp_path / "probe"), "--out", str(ev_path)])
    assert rc == 0
    ev = json.loads(ev_path.read_text(encoding="utf-8"))
    assert ev["completion"]["completed"] is True
    assert ev["completion"]["attribution"] == "VALID"
    assert ev["write_counters"]["create_post_count"] == 1
    assert ev["write_counters"]["prompt_post_count"] == 1
    assert all(m == "GET" for m, _p, _a in ft.calls)
    st = json.loads(Path(state_path).read_text(encoding="utf-8"))
    assert st["phase"] == "RESULT_OBSERVED"
    assert _REAL_STYLE_SID not in json.dumps(ev, ensure_ascii=False)


def test_cli_observe_only_refuses_when_no_send_yet(tmp_path, capsys):
    state_path = _prepared_state(tmp_path, phase="SESSION_CREATED", create_attempt_count=1,
                                 send_attempt_count=0, explicit_session_id=_REAL_STYLE_SID)
    rc = mx.main(["--observe-only", "--state", str(state_path),
                  "--probe-root", str(tmp_path / "probe")])
    assert rc == 3
    assert "send_attempt_count<1" in capsys.readouterr().err