"""T12：Fake 端到端闭环矩阵（100 任务 FIFO + 重复压力 + 重启边界 + R1 交付）。

覆盖：
- Q01：100 个任务认领 sequence 1..100 严格递增；执行发送顺序 === 入队顺序；
- P04：TASK-010×20 / TASK-050×10 / TASK-099×5 重复 → 全 EXISTING，计数不变；
- 终态验收：100 task/attempt COMPLETED、100 条 ACCEPTED INITIAL_SEND、
  100 results（revision=1 权威）、100 RESULT_COMMITTED、100 条 outbox OFFERED、
  0 OPEN attempt、0 ACTIVE lease；每任务 send_calls 恰 1；
- 重启边界：
  A 认领前重启：恰好 1 个 attempt、1 次发送，绝无重复调度；
  B schedule 后 send 前重启：新进程恰 1 次发送并收敛 COMMITTED；
  C ACCEPTED 后 commit 前重启（完成读取故障断电）：重启修复后无重发、确定性重建提交；
  D commit 后 delivery 前重启：重启后提供同一不可变 protocol_text；
- R1：逐条 OFFERED，写出的文本逐字等于权威 protocol_text，且顺序与任务入队一致。
"""

from __future__ import annotations

import itertools
import re
from datetime import datetime, timezone

import pytest

from adapters.fake_executor import FakeExecutor, FakeScript
from app.commands import ReceiveOutcomeKind
from app.controller import (
    AppController,
    DEFAULT_ENDPOINT,
    CycleKind,
    OfferKind,
)
from core.scheduler import ScheduleOutcome
from core.settings_service import SettingsService
from core.domain import SettingsDraft
from infra.clock import FakeClock
from storage.database import Database
from storage.result_store import ResultStore
from storage.settings_store import SettingsStore
from storage.task_store import TaskStore

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
_ENDPOINT = DEFAULT_ENDPOINT


def _v1(task_id: str, body: str | None = None) -> str:
    return _V1.format(task_id=task_id, body=body or f"处理任务 {task_id}")


class FakeClipboard:
    def __init__(self) -> None:
        self.text_value = ""
        self.writes: list[str] = []

    def text(self) -> str:
        return self.text_value

    def setText(self, text: str) -> None:
        self.text_value = text

    def write_text(self, *, text: str) -> None:
        self.writes.append(text)
        self.text_value = text


def _make_env(tmp_path, opened: list):
    counter = itertools.count(1)

    def attempt_factory(task_key: str) -> str:
        return f"attempt-{next(counter):03d}"

    def build(*, completion_read_fault=False, db=None, settings=None):
        if db is None:
            db = Database(tmp_path / f"e{len(opened) + 1}.sqlite")
            db.open()
            opened.append(db)
        clock = FakeClock(wall=_WALL)
        if settings is None:
            settings_store = SettingsStore(db)
            settings = SettingsService(settings_store, clock=clock)
            proj = tmp_path / "proj"
            proj.mkdir(exist_ok=True)
            draft = SettingsDraft.defaults()
            draft["openchamber"]["url"] = _ENDPOINT
            draft["openchamber"]["directory"] = str(proj)
            draft["openchamber"]["session_id"] = "sess-fixed"
            draft["openchamber"]["session_policy"] = "FIXED_SESSION"
            settings.submit_draft(draft, base_revision=None)
        cb = FakeClipboard()
        ex = FakeExecutor(
            FakeScript.ACCEPT_AND_COMPLETE, db=db,
            completion_read_fault=completion_read_fault)
        ctl = AppController(
            db, settings=settings, clipboard=cb, clock=clock, executor=ex,
            attempt_id_factory=attempt_factory,
        )
        return {"db": db, "ctl": ctl, "clipboard": cb, "executor": ex,
                "settings": settings}

    return build


@pytest.fixture
def make_env(tmp_path):
    opened: list[Database] = []
    builder = _make_env(tmp_path, opened)
    yield builder
    for db in opened:
        try:
            db.close()
        except Exception:  # noqa: BLE001 - 清理阶段失败不掩盖测试结果
            pass


def _count(db, sql, params=()):
    return int(db.connection.execute(sql, params).fetchone()[0])


def _task_id_of(payload: dict) -> str:
    return payload["task_key"].rsplit(":", 1)[-1]


class TestHundredTaskFifo:
    def test_full_chain(self, make_env):
        env = make_env()
        ctl = env["ctl"]
        clipboard = env["clipboard"]

        # 1) 100 个任务认领，sequence 严格 1..100（Q01 入队顺序）
        seqs = []
        for i in range(100):
            raw = _v1(f"TASK-{i:03d}")
            clipboard.setText(raw)
            res = ctl.accept_task(raw)
            assert res.kind is ReceiveOutcomeKind.ACCEPTED
            seqs.append(res.sequence)
        assert seqs == list(range(1, 101))
        assert ctl.stats()["tasks"] == 100
        assert ctl.stats()["open_attempts"] == 0

        # 2) P04 重复压力：计数不变、全 EXISTING
        for tid, n in [("TASK-010", 20), ("TASK-050", 10), ("TASK-099", 5)]:
            for _ in range(n):
                res = ctl.accept_task(_v1(tid))
                assert res.kind is ReceiveOutcomeKind.EXISTING
        assert ctl.stats()["tasks"] == 100

        # 3) 逐任务 FIFO 调度并闭环
        for i in range(100):
            started = ctl.tick_once()
            assert started.outcome is ScheduleOutcome.STARTED
            cycled = ctl.run_one_fake_cycle()
            assert cycled.kind is CycleKind.COMMITTED
            assert cycled.send_calls == 1
            assert cycled.revision == 1
            assert cycled.result_id is not None

        stats = ctl.stats()
        assert stats["terminal_tasks"] == 100
        assert stats["open_attempts"] == 0
        assert stats["results"] == 100
        assert stats["outbox_pending"] == 100
        assert stats["outbox_offered"] == 0
        assert stats["committed_events"] == 100
        assert stats["leases"] == 0
        assert stats["init_send_accepted"] == 100
        assert stats["total_send_calls"] == 100
        assert set(env["executor"].send_calls_by_task.values()) == {1}
        assert env["executor"].remote_received is True

        db = env["db"]
        assert _count(db, "SELECT COUNT(*) FROM attempts WHERE state='COMPLETED'") == 100
        assert _count(db, "SELECT COUNT(*) FROM tasks WHERE state='COMPLETED'") == 100
        assert _count(db, "SELECT COUNT(*) FROM results WHERE state='COMPLETED'") == 100

        # Q01 执行顺序 === 入队顺序
        sent = [_task_id_of(p) for p in env["executor"].sent_payloads]
        assert sent == [f"TASK-{i:03d}" for i in range(100)]

        # 4) R1：逐条提供；文本逐字等于当前队首权威结果，且 100 条互不遗漏
        offered_ids: set[str] = set()
        in_reply_to = re.compile(r"IN_REPLY_TO: (TASK-\d{3})")
        for _ in range(100):
            head = ResultStore(db).list_pending_deliveries(limit=1)
            authoritative = ResultStore(db).get_result_by_id(
                head[0].result_id).protocol_text
            offered = ctl.offer_next_result()
            assert offered.kind is OfferKind.OFFERED
            assert offered.response_text == authoritative == clipboard.writes[-1]
            match = in_reply_to.search(clipboard.writes[-1])
            assert match is not None
            offered_ids.add(match.group(1))
            assert ctl.confirm_local_delivery().confirmed_count == 1
        assert offered_ids == {f"TASK-{i:03d}" for i in range(100)}
        assert ctl.stats()["outbox_pending"] == 0
        assert ctl.stats()["outbox_offered"] == 100
        assert len(clipboard.writes) == 100


class TestRestartBoundaries:
    def test_boundary_a_before_claim(self, make_env):
        # 重启发生在调度之前：绝不产生重复 attempt/重复发送
        env = make_env()
        _accept(env, "TASK-BA")
        assert _count(env["db"], "SELECT COUNT(*) FROM attempts") == 0
        fresh = make_env(db=env["db"], settings=env["settings"])
        started = fresh["ctl"].tick_once()
        assert started.outcome is ScheduleOutcome.STARTED
        cycled = fresh["ctl"].run_one_fake_cycle()
        assert cycled.kind is CycleKind.COMMITTED
        assert cycled.send_calls == 1
        db = fresh["db"]
        assert _count(db, "SELECT COUNT(*) FROM attempts") == 1
        assert _count(db, "SELECT COUNT(*) FROM operations") == 1
        assert fresh["executor"].send_calls == 1
        assert env["executor"].send_calls == 0  # 旧进程从未发送

    def test_boundary_b_after_schedule_before_send(self, make_env):
        # 重启在 schedule 之后、send 之前：新进程恰 1 次发送并收敛
        env = make_env()
        _accept(env, "TASK-BB")
        started = env["ctl"].tick_once()
        assert started.outcome is ScheduleOutcome.STARTED
        assert env["executor"].send_calls == 0
        fresh = make_env(db=env["db"], settings=env["settings"])
        cycled = fresh["ctl"].run_one_fake_cycle()
        assert cycled.kind is CycleKind.COMMITTED
        assert cycled.send_calls == 1
        assert fresh["executor"].send_calls == 1
        db = fresh["db"]
        assert _count(db, "SELECT COUNT(*) FROM operations") == 1
        assert _count(db,
                      "SELECT COUNT(*) FROM operations WHERE state='ACCEPTED'") == 1
        assert _count(db, "SELECT COUNT(*) FROM attempts") == 1

    def test_boundary_c_after_accept_before_commit(self, make_env):
        # 发送已 ACCEPTED 但从未提交 → 断电重启：修复后无重发、确定性重建提交
        env = make_env(completion_read_fault=True)
        _accept(env, "TASK-BC")
        ctl = env["ctl"]
        started = ctl.tick_once()
        assert started.outcome is ScheduleOutcome.STARTED
        failed = ctl.run_one_fake_cycle()
        assert failed.kind is CycleKind.COMPLETION_READ_FAILED
        assert env["executor"].send_calls == 1
        assert ctl.stats()["results"] == 0  # 权威尚未落库
        fresh = make_env(db=env["db"], settings=env["settings"])
        healed = fresh["ctl"].run_one_fake_cycle()
        assert healed.kind is CycleKind.COMMITTED
        assert healed.send_calls == 0  # 重启后绝不重发
        assert fresh["executor"].send_calls == 0
        assert fresh["ctl"].stats()["results"] == 1

    def test_boundary_d_after_commit_before_delivery(self, make_env):
        # 提交完成、交付前重启：重启后提供同一不可变 protocol_text
        env = make_env()
        _accept(env, "TASK-BD")
        ctl = env["ctl"]
        ctl.tick_once()
        assert ctl.run_one_fake_cycle().kind is CycleKind.COMMITTED
        db = env["db"]
        task_key = env["db"].connection.execute(
            "SELECT task_key FROM tasks WHERE task_id='TASK-BD'"
        ).fetchone()[0]
        authoritative = ResultStore(db).get_authoritative_for_task(task_key)
        fresh = make_env(db=db, settings=env["settings"])
        offered = fresh["ctl"].offer_next_result()
        assert offered.kind is OfferKind.OFFERED
        assert offered.response_text == authoritative.protocol_text
        assert fresh["clipboard"].writes[0] == authoritative.protocol_text


def _accept(env, task_id: str) -> str:
    raw = _v1(task_id)
    env["clipboard"].setText(raw)
    res = env["ctl"].accept_task(raw)
    assert res.kind is ReceiveOutcomeKind.ACCEPTED
    return raw