"""T09：持久 FIFO 调度、Attempt 创建与项目执行权（主规格 01.3/05.3/10.2 + Q01/Q02/Q05/R05）。

覆盖：
- Q01：运行中接收 T2/T3 后重启 → 按 sequence 保留、不重复 Attempt、不越序启动；
- Q02：队首损坏 -> 无发送证据给明确本地终态并继续；有发送证据则隔离核验、禁止越过；
- Q05：队首项目隔离 → 不发送、显示阻塞、显式取消后才推进；
- R05：停止/人工包装后上游仍 busy → 本地终态已回传，同项目队列仍被隔离；
- 全局活动槽=1（OPEN attempt 权威），网络等待仍占槽；不同项目不并行；
- super 原子性：Attempt+Task=ACTIVE+project owner 同一事务；任一步失败整体回滚；
- 并发：双 Scheduler 指向同一 SQLite，BEGIN IMMEDIATE 串行化，仅一方 STARTED；
- 取消：QUEUED 可取消并推进队尾；ACTIVE 取消被拒绝；带 Attempt 证据的排队被拒绝。
"""

from __future__ import annotations

import itertools
import json
import threading
from datetime import datetime, timezone

import pytest

from core.domain import (
    ExecutionSettingsSnapshot,
    ReceiveSettingsSnapshot,
    SessionBindingMode,
    TargetExecutor,
)
from core.protocol_v1 import ProtocolFormat, content_digest, parse_message
from core.scheduler import (
    ScheduleBlockReason,
    ScheduleOutcome,
    Scheduler,
    SchedulerError,
    receive_snapshot_from_json,
)
from infra.clock import FakeClock
from storage.database import Database
from storage.task_store import (
    CancelOutcome,
    TaskStore,
    make_task_key,
)

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


def make_receive(
    project_key: str = "p-fixed",
    *,
    binding_mode: SessionBindingMode = SessionBindingMode.FIXED_SESSION,
    session_id: str | None = "sess-fixed",
    executor: TargetExecutor = TargetExecutor.OPENCHAMBER,
    config_revision: int = 5,
) -> ReceiveSettingsSnapshot:
    return ReceiveSettingsSnapshot(
        config_revision=config_revision,
        committed_at=_T0,
        received_at=_T0,
        effective_executor=executor,
        directory=r"D:\AIwork\proj",
        project_key=project_key,
        agent="build",
        requested_model="",
        binding_mode=binding_mode,
        frozen_session_id=session_id,
    )


def claim_ok(
    store: TaskStore,
    task_id: str,
    *,
    project_key: str = "p-fixed",
    binding_mode: SessionBindingMode = SessionBindingMode.FIXED_SESSION,
    session_id: str | None = "sess-fixed",
) -> str:
    raw = _V1.format(task_id=task_id, body=f"处理任务 {task_id}")
    msg = parse_message(raw)
    task_key = make_task_key("CHATGPT", task_id)
    store.claim(
        task_key=task_key,
        peer_id="CHATGPT",
        task_id=task_id,
        protocol_format=ProtocolFormat.V1.value.upper(),
        raw_message=raw,
        body=msg.body,
        canonical_hash=content_digest(msg),
        receive_snapshot=make_receive(
            project_key, binding_mode=binding_mode, session_id=session_id
        ),
        received_at=_T0,
    )
    return task_key


@pytest.fixture
def scheduler_env(tmp_path):
    db = Database(tmp_path / "t09.sqlite")
    db.open()
    store = TaskStore(db)
    clock = FakeClock(wall=_WALL)
    counter = itertools.count(1)
    attempt_ids: list[str] = []

    def attempt_id_factory(task_key: str) -> str:
        next_id = f"attempt-{next(counter):03d}"
        attempt_ids.append(next_id)
        return next_id

    scheduler = Scheduler(
        db,
        task_store=store,
        clock=clock,
        attempt_id_factory=attempt_id_factory,
    )
    yield db, store, clock, scheduler, attempt_ids
    db.close()


def _count(db, table: str, where: str = "", params: tuple = ()) -> int:
    return int(
        db.connection.execute(f"SELECT COUNT(*) FROM {table} {where}", params).fetchone()[0]
    )


def _state(db, task_key: str) -> str:
    return db.connection.execute(
        "SELECT state FROM tasks WHERE task_key=?", (task_key,)
    ).fetchone()[0]


def _events(db, task_key: str | None = None) -> list[str]:
    if task_key is None:
        rows = db.connection.execute(
            "SELECT event_code FROM events ORDER BY event_seq"
        ).fetchall()
    else:
        rows = db.connection.execute(
            "SELECT event_code FROM events WHERE task_key=? ORDER BY event_seq", (task_key,)
        ).fetchall()
    return [r[0] for r in rows]


def _close_open_attempt(db, task_key: str) -> None:
    db.connection.execute(
        "UPDATE attempts SET state='COMPLETED', ended_at=? WHERE task_key=? AND state='OPEN'",
        (_T0, task_key),
    )


def _corrupt(db, task_key: str, column: str, value: str) -> None:
    db.connection.execute(f"UPDATE tasks SET {column}=? WHERE task_key=?", (value, task_key))


def _insert_attempt(db, task_key: str, attempt_id: str, state: str = "FAILED") -> None:
    db.connection.execute(
        "INSERT INTO attempts (attempt_id, task_key, kind, state, authority_epoch,"
        " execution_snapshot_json, started_at) VALUES (?,?,?,?,?,?,?)",
        (attempt_id, task_key, "INITIAL", state, 1, "{}", _T0),
    )


class TestStartBasics:
    def test_empty_queue_returns_no_task(self, scheduler_env):
        db, _store, _clock, scheduler, _attempts = scheduler_env
        result = scheduler.tick()
        assert result.outcome is ScheduleOutcome.NO_TASK
        assert _count(db, "attempts") == 0
        assert _count(db, "project_leases") == 0
        assert _count(db, "events") == 0

    def test_start_locks_attempt_owner_and_task_atomically(self, scheduler_env):
        db, store, _clock, scheduler, attempts = scheduler_env
        key = claim_ok(store, "t1")
        result = scheduler.tick()
        assert result.outcome is ScheduleOutcome.STARTED
        assert result.task_key == key
        assert result.sequence == 1
        assert result.attempt_id == "attempt-001"

        task = db.connection.execute(
            "SELECT state, active_attempt_id, authority_epoch FROM tasks WHERE task_key=?",
            (key,),
        ).fetchone()
        assert task[0] == "ACTIVE"
        assert task[1] == "attempt-001"
        assert task[2] == 1

        attempt = db.connection.execute(
            "SELECT kind, state, authority_epoch, remote_state FROM attempts"
            " WHERE attempt_id='attempt-001'"
        ).fetchone()
        assert attempt == ("INITIAL", "OPEN", 1, "NOT_SENT")

        lease = db.connection.execute("SELECT * FROM project_leases").fetchone()
        assert lease is not None
        assert lease[1] == key  # owner_task_key
        assert lease[2] == "attempt-001"  # owner_attempt_id
        assert lease[3] == 1  # authority_epoch
        assert lease[4] == "ACTIVE"  # state

        assert db.connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE state='OPEN'"
        ).fetchone()[0] == 1
        result2 = scheduler.tick()
        assert result2.outcome is ScheduleOutcome.NO_TASK


class TestGlobalSlot:
    def test_global_slot_busy_blocks_second_different_project(self, scheduler_env):
        db, store, _clock, scheduler, _attempts = scheduler_env
        a = claim_ok(store, "t1", project_key="p-a")
        b = claim_ok(store, "t2", project_key="p-b")
        first = scheduler.tick()
        assert first.outcome is ScheduleOutcome.STARTED
        assert first.task_key == a

        second = scheduler.tick()
        assert second.outcome is ScheduleOutcome.BLOCKED
        assert second.block_reason is ScheduleBlockReason.GLOBAL_SLOT_BUSY
        assert second.task_key == b
        assert _state(db, b) == "QUEUED"
        assert _count(db, "attempts") == 1
        assert _count(db, "project_leases") == 1

    def test_network_wait_attempt_still_occupies_slot(self, scheduler_env):
        db, store, _clock, scheduler, _attempts = scheduler_env
        a = claim_ok(store, "t1", project_key="p-a")
        b = claim_ok(store, "t2", project_key="p-b")
        scheduler.tick()
        db.connection.execute(
            "INSERT INTO recovery_runtime (attempt_id, phase, resume_total,"
            " resume_send_attempts, consecutive_no_progress, enabled, resume_after_restart,"
            " runtime_json) VALUES (?,?,?,?,?,?,?,?)",
            ("attempt-001", "WAIT_NETWORK", 0, 0, 0, 1, 1, "{}"),
        )
        blocked = scheduler.tick()
        assert blocked.outcome is ScheduleOutcome.BLOCKED
        assert blocked.block_reason is ScheduleBlockReason.GLOBAL_SLOT_BUSY
        assert blocked.task_key == b
        assert _state(db, b) == "QUEUED"

    def test_q01_restart_preserves_sequence_no_duplicate_attempt(self, scheduler_env):
        db, store, _clock, _scheduler, _attempts = scheduler_env
        a = claim_ok(store, "t1", project_key="p-a")
        b = claim_ok(store, "t2", project_key="p-b")
        _scheduler.tick()
        # 模拟重启：同一数据库全新的 Scheduler 实例
        restarted = Scheduler(
            db,
            task_store=TaskStore(db),
            clock=FakeClock(wall=_WALL),
            attempt_id_factory=lambda _task: "attempt-reboot",
        )
        result = restarted.tick()
        assert result.outcome is ScheduleOutcome.BLOCKED
        assert result.block_reason is ScheduleBlockReason.GLOBAL_SLOT_BUSY
        assert result.task_key == b
        assert _count(db, "attempts") == 1  # 不重复创建 A 的 Attempt
        assert _count(db, "project_leases") == 1
        assert _state(db, a) == "ACTIVE"
        assert _state(db, b) == "QUEUED"

    def test_after_attempt_closes_next_in_sequence_starts(self, scheduler_env):
        db, store, _clock, scheduler, _attempts = scheduler_env
        a = claim_ok(store, "t1", project_key="p-a")
        b = claim_ok(store, "t2", project_key="p-b")
        scheduler.tick()
        _close_open_attempt(db, a)
        next_result = scheduler.tick()
        assert next_result.outcome is ScheduleOutcome.STARTED
        assert next_result.task_key == b
        assert next_result.sequence == 2
        assert _state(db, b) == "ACTIVE"
        assert _count(db, "attempts") == 2


class TestProjectIsolation:
    def test_q05_same_project_other_session_blocked_and_not_sent(self, scheduler_env):
        db, store, _clock, scheduler, _attempts = scheduler_env
        a = claim_ok(store, "t1", project_key="p-isolate", session_id="sess-1")
        b = claim_ok(store, "t2", project_key="p-isolate", session_id="sess-2")
        scheduler.tick()
        _close_open_attempt(db, a)  # 本地已回传终态，但执行权不因执行结束自动释放
        result = scheduler.tick()
        assert result.outcome is ScheduleOutcome.BLOCKED
        assert result.block_reason is ScheduleBlockReason.PROJECT_BUSY
        assert result.task_key == b
        assert result.project_key == "p-isolate"
        assert _state(db, b) == "QUEUED"
        assert _count(db, "attempts") == 1
        assert _count(db, "project_leases") == 1

    def test_q05_explicit_cancel_of_blocked_head_allows_next(self, scheduler_env):
        db, store, _clock, scheduler, attempts = scheduler_env
        a = claim_ok(store, "t1", project_key="p-isolate", session_id="sess-1")
        b = claim_ok(store, "t2", project_key="p-isolate", session_id="sess-2")
        c = claim_ok(store, "t3", project_key="p-other")
        scheduler.tick()
        _close_open_attempt(db, a)

        blocked_b = scheduler.tick()
        assert blocked_b.outcome is ScheduleOutcome.BLOCKED
        assert blocked_b.task_key == b
        assert _state(db, c) == "QUEUED"  # 不同项目也不越过被隔离的队首

        cancel = store.cancel_queued(b, cancelled_at=_T0)
        assert cancel.outcome is CancelOutcome.CANCELLED
        assert _events(db, b) == ["TASK_CLAIMED", "QUEUED_CANCELLED"]
        assert _state(db, b) == "STOPPED_BY_USER"

        started_c = scheduler.tick()
        assert started_c.outcome is ScheduleOutcome.STARTED
        assert started_c.task_key == c
        assert started_c.project_key == "p-other"

    def test_r05_quarantined_project_blocks_same_project_queue(self, scheduler_env):
        db, store, _clock, scheduler, _attempts = scheduler_env
        a = claim_ok(store, "t1", project_key="p-q")
        b = claim_ok(store, "t2", project_key="p-q")
        scheduler.tick()
        _close_open_attempt(db, a)
        db.connection.execute("UPDATE project_leases SET state='QUARANTINED'")
        result = scheduler.tick()
        assert result.outcome is ScheduleOutcome.BLOCKED
        assert result.block_reason is ScheduleBlockReason.PROJECT_BUSY
        assert result.task_key == b
        assert _state(db, b) == "QUEUED"


class TestCorruptHeadQ02:
    def test_q02_corrupt_unsent_local_terminal_then_continue(self, scheduler_env):
        db, store, _clock, scheduler, _attempts = scheduler_env
        a = claim_ok(store, "t1", project_key="p-a")
        b = claim_ok(store, "t2", project_key="p-b")
        _corrupt(db, a, "canonical_hash", "zzz-broken-hash")

        result = scheduler.tick()
        assert result.outcome is ScheduleOutcome.STARTED
        assert result.task_key == b
        assert result.local_rejected_count == 1
        # 坏队首：无发送证据 → 明确本地终态（不拼凑任务），队列才允许继续
        assert _state(db, a) == "STOPPED_BY_USER"
        assert db.connection.execute(
            "SELECT blocked_reason FROM tasks WHERE task_key=?", (a,)
        ).fetchone()[0] == "corrupt_unsent_local_reject"
        assert _events(db, a) == ["TASK_CLAIMED", "CORRUPT_HEAD_LOCAL_REJECT"]
        # 健康队尾在坏队首处理过的同一事务内继续启动
        assert _state(db, b) == "ACTIVE"
        assert _count(db, "attempts") == 1
        assert _count(db, "project_leases") == 1

    def test_q02_corrupt_with_send_evidence_blocked_not_overtaken(self, scheduler_env):
        db, store, _clock, scheduler, _attempts = scheduler_env
        a = claim_ok(store, "t1", project_key="p-a")
        b = claim_ok(store, "t2", project_key="p-b")
        _corrupt(db, a, "canonical_hash", "zzz-broken-hash")
        _insert_attempt(db, a, "attempt-legacy", state="FAILED")  # 可能已发送的证据

        result = scheduler.tick()
        assert result.outcome is ScheduleOutcome.BLOCKED
        assert result.block_reason is ScheduleBlockReason.CORRUPT_HEAD_MAYBE_SENT
        assert result.task_key == a
        assert _state(db, a) == "QUEUED"  # 保持 QUEUED 等待人工核验，不自动处理
        assert _state(db, b) == "QUEUED"  # 禁止越过坏队首
        assert _count(db, "attempts") == 1
        assert _count(db, "project_leases") == 0
        assert _events(db, a) == ["TASK_CLAIMED"]  # 只隔离，不新增调度事件

    def test_local_reject_cap_returns_local_rejected(self, scheduler_env):
        db, store, _clock, scheduler, _attempts = scheduler_env
        a = claim_ok(store, "t1", project_key="p-a")
        b = claim_ok(store, "t2", project_key="p-b")
        _corrupt(db, a, "canonical_hash", "zzz-a")
        _corrupt(db, b, "canonical_hash", "zzz-b")
        cap_scheduler = Scheduler(
            db,
            task_store=store,
            clock=FakeClock(wall=_WALL),
            attempt_id_factory=lambda _t: "attempt-cap",
            max_local_rejects=1,
        )
        result = cap_scheduler.tick()
        assert result.outcome is ScheduleOutcome.LOCAL_REJECTED
        assert result.local_rejected_count == 1
        assert _state(db, a) == "STOPPED_BY_USER"
        assert _state(db, b) == "QUEUED"
        second = cap_scheduler.tick()
        assert second.outcome is ScheduleOutcome.LOCAL_REJECTED
        assert _state(db, b) == "STOPPED_BY_USER"


class TestBarriers:
    def test_startup_barrier_blocks_without_writes(self, scheduler_env):
        db, store, _clock, _scheduler, _attempts = scheduler_env
        a = claim_ok(store, "t1", project_key="p-a")
        scheduler = Scheduler(
            db,
            task_store=store,
            clock=FakeClock(wall=_WALL),
            startup_ready=lambda: False,
        )
        result = scheduler.tick()
        assert result.outcome is ScheduleOutcome.BLOCKED
        assert result.block_reason is ScheduleBlockReason.STARTUP_RECONCILE_PENDING
        assert _state(db, a) == "QUEUED"
        assert _count(db, "attempts") == 0
        assert _count(db, "project_leases") == 0
        assert _count(db, "events") == 1  # 仅认领事件，调度未写入任何数据

    def test_rotation_barrier_blocks_head(self, scheduler_env):
        db, store, _clock, _scheduler, _attempts = scheduler_env
        a = claim_ok(store, "t1", project_key="p-a")
        b = claim_ok(store, "t2", project_key="p-b")
        scheduler = Scheduler(
            db,
            task_store=store,
            clock=FakeClock(wall=_WALL),
            rotation_pending=lambda: True,
        )
        result = scheduler.tick()
        assert result.outcome is ScheduleOutcome.BLOCKED
        assert result.block_reason is ScheduleBlockReason.ROTATION_PENDING
        assert result.task_key == a
        assert _state(db, a) == "QUEUED"
        assert _state(db, b) == "QUEUED"
        assert _count(db, "attempts") == 0


class TestCancel:
    def test_cancel_queued_allows_next_to_start(self, scheduler_env):
        db, store, _clock, scheduler, _attempts = scheduler_env
        a = claim_ok(store, "t1", project_key="p-a")
        b = claim_ok(store, "t2", project_key="p-b")
        cancel = store.cancel_queued(a, cancelled_at=_T0)
        assert cancel.outcome is CancelOutcome.CANCELLED
        assert cancel.state == "STOPPED_BY_USER"
        assert _events(db, a) == ["TASK_CLAIMED", "QUEUED_CANCELLED"]
        assert _state(db, a) == "STOPPED_BY_USER"
        result = scheduler.tick()
        assert result.outcome is ScheduleOutcome.STARTED
        assert result.task_key == b
        assert result.sequence == 2

    def test_cancel_unknown_returns_not_found(self, scheduler_env):
        _db, store, _clock, _scheduler, _attempts = scheduler_env
        result = store.cancel_queued("nope", cancelled_at=_T0)
        assert result.outcome is CancelOutcome.NOT_FOUND

    def test_cancel_active_rejected(self, scheduler_env):
        db, store, _clock, scheduler, _attempts = scheduler_env
        a = claim_ok(store, "t1", project_key="p-a")
        scheduler.tick()
        result = store.cancel_queued(a, cancelled_at=_T0)
        assert result.outcome is CancelOutcome.REJECTED_NOT_QUEUED
        assert result.state == "ACTIVE"
        # 取消不得触碰活动执行：attempt/lease 原样保留
        assert _count(db, "attempts") == 1
        assert _count(db, "project_leases") == 1
        assert _events(db, a) == ["TASK_CLAIMED", "TASK_STARTED"]

    def test_cancel_with_attempt_evidence_rejected(self, scheduler_env):
        db, store, _clock, _scheduler, _attempts = scheduler_env
        a = claim_ok(store, "t1", project_key="p-a")
        _insert_attempt(db, a, "attempt-legacy", state="FAILED")
        result = store.cancel_queued(a, cancelled_at=_T0)
        assert result.outcome is CancelOutcome.REJECTED_HAS_ATTEMPT
        assert result.attempt_count == 1


class TestAtomicity:
    @pytest.mark.parametrize("fault_step", [1, 2, 3, 4, 5])
    def test_fault_at_each_checkpoint_rolls_back_everything(self, scheduler_env, fault_step):
        db, store, _clock, scheduler, _attempts = scheduler_env
        a = claim_ok(store, "t1", project_key="p-a")
        scheduler.fault_inject_after = fault_step
        with pytest.raises(SchedulerError):
            scheduler.tick()
        # 事务整体回滚：无半条 Attempt、无 owner、任务仍 QUEUED、无事件
        assert _count(db, "attempts") == 0
        assert _count(db, "project_leases") == 0
        assert _count(db, "events") == 1  # 仅认领事件，调度未写入任何数据
        row = db.connection.execute(
            "SELECT state, active_attempt_id, authority_epoch FROM tasks WHERE task_key=?",
            (a,),
        ).fetchone()
        assert row == ("QUEUED", None, 0)

        # 故障关闭后同一 tick 可正常启动，不残留脏状态
        scheduler.fault_inject_after = None
        result = scheduler.tick()
        assert result.outcome is ScheduleOutcome.STARTED
        assert _count(db, "attempts") == 1
        assert _count(db, "project_leases") == 1
        assert _events(db, a) == ["TASK_CLAIMED", "TASK_STARTED"]
        assert _state(db, a) == "ACTIVE"


class TestExecutionSnapshot:
    def test_fixed_session_frozen_execution_snapshot(self, scheduler_env):
        db, store, _clock, scheduler, _attempts = scheduler_env
        a = claim_ok(store, "t1", project_key="p-fixed", session_id="sess-fixed")
        scheduler.tick()
        snap_text = db.connection.execute(
            "SELECT execution_snapshot_json FROM attempts WHERE attempt_id='attempt-001'"
        ).fetchone()[0]
        data = json.loads(snap_text)
        assert data["receive"]["project_key"] == "p-fixed"
        assert data["binding_revision"] == 5
        assert data["resolved_session_id"] == "sess-fixed"
        assert data["execution_started_at"] == _T0

    def test_project_rotating_resolved_session_none(self, scheduler_env):
        db, store, _clock, scheduler, _attempts = scheduler_env
        a = claim_ok(
            store, "t1", project_key="p-rot",
            binding_mode=SessionBindingMode.PROJECT_ROTATING, session_id=None,
        )
        result = scheduler.tick()
        assert result.outcome is ScheduleOutcome.STARTED
        snap_text = db.connection.execute(
            "SELECT execution_snapshot_json FROM attempts WHERE attempt_id='attempt-001'"
        ).fetchone()[0]
        data = json.loads(snap_text)
        assert data["resolved_session_id"] is None  # T09 无已提交新会话，轮换边界留给后续流程
        assert data["binding_revision"] == 5

    def test_fixed_without_binding_raises_and_rolls_back(self, scheduler_env):
        from core.errors import DomainError

        db, store, _clock, scheduler, _attempts = scheduler_env
        a = claim_ok(store, "t1", project_key="p-fixed", session_id="sess-fixed")
        # 人为破坏快照：FIXED 模式却清空冻结 session → 执行期必须报错且不产生半状态
        db.connection.execute(
            "UPDATE tasks SET ingress_snapshot_json=? WHERE task_key=?",
            (
                json.dumps({**json.loads(
                    db.connection.execute(
                        "SELECT ingress_snapshot_json FROM tasks WHERE task_key=?", (a,)
                    ).fetchone()[0]
                ), "frozen_session_id": None}),
                a,
            ),
        )
        with pytest.raises(DomainError, match="session_id"):
            scheduler.tick()
        assert _count(db, "attempts") == 0
        assert _count(db, "project_leases") == 0
        assert _state(db, a) == "QUEUED"

    def test_custom_execution_builder_invoked_with_parsed_receive(self, scheduler_env):
        db, store, _clock, scheduler, _attempts = scheduler_env
        a = claim_ok(store, "t1", project_key="p-custom")
        captured: dict = {}

        def custom_builder(receive, execution_started_at):
            captured["project_key"] = receive.project_key
            captured["started_at"] = execution_started_at
            return ExecutionSettingsSnapshot(
                receive=receive,
                binding_revision=receive.config_revision,
                resolved_session_id="custom-session",
                execution_started_at=execution_started_at,
            )

        scheduler = Scheduler(
            db,
            task_store=store,
            clock=FakeClock(wall=_WALL),
            attempt_id_factory=lambda _t: "attempt-custom",
            execution_builder=custom_builder,
        )
        result = scheduler.tick()
        assert result.outcome is ScheduleOutcome.STARTED
        assert captured["project_key"] == "p-custom"
        assert captured["started_at"] == _T0
        snap_text = db.connection.execute(
            "SELECT execution_snapshot_json FROM attempts WHERE attempt_id='attempt-custom'"
        ).fetchone()[0]
        assert json.loads(snap_text)["resolved_session_id"] == "custom-session"


class TestConcurrency:
    def test_double_scheduler_race_one_wins(self, tmp_path):
        path = tmp_path / "race.sqlite"
        bootstrap = Database(path)
        bootstrap.open()
        bootstrap.close()

        barrier = threading.Barrier(2)
        results: list = []
        lock = threading.Lock()

        def worker(idx: int) -> None:
            own_db = Database(path)
            own_db.open()
            try:
                store = TaskStore(own_db)
                claim_ok(store, "t1", project_key="p-race")
                scheduler = Scheduler(
                    own_db,
                    task_store=store,
                    clock=FakeClock(wall=_WALL),
                    attempt_id_factory=lambda _t: f"attempt-race-{idx}",
                )
                barrier.wait()
                result = scheduler.tick()
                with lock:
                    results.append(result)
            finally:
                own_db.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert [r.outcome for r in results].count(ScheduleOutcome.STARTED) == 1
        assert [r.outcome for r in results].count(ScheduleOutcome.NO_TASK) == 1

        check = Database(path)
        check.open()
        try:
            # 最终一致：恰好 1 个 ACTIVE 任务、1 个 OPEN attempt、1 个 owner、1 条事件
            assert _count(check, "tasks", "WHERE state='ACTIVE'") == 1
            assert _count(check, "attempts", "WHERE state='OPEN'") == 1
            assert _count(check, "project_leases") == 1
            assert _count(check, "events") == 2  # TASK_CLAIMED + TASK_STARTED
            row = check.connection.execute(
                "SELECT t.task_key, t.active_attempt_id, a.attempt_id, l.owner_task_key,"
                " l.owner_attempt_id FROM tasks t JOIN attempts a ON a.task_key=t.task_key"
                " JOIN project_leases l ON l.owner_task_key=t.task_key"
            ).fetchone()
            assert row is not None
            assert row[0] == row[3]
            assert row[1] == row[2] == row[4]
        finally:
            check.close()


def test_schedule_block_reason_strings_stable():
    # 阻塞原因使用稳定值，UI 不应匹配中文文本
    assert ScheduleBlockReason.GLOBAL_SLOT_BUSY.value == "global_slot_busy"
    assert ScheduleBlockReason.PROJECT_BUSY.value == "project_busy"
    assert ScheduleBlockReason.CORRUPT_HEAD_MAYBE_SENT.value == "corrupt_head_maybe_sent"


def test_receive_snapshot_roundtrip():
    snap = make_receive("p-rt", session_id="sess-rt")
    text = json.dumps(
        {
            "config_revision": snap.config_revision,
            "committed_at": snap.committed_at,
            "received_at": snap.received_at,
            "effective_executor": snap.effective_executor.value,
            "directory": snap.directory,
            "project_key": snap.project_key,
            "agent": snap.agent,
            "requested_model": snap.requested_model,
            "binding_mode": snap.binding_mode.value,
            "frozen_session_id": snap.frozen_session_id,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    restored = receive_snapshot_from_json(text)
    assert restored == snap


def test_fake_clock_wall_and_monotonic_independent():
    clock = FakeClock(wall=_WALL)
    clock.advance(120)
    assert clock.monotonic() == 120.0
    assert clock.now() > _WALL