"""T08：App 命令层——剪贴板快照 → 认领 → 提交后发布。

验收关联（149项验收场景 第17.5 节正式定义）：
- P06：自写RESPONSE、ACK、普通文本 → 不当作新TASK；无递归剪贴板循环。
- P07：启动/暂停恢复时已有新TASK → 持久接收恰一次；重复启动不重复。
- Q06：关窗前后接收事件交错 → 已确认任务均存在磁盘；未提交不称已接受。
- UI-A08：任务接收暂停 → 当前任务、队列与自动续接仍工作；文案明确只暂停新拾取。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.commands import (
    ClipboardReceiveController,
    ReceiveOutcomeKind,
    looks_like_relay_message,
)
from core.domain import SessionBindingMode, SettingsDraft, TargetExecutor
from core.ingress import IngressService, PEER_ID_CHATGPT, PEER_ID_LEGACY_DEFAULT
from core.protocol_v1 import ProtocolFormat
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


def legacy_task(task_id: str, body: str = "旧格式正文") -> str:
    return (
        "----- AI_RELAY_BEGIN -----\n"
        f"TASK_ID: {task_id}\n"
        "SOURCE: CHATGPT\n"
        "TARGET: OPENCHAMBER\n"
        "TYPE: TASK\n"
        f"CONTENT: {body}\n"
        "----- AI_RELAY_END -----"
    )


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
def settings(db, tmp_path, clock):
    service = SettingsService(SettingsStore(db), clock=clock)
    proj = tmp_path / "proj"
    proj.mkdir()
    draft = SettingsDraft.defaults()
    draft["openchamber"]["directory"] = str(proj)
    draft["openchamber"]["session_policy"] = "PROJECT_ROTATING"
    service.submit_draft(draft, base_revision=None, actor="test")
    return service


@pytest.fixture
def clock():
    return FakeClock(wall=datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def ingress(task_store, clock):
    return IngressService(task_store, clock=clock)


@pytest.fixture
def controller(ingress, settings):
    return ClipboardReceiveController(ingress, settings=settings)


def row_of(db, task_id: str):
    return db.connection.execute(
        "SELECT state, sequence, peer_id, protocol_format, ingress_snapshot_json "
        "FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()


class TestPrefilter:
    def test_looks_like_relay(self):
        assert looks_like_relay_message("AI_RELAY/1\nMESSAGE_ID: x")
        assert looks_like_relay_message("\r\n----- AI_RELAY_BEGIN -----")
        assert not looks_like_relay_message("hello 复制的内容")
        assert not looks_like_relay_message("AI_RELAY/2 不是本版本头部")


class TestP06IgnoreNonTask:
    def test_response_ack_and_plain_text_are_not_new_tasks(self, db, controller):
        # P06：自写RESPONSE、ACK、普通文本不当作新TASK
        resp = controller.execute(v1_response("r1"))
        assert resp.kind is ReceiveOutcomeKind.IGNORED_NOT_A_TASK
        assert resp.debounce is True
        plain = controller.execute("复制了一段普通段落 https://example.com/x")
        assert plain.kind is ReceiveOutcomeKind.IGNORED_PLAIN_TEXT
        assert plain.debounce is True
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

    def test_p06_no_recursion_loop_side_effect(self, db, controller, ingress):
        # 处理 RESPONSE/普通文本不产生新的剪贴板往返副作用（任务表与 attempts 均无变化）
        controller.execute(v1_response("r2"))
        controller.execute("普通路径复制")
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert db.connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0

    def test_invalid_source_ignored_zero_rows(self, db, controller):
        raw = (
            "AI_RELAY/1\n"
            "MESSAGE_ID: s1\n"
            "SOURCE: EXECUTOR\n"
            "TARGET: OPENCHAMBER\n"
            "TYPE: TASK\n"
            "\n"
            "正文"
        )
        result = controller.execute(raw)
        assert result.kind is ReceiveOutcomeKind.IGNORED_NOT_A_TASK
        assert result.debounce is True
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

    def test_bad_protocol_ignored_quietly(self, db, controller):
        result = controller.execute("AI_RELAY/1\n这是残缺协议头再换行\n正文")
        assert result.kind is ReceiveOutcomeKind.IGNORED_BAD_PROTOCOL
        assert result.debounce is True
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


class TestP07Idempotent:
    def test_same_task_accept_once(self, db, controller):
        # P07：持久接收恰一次；重复启动不重复
        first = controller.execute(v1_task("m1"))
        second = controller.execute(v1_task("m1"))
        assert first.kind is ReceiveOutcomeKind.ACCEPTED
        assert second.kind is ReceiveOutcomeKind.EXISTING
        count = db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        events = db.connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_code='TASK_CLAIMED'"
        ).fetchone()[0]
        assert count == 1
        assert events == 1

    def test_valid_v1_and_legacy_accepted(self, db, controller):
        r1 = controller.execute(v1_task("m2"))
        assert r1.kind is ReceiveOutcomeKind.ACCEPTED
        row1 = row_of(db, "m2")
        assert row1[2] == PEER_ID_CHATGPT
        assert row1[3] == ProtocolFormat.V1.value.upper()

        r2 = controller.execute(legacy_task("m3"))
        assert r2.kind is ReceiveOutcomeKind.ACCEPTED
        row2 = row_of(db, "m3")
        assert row2[2] == PEER_ID_LEGACY_DEFAULT
        assert row2[3] == ProtocolFormat.LEGACY_WEB.value.upper()


class TestQ06CommitBeforePublish:
    def test_failure_returns_error_and_no_row_then_retry(self, db, controller, task_store):
        # Q06：未提交不称已接受；已确认任务均存在磁盘
        task_store.fault_inject_after = 1
        failed = controller.execute(v1_task("m4"))
        assert failed.kind is ReceiveOutcomeKind.ERROR
        assert failed.debounce is False  # 绝不可进入防抖
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

        task_store.fault_inject_after = None
        ok = controller.execute(v1_task("m4"))
        assert ok.kind is ReceiveOutcomeKind.ACCEPTED
        assert db.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert db.connection.execute(
            "SELECT state FROM tasks WHERE task_id='m4'"
        ).fetchone()[0] == "QUEUED"

    def test_settings_frozen_error_retryable(self, ingress):
        # 快照冻结失败也走 ERROR，绝不标“已看过”
        def boom():
            raise RuntimeError("当前无生效配置")

        ctrl = ClipboardReceiveController(ingress, snapshot_provider=boom)
        result = ctrl.execute(v1_task("m5"))
        assert result.kind is ReceiveOutcomeKind.ERROR
        assert result.debounce is False


class TestUI_A08AndQueue:
    def test_pause_outside_layer_keeps_task_untouched(self, db, controller):
        # UI-A08：暂停只控制新拾取；controller 层不持有暂停概念，已有任务状态必须稳定。
        # 真正的暂停门在 adapter（test_t08_clipboard 覆盖）；这里证明提交不受影响。
        controller.execute(v1_task("a"))
        before = row_of(db, "a")
        controller.execute(v1_task("b"))
        after = row_of(db, "a")
        assert (after[0], after[1]) == (before[0], before[1])
        assert db.connection.execute(
            "SELECT COUNT(*) FROM attempts"
        ).fetchone()[0] == 0

    def test_queue_full_not_confirmed_and_can_retry(self, tmp_path):
        db_small = Database(tmp_path / "small.sqlite")
        db_small.open()
        try:
            s = TaskStore(db_small, queue_capacity=1)
            svc = SettingsService(SettingsStore(db_small))
            proj = tmp_path / "proj2"
            proj.mkdir()
            draft = SettingsDraft.defaults()
            draft["openchamber"]["directory"] = str(proj)
            svc.submit_draft(draft, base_revision=None, actor="test")
            ctrl = ClipboardReceiveController(
                IngressService(s, clock=FakeClock()), settings=svc
            )
            assert ctrl.execute(v1_task("q1")).kind is ReceiveOutcomeKind.ACCEPTED
            full = ctrl.execute(v1_task("q2"))
            assert full.kind is ReceiveOutcomeKind.QUEUE_FULL
            assert full.debounce is False  # 队满可再试
            db_small.connection.execute(
                "UPDATE tasks SET state='COMPLETED' WHERE task_id='q1'"
            )
            retried = ctrl.execute(v1_task("q2"))
            assert retried.kind is ReceiveOutcomeKind.ACCEPTED
            assert retried.sequence == 2
        finally:
            db_small.close()


class TestSnapshotPersistence:
    def test_complete_receive_snapshot_persisted(self, db, controller, settings, tmp_path):
        controller.execute(v1_task("m6"))
        row = db.connection.execute(
            "SELECT ingress_snapshot_json FROM tasks WHERE task_id='m6'"
        ).fetchone()
        snap = json.loads(row[0])
        assert snap["config_revision"] == settings.current.revision
        assert snap["project_key"]
        assert snap["directory"] == str(tmp_path / "proj")
        assert snap["effective_executor"] == TargetExecutor.OPENCHAMBER.value
        assert snap["binding_mode"] == SessionBindingMode.PROJECT_ROTATING.value
        assert snap["received_at"] == "2026-10-01T09:00:00+00:00"

    def test_receive_snapshot_frozen_against_later_changes(self, db, controller, settings, tmp_path):
        controller.execute(v1_task("m7"))
        new_proj = tmp_path / "proj_b"
        new_proj.mkdir()
        draft = SettingsDraft.defaults()
        draft["openchamber"]["directory"] = str(new_proj)
        settings.submit_draft(draft, base_revision=settings.current.revision, actor="test")
        # 任务排队后用户改设置：已认领任务仍用接收时冻结快照（主规格 5.2）
        row = db.connection.execute(
            "SELECT ingress_snapshot_json FROM tasks WHERE task_id='m7'"
        ).fetchone()
        snap = json.loads(row[0])
        assert snap["config_revision"] == 1
        assert snap["directory"] == str(tmp_path / "proj")


class TestNoSideEffects:
    def test_no_attempt_no_operation_no_result(self, db, controller):
        controller.execute(v1_task("m8"))
        for table in ("attempts", "operations", "results", "outbox"):
            count = db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0, f"{table} 应有 0 行（T08 禁止执行）"