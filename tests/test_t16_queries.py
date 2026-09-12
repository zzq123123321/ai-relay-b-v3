"""T16-B1：只读 TaskQueries + 历史数据合同测试。

依据 A 端 T16-B1 卡十三：逐项覆盖分页、六状态、搜索、坏 JSON 安全、任务详情、
Attempt 续接链、Result 版本、exact result_id、delivery 精确关联、只读无 DML、
schema v1 不变。查询层全部只读，测试数据经 TaskStore.claim + 直接 SQL 构造。
"""

from __future__ import annotations

import hashlib
import pytest

from core.domain import ReceiveSettingsSnapshot, SessionBindingMode, TargetExecutor
from core.protocol_v1 import ProtocolFormat, content_digest, parse_message
from storage.database import Database
from storage.schema import read_schema_version
from storage.task_queries import TaskQueries, TaskQueryError
from storage.task_store import TaskStore, make_task_key

_T0 = "2026-10-01T09:00:00+00:00"
_DIR_LONG = r"D:\AIwork\长 路径:1\子目录"


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


def make_receive_snapshot(
    *,
    project_key: str = "proj-gamma",
    session_id: str | None = None,
    directory: str = r"D:\AIwork\proj",
    **kw,
) -> ReceiveSettingsSnapshot:
    binding = (
        SessionBindingMode.FIXED_SESSION
        if session_id is not None
        else SessionBindingMode.PROJECT_ROTATING
    )
    return ReceiveSettingsSnapshot(
        config_revision=kw.get("config_revision", 5),
        committed_at=kw.get("committed_at", _T0),
        received_at=kw.get("received_at", _T0),
        effective_executor=kw.get("effective_executor", TargetExecutor.OPENCHAMBER),
        directory=directory,
        project_key=project_key,
        agent=kw.get("agent", "build"),
        requested_model=kw.get("requested_model", ""),
        binding_mode=binding,
        frozen_session_id=session_id,
    )


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t16.sqlite")
    database.open()
    yield database
    database.close()


@pytest.fixture
def store(db):
    return TaskStore(db, queue_capacity=1000)


@pytest.fixture
def queries(db):
    return TaskQueries(db)


def _claim(
    store: TaskStore,
    task_id: str,
    *,
    peer_id: str = "CHATGPT",
    project_key: str = "proj-gamma",
    session_id: str | None = None,
    directory: str = r"D:\AIwork\proj",
    at: str = _T0,
    body: str = "处理任务",
):
    raw = _raw(task_id, body)
    msg = parse_message(raw)
    return store.claim(
        task_key=make_task_key(peer_id, task_id),
        peer_id=peer_id,
        task_id=task_id,
        protocol_format=ProtocolFormat.V1.value.upper(),
        raw_message=raw,
        body=msg.body,
        canonical_hash=content_digest(msg),
        receive_snapshot=make_receive_snapshot(
            project_key=project_key, session_id=session_id, directory=directory
        ),
        received_at=at,
    )


def _set_task(
    db,
    task_key: str,
    *,
    state: str,
    current_result_revision: int = 0,
    blocked_reason: str | None = None,
    active_attempt_id: str | None = None,
) -> None:
    db.connection.execute(
        "UPDATE tasks SET state=?, current_result_revision=?, blocked_reason=?,"
        " active_attempt_id=? WHERE task_key=?",
        (state, current_result_revision, blocked_reason, active_attempt_id, task_key),
    )


def _corrupt_ingress(db, task_key: str) -> None:
    db.connection.execute(
        "UPDATE tasks SET ingress_snapshot_json=? WHERE task_key=?",
        ("{not a json", task_key),
    )


def _exec_json(session_id: str | None, started_at: str) -> str:
    import json

    return json.dumps(
        {
            "receive": {"config_revision": 5, "received_at": _T0,
                        "project_key": "proj", "binding_mode": "FIXED_SESSION"},
            "binding_revision": 5,
            "resolved_session_id": session_id,
            "execution_started_at": started_at,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _insert_attempt(
    db,
    *,
    attempt_id: str,
    task_key: str,
    kind: str,
    state: str,
    started_at: str,
    session_id: str | None = None,
    parent_attempt_id: str | None = None,
    ended_at: str | None = None,
    corrupt_execution: bool = False,
) -> None:
    execution = (
        "{not a json" if corrupt_execution else _exec_json(session_id, started_at)
    )
    db.connection.execute(
        "INSERT INTO attempts (attempt_id, task_key, parent_attempt_id, kind, state,"
        " authority_epoch, execution_snapshot_json, remote_state, started_at, ended_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (attempt_id, task_key, parent_attempt_id, kind, state, 1, execution,
         "NOT_SENT", started_at, ended_at),
    )


def _insert_result(
    db,
    *,
    result_id: str,
    task_key: str,
    attempt_id: str,
    revision: int,
    state: str,
    source: str,
    body: str,
    committed_at: str = "2026-10-01T09:10:00+00:00",
    remote_ids: list[str] | None = None,
    corrupt_ids: bool = False,
) -> None:
    import json as _json

    protocol_text = body
    ids_json = (
        "{not a list"
        if corrupt_ids
        else _json.dumps(remote_ids or [], ensure_ascii=False)
    )
    db.connection.execute(
        "INSERT INTO results (result_id, task_key, attempt_id, revision, state, source,"
        " final_body, protocol_text, sha256, remote_message_ids_json, committed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (result_id, task_key, attempt_id, revision, state, source, body,
         protocol_text, _sha(body), ids_json, committed_at),
    )


def _insert_outbox(
    db, *, delivery_id: str, result_id: str, peer_id: str, state: str
) -> None:
    db.connection.execute(
        "INSERT INTO outbox (delivery_id, result_id, peer_id, state, profile,"
        " offered_count) VALUES (?,?,?,?,?,0)",
        (delivery_id, result_id, peer_id, state, "legacy_v1"),
    )


def _count(db, table: str) -> int:
    row = db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) if row else 0


def _build_alpha_env(db, store):
    """任务 A（COMPLETED，3 个结果版本 + outbox），任务 B（ACTIVE）与任务 C（坏 JSON）。"""
    a = _claim(store, "task-A", project_key="proj-alpha",
               session_id="sess-alpha", directory=_DIR_LONG)
    _insert_attempt(db, attempt_id="a1", task_key=a.task_key, kind="INITIAL",
                    state="COMPLETED", started_at="2026-10-01T09:00:05+00:00",
                    session_id="sess-alpha")
    _insert_attempt(db, attempt_id="a2", task_key=a.task_key, kind="MANUAL_RESOLUTION",
                    state="COMPLETED", started_at="2026-10-01T09:00:50+00:00",
                    session_id="sess-alpha", parent_attempt_id="a1")
    _insert_attempt(db, attempt_id="a3", task_key=a.task_key, kind="NEW_SESSION_RETRY",
                    state="SUPERSEDED", started_at="2026-10-01T09:01:00+00:00",
                    corrupt_execution=True, parent_attempt_id="a2")
    _insert_result(db, result_id="res-a-1", task_key=a.task_key, attempt_id="a1",
                   revision=1, state="COMPLETED", source="AUTO_RELAY", body="甲方案")
    _insert_result(db, result_id="res-a-2", task_key=a.task_key, attempt_id="a2",
                   revision=2, state="FAILED", source="MANUAL_WRAP", body="乙失败")
    _insert_result(db, result_id="res-a-3", task_key=a.task_key, attempt_id="a2",
                   revision=3, state="COMPLETED", source="MANUAL_WRAP",
                   body="丙成功端到端", remote_ids=["msg-9"])
    _insert_outbox(db, delivery_id="deliv-a3c", result_id="res-a-3",
                   peer_id="CHATGPT", state="ACKED")
    _insert_outbox(db, delivery_id="deliv-a3o", result_id="res-a-3",
                   peer_id="OTHER_AGENT", state="PENDING")
    _insert_outbox(db, delivery_id="deliv-a1", result_id="res-a-1",
                   peer_id="CHATGPT", state="OFFERED")
    _set_task(db, a.task_key, state="COMPLETED", current_result_revision=3)

    b = _claim(store, "task-B", project_key="proj-alpha", session_id="sess-beta")
    _insert_attempt(db, attempt_id="b1", task_key=b.task_key, kind="INITIAL",
                    state="OPEN", started_at="2026-10-01T08:59:00+00:00",
                    session_id="sess-beta")
    _set_task(db, b.task_key, state="ACTIVE", active_attempt_id="b1")

    c = _claim(store, "task-C", project_key="proj-alpha")
    _corrupt_ingress(db, c.task_key)
    _set_task(db, c.task_key, state="FAILED")
    return a, b, c


def _build_misc_env(db, store):
    q = _claim(store, "task-Q")
    blk = _claim(store, "task-BLK")
    _set_task(db, blk.task_key, state="BLOCKED", blocked_reason="global_slot_busy")
    s = _claim(store, "task-S", project_key="proj-delta")
    _insert_attempt(db, attempt_id="s1", task_key=s.task_key, kind="INITIAL",
                    state="STOPPED", started_at="2026-10-01T08:50:00+00:00")
    _insert_result(db, result_id="res-s-1", task_key=s.task_key, attempt_id="s1",
                   revision=1, state="STOPPED_BY_USER", source="USER_STOP",
                   body="用户停止")
    _set_task(db, s.task_key, state="STOPPED_BY_USER", current_result_revision=1)
    pct = _claim(store, "pct-50%-x")
    v7 = _claim(store, "value_7")
    vx = _claim(store, "valueX7")
    return q, blk, s, pct, v7, vx


class TestPagination:
    def test_first_page_sequence_desc(self, db, store, queries):
        env, misc = _build_alpha_env(db, store), _build_misc_env(db, store)
        page = queries.list_tasks(limit=4)
        assert len(page.items) == 4
        seqs = [row.sequence for row in page.items]
        assert seqs == sorted(seqs, reverse=True)
        assert page.has_more is True
        assert page.next_cursor == page.items[-1].sequence
        assert page.total_count == 9

    def test_keyset_second_page(self, db, store, queries):
        _build_alpha_env(db, store)
        _build_misc_env(db, store)
        page2 = queries.list_tasks(limit=4, cursor_sequence=6)
        assert [row.sequence for row in page2.items] == [5, 4, 3, 2]
        assert page2.has_more is True
        assert page2.next_cursor == 2
        page3 = queries.list_tasks(limit=4, cursor_sequence=2)
        assert [row.sequence for row in page3.items] == [1]
        assert page3.has_more is False
        assert page3.next_cursor is None
        assert page3.total_count == 9

    def test_new_task_does_not_pollute_old_cursor(self, db, store, queries):
        _build_alpha_env(db, store)
        _build_misc_env(db, store)
        _claim(store, "task-before-page-two")
        page2 = queries.list_tasks(limit=4, cursor_sequence=6)
        # 新任务 sequence=10 > 6，绝不混入以 6 为游标的下一页
        assert page2.next_cursor == 2
        tail = queries.list_tasks(limit=4, cursor_sequence=2)
        assert tail.has_more is False
        assert [row.sequence for row in tail.items] == [1]

    def test_invalid_limit_raises(self, db, store, queries):
        for bad in (0, 101, "x", 3.14, True):
            with pytest.raises(TaskQueryError):
                queries.list_tasks(limit=bad)

    def test_invalid_cursor_raises(self, db, store, queries):
        with pytest.raises(TaskQueryError):
            queries.list_tasks(cursor_sequence=0)


class TestStates:
    def test_all_six_states_browsable(self, db, store, queries):
        a, b, c = _build_alpha_env(db, store)
        q, blk, s, *_ = _build_misc_env(db, store)
        page = queries.list_tasks(limit=20)
        states = {row.state for row in page.items}
        assert states == {"QUEUED", "ACTIVE", "BLOCKED", "COMPLETED", "FAILED",
                          "STOPPED_BY_USER"}
        by_key = {row.task_key: row for row in page.items}
        assert by_key[q.task_key].state == "QUEUED"
        assert by_key[b.task_key].state == "ACTIVE"
        assert by_key[blk.task_key].state == "BLOCKED"
        assert by_key[blk.task_key].blocked_reason == "global_slot_busy"
        assert by_key[a.task_key].state == "COMPLETED"
        assert by_key[c.task_key].state == "FAILED"
        assert by_key[s.task_key].state == "STOPPED_BY_USER"

    def test_state_filter(self, db, store, queries):
        _build_alpha_env(db, store)
        _build_misc_env(db, store)
        page = queries.list_tasks(state_filter=("BLOCKED",))
        assert len(page.items) == 1
        assert page.items[0].task_id == "task-BLK"
        assert page.total_count == 1
        active = queries.list_tasks(state_filter=("ACTIVE", "QUEUED"))
        assert len(active.items) == 5
        assert any(row.state == "ACTIVE" for row in active.items)
        assert all(row.state in ("ACTIVE", "QUEUED") for row in active.items)

    def test_unknown_state_filter_raises(self, db, store, queries):
        with pytest.raises(TaskQueryError):
            queries.list_tasks(state_filter=("NOPE",))
        with pytest.raises(TaskQueryError):
            queries.list_tasks(state_filter=("QUEUED", 1))

    def test_empty_state_filter_means_no_filter(self, db, store, queries):
        _build_alpha_env(db, store)
        _build_misc_env(db, store)
        page = queries.list_tasks(state_filter=())
        assert page.total_count == 9


class TestSearch:
    def test_task_id_substring(self, db, store, queries):
        _build_alpha_env(db, store)
        _build_misc_env(db, store)
        page = queries.list_tasks(search_text="task-BLK")
        assert len(page.items) == 1
        assert page.items[0].task_id == "task-BLK"
        assert page.total_count == 1

    def test_project_key_substring(self, db, store, queries):
        a, b, c = _build_alpha_env(db, store)
        _build_misc_env(db, store)
        page = queries.list_tasks(search_text="proj-alpha")
        assert {row.task_key for row in page.items} == {a.task_key, b.task_key}
        assert page.total_count == 2

    def test_result_id_substring_no_task_duplication(self, db, store, queries):
        a, _, _ = _build_alpha_env(db, store)
        page = queries.list_tasks(search_text="res-a-1")
        assert len(page.items) == 1
        assert page.items[0].task_key == a.task_key
        assert page.total_count == 1

    def test_percent_is_literal_not_wildcard(self, db, store, queries):
        _build_alpha_env(db, store)
        q, blk, s, pct, v7, vx = _build_misc_env(db, store)
        page = queries.list_tasks(search_text="%")
        assert [row.task_key for row in page.items] == [pct.task_key]

    def test_underscore_is_literal_not_wildcard(self, db, store, queries):
        _build_alpha_env(db, store)
        q, blk, s, pct, v7, vx = _build_misc_env(db, store)
        page = queries.list_tasks(search_text="value_")
        assert [row.task_key for row in page.items] == [v7.task_key]
        assert all("valueX7" != row.task_id for row in page.items)

    def test_search_combined_with_state_filter(self, db, store, queries):
        _build_alpha_env(db, store)
        _build_misc_env(db, store)
        page = queries.list_tasks(search_text="proj-gamma", state_filter=("QUEUED",))
        assert all(row.state == "QUEUED" for row in page.items)
        assert all(row.project_key == "proj-gamma" for row in page.items)
        assert len(page.items) == 4

    def test_empty_search_means_no_filter(self, db, store, queries):
        _build_alpha_env(db, store)
        _build_misc_env(db, store)
        assert queries.list_tasks(search_text="").total_count == 9
        assert queries.list_tasks(search_text=None).total_count == 9


class TestCorruptData:
    def test_malformed_ingress_not_drag_pagination(self, db, store, queries):
        a, b, c = _build_alpha_env(db, store)
        _build_misc_env(db, store)
        page = queries.list_tasks(limit=20)
        row = next(r for r in page.items if r.task_key == c.task_key)
        assert row.task_id == "task-C"
        assert row.project_key is None
        assert row.directory is None
        assert any("无法解析" in reason for reason in row.corrupt_reasons)
        assert len(page.items) == 9

    def test_malformed_ingress_not_crash_project_search(self, db, store, queries):
        a, b, c = _build_alpha_env(db, store)
        _build_misc_env(db, store)
        page = queries.list_tasks(search_text="proj-alpha")
        assert {row.task_key for row in page.items} == {a.task_key, b.task_key}
        by_id = queries.list_tasks(search_text="task-C")
        assert len(by_id.items) == 1
        assert "无法解析为 JSON" in by_id.items[0].corrupt_reasons[0]

    def test_detail_of_corrupt_task_still_readable(self, db, store, queries):
        a, b, c = _build_alpha_env(db, store)
        detail = queries.get_task_detail(c.task_key)
        assert detail is not None
        assert detail.task_id == "task-C"
        assert detail.project_key is None
        assert detail.directory is None
        assert detail.raw_message != ""
        assert any("无法解析" in reason for reason in detail.corrupt_reasons)


class TestTaskDetail:
    def test_detail_raw_message_and_body_complete(self, db, store, queries):
        a, b, c = _build_alpha_env(db, store)
        detail = queries.get_task_detail(a.task_key)
        assert detail is not None
        assert detail.task_id == "task-A"
        assert detail.project_key == "proj-alpha"
        assert detail.directory == _DIR_LONG
        assert detail.raw_message == _raw("task-A", "处理任务")
        assert detail.body == "处理任务"
        assert detail.canonical_hash == content_digest(parse_message(detail.raw_message))
        assert detail.config_revision == 5
        assert detail.current_result_revision == 3

    def test_detail_missing_returns_none(self, db, store, queries):
        assert queries.get_task_detail("no-such-task") is None

    def test_attempt_chain_includes_parent(self, db, store, queries):
        a, b, c = _build_alpha_env(db, store)
        attempts = queries.get_task_detail(a.task_key).attempts
        assert [x.attempt_id for x in attempts] == ["a1", "a2", "a3"]
        assert [x.started_at for x in attempts] == sorted(x.started_at for x in attempts)
        assert attempts[0].parent_attempt_id is None
        assert attempts[1].parent_attempt_id == "a1"
        assert attempts[2].parent_attempt_id == "a2"
        resolved = {x.attempt_id: x.resolved_session_id for x in attempts}
        assert resolved["a1"] == "sess-alpha"
        assert resolved["a2"] == "sess-alpha"

    def test_corrupt_execution_snapshot_degrades_safely(self, db, store, queries):
        a, b, c = _build_alpha_env(db, store)
        attempts = queries.get_task_detail(a.task_key).attempts
        a3 = next(x for x in attempts if x.attempt_id == "a3")
        assert a3.resolved_session_id is None
        assert any("无法解析为 JSON" in r for r in a3.corrupt_reasons)

    def test_list_task_attempts_public_contract(self, db, store, queries):
        a, b, c = _build_alpha_env(db, store)
        attempts = queries.list_task_attempts(a.task_key)
        assert [x.attempt_id for x in attempts] == ["a1", "a2", "a3"]
        assert queries.list_task_attempts("no-such-task") == ()


class TestResultVersions:
    def test_versions_revision_desc(self, db, store, queries):
        a, b, c = _build_alpha_env(db, store)
        versions = queries.list_task_result_versions(a.task_key)
        assert [v.revision for v in versions] == [3, 2, 1]
        assert versions[0].result_id == "res-a-3"

    def test_authoritative_only_from_current_result_revision(self, db, store, queries):
        a, b, c = _build_alpha_env(db, store)
        versions = {v.revision: v for v in queries.list_task_result_versions(a.task_key)}
        assert [v.revision for v in versions.values() if v.authoritative] == [3]
        assert versions[3].authoritative is True
        assert versions[2].authoritative is False
        assert versions[1].authoritative is False

    def test_delivery_state_matched_to_task_peer(self, db, store, queries):
        a, b, c = _build_alpha_env(db, store)
        versions = {v.revision: v for v in queries.list_task_result_versions(a.task_key)}
        assert versions[3].delivery_state == "ACKED"
        assert versions[1].delivery_state == "OFFERED"
        assert versions[2].delivery_state is None

    def test_versions_missing_task_returns_empty(self, db, store, queries):
        assert queries.list_task_result_versions("no-such-task") == ()


class TestExactResultId:
    def test_exact_a_r2(self, db, store, queries):
        a, b, c = _build_alpha_env(db, store)
        detail = queries.get_result("res-a-2")
        assert detail is not None
        assert detail.result_id == "res-a-2"
        assert detail.revision == 2
        assert detail.task_key == a.task_key
        assert detail.final_body == "乙失败"
        assert detail.authoritative is False

    def test_missing_result_returns_none_no_fallback(self, db, store, queries):
        _build_alpha_env(db, store)
        assert queries.get_result("不存在") is None
        assert queries.get_result("res-a-9") is None
        assert queries.get_result("") is None

    def test_all_three_result_states_readable(self, db, store, queries):
        _build_alpha_env(db, store)
        _build_misc_env(db, store)
        assert queries.get_result("res-a-1").state == "COMPLETED"
        assert queries.get_result("res-a-2").state == "FAILED"
        assert queries.get_result("res-s-1").state == "STOPPED_BY_USER"

    def test_authoritative_flag_on_authoritative_result(self, db, store, queries):
        a, b, c = _build_alpha_env(db, store)
        detail = queries.get_result("res-a-3")
        assert detail.authoritative is True
        assert detail.delivery_state == "ACKED"
        assert detail.remote_message_ids == ("msg-9",)


class TestLongValues:
    def test_long_ids_and_windows_directory_not_truncated(self, db, store, queries):
        long_task_id = "T" * 120
        long_result_id = "R" * 120
        a = _claim(store, long_task_id, project_key="proj-long",
                   session_id="sess-long",
                   directory=r"D:\AIwork\很长的 项目 路径:88\子")
        _insert_attempt(db, attempt_id="long-a1", task_key=a.task_key,
                        kind="INITIAL", state="COMPLETED",
                        started_at="2026-10-01T09:02:00+00:00", session_id="sess-long")
        _insert_result(db, result_id=long_result_id, task_key=a.task_key,
                       attempt_id="long-a1", revision=1, state="COMPLETED",
                       source="AUTO_RELAY", body="长值结果正文")
        _set_task(db, a.task_key, state="COMPLETED", current_result_revision=1)

        row = queries.list_tasks(search_text=long_task_id).items[0]
        assert row.task_id == long_task_id
        assert ":" in row.directory and " " in row.directory

        detail = queries.get_task_detail(a.task_key)
        assert detail.task_id == long_task_id
        assert detail.directory == r"D:\AIwork\很长的 项目 路径:88\子"

        result = queries.get_result(long_result_id)
        assert result is not None
        assert result.result_id == long_result_id
        assert result.revision == 1
        assert ":" in result.task_key


class TestReadOnly:
    def test_queries_produce_no_dml(self, db, store, queries):
        a, b, c = _build_alpha_env(db, store)
        _build_misc_env(db, store)
        before_counts = {t: _count(db, t) for t in ("tasks", "attempts", "results", "outbox")}
        before_schema = read_schema_version(db.connection)
        before_changes = int(db.connection.execute("SELECT total_changes()").fetchone()[0])

        queries.list_tasks(limit=4)
        queries.list_tasks(search_text="proj-alpha", state_filter=("ACTIVE",))
        queries.get_task_detail(a.task_key)
        queries.list_task_attempts(a.task_key)
        queries.list_task_result_versions(a.task_key)
        queries.get_result("res-a-2")
        queries.get_result("不存在")

        after_changes = int(db.connection.execute("SELECT total_changes()").fetchone()[0])
        assert after_changes == before_changes
        assert {t: _count(db, t) for t in before_counts} == before_counts
        assert read_schema_version(db.connection) == before_schema

    def test_queries_run_with_query_only_pragma(self, db, store, queries):
        a, b, c = _build_alpha_env(db, store)
        _build_misc_env(db, store)
        try:
            db.connection.execute("PRAGMA query_only=ON")
            page = queries.list_tasks(limit=4)
            assert page.total_count == 9
            assert queries.get_task_detail(a.task_key) is not None
            assert queries.get_result("res-a-3") is not None
        finally:
            db.connection.execute("PRAGMA query_only=OFF")

    def test_schema_version_remains_v1(self, db, store, queries):
        _build_alpha_env(db, store)
        assert read_schema_version(db.connection) == 1
        assert db.schema_version() == 1