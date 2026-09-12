"""T11R：正常权威完成后的 Project Lease 原子安全释放。

依据（A 端 T11R 前置修复卡 / 主规格 01 正常主链路"安全释放项目→下一任务"）：
- 设计决定：project_leases 是"当前占用权表"而非 lease 历史表；正常释放用
  release_active_in 对精确 ACTIVE owner row 做与结果事务同原子的 DELETE，不新增
  RELEASED 状态、不迁移 schema（历史仍由 task/attempt/result/event 记录）。
- 安全门：remote_state == IDLE_VERIFIED 才释放；QUARANTINED/ROTATING、owner 或 epoch
  不匹配、远端非安全空闲一律保留，绝不释放；lease 不存在不视为成功也不回滚合法结果。
- project_key 只取自 Attempt 冻结的 execution_snapshot_json（receive.project_key），
  不允许 Candidate 决定释放哪个项目。
- 覆盖 A1-A5 原语、Case1-6、远端 4 态矩阵（IDLE_VERIFIED/UNKNOWN/BUSY/MAYBE_RUNNING）、
  非 COMPLETED 终态保留、lease 缺失与重复提交幂等、事务故障整体回滚、旧 worker 不删
  新 owner、多同项目任务 FIFO 连续推进、schema 未变化。

本卡不执行 T12（无 FakeExecutor/无 Controller）。
"""

from __future__ import annotations

import itertools
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.protocol_v1 import ProtocolFormat, content_digest, parse_message
from core.result_commit import (
    CandidateResult,
    CommitOutcome,
    ResultClaim,
    ResultCommitService,
)
from core.scheduler import ScheduleBlockReason, ScheduleOutcome, Scheduler
from infra.clock import FakeClock
from storage.database import Database
from storage.lease_store import (
    LeaseReleaseOutcome,
    ProjectLeaseStore,
)
from storage.result_store import ResultStore
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


def _make_receive(project_key: str, session_id: str):
    from core.domain import ReceiveSettingsSnapshot, SessionBindingMode, TargetExecutor

    return ReceiveSettingsSnapshot(
        config_revision=5, committed_at=_T0, received_at=_T0,
        effective_executor=TargetExecutor.OPENCHAMBER, directory=r"D:\AIwork\proj",
        project_key=project_key, agent="build", requested_model="",
        binding_mode=SessionBindingMode.FIXED_SESSION, frozen_session_id=session_id,
    )


class _Env:
    def __init__(self, db, tasks, ops, clock, scheduler, attempts):
        self.db = db
        self.tasks = tasks
        self.ops = ops
        self.clock = clock
        self.scheduler = scheduler
        self.attempts = attempts


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t11r.sqlite")
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
    yield _Env(db, tasks, ops, clock, scheduler, attempts)
    db.close()


def _service(env, *, remote_state: str = "IDLE_VERIFIED", result_id: str | None = None):
    return ResultCommitService(
        env.db, result_store=env.ops, lease_store=ProjectLeaseStore(env.db),
        clock=env.clock,
        result_id_factory=(lambda t, a, e: result_id) if result_id
        else (lambda t, a, e: f"res-{a}"),
        delivery_id_factory=lambda t, r: f"deliv-{t}-{r}",
        event_id_factory=lambda r: f"evt-{r}",
    ), remote_state


def _claim(env, task_id: str, project_key: str = "p-fixed",
           session_id: str = "sess-fixed") -> str:
    raw = _V1.format(task_id=task_id, body=f"处理任务 {task_id}")
    msg = parse_message(raw)
    task_key = make_task_key("CHATGPT", task_id)
    env.tasks.claim(
        task_key=task_key, peer_id="CHATGPT", task_id=task_id,
        protocol_format=ProtocolFormat.V1.value.upper(), raw_message=raw,
        body=msg.body, canonical_hash=content_digest(msg),
        receive_snapshot=_make_receive(project_key, session_id), received_at=_T0,
    )
    return task_key


def _start(env, task_id: str, project_key: str = "p-fixed") -> tuple[str, str]:
    task_key = _claim(env, task_id, project_key=project_key)
    result = env.scheduler.tick()
    assert result.outcome is ScheduleOutcome.STARTED
    assert result.attempt_id is not None
    return task_key, result.attempt_id


def _candidate(task_key, attempt_id, *, epoch=1, result_id=None, state="COMPLETED",
               source="AUTO_RELAY", body="完成报告正文", message_id=None,
               remote_state="IDLE_VERIFIED"):
    claim = None
    if message_id is not None:
        claim = ResultClaim(endpoint=_ENDPOINT, session_id="sess-fixed",
                            message_id=message_id)
    return CandidateResult(
        task_key=task_key, attempt_id=attempt_id, authority_epoch=epoch,
        result_state=state, source=source, final_body=body,
        result_id=result_id or f"res-{attempt_id}", claim=claim,
        remote_state=remote_state,
    )


def _commit(env, task_key, attempt_id, *, remote_state="IDLE_VERIFIED",
            message_id=None, epoch=1, result_id=None):
    svc, _ = _service(env)
    candidate = _candidate(
        task_key, attempt_id, epoch=epoch, result_id=result_id,
        message_id=message_id, remote_state=remote_state,
    )
    return svc.commit(candidate)


def _count(db, table: str, where: str = "", params: tuple = ()) -> int:
    return int(db.connection.execute(
        f"SELECT COUNT(*) FROM {table} {where}", params).fetchone()[0])


def _lease(db, project_key: str = "p-fixed"):
    return ProjectLeaseStore(db).read_owner(project_key)


def _set_lease_state(db, project_key: str, state: str) -> None:
    with db.transaction():
        db.connection.execute(
            "UPDATE project_leases SET state=? WHERE project_key=?",
            (state, project_key),
        )


def _task_state(db, task_key: str) -> str:
    return db.connection.execute(
        "SELECT state FROM tasks WHERE task_key=?", (task_key,)).fetchone()[0]


def _attempt_state(db, attempt_id: str) -> str:
    return db.connection.execute(
        "SELECT state FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()[0]


def _result_id_for(db, task_key: str) -> str | None:
    row = db.connection.execute(
        "SELECT result_id FROM results WHERE task_key=? AND revision="
        "(SELECT current_result_revision FROM tasks WHERE task_key=?)",
        (task_key, task_key)).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# release_active_in 原语单元
# ---------------------------------------------------------------------------

class TestLeaseReleasePrimitive:
    """A 组（A1-A5）：release_active_in 原语——精确 CAS，绝不按 project_key 粗暴删除。"""

    def _snapshot(self, env):
        lease = _lease(env.db)
        if lease is None:
            return None
        return (lease.project_key, lease.owner_task_key, lease.owner_attempt_id,
                lease.authority_epoch, lease.state, lease.related_sessions,
                lease.last_verified_at, lease.reason)

    def _release(self, env, *, task_key, attempt_id, epoch=1, project_key="p-fixed"):
        with env.db.transaction():
            return ProjectLeaseStore(env.db).release_active_in(
                env.db.connection, project_key=project_key,
                owner_task_key=task_key, owner_attempt_id=attempt_id,
                authority_epoch=epoch)

    def test_a1_exact_active_owner_released(self, env):
        task_key, attempt_id = _start(env, "rel-001")
        res = self._release(env, task_key=task_key, attempt_id=attempt_id)
        assert res.outcome is LeaseReleaseOutcome.RELEASED
        assert _lease(env.db) is None

    def test_a2_not_found_creates_nothing(self, env):
        before = _count(env.db, "project_leases")
        res = self._release(env, task_key="k", attempt_id="a")
        assert res.outcome is LeaseReleaseOutcome.NOT_FOUND
        assert _count(env.db, "project_leases") == before == 0

    def test_a3_task_mismatch_keeps_row_unchanged(self, env):
        task_key, attempt_id = _start(env, "rel-002")
        before = self._snapshot(env)
        res = self._release(env, task_key="other-task", attempt_id=attempt_id)
        assert res.outcome is LeaseReleaseOutcome.OWNER_MISMATCH
        assert self._snapshot(env) == before

    def test_a3_attempt_mismatch_keeps_row_unchanged(self, env):
        task_key, attempt_id = _start(env, "rel-003")
        before = self._snapshot(env)
        res = self._release(env, task_key=task_key, attempt_id="other-attempt")
        assert res.outcome is LeaseReleaseOutcome.OWNER_MISMATCH
        assert self._snapshot(env) == before

    def test_a3_epoch_mismatch_keeps_row_unchanged(self, env):
        task_key, attempt_id = _start(env, "rel-004")
        with env.db.transaction():
            env.db.connection.execute(
                "UPDATE project_leases SET authority_epoch=9 WHERE project_key='p-fixed'")
        before = self._snapshot(env)
        res = self._release(env, task_key=task_key, attempt_id=attempt_id, epoch=1)
        assert res.outcome is LeaseReleaseOutcome.OWNER_MISMATCH
        assert self._snapshot(env) == before

    def test_a4_quarantined_not_releasable_even_owner_matches(self, env):
        task_key, attempt_id = _start(env, "rel-005")
        _set_lease_state(env.db, "p-fixed", "QUARANTINED")
        before = self._snapshot(env)
        res = self._release(env, task_key=task_key, attempt_id=attempt_id)
        assert res.outcome is LeaseReleaseOutcome.NOT_RELEASABLE
        assert self._snapshot(env) == before
        assert before[4] == "QUARANTINED"

    def test_a5_rotating_not_releasable_even_owner_matches(self, env):
        task_key, attempt_id = _start(env, "rel-006")
        _set_lease_state(env.db, "p-fixed", "ROTATING")
        before = self._snapshot(env)
        res = self._release(env, task_key=task_key, attempt_id=attempt_id)
        assert res.outcome is LeaseReleaseOutcome.NOT_RELEASABLE
        assert self._snapshot(env) == before
        assert before[4] == "ROTATING"


# ---------------------------------------------------------------------------
# Case1-6 权威提交安全门
# ---------------------------------------------------------------------------

class TestResultCommitLeaseLifecycle:
    def test_case1_normal_completion_releases_and_next_starts(self, env):
        tk_a, att_a = _start(env, "A")
        cr = _commit(env, tk_a, att_a)  # remote_state=IDLE_VERIFIED
        assert cr.outcome is CommitOutcome.COMMITTED
        assert _task_state(env.db, tk_a) == "COMPLETED"
        assert _attempt_state(env.db, att_a) == "COMPLETED"
        assert _result_id_for(env.db, tk_a) is not None
        assert _count(env.db, "outbox") == 1
        assert _lease(env.db) is None
        assert "已安全释放" in cr.detail

        tk_b, att_b = _start(env, "B")  # 同项目，直接回归 T12 blocker
        assert _lease(env.db) is not None
        assert _lease(env.db).owner_attempt_id == att_b

    def test_case2_quarantined_lease_survives_completion(self, env):
        tk_a, att_a = _start(env, "A")
        _set_lease_state(env.db, "p-fixed", "QUARANTINED")
        cr = _commit(env, tk_a, att_a)
        assert cr.outcome is CommitOutcome.COMMITTED  # 合法结果仍提交
        assert _lease(env.db) is not None and _lease(env.db).state == "QUARANTINED"
        _claim(env, "B")
        r = env.scheduler.tick()
        assert r.outcome is ScheduleOutcome.BLOCKED
        assert r.block_reason is ScheduleBlockReason.PROJECT_BUSY

    def test_case3_rotating_lease_survives_completion(self, env):
        tk_a, att_a = _start(env, "A")
        _set_lease_state(env.db, "p-fixed", "ROTATING")
        cr = _commit(env, tk_a, att_a)
        assert cr.outcome is CommitOutcome.COMMITTED
        assert _lease(env.db) is not None and _lease(env.db).state == "ROTATING"
        _claim(env, "B")
        r = env.scheduler.tick()
        assert r.outcome is ScheduleOutcome.BLOCKED
        assert r.block_reason is ScheduleBlockReason.PROJECT_BUSY

    @pytest.mark.parametrize("remote_state", ["UNKNOWN", "BUSY", "MAYBE_RUNNING"])
    def test_remote_non_idle_never_releases(self, env, remote_state):
        tk_a, att_a = _start(env, "A")
        cr = _commit(env, tk_a, att_a, remote_state=remote_state)
        assert cr.outcome is CommitOutcome.COMMITTED
        assert _task_state(env.db, tk_a) == "COMPLETED"      # 本地终态达成
        assert _lease(env.db) is not None                     # 但 lease 必须保留
        assert _lease(env.db).state == "ACTIVE"
        assert "不释放" in cr.detail

    def test_case5_owner_mismatch_never_deletes_other_owner(self, env):
        # 先完成 X（p-other）形成合法的父行，再启动 A（p-fixed）
        tk_x, att_x = _start(env, "X", project_key="p-other")
        assert _commit(env, tk_x, att_x).outcome is CommitOutcome.COMMITTED
        tk_a, att_a = _start(env, "A", project_key="p-fixed")
        with env.db.transaction():
            env.db.connection.execute(
                "UPDATE project_leases SET owner_task_key=?, owner_attempt_id=?"
                " WHERE project_key='p-fixed'",
                (tk_x, att_x),
            )
        cr = _commit(env, tk_a, att_a)
        assert cr.outcome is CommitOutcome.COMMITTED           # 合法结果仍提交
        lease = _lease(env.db)
        assert lease is not None and lease.owner_attempt_id == att_x  # 绝不删除别人的
        assert "保留并绝不删除" in cr.detail

    def test_case6_epoch_mismatch_never_releases(self, env):
        tk_a, att_a = _start(env, "A")
        with env.db.transaction():
            env.db.connection.execute(
                "UPDATE project_leases SET authority_epoch=99 WHERE project_key='p-fixed'")
        cr = _commit(env, tk_a, att_a)
        assert cr.outcome is CommitOutcome.COMMITTED
        lease = _lease(env.db)
        assert lease is not None and lease.authority_epoch == 99
        assert "不匹配" in cr.detail

    @pytest.mark.parametrize("result_state", ["FAILED", "STOPPED_BY_USER"])
    def test_non_completed_terminal_keeps_lease(self, env, result_state):
        tk_a, att_a = _start(env, "A")
        svc, _ = _service(env)
        cr = svc.commit(_candidate(tk_a, att_a, state=result_state))
        assert cr.outcome is CommitOutcome.COMMITTED
        assert _task_state(env.db, tk_a) == result_state
        assert _lease(env.db) is not None and _lease(env.db).state == "ACTIVE"
        assert "未释放" in cr.detail

    def test_lease_absent_fresh_commit_still_succeeds(self, env):
        tk_a, att_a = _start(env, "A")
        with env.db.transaction():
            env.db.connection.execute(
                "DELETE FROM project_leases WHERE project_key='p-fixed'")
        result_id = f"res-no-lease-{att_a}"
        cr = _commit(env, tk_a, att_a, result_id=result_id)
        assert cr.outcome is CommitOutcome.COMMITTED           # 不因 lease 缺失回滚
        assert _task_state(env.db, tk_a) == "COMPLETED"
        assert _count(env.db, "results") == 1
        assert _count(env.db, "outbox") == 1
        again = _commit(env, tk_a, att_a, result_id=result_id)  # 幂等同样不因 lease 缺失报错
        assert again.outcome is CommitOutcome.ALREADY_COMMITTED

    def test_candidate_has_no_project_key_field(self, env):
        assert "project_key" not in CandidateResult.__dataclass_fields__

    def test_release_scoped_to_attempt_snapshot_project(self, env):
        # 释放目标只来自 Attempt 冻结快照：改变候选其它数据不影响目标，其它项目 lease 不受触碰。
        tk_a, att_a = _start(env, "A", project_key="p-fixed")
        with env.db.transaction():  # p-other 上的 owner 也是 A（两张父表均合法存在）
            env.db.connection.execute(
                "INSERT INTO project_leases (project_key, owner_task_key,"
                " owner_attempt_id, authority_epoch, state, related_sessions_json,"
                " last_verified_at, reason) VALUES ('p-other',?,?,'1','ACTIVE','[]',?,NULL)",
                (tk_a, att_a, _T0),
            )
        assert _lease(env.db, "p-fixed").owner_attempt_id == att_a
        assert _lease(env.db, "p-other").owner_attempt_id == att_a

        svc, _ = _service(env)
        candidate = _candidate(  # 候选 data 全变：换 result_id/正文/认领 message
            tk_a, att_a, result_id="res-other-data", body="与超时快照无关的新正文",
            message_id="m-candidate-a", source="MANUAL_WRAP",
        )
        cr = svc.commit(candidate)
        assert cr.outcome is CommitOutcome.COMMITTED
        assert _lease(env.db, "p-fixed") is None               # 快照指向项目已释放
        other = _lease(env.db, "p-other")                      # 候选无法改变目标：p-other 保留
        assert other is not None and other.owner_attempt_id == att_a
        assert "p-fixed" in cr.detail


# ---------------------------------------------------------------------------
# 事务故障回滚覆盖 release / 幂等 / 代际安全 / 多任务连续推进
# ---------------------------------------------------------------------------

class TestAtomicityAndProgress:
    def test_release_fault_rolls_back_everything(self, env, monkeypatch):
        tk_a, att_a = _start(env, "A")
        real = ProjectLeaseStore.release_active_in

        def faulty(self, conn, **kw):
            result = real(self, conn, **kw)
            assert result.outcome is LeaseReleaseOutcome.RELEASED
            conn.execute("INSERT INTO meta (key, value) VALUES ('next_sequence', 'fault')")
            return result

        monkeypatch.setattr(ProjectLeaseStore, "release_active_in", faulty)
        svc, _ = _service(env)
        with pytest.raises(sqlite3.IntegrityError):
            svc.commit(_candidate(tk_a, att_a, message_id="m-rollback"))

        assert _task_state(env.db, tk_a) == "ACTIVE"
        assert _attempt_state(env.db, att_a) == "OPEN"
        task_row = env.db.connection.execute(
            "SELECT current_result_revision, active_attempt_id FROM tasks WHERE task_key=?",
            (tk_a,)).fetchone()
        assert task_row[0] == 0                      # current_result_revision 复原
        assert task_row[1] == att_a                  # active_attempt_id 复原
        assert _count(env.db, "results") == 0
        assert _count(env.db, "outbox") == 0
        assert _count(env.db, "remote_claims") == 0
        assert _count(env.db, "events", "WHERE event_code='RESULT_COMMITTED'") == 0
        lease = _lease(env.db)
        assert lease is not None and lease.state == "ACTIVE"  # DELETE 已回滚

        monkeypatch.undo()                           # 移除故障后再提交必须成功且正常释放
        retry = svc.commit(_candidate(tk_a, att_a))
        assert retry.outcome is CommitOutcome.COMMITTED
        assert _lease(env.db) is None

    def test_idempotent_recommit_survives_deleted_lease(self, env):
        tk_a, att_a = _start(env, "A")
        first = _commit(env, tk_a, att_a, result_id="res-same")
        assert first.outcome is CommitOutcome.COMMITTED
        assert _lease(env.db) is None

        second = _commit(env, tk_a, att_a, result_id="res-same")
        assert second.outcome is CommitOutcome.ALREADY_COMMITTED
        assert _lease(env.db) is None                        # 不因无 lease 报错/重建
        assert _count(env.db, "results") == 1
        assert _count(env.db, "outbox") == 1
        assert _count(env.db, "events", "WHERE event_code='RESULT_COMMITTED'") == 1

    def test_old_worker_late_does_not_touch_new_owner(self, env):
        tk_a, att_a = _start(env, "A")
        assert _commit(env, tk_a, att_a).outcome is CommitOutcome.COMMITTED
        assert _lease(env.db) is None

        tk_b, att_b = _start(env, "B")                        # B 已取得新 lease
        assert _lease(env.db).owner_attempt_id == att_b

        # 旧 worker A 带着一个它尚未见过的新候选结果晚到（同 attempt/epoch）
        old = _commit(env, tk_a, att_a, result_id="res-old-worker-late")
        assert old.outcome is CommitOutcome.LOST_AUTHORITY
        lease = _lease(env.db)
        assert lease is not None  # B 的 lease 逐字段完整保留
        assert (lease.project_key, lease.owner_task_key, lease.owner_attempt_id,
                lease.authority_epoch, lease.state) == ("p-fixed", tk_b, att_b, 1, "ACTIVE")
        assert _task_state(env.db, tk_b) == "ACTIVE"
        assert _count(env.db, "results", "WHERE task_key=?", (tk_b,)) == 0
        assert _count(env.db, "results", "WHERE task_key=? AND result_id=?",
                      (tk_a, "res-old-worker-late")) == 0

    def test_five_same_project_tasks_advance_fifo(self, env):
        task_seq: list[int] = []
        for i in range(1, 6):
            tid = f"task-{i:03d}"
            assert _count(env.db, "attempts", "WHERE state='OPEN'") == 0
            tk, att = _start(env, tid)
            assert _count(env.db, "attempts", "WHERE state='OPEN'") == 1  # 一次仅一个 OPEN
            assert _count(env.db, "project_leases", "WHERE state='ACTIVE'") == 1
            task_seq.append(env.db.connection.execute(
                "SELECT sequence FROM tasks WHERE task_key=?", (tk,)).fetchone()[0])
            cr = _commit(env, tk, att)
            assert cr.outcome is CommitOutcome.COMMITTED, f"{tid} commit 失败：{cr.detail}"
            assert _count(env.db, "attempts", "WHERE state='OPEN'") == 0
            assert _count(env.db, "project_leases") == 0  # 正常完成后 lease row 消失
        assert task_seq == sorted(task_seq) and len(set(task_seq)) == 5  # 严格递增
        assert _count(env.db, "results") == 5
        assert _count(env.db, "outbox") == 5
        assert _count(env.db, "attempts", "WHERE state='OPEN'") == 0
        assert _count(env.db, "project_leases") == 0       # 无任何 project lease
        assert _count(env.db, "tasks", "WHERE state != 'COMPLETED'") == 0  # 全部 terminal

    def test_schema_unchanged_no_released_state(self, env):
        ddl = env.db.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='project_leases'"
        ).fetchone()[0]
        assert "RELEASED" not in ddl
        assert "ACTIVE','QUARANTINED','ROTATING'" in ddl
        migration_files = sorted(
            p.name for p in Path(__file__).resolve().parents[1].joinpath(
                "storage", "schema").glob("*.sql")
        )
        assert migration_files == ["001_initial.sql"], migration_files