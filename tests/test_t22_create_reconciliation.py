"""T22-05B：OpenChamber UNKNOWN CREATE_SESSION after-before GET-only 对账自动测试
（全程 fake，0 真实网络）。

覆盖 T22-05B 卡（56+ 场景）：
- 01-09：reader 结构性 GET-only / 无 POST 能力 / loopback 白名单 / endpoint、
  directory 输入 fail closed（0 HTTP）/ 精确 GET 路径 + directory 编码 + 单 GET；
- 10-22：ledger + authoritative baseline 校验（malformed json / 缺 directory /
  错 binding mode / 非法 binding_revision / 缺 title / 重复 before id / count
  不一致 / before 非 list / session_id、remote_user_id 非 NULL / endpoint 空）；
- 23-31：after read 失败全保持 UNKNOWN（malformed json / 非 list / 重复 /
  entry 缺失 id / entry 非对象 / blank id / timeout / http / connection）；
- 32-41：核心差集——before 空+唯一合法→接受、单旧+同旧+一新→接受、多旧+一新→
  接受、乱序→接受；0 new / 2 new / 非法 new / 旧缺失+new / 旧缺失+0 new → UNKNOWN；
- 42-45：title / newest / list-last / slug 启发式一律不使用；
- 46-56：coordinator ACCEPTED 后 evidence 顶层 created_session_id、嵌套对账块、
  原 evidence 保留、session_id/remote_user_id/pre_snapshot 不变、task/attempt/
  lease/result/binding 0 改动；
- 57-62：非 UNKNOWN / 非 CREATE_SESSION kind / operation 不存在 → 0 GET；
- 63-70：GET 事务边界（观察在任何 DB 事务外、finalize 内无 HTTP）、绝不自动
  REJECTED、never blind recreate、repeated reconcile 只重复 GET、并发 CAS。

全部 fake opener / fake reader，无任何真实网络，无任何 create POST。
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

from adapters.openchamber_create_reconciliation import (  # noqa: E402
    CreateReconciliationObservation,
    OpenChamberCreateReconciliationInputError,
    OpenChamberUnknownCreateReconciliationReader,
)
from adapters.openchamber_reconciliation import (  # noqa: E402
    OpenChamberReconciliationInputError,
)
from app.openchamber_create_unknown_reconciler import (  # noqa: E402
    CreateReconciliationOutcome,
    CreateSessionDelta,
    OpenChamberUnknownCreateReconciler,
)
from storage.database import Database  # noqa: E402
from storage.operation_store import OperationStore  # noqa: E402

_BASE = "http://127.0.0.1:57123"
_DIR = r"D:\AIwork\proj dir"
_DIR_ENCODED = r"D:\AIwork\AI proj\新建 文件夹"
_TITLE = "T22-05B baseline title"
_SID_NEW = "ses_05b_new01"
_SID_A = "ses_old_a"
_SID_B = "ses_old_b"
_SECRET_TOKEN = "tok-TopSecret-9f8e"
_T0 = "2026-10-01T09:00:00+00:00"


# =====================================================================
# 基础设施：FakeResponse / FakeOpener / DB 辅助
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


@pytest.fixture
def db_env(tmp_path):
    db = Database(tmp_path / "t22cr.sqlite")
    db.open()
    yield db
    db.close()


def _baseline(*, directory: str = _DIR, binding_mode: str = "PROJECT_ROTATING",
              binding_revision: int = 1, before=(), create_title: str = _TITLE) -> dict:
    before = list(before)
    return {
        "directory": directory,
        "binding_mode": binding_mode,
        "binding_revision": binding_revision,
        "session_ids_before": before,
        "session_count_before": len(before),
        "create_title": create_title,
    }


def _insert_op(db, *, op_id="op-cs-1000", key=None, state="UNKNOWN",
               endpoint=_BASE, kind="CREATE_SESSION", session_id=None, remote=None,
               baseline=None, evidence=None) -> None:
    key = key if key is not None else f"create_session:k-{op_id}"
    baseline = baseline if baseline is not None else _baseline()
    evidence = evidence if evidence is not None else (
        '{"classification": "create_unknown", "http_status": 500,'
        ' "transport_error": "TIMEOUT"}'
    )
    db.connection.execute(
        "INSERT INTO operations (operation_id, operation_key, kind, task_key, attempt_id,"
        " authority_epoch, control_revision, endpoint, session_id, project_key,"
        " interruption_id, state, pre_snapshot_json, prompt_hash, prompt_text,"
        " remote_user_id, evidence_json, created_at, updated_at, finalized_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (op_id, key, kind, None, None, None, None, endpoint, session_id, None, None,
         state, json.dumps(baseline, ensure_ascii=False), None, None, remote,
         evidence, _T0, _T0, None),
    )


def _read_op(db, op_id: str):
    return db.connection.execute(
        "SELECT operation_id, kind, state, session_id, remote_user_id, evidence_json,"
        " pre_snapshot_json, endpoint FROM operations WHERE operation_id=?", (op_id,)
    ).fetchone()


def _count(db, table: str) -> int:
    return int(db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _module_code_source(cls) -> str:
    text = Path(cls.__module__.replace(".", "/") + ".py").read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith('"""'):
        start = text.find('"""')
        end = text.find('"""', start + 3)
        text = text[end + 3:]
    return text


def _reader(*, base_url: str = _BASE, token: str | None = _SECRET_TOKEN,
            opener=None) -> OpenChamberUnknownCreateReconciliationReader:
    return OpenChamberUnknownCreateReconciliationReader(base_url, token=token, opener=opener)


def _reconciler(db, reader) -> OpenChamberUnknownCreateReconciler:
    return OpenChamberUnknownCreateReconciler(db, reader=reader)


def _list_script(after) -> list:
    return [(200, json.dumps([{"id": sid} for sid in after]))]


def _observe(reader, *, endpoint: str = _BASE, directory: str = _DIR):
    return reader.observe_sessions_once(endpoint=endpoint, directory=directory)


def _assert_no_write(opener) -> None:
    assert opener.calls
    for call in opener.calls:
        assert call["method"] == "GET"
        assert "prompt_async" not in call["url"]
        assert "/create" not in call["url"]


# =====================================================================
# 01-09：reader 结构性 GET-only / fail closed / 精确 GET
# =====================================================================


def test_reader_is_get_only_structural():
    assert hasattr(OpenChamberUnknownCreateReconciliationReader, "observe_sessions_once")
    public = [name for name in dir(OpenChamberUnknownCreateReconciliationReader)
              if not name.startswith("_") and (hasattr(getattr(
                  OpenChamberUnknownCreateReconciliationReader, name), "__call__"))]
    assert public == ["observe_sessions_once"]   # 公开面只有一个入口
    source = _module_code_source(OpenChamberUnknownCreateReconciliationReader)
    for banned in ("method=\"POST\"", "method='POST'", "send_once", "create_once",
                   "urllib.request.Request", "_non_redirecting_write_opener",
                   "import requests", "from requests"):
        assert banned not in source


def test_reader_no_post_capability_runtime():
    opener = FakeOpener(script=_list_script([_SID_NEW]))
    obs = _observe(_reader(opener=opener))
    assert obs.read_error is None
    assert obs.session_ids_after == (_SID_NEW,)
    _assert_no_write(opener)


def test_reader_loopback_only_construction():
    for url in ("http://192.0.2.10:57123", "http://127.0.0.2:57123",
                "http://example.com/", "https://openchamber.example.com"):
        with pytest.raises(OpenChamberCreateReconciliationInputError) as ctx:
            _reader(base_url=url)
        assert ctx.value.kind == "NON_LOOPBACK_TARGET"


def test_reader_accepts_exact_loopback_hosts():
    for url in ("http://localhost:57123", "http://127.0.0.1:57123", "http://[::1]:57123"):
        r = _reader(base_url=url, opener=FakeOpener(script=[]))
        assert r.base_url == url.rstrip("/")


def test_observe_endpoint_mismatch_fail_closed():
    opener = FakeOpener(script=[])
    reader = _reader(opener=opener)
    with pytest.raises(OpenChamberCreateReconciliationInputError) as ctx:
        _observe(reader, endpoint="http://127.0.0.1:57124")
    assert ctx.value.kind == "ENDPOINT_MISMATCH"
    assert opener.calls == []


def test_observe_blank_directory_fail_closed():
    opener = FakeOpener(script=[])
    reader = _reader(opener=opener)
    with pytest.raises(OpenChamberCreateReconciliationInputError) as ctx:
        _observe(reader, directory="   ")
    assert ctx.value.kind == "MISSING_DIRECTORY"
    assert opener.calls == []


def test_observe_exact_get_path():
    opener = FakeOpener(script=_list_script([_SID_NEW]))
    _observe(_reader(opener=opener))
    assert opener.call_count == 1
    assert urllib.parse.urlsplit(opener.calls[0]["url"]).path == "/api/session"
    assert [c["method"] for c in opener.calls] == ["GET"]


def test_observe_directory_url_encoding():
    opener = FakeOpener(script=_list_script([_SID_NEW]))
    _observe(_reader(opener=opener), directory=_DIR_ENCODED)
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(opener.calls[0]["url"]).query)
    assert qs["directory"] == [_DIR_ENCODED]


def test_observe_single_get_no_retry():
    opener = FakeOpener(script=_list_script([_SID_NEW]))
    _observe(_reader(opener=opener))
    assert opener.call_count == 1               # 单轮恰好 1 个 GET


# =====================================================================
# 10-22：ledger + authoritative baseline 校验（0 GET）
# =====================================================================


def test_pre_snapshot_malformed_json_invalid_ledger(db_env):
    _insert_op(db_env, op_id="op-cs-mj", key="k-cs-mj", baseline="{bad json")
    opener = FakeOpener(script=[])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-mj")
    assert r.outcome is CreateReconciliationOutcome.INVALID_LEDGER
    assert opener.calls == []


def test_evidence_malformed_json_invalid_ledger(db_env):
    _insert_op(db_env, op_id="op-cs-me", key="k-cs-me", evidence="{not json")
    opener = FakeOpener(script=[])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-me")
    assert r.outcome is CreateReconciliationOutcome.INVALID_LEDGER
    assert opener.calls == []


def test_baseline_missing_directory_invalid_ledger(db_env):
    b = _baseline()
    del b["directory"]
    _insert_op(db_env, op_id="op-cs-md", key="k-cs-md", baseline=b)
    opener = FakeOpener(script=[])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-md")
    assert r.outcome is CreateReconciliationOutcome.INVALID_LEDGER
    assert opener.calls == []


def test_baseline_wrong_binding_mode_invalid_ledger(db_env):
    _insert_op(db_env, op_id="op-cs-wb", key="k-cs-wb",
               baseline=_baseline(binding_mode="FIXED_SESSION"))
    opener = FakeOpener(script=[])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-wb")
    assert r.outcome is CreateReconciliationOutcome.INVALID_LEDGER
    assert opener.calls == []


def test_baseline_missing_binding_revision_invalid_ledger(db_env):
    b = _baseline()
    del b["binding_revision"]
    _insert_op(db_env, op_id="op-cs-nbr", key="k-cs-nbr", baseline=b)
    opener = FakeOpener(script=[])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-nbr")
    assert r.outcome is CreateReconciliationOutcome.INVALID_LEDGER
    assert opener.calls == []


def test_baseline_bad_binding_revision_type_invalid_ledger(db_env):
    _insert_op(db_env, op_id="op-cs-bbr", key="k-cs-bbr",
               baseline=_baseline(binding_revision="1"))
    opener = FakeOpener(script=[])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-bbr")
    assert r.outcome is CreateReconciliationOutcome.INVALID_LEDGER
    assert opener.calls == []


def test_baseline_missing_create_title_invalid_ledger(db_env):
    b = _baseline()
    del b["create_title"]
    _insert_op(db_env, op_id="op-cs-mt", key="k-cs-mt", baseline=b)
    opener = FakeOpener(script=[])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-mt")
    assert r.outcome is CreateReconciliationOutcome.INVALID_LEDGER
    assert opener.calls == []


def test_baseline_duplicate_before_ids_invalid_ledger(db_env):
    _insert_op(db_env, op_id="op-cs-db", key="k-cs-db",
               baseline=_baseline(before=[_SID_A, _SID_A]))
    opener = FakeOpener(script=[])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-db")
    assert r.outcome is CreateReconciliationOutcome.INVALID_LEDGER
    assert opener.calls == []


def test_baseline_count_mismatch_invalid_ledger(db_env):
    b = _baseline(before=[_SID_A])
    b["session_count_before"] = 2
    _insert_op(db_env, op_id="op-cs-cm", key="k-cs-cm", baseline=b)
    opener = FakeOpener(script=[])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-cm")
    assert r.outcome is CreateReconciliationOutcome.INVALID_LEDGER
    assert opener.calls == []


def test_baseline_before_not_list_invalid_ledger(db_env):
    b = _baseline()
    b["session_ids_before"] = (_SID_A,)
    _insert_op(db_env, op_id="op-cs-nl", key="k-cs-nl", baseline=b)
    opener = FakeOpener(script=[])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-nl")
    assert r.outcome is CreateReconciliationOutcome.INVALID_LEDGER
    assert opener.calls == []


def test_ledger_session_id_not_null_invalid_ledger(db_env):
    _insert_op(db_env, op_id="op-cs-sq", key="k-cs-sq", session_id=_SID_A)
    opener = FakeOpener(script=[])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-sq")
    assert r.outcome is CreateReconciliationOutcome.INVALID_LEDGER
    assert opener.calls == []


def test_ledger_remote_user_id_not_null_invalid_ledger(db_env):
    _insert_op(db_env, op_id="op-cs-rq", key="k-cs-rq", remote="msg_123")
    opener = FakeOpener(script=[])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-rq")
    assert r.outcome is CreateReconciliationOutcome.INVALID_LEDGER
    assert opener.calls == []


def test_ledger_endpoint_blank_invalid_ledger(db_env):
    _insert_op(db_env, op_id="op-cs-eb", key="k-cs-eb", endpoint="   ")
    opener = FakeOpener(script=[])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-eb")
    assert r.outcome is CreateReconciliationOutcome.INVALID_LEDGER
    assert opener.calls == []


# =====================================================================
# 23-31：after read 失败 → READ_FAILED 保持 UNKNOWN
# =====================================================================


def test_after_malformed_json_read_failed(db_env):
    _insert_op(db_env, op_id="op-cs-aj", key="k-cs-aj")
    opener = FakeOpener(script=[(200, "{bad json")])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-aj")
    assert r.outcome is CreateReconciliationOutcome.READ_FAILED
    assert _read_op(db_env, "op-cs-aj")[2] == "UNKNOWN"
    assert "malformed_json" in r.detail


def test_after_not_list_read_failed(db_env):
    _insert_op(db_env, op_id="op-cs-nl2", key="k-cs-nl2")
    opener = FakeOpener(script=[(200, json.dumps({"id": _SID_NEW}))])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-nl2")
    assert r.outcome is CreateReconciliationOutcome.READ_FAILED
    assert "not_list" in r.detail


def test_after_duplicate_ids_read_failed(db_env):
    _insert_op(db_env, op_id="op-cs-du", key="k-cs-du")
    opener = FakeOpener(script=_list_script([_SID_NEW, _SID_NEW]))
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-du")
    assert r.outcome is CreateReconciliationOutcome.READ_FAILED
    assert "duplicate_ids" in r.detail
    assert _read_op(db_env, "op-cs-du")[2] == "UNKNOWN"


def test_after_item_missing_id_read_failed(db_env):
    _insert_op(db_env, op_id="op-cs-mid", key="k-cs-mid")
    opener = FakeOpener(script=[(200, json.dumps([{"title": "x"}]))])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-mid")
    assert r.outcome is CreateReconciliationOutcome.READ_FAILED
    assert "malformed_session_item" in r.detail


def test_after_item_not_object_read_failed(db_env):
    _insert_op(db_env, op_id="op-cs-obj", key="k-cs-obj")
    opener = FakeOpener(script=[(200, json.dumps([_SID_A]))])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-obj")
    assert r.outcome is CreateReconciliationOutcome.READ_FAILED
    assert "malformed_session_item" in r.detail


def test_after_blank_id_read_failed(db_env):
    _insert_op(db_env, op_id="op-cs-bid", key="k-cs-bid")
    opener = FakeOpener(script=[(200, json.dumps([{"id": "  "}]))])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-bid")
    assert r.outcome is CreateReconciliationOutcome.READ_FAILED
    assert "malformed_session_item" in r.detail


def test_get_timeout_read_failed_remains_unknown(db_env):
    _insert_op(db_env, op_id="op-cs-to", key="k-cs-to")
    opener = FakeOpener(script=[socket.timeout("t")])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-to")
    assert r.outcome is CreateReconciliationOutcome.READ_FAILED
    assert _read_op(db_env, "op-cs-to")[2] == "UNKNOWN"
    assert opener.call_count == 1                # 无 retry
    assert "TIMEOUT" in r.detail


def test_get_http_error_read_failed(db_env):
    _insert_op(db_env, op_id="op-cs-ht", key="k-cs-ht")
    opener = FakeOpener(script=[_http_error(403, '{"error":"forbidden"}')])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-ht")
    assert r.outcome is CreateReconciliationOutcome.READ_FAILED
    assert _read_op(db_env, "op-cs-ht")[2] == "UNKNOWN"
    assert opener.call_count == 1


def test_get_connection_error_read_failed(db_env):
    _insert_op(db_env, op_id="op-cs-co", key="k-cs-co")
    opener = FakeOpener(script=[socket.error(99, "refused")])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-co")
    assert r.outcome is CreateReconciliationOutcome.READ_FAILED
    assert _read_op(db_env, "op-cs-co")[2] == "UNKNOWN"
    assert opener.call_count == 1


# =====================================================================
# 32-41：核心差集
# =====================================================================


def test_before_empty_after_one_valid_accept(db_env):
    _insert_op(db_env, op_id="op-cs-17", key="k-cs-17", baseline=_baseline(before=[]))
    opener = FakeOpener(script=_list_script([_SID_NEW]))
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-17")
    assert r.outcome is CreateReconciliationOutcome.ACCEPTED_CONFIRMED
    assert r.created_session_id == _SID_NEW
    assert r.new_session_count == 1
    assert r.missing_old_session_count == 0


def test_before_one_same_one_new_accept(db_env):
    _insert_op(db_env, op_id="op-cs-18", key="k-cs-18",
               baseline=_baseline(before=[_SID_A]))
    opener = FakeOpener(script=_list_script([_SID_A, _SID_NEW]))
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-18")
    assert r.outcome is CreateReconciliationOutcome.ACCEPTED_CONFIRMED
    assert r.created_session_id == _SID_NEW


def test_before_multiple_exactly_one_new_accept(db_env):
    _insert_op(db_env, op_id="op-cs-19", key="k-cs-19",
               baseline=_baseline(before=[_SID_A, _SID_B]))
    opener = FakeOpener(script=_list_script([_SID_A, _SID_B, _SID_NEW]))
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-19")
    assert r.outcome is CreateReconciliationOutcome.ACCEPTED_CONFIRMED
    assert r.created_session_id == _SID_NEW
    assert r.missing_old_session_count == 0


def test_old_order_changed_accept(db_env):
    _insert_op(db_env, op_id="op-cs-20", key="k-cs-20",
               baseline=_baseline(before=[_SID_A, _SID_B]))
    opener = FakeOpener(script=_list_script([_SID_B, _SID_NEW, _SID_A]))
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-20")
    assert r.outcome is CreateReconciliationOutcome.ACCEPTED_CONFIRMED
    assert r.created_session_id == _SID_NEW


def test_zero_new_still_unknown(db_env):
    _insert_op(db_env, op_id="op-cs-21", key="k-cs-21",
               baseline=_baseline(before=[_SID_A]))
    opener = FakeOpener(script=_list_script([_SID_A]))
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-21")
    assert r.outcome is CreateReconciliationOutcome.STILL_UNKNOWN
    assert r.new_session_count == 0
    assert r.missing_old_session_count == 0
    assert _read_op(db_env, "op-cs-21")[2] == "UNKNOWN"


def test_two_new_still_unknown(db_env):
    _insert_op(db_env, op_id="op-cs-22", key="k-cs-22",
               baseline=_baseline(before=[_SID_A]))
    opener = FakeOpener(script=_list_script([_SID_A, _SID_NEW, _SID_B]))
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-22")
    assert r.outcome is CreateReconciliationOutcome.STILL_UNKNOWN
    assert r.new_session_count == 2


def test_one_new_invalid_id_still_unknown(db_env):
    _insert_op(db_env, op_id="op-cs-23", key="k-cs-23",
               baseline=_baseline(before=[_SID_A]))
    opener = FakeOpener(script=_list_script([_SID_A, "other-zone-9"]))
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-23")
    assert r.outcome is CreateReconciliationOutcome.STILL_UNKNOWN
    assert r.new_session_count == 1
    assert r.created_session_id is None


def test_old_missing_one_new_still_unknown(db_env):
    _insert_op(db_env, op_id="op-cs-24", key="k-cs-24",
               baseline=_baseline(before=[_SID_A, _SID_B]))
    opener = FakeOpener(script=_list_script([_SID_A, _SID_NEW]))
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-24")
    assert r.outcome is CreateReconciliationOutcome.STILL_UNKNOWN
    assert r.new_session_count == 1
    assert r.missing_old_session_count == 1


def test_old_missing_zero_new_still_unknown(db_env):
    _insert_op(db_env, op_id="op-cs-25", key="k-cs-25",
               baseline=_baseline(before=[_SID_A, _SID_B]))
    opener = FakeOpener(script=_list_script([_SID_A]))
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-25")
    assert r.outcome is CreateReconciliationOutcome.STILL_UNKNOWN
    assert r.new_session_count == 0
    assert r.missing_old_session_count == 1


def test_one_new_invalid_id_with_empty_before_still_unknown(db_env):
    _insert_op(db_env, op_id="op-cs-ei", key="k-cs-ei", baseline=_baseline(before=[]))
    opener = FakeOpener(script=_list_script(["project-x"]))
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-ei")
    assert r.outcome is CreateReconciliationOutcome.STILL_UNKNOWN
    assert r.created_session_id is None


# =====================================================================
# 42-45：不使用 title / newest / list-last / slug 启发式
# =====================================================================


def test_title_match_does_not_matter(db_env):
    _insert_op(db_env, op_id="op-cs-26", key="k-cs-26",
               baseline=_baseline(before=[]))
    opener = FakeOpener(script=[(
        200,
        json.dumps([{"id": _SID_NEW, "title": "A different title completely"}]),
    )])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-26")
    assert r.outcome is CreateReconciliationOutcome.ACCEPTED_CONFIRMED


def test_newest_heuristic_absent(db_env):
    _insert_op(db_env, op_id="op-cs-27", key="k-cs-27",
               baseline=_baseline(before=[_SID_A]))
    opener = FakeOpener(script=_list_script([_SID_NEW, _SID_A]))
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-27")
    assert r.outcome is CreateReconciliationOutcome.ACCEPTED_CONFIRMED   # 新 id 虽在最前
    assert r.created_session_id == _SID_NEW


def test_list_last_heuristic_absent(db_env):
    _insert_op(db_env, op_id="op-cs-lst", key="k-cs-lst",
               baseline=_baseline(before=[_SID_A, _SID_B]))
    opener = FakeOpener(script=_list_script([_SID_NEW, _SID_A, _SID_B]))
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-lst")
    assert r.outcome is CreateReconciliationOutcome.ACCEPTED_CONFIRMED


def test_slug_ignored(db_env):
    _insert_op(db_env, op_id="op-cs-28", key="k-cs-28",
               baseline=_baseline(before=[]))
    opener = FakeOpener(script=[(
        200,
        json.dumps([{"id": _SID_NEW, "slug": "zz-project-x", "mtime": "2026-10-02"}]),
    )])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-28")
    assert r.outcome is CreateReconciliationOutcome.ACCEPTED_CONFIRMED


# =====================================================================
# 46-56：coordinator 落账与 evidence 合并（identity 列/其他表不变）
# =====================================================================


def test_unknown_clean_delta_accepted_confirmed(db_env):
    _insert_op(db_env, op_id="op-cs-29", key="k-cs-29",
               baseline=_baseline(before=[]))
    opener = FakeOpener(script=_list_script([_SID_NEW]))
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-29")
    assert r.outcome is CreateReconciliationOutcome.ACCEPTED_CONFIRMED
    assert r.state == "ACCEPTED"
    assert _read_op(db_env, "op-cs-29")[2] == "ACCEPTED"


def test_created_session_id_top_level_evidence(db_env):
    _insert_op(db_env, op_id="op-cs-30", key="k-cs-30")
    opener = FakeOpener(script=_list_script([_SID_NEW]))
    _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-30")
    evidence = json.loads(_read_op(db_env, "op-cs-30")[5])
    assert evidence["created_session_id"] == _SID_NEW


def test_reconciliation_evidence_added(db_env):
    _insert_op(db_env, op_id="op-cs-31", key="k-cs-31",
               baseline=_baseline(before=[_SID_A, _SID_B]))
    opener = FakeOpener(script=_list_script([_SID_A, _SID_B, _SID_NEW]))
    _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-31")
    evidence = json.loads(_read_op(db_env, "op-cs-31")[5])
    rc = evidence["unknown_create_reconciliation"]
    assert rc["decision"] == "accepted_confirmed"
    assert rc["source"] == "GET_ONLY_SESSION_DIFF"
    assert rc["session_count_before"] == 2
    assert rc["session_count_after"] == 3
    assert rc["new_session_count"] == 1
    assert rc["missing_old_session_count"] == 0


def test_original_evidence_preserved(db_env):
    _insert_op(db_env, op_id="op-cs-32", key="k-cs-32",
               evidence='{"classification": "create_unknown", "http_status": 500,'
                        ' "transport_error": "TIMEOUT", "decision": "restart_recovery"}')
    opener = FakeOpener(script=_list_script([_SID_NEW]))
    _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-32")
    evidence = json.loads(_read_op(db_env, "op-cs-32")[5])
    assert evidence["classification"] == "create_unknown"     # 原 UNKNOWN 证据不擦除
    assert evidence["http_status"] == 500
    assert evidence["transport_error"] == "TIMEOUT"
    assert evidence["decision"] == "restart_recovery"
    assert evidence["unknown_create_reconciliation"]["decision"] == "accepted_confirmed"


def test_session_id_column_stays_null(db_env):
    _insert_op(db_env, op_id="op-cs-33", key="k-cs-33")
    opener = FakeOpener(script=_list_script([_SID_NEW]))
    _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-33")
    assert _read_op(db_env, "op-cs-33")[3] is None            # session_id 仍 NULL


def test_remote_user_id_stays_null(db_env):
    _insert_op(db_env, op_id="op-cs-34", key="k-cs-34")
    opener = FakeOpener(script=_list_script([_SID_NEW]))
    _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-34")
    assert _read_op(db_env, "op-cs-34")[4] is None            # remote_user_id 恒 NULL


def test_pre_snapshot_unchanged(db_env):
    b = _baseline(before=[_SID_A])
    _insert_op(db_env, op_id="op-cs-35", key="k-cs-35", baseline=b)
    opener = FakeOpener(script=_list_script([_SID_A, _SID_NEW]))
    _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-35")
    assert json.loads(_read_op(db_env, "op-cs-35")[6]) == b   # pre_snapshot 原样


def test_no_task_mutation(db_env):
    _insert_op(db_env, op_id="op-cs-36", key="k-cs-36")
    opener = FakeOpener(script=_list_script([_SID_NEW]))
    _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-36")
    assert _count(db_env, "tasks") == 0


def test_no_attempt_mutation(db_env):
    _insert_op(db_env, op_id="op-cs-37", key="k-cs-37")
    opener = FakeOpener(script=_list_script([_SID_NEW]))
    _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-37")
    assert _count(db_env, "attempts") == 0


def test_no_lease_mutation(db_env):
    _insert_op(db_env, op_id="op-cs-38", key="k-cs-38")
    opener = FakeOpener(script=_list_script([_SID_NEW]))
    _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-38")
    assert _count(db_env, "project_leases") == 0


def test_no_result_or_binding_mutation(db_env):
    _insert_op(db_env, op_id="op-cs-39", key="k-cs-39")
    opener = FakeOpener(script=_list_script([_SID_NEW]))
    _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-39")
    assert _count(db_env, "results") == 0
    assert _count(db_env, "outbox") == 0
    assert _count(db_env, "project_bindings") == 0


# =====================================================================
# 57-62：非 UNKNOWN / 非 CREATE_SESSION / 不存在 → 0 GET
# =====================================================================


@pytest.mark.parametrize(("state", "outcome"), [
    ("ACCEPTED", CreateReconciliationOutcome.ALREADY_ACCEPTED),
    ("REJECTED", CreateReconciliationOutcome.NOT_UNKNOWN),
    ("PREPARED", CreateReconciliationOutcome.NOT_UNKNOWN),
    ("SENDING", CreateReconciliationOutcome.NOT_UNKNOWN),
])
def test_non_unknown_states_zero_get(db_env, state, outcome):
    _insert_op(db_env, op_id="op-cs-nzu", key="k-cs-nzu", state=state)
    opener = FakeOpener(script=[])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-nzu")
    assert r.outcome is outcome
    assert opener.calls == []                    # 0 GET


def test_send_kind_unsupported_zero_get(db_env):
    _insert_op(db_env, op_id="op-cs-k1", key="k-cs-k1", kind="INITIAL_SEND")
    opener = FakeOpener(script=[])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-k1")
    assert r.outcome is CreateReconciliationOutcome.UNSUPPORTED_KIND
    assert opener.calls == []


def test_permission_kind_unsupported_zero_get(db_env):
    _insert_op(db_env, op_id="op-cs-k2", key="k-cs-k2", kind="SET_PERMISSION")
    opener = FakeOpener(script=[])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-k2")
    assert r.outcome is CreateReconciliationOutcome.UNSUPPORTED_KIND
    assert opener.calls == []


def test_not_found_outcome(db_env):
    opener = FakeOpener(script=[])
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-missing")
    assert r.outcome is CreateReconciliationOutcome.NOT_FOUND
    assert opener.calls == []


# =====================================================================
# 63-70：事务边界 / never blind recreate / repeated reconcile / 并发
# =====================================================================


def test_get_outside_db_transaction(db_env):
    _insert_op(db_env, op_id="op-cs-53", key="k-cs-53")
    txflags: list[bool] = []

    class TxProbeOpener(FakeOpener):
        def __call__(self, req):
            txflags.append(db_env.in_transaction)
            return super().__call__(req)

    opener = TxProbeOpener(script=_list_script([_SID_NEW]))
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-53")
    assert r.outcome is CreateReconciliationOutcome.ACCEPTED_CONFIRMED
    assert txflags and all(flag is False for flag in txflags)   # GET 在任何事务外


def test_finalize_has_no_http(db_env):
    _insert_op(db_env, op_id="op-cs-54", key="k-cs-54")
    opener = FakeOpener(script=_list_script([_SID_NEW]))
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-54")
    assert r.outcome is CreateReconciliationOutcome.ACCEPTED_CONFIRMED
    assert opener.call_count == 1                # 1 GET + 0 额外 HTTP
    assert [c["method"] for c in opener.calls] == ["GET"]


def test_no_auto_rejected_path(db_env):
    _insert_op(db_env, op_id="op-cs-49", key="k-cs-49",
               baseline=_baseline(before=[_SID_A]))
    opener = FakeOpener(script=_list_script([_SID_A]))
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-49")
    assert r.outcome is CreateReconciliationOutcome.STILL_UNKNOWN
    assert _read_op(db_env, "op-cs-49")[2] == "UNKNOWN"   # 绝不自动 REJECTED


def test_never_blind_recreate(db_env):
    source = _module_code_source(OpenChamberUnknownCreateReconciler)
    for banned in ("create_once", "send_once", "prompt_async",
                   "OpenChamberCreateSessionTransport", "capture_pre_create_snapshot",
                   "method=\"POST\""):
        assert banned not in source
    for state in ("UNKNOWN",):
        _insert_op(db_env, op_id="op-cs-50", key="k-cs-50",
                   baseline=_baseline(before=[]))
    opener = FakeOpener(script=_list_script([_SID_NEW]))
    _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-50")
    _assert_no_write(opener)                     # reconcile 永远只发 GET


def test_zero_post_capability_runtime(db_env):
    _insert_op(db_env, op_id="op-cs-51", key="k-cs-51")
    opener = FakeOpener(script=_list_script([_SID_NEW]))
    _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-51")
    assert [c["method"] for c in opener.calls] == ["GET"]
    assert not any(c["method"] == "POST" for c in opener.calls)


def test_repeated_reconcile_only_repeats_get(db_env):
    _insert_op(db_env, op_id="op-cs-52", key="k-cs-52",
               baseline=_baseline(before=[_SID_A]))
    opener = FakeOpener(script=_list_script([_SID_A]),
                        default=(200, json.dumps([{"id": _SID_A}])))
    recon = _reconciler(db_env, _reader(opener=opener))
    r1 = recon.reconcile("op-cs-52")
    assert r1.outcome is CreateReconciliationOutcome.STILL_UNKNOWN
    r2 = recon.reconcile("op-cs-52")
    assert r2.outcome is CreateReconciliationOutcome.STILL_UNKNOWN
    assert opener.call_count == 2                # 只重复 GET，永不二次 create
    assert all(c["method"] == "GET" for c in opener.calls)


def test_repeated_clean_reconcile_then_already_accepted(db_env):
    _insert_op(db_env, op_id="op-cs-52b", key="k-cs-52b", baseline=_baseline(before=[]))
    opener = FakeOpener(script=_list_script([_SID_NEW]))
    recon = _reconciler(db_env, _reader(opener=opener))
    r1 = recon.reconcile("op-cs-52b")
    assert r1.outcome is CreateReconciliationOutcome.ACCEPTED_CONFIRMED
    r2 = recon.reconcile("op-cs-52b")
    assert r2.outcome is CreateReconciliationOutcome.ALREADY_ACCEPTED
    assert opener.call_count == 1                # 二次对账 0 GET（已终态）


def test_concurrent_unknown_to_accepted_idempotent(db_env):
    _insert_op(db_env, op_id="op-cs-55", key="k-cs-55")
    ops = OperationStore(db_env)

    def other_worker_finalizes() -> None:
        with db_env.transaction():
            ops.finalize_in(
                db_env.connection, proposal=SimpleNamespace(operation_key="k-cs-55"),
                operation_id="op-cs-55", target_state="ACCEPTED",
                evidence_json='{"concurrent": true}', finalized_at=_T0, now=_T0,
            )

    class ConcurrentOpener(FakeOpener):
        def __call__(self, req):
            other_worker_finalizes()
            return super().__call__(req)

    opener = ConcurrentOpener(script=_list_script([_SID_NEW]))
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-55")
    assert r.outcome is CreateReconciliationOutcome.ALREADY_ACCEPTED   # 幂等
    assert _read_op(db_env, "op-cs-55")[2] == "ACCEPTED"
    evidence = json.loads(_read_op(db_env, "op-cs-55")[5])
    assert evidence.get("concurrent") is True      # 另一 worker 的 evidence 未被覆盖


def test_concurrent_unknown_to_rejected_not_overwritten(db_env):
    _insert_op(db_env, op_id="op-cs-56", key="k-cs-56")
    ops = OperationStore(db_env)

    def other_worker_finalizes() -> None:
        with db_env.transaction():
            ops.finalize_in(
                db_env.connection, proposal=SimpleNamespace(operation_key="k-cs-56"),
                operation_id="op-cs-56", target_state="REJECTED",
                evidence_json='{"concurrent": true}', finalized_at=_T0, now=_T0,
            )

    class ConcurrentOpener(FakeOpener):
        def __call__(self, req):
            other_worker_finalizes()
            return super().__call__(req)

    opener = ConcurrentOpener(script=_list_script([_SID_NEW]))
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-56")
    assert r.outcome is CreateReconciliationOutcome.CONCURRENT_CHANGE
    assert _read_op(db_env, "op-cs-56")[2] == "REJECTED"   # 不反向转换回 ACCEPTED
    evidence = json.loads(_read_op(db_env, "op-cs-56")[5])
    assert evidence.get("concurrent") is True               # evidence 未被覆盖


# =====================================================================
# 补充：bidirectional endpoint / 输入侧 reader 复用依赖 sanity
# =====================================================================


def test_reader_input_error_type_distinct():
    # reader 的输入错误类型独立于 T22-04 的 reconciliation reader
    assert OpenChamberCreateReconciliationInputError is not OpenChamberReconciliationInputError
    assert issubclass(OpenChamberCreateReconciliationInputError, Exception)


def test_reader_observation_is_frozen():
    obs = CreateReconciliationObservation(session_ids_after=(_SID_A,), session_count_after=1)
    assert obs.session_ids_after == (_SID_A,)
    with pytest.raises(AttributeError):
        obs.session_count_after = 2             # 不可变 DTO
    assert obs.read_error is None


def test_delta_computed_by_coordinator(db_env):
    # §17 建议 DTO：协调器暴露 CreateSessionDelta（不可变）
    _insert_op(db_env, op_id="op-cs-dl", key="k-cs-dl",
               baseline=_baseline(before=[_SID_A, _SID_B]))
    opener = FakeOpener(script=_list_script([_SID_B, _SID_NEW, _SID_A]))
    r = _reconciler(db_env, _reader(opener=opener)).reconcile("op-cs-dl")
    assert r.created_session_id == _SID_NEW
    assert CreateSessionDelta(new_session_ids=(_SID_NEW,)).new_session_ids == (_SID_NEW,)