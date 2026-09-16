"""T22-04：OpenChamber UNKNOWN send GET-only Reconciliation 自动测试（全程 fake，0 真实网络）。

覆盖 T22-04 卡（44+ 场景）：
- 01-06：reader 结构性 GET-only / 无 POST 能力 / loopback 白名单 / endpoint、session、
  expected-msg-id 输入 fail closed（0 HTTP）；
- 07-10：精确 GET endpoint 路径（session list / message list / status）+ directory 编码；
- 11-18：接受判定——精确 planned id + role=user 唯一证据；无 last-message 启发式；
  parent 关系只计数不作为必要条件；
- 19-22：status 仅 advisory，busy/idle 单证不能接受，status 失败不抹除 message proof；
- 23-28：协调器 UNKNOWN→ACCEPTED、remote_user_id 保留、evidence 合并、不改 result/task/attempt；
- 29-33：非 UNKNOWN / 非发送 kind / ledger 缺失 → 0 GET；
- 34-37：GET 失败 → READ_FAILED 且保持 UNKNOWN，无 retry；
- 38-39：GET 全部在 SQLite 事务外，finalize 事务内无 HTTP（与 T10 网络边界一致）；
- 40-41：并发 UNKNOWN→ACCEPTED 幂等 / UNKNOWN→REJECTED 不被覆盖；
- 42-44：全程 0 prompt_async、0 create、0 write opener。

全部 fake opener / fake reader，无任何真实网络。
"""

from __future__ import annotations

import io
import json
import socket
import sys
import urllib.error
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from adapters.openchamber_reconciliation import (  # noqa: E402
    OpenChamberReconciliationInputError,
    OpenChamberUnknownReconciliationReader,
    ReconciliationObservation,
)
from app.openchamber_unknown_reconciler import (  # noqa: E402
    OpenChamberUnknownReconciler,
    ReconciliationOutcome,
)
from storage.database import Database  # noqa: E402
from storage.operation_store import OperationStore, FinalizeOutcome  # noqa: E402

_BASE = "http://127.0.0.1:57123"
_DIR = r"D:\AIwork\proj dir"
_DIR_ENCODED = r"D:\AIwork\AI proj\新建 文件夹"
_SID = "ses_abc123"
_PLANNED = "msg_0123456789abcdef0123456789abcdef"
_SECRET_TOKEN = "tok-TopSecret-9f8e"
_T0 = "2026-10-01T09:00:00+00:00"


# =====================================================================
# 测试基础设施：FakeResponse / FakeOpener / FakeReader / DB 辅助
# =====================================================================


class FakeResponse:
    def __init__(self, status: int, body: str | bytes, content_type="application/json",
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
        "http://fake.local/", code, "mock http error",
        {"Content-Type": "application/json"},
        io.BytesIO(body.encode("utf-8")),
    )


class FakeOpener:
    """脚本式 fake opener：按序消费响应，记录全部请求与计数。"""

    def __init__(self, script=None, default=None):
        self.script = list(script or [])
        self.default = default
        self.calls: list[dict] = []
        self.call_count = 0

    def __call__(self, req):
        self.call_count += 1
        self.calls.append({
            "method": req.get_method(),
            "url": req.full_url,
            "headers": {k: v for k, v in req.header_items()},
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
        status, body = item
        return FakeResponse(status, body)


class FakeReader:
    """与 observe_once 同签名的假 reader：返回预设 observation / 触发回调 / 抛输入错误。"""

    def __init__(self, observation=None, *, on_observe=None, input_error=None):
        self.observation = observation
        self.on_observe = on_observe
        self.input_error = input_error
        self.calls: list[dict] = []

    def observe_once(self, *, endpoint, session_id, expected_user_message_id):
        self.calls.append({
            "endpoint": endpoint, "session_id": session_id,
            "expected_user_message_id": expected_user_message_id,
        })
        if self.input_error is not None:
            kind, detail = self.input_error
            raise OpenChamberReconciliationInputError(kind, detail)
        if self.on_observe is not None:
            self.on_observe(self.calls[-1])
        if self.observation is not None:
            return self.observation
        return ReconciliationObservation()


def _msg(mid: str, role: str, parentID=None) -> dict:
    parts = [] if role != "user" else []
    return {"info": {"id": mid, "role": role, "parentID": parentID}, "parts": parts}


def _ok_script(sessions=None, messages=None, status=None) -> list:
    sessions = sessions if sessions is not None else [{"id": _SID}]
    messages = messages if messages is not None else [_msg(_PLANNED, "user")]
    status = status if status is not None else {_SID: {"active": True}}
    return [
        (200, json.dumps(sessions)),
        (200, json.dumps(messages)),
        (200, json.dumps(status)),
    ]


def _reader(*, base_url: str = _BASE, directory: str = _DIR, token: str | None = _SECRET_TOKEN,
            opener=None) -> OpenChamberUnknownReconciliationReader:
    return OpenChamberUnknownReconciliationReader(
        base_url, directory=directory, token=token, opener=opener
    )


def _observe(reader, *, endpoint: str = _BASE, session_id: str = _SID,
             expected: str = _PLANNED) -> ReconciliationObservation:
    return reader.observe_once(
        endpoint=endpoint, session_id=session_id, expected_user_message_id=expected
    )


@pytest.fixture
def db_env(tmp_path):
    db = Database(tmp_path / "t22rc.sqlite")
    db.open()
    yield db
    db.close()


def _insert_operation(db, *, op_id="op-rc-1000", key="k-rc-1000", kind="INITIAL_SEND",
                      state="UNKNOWN", endpoint=_BASE, session_id=_SID, planned=_PLANNED,
                      evidence='{"timeout": true, "attempt": 1}') -> None:
    db.connection.execute(
        "INSERT INTO operations (operation_id, operation_key, kind, task_key, attempt_id,"
        " authority_epoch, control_revision, endpoint, session_id, project_key,"
        " interruption_id, state, pre_snapshot_json, prompt_hash, prompt_text,"
        " remote_user_id, evidence_json, created_at, updated_at, finalized_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (op_id, key, kind, None, None, None, None, endpoint, session_id, None, None,
         state, "{}", None, None, planned, evidence, _T0, _T0, None),
    )


def _read_op(db, op_id: str):
    row = db.connection.execute(
        "SELECT operation_id, operation_key, kind, endpoint, session_id, remote_user_id,"
        " state, evidence_json FROM operations WHERE operation_id=?", (op_id,)
    ).fetchone()
    return row


def _count(db, table: str) -> int:
    return int(db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _make_reconciler(db, reader) -> OpenChamberUnknownReconciler:
    return OpenChamberUnknownReconciler(db, reader=reader)


# =====================================================================
# 01-02：reader 结构性 GET-only / 无 POST 能力
# =====================================================================

def _module_code_source(cls) -> str:
    """模块源码去掉首部 docstring，仅对可执行代码做 token 结构检查。"""
    text = Path(cls.__module__.replace(".", "/") + ".py").read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith('"""'):
        start = text.find('"""')
        end = text.find('"""', start + 3)
        text = text[end + 3:]
    return text


def test_reader_is_get_only_structural():
    assert hasattr(OpenChamberUnknownReconciliationReader, "observe_once")
    public = [name for name in dir(OpenChamberUnknownReconciliationReader)
              if not name.startswith("_") and (hasattr(getattr(
                  OpenChamberUnknownReconciliationReader, name), "__call__"))]
    assert public == ["observe_once"]   # 公开面只有 observe_once，无其他可调用入口
    source = _module_code_source(OpenChamberUnknownReconciliationReader)
    for banned in ("method=\"POST\"", "method='POST'", "send_once", "urllib.request.Request",
                   "_non_redirecting_write_opener", "import requests", "from requests"):
        assert banned not in source


def test_reader_no_post_capability_runtime():
    opener = FakeOpener(script=_ok_script())
    reader = _reader(opener=opener)
    obs = _observe(reader)
    assert obs.exact_user_message_observed is True
    assert [c["method"] for c in opener.calls] == ["GET", "GET", "GET"]
    for call in opener.calls:
        assert "prompt_async" not in call["url"]
        assert "/create" not in call["url"]


# =====================================================================
# 03-06：构造 / 输入 fail closed（0 HTTP）
# =====================================================================

def test_reader_loopback_only_construction():
    for url in ("http://192.0.2.10:57123", "http://127.0.0.2:57123",
                "http://example.com/", "https://openchamber.example.com"):
        with pytest.raises(OpenChamberReconciliationInputError) as ctx:
            _reader(base_url=url)
        assert ctx.value.kind == "NON_LOOPBACK_TARGET"


def test_reader_accepts_exact_loopback_hosts():
    for url in ("http://localhost:57123", "http://127.0.0.1:57123", "http://[::1]:57123"):
        r = _reader(base_url=url, opener=FakeOpener(script=[]))
        assert r.base_url == url.rstrip("/")


def test_observe_endpoint_mismatch_fail_closed():
    opener = FakeOpener(script=[])
    reader = _reader(opener=opener)
    with pytest.raises(OpenChamberReconciliationInputError) as ctx:
        _observe(reader, endpoint="http://127.0.0.1:57124")
    assert ctx.value.kind == "ENDPOINT_MISMATCH"
    assert opener.calls == []


@pytest.mark.parametrize("sid", ["bad123", "sigma_1", "ses_", "sess_", "msg_x", ""])
def test_observe_invalid_session_id_fail_closed(sid):
    opener = FakeOpener(script=[])
    reader = _reader(opener=opener)
    with pytest.raises(OpenChamberReconciliationInputError) as ctx:
        _observe(reader, session_id=sid)
    assert ctx.value.kind == "INVALID_SESSION_ID"
    assert opener.calls == []


@pytest.mark.parametrize(("mid", "kind"), [
    ("   ", "MISSING_EXPECTED_ID"),
    ("", "MISSING_EXPECTED_ID"),
    ("user-abc", "INVALID_EXPECTED_ID"),
    ("id-123", "INVALID_EXPECTED_ID"),
])
def test_observe_invalid_expected_id_fail_closed(mid, kind):
    opener = FakeOpener(script=[])
    reader = _reader(opener=opener)
    with pytest.raises(OpenChamberReconciliationInputError) as ctx:
        _observe(reader, expected=mid)
    assert ctx.value.kind == kind
    assert opener.calls == []


# =====================================================================
# 07-10：精确 GET endpoint 路径 + directory 编码
# =====================================================================

def test_observe_exact_session_list_path():
    opener = FakeOpener(script=_ok_script())
    _observe(_reader(opener=opener))
    assert urllib.parse.urlsplit(opener.calls[0]["url"]).path == "/api/session"


def test_observe_exact_message_list_path():
    opener = FakeOpener(script=_ok_script())
    _observe(_reader(opener=opener))
    assert urllib.parse.urlsplit(opener.calls[1]["url"]).path == f"/api/session/{_SID}/message"


def test_observe_exact_status_path():
    opener = FakeOpener(script=_ok_script())
    _observe(_reader(opener=opener))
    assert urllib.parse.urlsplit(opener.calls[2]["url"]).path == "/api/session/status"
    assert [c["method"] for c in opener.calls] == ["GET", "GET", "GET"]


def test_observe_directory_encoded_in_all_gets():
    opener = FakeOpener(script=_ok_script())
    _observe(_reader(directory=_DIR_ENCODED, opener=opener))
    for call in opener.calls:
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(call["url"]).query)
        assert qs["directory"] == [_DIR_ENCODED]


# =====================================================================
# 11-18：接受判定——精确 identity 唯一证据，无启发式
# =====================================================================

def test_exact_planned_id_plus_user_is_proof():
    opener = FakeOpener(script=_ok_script(
        messages=[_msg(_PLANNED, "user"), _msg("msg_other_0", "user")],
    ))
    obs = _observe(_reader(opener=opener))
    assert obs.session_exists is True
    assert obs.message_count == 2
    assert obs.exact_id_match_count == 1
    assert obs.exact_user_match_count == 1
    assert obs.exact_user_message_observed is True


def test_planned_id_absent_still_no_proof():
    opener = FakeOpener(script=_ok_script(
        messages=[_msg("msg_x1", "user"), _msg("msg_x2", "user")],
    ))
    obs = _observe(_reader(opener=opener))
    assert obs.exact_id_match_count == 0
    assert obs.exact_user_match_count == 0
    assert obs.exact_user_message_observed is False


def test_same_id_wrong_role_no_proof():
    opener = FakeOpener(script=_ok_script(messages=[_msg(_PLANNED, "assistant")]))
    obs = _observe(_reader(opener=opener))
    assert obs.exact_id_match_count == 1
    assert obs.exact_user_match_count == 0
    assert obs.exact_user_message_observed is False


def test_duplicate_exact_id_no_proof():
    opener = FakeOpener(script=_ok_script(
        messages=[_msg(_PLANNED, "user"), _msg(_PLANNED, "user")],
    ))
    obs = _observe(_reader(opener=opener))
    assert obs.exact_id_match_count == 2
    assert obs.exact_user_message_observed is False   # 唯一性条件：恰好 1 个


def test_assistant_parent_match_counted():
    opener = FakeOpener(script=_ok_script(
        messages=[
            _msg(_PLANNED, "user"),
            _msg("msg_assistant_1", "assistant", parentID=_PLANNED),
        ],
    ))
    obs = _observe(_reader(opener=opener))
    assert obs.assistant_parent_match_count == 1
    assert obs.assistant_child_message_ids == ("msg_assistant_1",)
    assert obs.exact_user_message_observed is True


def test_no_parent_still_allows_accepted_proof():
    opener = FakeOpener(script=_ok_script(messages=[_msg(_PLANNED, "user", parentID=None)]))
    obs = _observe(_reader(opener=opener))
    assert obs.assistant_parent_match_count == 0
    assert obs.exact_user_message_observed is True   # parent 不是必要条件


def test_parent_without_user_match_no_accept():
    opener = FakeOpener(script=_ok_script(
        messages=[_msg("msg_assistant_1", "assistant", parentID=_PLANNED)],
    ))
    obs = _observe(_reader(opener=opener))
    assert obs.assistant_parent_match_count == 1
    assert obs.exact_user_match_count == 0
    assert obs.exact_user_message_observed is False   # parent 关系不能单独证明投递


def test_no_last_message_heuristic():
    # planned 出现但不在最后：只按精确 id 判定，绝不把 last/newest 当证据
    opener = FakeOpener(script=_ok_script(
        messages=[_msg(_PLANNED, "user"), _msg("msg_newer", "user")],
    ))
    obs = _observe(_reader(opener=opener))
    assert obs.exact_user_match_count == 1
    assert obs.exact_user_message_observed is True
    # 最后一条是不同 id 的 user：若用启发式会误判，但精确规则下仍无证据
    opener2 = FakeOpener(script=_ok_script(
        messages=[_msg("msg_old", "user"), _msg("msg_last", "user")],
    ))
    obs2 = _observe(_reader(opener=opener2))
    assert obs2.exact_user_message_observed is False


# =====================================================================
# 19-22：status 仅 advisory
# =====================================================================

def test_status_busy_alone_does_not_accept():
    opener = FakeOpener(script=_ok_script(
        messages=[_msg("msg_other", "user")],
        status={_SID: {"state": "busy"}},
    ))
    obs = _observe(_reader(opener=opener))
    assert obs.status_entry_present is True
    assert obs.exact_user_match_count == 0
    assert obs.exact_user_message_observed is False


def test_status_idle_alone_does_not_accept():
    opener = FakeOpener(script=_ok_script(
        messages=[_msg("msg_other", "user")], status={},
    ))
    obs = _observe(_reader(opener=opener))
    assert obs.status_entry_present is False
    assert obs.exact_user_message_observed is False


def test_missing_status_does_not_reject():
    opener = FakeOpener(script=_ok_script()[:2] + [_http_error(500, "status down")])
    obs = _observe(_reader(opener=opener))
    assert obs.exact_user_message_observed is True      # 精确 message proof 保留
    assert obs.status_entry_present is None
    assert obs.status_read_error == "http"


def test_status_failure_after_message_proof_keeps_proof():
    opener = FakeOpener(script=_ok_script()[:2] + [socket.timeout("t")])
    obs = _observe(_reader(opener=opener))
    assert obs.exact_user_message_observed is True
    assert obs.message_count == 1
    assert obs.exact_id_match_count == 1
    assert obs.status_entry_present is None
    assert obs.status_read_error == "TIMEOUT"


# =====================================================================
# 23-28：协调器 UNKNOWN→ACCEPTED
# =====================================================================

def test_unknown_exact_match_accepted_confirmed(db_env):
    _insert_operation(db_env)
    opener = FakeOpener(script=_ok_script())
    result = _make_reconciler(db_env, _reader(opener=opener)).reconcile("op-rc-1000")
    assert result.outcome is ReconciliationOutcome.ACCEPTED_CONFIRMED
    assert result.state == "ACCEPTED"
    assert result.exact_user_match_count == 1
    row = _read_op(db_env, "op-rc-1000")
    assert row[6] == "ACCEPTED"


def test_remote_user_id_preserved(db_env):
    _insert_operation(db_env)
    opener = FakeOpener(script=_ok_script())
    _make_reconciler(db_env, _reader(opener=opener)).reconcile("op-rc-1000")
    _, _, _, _, _, planned, _, _ = _read_op(db_env, "op-rc-1000")
    assert planned == _PLANNED   # planned remote_user_id 原样保留


def test_old_evidence_merged_preserved(db_env):
    _insert_operation(db_env, evidence='{"timeout": true, "transport_error": "TIMEOUT"}')
    opener = FakeOpener(script=_ok_script())
    _make_reconciler(db_env, _reader(opener=opener)).reconcile("op-rc-1000")
    evidence = json.loads(_read_op(db_env, "op-rc-1000")[7])
    assert evidence["timeout"] is True                      # 原 evidence 未被擦除
    assert evidence["transport_error"] == "TIMEOUT"
    rc = evidence["unknown_reconciliation"]
    assert rc["decision"] == "accepted_confirmed"
    assert rc["source"] == "GET_ONLY"
    assert rc["exact_id_match_count"] == 1
    assert rc["exact_user_match_count"] == 1
    assert rc["assistant_parent_match_count"] == 0
    assert rc["status_entry_present"] is True
    assert "message_count" in rc


def test_no_result_created(db_env):
    _insert_operation(db_env)
    opener = FakeOpener(script=_ok_script())
    _make_reconciler(db_env, _reader(opener=opener)).reconcile("op-rc-1000")
    assert _count(db_env, "results") == 0
    assert _count(db_env, "outbox") == 0


def test_no_task_state_changed(db_env):
    _insert_operation(db_env)
    opener = FakeOpener(script=_ok_script())
    _make_reconciler(db_env, _reader(opener=opener)).reconcile("op-rc-1000")
    assert _count(db_env, "tasks") == 0          # 未创建/未修改 task


def test_no_attempt_state_changed(db_env):
    _insert_operation(db_env)
    opener = FakeOpener(script=_ok_script())
    _make_reconciler(db_env, _reader(opener=opener)).reconcile("op-rc-1000")
    assert _count(db_env, "attempts") == 0       # 未创建/未修改 attempt


# =====================================================================
# 29-33：前置状态 / kind / ledger 前置校验（0 GET）
# =====================================================================

@pytest.mark.parametrize(("state", "outcome"), [
    ("ACCEPTED", ReconciliationOutcome.ALREADY_ACCEPTED),
    ("REJECTED", ReconciliationOutcome.NOT_UNKNOWN),
    ("PREPARED", ReconciliationOutcome.NOT_UNKNOWN),
    ("SENDING", ReconciliationOutcome.NOT_UNKNOWN),
])
def test_non_unknown_states_zero_get(db_env, state, outcome):
    _insert_operation(db_env, state=state, op_id="op-rc-nzu", key="k-rc-nzu")
    reader = FakeReader(observation=ReconciliationObservation(
        session_exists=True, exact_id_match_count=1, exact_user_match_count=1,
        status_entry_present=True,
    ))
    result = _make_reconciler(db_env, reader).reconcile("op-rc-nzu")
    assert result.outcome is outcome
    assert reader.calls == []                    # 0 GET


def test_unsupported_kind_zero_get(db_env):
    _insert_operation(db_env, kind="CREATE_SESSION", op_id="op-rc-cs", key="k-rc-cs")
    reader = FakeReader(observation=ReconciliationObservation(
        session_exists=True, exact_id_match_count=1, exact_user_match_count=1,
    ))
    result = _make_reconciler(db_env, reader).reconcile("op-rc-cs")
    assert result.outcome is ReconciliationOutcome.UNSUPPORTED_KIND
    assert reader.calls == []


def test_missing_session_id_invalid_ledger(db_env):
    _insert_operation(db_env, session_id=None, op_id="op-rc-ns", key="k-rc-ns")
    reader = FakeReader()
    result = _make_reconciler(db_env, reader).reconcile("op-rc-ns")
    assert result.outcome is ReconciliationOutcome.INVALID_LEDGER
    assert reader.calls == []


def test_missing_planned_id_invalid_ledger(db_env):
    _insert_operation(db_env, planned=None, op_id="op-rc-np", key="k-rc-np")
    reader = FakeReader()
    result = _make_reconciler(db_env, reader).reconcile("op-rc-np")
    assert result.outcome is ReconciliationOutcome.INVALID_LEDGER
    assert reader.calls == []


def test_malformed_evidence_invalid_ledger(db_env):
    _insert_operation(db_env, evidence="{not json", op_id="op-rc-me", key="k-rc-me")
    reader = FakeReader()
    result = _make_reconciler(db_env, reader).reconcile("op-rc-me")
    assert result.outcome is ReconciliationOutcome.INVALID_LEDGER
    assert reader.calls == []


# =====================================================================
# 34-37：GET 失败 → READ_FAILED 且保持 UNKNOWN，无 retry
# =====================================================================

def test_get_timeout_read_failed_remains_unknown(db_env):
    _insert_operation(db_env, op_id="op-rc-to", key="k-rc-to")
    opener = FakeOpener(script=[socket.timeout("t")])
    result = _make_reconciler(db_env, _reader(opener=opener)).reconcile("op-rc-to")
    assert result.outcome is ReconciliationOutcome.READ_FAILED
    assert _read_op(db_env, "op-rc-to")[6] == "UNKNOWN"
    assert opener.call_count == 1                # 无 retry
    assert "TIMEOUT" in result.detail


def test_get_auth_error_read_failed_remains_unknown(db_env):
    _insert_operation(db_env, op_id="op-rc-au", key="k-rc-au")
    opener = FakeOpener(script=[_http_error(403, '{"error":"forbidden"}')])
    result = _make_reconciler(db_env, _reader(opener=opener)).reconcile("op-rc-au")
    assert result.outcome is ReconciliationOutcome.READ_FAILED
    assert _read_op(db_env, "op-rc-au")[6] == "UNKNOWN"
    assert opener.call_count == 1


def test_malformed_messages_read_failed_remains_unknown(db_env):
    _insert_operation(db_env, op_id="op-rc-mm", key="k-rc-mm")
    opener = FakeOpener(script=[
        (200, json.dumps([{"id": _SID}])),
        (200, json.dumps([{"info": {"id": 42, "role": "user"}, "parts": []}])),
    ])
    result = _make_reconciler(db_env, _reader(opener=opener)).reconcile("op-rc-mm")
    assert result.outcome is ReconciliationOutcome.READ_FAILED
    assert _read_op(db_env, "op-rc-mm")[6] == "UNKNOWN"
    assert opener.call_count == 2                # 会话 GET + 消息 GET，无状态 GET / 无 retry
    assert "malformed_messages" in result.detail


def test_malformed_json_no_retry(db_env):
    _insert_operation(db_env, op_id="op-rc-mj", key="k-rc-mj")
    opener = FakeOpener(script=[
        (200, json.dumps([{"id": _SID}])),
        (200, "{bad json"),
    ])
    result = _make_reconciler(db_env, _reader(opener=opener)).reconcile("op-rc-mj")
    assert result.outcome is ReconciliationOutcome.READ_FAILED
    assert _read_op(db_env, "op-rc-mj")[6] == "UNKNOWN"
    assert opener.call_count == 2
    assert "malformed_json" in result.detail


# =====================================================================
# 38-39：网络事务边界（GET 在事务外 / finalize 内无 HTTP）
# =====================================================================

def test_gets_outside_db_transaction(db_env):
    _insert_operation(db_env, op_id="op-rc-tx", key="k-rc-tx")
    flags: list[bool] = []

    proof = ReconciliationObservation(
        session_exists=True, message_count=1, exact_id_match_count=1,
        exact_user_match_count=1, status_entry_present=True,
    )

    def note(record) -> None:
        flags.append(db_env.in_transaction)

    reader = FakeReader(observation=proof, on_observe=note)
    result = _make_reconciler(db_env, reader).reconcile("op-rc-tx")
    assert result.outcome is ReconciliationOutcome.ACCEPTED_CONFIRMED
    assert flags and all(flag is False for flag in flags)   # GET 观察发生在任何事务外


def test_finalize_has_no_http(db_env):
    _insert_operation(db_env, op_id="op-rc-nh", key="k-rc-nh")
    opener = FakeOpener(script=_ok_script())
    result = _make_reconciler(db_env, _reader(opener=opener)).reconcile("op-rc-nh")
    assert result.outcome is ReconciliationOutcome.ACCEPTED_CONFIRMED
    assert _read_op(db_env, "op-rc-nh")[6] == "ACCEPTED"
    assert opener.call_count == 3                # 3 个 GET + 0 个额外 HTTP
    assert [c["method"] for c in opener.calls] == ["GET", "GET", "GET"]
    for call in opener.calls:
        assert "prompt_async" not in call["url"]  # finalize 事务内无任何 HTTP 写


# =====================================================================
# 40-41：并发 CAS
# =====================================================================

def test_concurrent_unknown_to_accepted_idempotent(db_env):
    _insert_operation(db_env, op_id="op-rc-ca", key="k-rc-ca")
    ops = OperationStore(db_env)

    def other_worker_finalizes(call) -> None:
        with db_env.transaction():
            ops.finalize_in(
                db_env.connection, proposal=SimpleNamespace(operation_key="k-rc-ca"),
                operation_id="op-rc-ca", target_state="ACCEPTED",
                evidence_json='{"concurrent": true}', finalized_at=_T0, now=_T0,
            )

    proof = ReconciliationObservation(
        session_exists=True, message_count=1, exact_id_match_count=1,
        exact_user_match_count=1, status_entry_present=True,
    )
    reader = FakeReader(observation=proof, on_observe=other_worker_finalizes)
    result = _make_reconciler(db_env, reader).reconcile("op-rc-ca")
    assert result.outcome is ReconciliationOutcome.ALREADY_ACCEPTED   # 幂等：不二次写
    assert _read_op(db_env, "op-rc-ca")[6] == "ACCEPTED"
    evidence = json.loads(_read_op(db_env, "op-rc-ca")[7])
    assert evidence.get("concurrent") is True     # 另一 worker 的 evidence 未被覆盖


def test_concurrent_unknown_to_rejected_not_overwritten(db_env):
    _insert_operation(db_env, op_id="op-rc-cr", key="k-rc-cr")
    ops = OperationStore(db_env)

    def other_worker_finalizes(call) -> None:
        with db_env.transaction():
            ops.finalize_in(
                db_env.connection, proposal=SimpleNamespace(operation_key="k-rc-cr"),
                operation_id="op-rc-cr", target_state="REJECTED",
                evidence_json='{"concurrent": true}', finalized_at=_T0, now=_T0,
            )

    proof = ReconciliationObservation(
        session_exists=True, message_count=1, exact_id_match_count=1,
        exact_user_match_count=1, status_entry_present=True,
    )
    reader = FakeReader(observation=proof, on_observe=other_worker_finalizes)
    result = _make_reconciler(db_env, reader).reconcile("op-rc-cr")
    assert result.outcome is ReconciliationOutcome.CONCURRENT_CHANGE
    assert _read_op(db_env, "op-rc-cr")[6] == "REJECTED"   # 不反向转换回 ACCEPTED
    evidence = json.loads(_read_op(db_env, "op-rc-cr")[7])
    assert evidence.get("concurrent") is True               # evidence 未被覆盖


# =====================================================================
# 42-44：全程 0 prompt_async / 0 create / 0 write opener
# =====================================================================

def test_no_prompt_async_call(db_env):
    _insert_operation(db_env, op_id="op-rc-np2", key="k-rc-np2")
    opener = FakeOpener(script=_ok_script())
    _make_reconciler(db_env, _reader(opener=opener)).reconcile("op-rc-np2")
    for call in opener.calls:
        assert call["method"] == "GET"
        assert "prompt_async" not in call["url"]


def test_no_create_call(db_env):
    _insert_operation(db_env, op_id="op-rc-nc", key="k-rc-nc")
    opener = FakeOpener(script=_ok_script())
    _make_reconciler(db_env, _reader(opener=opener)).reconcile("op-rc-nc")
    for call in opener.calls:
        assert call["method"] == "GET"
        assert "/session/create" not in call["url"]
        assert "method=POST" not in call["url"]


def test_zero_write_opener_calls(db_env):
    source = _module_code_source(OpenChamberUnknownReconciliationReader)
    assert "send_once" not in source
    assert "_non_redirecting_write_opener" not in source
    assert "urllib.request.Request" not in source
    _insert_operation(db_env, op_id="op-rc-zw", key="k-rc-zw")
    opener = FakeOpener(script=_ok_script())
    _make_reconciler(db_env, _reader(opener=opener)).reconcile("op-rc-zw")
    for call in opener.calls:
        assert call["method"] == "GET"           # 写入侧调用次数 = 0