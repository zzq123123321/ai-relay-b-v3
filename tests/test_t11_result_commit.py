"""T11：权威结果原子提交与一次权威（S04/S05/S06 + 任务卡边界）。

覆盖：
- S04：对权威提交事务的每一条 SQL 故障注入 → 整事务回滚、零半状态；
- S05：同 attempt 双 worker 完成竞争（顺序 + 真实现两线程）→ 唯一权威结果与唯一 Outbox；
- S06：人工包装成为权威后，旧 worker 晚到 → 不能覆盖、重新复制/重启读取一致；
- 旧 attempt / 旧 epoch 失权、同结果幂等、同 result_id 异正文冲突、消息认领防重、
  task/attempt 终态与当前权威正文绑定、全局 OPEN 槽释放。
"""

from __future__ import annotations

import itertools
import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from core.protocol_v1 import ProtocolFormat, content_digest, parse_message
from core.result_commit import (
    CandidateResult,
    CommitOutcome,
    ResultClaim,
    ResultCommitService,
)
from core.scheduler import ScheduleOutcome, Scheduler
from infra.clock import FakeClock
from storage.database import Database
from storage.result_store import ResultStore
from storage.task_store import TaskStore, make_task_key

_V1 = (
    "AI_RELAY/1\n"
    "MESSAGE_ID: work-001\n"
    "SOURCE: CHATGPT\n"
    "TARGET: OPENCHAMBER\n"
    "TYPE: TASK\n"
    "\n"
    "{body}"
)
_T0 = "2026-10-01T09:00:00+00:00"
_WALL = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)
_ENDPOINT = "http://127.0.0.1:57123"


def _make_receive(project_key: str, session_id: str):
    from core.domain import ReceiveSettingsSnapshot, SessionBindingMode, TargetExecutor

    return ReceiveSettingsSnapshot(
        config_revision=5, committed_at=_T0, received_at=_T0,
        effective_executor=TargetExecutor.OPENCHAMBER, directory=r"D:\AIwork\proj",
        project_key=project_key, agent="build", requested_model="",
        binding_mode=SessionBindingMode.FIXED_SESSION, frozen_session_id=session_id,
    )


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t11.sqlite")
    db.open()
    tasks = TaskStore(db)
    ops = ResultStore(db)
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


def _make_service(db, ops, *, fault_inject_after: int | None = None,
                  result_id: str | None = None, epoch: int = 1,
                  clock=FakeClock(wall=_WALL)):
    if fault_inject_after is not None:
        ops._fault_step = 0
        ops.fault_inject_after = fault_inject_after
    return ResultCommitService(
        db, result_store=ops, clock=clock,
        result_id_factory=(lambda t, a, e: result_id) if result_id
        else (lambda t, a, e: f"res-{a}"),
        delivery_id_factory=lambda t, r: f"deliv-{r}",
        event_id_factory=lambda r: f"evt-{r}",
    )


def _start(env, task_id: str = "t11-001", project_key: str = "p-fixed",
           session_id: str = "sess-fixed") -> tuple[str, str, str, int]:
    db, tasks, _, _, scheduler, _ = env
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


def _candidate(task_key, attempt_id, *, result_id=None, epoch=1, state="COMPLETED",
               source="AUTO_RELAY", body="完成报告正文", message_id=None):
    claim = None
    if message_id is not None:
        claim = ResultClaim(endpoint=_ENDPOINT, session_id="sess-fixed",
                            message_id=message_id)
    return CandidateResult(
        task_key=task_key, attempt_id=attempt_id, authority_epoch=epoch,
        result_state=state, source=source, final_body=body,
        result_id=result_id or f"res-{attempt_id}", claim=claim,
    )


def _count(db, table: str, where: str = "", params: tuple = ()) -> int:
    return int(db.connection.execute(
        f"SELECT COUNT(*) FROM {table} {where}", params).fetchone()[0])


def _task_row(db, task_key: str):
    return db.connection.execute(
        "SELECT state, active_attempt_id, authority_epoch, current_result_revision"
        " FROM tasks WHERE task_key=?", (task_key,)).fetchone()


class TestAtomicCommit:
    def test_commit_completed_single_transaction(self, env):
        db, _, ops, _, _, _ = env
        task_key, attempt_id, project, _ = _start(env)
        service = _make_service(db, ops, result_id="res-complete")
        result = service.commit(_candidate(task_key, attempt_id, result_id="res-complete",
                                           message_id="msg-1"))
        assert result.outcome is CommitOutcome.COMMITTED
        assert result.revision == 1
        state, active_attempt, epoch, current_rev = _task_row(db, task_key)
        assert state == "COMPLETED"
        assert active_attempt is None
        assert current_rev == 1
        attempt = db.connection.execute(
            "SELECT state, ended_at FROM attempts WHERE attempt_id=?",
            (attempt_id,)).fetchone()
        assert attempt[0] == "COMPLETED"
        assert attempt[1] == _T0
        assert _count(db, "outbox") == 1
        assert _count(db, "remote_claims") == 1
        assert _count(db, "events", "WHERE event_code='RESULT_COMMITTED'") == 1
        outbox = db.connection.execute(
            "SELECT result_id, state, profile FROM outbox").fetchone()
        assert outbox[0] == "res-complete"
        assert outbox[1] == "PENDING"
        assert outbox[2] == "legacy_v1"

    def test_task_track_and_outbox_reference_same_authoritative_body(self, env):
        db, _, ops, _, _, _ = env
        task_key, attempt_id, project, _ = _start(env)
        service = _make_service(db, ops)
        service.commit(_candidate(task_key, attempt_id, message_id="msg-1"))
        authoritative = ops.get_authoritative_for_task(task_key)
        assert authoritative is not None
        outbox = db.connection.execute(
            "SELECT o.result_id, o.peer_id FROM outbox o").fetchone()
        assert outbox[0] == authoritative.result_id
        result = ops.get_result_by_id(authoritative.result_id)
        assert result is not None
        assert result.protocol_text == authoritative.protocol_text
        assert result.sha256 == authoritative.sha256  # 交付读取的正是这一版

    def test_failed_maps_attempt_failed(self, env):
        db, _, ops, _, _, _ = env
        task_key, attempt_id, project, _ = _start(env)
        service = _make_service(db, ops)
        result = service.commit(
            _candidate(task_key, attempt_id, state="FAILED", body="失败说明",
                       message_id="msg-f"))
        assert result.outcome is CommitOutcome.COMMITTED
        assert _task_row(db, task_key)[0] == "FAILED"
        assert db.connection.execute(
            "SELECT state FROM attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()[0] == "FAILED"

    def test_stopped_by_user_maps_attempt_stopped_and_release_slot(self, env):
        db, _, ops, _, _, _ = env
        task_key, attempt_id, project, _ = _start(env)
        service = _make_service(db, ops)
        result = service.commit(
            _candidate(task_key, attempt_id, state="STOPPED_BY_USER",
                       body="人工停止", message_id="msg-s"))
        assert result.outcome is CommitOutcome.COMMITTED
        assert _task_row(db, task_key)[0] == "STOPPED_BY_USER"
        assert db.connection.execute(
            "SELECT state FROM attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()[0] == "STOPPED"
        assert _count(db, "attempts", "WHERE state='OPEN'") == 0

    def test_global_open_slot_released(self, env):
        db, _, ops, _, _, _ = env
        task_key, attempt_id, project, _ = _start(env)
        assert _count(db, "attempts", "WHERE state='OPEN'") == 1
        service = _make_service(db, ops)
        service.commit(_candidate(task_key, attempt_id, message_id="msg-1"))
        assert _count(db, "attempts", "WHERE state='OPEN'") == 0


class TestS05:
    def test_s05_two_workers_same_attempt_one_wins(self, env):
        db, _, ops, _, _, _ = env
        task_key, attempt_id, project, _ = _start(env)
        service = _make_service(db, ops)
        first = service.commit(_candidate(task_key, attempt_id, result_id="res-A",
                                          body="正文A", message_id="msg-A"))
        assert first.outcome is CommitOutcome.COMMITTED
        second = service.commit(_candidate(task_key, attempt_id, result_id="res-B",
                                           body="正文B", message_id="msg-B"))
        assert second.outcome is CommitOutcome.LOST_AUTHORITY
        assert _count(db, "results") == 1
        assert _count(db, "outbox") == 1
        assert _count(db, "events", "WHERE event_code='RESULT_COMMITTED'") == 1
        assert ops.get_authoritative_for_task(task_key).result_id == "res-A"

    def test_s05_concurrent_threads_single_authoritative(self, env, tmp_path):
        db, _, _, _, _, _ = env
        task_key, attempt_id, project, _ = _start(env)
        path = db.path
        barrier = threading.Barrier(2)
        outcomes: list = []
        lock = threading.Lock()

        def worker(name: str, body: str, message_id: str) -> None:
            worker_db = Database(path)
            worker_db.open()
            try:
                worker_ops = ResultStore(worker_db)
                worker_service = _make_service(worker_db, worker_ops)
                candidate = _candidate(task_key, attempt_id, result_id=f"res-{name}",
                                       body=body, message_id=message_id)
                barrier.wait()
                result = worker_service.commit(candidate)
                with lock:
                    outcomes.append(result.outcome)
            finally:
                worker_db.close()

        threads = [
            threading.Thread(target=worker, args=("A", "正文A", "msg-A")),
            threading.Thread(target=worker, args=("B", "正文B", "msg-B")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert set(outcomes) == {CommitOutcome.COMMITTED, CommitOutcome.LOST_AUTHORITY}
        assert _count(db, "results") == 1
        assert _count(db, "outbox") == 1
        assert _count(db, "events", "WHERE event_code='RESULT_COMMITTED'") == 1
        state, _, _, current_rev = _task_row(db, task_key)
        assert state == "COMPLETED"
        assert current_rev == 1


class TestS06OldWorkerLate:
    def test_s06_manual_result_is_authority_and_old_worker_cannot_override(self, env):
        from core.result_commit import build_result_response, sha256_hex

        db, _, ops, _, _, _ = env
        task_key, attempt_id, project, _ = _start(env)
        service = _make_service(db, ops)
        manual = service.commit(_candidate(task_key, attempt_id, result_id="res-manual",
                                           source="MANUAL_WRAP", body="人工权威正文",
                                           message_id="msg-manual"))
        assert manual.outcome is CommitOutcome.COMMITTED
        late = service.commit(_candidate(task_key, attempt_id, result_id="res-old",
                                         source="AUTO_RELAY", body="旧worker正文",
                                         message_id="msg-old"))
        assert late.outcome is CommitOutcome.LOST_AUTHORITY
        authoritative = ops.get_authoritative_for_task(task_key)
        assert authoritative is not None
        assert authoritative.result_id == "res-manual"
        assert authoritative.final_body == "人工权威正文"
        assert _count(db, "results") == 1
        assert _count(db, "outbox") == 1
        # 重新复制（读 protocol_text）与重启后读取完全一致
        expected = build_result_response(
            result_state="COMPLETED", attempt_id=attempt_id, revision=1,
            source="MANUAL_WRAP", remote_state="IDLE_VERIFIED",
            final_body="人工权威正文", in_reply_to="t11-001",
            response_message_id="res-manual",
        )
        assert authoritative.protocol_text == expected
        assert authoritative.sha256 == sha256_hex(expected)
        db.close()
        restarted = Database(db.path)
        restarted.open()
        try:
            restarted_ops = ResultStore(restarted)
            revived = restarted_ops.get_authoritative_for_task(task_key)
            assert revived is not None
            assert revived.protocol_text == expected
            assert revived.result_id == "res-manual"
        finally:
            restarted.close()


class TestAuthorityAndIdempotency:
    def test_stale_attempt_rejected(self, env):
        db, _, ops, _, _, _ = env
        task_key, attempt_id, project, _ = _start(env)
        db.connection.execute(
            "UPDATE tasks SET active_attempt_id='attempt-alien' WHERE task_key=?",
            (task_key,))
        service = _make_service(db, ops)
        result = service.commit(_candidate(task_key, attempt_id, message_id="msg-1"))
        assert result.outcome is CommitOutcome.LOST_AUTHORITY
        assert _count(db, "results") == 0
        assert _count(db, "outbox") == 0
        assert _count(db, "events", "WHERE event_code='RESULT_COMMITTED'") == 0

    def test_old_epoch_rejected(self, env):
        db, _, ops, _, _, _ = env
        task_key, attempt_id, project, _ = _start(env)
        db.connection.execute(
            "UPDATE tasks SET authority_epoch=5 WHERE task_key=?", (task_key,))
        service = _make_service(db, ops)
        result = service.commit(_candidate(task_key, attempt_id, epoch=1,
                                           message_id="msg-1"))
        assert result.outcome is CommitOutcome.LOST_AUTHORITY
        assert _count(db, "results") == 0

    def test_same_result_repeat_is_idempotent(self, env):
        db, _, ops, _, _, _ = env
        task_key, attempt_id, project, _ = _start(env)
        service = _make_service(db, ops)
        first = service.commit(_candidate(task_key, attempt_id, result_id="res-1",
                                          body="正文", message_id="msg-1"))
        assert first.outcome is CommitOutcome.COMMITTED
        second = service.commit(_candidate(task_key, attempt_id, result_id="res-1",
                                           body="正文", message_id="msg-1"))
        assert second.outcome is CommitOutcome.ALREADY_COMMITTED
        assert _count(db, "results") == 1
        assert _count(db, "outbox") == 1
        assert _count(db, "events", "WHERE event_code='RESULT_COMMITTED'") == 1

    def test_same_result_id_different_body_conflict(self, env):
        db, _, ops, _, _, _ = env
        task_key, attempt_id, project, _ = _start(env)
        service = _make_service(db, ops)
        first = service.commit(_candidate(task_key, attempt_id, result_id="res-1",
                                          body="正文A", message_id="msg-A"))
        assert first.outcome is CommitOutcome.COMMITTED
        second = service.commit(_candidate(task_key, attempt_id, result_id="res-1",
                                           body="正文B", message_id="msg-B"))
        assert second.outcome is CommitOutcome.RESULT_ID_CONFLICT
        assert ops.get_authoritative_for_task(task_key).final_body == "正文A"

    def test_claim_already_bound_blocks_second_authority(self, env):
        db, _, ops, _, _, _ = env
        task_key, attempt_id, project, _ = _start(env)
        with db.transaction():
            ops.insert_result_in(
                db.connection, result_id="res-other", task_key=task_key,
                attempt_id=attempt_id, revision=99, state="COMPLETED",
                source="AUTO_RELAY", final_body="占位",
                protocol_text="AI_RELAY/1\n\n占位", sha256="h",
                remote_message_ids=[], committed_at=_T0,
            )
            ops.insert_claim_in(
                db.connection, endpoint=_ENDPOINT, session_id="sess-fixed",
                message_id="msg-1", task_key=task_key, result_id="res-other",
            )
        service = _make_service(db, ops)
        result = service.commit(_candidate(task_key, attempt_id, message_id="msg-1"))
        assert result.outcome is CommitOutcome.LOST_AUTHORITY
        # 只有占位版本存在；候选结果绝不能新落库，也没产生提交事件与 Outbox
        assert _count(db, "results") == 1
        assert _count(db, "remote_claims") == 1
        assert _count(db, "events", "WHERE event_code='RESULT_COMMITTED'") == 0
        assert _count(db, "outbox") == 0

    def test_empty_candidate_rejected(self, env):
        _, _, ops, _, _, _ = env
        task_key, attempt_id, project, _ = _start(env)
        service = _make_service(env[0], ops)
        with pytest.raises(ValueError):
            service.commit(_candidate(task_key, attempt_id, body="   "))


class TestS04FaultInjection:
    @pytest.mark.parametrize("checkpoint", [1, 2, 3, 4, 5, 6])
    def test_s04_each_sql_failure_rolls_back_all(self, env, checkpoint):
        db, _, ops, _, _, _ = env
        task_key, attempt_id, project, _ = _start(env)
        service = _make_service(db, ops, fault_inject_after=checkpoint)
        with pytest.raises(sqlite3.IntegrityError):
            service.commit(_candidate(task_key, attempt_id, message_id="msg-1"))
        assert _count(db, "results") == 0                    # 没有结果半条
        state, active_attempt, epoch, current_rev = _task_row(db, task_key)
        assert state == "ACTIVE" and current_rev == 0        # task 未错误终态
        assert db.connection.execute(
            "SELECT state FROM attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()[0] == "OPEN"                            # attempt 未错误终态
        assert _count(db, "outbox") == 0                     # outbox 不存在
        assert _count(db, "remote_claims") == 0              # claim 不存在
        assert _count(db, "events", "WHERE event_code='RESULT_COMMITTED'") == 0