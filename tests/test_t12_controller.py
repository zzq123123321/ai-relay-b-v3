"""T12：AppController 显式闭环步骤（Fake 执行端 + 确定性提交 + R1 提供）。

覆盖：
- accept_task / tick_once / run_one_fake_cycle 的每个结构化结论；
- 失败分层：REJECTED/UNKNOWN 绝不自动重发、绝不冒充足完成；FAIL_COMPLETION_READ
  修复后无重发重建；提交存储故障（注入同意术 IntegrityError）→ COMMIT_STORE_ERROR，
  整事务回滚后无重发幂等收敛；
- D01：未确认的 OFFERED 阻塞再次提供，confirm 后放行，重启后安全侧失败；
- D08：剪贴板存在未持久化入站任务（QUEUE_FULL/ERROR/CONFLICT）时拒绝覆盖；
- offer 语义：NO_PENDING / MARK_FAILED / CLIPBOARD_WRITE_FAILED，写副作用恰一次；
- 提供文本即权威不可变 protocol_text（逐字一致）。
"""

from __future__ import annotations

import itertools
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
from core.delivery import ClipboardWriteError, DeliveryService
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
_RESPONSE = (
    "AI_RELAY/1\n"
    "MESSAGE_ID: {task_id}\n"
    "IN_REPLY_TO: t1\n"
    "SOURCE: EXECUTOR\n"
    "TARGET: CHATGPT\n"
    "TYPE: RESPONSE\n"
    "\n"
    "完成"
)
_T0 = "2026-10-01T09:00:00+00:00"
_WALL = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)
_ENDPOINT = DEFAULT_ENDPOINT


def _v1(task_id: str, body: str | None = None) -> str:
    return _V1.format(task_id=task_id, body=body or f"处理任务 {task_id}")


def _response(task_id: str) -> str:
    return _RESPONSE.format(task_id=task_id)


class FakeSharedClipboard:
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


class FailingClipboard(FakeSharedClipboard):
    def write_text(self, *, text: str) -> None:
        raise ClipboardWriteError("模拟剪贴板不可用")


@pytest.fixture
def make_env(tmp_path):
    counter = itertools.count(1)

    def attempt_factory(task_key: str) -> str:
        return f"attempt-{next(counter):03d}"

    opened: list[Database] = []

    def build(*, script=FakeScript.ACCEPT_AND_COMPLETE,
              completion_read_fault=False, queue_capacity=None,
              task_fault_step=None, delivery=None, clipboard=None, db=None,
              settings=None):
        if db is None:
            db = Database(tmp_path / f"c{len(opened) + 1}.sqlite")
            db.open()
            opened.append(db)
        else:
            db = db
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

        tasks = (TaskStore(db, queue_capacity=queue_capacity)
                 if queue_capacity is not None else TaskStore(db))
        if task_fault_step is not None:
            tasks.fault_inject_after = task_fault_step

        cb = clipboard if clipboard is not None else FakeSharedClipboard()
        executor = FakeExecutor(script, db=db,
                                completion_read_fault=completion_read_fault)
        ctl = AppController(
            db, settings=settings, clipboard=cb, clock=clock, executor=executor,
            task_store=tasks, attempt_id_factory=attempt_factory, delivery=delivery,
        )
        return {"db": db, "ctl": ctl, "clipboard": cb,
                "executor": executor, "settings": settings, "tasks": tasks}

    yield build
    for db in opened:
        try:
            db.close()
        except Exception:  # noqa: BLE001 - 清理阶段失败不掩盖测试结果
            pass


def _accept(env, task_id: str, body: str | None = None):
    ctl, clipboard = env["ctl"], env["clipboard"]
    raw = _v1(task_id, body)
    clipboard.setText(raw)
    result = ctl.accept_task(raw)
    assert result.kind in (ReceiveOutcomeKind.ACCEPTED, ReceiveOutcomeKind.EXISTING)
    return raw


def _start(env):
    ctl = env["ctl"]
    started = ctl.tick_once()
    assert started.outcome is ScheduleOutcome.STARTED
    return started, ctl


def _count(db, sql, params=()):
    return int(db.connection.execute(sql, params).fetchone()[0])


def _task_key_of(db, task_id: str) -> str:
    row = db.connection.execute(
        "SELECT task_key FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    assert row is not None
    return row[0]


class TestAccept:
    def test_accept_new_and_duplicate(self, make_env):
        env = make_env()
        _accept(env, "c-001")
        assert env["ctl"].stats()["tasks"] == 1
        _accept(env, "c-001")
        assert env["ctl"].stats()["tasks"] == 1  # 同 ID 同内容 → EXISTING，不重复

    def test_accept_response_is_ignored_not_a_task(self, make_env):
        env = make_env()
        result = env["ctl"].accept_task(_response("r1"))
        assert result.kind is ReceiveOutcomeKind.IGNORED_NOT_A_TASK
        assert env["ctl"].stats()["tasks"] == 0


class TestCycle:
    def test_no_open_attempt(self, make_env):
        env = make_env()
        result = env["ctl"].run_one_fake_cycle()
        assert result.kind is CycleKind.NO_OPEN_ATTEMPT

    def test_full_happy_cycle(self, make_env):
        env = make_env()
        _accept(env, "c-happy")
        _, ctl = _start(env)
        sent = ctl.run_one_fake_cycle()
        assert sent.kind is CycleKind.COMMITTED
        assert sent.send_calls == 1
        stats = ctl.stats()
        assert stats["results"] == 1
        assert stats["outbox_pending"] == 1
        assert stats["outbox_offered"] == 0
        assert stats["committed_events"] == 1
        assert stats["terminal_tasks"] == 1
        assert stats["leases"] == 0  # COMPLETED+IDLE_VERIFIED → 同事务安全释放
        assert _count(env["db"],
                      "SELECT COUNT(*) FROM operations WHERE state='ACCEPTED'") == 1
        # 已终态 → 无 OPEN attempt，也无 QUEUED 任务
        assert ctl.tick_once().outcome is ScheduleOutcome.NO_TASK
        assert ctl.run_one_fake_cycle().kind is CycleKind.NO_OPEN_ATTEMPT


class TestFailureLayering:
    def test_rejected_no_commit_and_no_resend(self, make_env):
        env = make_env(script=FakeScript.REJECT_BEFORE_SIDE_EFFECT)
        _accept(env, "c-reject")
        _, ctl = _start(env)
        first = ctl.run_one_fake_cycle()
        assert first.kind is CycleKind.REJECTED_NO_COMMIT
        assert first.send_calls == 1
        assert ctl.stats()["results"] == 0
        assert _count(env["db"],
                      "SELECT COUNT(*) FROM operations WHERE state='REJECTED'") == 1
        second = ctl.run_one_fake_cycle()
        assert second.kind is CycleKind.REJECTED_NO_COMMIT
        assert second.send_calls == 0  # 绝不第二次 POST
        assert env["executor"].send_calls == 1

    def test_unknown_no_commit_and_no_resend(self, make_env):
        env = make_env(script=FakeScript.UNKNOWN_AFTER_SIDE_EFFECT)
        _accept(env, "c-unknown")
        _, ctl = _start(env)
        first = ctl.run_one_fake_cycle()
        assert first.kind is CycleKind.UNKNOWN_NO_COMMIT
        assert first.send_calls == 1
        assert ctl.stats()["results"] == 0
        assert _count(env["db"],
                      "SELECT COUNT(*) FROM operations WHERE state='UNKNOWN'") == 1
        second = ctl.run_one_fake_cycle()
        assert second.kind is CycleKind.UNKNOWN_NO_COMMIT
        assert second.send_calls == 0
        assert env["executor"].send_calls == 1

    def test_fail_completion_read_then_heal_without_resend(self, make_env):
        env = make_env(script=FakeScript.FAIL_COMPLETION_READ,
                       completion_read_fault=True)
        _accept(env, "c-fault")
        _, ctl = _start(env)
        first = ctl.run_one_fake_cycle()
        assert first.kind is CycleKind.COMPLETION_READ_FAILED
        assert first.send_calls == 1
        assert ctl.stats()["results"] == 0  # 已接受，但绝无冒充足完成
        assert _count(env["db"],
                      "SELECT COUNT(*) FROM operations WHERE state='ACCEPTED'") == 1
        env["executor"].completion_read_fault = False
        healed = ctl.run_one_fake_cycle()
        assert healed.kind is CycleKind.COMMITTED
        assert healed.send_calls == 0  # 修复后无重发，复用同一确定性完成
        assert healed.result_id is not None
        assert ctl.stats()["results"] == 1

    def test_commit_store_error_then_heal_without_resend(self, make_env):
        env = make_env()
        _accept(env, "c-commit")
        _, ctl = _start(env)
        ops = ctl.commit.result_store
        ops.fault_inject_after = 1  # 提交事务第一个写检查点撞 meta 主键 → 整体回滚
        failed = ctl.run_one_fake_cycle()
        assert failed.kind is CycleKind.COMMIT_STORE_ERROR
        assert failed.send_calls == 1
        assert ctl.stats()["results"] == 0  # 无半条结果
        ops._fault_step = 0
        ops.fault_inject_after = None
        healed = ctl.run_one_fake_cycle()
        assert healed.kind is CycleKind.COMMITTED
        assert healed.send_calls == 0  # 不重发，幂等收敛到同一确定性 result_id
        assert ctl.stats()["results"] == 1


class TestOffer:
    def test_no_pending_when_nothing_committed(self, make_env):
        env = make_env()
        result = env["ctl"].offer_next_result()
        assert result.kind is OfferKind.NO_PENDING
        assert result.write_calls == 0

    def test_offered_exactly_once_then_blocked_until_confirm(self, make_env):
        env = make_env()
        _accept(env, "c-offer")
        _, ctl = _start(env)
        ctl.run_one_fake_cycle()
        first = ctl.offer_next_result()
        assert first.kind is OfferKind.OFFERED
        assert first.write_calls == 1
        assert len(env["clipboard"].writes) == 1
        # D01：未确认的 OFFERED 阻塞第二次提供
        second = ctl.offer_next_result()
        assert second.kind is OfferKind.BLOCKED_UNCONFIRMED_OFFERED
        assert second.write_calls == 0
        confirmed = ctl.confirm_local_delivery()
        assert confirmed.confirmed_count == 1
        assert ctl.confirmed_offered == frozenset({first.delivery_id})
        # 已确认 → 不再阻塞；没有更多 PENDING → NO_PENDING
        third = ctl.offer_next_result()
        assert third.kind is OfferKind.NO_PENDING
        assert third.write_calls == 0

    def test_d01_reset_after_restart_is_fail_safe(self, make_env):
        env = make_env()
        _accept(env, "c-d01")
        _, ctl = _start(env)
        ctl.run_one_fake_cycle()
        assert ctl.offer_next_result().kind is OfferKind.OFFERED
        ctl.confirm_local_delivery()
        # 重启：进程内确认丢失，同一 DB 上 OFFERED 仍在 outbox → 必须安全侧失败
        fresh = make_env(db=env["db"], settings=env["settings"])
        blocked = fresh["ctl"].offer_next_result()
        assert blocked.kind is OfferKind.BLOCKED_UNCONFIRMED_OFFERED
        assert blocked.write_calls == 0

    def test_mark_failed_leaves_pending_recoverable(self, make_env):
        env = make_env()
        raw = _accept(env, "c-mark")
        _, ctl = _start(env)
        ctl.run_one_fake_cycle()
        # 独立 ResultStore 在 mark_offered 落库时故障注入 → 透传为 MARK_FAILED
        ops = ResultStore(env["db"])
        ops.fault_inject_after = 1
        delivery = DeliveryService(env["db"], result_store=ops,
                                   clock=FakeClock(wall=_WALL),
                                   clipboard=env["clipboard"])
        ctl2 = AppController(
            env["db"], settings=env["settings"], clipboard=env["clipboard"],
            clock=FakeClock(wall=_WALL), executor=FakeExecutor(), delivery=delivery,
        )
        result = ctl2.offer_next_result()
        assert result.kind is OfferKind.MARK_FAILED
        assert result.write_calls == 1  # 剪贴板副作用已发生一次，绝不自动重写
        assert ctl2.stats()["outbox_pending"] == 1
        # 清除故障后显式补发。剪贴板此刻是本端已写入的 RESPONSE：新进程无法证明其
        # 「非待处理入站」，按 D08 安全侧失败 → 操作者重新贴回已持久入站任务文本。
        good = AppController(
            env["db"], settings=env["settings"], clipboard=env["clipboard"],
            clock=FakeClock(wall=_WALL), executor=FakeExecutor(),
            delivery=DeliveryService(env["db"], result_store=ResultStore(env["db"]),
                                     clock=FakeClock(wall=_WALL),
                                     clipboard=env["clipboard"]),
        )
        blocked = good.offer_next_result()
        assert blocked.kind is OfferKind.BLOCKED_INBOUND_NOT_PERSISTED
        assert blocked.write_calls == 0
        env["clipboard"].setText(raw)  # 操作者重现已持久化入站 → EXISTING → 放行
        ops._fault_step = 0
        ops.fault_inject_after = None
        offered = good.offer_next_result()
        assert offered.kind is OfferKind.OFFERED
        assert offered.response_text == env["clipboard"].writes[0]

    def test_clipboard_write_failure_reported(self, make_env):
        env = make_env(clipboard=FailingClipboard())
        _accept(env, "c-writefail")
        _, ctl = _start(env)
        ctl.run_one_fake_cycle()
        result = ctl.offer_next_result()
        assert result.kind is OfferKind.CLIPBOARD_WRITE_FAILED
        assert result.write_calls == 1
        assert ctl.stats()["outbox_pending"] == 1  # 权威结果完好，可重试

    def test_offered_text_is_immutable_authoritative_protocol_text(self, make_env):
        env = make_env()
        _accept(env, "c-immut")
        _, ctl = _start(env)
        ctl.run_one_fake_cycle()
        result = ctl.offer_next_result()
        assert result.kind is OfferKind.OFFERED
        task_key = _task_key_of(env["db"], "c-immut")
        auth = ResultStore(env["db"]).get_authoritative_for_task(task_key)
        assert auth is not None
        assert result.response_text == auth.protocol_text
        assert env["clipboard"].writes[0] == auth.protocol_text  # 逐字一致，不可变


class TestD08:
    def _commit_pending(self, env, task_id: str) -> str:
        raw = _accept(env, task_id)
        _, ctl = _start(env)
        assert ctl.run_one_fake_cycle().kind is CycleKind.COMMITTED
        return raw

    def test_d08_queue_full_blocks_offer(self, make_env):
        #（必需场景 1）合法新 TASK + 存储故障/占用不足：未持久 → 阻塞，write=0
        env = make_env(queue_capacity=1)
        _accept(env, "d08-q")  # 占用已满，且已持久化
        env["clipboard"].setText(_v1("d08-unclaimed", "占满配额后的新任务"))
        result = env["ctl"].offer_next_result()
        assert result.kind is OfferKind.BLOCKED_INBOUND_NOT_PERSISTED
        assert result.write_calls == 0

    def test_d08_error_blocks_offer(self, make_env):
        #（必需场景 1）storage fault：认领失败（未提交）→ ERROR → 阻塞
        env = make_env(task_fault_step=1)  # 下一次认领必然失败（未提交）→ ERROR
        unclaimed = _v1("d08-error", "认领会失败的任务")
        env["clipboard"].setText(unclaimed)
        result = env["ctl"].offer_next_result()
        assert result.kind is OfferKind.BLOCKED_INBOUND_NOT_PERSISTED
        assert result.write_calls == 0
        assert env["ctl"].stats()["tasks"] == 0  # 未持久化，绝不覆盖
        assert env["clipboard"].text() == unclaimed  # 原始输入保持不动

    def test_d08_conflict_blocks_offer(self, make_env):
        env = make_env()
        _accept(env, "d08-c", body="第一版内容")
        env["clipboard"].setText(_v1("d08-c", body="第二版冲突内容"))
        result = env["ctl"].offer_next_result()
        assert result.kind is OfferKind.BLOCKED_INBOUND_NOT_PERSISTED
        assert result.write_calls == 0

    def test_d08_malformed_relay_blocks_and_preserves(self, make_env):
        #（必需场景 2）Relay-looking 但 malformed TASK：IGNORED_BAD_PROTOCOL → 阻塞
        env = make_env()
        self._commit_pending(env, "d08-mal")
        env["clipboard"].setText("AI_RELAY/1\n残\n")  # 协议损坏/未完整复制
        result = env["ctl"].offer_next_result()
        assert result.kind is OfferKind.BLOCKED_INBOUND_NOT_PERSISTED
        assert result.write_calls == 0
        assert env["clipboard"].text() == "AI_RELAY/1\n残\n"  # 原始输入保持不动

    def test_d08_external_response_blocks_offer(self, make_env):
        #（必需场景 3）外来的 TYPE RESPONSE / 非可入站 TASK 的 Relay envelope：
        # 本端未写过的 RESPONSE 不可按「普通文本」误判 → 必须阻塞
        env = make_env()
        self._commit_pending(env, "d08-resp")
        env["clipboard"].setText(_response("external-1"))  # 模拟 A 端复制来的 RESPONSE
        result = env["ctl"].offer_next_result()
        assert result.kind is OfferKind.BLOCKED_INBOUND_NOT_PERSISTED
        assert result.write_calls == 0

    def test_d08_own_last_response_allows_continue(self, make_env):
        # 本端最近一次自写的 RESPONSE = 已持久出站 → 放行（供多结果顺序提供）
        env = make_env()
        self._commit_pending(env, "d08-self")
        env["clipboard"].setText(_v1("d08-s3"))
        first = env["ctl"].offer_next_result()
        assert first.kind is OfferKind.OFFERED
        assert first.write_calls == 1
        env["ctl"].confirm_local_delivery()
        # 剪贴板此刻是本端自写 RESPONSE：不被 D08 误杀，且没有更多 PENDING → NO_PENDING
        again = env["ctl"].offer_next_result()
        assert again.kind is OfferKind.NO_PENDING

    def test_d08_plain_text_allowed(self, make_env):
        #（必需场景 4）普通中文/URL/路径：明确非 Relay envelope → 可以正常覆盖
        env = make_env()
        self._commit_pending(env, "d08-plain")
        env["clipboard"].setText(r"普通中文和路径 D:\proj\存档.txt 还有 https://example.org/x")
        result = env["ctl"].offer_next_result()
        assert result.kind is OfferKind.OFFERED
        assert result.write_calls == 1
        assert env["clipboard"].text() == result.response_text

    def test_d08_persisted_inbound_does_not_block(self, make_env):
        #（必需场景 5）已 ACCEPTED/EXISTING 的 TASK：已持久 → 可以继续 offer
        env = make_env()
        _accept(env, "d08-ok")
        result = env["ctl"].offer_next_result()  # 剪贴板就是已持久化的任务
        assert result.kind in (OfferKind.NO_PENDING, OfferKind.OFFERED)
        assert result.write_calls in (0, 1)

    def test_d08_fix_then_persist_then_allow(self, make_env):
        #（必需场景 6）清除 fault/修复内容：先持久入站 → 再允许 offer
        env = make_env(task_fault_step=1)
        raw = _v1("d08-fix", "首轮认领会失败")
        env["clipboard"].setText(raw)
        env["ctl"].offer_next_result()  # fault → ERROR → 阻塞（未持久）
        env["tasks"].fault_inject_after = None
        result = env["ctl"].accept_task(raw)  # fault 清除后成功持久入站
        assert result.kind is ReceiveOutcomeKind.ACCEPTED
        allowed = env["ctl"].offer_next_result()  # 已 EXISTING → 放行
        assert allowed.kind in (OfferKind.NO_PENDING, OfferKind.OFFERED)