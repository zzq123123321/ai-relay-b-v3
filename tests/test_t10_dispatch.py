"""T10：发送两阶段调度与一次发送资格——Dispatch 层（主规格 07.2 / O02 / O03 / R01 / C19）。

命名直接对应正式验收：
- O02 快照失败 → send=0、operations=0、明确未发送；
- O03 POST 已到远端但客户端超时 → UNKNOWN，禁止第二次 POST（send_calls 不增）；
- R01 四来源并发续接 → 同一恢复轮一个 operation、最多一次 POST；
- C19 重复 timer/双击 → 同一 operation_key 只一次获取发送权；
- 停止先赢 / 发送权先赢、PREPARED/SENDING 崩溃差异、双请求单发送权、
  同 session 未决阻塞、transport 严格在事务外、快照/标记/哈希持久化。
"""

from __future__ import annotations

import itertools
import threading
from datetime import datetime, timezone

import pytest

from core.dispatch import (
    DispatchOutcome,
    DispatchProposal,
    DispatchService,
    SnapshotFailure,
    TransportTimeoutError,
    SendAttempt,
    SendOutcome,
    build_wire_prompt,
    derive_operation_id,
    sha256_hex,
)
from core.protocol_v1 import ProtocolFormat, content_digest, parse_message
from core.scheduler import ScheduleOutcome, Scheduler
from infra.clock import FakeClock
from storage.database import Database
from storage.operation_store import OperationStore, PrepareOutcome
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


class FakeMode(str):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    SNAPSHOT_FAIL = "SNAPSHOT_FAIL"
    TIMEOUT_AFTER_SIDE_EFFECT = "TIMEOUT_AFTER_SIDE_EFFECT"


class FakeTransport:
    """记录 snapshot/send 次数与载荷；枚举模式覆盖 ACCEPTED/REJECTED/SNAPSHOT_FAIL/
    TIMEOUT_AFTER_SIDE_EFFECT，并记录每次调用时 DB 是否处于事务中。"""

    def __init__(self, mode: str = FakeMode.ACCEPTED, *, db: Database | None = None,
                 remote_user_id: str = "user-alice-001") -> None:
        self.mode = mode
        self.db = db
        self.remote_user_id = remote_user_id
        self.snapshot_calls = 0
        self.send_calls = 0
        self.sent_payloads: list[dict] = []
        self.snapshot_in_txn: list[bool] = []
        self.send_in_txn: list[bool] = []
        self.remote_received = False

    def capture_pre_send_snapshot(self, *, endpoint: str, session_id: str,
                                  task_key: str) -> dict:
        self.snapshot_calls += 1
        if self.db is not None:
            self.snapshot_in_txn.append(self.db.in_transaction)
        if self.mode == FakeMode.SNAPSHOT_FAIL:
            raise SnapshotFailure("模拟发送前快照失败")
        return {"message_ids": ["m1", "m2"]}

    def send_once(self, *, endpoint: str, session_id: str, prompt_text: str,
                  operation_id: str,
                  planned_remote_user_id: str | None = None) -> SendAttempt:
        self.send_calls += 1
        if self.db is not None:
            self.send_in_txn.append(self.db.in_transaction)
        self.sent_payloads.append(
            {"endpoint": endpoint, "session_id": session_id, "prompt_text": prompt_text,
             "operation_id": operation_id,
             "planned_remote_user_id": planned_remote_user_id}
        )
        self.remote_received = True
        if self.mode == FakeMode.TIMEOUT_AFTER_SIDE_EFFECT:
            raise TransportTimeoutError("客户端超时（远端已收到）")
        if self.mode == FakeMode.REJECTED:
            return SendAttempt(outcome=SendOutcome.REJECTED,
                               evidence={"promptDispatched": False})
        return SendAttempt(outcome=SendOutcome.ACCEPTED,
                           remote_user_id=planned_remote_user_id or self.remote_user_id,
                           evidence={"message_id": "msg-1"})


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


def _start(dispatch_env, task_id: str = "t10-001", project_key: str = "p-fixed",
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


def _op_state(db, operation_key: str) -> str | None:
    row = db.connection.execute(
        "SELECT state FROM operations WHERE operation_key=?", (operation_key,)
    ).fetchone()
    return row[0] if row is not None else None


class TestDispatchBasics:
    def test_initial_send_accepted(self, dispatch_env):
        db, _, _, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        fake = FakeTransport(FakeMode.ACCEPTED, db=db)
        dispatch = DispatchService(db, clock=FakeClock(wall=_WALL))
        result = dispatch.dispatch_once(p, transport=fake)
        assert result.outcome is DispatchOutcome.ACCEPTED
        assert result.state == "ACCEPTED"
        assert fake.send_calls == 1
        record = dispatch._ops.read_by_key(p.operation_key)
        assert record is not None
        assert record.state == "ACCEPTED"
        assert record.remote_user_id  # T22-02：ACCEPTED 保留 planned remote identity
        assert record.remote_user_id.startswith("msg_")
        assert fake.sent_payloads[0]["planned_remote_user_id"] == record.remote_user_id
        assert 'message_ids' in record.pre_snapshot_json
        assert "[AI_RELAY_TASK_ID:" in record.prompt_text
        assert "[AI_RELAY_ATTEMPT_ID:" in record.prompt_text
        assert "[AI_RELAY_OPERATION_ID:" in record.prompt_text
        assert record.operation_id == derive_operation_id(p.operation_key)
        assert record.prompt_hash == sha256_hex(record.prompt_text)
        assert fake.sent_payloads[0]["prompt_text"] == record.prompt_text

    def test_transport_calls_strictly_outside_transaction(self, dispatch_env):
        db, _, _, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        fake = FakeTransport(FakeMode.ACCEPTED, db=db)
        dispatch = DispatchService(db, clock=FakeClock(wall=_WALL))
        dispatch.dispatch_once(p, transport=fake)
        assert fake.snapshot_calls == 1
        assert fake.send_calls == 1
        assert fake.snapshot_in_txn == [False]
        assert fake.send_in_txn == [False]

    def test_clear_rejection(self, dispatch_env):
        db, _, _, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        fake = FakeTransport(FakeMode.REJECTED, db=db)
        dispatch = DispatchService(db, clock=FakeClock(wall=_WALL))
        result = dispatch.dispatch_once(p, transport=fake)
        assert result.outcome is DispatchOutcome.REJECTED
        assert fake.send_calls == 1
        assert _op_state(db, p.operation_key) == "REJECTED"
        record = dispatch._ops.read_by_key(p.operation_key)
        assert '"promptDispatched": false' in record.evidence_json

    def test_session_unresolved_no_rows(self, dispatch_env):
        db, _, _, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project, session_id=None)
        fake = FakeTransport(FakeMode.ACCEPTED, db=db)
        dispatch = DispatchService(db, clock=FakeClock(wall=_WALL))
        result = dispatch.dispatch_once(p, transport=fake)
        assert result.outcome is DispatchOutcome.SESSION_UNRESOLVED
        assert fake.snapshot_calls == 0
        assert fake.send_calls == 0
        assert _count(db, "operations") == 0


class TestO02:
    def test_o02_snapshot_failure_does_not_call_send(self, dispatch_env):
        db, _, _, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        fake = FakeTransport(FakeMode.SNAPSHOT_FAIL, db=db)
        dispatch = DispatchService(db, clock=FakeClock(wall=_WALL))
        result = dispatch.dispatch_once(p, transport=fake)
        assert result.outcome is DispatchOutcome.PRECHECK_FAILED
        assert fake.snapshot_calls == 1
        assert fake.send_calls == 0
        assert _count(db, "operations") == 0  # 最干净：尚未发生副作用 POST


class TestO03:
    def test_o03_timeout_after_remote_acceptance_becomes_unknown_and_is_not_reposted(
        self, dispatch_env,
    ):
        db, _, _, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        fake = FakeTransport(FakeMode.TIMEOUT_AFTER_SIDE_EFFECT, db=db)
        dispatch = DispatchService(db, clock=FakeClock(wall=_WALL))
        first = dispatch.dispatch_once(p, transport=fake)
        assert first.outcome is DispatchOutcome.UNKNOWN
        assert fake.send_calls == 1
        assert fake.remote_received is True
        assert _op_state(db, p.operation_key) == "UNKNOWN"
        second = dispatch.dispatch_once(p, transport=fake)
        assert second.outcome is DispatchOutcome.ALREADY_EXISTS
        assert second.state == "UNKNOWN"
        assert fake.send_calls == 1  # 绝对不能第二次 POST
        assert _count(db, "operations") == 1


class TestStopRace:
    def test_stop_wins_send_right_revoked_zero_send(self, dispatch_env):
        db, _, ops, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        fake = FakeTransport(FakeMode.ACCEPTED, db=db)
        dispatch = DispatchService(db, operation_store=ops, clock=FakeClock(wall=_WALL))
        prepared = dispatch.prepare(p, transport=fake, now=_WALL)
        assert prepared.outcome is DispatchOutcome.PREPARED
        db.connection.execute(
            "UPDATE tasks SET authority_epoch=2 WHERE task_key=?", (task_key,)
        )
        db.connection.execute(
            "UPDATE attempts SET authority_epoch=2 WHERE attempt_id=?", (attempt_id,)
        )
        revoked = dispatch.acquire_send_right(p, now=_WALL)
        assert revoked.outcome is DispatchOutcome.SEND_RIGHT_REVOKED
        assert fake.send_calls == 0
        assert _op_state(db, p.operation_key) == "PREPARED"  # 保持 PREPARED，不写 CANCELLED

    def test_send_gate_wins_sends_exactly_once(self, dispatch_env):
        db, _, ops, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        fake = FakeTransport(FakeMode.ACCEPTED, db=db)
        dispatch = DispatchService(db, operation_store=ops, clock=FakeClock(wall=_WALL))
        prepared = dispatch.prepare(p, transport=fake, now=_WALL)
        assert prepared.outcome is DispatchOutcome.PREPARED
        granted = dispatch.acquire_send_right(p, now=_WALL)
        assert granted.outcome is DispatchOutcome.SENDING
        db.connection.execute(
            "UPDATE tasks SET authority_epoch=2 WHERE task_key=?", (task_key,)
        )
        db.connection.execute(
            "UPDATE attempts SET authority_epoch=2 WHERE attempt_id=?", (attempt_id,)
        )
        sent = dispatch.send_prepared(p, operation_key=p.operation_key,
                                      transport=fake, now=_WALL)
        assert sent.outcome is DispatchOutcome.ACCEPTED
        assert fake.send_calls == 1  # 发送权先赢：已获权的那一次仍执行，但绝不超过一次


class TestCrashRecovery:
    def test_prepared_crash_restart_continues_and_sends_once(self, tmp_path):
        path = tmp_path / "crash.sqlite"
        p = _proposal("2:CHATGPT:t10-crash", "attempt-001", operation_key="k:prepared")
        # 阶段 0：先启动一个真实 ACTIVE 任务（用主库）。
        db1 = Database(path)
        db1.open()
        tasks = TaskStore(db1)
        attempt_ids: list[str] = []

        def attempt_factory(task_key: str) -> str:
            attempt_id = f"attempt-{len(attempt_ids) + 1:03d}"
            attempt_ids.append(attempt_id)
            return attempt_id

        scheduler = Scheduler(db1, task_store=tasks,
                              clock=FakeClock(wall=_WALL),
                              attempt_id_factory=attempt_factory)
        raw = _V1.format(task_id="t10-crash", body="处理任务 t10-crash")
        msg = parse_message(raw)
        task_key = make_task_key("CHATGPT", "t10-crash")
        tasks.claim(
            task_key=task_key, peer_id="CHATGPT", task_id="t10-crash",
            protocol_format=ProtocolFormat.V1.value.upper(), raw_message=raw,
            body=msg.body, canonical_hash=content_digest(msg),
            receive_snapshot=_make_receive("p-crash", "sess-crash"), received_at=_T0,
        )
        start = scheduler.tick()
        assert start.outcome is ScheduleOutcome.STARTED
        p = _proposal(task_key, start.attempt_id, project_key="p-crash",
                      session_id="sess-crash", operation_key="continue:crash:interrupt-7")
        fake1 = FakeTransport(FakeMode.ACCEPTED, db=db1)
        dispatch1 = DispatchService(db1, clock=FakeClock(wall=_WALL))
        prepared = dispatch1.prepare(p, transport=fake1, now=_WALL)
        assert prepared.outcome is DispatchOutcome.PREPARED
        db1.close()  # 模拟 PREPARED 后进程崩溃（发送权从未取得）
        # 重启：全新 Database/OperationStore/DispatchService
        db2 = Database(path)
        db2.open()
        fake2 = FakeTransport(FakeMode.ACCEPTED, db=db2)
        dispatch2 = DispatchService(db2, clock=FakeClock(wall=_WALL))
        result = dispatch2.dispatch_once(p, transport=fake2, now=_WALL)
        assert result.outcome is DispatchOutcome.ACCEPTED
        assert fake2.send_calls == 1
        db2.close()

    def test_sending_crash_restart_becomes_unknown_zero_send(self, tmp_path):
        path = tmp_path / "crash.sqlite"
        db1 = Database(path)
        db1.open()
        tasks = TaskStore(db1)
        attempt_ids: list[str] = []

        def attempt_factory(task_key: str) -> str:
            attempt_id = f"attempt-{len(attempt_ids) + 1:03d}"
            attempt_ids.append(attempt_id)
            return attempt_id

        scheduler = Scheduler(db1, task_store=tasks,
                              clock=FakeClock(wall=_WALL),
                              attempt_id_factory=attempt_factory)
        raw = _V1.format(task_id="t10-crash", body="处理任务 t10-crash")
        msg = parse_message(raw)
        task_key = make_task_key("CHATGPT", "t10-crash")
        tasks.claim(
            task_key=task_key, peer_id="CHATGPT", task_id="t10-crash",
            protocol_format=ProtocolFormat.V1.value.upper(), raw_message=raw,
            body=msg.body, canonical_hash=content_digest(msg),
            receive_snapshot=_make_receive("p-crash", "sess-crash"), received_at=_T0,
        )
        start = scheduler.tick()
        assert start.outcome is ScheduleOutcome.STARTED
        p = _proposal(task_key, start.attempt_id, project_key="p-crash",
                      session_id="sess-crash", operation_key="continue:crash2:interrupt-7")
        fake1 = FakeTransport(FakeMode.ACCEPTED, db=db1)
        dispatch1 = DispatchService(db1, clock=FakeClock(wall=_WALL))
        prepared = dispatch1.prepare(p, transport=fake1, now=_WALL)
        assert prepared.outcome is DispatchOutcome.PREPARED
        granted = dispatch1.acquire_send_right(p, now=_WALL)
        assert granted.outcome is DispatchOutcome.SENDING
        db1.close()  # 模拟 SENDING 后进程崩溃（POST 可能已到达远端）
        db2 = Database(path)
        db2.open()
        fake2 = FakeTransport(FakeMode.ACCEPTED, db=db2)
        dispatch2 = DispatchService(db2, clock=FakeClock(wall=_WALL))
        result = dispatch2.dispatch_once(p, transport=fake2, now=_WALL)
        assert result.outcome is DispatchOutcome.ALREADY_EXISTS
        assert result.state == "UNKNOWN"
        assert fake2.send_calls == 0  # 绝不以“旧内存不存在”当作没发过
        assert _op_state(db2, p.operation_key) == "UNKNOWN"
        db2.close()


class TestConcurrency:
    @staticmethod
    def _run_workers(path, proposal, *, n, mode=FakeMode.ACCEPTED):
        barrier = threading.Barrier(n)
        outcomes: list = []
        send_total = [0]
        lock = threading.Lock()

        def worker(index: int) -> None:
            worker_db = Database(path)
            worker_db.open()
            try:
                ops = OperationStore(worker_db)
                dispatch = DispatchService(worker_db, operation_store=ops,
                                           clock=FakeClock(wall=_WALL))
                fake = FakeTransport(mode, db=worker_db)
                barrier.wait()
                res = dispatch.dispatch_once(proposal, transport=fake, now=_WALL)
                with lock:
                    outcomes.append(res.outcome)
                    send_total[0] += fake.send_calls
            finally:
                worker_db.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return outcomes, send_total[0]

    def test_double_request_single_send(self, dispatch_env):
        db, _, _, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        _, sends = self._run_workers(db.path, p, n=2)
        assert sends == 1
        assert _count(db, "operations") == 1

    def test_r01_multiple_sources_share_one_operation_and_one_send(self, dispatch_env):
        db, _, _, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project,
                      operation_key="continue:r01:interrupt-7",
                      interruption_id="interrupt-7")
        # Relay Proposal / Monitor Proposal / 网络恢复 Proposal / 用户继续 四种来源并发。
        _, sends = self._run_workers(db.path, p, n=4)
        assert sends == 1
        assert _count(db, "operations") == 1

    def test_c19_duplicate_timer_and_double_click_get_single_send_right(
        self, dispatch_env,
    ):
        db, _, _, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project,
                      operation_key="continue:c19:interrupt-7")
        # 三个重复 timer + 用户双击“继续” = 4 个重复 Proposals。
        _, sends = self._run_workers(db.path, p, n=4)
        assert sends == 1
        assert _count(db, "operations") == 1

    def test_same_session_unresolved_blocks_new_operation(self, dispatch_env):
        db, _, _, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        pa = _proposal(task_key, attempt_id, project_key=project,
                       operation_key="continue:a:interrupt-7")
        pb = _proposal(task_key, attempt_id, project_key=project,
                       operation_key="continue:b:interrupt-7")
        fake = FakeTransport(FakeMode.TIMEOUT_AFTER_SIDE_EFFECT, db=db)
        dispatch = DispatchService(db, clock=FakeClock(wall=_WALL))
        first = dispatch.dispatch_once(pa, transport=fake)
        assert first.outcome is DispatchOutcome.UNKNOWN
        assert fake.send_calls == 1
        second = dispatch.dispatch_once(pb, transport=fake)
        assert second.outcome is DispatchOutcome.SESSION_HAS_UNRESOLVED_OPERATION
        assert fake.send_calls == 1  # 第二个发送被阻塞，不产生第二次 POST


class TestIdempotency:
    def test_accepted_repeat_returns_already_exists(self, dispatch_env):
        db, _, _, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        p = _proposal(task_key, attempt_id, project_key=project)
        fake = FakeTransport(FakeMode.ACCEPTED, db=db)
        dispatch = DispatchService(db, clock=FakeClock(wall=_WALL))
        assert dispatch.dispatch_once(p, transport=fake).outcome is DispatchOutcome.ACCEPTED
        again = dispatch.dispatch_once(p, transport=fake)
        assert again.outcome is DispatchOutcome.ALREADY_EXISTS
        assert again.state == "ACCEPTED"
        assert fake.send_calls == 1
        assert _count(db, "operations") == 1

    def test_key_conflict_original_unchanged(self, dispatch_env):
        db, _, _, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        pa = _proposal(task_key, attempt_id, project_key=project,
                       operation_key="continue:conflict:interrupt-7")
        pb = _proposal(task_key, "attempt-alien", project_key=project,
                       operation_key="continue:conflict:interrupt-7")
        fake = FakeTransport(FakeMode.ACCEPTED, db=db)
        dispatch = DispatchService(db, clock=FakeClock(wall=_WALL))
        first = dispatch.dispatch_once(pa, transport=fake)
        assert first.outcome is DispatchOutcome.ACCEPTED
        clash = dispatch.dispatch_once(pb, transport=fake)
        assert clash.outcome is DispatchOutcome.KEY_CONFLICT
        assert _op_state(db, pb.operation_key) == "ACCEPTED"  # 原 operation 未被覆盖
        assert fake.send_calls == 1

    def test_same_key_same_identity_different_body_no_false_conflict(self, dispatch_env):
        _, _, ops, _, _, _ = dispatch_env
        task_key, attempt_id, project, _ = _start(dispatch_env)
        pa = _proposal(task_key, attempt_id, project_key=project,
                       operation_key="continue:content:interrupt-7", prompt_body="正文 A")
        with dispatch_env[0].transaction():
            first = ops.prepare_in(
                dispatch_env[0].connection, proposal=pa,
                operation_id=derive_operation_id(pa.operation_key),
                remote_user_id="msg_first",
                pre_snapshot_json="{}", prompt_text="A", prompt_hash="h1",
                created_at=_T0,
            )
        pb = _proposal(task_key, attempt_id, project_key=project,
                       operation_key="continue:content:interrupt-7", prompt_body="正文 B")
        with dispatch_env[0].transaction():
            second = ops.prepare_in(
                dispatch_env[0].connection, proposal=pb,
                operation_id=derive_operation_id(pb.operation_key),
                remote_user_id="msg_second",
                pre_snapshot_json="{}", prompt_text="B", prompt_hash="h2",
                created_at=_T0,
            )
        assert first.outcome is PrepareOutcome.CREATED
        assert second.outcome is PrepareOutcome.EXISTING  # 身份冲突判定不只看 hash


class TestWireMarkers:
    def test_deterministic_operation_id_and_hashes(self):
        key = "continue:2:CHATGPT:t1:interrupt-7"
        assert derive_operation_id(key) == derive_operation_id(key)
        assert derive_operation_id(key) != derive_operation_id(key + "x")
        one = build_wire_prompt(task_key="t", attempt_id="a", operation_id="op-1",
                                body="正文")
        assert sha256_hex(one) == sha256_hex(one)
        two = build_wire_prompt(task_key="t", attempt_id="a", operation_id="op-2",
                                body="正文")
        assert sha256_hex(one) != sha256_hex(two)  # operation 标记影响摘要