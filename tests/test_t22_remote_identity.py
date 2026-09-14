"""T22-02：Durable Remote Message Identity——planned remote_user_id 全生命周期（§17 18+ 项）。

覆盖：
01 new PREPARED nonblank planned id
02 planned id persisted before SENDING
03 default id uses msg_ form
04 injected factory deterministic
05 factory called once
06 existing PREPARED does not regenerate
07 duplicate operation_key reuses same id
08 acquire refuses PREPARED missing remote id
09 send_once receives ledger planned id
10 ACCEPTED preserves
11 REJECTED preserves
12 timeout UNKNOWN preserves
13 stale SENDING→UNKNOWN preserves（含 crash 重启变体）
14 returned same id accepted / transport 未返回 remote id 也 ACCEPTED
15 returned mismatched id→UNKNOWN
16 mismatch does not overwrite planned
17 UNKNOWN repeated dispatch zero resend
18 no schema migration（SUPPORTED_SCHEMA_VERSION==1）
19 SENDING 缺 durable remote identity → send_prepared fail-closed（send=0）
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timezone

import pytest

from core.dispatch import (
    DispatchError,
    DispatchOutcome,
    DispatchProposal,
    DispatchService,
    TransportTimeoutError,
    SendAttempt,
    SendOutcome,
    default_remote_user_id_factory,
    derive_operation_id,
)
from core.protocol_v1 import ProtocolFormat, content_digest, parse_message
from core.scheduler import ScheduleOutcome, Scheduler
from infra.clock import FakeClock
from storage.database import Database
from storage.operation_store import (
    OperationStore,
    OperationStoreError,
)
from storage.schema import SUPPORTED_SCHEMA_VERSION, SCHEMA_DIR
from storage.task_store import TaskStore, make_task_key

_V1 = (
    "AI_RELAY/1\n"
    "MESSAGE_ID: {task_id}\n"
    "SOURCE: CHATGPT\n"
    "TARGET: OPENCHAMBER\n"
    "TYPE: TASK\n"
    "\n"
    "{body}"
)

_T0 = "2026-10-01T09:00:00+00:00"
_WALL = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)
_ENDPOINT = "http://127.0.0.1:57123"
_MISMATCH_ID = "msg_other"


class T22Mode(str):
    ACCEPT = "ACCEPT"
    ACCEPT_NO_REMOTE = "ACCEPT_NO_REMOTE"
    REJECT = "REJECT"
    TIMEOUT = "TIMEOUT"
    MISMATCH = "MISMATCH"


class T22Transport:
    """记录 planned_remote_user_id 是否送达 send_once；按模式决定返回。"""

    def __init__(self, mode: str = T22Mode.ACCEPT, *, db: Database | None = None) -> None:
        self.mode = mode
        self.db = db
        self.snapshot_calls = 0
        self.send_calls = 0
        self.sent_planned: list[str | None] = []

    def capture_pre_send_snapshot(self, *, endpoint: str, session_id: str,
                                  task_key: str) -> dict:
        self.snapshot_calls += 1
        return {"message_ids": ["m1"]}

    def send_once(self, *, endpoint: str, session_id: str, prompt_text: str,
                  operation_id: str,
                  planned_remote_user_id: str | None) -> SendAttempt:
        self.send_calls += 1
        self.sent_planned.append(planned_remote_user_id)
        if self.mode == T22Mode.TIMEOUT:
            raise TransportTimeoutError("客户端超时（远端已收到）")
        if self.mode == T22Mode.REJECT:
            return SendAttempt(outcome=SendOutcome.REJECTED,
                               evidence={"promptDispatched": False})
        if self.mode == T22Mode.MISMATCH:
            return SendAttempt(outcome=SendOutcome.ACCEPTED,
                               remote_user_id=_MISMATCH_ID,
                               evidence={"message_id": "m"})
        if self.mode == T22Mode.ACCEPT_NO_REMOTE:
            return SendAttempt(outcome=SendOutcome.ACCEPTED,
                               evidence={"message_id": "m"})
        return SendAttempt(outcome=SendOutcome.ACCEPTED,
                           remote_user_id=planned_remote_user_id,
                           evidence={"message_id": "m"})


def _make_receive(project_key: str, session_id: str):
    from core.domain import ReceiveSettingsSnapshot, SessionBindingMode, TargetExecutor

    return ReceiveSettingsSnapshot(
        config_revision=5, committed_at=_T0, received_at=_T0,
        effective_executor=TargetExecutor.OPENCHAMBER, directory=r"D:\AIwork\proj",
        project_key=project_key, agent="build", requested_model="",
        binding_mode=SessionBindingMode.FIXED_SESSION, frozen_session_id=session_id,
    )


@pytest.fixture
def dispatch_env(tmp_path):
    db = Database(tmp_path / "t22.sqlite")
    db.open()
    tasks = TaskStore(db)
    ops = OperationStore(db)
    clock = FakeClock(wall=_WALL)
    counter = itertools.count(1)
    attempts: list[str] = []

    def attempt_factory(task_key: str) -> str:
        attempt_id = f"attempt-{next(counter):03d}"
        attempts.append(attempt_id)
        return attempt_id

    scheduler = Scheduler(db, task_store=tasks, clock=clock,
                          attempt_id_factory=attempt_factory)
    yield db, tasks, ops, clock, scheduler, attempts
    db.close()


def _start(dispatch_env, task_id: str = "t22-001", project_key: str = "p-fixed",
           session_id: str = "sess-fixed") -> tuple[str, str, str, int]:
    db, tasks, _, _, scheduler, _ = dispatch_env
    raw = _V1.format(task_id=task_id, body=f"处理任务 {task_id}")
    msg = parse_message(raw)
    task_key = make_task_key("CHATGPT", task_id)
    tasks.claim(
        task_key=task_key, peer_id="CHATGPT", task_id=task_id,
        protocol_format=ProtocolFormat.V1.value.upper(), raw_message=raw,
        body=msg.body, canonical_hash=content_digest(msg),
        receive_snapshot=_make_receive(project_key, session_id), received_at=_T0,
    )
    result = scheduler.tick()
    assert result.outcome is ScheduleOutcome.STARTED
    assert result.attempt_id is not None
    return task_key, result.attempt_id, project_key, 1


def _counting_factory(calls: list[str], value: str = "msg_x"):
    def _factory(key: str) -> str:
        calls.append(key)
        return value
    return _factory


def _proposal(task_key: str, attempt_id: str, *,
              operation_key: str | None = None, kind: str = "INITIAL_SEND",
              epoch: int = 1, control_revision: int = 0,
              endpoint: str = _ENDPOINT, session_id: str = "sess-fixed",
              project_key: str = "p-fixed", interruption_id: str | None = None,
              prompt_body: str = "继续任务的报告正文") -> DispatchProposal:
    return DispatchProposal(
        operation_key=operation_key or f"continue:{task_key}:interrupt-7",
        kind=kind, task_key=task_key, attempt_id=attempt_id,
        authority_epoch=epoch, control_revision=control_revision,
        endpoint=endpoint, session_id=session_id, project_key=project_key,
        interruption_id=interruption_id, prompt_body=prompt_body,
    )


def _count(db, table: str, where: str = "", params: tuple = ()) -> int:
    return int(
        db.connection.execute(f"SELECT COUNT(*) FROM {table} {where}", params).fetchone()[0]
    )


class TestPlannedIdentity:
    def test_01_new_prepared_nonblank_planned_id(self, dispatch_env):
        db, _, ops, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        dispatch = DispatchService(db, operation_store=ops,
                                   clock=FakeClock(wall=_WALL))
        result = dispatch.prepare(p, transport=T22Transport(), now=_WALL)
        assert result.outcome is DispatchOutcome.PREPARED
        record = ops.read_by_key(p.operation_key)
        assert record is not None
        assert record.remote_user_id
        assert record.remote_user_id.strip()

    def test_02_planned_id_persisted_before_sending(self, dispatch_env):
        db, _, ops, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        dispatch = DispatchService(db, operation_store=ops,
                                   clock=FakeClock(wall=_WALL))
        assert dispatch.prepare(p, transport=T22Transport(), now=_WALL).outcome is \
            DispatchOutcome.PREPARED
        planned = ops.read_by_key(p.operation_key).remote_user_id
        assert planned
        assert dispatch.acquire_send_right(p, now=_WALL).outcome is DispatchOutcome.SENDING
        record = ops.read_by_key(p.operation_key)
        assert record.state == "SENDING"
        assert record.remote_user_id == planned  # SENDING 前已 durable

    def test_03_default_id_uses_msg_form(self):
        value = default_remote_user_id_factory("continue:k:x")
        assert value.startswith("msg_")
        assert len(value) > 4
        hex_part = value[len("msg_"):]
        assert all(c in "0123456789abcdef" for c in hex_part)
        assert len(hex_part) == 32  # token_hex(16) 输出 32 位小写 hex

    def test_04_injected_factory_deterministic(self, dispatch_env):
        db, _, ops, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        dispatch = DispatchService(db, operation_store=ops,
                                   clock=FakeClock(wall=_WALL),
                                   remote_user_id_factory=lambda _k: "msg_fixed")
        assert dispatch.prepare(p, transport=T22Transport(), now=_WALL).outcome is \
            DispatchOutcome.PREPARED
        assert ops.read_by_key(p.operation_key).remote_user_id == "msg_fixed"

    def test_05_factory_called_once(self, dispatch_env):
        db, _, _, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        calls: list[str] = []
        dispatch = DispatchService(db, clock=FakeClock(wall=_WALL),
                                   remote_user_id_factory=_counting_factory(calls))
        fake = T22Transport()
        assert dispatch.dispatch_once(p, transport=fake, now=_WALL).outcome is \
            DispatchOutcome.ACCEPTED
        assert len(calls) == 1  # 只调用一次 factory
        assert len(fake.sent_planned) == 1

    def test_06_existing_prepared_does_not_regenerate(self, dispatch_env):
        db, _, ops, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        calls: list[str] = []
        dispatch = DispatchService(db, operation_store=ops,
                                   clock=FakeClock(wall=_WALL),
                                   remote_user_id_factory=_counting_factory(calls))
        first = dispatch.prepare(p, transport=T22Transport(), now=_WALL)
        assert first.outcome is DispatchOutcome.PREPARED
        assert len(calls) == 1
        second = dispatch.prepare(p, transport=T22Transport(), now=_WALL)
        assert second.outcome is DispatchOutcome.PREPARED
        assert len(calls) == 1  # existing PREPARED 不重新生成
        assert ops.read_by_key(p.operation_key).remote_user_id == "msg_x"

    def test_07_duplicate_operation_key_reuses_same_id(self, dispatch_env):
        db, _, _, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        fake = T22Transport()
        dispatch = DispatchService(db, clock=FakeClock(wall=_WALL))
        assert dispatch.dispatch_once(p, transport=fake, now=_WALL).outcome is \
            DispatchOutcome.ACCEPTED
        rec1 = dispatch._ops.read_by_key(p.operation_key)
        again = dispatch.dispatch_once(p, transport=fake, now=_WALL)
        assert again.outcome is DispatchOutcome.ALREADY_EXISTS
        rec2 = dispatch._ops.read_by_key(p.operation_key)
        assert rec1.operation_id == rec2.operation_id
        assert rec1.remote_user_id == rec2.remote_user_id
        assert fake.send_calls == 1


class TestSafety:
    def test_08_acquire_refuses_prepared_missing_remote_id(self, dispatch_env):
        db, _, ops, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        fake = T22Transport()
        dispatch = DispatchService(db, operation_store=ops,
                                   clock=FakeClock(wall=_WALL))
        assert dispatch.prepare(p, transport=fake, now=_WALL).outcome is \
            DispatchOutcome.PREPARED
        assert ops.read_by_key(p.operation_key).remote_user_id
        db.connection.execute(
            "UPDATE operations SET remote_user_id=NULL WHERE operation_key=?",
            (p.operation_key,),
        )  # 模拟历史/异常 PREPARED 缺 durable identity，而非经正常入口构造 NULL
        result = dispatch.acquire_send_right(p, now=_WALL)
        assert result.outcome is DispatchOutcome.SEND_RIGHT_REVOKED
        assert "缺少 durable remote identity" in result.detail
        assert fake.send_calls == 0
        assert _count(db, "operations") == 1  # 不创建第二个 operation

    def test_20_prepare_in_rejects_blank_planned_id_for_send_kinds(self, dispatch_env):
        db, _, ops, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        for kind in ("INITIAL_SEND", "CONTINUE"):
            p = _proposal(task_key, attempt_id, project_key=project,
                          kind=kind, operation_key=f"blank:{kind}:interrupt-7")
            with db.transaction():
                with pytest.raises(OperationStoreError):
                    ops.prepare_in(
                        db.connection, proposal=p,
                        operation_id=derive_operation_id(p.operation_key),
                        pre_snapshot_json="{}", prompt_text="x", prompt_hash="h",
                        remote_user_id=None, created_at=_T0,
                    )
        assert _count(db, "operations") == 0  # NULL/blank planned 的 PREPARED 一条都不落库

    def test_21_dispatch_rejects_blank_factory_output(self, dispatch_env):
        db, _, _, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        dispatch = DispatchService(db, clock=FakeClock(wall=_WALL),
                                   remote_user_id_factory=lambda _k: "   ")
        with pytest.raises(DispatchError):
            dispatch.prepare(p, transport=T22Transport(), now=_WALL)
        assert _count(db, "operations") == 0

    def test_19_send_prepared_refuses_sending_missing_remote_id(self, dispatch_env):
        db, _, ops, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        dispatch = DispatchService(db, operation_store=ops,
                                   clock=FakeClock(wall=_WALL))
        assert dispatch.prepare(p, transport=T22Transport(), now=_WALL).outcome is \
            DispatchOutcome.PREPARED
        assert dispatch.acquire_send_right(p, now=_WALL).outcome is DispatchOutcome.SENDING
        db.connection.execute(
            "UPDATE operations SET remote_user_id=NULL WHERE operation_key=?",
            (p.operation_key,),
        )
        fake = T22Transport()
        result = dispatch.send_prepared(p, operation_key=p.operation_key,
                                        transport=fake, now=_WALL)
        assert result.outcome is DispatchOutcome.SEND_RIGHT_REVOKED
        assert "缺少 durable remote identity" in result.detail
        assert fake.send_calls == 0

    def test_17_unknown_repeat_dispatch_zero_resend(self, dispatch_env):
        db, _, _, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        fake = T22Transport(T22Mode.MISMATCH)
        dispatch = DispatchService(db, clock=FakeClock(wall=_WALL))
        first = dispatch.dispatch_once(p, transport=fake, now=_WALL)
        assert first.outcome is DispatchOutcome.UNKNOWN
        assert fake.send_calls == 1
        second = dispatch.dispatch_once(p, transport=fake, now=_WALL)
        assert second.outcome is DispatchOutcome.ALREADY_EXISTS
        assert second.state == "UNKNOWN"
        assert fake.send_calls == 1  # UNKNOWN 绝不第二次 POST
        assert _count(db, "operations") == 1

    def test_18_no_schema_migration(self, dispatch_env):
        _, _, _, _, _, _ = dispatch_env
        db = dispatch_env[0]
        assert SUPPORTED_SCHEMA_VERSION == 1
        assert db.schema_version() == 1
        files = sorted(p.name for p in SCHEMA_DIR.glob("*.sql"))
        assert files == ["001_initial.sql"]  # 复用现有 remote_user_id TEXT 列，无新增迁移


class TestSeamAndPreservation:
    def test_09_send_once_receives_ledger_planned_id(self, dispatch_env):
        db, _, _, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        fake = T22Transport()
        dispatch = DispatchService(db, clock=FakeClock(wall=_WALL))
        assert dispatch.dispatch_once(p, transport=fake, now=_WALL).outcome is \
            DispatchOutcome.ACCEPTED
        planned = dispatch._ops.read_by_key(p.operation_key).remote_user_id
        assert fake.sent_planned == [planned]  # value 来自 Operation Ledger

    def test_10_accepted_preserves_planned_id(self, dispatch_env):
        db, _, _, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        fake = T22Transport()
        dispatch = DispatchService(db, clock=FakeClock(wall=_WALL))
        assert dispatch.dispatch_once(p, transport=fake, now=_WALL).outcome is \
            DispatchOutcome.ACCEPTED
        record = dispatch._ops.read_by_key(p.operation_key)
        assert record.state == "ACCEPTED"
        assert record.remote_user_id == fake.sent_planned[0]

    def test_14a_transport_not_returning_remote_id_accepted(self, dispatch_env):
        db, _, _, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        fake = T22Transport(T22Mode.ACCEPT_NO_REMOTE)
        dispatch = DispatchService(db, clock=FakeClock(wall=_WALL))
        result = dispatch.dispatch_once(p, transport=fake, now=_WALL)
        assert result.outcome is DispatchOutcome.ACCEPTED
        record = dispatch._ops.read_by_key(p.operation_key)
        assert record.state == "ACCEPTED"
        assert record.remote_user_id == fake.sent_planned[0]  # planned 保留

    def test_11_rejected_preserves_planned_id(self, dispatch_env):
        db, _, _, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        fake = T22Transport(T22Mode.REJECT)
        dispatch = DispatchService(db, clock=FakeClock(wall=_WALL))
        assert dispatch.dispatch_once(p, transport=fake, now=_WALL).outcome is \
            DispatchOutcome.REJECTED
        record = dispatch._ops.read_by_key(p.operation_key)
        assert record.state == "REJECTED"
        assert record.remote_user_id == fake.sent_planned[0]

    def test_12_timeout_unknown_preserves_planned_id(self, dispatch_env):
        db, _, _, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        fake = T22Transport(T22Mode.TIMEOUT)
        dispatch = DispatchService(db, clock=FakeClock(wall=_WALL))
        assert dispatch.dispatch_once(p, transport=fake, now=_WALL).outcome is \
            DispatchOutcome.UNKNOWN
        record = dispatch._ops.read_by_key(p.operation_key)
        assert record.state == "UNKNOWN"
        assert record.remote_user_id == fake.sent_planned[0]
        assert "transport_error" in record.evidence_json

    def test_13_stale_sending_preserves_planned_id(self, dispatch_env):
        db, _, ops, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        dispatch = DispatchService(db, operation_store=ops,
                                   clock=FakeClock(wall=_WALL))
        assert dispatch.prepare(p, transport=T22Transport(), now=_WALL).outcome is \
            DispatchOutcome.PREPARED
        assert dispatch.acquire_send_right(p, now=_WALL).outcome is DispatchOutcome.SENDING
        planned = ops.read_by_key(p.operation_key).remote_user_id
        assert planned
        fake = T22Transport()
        result = dispatch.dispatch_once(p, transport=fake, now=_WALL)
        assert result.outcome is DispatchOutcome.ALREADY_EXISTS
        assert result.state == "UNKNOWN"
        assert fake.send_calls == 0
        record = ops.read_by_key(p.operation_key)
        assert record.state == "UNKNOWN"
        assert record.remote_user_id == planned  # SENDING→UNKNOWN 原样保留

    def test_13b_stale_sending_after_restart_preserves_planned_id(self, dispatch_env):
        db, _, ops, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        dispatch = DispatchService(db, operation_store=ops,
                                   clock=FakeClock(wall=_WALL))
        assert dispatch.prepare(p, transport=T22Transport(), now=_WALL).outcome is \
            DispatchOutcome.PREPARED
        assert dispatch.acquire_send_right(p, now=_WALL).outcome is DispatchOutcome.SENDING
        planned = ops.read_by_key(p.operation_key).remote_user_id
        db.close()  # 模拟崩溃
        db2 = Database(db.path)
        db2.open()
        try:
            fake = T22Transport()
            dispatch2 = DispatchService(db2, clock=FakeClock(wall=_WALL))
            result = dispatch2.dispatch_once(p, transport=fake, now=_WALL)
            assert result.outcome is DispatchOutcome.ALREADY_EXISTS
            assert result.state == "UNKNOWN"
            assert fake.send_calls == 0
            record = dispatch2._ops.read_by_key(p.operation_key)
            assert record.state == "UNKNOWN"
            assert record.remote_user_id == planned  # 重启恢复绝不生成新 id
        finally:
            db2.close()

    def test_15_returned_mismatched_id_unknown(self, dispatch_env):
        db, _, _, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        fake = T22Transport(T22Mode.MISMATCH)
        dispatch = DispatchService(db, clock=FakeClock(wall=_WALL))
        result = dispatch.dispatch_once(p, transport=fake, now=_WALL)
        assert result.outcome is DispatchOutcome.UNKNOWN
        record = dispatch._ops.read_by_key(p.operation_key)
        assert record.state == "UNKNOWN"
        assert json.loads(record.evidence_json).get("remote_identity_mismatch") is True

    def test_16_mismatch_does_not_overwrite_planned(self, dispatch_env):
        db, _, _, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        fake = T22Transport(T22Mode.MISMATCH)
        dispatch = DispatchService(db, clock=FakeClock(wall=_WALL))
        assert dispatch.dispatch_once(p, transport=fake, now=_WALL).outcome is \
            DispatchOutcome.UNKNOWN
        record = dispatch._ops.read_by_key(p.operation_key)
        assert fake.sent_planned == [record.remote_user_id]
        assert record.remote_user_id == fake.sent_planned[0]
        assert record.remote_user_id != _MISMATCH_ID  # transport 返回值绝不覆盖 planned