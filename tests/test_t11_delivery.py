"""T11：R1 结果交付（Outbox → 不可变 Result → 剪贴板/文件导出）。

覆盖 S08（DB 提交成功后导出失败仍 COMPLETED、数据库回复可复制）、S09（恶意
task_id 不能写出 replies 目录）、D02（提交后复制前崩溃 → 重启补发，不重跑执行端），
以及 R1 语义（"已复制"≠"A 端已确认"、恰好一次副作用、剪贴板在事务外）。
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.delivery import (
    ClipboardWriteError,
    DeliveryOutcome,
    DeliveryResult,
    DeliveryService,
    FileReplyExport,
)
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


class FakeClipboard:
    """事务外剪贴板桩：记录写入并断言自己从不在数据库事务内执行。"""

    def __init__(self, db=None, *, failure: Exception | None = None) -> None:
        self.db = db
        self.failure = failure
        self.writes: list[str] = []
        self.saw_in_transaction = False

    def write_text(self, *, text: str) -> None:
        if self.db is not None and self.db.in_transaction:
            self.saw_in_transaction = True
        if self.failure is not None:
            raise self.failure
        self.writes.append(text)


class FailingReplyExport:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def export(self, *, response_text: str, task_key: str, result_id: str,
               revision: int) -> None:
        raise self.error


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t11.sqlite")
    db.open()
    tasks = TaskStore(db)
    ops = ResultStore(db)
    clock = FakeClock(wall=_WALL)
    counter = itertools.count(1)

    def attempt_factory(task_key: str) -> str:
        return f"attempt-{next(counter):03d}"

    scheduler = Scheduler(db, task_store=tasks, clock=clock,
                          attempt_id_factory=attempt_factory)
    yield db, tasks, ops, clock, scheduler
    db.close()


def _start(env, task_id: str = "t11-001", project_key: str = "p-fixed",
           session_id: str = "sess-fixed") -> tuple[str, str]:
    db, tasks, _, _, scheduler = env
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
    return task_key, result.attempt_id


def _commit_completed(env, task_key, attempt_id, *, task_id="t11-001"):
    db, _, ops, clock, _ = env
    service = ResultCommitService(
        db, result_store=ops, clock=clock,
        result_id_factory=(lambda t, a, e: f"res-{a}"),
        delivery_id_factory=lambda t, r: f"deliv-{r}",
        event_id_factory=lambda r: f"evt-{r}",
    )
    result = service.commit(CandidateResult(
        task_key=task_key, attempt_id=attempt_id, authority_epoch=1,
        result_state="COMPLETED", source="AUTO_RELAY",
        final_body="完成报告正文",
        result_id=f"res-{attempt_id}",
        claim=ResultClaim(endpoint="http://127.0.0.1:57123",
                          session_id="sess-fixed", message_id="msg-1"),
    ))
    assert result.outcome is CommitOutcome.COMMITTED
    return result


def _make_delivery(env, db=None, ops=None, clock=None, clipboard=None,
                   reply_export=None, task_id=None):
    base_db, _, base_ops, base_clock, _ = env
    return DeliveryService(
        db or base_db,
        result_store=ops or base_ops,
        clock=clock or base_clock,
        clipboard=clipboard,
        reply_export=reply_export,
    )


def _outbox_row(db, delivery_id: str):
    return db.connection.execute(
        "SELECT result_id, state, offered_at, offered_count FROM outbox"
        " WHERE delivery_id=?", (delivery_id,)).fetchone()


class TestD02:
    def test_d02_crash_after_commit_before_copy_redelivers_on_restart(self, env, tmp_path):
        db, _, ops, clock, _ = env
        task_key, attempt_id = _start(env)
        committed = _commit_completed(env, task_key, attempt_id)
        assert committed.revision == 1
        delivery_id = f"deliv-{committed.revision}"
        assert _outbox_row(db, delivery_id)[1] == "PENDING"

        # 提交后复制前崩溃：不 provide，直接关库模拟进程崩溃
        db.close()
        shown: list[str] = []

        class Sink:
            def write_text(self, *, text: str) -> None:
                shown.append(text)

        # 重启：新 Database 实例，绝不重跑执行端
        restarted = Database(db.path)
        restarted.open()
        try:
            restarted_ops = ResultStore(restarted)
            service = DeliveryService(restarted, result_store=restarted_ops,
                                      clock=clock, clipboard=Sink())
            first = service.provide_once()
            assert first.outcome is DeliveryOutcome.OFFERED
            assert shown[0] == first.response_text
            second = service.provide_once()
            assert second.outcome is DeliveryOutcome.NO_PENDING
            assert len(shown) == 1
            # 不重跑执行端：attempt 依然只有原一条且已是 COMPLETED
            count = restarted.connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE attempt_id=? AND state='COMPLETED'",
                (attempt_id,)).fetchone()[0]
            assert count == 1
            # 补发的文本与权威结果完全一致
            authoritative = restarted_ops.get_authoritative_for_task(task_key)
            assert shown[0] == authoritative.protocol_text
        finally:
            restarted.close()


class TestOfferSemantics:
    def test_copy_success_is_offered_not_acked(self, env):
        db, _, _, clock, _ = env
        task_key, attempt_id = _start(env)
        committed = _commit_completed(env, task_key, attempt_id)
        sink = FakeClipboard(db)
        service = _make_delivery(env, clock=clock, clipboard=sink)
        result = service.provide_once()
        assert result.outcome is DeliveryOutcome.OFFERED
        assert result.delivery_id == f"deliv-{committed.revision}"
        assert not sink.saw_in_transaction  # 剪贴板副作用绝不能在事务中
        assert len(sink.writes) == 1
        row = _outbox_row(db, result.delivery_id)
        assert row[1] == "OFFERED"  # 已是"提供过"，不是 ACKED/MANUAL_CONFIRMED

    def test_r1_offer_once_exactly_one_side_effect(self, env):
        db, _, _, clock, _ = env
        task_key, attempt_id = _start(env)
        _commit_completed(env, task_key, attempt_id)
        sink = FakeClipboard()
        service = _make_delivery(env, clock=clock, clipboard=sink)
        first = service.provide_once()
        assert first.outcome is DeliveryOutcome.OFFERED
        second = service.provide_once()
        assert second.outcome is DeliveryOutcome.NO_PENDING
        third = service.provide_once()
        assert third.outcome is DeliveryOutcome.NO_PENDING
        assert len(sink.writes) == 1

    def test_provide_no_pending(self, env):
        db, _, _, clock, _ = env
        sink = FakeClipboard()
        service = _make_delivery(env, clock=clock, clipboard=sink)
        result = service.provide_once()
        assert result.outcome is DeliveryOutcome.NO_PENDING
        assert len(sink.writes) == 0

    def test_response_text_binds_original_task(self, env):
        db, _, _, clock, _ = env
        task_key, attempt_id = _start(env)
        _commit_completed(env, task_key, attempt_id)
        sink = FakeClipboard()
        service = _make_delivery(env, clock=clock, clipboard=sink)
        result = service.provide_once()
        assert "IN_REPLY_TO: t11-001" in result.response_text
        assert "任务状态：COMPLETED" in result.response_text
        assert "结果版本：1" in result.response_text
        assert "结果来源：auto_relay" in result.response_text


class TestClipboardFailure:
    def test_clipboard_failure_keeps_authoritative_result(self, env):
        db, _, ops, clock, _ = env
        task_key, attempt_id = _start(env)
        committed = _commit_completed(env, task_key, attempt_id)
        failing = FakeClipboard(failure=ClipboardWriteError("剪贴板不可用"))
        service = _make_delivery(env, clock=clock, clipboard=failing)
        with pytest.raises(ClipboardWriteError):
            service.provide_once()
        # 权威结果/outbox 不受剪贴板失败影响，仍可重新提供
        assert _outbox_row(db, f"deliv-{committed.revision}")[1] == "PENDING"
        authoritative = ops.get_authoritative_for_task(task_key)
        assert authoritative is not None
        assert authoritative.state == "COMPLETED"  # 权威结果不受剪贴板失败影响
        good = FakeClipboard()
        service = _make_delivery(env, clock=clock, clipboard=good)
        retry = service.provide_once()
        assert retry.outcome is DeliveryOutcome.OFFERED
        assert len(good.writes) == 1


class TestS08Export:
    def test_s08_db_commit_ok_export_fail_still_completed(self, env, tmp_path):
        db, _, ops, clock, _ = env
        task_key, attempt_id = _start(env)
        committed = _commit_completed(env, task_key, attempt_id)
        failing_export = FailingReplyExport(OSError("磁盘已满"))
        sink = FakeClipboard()
        service = _make_delivery(env, clock=clock, clipboard=sink,
                                 reply_export=failing_export)
        result = service.provide_once()
        assert result.outcome is DeliveryOutcome.OFFERED
        assert result.export_error is not None
        assert len(sink.writes) == 1  # 数据库回复仍可复制
        authoritative = ops.get_authoritative_for_task(task_key)
        assert authoritative is not None
        assert authoritative.state == "COMPLETED"  # 导出失败不降级终态
        assert _outbox_row(db, f"deliv-{committed.revision}")[1] == "OFFERED"

    def test_delivery_export_not_required_by_default(self, env):
        db, _, _, clock, _ = env
        task_key, attempt_id = _start(env)
        _commit_completed(env, task_key, attempt_id)
        sink = FakeClipboard()
        service = _make_delivery(env, clock=clock, clipboard=sink)
        result = service.provide_once()
        assert result.outcome is DeliveryOutcome.OFFERED
        assert result.export_error is None

    def test_file_export_writes_immutable_response(self, env, tmp_path):
        from core.result_commit import build_result_response, sha256_hex

        db, _, ops, clock, _ = env
        task_key, attempt_id = _start(env)
        committed = _commit_completed(env, task_key, attempt_id)
        root = tmp_path / "replies"
        export = FileReplyExport(root)
        sink = FakeClipboard()
        service = _make_delivery(env, clock=clock, clipboard=sink,
                                 reply_export=export)
        result = service.provide_once()
        assert result.export_error is None
        authoritative = ops.get_authoritative_for_task(task_key)
        expected = build_result_response(
            result_state="COMPLETED", attempt_id=attempt_id, revision=1,
            source="AUTO_RELAY", remote_state="IDLE_VERIFIED",
            final_body="完成报告正文", in_reply_to="t11-001",
            response_message_id=f"res-{attempt_id}",
        )
        target = (root / sha256_hex(task_key)[:32]
                  / f"1_{f'res-{attempt_id}'}.response.txt")
        assert target.exists()
        assert target.read_text(encoding="utf-8") == expected
        assert result.response_text == expected

    @pytest.mark.parametrize("task_id", [
        "../../evil",
        r"..\..\evil",
        r"C:\Windows\Temp\x",
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "LPT1",
        "dir/name",
        "中文任务ID",
        "a:b",
        "x" * 300,
    ])
    def test_s09_malicious_task_id_cannot_escape_reply_dir(self, env, tmp_path, task_id):
        from core.result_commit import sha256_hex

        db, _, ops, clock, _ = env
        task_key, attempt_id = _start(env, task_id=task_id)
        committed = _commit_completed(env, task_key, attempt_id)
        assert committed.result_id == f"res-{attempt_id}"
        root = tmp_path / "replies"
        root.mkdir()
        export = FileReplyExport(root)
        sink = FakeClipboard()
        service = _make_delivery(env, clock=clock, clipboard=sink,
                                 reply_export=export)
        result = service.provide_once()
        assert result.outcome is DeliveryOutcome.OFFERED
        assert result.export_error is None
        # 原始 TaskId 原样保留在 IN_REPLY_TO，可回传；文件只落在 hash 目录下
        assert f"IN_REPLY_TO: {task_id}" in result.response_text
        indir = root / sha256_hex(task_key)[:32]
        assert indir.is_dir()
        files = list(indir.iterdir())
        assert len(files) == 1
        assert files[0].name == f"1_{f'res-{attempt_id}'}.response.txt"
        for entry in indir.iterdir():
            assert entry.resolve().is_relative_to(root.resolve())
        assert [p.name for p in root.iterdir()] == [sha256_hex(task_key)[:32]]
        names = sorted(p.name for p in tmp_path.iterdir())
        assert names == ["replies", "t11.sqlite", "t11.sqlite-shm", "t11.sqlite-wal"]

    def test_s09_unit_export_rejects_malicious_identifiers(self, tmp_path):
        from core.result_commit import sha256_hex

        root = tmp_path / "replies"
        root.mkdir()
        export = FileReplyExport(root)
        # 净化层直接拒绝：路径分隔符 / 反斜杠 / 冒号 / 点段逃逸
        for malicious_result_id in (
            "../../pwn", r"..\..\pwn", r".\oops\..\pwn2",
            r"C:\Windows\Temp\x", "a/b",
        ):
            with pytest.raises(ResultStoreError):
                export.export(response_text="R", task_key="x",
                              result_id=malicious_result_id, revision=1)
        assert not (tmp_path / "pwn").exists()
        assert not (tmp_path / "pwn2").exists()
        # task_key 只进哈希；Windows 保留名/路径名/中文作为 task_key 永不逃逸
        for hostile_task in ("CON", r"C:\Windows\Temp\x", r"..\..\evil", "中文任务ID"):
            export.export(response_text="R", task_key=hostile_task,
                          result_id="res-ok", revision=1)
            d = root / sha256_hex(hostile_task)[:32]
            files = list(d.iterdir())
            assert len(files) == 1
            written = files[0].resolve()
            assert written.is_relative_to(root.resolve())
            assert written.parent == d
            assert written.read_text(encoding="utf-8") == "R"
        # 保留名作为 result_id 只能出现在带前缀的安全文件名中
        for reserved in ("CON", "PRN", "AUX", "NUL", "COM1", "LPT1"):
            export.export(response_text="R", task_key="y",
                          result_id=reserved, revision=1)
            d = root / sha256_hex("y")[:32]
            assert (d / f"1_{reserved}.response.txt").read_text(encoding="utf-8") == "R"


class TestMarkFailed:
    def test_mark_failed_after_clipboard_success_leaves_recoverable(self, env):
        db, _, ops, clock, _ = env
        task_key, attempt_id = _start(env)
        committed = _commit_completed(env, task_key, attempt_id)
        sink = FakeClipboard()
        ops._fault_step = 0
        ops.fault_inject_after = 1  # mark_offered 的第一个写检查点故障注入
        service = DeliveryService(db, result_store=ops, clock=clock,
                                  clipboard=sink)
        result = service.provide_once()
        assert result.outcome is DeliveryOutcome.MARK_FAILED
        assert len(sink.writes) == 1  # 剪贴板副作用发生过一次，绝不自动重写
        assert _outbox_row(db, f"deliv-{committed.revision}")[1] == "PENDING"
        authoritative = ops.get_authoritative_for_task(task_key)
        assert authoritative is not None
        assert authoritative.state == "COMPLETED"  # Result/Task/Attempt 不变
        # 显式恢复：清除故障后再提供成功，且补发同一文本
        ops._fault_step = 0
        ops.fault_inject_after = None
        good = FakeClipboard()
        retried = DeliveryService(db, result_store=ops, clock=clock,
                                  clipboard=good).provide_once()
        assert retried.outcome is DeliveryOutcome.OFFERED
        assert len(good.writes) == 1
        assert retried.response_text == sink.writes[0]