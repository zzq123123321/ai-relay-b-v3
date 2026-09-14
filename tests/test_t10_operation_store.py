"""T10：Operation 账本与一次发送资格——Store 层（主规格 03/04.3/07.2）。

直接覆盖（与 149 项验收呼应）：
- PREPARED 创建；operation_key 幂等（EXISTING）；同 key 异身份 → CONFLICT 且原操作不变；
- PREPARED→SENDING CAS；SENDING→ACCEPTED/REJECTED/UNKNOWN；终态禁止回 SENDING；
- task/attempt/owner 三层权威 CAS（epoch / control_revision / lease state）；
- 同一 endpoint/session 未决发送阻塞第二个（结构化结果，非裸 IntegrityError）；
- SENDING 崩溃恢复：sweep_stale_sendings → UNKNOWN；
- 各写点故障注入：不产生非法半状态。
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from core.dispatch import DispatchProposal, derive_operation_id, sha256_hex
from core.protocol_v1 import ProtocolFormat, content_digest, parse_message
from core.scheduler import ScheduleOutcome, Scheduler
from infra.clock import FakeClock
from storage.database import Database
from storage.operation_store import (
    AcquireOutcome,
    FinalizeOutcome,
    OperationStore,
    PrepareOutcome,
)
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
# T22-02A：planned remote message identity——PREPARED 落库时已 durable，finalize 永不覆盖。
_PLANNED_REMOTE_USER_ID = "msg_t10_planned_001"


@pytest.fixture
def store_env(tmp_path):
    db = Database(tmp_path / "t10.sqlite")
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


def _start(store_env, task_id: str = "t10-001", project_key: str = "p-fixed",
           session_id: str = "sess-fixed") -> tuple[str, str, str, int]:
    db, tasks, _, _, scheduler, attempts = store_env
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
    attempt_id = result.attempt_id
    assert attempt_id is not None
    return task_key, attempt_id, project_key, 1


def _make_receive(project_key: str, session_id: str):
    from core.domain import SessionBindingMode, TargetExecutor
    from core.domain import ReceiveSettingsSnapshot

    return ReceiveSettingsSnapshot(
        config_revision=5, committed_at=_T0, received_at=_T0,
        effective_executor=TargetExecutor.OPENCHAMBER, directory=r"D:\AIwork\proj",
        project_key=project_key, agent="build", requested_model="",
        binding_mode=SessionBindingMode.FIXED_SESSION, frozen_session_id=session_id,
    )


def _proposal(task_key: str, attempt_id: str, *,
              operation_key: str | None = None, kind: str = "INITIAL_SEND",
              epoch: int = 1, control_revision: int = 0,
              endpoint: str = _ENDPOINT, session_id: str = "sess-fixed",
              project_key: str = "p-fixed", interruption_id: str | None = None) -> DispatchProposal:
    return DispatchProposal(
        operation_key=operation_key or f"continue:{task_key}:interrupt-7",
        kind=kind, task_key=task_key, attempt_id=attempt_id,
        authority_epoch=epoch, control_revision=control_revision,
        endpoint=endpoint, session_id=session_id, project_key=project_key,
        interruption_id=interruption_id, prompt_body="继续任务的报告正文",
    )


def _count(db, table: str, where: str = "", params: tuple = ()) -> int:
    return int(
        db.connection.execute(f"SELECT COUNT(*) FROM {table} {where}", params).fetchone()[0]
    )


def _op_state(db, operation_key: str) -> str:
    row = db.connection.execute(
        "SELECT state FROM operations WHERE operation_key=?", (operation_key,)
    ).fetchone()
    return row[0] if row is not None else None


def _prepare(ops: OperationStore, conn, proposal, *, now: str = _T0):
    return ops.prepare_in(
        conn,
        proposal=proposal,
        operation_id=derive_operation_id(proposal.operation_key),
        pre_snapshot_json='{"message_ids": ["m1", "m2"]}',
        prompt_text=sha256_hex(proposal.prompt_body),
        prompt_hash=sha256_hex(proposal.prompt_body),
        remote_user_id=_PLANNED_REMOTE_USER_ID,
        created_at=now,
    )


class TestPrepare:
    def test_created_and_persisted(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, epoch = _start(store_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        with db.transaction():
            result = _prepare(ops, db.connection, p)
        assert result.outcome is PrepareOutcome.CREATED
        record = ops.read_by_key(p.operation_key)
        assert record is not None
        assert record.state == "PREPARED"
        assert record.remote_user_id == _PLANNED_REMOTE_USER_ID
        assert record.operation_id == derive_operation_id(p.operation_key)
        assert record.kind == "INITIAL_SEND"
        assert record.task_key == task_key
        assert record.attempt_id == attempt_id
        assert record.authority_epoch == 1
        assert record.control_revision == 0
        assert record.endpoint == _ENDPOINT
        assert record.session_id == "sess-fixed"
        assert record.project_key == project
        assert record.prompt_hash == sha256_hex(p.prompt_body)
        assert json.loads(record.pre_snapshot_json) == {"message_ids": ["m1", "m2"]}
        assert ops.read_by_id(record.operation_id) is not None

    def test_same_key_same_identity_existing(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        with db.transaction():
            first = _prepare(ops, db.connection, p)
            second = _prepare(ops, db.connection, p)
        assert first.outcome is PrepareOutcome.CREATED
        assert second.outcome is PrepareOutcome.EXISTING
        assert _count(db, "operations") == 1

    def test_same_key_different_identity_conflict(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        proposal_a = _proposal(task_key, attempt_id, project_key=project)
        proposal_b = _proposal(
            task_key, "attempt-alien", project_key=project,
            operation_key=proposal_a.operation_key,
        )
        with db.transaction():
            first = _prepare(ops, db.connection, proposal_a)
            second = _prepare(ops, db.connection, proposal_b)
        assert first.outcome is PrepareOutcome.CREATED
        assert second.outcome is PrepareOutcome.CONFLICT
        assert _count(db, "operations") == 1
        record = ops.read_by_key(proposal_a.operation_key)
        assert record is not None
        assert record.attempt_id == attempt_id  # 原 operation 未被修改
        assert record.state == "PREPARED"

    def test_identity_tuple_matches_proposal(self, store_env):
        task_key, attempt_id, project, _ = _start(store_env)
        p = _proposal(task_key, attempt_id, project_key=project,
                      interruption_id="interrupt-7")
        assert p.identity_tuple() == (
            "INITIAL_SEND", task_key, attempt_id, 1, 0, _ENDPOINT,
            "sess-fixed", project, "interrupt-7",
        )


class TestAuthority:
    def test_authority_ok(self, store_env):
        _, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        status = ops.check_authority_in(store_env[0].connection, p)
        assert status.ok

    def test_task_non_active_fails(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        db.connection.execute(
            "UPDATE tasks SET state='BLOCKED' WHERE task_key=?", (task_key,)
        )
        p = _proposal(task_key, attempt_id, project_key=project)
        assert not ops.check_authority_in(store_env[0].connection, p).ok

    def test_attempt_mismatch_fails(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        p_wrong_attempt = _proposal(task_key, "attempt-another", project_key=project,
                                    operation_key=p.operation_key)
        assert not ops.check_authority_in(store_env[0].connection, p_wrong_attempt).ok

    def test_epoch_mismatch_fails(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        p = _proposal(task_key, attempt_id, project_key=project, epoch=2)
        assert not ops.check_authority_in(store_env[0].connection, p).ok

    def test_control_revision_mismatch_fails(self, store_env):
        _, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        p = _proposal(task_key, attempt_id, project_key=project, control_revision=9)
        assert not ops.check_authority_in(store_env[0].connection, p).ok

    def test_quarantined_owner_blocks_send(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        db.connection.execute(
            "UPDATE project_leases SET state='QUARANTINED' WHERE project_key=?", (project,)
        )
        p = _proposal(task_key, attempt_id, project_key=project)
        status = ops.check_authority_in(store_env[0].connection, p)
        assert not status.ok
        assert "lease state" in status.detail

    def test_rotating_owner_blocks_send(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        db.connection.execute(
            "UPDATE project_leases SET state='ROTATING' WHERE project_key=?", (project,)
        )
        p = _proposal(task_key, attempt_id, project_key=project)
        assert not ops.check_authority_in(store_env[0].connection, p).ok

    def test_lease_owner_mismatch_fails(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        db.connection.execute(
            "INSERT INTO attempts (attempt_id, task_key, kind, state, authority_epoch,"
            " execution_snapshot_json, control_revision, started_at)"
            " VALUES ('attempt-alien', ?, 'MANUAL_CONTINUE', 'COMPLETED', 2, '{}', 0, ?)",
            (task_key, _T0),
        )
        db.connection.execute(
            "UPDATE project_leases SET owner_attempt_id=? WHERE project_key=?",
            ("attempt-alien", project),
        )
        p = _proposal(task_key, attempt_id, project_key=project)
        assert not ops.check_authority_in(store_env[0].connection, p).ok


class TestTransitions:
    def test_prepared_to_sending_granted(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        with db.transaction():
            _prepare(ops, db.connection, p)
        with db.transaction():
            acquired = ops.acquire_send_right_in(
                db.connection, proposal=p,
                operation_id=derive_operation_id(p.operation_key), now=_T0,
            )
        assert acquired.outcome is AcquireOutcome.GRANTED
        assert _op_state(db, p.operation_key) == "SENDING"

    def test_sending_to_accepted(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        with db.transaction():
            _prepare(ops, db.connection, p)
        with db.transaction():
            ops.acquire_send_right_in(db.connection, proposal=p,
                                      operation_id=derive_operation_id(p.operation_key),
                                      now=_T0)
        with db.transaction():
            final = ops.finalize_in(
                db.connection, proposal=p,
                operation_id=derive_operation_id(p.operation_key),
                target_state="ACCEPTED",
                remote_user_id="msg_transport_observed_other",
                evidence_json='{"message_id": "msg-1"}', finalized_at=_T0, now=_T0,
            )
        assert final.outcome is FinalizeOutcome.FINALIZED
        record = ops.read_by_key(p.operation_key)
        assert record is not None
        assert record.state == "ACCEPTED"
        assert record.remote_user_id == _PLANNED_REMOTE_USER_ID  # finalize 不覆盖 planned
        assert json.loads(record.evidence_json) == {"message_id": "msg-1"}
        assert record.finalized_at == _T0

    def test_sending_to_rejected(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        with db.transaction():
            _prepare(ops, db.connection, p)
        with db.transaction():
            ops.acquire_send_right_in(db.connection, proposal=p,
                                      operation_id=derive_operation_id(p.operation_key),
                                      now=_T0)
        with db.transaction():
            final = ops.finalize_in(
                db.connection, proposal=p,
                operation_id=derive_operation_id(p.operation_key),
                target_state="REJECTED", remote_user_id=None,
                evidence_json='{"promptDispatched": false}', finalized_at=_T0, now=_T0,
            )
        assert final.outcome is FinalizeOutcome.FINALIZED
        assert _op_state(db, p.operation_key) == "REJECTED"
        record = ops.read_by_key(p.operation_key)
        assert record.remote_user_id == _PLANNED_REMOTE_USER_ID  # REJECTED 保留 planned

    def test_sending_to_unknown(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        with db.transaction():
            _prepare(ops, db.connection, p)
        with db.transaction():
            ops.acquire_send_right_in(db.connection, proposal=p,
                                      operation_id=derive_operation_id(p.operation_key),
                                      now=_T0)
        with db.transaction():
            final = ops.finalize_in(
                db.connection, proposal=p,
                operation_id=derive_operation_id(p.operation_key),
                target_state="UNKNOWN", remote_user_id=None,
                evidence_json='{"timeout": true}', finalized_at=_T0, now=_T0,
            )
        assert final.outcome is FinalizeOutcome.FINALIZED
        assert _op_state(db, p.operation_key) == "UNKNOWN"
        record = ops.read_by_key(p.operation_key)
        assert record.remote_user_id == _PLANNED_REMOTE_USER_ID  # UNKNOWN 保留 planned
        assert record.finalized_at == _T0

    def test_unknown_reconciled_to_accepted(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        with db.transaction():
            _prepare(ops, db.connection, p)
        with db.transaction():
            ops.acquire_send_right_in(db.connection, proposal=p,
                                      operation_id=derive_operation_id(p.operation_key),
                                      now=_T0)
        with db.transaction():
            ops.finalize_in(db.connection, proposal=p,
                            operation_id=derive_operation_id(p.operation_key),
                            target_state="UNKNOWN", remote_user_id=None,
                            evidence_json="{}", finalized_at=_T0, now=_T0)
        with db.transaction():
            final = ops.finalize_in(
                db.connection, proposal=p,
                operation_id=derive_operation_id(p.operation_key),
                target_state="ACCEPTED", remote_user_id="user-alice-001",
                evidence_json='{"message_id": "msg-9"}', finalized_at=_T0, now=_T0,
            )
        assert final.outcome is FinalizeOutcome.FINALIZED
        assert _op_state(db, p.operation_key) == "ACCEPTED"

    def test_terminal_cannot_return_to_sending(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        with db.transaction():
            _prepare(ops, db.connection, p)
        with db.transaction():
            ops.acquire_send_right_in(db.connection, proposal=p,
                                      operation_id=derive_operation_id(p.operation_key),
                                      now=_T0)
        with db.transaction():
            ops.finalize_in(db.connection, proposal=p,
                            operation_id=derive_operation_id(p.operation_key),
                            target_state="UNKNOWN", remote_user_id=None,
                            evidence_json="{}", finalized_at=_T0, now=_T0)
        with db.transaction():
            bad = ops.finalize_in(
                db.connection, proposal=p,
                operation_id=derive_operation_id(p.operation_key),
                target_state="SENDING", remote_user_id=None,
                evidence_json="{}", finalized_at=_T0, now=_T0,
            )
        assert bad.outcome is FinalizeOutcome.INVALID_TRANSITION
        assert _op_state(db, p.operation_key) == "UNKNOWN"
        with db.transaction():
            acquire = ops.acquire_send_right_in(
                db.connection, proposal=p,
                operation_id=derive_operation_id(p.operation_key), now=_T0,
            )
        assert acquire.outcome is AcquireOutcome.NOT_PREPARED
        assert _op_state(db, p.operation_key) == "UNKNOWN"

    def test_accepted_cannot_return_to_sending(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        with db.transaction():
            _prepare(ops, db.connection, p)
        with db.transaction():
            ops.acquire_send_right_in(db.connection, proposal=p,
                                      operation_id=derive_operation_id(p.operation_key),
                                      now=_T0)
        with db.transaction():
            ops.finalize_in(db.connection, proposal=p,
                            operation_id=derive_operation_id(p.operation_key),
                            target_state="ACCEPTED", remote_user_id="u",
                            evidence_json="{}", finalized_at=_T0, now=_T0)
        with db.transaction():
            bad = ops.finalize_in(
                db.connection, proposal=p,
                operation_id=derive_operation_id(p.operation_key),
                target_state="SENDING", remote_user_id=None,
                evidence_json="{}", finalized_at=_T0, now=_T0,
            )
        assert bad.outcome is FinalizeOutcome.INVALID_TRANSITION
        assert _op_state(db, p.operation_key) == "ACCEPTED"


class TestUnresolvedSessionGuard:
    def test_second_pending_send_blocked_structured(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        pa = _proposal(task_key, attempt_id, project_key=project,
                       operation_key="continue:a:interrupt-7")
        pb = _proposal(task_key, attempt_id, project_key=project,
                       operation_key="continue:b:interrupt-7")
        with db.transaction():
            _prepare(ops, db.connection, pa)
        with db.transaction():
            assert ops.acquire_send_right_in(
                db.connection, proposal=pa,
                operation_id=derive_operation_id(pa.operation_key), now=_T0,
            ).outcome is AcquireOutcome.GRANTED
        with db.transaction():
            _prepare(ops, db.connection, pb)
        with db.transaction():
            blocked = ops.acquire_send_right_in(
                db.connection, proposal=pb,
                operation_id=derive_operation_id(pb.operation_key), now=_T0,
            )
        assert blocked.outcome is AcquireOutcome.SESSION_HAS_UNRESOLVED
        assert _op_state(db, pb.operation_key) == "PREPARED"

    def test_resolved_then_next_can_acquire(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        pa = _proposal(task_key, attempt_id, project_key=project,
                       operation_key="continue:a:interrupt-7")
        pb = _proposal(task_key, attempt_id, project_key=project,
                       operation_key="continue:b:interrupt-7")
        with db.transaction():
            _prepare(ops, db.connection, pa)
        with db.transaction():
            ops.acquire_send_right_in(db.connection, proposal=pa,
                                      operation_id=derive_operation_id(pa.operation_key),
                                      now=_T0)
        with db.transaction():
            ops.finalize_in(db.connection, proposal=pa,
                            operation_id=derive_operation_id(pa.operation_key),
                            target_state="ACCEPTED", remote_user_id="u",
                            evidence_json="{}", finalized_at=_T0, now=_T0)
        with db.transaction():
            _prepare(ops, db.connection, pb)
        with db.transaction():
            granted = ops.acquire_send_right_in(
                db.connection, proposal=pb,
                operation_id=derive_operation_id(pb.operation_key), now=_T0,
            )
        assert granted.outcome is AcquireOutcome.GRANTED


class TestRecovery:
    def test_sweep_stale_sendings_to_unknown(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        results = []
        for i, (session, key) in enumerate(
            [("sess-a", "continue:a:interrupt-7"), ("sess-b", "continue:b:interrupt-7")]
        ):
            p = _proposal(task_key, attempt_id, project_key=project,
                          session_id=session, operation_key=key)
            with db.transaction():
                _prepare(ops, db.connection, p)
            with db.transaction():
                r = ops.acquire_send_right_in(
                    db.connection, proposal=p,
                    operation_id=derive_operation_id(p.operation_key), now=_T0,
                )
                results.append((r.outcome is AcquireOutcome.GRANTED, key))
        assert all(ok for ok, _ in results)
        recovered = ops.sweep_stale_sendings(now=_T0)
        assert sorted(recovered) == sorted(k for _, k in results)
        for _, key in results:
            record = ops.read_by_key(key)
            assert record is not None
            assert record.state == "UNKNOWN"
            assert record.finalized_at == _T0
            assert record.evidence_json != "{}"

    def test_recover_sending_as_unknown_specific(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        with db.transaction():
            _prepare(ops, db.connection, p)
        with db.transaction():
            ops.acquire_send_right_in(db.connection, proposal=p,
                                      operation_id=derive_operation_id(p.operation_key),
                                      now=_T0)
        with db.transaction():
            changed = ops.recover_sending_as_unknown_in(
                db.connection, operation_key=p.operation_key,
                operation_id=derive_operation_id(p.operation_key), now=_T0,
            )
        assert changed
        assert _op_state(db, p.operation_key) == "UNKNOWN"


class TestFaultInjection:
    def test_prepared_insert_fault_rolls_back(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        ops.fault_inject_after = 1
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction():
                _prepare(ops, db.connection, p)
        assert _count(db, "operations") == 0

    def test_sending_cas_fault_rolls_back(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        with db.transaction():
            _prepare(ops, db.connection, p)
        ops.fault_inject_after = 1
        ops._fault_step = 0
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction():
                ops.acquire_send_right_in(
                    db.connection, proposal=p,
                    operation_id=derive_operation_id(p.operation_key), now=_T0,
                )
        assert _op_state(db, p.operation_key) == "PREPARED"

    def test_finalize_fault_rolls_back(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        with db.transaction():
            _prepare(ops, db.connection, p)
        with db.transaction():
            ops.acquire_send_right_in(db.connection, proposal=p,
                                      operation_id=derive_operation_id(p.operation_key),
                                      now=_T0)
        ops.fault_inject_after = 1
        ops._fault_step = 0
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction():
                ops.finalize_in(
                    db.connection, proposal=p,
                    operation_id=derive_operation_id(p.operation_key),
                    target_state="UNKNOWN", remote_user_id=None,
                    evidence_json="{}", finalized_at=_T0, now=_T0,
                )
        assert _op_state(db, p.operation_key) == "SENDING"
        with db.transaction():
            ok = ops.finalize_in(
                db.connection, proposal=p,
                operation_id=derive_operation_id(p.operation_key),
                target_state="UNKNOWN", remote_user_id=None,
                evidence_json="{}", finalized_at=_T0, now=_T0,
            )
        assert ok.outcome is FinalizeOutcome.FINALIZED
        assert _op_state(db, p.operation_key) == "UNKNOWN"


def test_hash_stable_for_same_prompt():
    assert sha256_hex("相同正文") == sha256_hex("相同正文")
    assert sha256_hex("相同正文") != sha256_hex("不同正文")