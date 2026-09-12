"""T08：Qt 剪贴板适配器——GUI 线程快照、自写一次性保护、补拾、暂停与防抖。

验收关联（149项验收场景 第17.5 节正式定义）：
- P06：自写RESPONSE、ACK、普通文本 → 不当作新TASK；无递归剪贴板循环。
- P07：启动/暂停恢复时已有新TASK → 持久接收恰一次；重复启动不重复。
- Q06：关窗前后接收事件交错 → 已确认任务均存在磁盘；未提交不称已接受。
- UI-A08：任务接收暂停 → 当前任务、队列与自动续接仍工作；文案明确只暂停新拾取。

测试使用 FakeClipboard（不触碰真实系统剪贴板），并记录 QClipboard.text() 被调用的
线程，证明文本读取只发生在监听器所属 GUI 线程，业务层收到的是普通 Python str。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from adapters.clipboard import (
    PickupReason,
    QClipboardListener,
    _digest,
)
from app.commands import ClipboardReceiveController, ReceiveOutcomeKind
from core.domain import SettingsDraft
from core.ingress import IngressService
from core.settings_service import SettingsService
from infra.clock import FakeClock
from storage.database import Database
from storage.settings_store import SettingsStore
from storage.task_store import TaskStore


def v1_task(task_id: str, body: str = "修复仪表盘") -> str:
    return (
        "AI_RELAY/1\n"
        f"MESSAGE_ID: {task_id}\n"
        "SOURCE: CHATGPT\n"
        "TARGET: OPENCHAMBER\n"
        "TYPE: TASK\n"
        "\n"
        f"{body}"
    )


def v1_response(task_id: str) -> str:
    return (
        "AI_RELAY/1\n"
        f"MESSAGE_ID: {task_id}\n"
        "SOURCE: CHATGPT\n"
        "TARGET: OPENCHAMBER\n"
        "TYPE: RESPONSE\n"
        f"IN_REPLY_TO: {task_id}-orig\n"
        "\n"
        "完成"
    )


class FakeClipboard:
    """模拟 QClipboard：记录 text() 被调用线程；setText 同步触发 dataChanged（等价 Qt 行为）。"""

    def __init__(self) -> None:
        self._text = ""
        self._callbacks: list = []
        self.read_threads: list[int] = []
        self.write_count = 0

    def text(self) -> str:
        self.read_threads.append(threading.current_thread().ident or 0)
        return self._text

    def setText(self, text: str) -> None:
        self._text = text
        self.write_count += 1
        for callback in list(self._callbacks):
            callback()

    def set_without_notify(self, text: str) -> None:
        """仅改内容不发事件（模拟 Qt 事件可能合并/延后）。"""
        self._text = text

    def connect_data_changed(self, callback) -> None:
        self._callbacks.append(callback)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t08.sqlite")
    database.open()
    yield database
    database.close()


@pytest.fixture
def task_store(db):
    return TaskStore(db)


@pytest.fixture
def clock():
    return FakeClock(wall=datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def settings(db, tmp_path, clock):
    service = SettingsService(SettingsStore(db), clock=clock)
    proj = tmp_path / "proj"
    proj.mkdir()
    draft = SettingsDraft.defaults()
    draft["openchamber"]["directory"] = str(proj)
    service.submit_draft(draft, base_revision=None, actor="test")
    return service


@pytest.fixture
def controller(task_store, settings, clock):
    return ClipboardReceiveController(IngressService(task_store, clock=clock), settings=settings)


@pytest.fixture
def fake_clipboard():
    return FakeClipboard()


@pytest.fixture
def outcomes():
    """记录 handler 收到的 (snapshot, result)，用于断言线程边界与发布顺序。"""
    return []


@pytest.fixture
def listener(fake_clipboard, controller, outcomes, clock):
    def handler(snapshot):
        assert isinstance(snapshot.text, str), "业务层必须收到普通 str，而非 QClipboard/QMimeData"
        result = controller.execute(snapshot.text, reason=snapshot.reason.value)
        outcomes.append((snapshot, result))
        return result

    return QClipboardListener(fake_clipboard, handler=handler, clock=clock)


def latest(outcomes):
    assert outcomes, "尚未产生任何发布结果"
    return outcomes[-1][1]


class TestGuiThreadBoundary:
    def test_clipboard_text_only_read_on_gui_thread(self, fake_clipboard, listener):
        # 证明 QClipboard.text() 只在监听器所属（GUI）线程被读取
        listener.start()
        fake_clipboard.setText(v1_task("b1"))
        fake_clipboard.setText("普通文本")
        main_thread_id = threading.current_thread().ident or 0
        assert listener.gui_thread_id == main_thread_id
        for thread_id in fake_clipboard.read_threads:
            assert thread_id == main_thread_id

    def test_handler_receives_plain_str_copy(self, outcomes, listener, fake_clipboard):
        listener.start()
        fake_clipboard.setText(v1_task("b2"))
        snapshot = outcomes[0][0]
        assert isinstance(snapshot.text, str)
        assert snapshot.digest
        assert snapshot.captured_at == "2026-10-01T09:00:00+00:00"  # FakeClock 注入
        assert snapshot.reason is PickupReason.STARTUP_PICKUP


class TestP07Pickup:
    def test_startup_pickup_without_data_changed(self, db, listener, fake_clipboard):
        fake_clipboard.set_without_notify(v1_task("s1"))  # 启动前已存在，且不再变化
        result = listener.start()
        assert result.kind is ReceiveOutcomeKind.ACCEPTED
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1

    def test_startup_idempotent_no_second_pickup(self, listener):
        listener.start()
        assert listener.start() is None  # ACT01 幂等

    def test_resume_picks_up_task_added_while_paused(self, db, listener, fake_clipboard):
        listener.start()
        listener.pause()
        fake_clipboard.setText(v1_task("r1"))
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        result = listener.resume()
        assert result.kind is ReceiveOutcomeKind.ACCEPTED
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert db.connection.execute(
            "SELECT state FROM tasks WHERE task_id='r1'"
        ).fetchone()[0] == "QUEUED"

    def test_repeated_resume_does_not_duplicate(self, db, listener, fake_clipboard):
        listener.start()
        listener.pause()
        fake_clipboard.setText(v1_task("r2"))
        listener.resume()
        second = listener.resume()  # 连续 resume：不重复认领
        assert second.kind == "DEBOUNCED"
        rows = db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        events = db.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_code='TASK_CLAIMED'"
        ).fetchone()[0]
        assert rows == 1 and events == 1

    def test_duplicate_change_events_single_row(self, db, listener, fake_clipboard):
        # 同一次用户操作多次 dataChanged：adapter 防抖 + TaskStore 去重双层收敛
        listener.start()
        fake_clipboard.setText(v1_task("d1"))
        fake_clipboard.setText(v1_task("d1"))
        fake_clipboard.setText(v1_task("d1"))
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert db.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_code='TASK_CLAIMED'"
        ).fetchone()[0] == 1


class TestP06Ignore:
    def test_plain_text_at_startup_and_change_ignored(self, db, outcomes, listener, fake_clipboard):
        fake_clipboard.set_without_notify("用户复制的一段普通文字 path\\to\\file")
        listener.start()
        assert latest(outcomes).kind is ReceiveOutcomeKind.IGNORED_PLAIN_TEXT
        fake_clipboard.setText("https://example.com/some-url")
        assert latest(outcomes).kind is ReceiveOutcomeKind.IGNORED_PLAIN_TEXT
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

    def test_self_write_ignored_once_then_external_same_text_accepted(
        self, db, outcomes, listener, fake_clipboard
    ):
        # P06：Relay 自写只忽略一次；消费后外部同文本按真实内容正常处理
        listener.start()
        task = v1_task("w1")
        listener.write_text_from_relay(task)  # setText 同步触发 dataChanged，自写路径不进 handler
        assert listener._pending_self_digest is None  # marker 已消费
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

        fake_clipboard.setText(task)  # 外部再次复制同一文本：按真实协议内容接收
        assert latest(outcomes).kind is ReceiveOutcomeKind.ACCEPTED
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1

    def test_self_write_response_not_recursive(self, db, outcomes, listener, fake_clipboard):
        # P06：自写 RESPONSE 忽略后无递归剪贴板循环（无新任务/无 TASK_CLAIMED 事件）
        listener.start()
        listener.write_text_from_relay(v1_response("w2"))
        assert listener._pending_self_digest is None  # marker 已消费，无残留扣留
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert db.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_code='TASK_CLAIMED'"
        ).fetchone()[0] == 0

    def test_stale_self_marker_cleared_on_mismatch(self, db, outcomes, listener, fake_clipboard):
        # P06：准备写 X 但实际观察到外部 Y → 旧 marker 清理，Y 正常处理，之后 X 也不被吞
        listener.start()
        task_x = v1_task("x-1")
        task_y = v1_task("y-1")
        listener._pending_self_digest = _digest(task_x)
        fake_clipboard.setText(task_y)
        assert latest(outcomes).kind is ReceiveOutcomeKind.ACCEPTED  # Y 正常处理
        assert listener._pending_self_digest is None
        fake_clipboard.setText(task_x)  # 之后再外部复制 X
        assert latest(outcomes).kind is ReceiveOutcomeKind.ACCEPTED  # 未被 stale marker 吞掉
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2


class TestUI_A08Pause:
    def test_pause_does_not_claim_new_task(self, db, listener, fake_clipboard):
        listener.start()
        listener.pause()
        fake_clipboard.setText(v1_task("p1"))  # 暂停期间放新任务
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        listener.resume()  # 恢复补拾
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1

    def test_pause_leaves_existing_task_untouched(self, db, listener, fake_clipboard):
        listener.start()
        fake_clipboard.setText(v1_task("p2"))
        before = db.connection.execute(
            "SELECT state, sequence, ingress_snapshot_json FROM tasks WHERE task_id='p2'"
        ).fetchone()
        listener.pause()
        fake_clipboard.setText(v1_task("p3"))  # 暂停期间事件被忽略
        listener.resume()
        after = db.connection.execute(
            "SELECT state, sequence, ingress_snapshot_json FROM tasks WHERE task_id='p2'"
        ).fetchone()
        assert (after[0], after[1], after[2]) == (before[0], before[1], before[2])
        assert db.connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0

    def test_overwritten_history_not_recoverable(self, db, listener, fake_clipboard):
        # 系统只补拾当前仍存在的剪贴板内容，不具备剪贴板历史恢复能力
        listener.start()
        listener.pause()
        fake_clipboard.setText(v1_task("h-a"))  # 暂停期间 A → B，A 被覆盖
        fake_clipboard.setText(v1_task("h-b"))
        listener.resume()
        task_ids = {
            row[0]
            for row in db.connection.execute("SELECT task_id FROM tasks").fetchall()
        }
        assert task_ids == {"h-b"}  # B 可补拾，A 无法恢复（非 bug）
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


class TestQ06FailureRetry:
    def test_storage_failure_then_resume_retries_current(
        self, db, listener, task_store, outcomes, fake_clipboard
    ):
        listener.start()
        task_store.fault_inject_after = 1
        fake_clipboard.setText(v1_task("f1"))
        assert latest(outcomes).kind is ReceiveOutcomeKind.ERROR
        assert latest(outcomes).debounce is False  # 失败绝不进防抖
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

        task_store.fault_inject_after = None
        listener.resume()  # 剪贴板仍是 f1，无需用户重新复制
        assert latest(outcomes).kind is ReceiveOutcomeKind.ACCEPTED
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1

    def test_storage_failure_then_retry_current(
        self, db, listener, task_store, outcomes, fake_clipboard
    ):
        listener.start()
        task_store.fault_inject_after = 2
        fake_clipboard.setText(v1_task("f2"))
        assert latest(outcomes).kind is ReceiveOutcomeKind.ERROR
        task_store.fault_inject_after = None
        result = listener.retry_current()  # 显式重试通道（暂停态也可用）
        assert result.kind is ReceiveOutcomeKind.ACCEPTED
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1

    def test_existing_after_retry_not_new_claim(self, db, listener, fake_clipboard):
        listener.start()
        fake_clipboard.setText(v1_task("f3"))
        listener.resume()
        listener.resume()
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


class TestNoSideEffects:
    def test_accept_never_creates_attempt_or_operation(self, db, listener, fake_clipboard):
        listener.start()
        fake_clipboard.setText(v1_task("n1"))
        fake_clipboard.setText(v1_task("n2"))
        for table in ("attempts", "operations", "results", "outbox"):
            assert db.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0] == 0