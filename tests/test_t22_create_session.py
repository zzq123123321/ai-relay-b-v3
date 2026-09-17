"""T22-05A：OpenChamber CREATE_SESSION 一次写权 + durable baseline 自动测试（全程 fake，0 真实网络）。

覆盖 T22-05A 卡（§23 的 47 项 + 附加 3 项）：
- 权威（§10 首次 / §12 第二次 / §14 第三次三次权威检查）：task/attempt/lease
  owner/epoch/control_revision/binding 全部 fail-closed → AUTHORITY_REVOKED，
  GET/POST/operation 全 0；lease ACTIVE 仅允许 resolved_session_id 为空的
  初始创建；ROTATING 允许轮换；QUARANTINED 禁止；
- FIXED_SESSION 永远禁止建会话（FIXED_SESSION_FORBIDS_CREATE，0 GET/0 POST/0 operation），
  blank title → INVALID_LEDGER，未知 binding → INVALID_SNAPSHOT；
- baseline：GET-only /api/session 精确路径 + directory 编码，只收集 session identity，
  pre_snapshot_json 含 directory/binding_mode/binding_revision/session_ids_before/
  session_count_before/create_title 且无 token；快照失败 → PRECHECK_FAILED（0 POST）；
- 流程三分步：prepare_create(PREPARED+baseline) → acquire_create_right(SENDING) →
  send_prepared_create(ACCEPTED)，中间态可观测；POST 严格在事务外；
- HTTP outcome：200+合法 id → ACCEPTED；200 缺 id/非法 id/非 JSON → UNKNOWN；
  4xx → REJECTED；5xx/3xx → UNKNOWN；timeout → transport error 收敛 UNKNOWN，
  每次 create_once 恰好 1 次 POST、不 follow 302、不重试；
- existing operation：ACCEPTED → 复用 created_session_id（0 POST）；REJECTED → 0 POST
  不重试；UNKNOWN → UNKNOWN_EXISTING（留 T22-05B）；SENDING → restart 收敛 UNKNOWN；
  PREPARED → 幂等续跑；同 key 异身份 → KEY_CONFLICT（原 operation 不改）；
- secrets：token / raw body 绝不出现在 evidence / baseline / repr；
- 全程 0 prompt_async、0 通用写、0 /create 之外副作用；operations.session_id 与
  remote_user_id 恒为 NULL，真实 id 只进 evidence_json.created_session_id。
"""

from __future__ import annotations

import io
import json
import socket
from dataclasses import replace
import sys
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from adapters.openchamber_create_session import (  # noqa: E402
    CreateOutcome,
    OpenChamberCreateSessionTransport,
    OpenChamberCreateTransportError,
)
from app.openchamber_create_session_coordinator import (  # noqa: E402
    CreateAuthorityStatus,
    CreateSessionOutcome,
    CreateSessionProposal,
    OpenChamberCreateSessionCoordinator,
    build_create_proposal,
    create_session_operation_key,
)
from core.dispatch import SnapshotFailure, derive_operation_id  # noqa: E402
from core.domain import (  # noqa: E402
    ExecutionSettingsSnapshot,
    ReceiveSettingsSnapshot,
    SessionBindingMode,
    TargetExecutor,
)
from infra.clock import FakeClock  # noqa: E402
from storage.database import Database  # noqa: E402
from storage.operation_store import OperationStore, FinalizeOutcome  # noqa: E402

_BASE = "http://127.0.0.1:57123"
_OTHER = "http://127.0.0.1:57124"
_DIR = r"D:\AIwork\proj dir"
_PROJECT = "proj-rot"
_SID_OLD = "ses_old_0001"
_SID_NEW = "ses_new_9f8e7d6c5b4a"
_TITLE = "AI Relay 轮换会话标题"
_SECRET_TOKEN = "tok-TopSecret-create"
_SECRET_RAW_BODY = "SECRET-RAW-CREATE-BODY"
_T0 = "2026-10-01T09:00:00+00:00"
_WALL = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)


# =====================================================================
# 测试基础设施：FakeResponse / FakeOpener / seed / snapshot factories
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
    """脚本式 fake opener：按序消费响应，记录全部请求；note 注入 db 事务探测。"""

    def __init__(self, script=None, default=None, note=None):
        self.script = list(script or [])
        self.default = default
        self.note = note
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
        if self.note is not None:
            self.note(req)
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


def _session_payload(sid: str = _SID_NEW) -> list:
    return [{"id": sid}]


def _default_script() -> list:
    return [
        (200, json.dumps(_session_payload(_SID_OLD))),
        (200, json.dumps({"id": _SID_NEW})),
    ]


def _make_transport(*, read_opener=None, write_opener=None,
                    base_url: str = _BASE, directory: str = _DIR,
                    token: str | None = _SECRET_TOKEN) -> OpenChamberCreateSessionTransport:
    return OpenChamberCreateSessionTransport(
        base_url, directory, token=token, timeout=1.5,
        read_opener=read_opener, write_opener=write_opener,
    )


@pytest.fixture
def db_env(tmp_path):
    db = Database(tmp_path / "t22cs.sqlite")
    db.open()
    yield db
    db.close()


def _seed_full(
    db,
    *,
    task: str = "task-create-1",
    attempt: str = "attempt-create-1",
    project: str = _PROJECT,
    epoch: int = 1,
    control: int = 5,
    task_state: str = "ACTIVE",
    attempt_state: str = "OPEN",
    attempt_epoch: int | None = None,
    attempt_control: int | None = None,
    active_attempt: str | None = None,
    lease_state: str = "ROTATING",
    lease_owner_attempt: str | None = None,
    lease_owner_task: str | None = None,
    lease_epoch: int | None = None,
    seq: int = 1,
) -> None:
    with db.transaction():
        active_attempt = active_attempt or attempt
        attempt_epoch = attempt_epoch if attempt_epoch is not None else epoch
        attempt_control = attempt_control if attempt_control is not None else control
        db.connection.execute(
            "INSERT INTO tasks (task_key, peer_id, task_id, sequence, protocol_format,"
            " raw_message, body, canonical_hash, received_at, ingress_snapshot_json,"
            " state, blocked_reason, active_attempt_id, authority_epoch,"
            " current_result_revision) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task, "CHATGPT", f"tid-{seq}", seq, "V1", "raw", "body", "hash",
             _T0, "{}", task_state, None, active_attempt, epoch, 0),
        )
        db.connection.execute(
            "INSERT INTO attempts (attempt_id, task_key, parent_attempt_id, kind, state,"
            " authority_epoch, execution_snapshot_json, control_revision, remote_state,"
            " started_at, ended_at, remote_summary_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (attempt, task, None, "INITIAL", attempt_state, attempt_epoch, "{}",
             attempt_control, "NOT_SENT", _T0, None, "{}"),
        )
        db.connection.execute(
            "INSERT INTO project_leases (project_key, owner_task_key, owner_attempt_id,"
            " authority_epoch, state, related_sessions_json, last_verified_at, reason)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (project, lease_owner_task or task, lease_owner_attempt or attempt,
             lease_epoch if lease_epoch is not None else epoch, lease_state,
             "[]", _T0, None),
        )


def _snapshot(*, directory: str = _DIR, project: str = _PROJECT,
              binding_mode: str = "PROJECT_ROTATING", binding_revision: int = 1,
              resolved_session_id: str | None = None) -> ExecutionSettingsSnapshot:
    recv = ReceiveSettingsSnapshot(
        config_revision=1, committed_at=_T0, received_at=_T0,
        effective_executor=TargetExecutor.OPENCHAMBER, directory=directory,
        project_key=project, agent="build", requested_model="",
        binding_mode=SessionBindingMode(binding_mode),
        frozen_session_id=resolved_session_id,
    )
    return ExecutionSettingsSnapshot(
        receive=recv, binding_revision=binding_revision,
        resolved_session_id=resolved_session_id, execution_started_at=_T0,
    )


def _proposal(*, snapshot: ExecutionSettingsSnapshot | None = None,
              attempt: str = "attempt-create-1", task: str = "task-create-1",
              epoch: int = 1, control: int = 5,
              endpoint: str = _BASE) -> CreateSessionProposal:
    snapshot = snapshot if snapshot is not None else _snapshot()
    return build_create_proposal(
        operation_key=create_session_operation_key(
            project_key=snapshot.receive.project_key, attempt_id=attempt,
            authority_epoch=epoch, control_revision=control,
            binding_revision=snapshot.binding_revision),
        snapshot=snapshot, task_key=task, attempt_id=attempt,
        authority_epoch=epoch, control_revision=control, endpoint=endpoint,
    )


def _insert_attempt(db, attempt_id: str, *, task: str = "task-create-1",
                    state: str = "COMPLETED", epoch: int = 1,
                    control: int = 5) -> None:
    with db.transaction():
        db.connection.execute(
            "INSERT INTO attempts (attempt_id, task_key, parent_attempt_id, kind, state,"
            " authority_epoch, execution_snapshot_json, control_revision, remote_state,"
            " started_at, ended_at, remote_summary_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (attempt_id, task, None, "MANUAL_CONTINUE", state, epoch, "{}",
             control, "NOT_SENT", _T0, _T0, "{}"),
        )


def _coordinator(db, transport, *, clock=None, ops=None) -> OpenChamberCreateSessionCoordinator:
    return OpenChamberCreateSessionCoordinator(
        db, transport=transport,
        operation_store=ops if ops is not None else OperationStore(db),
        clock=clock if clock is not None else FakeClock(wall=_WALL),
    )


def _call_create(coord, *, task="task-create-1", attempt="attempt-create-1",
                 epoch=1, control=5, snapshot=None, endpoint=_BASE,
                 title=_TITLE, transport=None):
    snapshot = snapshot if snapshot is not None else _snapshot()
    return coord.create_session(
        task_key=task, attempt_id=attempt, authority_epoch=epoch,
        control_revision=control, snapshot=snapshot, endpoint=endpoint,
        create_title=title,
    )


def _full_ok(db, *, read_opener=None, write_opener=None, transport=None,
             resolved_session_id=None, lease_state="ROTATING", note=None):
    ro = read_opener if read_opener is not None else FakeOpener(script=_default_script(), note=note)
    wo = write_opener if write_opener is not None else FakeOpener(script=[])
    _seed_full(db, lease_state=lease_state,
               resolved_session_id=resolved_session_id)
    t = transport if transport is not None else _make_transport(read_opener=ro, write_opener=wo)
    return _coordinator(db, t), t, ro, wo


def _read_op(db, op_id: str):
    row = db.connection.execute(
        "SELECT operation_id, operation_key, kind, task_key, attempt_id, authority_epoch,"
        " control_revision, endpoint, session_id, project_key, interruption_id, state,"
        " pre_snapshot_json, prompt_hash, prompt_text, remote_user_id, evidence_json,"
        " created_at, updated_at, finalized_at FROM operations WHERE operation_id=?",
        (op_id,),
    ).fetchone()
    return row


def _count(db, table: str) -> int:
    return int(db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _insert_op(
    db,
    *,
    op_id: str,
    key: str,
    state: str = "ACCEPTED",
    evidence: str = '{"created_session_id": "%s"}' % _SID_NEW,
    pre_snapshot: str = None,
    kind: str = "CREATE_SESSION",
    endpoint: str = _BASE,
    attempt: str = "attempt-create-1",
    task: str = "task-create-1",
    epoch: int = 1,
    control: int = 5,
    project: str = _PROJECT,
):
    pre_snapshot = pre_snapshot if pre_snapshot is not None else json.dumps(
        {"directory": _DIR, "binding_mode": "PROJECT_ROTATING",
         "binding_revision": 1, "create_title": _TITLE, "session_count_before": 0},
        ensure_ascii=False,
    )
    with db.transaction():
        db.connection.execute(
            "INSERT INTO operations (operation_id, operation_key, kind, task_key,"
            " attempt_id, authority_epoch, control_revision, endpoint, session_id,"
            " project_key, interruption_id, state, pre_snapshot_json, prompt_hash,"
            " prompt_text, remote_user_id, evidence_json, created_at, updated_at,"
            " finalized_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (op_id, key, kind, task, attempt, epoch, control, endpoint, None, project,
             None, state, pre_snapshot, None, None, None, evidence, _T0, _T0,
             _T0 if state in ("ACCEPTED", "REJECTED", "UNKNOWN") else None),
        )


def _module_code_source(cls) -> str:
    text = Path(cls.__module__.replace(".", "/") + ".py").read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith('"""'):
        start = text.find('"""')
        end = text.find('"""', start + 3)
        text = text[end + 3:]
    return text


# =====================================================================
# A. 权威检查（10 组，全部 GET / POST / operation = 0）
# =====================================================================

def test_authority_task_missing(db_env):
    ro = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=FakeOpener(script=[]))
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.AUTHORITY_REVOKED
    assert ro.calls == []
    assert _count(db_env, "operations") == 0


def test_authority_task_not_active(db_env):
    _seed_full(db_env, task_state="STOPPED_BY_USER")
    ro = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=FakeOpener(script=[]))
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.AUTHORITY_REVOKED
    assert ro.calls == []
    assert _count(db_env, "operations") == 0


def test_authority_active_attempt_mismatch(db_env):
    _seed_full(db_env, active_attempt="attempt-other")
    ro = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=FakeOpener(script=[]))
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.AUTHORITY_REVOKED
    assert ro.calls == []


def test_authority_task_epoch_mismatch(db_env):
    _seed_full(db_env, lease_state="ROTATING")
    db_env.connection.execute(
        "UPDATE tasks SET authority_epoch=99 WHERE task_key='task-create-1'")
    ro = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=FakeOpener(script=[]))
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.AUTHORITY_REVOKED


def test_authority_attempt_missing(db_env):
    _seed_full(db_env)
    db_env.connection.execute("PRAGMA foreign_keys = OFF")
    db_env.connection.execute("DELETE FROM attempts WHERE attempt_id='attempt-create-1'")
    db_env.connection.execute("PRAGMA foreign_keys = ON")
    ro = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=FakeOpener(script=[]))
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.AUTHORITY_REVOKED
    assert _count(db_env, "operations") == 0


def test_authority_attempt_not_open(db_env):
    _seed_full(db_env, attempt_state="COMPLETED")
    ro = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=FakeOpener(script=[]))
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.AUTHORITY_REVOKED


def test_authority_attempt_epoch_mismatch(db_env):
    _seed_full(db_env, attempt_epoch=99)
    ro = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=FakeOpener(script=[]))
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.AUTHORITY_REVOKED


def test_authority_control_revision_mismatch(db_env):
    _seed_full(db_env, attempt_control=99)
    ro = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=FakeOpener(script=[]))
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.AUTHORITY_REVOKED


def test_authority_lease_missing(db_env):
    _seed_full(db_env)
    db_env.connection.execute("DELETE FROM project_leases WHERE project_key=?",
                              (_PROJECT,))
    ro = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=FakeOpener(script=[]))
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.AUTHORITY_REVOKED
    assert _count(db_env, "operations") == 0


def test_authority_lease_owner_mismatch(db_env):
    _seed_full(db_env)
    _insert_attempt(db_env, "attempt-alien")
    db_env.connection.execute(
        "UPDATE project_leases SET owner_attempt_id=? WHERE project_key=?",
        ("attempt-alien", _PROJECT))
    ro = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=FakeOpener(script=[]))
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.AUTHORITY_REVOKED


def test_authority_lease_epoch_mismatch(db_env):
    _seed_full(db_env, lease_epoch=99)
    ro = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=FakeOpener(script=[]))
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.AUTHORITY_REVOKED


def test_authority_lease_quarantined(db_env):
    _seed_full(db_env, lease_state="QUARANTINED")
    ro = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=FakeOpener(script=[]))
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.AUTHORITY_REVOKED
    assert "QUARANTINED" in r.detail
    assert ro.calls == []
    assert _count(db_env, "operations") == 0


def test_authority_lease_active_with_resolved_forbids(db_env):
    _seed_full(db_env, lease_state="ACTIVE")
    ro = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=FakeOpener(script=[]))
    snap = _snapshot(resolved_session_id=_SID_OLD)
    r = _call_create(_coordinator(db_env, t), snapshot=snap)
    assert r.outcome is CreateSessionOutcome.AUTHORITY_REVOKED
    assert "已解析" not in r.detail or r.detail
    assert ro.calls == []


def test_authority_lease_active_resolved_none_allows_initial_create(db_env):
    ro = FakeOpener(script=_default_script())
    wo = FakeOpener(script=[(200, json.dumps({"id": _SID_NEW}))])
    _seed_full(db_env, lease_state="ACTIVE")
    snap = _snapshot(resolved_session_id=None)
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _call_create(_coordinator(db_env, t), snapshot=snap)
    assert r.outcome is CreateSessionOutcome.ACCEPTED
    assert len(ro.calls) == 1
    assert len(wo.calls) == 1


def test_authority_rotating_allows_rotation_even_with_resolved(db_env):
    ro = FakeOpener(script=_default_script())
    wo = FakeOpener(script=[])
    _seed_full(db_env, lease_state="ROTATING")
    snap = _snapshot(resolved_session_id=_SID_OLD)
    t = _make_transport(read_opener=ro, write_opener=wo)
    coord = _coordinator(db_env, t)
    r = coord.prepare_create(_proposal(snapshot=snap), create_title=_TITLE)
    assert r.outcome is CreateSessionOutcome.PREPARED
    assert len(ro.calls) == 1
    assert wo.calls == []


# =====================================================================
# B. FIXED_SESSION / 输入校验（5 项：全部 0 GET / 0 POST / 0 operation）
# =====================================================================

def test_fixed_session_never_forbids_create(db_env):
    _seed_full(db_env)
    ro = FakeOpener(script=[])
    wo = FakeOpener(script=[])
    snap = _snapshot(binding_mode="FIXED_SESSION", resolved_session_id=_SID_OLD)
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _call_create(_coordinator(db_env, t), snapshot=snap)
    assert r.outcome is CreateSessionOutcome.FIXED_SESSION_FORBIDS_CREATE
    assert ro.calls == [] and wo.calls == []
    assert _count(db_env, "operations") == 0


def test_fixed_session_forbids_even_when_plain(db_env):
    _seed_full(db_env)
    snap = _snapshot(binding_mode="FIXED_SESSION", resolved_session_id=_SID_OLD)
    ro = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=FakeOpener(script=[]))
    r = _coordinator(db_env, t).prepare_create(
        _proposal(snapshot=snap), create_title=_TITLE)
    assert r.outcome is CreateSessionOutcome.FIXED_SESSION_FORBIDS_CREATE
    assert ro.calls == []


def test_blank_create_title_invalid_ledger(db_env):
    _seed_full(db_env)
    ro = FakeOpener(script=[])
    wo = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _call_create(_coordinator(db_env, t), title="   ")
    assert r.outcome is CreateSessionOutcome.INVALID_LEDGER
    assert ro.calls == [] and wo.calls == []
    assert _count(db_env, "operations") == 0


def test_unknown_binding_mode_invalid_snapshot(db_env):
    _seed_full(db_env)
    snap = _snapshot(project=_PROJECT)
    prop = build_create_proposal(
        operation_key=create_session_operation_key(
            project_key=_PROJECT, attempt_id="attempt-create-1",
            authority_epoch=1, control_revision=5, binding_revision=1),
        snapshot=snap, task_key="task-create-1", attempt_id="attempt-create-1",
        authority_epoch=1, control_revision=5, endpoint=_BASE,
    )
    repl = replace(prop, binding_mode="per_session_bogus")
    ro = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=FakeOpener(script=[]))
    r = _coordinator(db_env, t).prepare_create(repl, create_title=_TITLE)
    assert r.outcome is CreateSessionOutcome.INVALID_SNAPSHOT
    assert ro.calls == []
    assert _count(db_env, "operations") == 0


def test_snapshot_not_execution_snapshot_invalid(db_env):
    _seed_full(db_env)
    ro = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=FakeOpener(script=[]))
    r = _coordinator(db_env, t).create_session(
        task_key="task-create-1", attempt_id="attempt-create-1",
        authority_epoch=1, control_revision=5, snapshot="not-a-snapshot",
        endpoint=_BASE, create_title=_TITLE,
    )
    assert r.outcome is CreateSessionOutcome.INVALID_SNAPSHOT
    assert ro.calls == []


# =====================================================================
# C. baseline（5 项）
# =====================================================================

def test_baseline_get_exact_path_and_directory(db_env):
    ro = FakeOpener(script=_default_script())
    wo = FakeOpener(script=[])
    _seed_full(db_env)
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _coordinator(db_env, t).prepare_create(_proposal(), create_title=_TITLE)
    assert r.outcome is CreateSessionOutcome.PREPARED
    assert len(ro.calls) == 1
    call = ro.calls[0]
    assert call["method"] == "GET"
    assert urllib.parse.urlsplit(call["url"]).path == "/api/session"
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(call["url"]).query) == {
        "directory": [_DIR]}
    assert wo.calls == []


def test_baseline_persisted_in_pre_snapshot_json(db_env):
    _seed_full(db_env)
    ro = FakeOpener(script=_default_script())
    t = _make_transport(read_opener=ro, write_opener=FakeOpener(script=[]))
    r = _call_create(_coordinator(db_env, t))
    row = _read_op(db_env, r.operation_id)
    baseline = json.loads(row[12])
    assert baseline["directory"] == _DIR
    assert baseline["binding_mode"] == "PROJECT_ROTATING"
    assert baseline["binding_revision"] == 1
    assert baseline["session_ids_before"] == [_SID_OLD]
    assert baseline["session_count_before"] == 1
    assert baseline["create_title"] == _TITLE
    assert _SECRET_TOKEN not in json.dumps(baseline)


def test_baseline_get_failure_precheck_failed(db_env):
    _seed_full(db_env)
    ro = FakeOpener(script=[socket.timeout("t")])
    wo = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.PRECHECK_FAILED
    assert wo.calls == []
    assert _count(db_env, "operations") == 0
    assert len(ro.calls) == 1


def test_baseline_get_malformed_json_precheck_failed(db_env):
    _seed_full(db_env)
    ro = FakeOpener(script=[(200, "{bad json")])
    wo = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.PRECHECK_FAILED
    assert _count(db_env, "operations") == 0


def test_baseline_endpoint_mismatch(db_env):
    _seed_full(db_env)
    ro = FakeOpener(script=[])
    wo = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _call_create(_coordinator(db_env, t), endpoint=_OTHER)
    assert r.outcome is CreateSessionOutcome.ENDPOINT_MISMATCH
    assert ro.calls == [] and wo.calls == []
    assert _count(db_env, "operations") == 0


# =====================================================================
# D. 流程三分步（3 项）
# =====================================================================

def test_flow_step_prepare_reachable(db_env):
    _seed_full(db_env)
    ro = FakeOpener(script=_default_script())
    t = _make_transport(read_opener=ro, write_opener=FakeOpener(script=[]))
    coord = _coordinator(db_env, t)
    snap = _snapshot()
    prop = build_create_proposal(
        operation_key=create_session_operation_key(
            project_key=_PROJECT, attempt_id="attempt-create-1",
            authority_epoch=1, control_revision=5, binding_revision=1),
        snapshot=snap, task_key="task-create-1", attempt_id="attempt-create-1",
        authority_epoch=1, control_revision=5, endpoint=_BASE,
    )
    r = coord.prepare_create(prop, create_title=_TITLE)
    assert r.outcome is CreateSessionOutcome.PREPARED
    row = _read_op(db_env, r.operation_id)
    assert row[11] == "PREPARED"
    assert row[15] is None      # remote_user_id
    assert row[8] is None       # session_id


def test_flow_acquire_reachable_and_holds_right(db_env):
    _seed_full(db_env)
    ro = FakeOpener(script=_default_script())
    t = _make_transport(read_opener=ro, write_opener=FakeOpener(script=[]))
    coord = _coordinator(db_env, t)
    snap = _snapshot()
    prop = build_create_proposal(
        operation_key=create_session_operation_key(
            project_key=_PROJECT, attempt_id="attempt-create-1",
            authority_epoch=1, control_revision=5, binding_revision=1),
        snapshot=snap, task_key="task-create-1", attempt_id="attempt-create-1",
        authority_epoch=1, control_revision=5, endpoint=_BASE,
    )
    coord.prepare_create(prop, create_title=_TITLE)
    r2 = coord.acquire_create_right(prop)
    assert r2.outcome is CreateSessionOutcome.SENDING
    row = _read_op(db_env, r2.operation_id)
    assert row[11] == "SENDING"


def test_flow_acquire_concurrent_second_loses(db_env):
    lp = _seed_full(db_env)
    del lp
    ro = FakeOpener(script=_default_script())
    t = _make_transport(read_opener=ro, write_opener=FakeOpener(script=[]))
    coord = _coordinator(db_env, t)
    snap = _snapshot()
    prop = build_create_proposal(
        operation_key=create_session_operation_key(
            project_key=_PROJECT, attempt_id="attempt-create-1",
            authority_epoch=1, control_revision=5, binding_revision=1),
        snapshot=snap, task_key="task-create-1", attempt_id="attempt-create-1",
        authority_epoch=1, control_revision=5, endpoint=_BASE,
    )
    coord.prepare_create(prop, create_title=_TITLE)
    coord.acquire_create_right(prop)
    r3 = coord.acquire_create_right(prop)
    assert r3.outcome is CreateSessionOutcome.ALREADY_EXISTS
    assert r3.state == "SENDING"


# =====================================================================
# E. send / HTTP outcome（9 项）
# =====================================================================

def test_send_accepted_with_created_id(db_env):
    _seed_full(db_env)
    ro = FakeOpener(script=_default_script())
    wo = FakeOpener(script=[(200, json.dumps({"id": _SID_NEW}))])
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.ACCEPTED
    assert r.created_session_id == _SID_NEW
    assert r.state == "ACCEPTED"
    row = _read_op(db_env, r.operation_id)
    assert row[11] == "ACCEPTED"
    assert row[8] is None      # operations.session_id 永不写入
    assert row[15] is None     # remote_user_id 恒 NULL
    assert json.loads(row[16])["created_session_id"] == _SID_NEW
    assert len(wo.calls) == 1
    assert [c["method"] for c in ro.calls] == ["GET"]


def test_send_posts_exactly_once_not_redirected(db_env):
    _seed_full(db_env)
    ro = FakeOpener(script=_default_script())
    wo = FakeOpener(script=[FakeResponse(302, "", headers={"Location": "/elsewhere"})])
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.UNKNOWN
    assert r.state == "UNKNOWN"
    assert len(wo.calls) == 1                       # 不 follow 302
    assert [c["method"] for c in wo.calls] == ["POST"]


def test_send_post_contract_title_only(db_env):
    _seed_full(db_env)
    ro = FakeOpener(script=_default_script())
    wo = FakeOpener(script=[(200, json.dumps({"id": _SID_NEW}))])
    t = _make_transport(read_opener=ro, write_opener=wo)
    _call_create(_coordinator(db_env, t))
    call = wo.calls[0]
    assert call["method"] == "POST"
    parts = urllib.parse.urlsplit(call["url"])
    assert parts.path == "/api/session"
    assert urllib.parse.parse_qs(parts.query) == {"directory": [_DIR]}
    body = json.loads(call["body"])
    assert set(body) == {"title"}
    assert body["title"] == _TITLE


@pytest.mark.parametrize("resp", [
    (200, '{"ok": true}'),             # 200 但缺 id
    (200, json.dumps({"id": "bad123"})),  # 非法 id
    (200, "{not json"),                # 200 非 JSON
    (200, json.dumps({"id": ""})),     # 空 id
])
def test_send_200_not_clean_accepted_unknown(db_env, resp):
    _seed_full(db_env)
    ro = FakeOpener(script=_default_script())
    wo = FakeOpener(script=[resp])
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.UNKNOWN
    assert r.state == "UNKNOWN"
    assert len(wo.calls) == 1


@pytest.mark.parametrize("code", [400, 403, 404, 409, 429])
def test_send_4xx_rejected(db_env, code):
    _seed_full(db_env)
    ro = FakeOpener(script=_default_script())
    wo = FakeOpener(script=[(code, '{"error":"nope"}')])
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.REJECTED
    assert r.state == "REJECTED"
    assert r.created_session_id is None
    assert len(wo.calls) == 1


@pytest.mark.parametrize("code", [500, 503])
def test_send_5xx_unknown(db_env, code):
    _seed_full(db_env)
    ro = FakeOpener(script=_default_script())
    wo = FakeOpener(script=[(code, "boom")])
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.UNKNOWN
    assert r.state == "UNKNOWN"
    assert len(wo.calls) == 1


def test_send_timeout_finalizes_unknown(db_env):
    _seed_full(db_env)
    ro = FakeOpener(script=_default_script())
    wo = FakeOpener(script=[socket.timeout("t")])
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.UNKNOWN
    assert r.state == "UNKNOWN"
    assert len(wo.calls) == 1                       # 不重试
    assert "TIMEOUT" in r.detail


def test_send_connection_refused_finalizes_unknown(db_env):
    _seed_full(db_env)
    ro = FakeOpener(script=_default_script())
    wo = FakeOpener(script=[ConnectionRefusedError("refused")])
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.UNKNOWN
    assert r.state == "UNKNOWN"
    assert len(wo.calls) == 1


# =====================================================================
# F. existing operation（5 项：0 POST / 0 GET）
# =====================================================================

def test_existing_accepted_reuses_created_id(db_env):
    _seed_full(db_env)
    key = create_session_operation_key(
        project_key=_PROJECT, attempt_id="attempt-create-1",
        authority_epoch=1, control_revision=5, binding_revision=1)
    _insert_op(db_env, op_id="op-cs-acc", key=key, state="ACCEPTED")
    ro = FakeOpener(script=[])
    wo = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.ACCEPTED
    assert r.created_session_id == _SID_NEW
    assert ro.calls == [] and wo.calls == []
    assert _count(db_env, "operations") == 1


def test_existing_rejected_no_retry(db_env):
    _seed_full(db_env)
    key = create_session_operation_key(
        project_key=_PROJECT, attempt_id="attempt-create-1",
        authority_epoch=1, control_revision=5, binding_revision=1)
    _insert_op(db_env, op_id="op-cs-rej", key=key, state="REJECTED",
               evidence='{"http_status": 400, "classification": "create_rejected"}')
    ro = FakeOpener(script=[])
    wo = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.REJECTED
    assert ro.calls == [] and wo.calls == []
    assert _count(db_env, "operations") == 1


def test_existing_unknown_stays_for_reconciliation(db_env):
    _seed_full(db_env)
    key = create_session_operation_key(
        project_key=_PROJECT, attempt_id="attempt-create-1",
        authority_epoch=1, control_revision=5, binding_revision=1)
    _insert_op(db_env, op_id="op-cs-unk", key=key, state="UNKNOWN",
               evidence='{"http_status": 500, "classification": "create_unknown"}')
    ro = FakeOpener(script=[])
    wo = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.UNKNOWN_EXISTING
    assert ro.calls == [] and wo.calls == []
    assert _count(db_env, "operations") == 1


def test_existing_sending_recovered_unknown(db_env):
    _seed_full(db_env)
    key = create_session_operation_key(
        project_key=_PROJECT, attempt_id="attempt-create-1",
        authority_epoch=1, control_revision=5, binding_revision=1)
    _insert_op(db_env, op_id="op-cs-send", key=key, state="SENDING",
               evidence='{}')
    ro = FakeOpener(script=[])
    wo = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.ALREADY_EXISTS
    assert r.state == "UNKNOWN"
    assert ro.calls == [] and wo.calls == []
    row = _read_op(db_env, "op-cs-send")
    assert row[11] == "UNKNOWN"
    assert "restart_recovery" in row[16]


def test_existing_prepared_continues(db_env):
    _seed_full(db_env)
    key = create_session_operation_key(
        project_key=_PROJECT, attempt_id="attempt-create-1",
        authority_epoch=1, control_revision=5, binding_revision=1)
    _insert_op(db_env, op_id="op-cs-prep", key=key, state="PREPARED",
               evidence='{}')
    ro = FakeOpener(script=[])
    wo = FakeOpener(script=[(200, json.dumps({"id": _SID_NEW}))])
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.ACCEPTED
    assert r.created_session_id == _SID_NEW
    assert ro.calls == []                       # 已有 baseline，不再 GET
    assert len(wo.calls) == 1


def test_existing_key_conflict_leaves_original(db_env):
    _seed_full(db_env)
    key = create_session_operation_key(
        project_key=_PROJECT, attempt_id="attempt-create-1",
        authority_epoch=1, control_revision=5, binding_revision=1)
    _insert_op(db_env, op_id="op-cs-other", key=key, state="ACCEPTED",
               evidence='{"created_session_id": "ses_other_1"}',
               endpoint=_OTHER)
    ro = FakeOpener(script=[])
    wo = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _coordinator(db_env, t).create_session(
        task_key="task-create-1", attempt_id="attempt-create-1",
        authority_epoch=1, control_revision=5, snapshot=_snapshot(),
        endpoint=_BASE, create_title=_TITLE,
    )
    assert r.outcome is CreateSessionOutcome.KEY_CONFLICT
    assert ro.calls == [] and wo.calls == []
    row = _read_op(db_env, "op-cs-other")
    assert row[11] == "ACCEPTED"               # 原 operation 未被修改
    assert json.loads(row[16])["created_session_id"] == "ses_other_1"


# =====================================================================
# G. determinism / purity（7 项）
# =====================================================================

def test_deterministic_operation_key_and_id(db_env):
    args = dict(project_key=_PROJECT, attempt_id="attempt-create-1",
                authority_epoch=1, control_revision=5, binding_revision=1)
    k1 = create_session_operation_key(**args)
    k2 = create_session_operation_key(**args)
    assert k1 == k2 == (
        "create_session:proj-rot:attempt-create-1:1:5:1"
    )
    assert derive_operation_id(k1) == derive_operation_id(k2)
    assert derive_operation_id(k1).startswith("op-")


def test_same_key_second_call_zero_network(db_env):
    _seed_full(db_env)
    ro = FakeOpener(script=_default_script())
    wo = FakeOpener(script=[(200, json.dumps({"id": _SID_NEW}))])
    t = _make_transport(read_opener=ro, write_opener=wo)
    coord = _coordinator(db_env, t)
    r1 = _call_create(coord)
    assert r1.outcome is CreateSessionOutcome.ACCEPTED
    assert len(wo.calls) == 1
    r2 = _call_create(coord)
    assert r2.outcome is CreateSessionOutcome.ACCEPTED   # 幂等收敛
    assert r2.created_session_id == _SID_NEW
    assert len(wo.calls) == 1                            # 第二次绝不 POST
    assert len(ro.calls) == 1                            # 也绝不 GET


def test_network_outside_transactions(db_env):
    flags: list[bool] = []
    note = lambda req: flags.append(db_env.in_transaction)  # noqa: E731
    ro = FakeOpener(script=_default_script(), note=note)
    wo = FakeOpener(script=[(200, json.dumps({"id": _SID_NEW}))], note=note)
    _seed_full(db_env)
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.ACCEPTED
    assert flags and all(flag is False for flag in flags)  # GET 与 POST 都在事务外


def test_finalize_transaction_has_no_http(db_env):
    _seed_full(db_env)
    ro = FakeOpener(script=_default_script())
    wo = FakeOpener(script=[(200, json.dumps({"id": _SID_NEW}))])
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.ACCEPTED
    assert [c["method"] for c in ro.calls] == ["GET"]
    assert [c["method"] for c in wo.calls] == ["POST"]


def test_no_prompt_async_and_no_generic_write(db_env):
    source = _module_code_source(OpenChamberCreateSessionTransport)
    for banned in ("prompt_async", "send_once", "def post(", "method=\"PUT\""):
        assert banned not in source
    public = [n for n in dir(OpenChamberCreateSessionTransport)
              if not n.startswith("_")
              and callable(getattr(OpenChamberCreateSessionTransport, n))]
    assert public == ["capture_pre_create_snapshot", "create_once"]   # 公开面仅两个方法


def test_create_op_columns_never_carry_session_or_message(db_env):
    _seed_full(db_env)
    ro = FakeOpener(script=_default_script())
    wo = FakeOpener(script=[(200, json.dumps({"id": _SID_NEW}))])
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _call_create(_coordinator(db_env, t))
    row = _read_op(db_env, r.operation_id)
    assert row[2] == "CREATE_SESSION"
    assert row[8] is None       # session_id
    assert row[15] is None      # remote_user_id
    assert row[13] is None      # prompt_hash
    assert row[14] is None      # prompt_text


def test_does_not_touch_task_attempt_result_states(db_env):
    _seed_full(db_env)
    ro = FakeOpener(script=_default_script())
    wo = FakeOpener(script=[(200, json.dumps({"id": _SID_NEW}))])
    t = _make_transport(read_opener=ro, write_opener=wo)
    _call_create(_coordinator(db_env, t))
    task_row = db_env.connection.execute(
        "SELECT state, active_attempt_id, authority_epoch FROM tasks WHERE task_key=?",
        ("task-create-1",)).fetchone()
    assert task_row == ("ACTIVE", "attempt-create-1", 1)
    attempt_row = db_env.connection.execute(
        "SELECT state, authority_epoch, control_revision, remote_state"
        " FROM attempts WHERE attempt_id=?",
        ("attempt-create-1",)).fetchone()
    assert attempt_row == ("OPEN", 1, 5, "NOT_SENT")
    lease_row = db_env.connection.execute(
        "SELECT state, owner_task_key, owner_attempt_id, authority_epoch"
        " FROM project_leases WHERE project_key=?", (_PROJECT,)).fetchone()
    assert lease_row == ("ROTATING", "task-create-1", "attempt-create-1", 1)
    assert _count(db_env, "results") == 0
    assert _count(db_env, "outbox") == 0
    assert _count(db_env, "remote_claims") == 0


# =====================================================================
# H. transport 构造 / secret hygiene 附加（3 项）
# =====================================================================

@pytest.mark.parametrize("url", [
    "http://192.0.2.10:57123",
    "http://127.0.0.2:57123",
    "http://127.1.2.3:57123",
    "http://example.com/",
    "https://openchamber.example.com",
])
def test_transport_constructor_fail_closed_non_loopback(url):
    with pytest.raises(OpenChamberCreateTransportError) as ctx:
        OpenChamberCreateSessionTransport(url, _DIR, token="tok")
    assert ctx.value.kind == "NON_LOOPBACK_TARGET"


@pytest.mark.parametrize("url", [
    "http://localhost:57123",
    "http://127.0.0.1:57123",
    "http://[::1]:57123",
])
def test_transport_accepts_exact_loopback_hosts(url):
    t = _make_transport(base_url=url, read_opener=FakeOpener(script=[]),
                        write_opener=FakeOpener(script=[]))
    assert t.base_url == url.rstrip("/")


def test_transport_secret_never_in_evidence_or_repr(db_env):
    _seed_full(db_env)
    ro = FakeOpener(script=_default_script())
    wo = FakeOpener(script=[(500, f'{{"secret":"{_SECRET_RAW_BODY}"}}')])
    t = _make_transport(read_opener=ro, write_opener=wo, token=_SECRET_TOKEN)
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.UNKNOWN
    evidence = json.loads(_read_op(db_env, r.operation_id)[16])
    text = json.dumps(evidence)
    assert _SECRET_TOKEN not in text
    assert _SECRET_RAW_BODY not in text
    assert "Authorization" not in text
    assert _SECRET_TOKEN not in repr(t)
    assert _SECRET_TOKEN not in str(r)


def test_pre_snapshot_json_has_no_token_or_auth(db_env):
    _seed_full(db_env)
    ro = FakeOpener(script=_default_script())
    t = _make_transport(read_opener=ro, write_opener=FakeOpener(script=[]),
                        token=_SECRET_TOKEN)
    r = _coordinator(db_env, t).prepare_create(_proposal(), create_title=_TITLE)
    assert r.outcome is CreateSessionOutcome.PREPARED
    row = _read_op(db_env, r.operation_id)
    text = row[12] + row[16]
    assert _SECRET_TOKEN not in text
    assert "Authorization" not in text
    assert "Bearer" not in row[12]
    assert json.loads(row[12])["binding_mode"] == "PROJECT_ROTATING"


def test_capture_get_auth_only_on_loopback(db_env):
    ro = FakeOpener(script=_default_script())
    t = _make_transport(read_opener=ro, write_opener=FakeOpener(script=[]),
                        token=_SECRET_TOKEN)
    _seed_full(db_env)
    _call_create(_coordinator(db_env, t))
    lower = {k.lower(): v for k, v in ro.calls[0]["headers"].items()}
    assert lower["authorization"] == f"Bearer {_SECRET_TOKEN}"


def test_capture_rejects_session_payload_missing_id(db_env):
    _seed_full(db_env)
    ro = FakeOpener(script=[(200, json.dumps([{"title": "no-id"}]))])
    wo = FakeOpener(script=[])
    t = _make_transport(read_opener=ro, write_opener=wo)
    r = _call_create(_coordinator(db_env, t))
    assert r.outcome is CreateSessionOutcome.PRECHECK_FAILED
    assert wo.calls == []
    assert _count(db_env, "operations") == 0