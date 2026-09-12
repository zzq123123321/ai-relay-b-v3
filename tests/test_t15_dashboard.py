"""T15-B2 Dashboard 定向测试（真实 widget 渲染）。

验收依据：规格 14.2 / 14.3 + T15-A 审核定稿 + T15-B2 任务卡。
覆盖：当前任务卡字段与缺省值、正式三卡独立表达、7 节点恢复条、
4 段阶段条（COMPLETED≠交付完成）、累计/连续独立计数、事件 ≤6、
队列计数 vs 摘要、stale 连接、空状态、BLOCKED 文字、单列响应式、
长中文不撑宽、无假百分比。
"""

import os
import sys
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.snapshots import (  # noqa: E402
    AutoResumeSnapshot,
    ConnectionSnapshot,
    EventSnapshot,
    RecoverySnapshot,
    empty_snapshot,
    fake_snapshot,
)
from ui.dashboard import Dashboard  # noqa: E402

T = datetime(2026, 9, 10, 12, 34, 56)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    app.setStyleSheet("")


def _dash(snapshot=None, width=1280, height=820):
    dash = Dashboard(snapshot if snapshot is not None else fake_snapshot())
    dash.resize(width, height)
    dash.show()
    QApplication.processEvents()
    return dash


def _snap(**kwargs):
    defaults = dict(
        task_id="task-1",
        sequence=1,
        attempt_id="a1",
        epoch=1,
        state="ACTIVE",
        phase="WATCHING",
        resume_total=0,
        consecutive_no_progress=0,
    )
    defaults.update(kwargs)
    phase = defaults.pop("phase")
    kwargs = dict(defaults)
    rec = RecoverySnapshot(phase=phase, resume_total=kwargs.get("resume_total", 0),
                           consecutive_no_progress=kwargs.get("consecutive_no_progress", 0))
    kwargs.pop("resume_total", None)
    kwargs.pop("consecutive_no_progress", None)
    if "epoch" in kwargs:
        kwargs["authority_epoch"] = kwargs.pop("epoch")
    return fake_snapshot(**kwargs, recovery=rec)


# ------------------------------------------------------------ 当前任务卡


def test_current_task_rendering_and_missing_values(qapp):
    snap = fake_snapshot(
        task_id="t1", title="中文标题", project="p1", session="s1",
        requested_model="m1", parsed_model="m2", actual_model="m3",
        config_revision="cfg-9", received_at=T, state="ACTIVE",
    )
    dash = _dash(snap)
    assert dash.task_card.isVisible()
    assert dash._task_id.text() == "task_id：t1"  # noqa: SLF001
    assert dash._task_title.text() == "中文标题"  # noqa: SLF001
    assert dash._task_meta.text() == "project：p1｜session：s1"
    assert "m1" in dash._models_label.text() and "m3" in dash._models_label.text()
    assert "cfg-9" in dash._config_label.text()
    assert "2026-09-10 12:34:56" in dash._times_label.text()  # noqa: SLF001


def test_missing_model_shows_unspecified_or_unknown(qapp):
    dash = _dash(fake_snapshot(title=None, requested_model=None,
                               parsed_model=None, actual_model=None, config_revision=None))
    assert dash._task_title.text() == "未指定"  # noqa: SLF001
    assert "未指定" in dash._models_label.text()
    assert "未知" in dash._config_label.text()


def test_no_timer_and_no_fake_seconds(qapp):
    """开始信息只来自快照时间；T15 不启动计时器自增秒数。"""
    dash = _dash(fake_snapshot(received_at=T))
    assert "12:34:56" in dash._times_label.text()  # noqa: SLF001
    assert not hasattr(dash, "timer")


def test_no_previous_task_residue_when_empty(qapp):
    """空状态不得残留上一任务标题/session/模型/恢复相位。"""
    prev = fake_snapshot(
        task_id="old", title="旧标题", session="old-session",
        requested_model="old-model", recovery=RecoverySnapshot(phase="COOLDOWN"),
    )
    dash = _dash(prev)
    dash.render(empty_snapshot(waiting_task_count=3))
    QApplication.processEvents()
    assert dash._empty_card.isVisible()
    assert not dash.task_card.isVisible()
    texts = " ".join([dash._empty_label.text(), dash._queue_empty_line.text()])
    assert "旧标题" not in texts
    assert "old-session" not in texts
    assert "old-model" not in texts
    assert "冷却" not in texts


# ------------------------------------------------------------ 正式三卡


def test_three_status_cards_are_independent_blocks(qapp):
    """连接正常、续接暂停、等待数量——三个卡片必须各自表达，不合并成一个绿灯。"""
    auto = AutoResumeSnapshot(enabled=False, paused_by_user=True)
    conn = ConnectionSnapshot(source="S1", transport_ok=True, is_stale=False)
    dash = _dash(fake_snapshot(
        connection=conn,
        auto_resume=auto,
        waiting_task_count=27,
        recovery=RecoverySnapshot(phase="COOLDOWN", resume_total=7, consecutive_no_progress=3),
    ))
    assert "连接正常" in dash._conn_badge.text()
    assert "已暂停" in dash._auto_badge.text()
    assert "等待任务：27" in dash._queue_count.text()
    assert dash.is_single_column is False


def test_stale_connection_not_green_even_when_legacy_healthy(qapp):
    """connection_healthy=True + connection.is_stale=True → 确定不绿。"""
    conn = ConnectionSnapshot(source="S1", transport_ok=True, is_stale=True,
                              last_observed_at=T)
    dash = _dash(fake_snapshot(connection_healthy=True, connection=conn))
    assert "可能已过期" in dash._conn_badge.text()
    assert dash._conn_badge.property("tone") == "recovering"


# ------------------------------------------------------------ 7 节点恢复条


def test_seven_recovery_nodes_rendered(qapp):
    dash = _dash(_snap(phase="WAIT_NETWORK"))
    nodes = dash.recovery_node_texts()
    assert len(nodes) == 7
    assert "等待网络" in nodes[0]
    assert "冷却" in nodes[6]
    assert "已恢复执行" in nodes[5]


def test_wait_network_node_is_current_not_failed(qapp):
    dash = _dash(_snap(phase="WAIT_NETWORK"))
    assert dash._recovery_headline.text() == "⚠ 等待网络"
    assert dash._recovery_headline.property("tone") == "recovering"


# ------------------------------------------------------------ 4 段阶段条


def test_four_progress_stages(qapp):
    dash = _dash(fake_snapshot(progress_stage="RESULT_SAVED", progress_stage_completed=False))
    stages = dash.stage_texts()
    assert len(stages) == 4
    assert "已接收" in stages[0] and "执行与验证" in stages[1]
    assert "结果保存" in stages[2] and "结果交付" in stages[3]


def test_completed_but_delivery_pending_keeps_fourth_incomplete(qapp):
    """Task=COMPLETED 但 RESULT_DELIVERY 未完成 → 第四段仍进行中，不能全亮。"""
    dash = _dash(fake_snapshot(
        state="COMPLETED", progress_stage="RESULT_DELIVERY", progress_stage_completed=False,
    ))
    stages = dash.stage_texts()
    assert "● 结果交付" in stages[3]     # current，不是 done
    assert "✓" in stages[2]             # 前三段 done


# ------------------------------------------------------------ 计数与队列


def test_counters_seven_zero_independent_and_no_failure_phrase(qapp):
    dash = _dash(_snap(resume_total=7, consecutive_no_progress=0, phase="WAIT_NETWORK"))
    total, consecutive = dash.counter_texts()
    assert total == "累计续接 7 次"
    assert consecutive == "连续未恢复 0 次"
    for forbidden in ("失败次数", "耗尽", "达到最大", "停止自动续接"):
        assert forbidden not in total + consecutive


def test_queue_count_and_brief_are_separate(qapp):
    from app.snapshots import QueueItemSnapshot

    items = tuple(QueueItemSnapshot(task_id=f"t{i}", sequence=i, title=f"摘要{i}") for i in range(5))
    dash = _dash(fake_snapshot(
        waiting_task_count=27, queue_brief=items,
        recovery=RecoverySnapshot(phase="NONE"),
    ))
    assert "等待任务：27" in dash._queue_count.text()
    brief = dash._queue_brief.text()
    assert "摘要0" in brief
    assert "等待任务：3" not in dash._queue_count.text()  # 计数不得被摘要截短污染


def test_events_at_most_six(qapp):
    events = tuple(
        EventSnapshot(occurred_at=datetime(2026, 9, 10, 10, 0, i), summary=f"事件{i}")
        for i in range(9)
    )
    dash = _dash(fake_snapshot(events=events, recovery=RecoverySnapshot(phase="WATCHING")))
    rows = dash._events_box.count()  # noqa: SLF001
    assert rows == 6


# ------------------------------------------------------------ 空状态 / BLOCKED


def test_empty_state_shows_receiving_conn_and_queue(qapp):
    snap = empty_snapshot(
        receiving_enabled=True,
        connection_healthy=True,
        connection_source="S1",
        waiting_task_count=5,
    )
    dash = _dash(snap)
    assert dash._empty_label.text() == "等待 A 端任务"  # noqa: SLF001
    assert "接收 开启" in dash._recv_empty_badge.text()  # noqa: SLF001
    assert "连接" in dash._conn_empty_line.text()
    assert "等待任务：5" in dash._queue_empty_line.text()


def test_blocked_shows_reason_text_only_no_retry_button(qapp):
    rec = RecoverySnapshot(phase="BLOCKED", blocked_reason="SESSION_MISSING")
    dash = _dash(fake_snapshot(state="BLOCKED", recovery=rec))
    assert dash.blocked_hint() == "原会话记录缺失，需要人工核对"
    # 无业务按钮：本页没有任何 QPushButton
    from PySide6.QtWidgets import QPushButton

    assert dash.findChildren(QPushButton) == []


# ------------------------------------------------------------ 响应式


def test_single_column_switches_layout_and_no_horizontal_overflow(qapp):
    dash = _dash(fake_snapshot(state="ACTIVE"), width=480, height=820)
    dash.set_single_column(True)
    QApplication.processEvents()
    assert dash.is_single_column is True
    width = dash.width()
    for card in (dash.task_card, dash.recovery_card, dash.stage_card, dash.rail):
        assert card.geometry().right() <= width + 1 and card.geometry().left() >= -1, card
    # 右栏在单列下放宽
    assert dash.rail.maximumWidth() > 330


def test_long_chinese_does_not_force_main_window_wide(qapp):
    long_title = "很长的中文任务标题，" * 12 + "用于验证 wordWrap 与不撑宽"
    snap = fake_snapshot(title=long_title, recovery=RecoverySnapshot(phase="WATCHING"))
    dash = _dash(snap, width=480, height=820)
    dash.set_single_column(True)
    QApplication.processEvents()
    assert dash._task_title.geometry().right() <= dash.width() + 1  # noqa: SLF001


def test_no_fake_percentage_in_any_label(qapp):
    """工作台所有可见文本不得出现任何百分比。"""
    from PySide6.QtWidgets import QLabel

    dash = _dash(fake_snapshot(
        progress_stage="RESULT_DELIVERY", progress_stage_completed=False,
        recovery=RecoverySnapshot(phase="COOLDOWN", resume_total=7, consecutive_no_progress=3),
    ))
    all_text = " ".join(w.text() for w in dash.findChildren(QLabel))
    assert "%" not in all_text


# ------------------------------------------------------------ 焦点


def test_focus_target_is_label_and_focusable(qapp):
    dash = _dash(fake_snapshot(state="ACTIVE"))
    assert dash.focus_target is dash._task_title  # noqa: SLF001
    assert dash.focus_target.focusPolicy() == Qt.StrongFocus
    empty = _dash(empty_snapshot())
    assert empty.focus_target is empty._empty_label  # noqa: SLF001