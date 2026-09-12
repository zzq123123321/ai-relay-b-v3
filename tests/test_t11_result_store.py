"""T11：权威 ResultStore 原语（不可变版本 / revision / Outbox / claim / 权威指针）。

任务卡验收关联：S04（逐 SQL 故障由 result_commit 测试覆盖，本文件覆盖不可变与
唯一性原语）、S09 的文件安全由 delivery 测试覆盖。本文件专注 store 层：不可变
trigger、revision 单调、authoritative 指针读回、outbox/claim 唯一性、delivery 标记。
"""

from __future__ import annotations

import itertools
import sqlite3
from datetime import datetime, timezone

import pytest

from core.protocol_v1 import ProtocolFormat, content_digest, parse_message
from core.scheduler import ScheduleOutcome, Scheduler
from infra.clock import FakeClock
from storage.database import Database
from storage.result_store import ResultStore, ResultStoreError
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


def _make_receive(project_key: str, session_id: str):
    from core.domain import ReceiveSettingsSnapshot, SessionBindingMode, TargetExecutor

    return ReceiveSettingsSnapshot(
        config_revision=5, committed_at=_T0, received_at=_T0,
        effective_executor=TargetExecutor.OPENCHAMBER, directory=r"D:\AIwork\proj",
        project_key=project_key, agent="build", requested_model="",
        binding_mode=SessionBindingMode.FIXED_SESSION, frozen_session_id=session_id,
    )


@pytest.fixture
def store_env(tmp_path):
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


class TestImmutableResult:
    def test_result_update_forbidden_by_trigger(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        with db.transaction():
            created = ops.insert_result_in(
                db.connection, result_id="res-1", task_key=task_key,
                attempt_id=attempt_id, revision=1, state="COMPLETED",
                source="AUTO_RELAY", final_body="报告正文",
                protocol_text="AI_RELAY/1\n\n报告正文", sha256="h", 
                remote_message_ids=["m1"], committed_at=_T0,
            )
        assert created == "created"
        with pytest.raises(sqlite3.IntegrityError):
            db.connection.execute(
                "UPDATE results SET final_body='覆盖' WHERE result_id='res-1'"
            )

    def test_result_delete_forbidden_by_trigger(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        with db.transaction():
            ops.insert_result_in(
                db.connection, result_id="res-1", task_key=task_key,
                attempt_id=attempt_id, revision=1, state="COMPLETED",
                source="AUTO_RELAY", final_body="报告正文",
                protocol_text="AI_RELAY/1\n\n报告正文", sha256="h",
                remote_message_ids=[], committed_at=_T0,
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.connection.execute("DELETE FROM results WHERE result_id='res-1'")


class TestRevisionAndReads:
    def test_revision_monotonic(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        with db.transaction():
            assert ops.next_revision_in(db.connection, task_key) == 1
            ops.insert_result_in(
                db.connection, result_id="res-1", task_key=task_key,
                attempt_id=attempt_id, revision=1, state="COMPLETED",
                source="AUTO_RELAY", final_body="A",
                protocol_text="AI_RELAY/1\n\nA", sha256="h1",
                remote_message_ids=[], committed_at=_T0,
            )
            assert ops.next_revision_in(db.connection, task_key) == 2

    def test_insert_result_same_revision_conflict(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        with db.transaction():
            first = ops.insert_result_in(
                db.connection, result_id="res-1", task_key=task_key,
                attempt_id=attempt_id, revision=1, state="COMPLETED",
                source="AUTO_RELAY", final_body="A",
                protocol_text="AI_RELAY/1\n\nA", sha256="h1",
                remote_message_ids=[], committed_at=_T0,
            )
            second = ops.insert_result_in(
                db.connection, result_id="res-2", task_key=task_key,
                attempt_id=attempt_id, revision=1, state="COMPLETED",
                source="AUTO_RELAY", final_body="B",
                protocol_text="AI_RELAY/1\n\nB", sha256="h2",
                remote_message_ids=[], committed_at=_T0,
            )
        assert first == "created"
        assert second == "conflict"

    def test_authoritative_pointer_read_and_status(self, store_env):
        from core.result_commit import (
            CandidateResult,
            CommitOutcome,
            ResultCommitService,
        )
        from core.domain import ResultStatus

        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        service = ResultCommitService(
            db, result_store=ops, clock=FakeClock(wall=_WALL),
            result_id_factory=lambda t, a, e: f"res-{a}-{e}",
            delivery_id_factory=lambda t, r: f"deliv-{r}",
            event_id_factory=lambda r: f"evt-{r}",
        )
        outcome = service.commit(
            CandidateResult(task_key=task_key, attempt_id=attempt_id,
                            authority_epoch=1, result_state="COMPLETED",
                            source="AUTO_RELAY", final_body="报告正文", result_id="res-1")
        )
        assert outcome.outcome is CommitOutcome.COMMITTED
        current = ops.get_authoritative_for_task(task_key)
        assert current is not None
        assert current.status is ResultStatus.AUTHORITATIVE
        assert current.result_id == "res-1"
        assert current.revision == 1
        assert current.final_body == "报告正文"
        plain = ops.get_result_by_id("res-1")
        assert plain is not None
        assert plain.status is ResultStatus.CANDIDATE  # 无 task 指针时只是普通版本

    def test_fetch_missing_result_returns_none(self, store_env):
        _, _, ops, _, _, _ = store_env
        assert ops.get_result_by_id("res-不存在") is None
        assert ops.get_authoritative_for_task("1:CHATGPT:missing") is None


class TestOutboxClaims:
    def test_outbox_unique_per_result_peer(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        with db.transaction():
            ops.insert_result_in(
                db.connection, result_id="res-1", task_key=task_key,
                attempt_id=attempt_id, revision=1, state="COMPLETED",
                source="AUTO_RELAY", final_body="A",
                protocol_text="AI_RELAY/1\n\nA", sha256="h",
                remote_message_ids=[], committed_at=_T0,
            )
            first = ops.insert_outbox_in(
                db.connection, delivery_id="deliv-1", result_id="res-1",
                peer_id="CHATGPT", profile="legacy_v1", now=_T0,
            )
            second = ops.insert_outbox_in(
                db.connection, delivery_id="deliv-2", result_id="res-1",
                peer_id="CHATGPT", profile="legacy_v1", now=_T0,
            )
        assert first == "created"
        assert second == "existing"  # UNIQUE(result_id,peer_id) 幂等：不能两次排队同一结果

    def test_claim_unique_per_message_and_case(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        with db.transaction():
            for idx, result_id in enumerate(("res-1", "res-2"), start=1):
                ops.insert_result_in(
                    db.connection, result_id=result_id, task_key=task_key,
                    attempt_id=attempt_id, revision=idx, state="COMPLETED",
                    source="AUTO_RELAY", final_body="A",
                    protocol_text="AI_RELAY/1\n\nA", sha256="h",
                    remote_message_ids=[], committed_at=_T0,
                )
            first = ops.insert_claim_in(
                db.connection, endpoint="http://oc", session_id="sess-fixed",
                message_id="m1", task_key=task_key, result_id="res-1",
            )
            second = ops.insert_claim_in(
                db.connection, endpoint="http://oc", session_id="sess-fixed",
                message_id="m1", task_key=task_key, result_id="res-2",
            )
            other = ops.insert_claim_in(
                db.connection, endpoint="http://oc", session_id="sess-fixed",
                message_id="m2", task_key=task_key, result_id="res-1",
            )
        assert first == "created"
        assert second == "existing"  # 同一远端消息只能被一个 result 认领
        assert other == "created"

    def test_pending_head_and_mark_offered_cas(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        with db.transaction():
            for idx, result_id in enumerate(("res-1", "res-2"), start=1):
                ops.insert_result_in(
                    db.connection, result_id=result_id, task_key=task_key,
                    attempt_id=attempt_id, revision=idx, state="COMPLETED",
                    source="AUTO_RELAY", final_body="A",
                    protocol_text="AI_RELAY/1\n\nA", sha256="h",
                    remote_message_ids=[], committed_at=_T0,
                )
            ops.insert_outbox_in(
                db.connection, delivery_id="deliv-1", result_id="res-1",
                peer_id="CHATGPT", profile="legacy_v1", now=_T0,
            )
            ops.insert_outbox_in(
                db.connection, delivery_id="deliv-2", result_id="res-2",
                peer_id="CHATGPT", profile="legacy_v1", now=_T0,
            )
        head = ops.list_pending_deliveries(limit=1)
        assert len(head) == 1
        assert head[0].delivery_id == "deliv-1"  # 按 delivery_id 有序，先入先提供
        with db.transaction():
            assert ops.mark_offered_in(db.connection, delivery_id="deliv-1",
                                       now=_T0) is True
            assert ops.mark_offered_in(db.connection, delivery_id="deliv-1",
                                       now=_T0) is False  # CAS 只放行一次
        assert ops.list_pending_deliveries(limit=5)[0].delivery_id == "deliv-2"

    def test_invalid_state_rejected(self, store_env):
        db, _, ops, _, _, _ = store_env
        task_key, attempt_id, project, _ = _start(store_env)
        with pytest.raises(ResultStoreError):
            with db.transaction():
                ops.insert_result_in(
                    db.connection, result_id="res-x", task_key=task_key,
                    attempt_id=attempt_id, revision=1, state="DELIVERED",
                    source="AUTO_RELAY", final_body="A",
                    protocol_text="AI_RELAY/1\n\nA", sha256="h",
                    remote_message_ids=[], committed_at=_T0,
                )