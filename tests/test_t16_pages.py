"""T16-B2 PAGE02 TaskRecords + PANEL01 TaskDetail 页面级测试（Fake Provider）。

全部 UI 测试使用 FakerTaskHistoryProvider，不连 Database/不执行 SQL；
验证：分页 keyset、搜索/过滤传递、select 具体 task_key、exact result_id、
missing 无 fallback、Attempt 链与 active 标记、corrupt attempt 仍显示、
active/viewed 隔离（UI-A12）、长值（UI-A07）、错误保留旧数据与重试同 query、
copy signals 为真实完整值、宽窄布局、无 DB/clipboard/业务按钮。
"""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.snapshots import fake_snapshot  # noqa: E402
from ui.task_records import TaskRecordsPage  # noqa: E402
from ui.task_detail import TaskDetailPanel  # noqa: E402
from PySide6.QtWidgets import QLabel  # noqa: E402

LONG_ID = "T" * 140
LONG_RESULT = "R" * 140
WINDOWS_LONG_PATH = r"D:\AIwork\很长的 项目 路径:88\子目录\更深\继续"
_SHA = hashlib.sha256(b"demo").hexdigest()


# ---------------------------------------------------------------- DTO 镜像


@dataclass(frozen=True)
class Row:
    task_key: str
    task_id: str
    sequence: int
    state: str
    project_key: str | None
    delivery_state: str | None
    received_at: str
    peer_id: str = "CHATGPT"
    protocol_format: str = "V1"
    blocked_reason: str | None = None
    current_result_revision: int = 0
    current_result_id: str | None = None
    directory: str | None = None
    corrupt_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Page:
    items: tuple
    next_cursor: int | None
    has_more: bool
    total_count: int


@dataclass(frozen=True)
class AttemptRow:
    attempt_id: str
    task_key: str
    parent_attempt_id: str | None
    kind: str
    state: str
    authority_epoch: int
    remote_state: str
    started_at: str
    ended_at: str | None
    resolved_session_id: str | None
    corrupt_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskDetail:
    task_key: str
    peer_id: str
    task_id: str
    sequence: int
    protocol_format: str
    state: str
    blocked_reason: str | None
    received_at: str
    authority_epoch: int
    active_attempt_id: str | None
    current_result_revision: int
    raw_message: str
    body: str
    canonical_hash: str
    project_key: str | None
    directory: str | None
    requested_model: str | None
    frozen_session_id: str | None
    config_revision: int | None
    attempts: tuple
    corrupt_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class VersionRow:
    result_id: str
    task_key: str
    attempt_id: str
    revision: int
    state: str
    source: str
    sha256: str
    committed_at: str
    authoritative: bool
    delivery_state: str | None
    corrupt_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResultDetail:
    result_id: str
    task_key: str
    attempt_id: str
    revision: int
    state: str
    source: str
    final_body: str
    protocol_text: str
    sha256: str
    remote_message_ids: tuple
    committed_at: str
    authoritative: bool
    delivery_state: str | None
    corrupt_reasons: tuple[str, ...] = ()


# ---------------------------------------------------------------- Fake


class FakeTaskHistoryProvider:
    def __init__(self, *, empty: bool = False) -> None:
        self.empty = empty
        self.fail = False
        self.fail_detail = False
        self.fail_versions = False
        self.fail_result = False
        self.calls: list[tuple] = []
        self.list_calls: list[dict] = []
        self.detail_calls: list[str] = []
        self.version_calls: list[str] = []
        self.result_calls: list[str] = []
        self._build()

    def _build(self) -> None:
        states = ("QUEUED", "ACTIVE", "BLOCKED", "COMPLETED", "FAILED", "STOPPED_BY_USER")
        rows: list[Row] = []
        rows.append(
            Row(task_key="CHATGPT:" + LONG_ID, task_id=LONG_ID, sequence=32,
                state="QUEUED", project_key="proj-alpha", delivery_state=None,
                received_at="2026-10-01T08:00:00+00:00")
        )
        rows.append(
            Row(task_key="CHATGPT:task-A", task_id="task-A", sequence=31,
                state="ACTIVE", project_key="proj-alpha", delivery_state=None,
                received_at="2026-10-01T08:05:00+00:00",
                current_result_revision=3, current_result_id="res-a-3")
        )
        rows.append(
            Row(task_key="CHATGPT:task-C", task_id="task-C", sequence=30,
                state="BLOCKED", project_key="proj-alpha", delivery_state=None,
                blocked_reason="global_slot_busy",
                received_at="2026-10-01T09:00:00+00:00")
        )
        for k in range(22):
            i = k + 1
            seq = 29 - k
            rows.append(
                Row(task_key=f"CHATGPT:task-{i:02d}", task_id=f"task-{i:02d}",
                    sequence=seq, state=states[i % 6],
                    project_key="proj-alpha" if i % 2 else "proj-delta",
                    delivery_state="ACKED" if i == 4 else None,
                    received_at=f"2026-10-01T09:{i:02d}:00+00:00")
            )
        self._all = rows

    # ---------------- provider 语义 ----------------

    def list_tasks(self, *, limit: int = 20, cursor_sequence: int | None = None,
                   search_text: str | None = None,
                   state_filter: tuple[str, ...] | None = None) -> Page:
        kwargs = dict(limit=limit, cursor_sequence=cursor_sequence,
                      search_text=search_text, state_filter=state_filter)
        self.calls.append(("list_tasks", kwargs))
        self.list_calls.append(kwargs)
        if self.fail:
            raise RuntimeError("simulated query failure")
        if self.empty:
            return Page(items=(), next_cursor=None, has_more=False, total_count=0)
        rows = [r for r in self._all if self._match(r, search_text, state_filter)]
        if cursor_sequence is not None:
            rows = [r for r in rows if r.sequence < cursor_sequence]
        rows.sort(key=lambda r: r.sequence, reverse=True)
        page = rows[:limit]
        has_more = len(rows) > limit
        return Page(
            items=tuple(page),
            next_cursor=page[-1].sequence if has_more else None,
            has_more=has_more,
            total_count=len(rows),
        )

    def _match(self, row, text, states) -> bool:
        if states is not None and row.state not in states:
            return False
        if not text:
            return True
        if text in row.task_id or (row.project_key and text in row.project_key):
            return True
        return any(text in v.result_id for v in self.versions_for(row.task_key))

    def get_task_detail(self, task_key: str) -> TaskDetail | None:
        self.calls.append(("get_task_detail", task_key))
        self.detail_calls.append(task_key)
        if self.fail or self.fail_detail:
            raise RuntimeError("simulated query failure")
        if task_key == "CHATGPT:task-missing":
            return None
        long = task_key == "CHATGPT:" + LONG_ID
        task_id = LONG_ID if long else task_key.split(":", 1)[-1]
        project = "proj-alpha" if long else ("proj-delta" if task_id == "task-B" else "proj-alpha")
        raw = _raw_for(task_id)
        return TaskDetail(
            task_key=task_key, peer_id="CHATGPT", task_id=task_id, sequence=31,
            protocol_format="V1",
            state="BLOCKED" if task_id == "task-C" else "ACTIVE",
            blocked_reason="global_slot_busy" if task_id == "task-C" else None,
            received_at="2026-10-01T09:00:00+00:00", authority_epoch=1,
            active_attempt_id="a2" if task_id == "task-A" else None,
            current_result_revision=3 if task_id == "task-A" else 0,
            raw_message=raw, body="很长的中文正文：任务内容一脉相承地延续……" + task_id,
            canonical_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            project_key=project, directory=WINDOWS_LONG_PATH,
            requested_model="qwen3.8-27b", frozen_session_id="sess-alpha",
            config_revision=5,
            attempts=self._attempts(task_id),
            corrupt_reasons=() if task_id == "task-A" else (),
        )

    def _attempts(self, task_id: str) -> tuple:
        if task_id != "task-A":
            return ()
        return (
            AttemptRow(attempt_id="a1", task_key="CHATGPT:task-A",
                       parent_attempt_id=None, kind="INITIAL", state="COMPLETED",
                       authority_epoch=1, remote_state="NOT_SENT",
                       started_at="2026-10-01T08:05:10+00:00",
                       ended_at="2026-10-01T08:06:00+00:00",
                       resolved_session_id="sess-alpha"),
            AttemptRow(attempt_id="a2", task_key="CHATGPT:task-A",
                       parent_attempt_id="a1", kind="MANUAL_RESOLUTION",
                       state="COMPLETED", authority_epoch=1, remote_state="NOT_SENT",
                       started_at="2026-10-01T08:07:00+00:00",
                       ended_at="2026-10-01T08:08:00+00:00",
                       resolved_session_id="sess-alpha"),
            AttemptRow(attempt_id="a3", task_key="CHATGPT:task-A",
                       parent_attempt_id="a2", kind="NEW_SESSION_RETRY",
                       state="SUPERSEDED", authority_epoch=1, remote_state="NOT_SENT",
                       started_at="2026-10-01T08:09:00+00:00", ended_at=None,
                       resolved_session_id=None,
                       corrupt_reasons=("execution_snapshot_json 无法解析为 JSON",)),
        )

    def versions_for(self, task_key: str) -> tuple:
        if task_key != "CHATGPT:task-A":
            return ()
        return (
            VersionRow(result_id="res-a-3", task_key=task_key, attempt_id="a2",
                       revision=3, state="COMPLETED", source="MANUAL_WRAP",
                       sha256=_SHA, committed_at="2026-10-01T08:10:00+00:00",
                       authoritative=True, delivery_state="ACKED"),
            VersionRow(result_id="res-a-2", task_key=task_key, attempt_id="a2",
                       revision=2, state="FAILED", source="MANUAL_WRAP",
                       sha256=_SHA, committed_at="2026-10-01T08:09:40+00:00",
                       authoritative=False, delivery_state=None),
            VersionRow(result_id="res-a-1", task_key=task_key, attempt_id="a1",
                       revision=1, state="COMPLETED", source="AUTO_RELAY",
                       sha256=_SHA, committed_at="2026-10-01T08:06:30+00:00",
                       authoritative=False, delivery_state="OFFERED",
                       corrupt_reasons=("结果正文解码失败",)),
        )

    def list_task_result_versions(self, task_key: str) -> tuple:
        self.calls.append(("list_task_result_versions", task_key))
        self.version_calls.append(task_key)
        if self.fail or self.fail_versions:
            raise RuntimeError("simulated query failure")
        return self.versions_for(task_key)

    def get_result(self, result_id: str) -> ResultDetail | None:
        self.calls.append(("get_result", result_id))
        self.result_calls.append(result_id)
        if self.fail or self.fail_result:
            raise RuntimeError("simulated query failure")
        if result_id == LONG_RESULT:
            return ResultDetail(
                result_id=result_id, task_key="CHATGPT:task-A", attempt_id="a2",
                revision=9, state="COMPLETED", source="MANUAL_WRAP",
                final_body="长 result_id 完整值复制验证", protocol_text="AI_RELAY/1\n\n长ID",
                sha256=_SHA, remote_message_ids=("m-long",),
                committed_at="2026-10-01T09:00:00+00:00", authoritative=False,
                delivery_state="ACKED",
            )
        if result_id == "res-a-2":
            return ResultDetail(
                result_id=result_id, task_key="CHATGPT:task-A", attempt_id="a2",
                revision=2, state="FAILED", source="MANUAL_WRAP",
                final_body="乙失败：精确读取的这一版", protocol_text="AI_RELAY/1\n\n乙失败",
                sha256=_SHA, remote_message_ids=("m-r2",),
                committed_at="2026-10-01T08:09:40+00:00", authoritative=False,
                delivery_state=None,
            )
        return None


def _raw_for(task_id: str) -> str:
    return (
        "AI_RELAY/1\n"
        f"MESSAGE_ID: {task_id}\n"
        "SOURCE: CHATGPT\n"
        "TARGET: OPENCHAMBER\n"
        "TYPE: TASK\n"
        "\n"
        f"中文正文 {task_id} 完整内容不截断"
    )


# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    app.setStyleSheet("")


@pytest.fixture
def provider():
    return FakeTaskHistoryProvider()


def _make_page(provider, snapshot=None):
    return TaskRecordsPage(provider, snapshot)


def _row_index_by_taskid(table, task_id_value: str) -> int:
    for row in range(table.rowCount()):
        item = table.item(row, 0)
        if item is not None and item.text() == task_id_value:
            return row
    raise AssertionError(f"表中找不到 task_id={task_id_value!r}")


def _panel_texts(panel) -> list[str]:
    return [label.text() for label in panel.findChildren(QLabel)]


# ---------------------------------------------------------------- fixtures end


class TestListAndPagination:
    def test_01_initial_list_desc_order_kept(self, qapp, provider):
        page = _make_page(provider)
        table = page.tasks_table
        assert table.rowCount() == 20
        ids = [table.item(r, 0).text() for r in range(table.rowCount())]
        provider_rows = list(provider.list_tasks().items)
        assert ids == [r.task_id for r in provider_rows[:20]]
        assert ids[0] == LONG_ID  # sequence 32 最大 → 首行
        assert page.result_count_label.text().startswith("当前结果 20 / 总数 25")
        assert page.prev_button.isEnabled() is False
        assert page.next_button.isEnabled() is True

    def test_02_six_states_and_blocked_reason_shown(self, qapp, provider):
        page = _make_page(provider)
        table = page.tasks_table
        states = {table.item(r, 2).text() for r in range(table.rowCount())}
        assert states >= {"QUEUED", "ACTIVE", "BLOCKED", "COMPLETED", "FAILED", "STOPPED_BY_USER"}
        row = _row_index_by_taskid(table, "task-C")
        table.cellClicked.emit(row, 0)
        assert page.detail_panel.state_value.text() == "BLOCKED"
        assert page.detail_panel.blocked_value.text() == "global_slot_busy"

    def test_03_search_passed_to_provider(self, qapp, provider):
        page = _make_page(provider)
        page.search_input.setText("proj-alpha")
        assert provider.list_calls[-1]["search_text"] == "proj-alpha"
        assert provider.list_calls[-1]["cursor_sequence"] is None
        assert page.tasks_table.rowCount() >= 1

    def test_04_state_filter_passed_to_provider(self, qapp, provider):
        page = _make_page(provider)
        page.filter_combo.setCurrentText("QUEUED")
        assert provider.list_calls[-1]["state_filter"] == ("QUEUED",)
        assert page.tasks_table.rowCount() >= 1

    def test_05_next_page_uses_cursor(self, qapp, provider):
        page = _make_page(provider)
        first_next = page._next_cursor
        assert first_next is not None
        page.next_button.click()
        assert provider.list_calls[-1]["cursor_sequence"] == first_next
        assert page.tasks_table.rowCount() == 5
        ids = [page.tasks_table.item(r, 0).text() for r in range(page.tasks_table.rowCount())]
        assert ids == ["task-18", "task-19", "task-20", "task-21", "task-22"]
        assert page.next_button.isEnabled() is False
        assert page.prev_button.isEnabled() is True
        assert page.cursor_history == [None]
        assert page.current_cursor == first_next

    def test_06_previous_page_restores_cursor(self, qapp, provider):
        page = _make_page(provider)
        page.next_button.click()
        page.prev_button.click()
        assert provider.list_calls[-1]["cursor_sequence"] is None
        assert page.tasks_table.rowCount() == 20
        assert page.tasks_table.item(0, 0).text() == LONG_ID

    def test_07_search_resets_pagination(self, qapp, provider):
        page = _make_page(provider)
        page.next_button.click()
        assert page.current_cursor is not None
        page.search_input.setText("proj-alpha")
        assert provider.list_calls[-1]["cursor_sequence"] is None
        assert page.cursor_history == []
        assert page.selected_task_key is None

    def test_21_empty_state(self, qapp):
        provider = FakeTaskHistoryProvider(empty=True)
        page = _make_page(provider)
        assert page.tasks_table.rowCount() == 0
        assert not page._empty_label.isHidden()
        assert page._empty_label.text() == "无匹配任务"


class TestDetailAndAttempts:
    def test_08_select_task_reads_exact_task_key(self, qapp, provider):
        page = _make_page(provider)
        row = _row_index_by_taskid(page.tasks_table, "task-A")
        page.tasks_table.cellClicked.emit(row, 0)
        assert provider.detail_calls[-1] == "CHATGPT:task-A"
        assert page.selected_task_key == "CHATGPT:task-A"
        assert page.detail_panel.task_id_value.text() == "task-A"

    def test_09_raw_message_complete_collapsible(self, qapp, provider):
        page = _make_page(provider)
        page.load_task_detail("CHATGPT:task-A")
        panel = page.detail_panel
        assert panel.raw_message_visible is False
        assert panel.raw_toggle.text() == "显示原始消息"
        assert panel.raw_message_value == _raw_for("task-A")
        assert "很长的中文正文" in panel.body_value.text()
        assert panel.body_value.text().endswith("task-A")
        panel.raw_toggle.click()
        assert panel.raw_message_visible is True
        assert panel.raw_toggle.text() == "折叠原始消息"
        assert panel.raw_message_label.text() == _raw_for("task-A")

    def test_10_attempt_chain_parent_and_active_mark(self, qapp, provider):
        page = _make_page(provider)
        page.load_task_detail("CHATGPT:task-A")
        texts = "".join(_panel_texts(page.detail_panel))
        assert "attempt_id：a1" in texts and "attempt_id：a2" in texts and "attempt_id：a3" in texts
        a1 = texts.index("attempt_id：a1")
        a2 = texts.index("attempt_id：a2")
        a3 = texts.index("attempt_id：a3")
        assert a1 < a2 < a3
        assert "parent_attempt_id：a1" in texts
        assert "parent_attempt_id：a2" in texts
        # active_attempt_id=a2 → 只标记 a2 为当前进行中（不强加给最后一行 a3）
        block_a2 = panel_block_texts(page.detail_panel, "a2")
        block_a3 = panel_block_texts(page.detail_panel, "a3")
        assert "当前进行中" in block_a2
        assert "当前进行中" not in block_a3

    def test_11_corrupt_attempt_still_shown(self, qapp, provider):
        page = _make_page(provider)
        page.load_task_detail("CHATGPT:task-A")
        block = panel_block_texts(page.detail_panel, "a3")
        assert "attempt_id：a3" in block
        assert "执行" in block or "数据不完整" in block
        assert "无法解析为 JSON" in block

    def test_24_long_values_full_and_wrap(self, qapp, provider):
        page = _make_page(provider)
        page.load_task_detail("CHATGPT:" + LONG_ID)
        panel = page.detail_panel
        assert panel.task_id_value.text() == LONG_ID
        assert panel.task_id_value.toolTip() == LONG_ID
        assert panel.directory_value.text() == WINDOWS_LONG_PATH
        assert panel.directory_value.toolTip() == WINDOWS_LONG_PATH
        assert panel.body_value.wordWrap() is True
        assert "很长的中文正文" in panel.body_value.text()
        assert panel.raw_message_value == _raw_for(LONG_ID)


class TestResultVersions:
    def test_12_versions_revision_desc(self, qapp, provider):
        page = _make_page(provider)
        page.load_task_detail("CHATGPT:task-A")
        table = page.detail_panel.version_table
        assert table.rowCount() == 3
        revs = [table.item(r, 0).text() for r in range(table.rowCount())]
        assert revs == ["3", "2", "1"]
        assert table.item(0, 6).text() == "当前权威"
        assert table.item(1, 6).text() == "历史版本"

    def test_13_select_a_r2_exact_get_result(self, qapp, provider):
        page = _make_page(provider)
        page.load_task_detail("CHATGPT:task-A")
        page.select_result("res-a-2")
        assert provider.result_calls[-1] == "res-a-2"
        assert page.selected_result_id == "res-a-2"
        assert page.detail_panel.selected_result_id == "res-a-2"
        assert page.detail_panel.result_readable is True
        assert "乙失败" in page.detail_panel._result_body.text()

    def test_14_missing_result_no_fallback(self, qapp, provider):
        page = _make_page(provider)
        page.load_task_detail("CHATGPT:task-A")
        page.select_result("res-missing")
        assert provider.result_calls[-1] == "res-missing"
        assert "res-a-3" not in provider.result_calls
        assert page.selected_result_id == "res-missing"
        assert page.detail_panel.result_readable is False
        assert page.detail_panel.copy_reply_button.isEnabled() is False
        assert not page.detail_panel._result_missing.isHidden()
        assert "此版本无法读取" in page.detail_panel._result_missing.text()

    def test_15_copy_result_requested_exact(self, qapp, provider):
        page = _make_page(provider)
        received = []
        page.copy_result_requested.connect(received.append)
        page.load_task_detail("CHATGPT:task-A")
        page.select_result("res-a-2")
        assert page.detail_panel.copy_reply_button.isEnabled() is True
        page.detail_panel.copy_reply_button.click()
        assert received == ["res-a-2"]

    def test_16_copy_value_requested_full_long_id(self, qapp, provider):
        page = _make_page(provider)
        received = []
        page.copy_value_requested.connect(lambda kind, value: received.append((kind, value)))
        page.load_task_detail("CHATGPT:" + LONG_ID)
        assert page.detail_panel.task_id_value.text() == LONG_ID
        page.detail_panel.task_id_copy.click()
        assert ("task_id", LONG_ID) in received
        page.detail_panel.hash_value_copy.click()
        assert received[-1][0] == "canonical_hash"
        assert len(received[-1][1]) == 64


class TestIsolationAndRender:
    def test_17_active_viewed_independent(self, qapp, provider):
        snapshot = fake_snapshot(task_id="task-B", title="B 标题", state="ACTIVE")
        page = _make_page(provider, snapshot)
        assert "task-B" in page.banner.text()
        page.load_task_detail("CHATGPT:task-A")
        assert page.selected_task_key == "CHATGPT:task-A"
        assert page.detail_panel.task_id_value.text() == "task-A"
        assert "task-B" in page.banner.text()
        assert snapshot.active_task.task_id == "task-B"

    def test_18_render_snapshot_update_keeps_viewed_a(self, qapp, provider):
        snapshot_b = fake_snapshot(task_id="task-B", title="B 标题", state="ACTIVE")
        page = _make_page(provider, snapshot_b)
        page.load_task_detail("CHATGPT:task-A")
        updated = fake_snapshot(task_id="task-B", title="B 更新后标题", state="ACTIVE")
        page.render(updated)
        assert "B 更新后标题" in page.banner.text() or "task-B" in page.banner.text()
        assert page.banner.text() == "✓ 当前真实活动任务：task-B · ACTIVE" or "task-B · ACTIVE" in page.banner.text()
        assert page.selected_task_key == "CHATGPT:task-A"
        assert page.detail_panel.task_id_value.text() == "task-A"
        assert page.selected_result_id is None


class TestQueryErrorAndRetry:
    def test_19_error_keeps_old_data(self, qapp, provider):
        page = _make_page(provider)
        assert page.tasks_table.rowCount() == 20
        provider.fail = True
        page.next_button.click()
        assert not page._error_bar.isHidden()
        assert page._error_label.text() != ""
        assert page.tasks_table.rowCount() == 20
        assert page.tasks_table.item(0, 0).text() == LONG_ID

    def test_20_retry_uses_same_query(self, qapp, provider):
        page = _make_page(provider)
        provider.fail = True
        page._load_list()
        failed = provider.list_calls[-1]
        assert not page._error_bar.isHidden()
        provider.fail = False
        page.retry_button.click()
        retried = provider.list_calls[-1]
        assert retried == failed
        assert retried["cursor_sequence"] is None
        assert page._error_bar.isHidden()
        assert page.tasks_table.rowCount() == 20


class TestLayoutAndConstraints:
    def test_22_narrow_single_column(self, qapp, provider):
        page = _make_page(provider)
        assert page.is_single_column is False
        page.set_single_column(True)
        assert page.is_single_column is True
        assert page.detail_panel.parent() is page

    def test_23_width_480_no_horizontal_overflow(self, qapp, provider):
        page = _make_page(provider)
        page.set_single_column(True)
        page.show()
        page.resize(480, 800)
        QApplication.processEvents()
        from PySide6.QtWidgets import QFrame

        for frame in page.findChildren(QFrame):
            if frame.isVisible():
                assert frame.width() <= page.width(), (
                    f"卡片 {frame.objectName() or frame.__class__.__name__} 宽度 {frame.width()} 溢出 {page.width()}"
                )

    def test_25_no_database_instantiation(self, qapp, provider, monkeypatch):
        import storage.database as db_import

        original = db_import.Database.__init__
        raised = []

        def _boom(self, *args, **kw):
            raised.append("instantiated")

        monkeypatch.setattr(db_import.Database, "__init__", _boom)
        page = _make_page(provider, fake_snapshot(task_id="task-B", state="ACTIVE"))
        assert raised == []
        monkeypatch.setattr(db_import.Database, "__init__", original)

    def test_26_no_clipboard_write(self, qapp, provider, monkeypatch):
        writes = []

        class _RecorderClipboard:
            def setText(self, text):
                writes.append(text)

            def text(self):
                return ""

        recorder = _RecorderClipboard()
        monkeypatch.setattr(QApplication, "clipboard", lambda self_qt: recorder)
        page = _make_page(provider)
        page.copy_result_requested.connect(lambda _rid: None)
        page.load_task_detail("CHATGPT:task-A")
        page.select_result("res-a-2")
        page.detail_panel.copy_reply_button.click()
        page.detail_panel.copy_sha_button.click()
        assert writes == []

    def test_27_no_re_run_business_buttons(self, qapp, provider):
        page = _make_page(provider)
        from PySide6.QtWidgets import QPushButton

        for button in page.findChildren(QPushButton):
            text = button.text()
            assert "重新执行" not in text
            assert "重跑" not in text
        assert page.retry_button.text() == "重试查询"


# ------------------------------------------- 意图辅助（供本文件内部断言）


def panel_block_texts(panel: TaskDetailPanel, attempt_id: str) -> str:
    for label in panel.findChildren(QLabel):
        if label.text() and f"attempt_id：{attempt_id}" in label.text():
            return label.text()
    raise AssertionError(f"找不到 attempt_id={attempt_id} 的展示块")


class TestB2RAtomicity:
    """B2R：query 失败时页面状态只在新 query 成功后 commit。
    验收 1-11：旧选择/旧详情/旧版本/旧 Result/旧 copy target/游标均保持。"""

    def test_01_detail_failure_keeps_old_viewed_and_result(self, qapp, provider):
        page = _make_page(provider)
        page.load_task_detail("CHATGPT:task-A")
        page.select_result("res-a-2")
        provider.fail_detail = True
        page.load_task_detail("CHATGPT:task-B")
        assert page.selected_task_key == "CHATGPT:task-A"
        assert page.selected_result_id == "res-a-2"
        assert page.detail_panel.task_id_value.text() == "task-A"
        assert page.detail_panel.version_table.item(0, 1).text() == "res-a-3"
        assert page.detail_panel.result_readable is True
        assert page.detail_panel.copy_reply_button.isEnabled() is True
        assert "res-a-2" in page.detail_panel._result_title.text()
        assert not page._error_bar.isHidden()
        assert provider.detail_calls[-1] == "CHATGPT:task-B"
        assert "task-B" not in page.detail_panel.task_id_value.text()

    def test_02_versions_failure_keeps_old_bundle(self, qapp, provider):
        page = _make_page(provider)
        page.load_task_detail("CHATGPT:task-A")
        provider.fail_versions = True
        page.load_task_detail("CHATGPT:task-B")
        assert page.selected_task_key == "CHATGPT:task-A"
        assert page.detail_panel.task_id_value.text() == "task-A"
        assert page.detail_panel.version_table.rowCount() == 3
        assert page.detail_panel.version_table.item(0, 1).text() == "res-a-3"
        assert page.detail_panel.version_table.item(1, 1).text() == "res-a-2"
        assert page.detail_panel.selected_result_id is None
        assert not page._error_bar.isHidden()

    def test_03_retry_bundle_switches_after_success(self, qapp, provider):
        page = _make_page(provider)
        page.load_task_detail("CHATGPT:task-A")
        provider.fail_versions = True
        page.load_task_detail("CHATGPT:task-B")
        assert page.selected_task_key == "CHATGPT:task-A"
        provider.fail_versions = False
        page.retry_button.click()
        assert page.selected_task_key == "CHATGPT:task-B"
        assert page.detail_panel.task_id_value.text() == "task-B"
        assert page.detail_panel.version_table.rowCount() == 0
        assert page.detail_panel.version_note.text() == "无 Result 版本"
        assert page.detail_panel.selected_result_id is None
        assert page.detail_panel.copy_result_id_button.isEnabled() is False
        assert page._error_bar.isHidden()

    def test_04_result_failure_keeps_old_copy_target(self, qapp, provider):
        page = _make_page(provider)
        page.load_task_detail("CHATGPT:task-A")
        page.select_result("res-a-2")
        provider.fail_result = True
        page.select_result("res-a-3")
        assert page.selected_result_id == "res-a-2"
        assert page.detail_panel.selected_result_id == "res-a-2"
        assert page.detail_panel.result_readable is True
        assert page.detail_panel.copy_reply_button.isEnabled() is True
        assert page.detail_panel.copy_sha_button.isEnabled() is True
        assert "res-a-2" in page.detail_panel._result_title.text()
        assert page.detail_panel._result_body.text() == "乙失败：精确读取的这一版"
        assert not page._error_bar.isHidden()
        assert provider.result_calls[-1] == "res-a-3"

    def test_05_next_page_failure_keeps_cursor(self, qapp, provider):
        page = _make_page(provider)
        assert page._next_cursor == 13
        provider.fail = True
        page.next_button.click()
        assert page.current_cursor is None
        assert page.cursor_history == []
        assert page.tasks_table.rowCount() == 20
        assert page.tasks_table.item(19, 0).text() == "task-17"
        assert not page._error_bar.isHidden()

    def test_06_previous_page_failure_keeps_cursor(self, qapp, provider):
        page = _make_page(provider)
        page.next_button.click()
        assert page.current_cursor == 13
        assert page.cursor_history == [None]
        provider.fail = True
        page.prev_button.click()
        assert page.current_cursor == 13
        assert page.cursor_history == [None]
        assert page.tasks_table.rowCount() == 5
        assert page.tasks_table.item(0, 0).text() == "task-18"
        assert not page._error_bar.isHidden()

    def test_07_search_failure_keeps_old_selection(self, qapp, provider):
        page = _make_page(provider)
        page.load_task_detail("CHATGPT:task-A")
        provider.fail = True
        page.search_input.setText("task-01")
        assert page.selected_task_key == "CHATGPT:task-A"
        assert page.selected_result_id is None
        assert page.detail_panel.task_id_value.text() == "task-A"
        assert page.current_cursor is None
        assert page.cursor_history == []
        assert not page._error_bar.isHidden()
        assert page.tasks_table.rowCount() == 20
        assert page.tasks_table.item(0, 0).text() == LONG_ID

    def test_08_filter_failure_keeps_old_selection(self, qapp, provider):
        page = _make_page(provider)
        page.load_task_detail("CHATGPT:task-A")
        provider.fail = True
        page.filter_combo.setCurrentText("COMPLETED")
        assert page.selected_task_key == "CHATGPT:task-A"
        assert page.detail_panel.task_id_value.text() == "task-A"
        assert not page._error_bar.isHidden()
        assert page.tasks_table.rowCount() == 20

    def test_09_retry_list_frozen_params(self, qapp, provider):
        page = _make_page(provider)
        page.search_input.setText("task-0")
        provider.fail = True
        page.filter_combo.setCurrentText("COMPLETED")
        failed = provider.list_calls[-1]
        assert failed["search_text"] == "task-0"
        assert failed["state_filter"] == ("COMPLETED",)
        provider.fail = False
        page.retry_button.click()
        retried = provider.list_calls[-1]
        assert retried == failed
        assert page._error_bar.isHidden()

    def test_10_corrupt_version_visible_with_reasons(self, qapp, provider):
        page = _make_page(provider)
        page.load_task_detail("CHATGPT:task-A")
        table = page.detail_panel.version_table
        row = None
        for r in range(table.rowCount()):
            if table.item(r, 1).text() == "res-a-1":
                row = r
                break
        assert row is not None
        assert table.item(row, 7).text() == "数据不完整"
        assert "结果正文解码失败" in table.item(row, 7).toolTip()
        assert table.item(row, 6).text() == "历史版本"

    def test_11_long_result_id_copy_full_and_missing_id_copyable(self, qapp, provider):
        page = _make_page(provider)
        emitted: list[tuple] = []
        page.copy_value_requested.connect(lambda kind, value: emitted.append((kind, value)))
        page.load_task_detail("CHATGPT:task-A")
        panel = page.detail_panel
        assert panel.copy_result_id_button.isEnabled() is False
        page.select_result(LONG_RESULT)
        assert panel.copy_result_id_button.isEnabled() is True
        panel.copy_result_id_button.click()
        assert emitted == [("result_id", LONG_RESULT)]
        emitted.clear()
        page.select_result("res-missing")
        assert panel.copy_reply_button.isEnabled() is False
        assert panel.copy_result_id_button.isEnabled() is True
        panel.copy_result_id_button.click()
        assert emitted == [("result_id", "res-missing")]