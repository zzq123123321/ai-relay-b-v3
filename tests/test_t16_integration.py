"""T16-B3：MainWindow + 真实 TaskQueries + HistoryCopyService 接缝集成测试。

验证：PAGE02 正式 TaskRecordsPage、真实查询、exact 复制 protocol_text（U05）、
UI-B15「未确认交付」文案、ACKED 历史与本次复制隔离、只读无 Outbox 副作用、
UI-A12 active/viewed 隔离与切页保持、长完整值复制（UI-A07）、响应式单列、
搜索框焦点、无 ACT12/重跑按钮、T14 legacy 构造兼容。
MainWindow 本身不创建 Database/TaskQueries/ClipboardSink，全部注入。
"""

from __future__ import annotations

import hashlib

import pytest
from PySide6.QtCore import Qt  # noqa: F401
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_APP_INSTANCE = QApplication.instance() or QApplication([])

from app.history_copy import HistoryCopyService  # noqa: E402
from app.snapshots import fake_snapshot  # noqa: E402
from core.domain import (  # noqa: E402
    ReceiveSettingsSnapshot,
    SessionBindingMode,
    TargetExecutor,
)
from core.protocol_v1 import (  # noqa: E402
    ProtocolFormat,
    content_digest,
    parse_message,
)
from storage.database import Database  # noqa: E402
from storage.task_queries import TaskQueries  # noqa: E402
from storage.task_store import TaskStore, make_task_key  # noqa: E402
from ui.main_window import MainWindow, TIER_NARROW  # noqa: E402
from ui.task_records import TaskRecordsPage  # noqa: E402

_T0 = "2026-10-01T09:00:00+00:00"
LONG_TASK_ID = "T" * 140
LONG_RESULT_ID = "R" * 140
DIRECTORY_LONG = r"D:\AIwork\很长的 项目 路径:88\子目录\更深\继续"

TASKA_KEY = make_task_key("CHATGPT", "task-A")
TASKB_KEY = make_task_key("CHATGPT", "task-B")
LONGTASK_KEY = make_task_key("CHATGPT", LONG_TASK_ID)

PROTO_A2 = "AI_RELAY/1\nRESPONSE MESSAGE_ID: res-a-2\n\n乙失败协议文本"
PROTO_A3 = "AI_RELAY/1\nRESPONSE MESSAGE_ID: res-a-3\n\n丙成功协议文本"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _raw(task_id: str, body: str) -> str:
    return (
        "AI_RELAY/1\n"
        f"MESSAGE_ID: {task_id}\n"
        "SOURCE: CHATGPT\n"
        "TARGET: OPENCHAMBER\n"
        "TYPE: TASK\n"
        "\n"
        f"{body}"
    )


def _receive(
    *,
    project_key: str = "proj-alpha",
    directory: str = r"D:\AIwork\proj",
) -> ReceiveSettingsSnapshot:
    return ReceiveSettingsSnapshot(
        config_revision=5,
        committed_at=_T0,
        received_at=_T0,
        effective_executor=TargetExecutor.OPENCHAMBER,
        directory=directory,
        project_key=project_key,
        agent="build",
        requested_model="qwen3.8-27b",
        binding_mode=SessionBindingMode.PROJECT_ROTATING,
        frozen_session_id=None,
    )


def _claim(store: TaskStore, task_id: str, *, directory: str = r"D:\AIwork\proj"):
    raw = _raw(task_id, "处理任务")
    msg = parse_message(raw)
    return store.claim(
        task_key=make_task_key("CHATGPT", task_id),
        peer_id="CHATGPT",
        task_id=task_id,
        protocol_format=ProtocolFormat.V1.value.upper(),
        raw_message=raw,
        body=msg.body,
        canonical_hash=content_digest(msg),
        receive_snapshot=_receive(directory=directory),
        received_at=_T0,
    )


def _insert_attempt(db, *, attempt_id, task_key, state="COMPLETED"):
    import json

    execution = json.dumps(
        {"resolved_session_id": "sess", "execution_started_at": _T0},
        ensure_ascii=False,
    )
    db.connection.execute(
        "INSERT INTO attempts (attempt_id, task_key, parent_attempt_id, kind, state,"
        " authority_epoch, execution_snapshot_json, remote_state, started_at, ended_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (attempt_id, task_key, None, "INITIAL", state, 1, execution, "NOT_SENT",
         _T0, None),
    )


def _insert_result(db, *, result_id, task_key, attempt_id, revision, state, source,
                   final_body, protocol_text, committed_at=_T0, remote_ids=None):
    import json

    id_json = json.dumps(remote_ids or [], ensure_ascii=False)
    db.connection.execute(
        "INSERT INTO results (result_id, task_key, attempt_id, revision, state, source,"
        " final_body, protocol_text, sha256, remote_message_ids_json, committed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (result_id, task_key, attempt_id, revision, state, source, final_body,
         protocol_text, _sha(protocol_text), id_json, committed_at),
    )


def _insert_outbox(db, *, delivery_id, result_id, peer_id, state):
    db.connection.execute(
        "INSERT INTO outbox (delivery_id, result_id, peer_id, state, profile,"
        " offered_count) VALUES (?,?,?,?,?,0)",
        (delivery_id, result_id, peer_id, state, "legacy_v1"),
    )


def _set_task(db, task_key, *, state, current_result_revision=0):
    db.connection.execute(
        "UPDATE tasks SET state=?, current_result_revision=? WHERE task_key=?",
        (state, current_result_revision, task_key),
    )


def _seed(db, store) -> None:
    a = _claim(store, "task-A")
    _insert_attempt(db, attempt_id="a1", task_key=a.task_key)
    _insert_attempt(db, attempt_id="a2", task_key=a.task_key)
    _insert_result(
        db, result_id="res-a-1", task_key=a.task_key, attempt_id="a1",
        revision=1, state="COMPLETED", source="AUTO_RELAY",
        final_body="甲方案", protocol_text="AI_RELAY/1\n\n甲方案",
    )
    _insert_result(
        db, result_id="res-a-2", task_key=a.task_key, attempt_id="a2",
        revision=2, state="FAILED", source="MANUAL_WRAP",
        final_body="乙失败正文", protocol_text=PROTO_A2,
    )
    _insert_result(
        db, result_id="res-a-3", task_key=a.task_key, attempt_id="a2",
        revision=3, state="COMPLETED", source="MANUAL_WRAP",
        final_body="丙成功正文", protocol_text=PROTO_A3, remote_ids=["msg-9"],
    )
    _insert_outbox(db, delivery_id="deliv-a3c", result_id="res-a-3",
                   peer_id="CHATGPT", state="ACKED")
    _insert_outbox(db, delivery_id="deliv-a1", result_id="res-a-1",
                   peer_id="CHATGPT", state="OFFERED")
    _set_task(db, a.task_key, state="COMPLETED", current_result_revision=3)

    b = _claim(store, "task-B")
    _insert_attempt(db, attempt_id="b1", task_key=b.task_key, state="OPEN")
    _set_task(db, b.task_key, state="ACTIVE")

    long = _claim(store, LONG_TASK_ID, directory=DIRECTORY_LONG)
    _insert_attempt(db, attempt_id="l1", task_key=long.task_key)
    _insert_result(
        db, result_id=LONG_RESULT_ID, task_key=long.task_key, attempt_id="l1",
        revision=1, state="COMPLETED", source="MANUAL_WRAP",
        final_body="长正文", protocol_text="AI_RELAY/1\n\n长协议正文",
    )
    _set_task(db, long.task_key, state="ACTIVE")


class _Sink:
    def __init__(self, *, fail: bool = False) -> None:
        self.writes: list[str] = []
        self.fail = fail

    def write_text(self, *, text: str) -> None:
        if self.fail:
            raise OSError("clipboard down")
        self.writes.append(text)


def _outbox_states(db) -> list:
    return db.connection.execute(
        "SELECT delivery_id, state FROM outbox ORDER BY delivery_id"
    ).fetchall()


def _count(db, table: str) -> int:
    row = db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) if row else 0


@pytest.fixture(scope="module")
def qapp():
    yield _APP_INSTANCE
    _APP_INSTANCE.setStyleSheet("")


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "b3.sqlite")
    db.open()
    store = TaskStore(db, queue_capacity=1000)
    _seed(db, store)
    queries = TaskQueries(db)
    sink = _Sink()
    service = HistoryCopyService(result_reader=queries, clipboard_sink=sink)
    win = MainWindow(
        fake_snapshot(task_id="task-B", state="ACTIVE", stop_available=True),
        task_history_provider=queries,
        history_copy_service=service,
    )
    yield {
        "db": db,
        "queries": queries,
        "sink": sink,
        "service": service,
        "win": win,
    }
    win.close()
    db.close()


def _feedback(win) -> str:
    return win.tasks_page._copy_feedback.text()


def _open_task_a(win) -> None:
    win.navigate("PAGE02")
    win.tasks_page.load_task_detail(TASKA_KEY)


def test_01_page02_is_task_records_with_real_tasks(env):
    win = env["win"]
    assert isinstance(win.tasks_page, TaskRecordsPage)
    win.navigate("PAGE02")
    table = win.tasks_page.tasks_table
    assert table.rowCount() >= 3
    ids = [table.item(r, 0).text() for r in range(table.rowCount())]
    assert "task-A" in ids
    assert "task-B" in ids
    assert LONG_TASK_ID in ids


def test_02_viewed_task_a_and_exact_result(env):
    win = env["win"]
    _open_task_a(win)
    assert win.tasks_page.selected_task_key == TASKA_KEY
    assert win.tasks_page.detail_panel.task_id_value.text() == "task-A"
    win.tasks_page.select_result("res-a-2")
    assert win.tasks_page.selected_result_id == "res-a-2"
    assert win.tasks_page.detail_panel.result_readable is True


def test_03_ui_b15_exact_protocol_text_copied(env):
    win = env["win"]
    _open_task_a(win)
    win.tasks_page.select_result("res-a-2")
    env["sink"].writes.clear()
    win.tasks_page.detail_panel.copy_reply_button.click()
    assert env["sink"].writes == [PROTO_A2]
    assert env["sink"].writes[0] != "乙失败正文"
    assert env["sink"].writes[0] != PROTO_A3
    assert _feedback(win) == "已写入剪贴板（未确认交付）"


def test_04_ack_history_not_current_copy_confirmation(env):
    win = env["win"]
    _open_task_a(win)
    win.tasks_page.select_result("res-a-3")
    panel = win.tasks_page.detail_panel
    assert "delivery ACKED" in panel._result_meta.text() or "ACKED" in panel._result_meta.text()
    env["sink"].writes.clear()
    panel.copy_reply_button.click()
    assert env["sink"].writes == [PROTO_A3]
    assert _feedback(win) == "已写入剪贴板（未确认交付）"
    forbidden = ("A端已收到", "ACK成功", "已发送", "已交付")
    all_text = " ".join(
        label.text() for label in win.tasks_page.findChildren(QLabel) if label.text()
    )
    assert not any(word in all_text for word in forbidden)


def test_05_outbox_unchanged_across_copy(env):
    db = env["db"]
    service = env["service"]
    before = _outbox_states(db)
    assert _count(db, "outbox") == 2
    service.copy_result("res-a-2")
    service.copy_value(kind="task_id", value="v")
    assert _outbox_states(db) == before


def test_06_outbox_exact_states_frozen(env):
    db = env["db"]
    service = env["service"]
    service.copy_result("res-a-1")  # OFFERED 关联
    service.copy_result("res-a-3")  # ACKED 关联
    assert _outbox_states(db) == [
        ("deliv-a1", "OFFERED"),
        ("deliv-a3c", "ACKED"),
    ]


def test_07_tasks_attempts_results_unchanged(env):
    db = env["db"]
    service = env["service"]
    before = (
        _count(db, "tasks"),
        _count(db, "attempts"),
        _count(db, "results"),
    )
    service.copy_result("res-a-2")
    service.copy_value(kind="canonical_hash", value="h" * 64)
    assert (_count(db, "tasks"), _count(db, "attempts"), _count(db, "results")) == before


def test_08_missing_result_no_fallback_ui(env):
    win = env["win"]
    sink = env["sink"]
    sink.writes.clear()
    _open_task_a(win)
    env["service"].copy_result("res-none")
    assert sink.writes == []
    win._on_copy_result("res-none")  # race：点击复制时 get_result None
    assert _feedback(win) == "此版本无法读取"
    assert win.tasks_page._copy_feedback.property("tone") == "neutral"
    assert sink.writes == []


def test_09_clipboard_failure_page_feedback(env):
    win = env["win"]
    sink = env["sink"]
    sink.fail = True
    win._on_copy_result("res-a-2")
    assert _feedback(win) == "写入剪贴板失败"
    assert win.tasks_page._copy_feedback.property("tone") == "danger"
    assert sink.writes == []
    sink.fail = False


def test_10_active_b_viewed_a_stop_b_dashboard_b(env):
    win = env["win"]
    _open_task_a(win)
    assert win.snapshot.active_task.task_id == "task-B"
    assert win.tasks_page.selected_task_key == TASKA_KEY
    assert win.tasks_page.detail_panel.task_id_value.text() == "task-A"
    assert win.stop_button.toolTip() == "作用于当前活动任务：task-B"
    dash_text = " ".join(
        label.text() for label in win.workbench_page.findChildren(QLabel) if label.text()
    )
    assert "task-B" in dash_text


def test_11_paging_switch_keeps_viewed_a(env):
    win = env["win"]
    _open_task_a(win)
    win.navigate("PAGE01")
    win.navigate("PAGE02")
    assert win.tasks_page.selected_task_key == TASKA_KEY
    assert win.tasks_page.detail_panel.task_id_value.text() == "task-A"
    assert win.snapshot.active_task.task_id == "task-B"


def test_12_focus_target_search_box(env):
    win = env["win"]
    win.navigate("PAGE02")
    focused = win.focusWidget()
    assert focused is win.tasks_page.search_input


def test_13_narrow_single_column(env):
    win = env["win"]
    win.show()
    win.resize(480, 820)
    QApplication.processEvents()
    assert win.tier == TIER_NARROW
    assert win.tasks_page.is_single_column is True
    win.resize(1280, 820)
    QApplication.processEvents()
    assert win.tasks_page.is_single_column is False
    win.hide()


def test_14_no_act12_no_rerun_buttons(env):
    win = env["win"]
    for button in win.tasks_page.findChildren(QPushButton):
        text = button.text()
        assert "取消" not in text
        assert "重跑" not in text
        assert "重新执行" not in text
    assert win.tasks_page.retry_button.text() == "重试查询"


def test_15_legacy_construction_compatible(qapp):
    legacy = MainWindow(fake_snapshot(task_id="task-B", state="ACTIVE"))
    assert not isinstance(legacy.tasks_page, TaskRecordsPage)
    legacy.update_snapshot(fake_snapshot(task_id="task-B", state="ACTIVE"))
    legacy.close()


def test_16_ui_a12_render_keeps_viewed_a(env):
    win = env["win"]
    _open_task_a(win)
    win.update_snapshot(fake_snapshot(task_id="task-C", state="ACTIVE"))
    assert win.tasks_page.selected_task_key == TASKA_KEY
    assert win.tasks_page.detail_panel.task_id_value.text() == "task-A"
    assert win.snapshot.active_task.task_id == "task-C"
    assert win.stop_button.toolTip() == "作用于当前活动任务：task-C"


def test_17_ui_a07_long_full_value_copy(env):
    win = env["win"]
    sink = env["sink"]
    win.navigate("PAGE02")
    win.tasks_page.load_task_detail(LONGTASK_KEY)
    sink.writes.clear()
    win.tasks_page.detail_panel.task_id_copy.click()
    assert sink.writes == [LONG_TASK_ID]
    win.tasks_page.detail_panel.directory_copy.click()
    assert sink.writes[-1] == DIRECTORY_LONG
    win.tasks_page.select_result(LONG_RESULT_ID)
    assert win.tasks_page.detail_panel.copy_result_id_button.isEnabled() is True
    win.tasks_page.detail_panel.copy_result_id_button.click()
    assert sink.writes[-1] == LONG_RESULT_ID