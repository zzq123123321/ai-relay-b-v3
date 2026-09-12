"""T15-B1 Presenter 层（ui/status_presenter.py）定向测试。

验收依据：UI-A02 / UI-B01 / UI-B02 / UI-B03 原文（17.5 / 149 项）与
A 端 T15-A 审核定稿：7 节点恢复条、4 段阶段条来自 Snapshot 权威、
累计/连续双计数、sequence 身份守卫、时间 stale 与身份 stale 分离、
next_action 只来自快照、无假百分比、presenter 静态隔离（无 Qt/DB/client）。
"""

import io
import sys
import tokenize as _tokenize
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.snapshots import (  # noqa: E402
    AutoResumeSnapshot,
    ConnectionSnapshot,
    EventSnapshot,
    RecoverySnapshot,
    fake_snapshot,
)
from ui.status_presenter import (  # noqa: E402
    RECOVERY_STEP_LABELS,
    action_hint,
    connection_presentation,
    counters,
    event_rows,
    is_superseded_update,
    present_status,
    progress_presentation,
    queue_brief_rows,
    recovery_steps,
)

T = datetime(2026, 9, 10, 12, 34, 56)


def _snap(
    task_id="task-1",
    sequence=1,
    attempt_id="a1",
    epoch=1,
    state="ACTIVE",
    phase="WATCHING",
    interruption_id=None,
    last_real_progress_at=None,
    resume_total=0,
    consecutive_no_progress=0,
    next_check_at=None,
    conn_struct=None,
    conn_healthy=False,
    conn_source=None,
    auto=None,
    progress_stage=None,
    progress_completed=None,
    blocked_reason=None,
):
    rec = RecoverySnapshot(
        phase=phase,
        interruption_id=interruption_id,
        resume_total=resume_total,
        consecutive_no_progress=consecutive_no_progress,
        next_check_at=next_check_at,
        last_real_progress_at=last_real_progress_at,
        blocked_reason=blocked_reason,
    )
    return fake_snapshot(
        task_id=task_id,
        sequence=sequence,
        attempt_id=attempt_id,
        authority_epoch=epoch,
        state=state,
        connection=conn_struct,
        connection_healthy=conn_healthy,
        connection_source=conn_source,
        auto_resume=auto,
        recovery=rec,
        progress_stage=progress_stage,
        progress_stage_completed=progress_completed,
    )


# ------------------------------------------------- UI-A02 / UI-B01 状态条


def test_ui_a02_wait_network_is_recovering_not_failed():
    """UI-A02 运行态：临时断网必须显示为“等待网络”琥珀色，不得判为失败。"""
    s = _snap(phase="WAIT_NETWORK", next_check_at=T)
    p = present_status(s)
    assert p.headline == "等待网络"
    assert p.tone == "recovering"
    assert p.is_terminal is False
    assert "网络暂不可达" in p.detail
    assert p.next_action_text == "下次检查 12:34:56"

    steps = recovery_steps(s)
    assert len(steps) == 7
    assert steps[0].label == "等待网络"
    assert steps[0].state == "current"
    assert steps[4].state == "todo"  # 未恢复成功前，后面的节点保持 todo


def test_ui_b01_recovery_chain_does_not_claim_success_before_progress():
    """UI-B01：等待→核对→续接→待进展；任何未确认真进展的阶段不得显示“已恢复”。"""
    chain_cases = [
        ("VERIFYING", "恢复核对", 1),
        ("SUSPECTED", "计划复查", 2),
        ("SCHEDULED", "计划复查", 2),
        ("SENDING", "续接发送", 3),
        ("AWAITING_PROGRESS", "续接待进展", 4),
    ]
    for phase, headline, idx in chain_cases:
        s = _snap(phase=phase)
        p = present_status(s)
        assert p.headline == headline, phase
        assert p.tone == "recovering", phase
        assert p.highlight_step_index == idx, phase
        assert p.is_terminal is False, phase
        assert "已恢复" not in p.headline, phase
        # 第 6 节点“已恢复执行”必须是 todo，不能提前亮
        assert recovery_steps(s)[5].state == "todo", phase


def test_recovered_execution_shows_only_after_real_progress():
    """已恢复执行仅在 WATCHING + interruption + 真实进展 三条件齐备时出现。"""
    watch_normal = _snap(phase="WATCHING")
    assert present_status(watch_normal).headline == "正常执行"
    assert present_status(watch_normal).tone == "success"

    watch_recovered = _snap(
        phase="WATCHING",
        interruption_id="i-7",
        last_real_progress_at=T,
    )
    p = present_status(watch_recovered)
    assert p.headline == "已恢复执行"
    assert p.tone == "success"
    assert recovery_steps(watch_recovered)[5].state == "done"
    assert p.highlight_step_index == 5


def test_recovery_bar_always_has_exactly_seven_nodes():
    """恢复状态条只有 7 个用户可见节点；分支状态不占节点。"""
    assert len(RECOVERY_STEP_LABELS) == 7
    assert RECOVERY_STEP_LABELS == (
        "等待网络", "恢复核对", "计划复查", "续接发送",
        "已入会话待进展", "已恢复执行", "冷却",
    )
    for phase in ("WAIT_NETWORK", "COOLDOWN", "PAUSED", "BLOCKED", "NONE"):
        assert len(recovery_steps(_snap(phase=phase))) == 7, phase


def test_paused_is_distinct_from_stopped_by_user():
    """PAUSED（自动续接暂停）≠ STOPPED_BY_USER（任务已停止）。"""
    auto = AutoResumeSnapshot(enabled=False, paused_by_user=True)
    paused = _snap(phase="WAIT_NETWORK", auto=auto)
    p = present_status(paused)
    assert p.headline == "自动续接暂停"
    assert p.tone == "neutral"
    assert p.is_terminal is False

    stopped = _snap(state="STOPPED_BY_USER")
    sp = present_status(stopped)
    assert sp.headline == "已停止"
    assert sp.is_terminal is True


def test_blocked_shows_reason_and_action_hint_text():
    """BLOCKED 只展示阻断原因与处理提示（文本），不自动恢复。"""
    s = _snap(phase="BLOCKED", blocked_reason="SESSION_MISSING")
    p = present_status(s)
    assert p.headline == "待人工处理"
    assert "恢复被阻断" in p.detail
    assert "原会话记录缺失" in p.detail
    assert action_hint("SESSION_MISSING") == "原会话记录缺失，需要人工核对"
    assert action_hint("USER_ACTION") is None
    assert action_hint("NOT_A_REASON") == "请检查任务状态后处理"


def test_waiting_empty_state_is_neutral_not_failure():
    """无任务时空状态为中性等待，不显示连接失败。"""
    from app.snapshots import empty_snapshot

    p = present_status(empty_snapshot())
    assert p.headline == "等待接收任务"
    assert p.tone == "neutral"
    assert p.is_terminal is False


# ------------------------------------------------ UI-B02 / UI-B03 计数与冷却


def test_ui_b02_seven_total_zero_consecutive_does_not_exhaust_recovery():
    """UI-B02：累计续接 7 次之后再次断网仍可继续自动续接。"""
    s = _snap(phase="WAIT_NETWORK", resume_total=7, consecutive_no_progress=0)
    p = present_status(s)
    assert p.tone == "recovering"
    assert p.is_terminal is False
    assert p.headline == "等待网络"  # 再次中断 → 回到等待，而不是失败/停止

    c = counters(s)
    assert c.total_text == "累计续接 7 次"
    assert c.consecutive_text == "连续未恢复 0 次"
    all_text = " ".join([p.headline, p.detail, c.total_text, c.consecutive_text])
    for forbidden in ("达到最大次数", "停止续接", "恢复耗尽", "连续失败 7 次"):
        assert forbidden not in all_text


def test_ui_b03_cooldown_three_consecutive_then_real_progress_resets_only_consecutive():
    """UI-B03：连续 3 次未确认 → 冷却 + 下一次检查时间；真实进展只清零连续。"""
    before = _snap(
        phase="COOLDOWN",
        resume_total=7,
        consecutive_no_progress=3,
        next_check_at=T,
    )
    p = present_status(before)
    assert p.headline == "冷却中"
    assert p.tone == "recovering"
    assert p.is_terminal is False
    assert "连续 3 次" in p.detail
    assert p.next_action_text == "下次检查 12:34:56"
    assert recovery_steps(before)[6].state == "current"

    after = _snap(
        phase="WATCHING",
        interruption_id="i-8",
        resume_total=8,
        consecutive_no_progress=0,
        last_real_progress_at=T,
    )
    pa = present_status(after)
    assert pa.headline == "已恢复执行"
    ca = counters(after)
    assert ca.total_text == "累计续接 8 次"           # 累计只增不清零
    assert ca.consecutive_text == "连续未恢复 0 次"    # 仅连续被清零


# ------------------------------------------------------ 身份守卫（sequence）


def test_newer_sequence_replaces_old_task():
    """新任务（sequence 更大）允许替换，即使 task_id 不同。"""
    cand = _snap(task_id="t2", sequence=5, state="QUEUED")
    curr = _snap(task_id="t1", sequence=4, state="ACTIVE")
    assert is_superseded_update(cand, curr) is False


def test_older_sequence_is_rejected():
    """旧 sequence 回调必须被拒绝。"""
    cand = _snap(task_id="t1", sequence=3)
    curr = _snap(task_id="t1", sequence=5)
    assert is_superseded_update(cand, curr) is True


def test_same_sequence_different_task_id_is_rejected():
    """同 sequence 但 task_id 异常不同 → 拒绝。"""
    cand = _snap(task_id="t2", sequence=5)
    curr = _snap(task_id="t1", sequence=5)
    assert is_superseded_update(cand, curr) is True


def test_same_sequence_lower_epoch_is_rejected():
    """同 sequence 低 epoch → 旧代，拒绝。"""
    cand = _snap(sequence=5, epoch=1)
    curr = _snap(sequence=5, epoch=2)
    assert is_superseded_update(cand, curr) is True


def test_same_sequence_higher_epoch_is_allowed():
    """同 sequence 高 epoch → 新代，允许。"""
    cand = _snap(sequence=5, epoch=2)
    curr = _snap(sequence=5, epoch=1)
    assert is_superseded_update(cand, curr) is False


def test_same_epoch_old_attempt_is_rejected():
    """同 sequence 同 epoch 但 attempt 不同 → 旧 attempt 拒绝。"""
    cand = _snap(sequence=5, epoch=1, attempt_id="a-old")
    curr = _snap(sequence=5, epoch=1, attempt_id="a-new")
    assert is_superseded_update(cand, curr) is True


def test_queued_then_started_same_attempt_is_allowed():
    """QUEUED（无 attempt）→ STARTED（有 attempt）同 sequence 同 epoch 合法推进。"""
    cand = _snap(sequence=5, epoch=1, attempt_id="a1")
    curr = _snap(sequence=5, epoch=1, attempt_id=None)
    assert is_superseded_update(cand, curr) is False


def test_regression_without_attempt_after_attempt_is_rejected():
    """有 attempt 之后回到无 attempt → 回退，拒绝。"""
    cand = _snap(sequence=5, epoch=1, attempt_id=None)
    curr = _snap(sequence=5, epoch=1, attempt_id="a1")
    assert is_superseded_update(cand, curr) is True


def test_legacy_different_task_id_without_sequence_is_rejected_safe_side():
    """legacy 无 sequence：task_id 不同 → 安全侧拒绝（防旧回调覆盖）。"""
    cand = factory_legacy("t2", "a1", 1)
    curr = factory_legacy("t1", "a1", 1)
    assert is_superseded_update(cand, curr) is True


def test_legacy_same_task_same_epoch_same_attempt_is_accepted():
    """legacy 无 sequence 且同 task/epoch/attempt：接受。"""
    cand = factory_legacy("t1", "a1", 1)
    curr = factory_legacy("t1", "a1", 1)
    assert is_superseded_update(cand, curr) is False


def factory_legacy(task_id, attempt_id, epoch):
    """无 sequence 的 T14 兼容构造 helper。"""
    return _snap(task_id=task_id, sequence=None, attempt_id=attempt_id, epoch=epoch)


def test_legacy_candidate_without_attempt_after_attempt_is_rejected():
    """legacy 候选无 attempt 而当前有 attempt → 视作回退拒绝。"""
    cand = factory_legacy("t1", None, 1)
    curr = factory_legacy("t1", "a1", 1)
    assert is_superseded_update(cand, curr) is True


def test_higher_epoch_resolves_over_same_epoch():
    """高 epoch 覆盖同 sequence 任意 attempt（新代授权）。"""
    cand = _snap(sequence=5, epoch=3, attempt_id="a-new")
    curr = _snap(sequence=5, epoch=2, attempt_id="a-old")
    assert is_superseded_update(cand, curr) is False


# --------------------------------------------------- 阶段条与连接卡


def test_completed_task_does_not_imply_delivery_stage_complete():
    """Task COMPLETED 且 RESULT_DELIVERY 未完成 → 前三段 done、交付段 current。"""
    s = _snap(
        state="COMPLETED",
        progress_stage="RESULT_DELIVERY",
        progress_completed=False,
    )
    p = present_status(s)
    assert p.headline == "已完成"
    assert p.is_terminal is True

    pr = progress_presentation(s)
    assert [x.state for x in pr.stages] == ["done", "done", "done", "current"]
    assert pr.completed is False


def test_delivery_completed_only_when_snapshot_says_completed():
    """只有 progress_stage_completed=True 才全亮；Presenter 不读 task.state。"""
    s = _snap(
        state="COMPLETED",
        progress_stage="RESULT_DELIVERY",
        progress_completed=True,
    )
    pr = progress_presentation(s)
    assert [x.state for x in pr.stages] == ["done", "done", "done", "done"]
    assert pr.completed is True


def test_progress_stage_absent_is_all_todo_without_percentage():
    """无进度阶段时全部 todo，绝不出现假百分比。"""
    pr = progress_presentation(_snap(state="ACTIVE"))
    assert [x.state for x in pr.stages] == ["todo", "todo", "todo", "todo"]
    assert all("%" not in x.label for x in pr.stages)


def test_stale_connection_does_not_keep_green_status():
    """is_stale=True 时即使旧连接 healthy 也不能保持绿灯。"""
    conn = ConnectionSnapshot(
        source="S1",
        transport_ok=True,
        payload_valid=True,
        last_observed_at=T,
        is_stale=True,
    )
    s = _snap(conn_struct=conn, conn_healthy=True)
    c = connection_presentation(s)
    assert c.tone == "recovering"
    assert "可能已过期" in c.headline
    assert c.headline != "模型/接口连接正常"
    assert "最后检测 2026-09-10 12:34:56" in c.detail


def test_connection_falls_back_to_legacy_fields():
    """无结构化 connection 时回退 connection_healthy/connection_source。"""
    ok = _snap(conn_struct=None, conn_healthy=True, conn_source="S2")
    c_ok = connection_presentation(ok)
    assert c_ok.tone == "success"
    assert c_ok.headline == "模型/接口连接正常"
    assert "S2" in c_ok.detail

    down = _snap(conn_struct=None, conn_healthy=False, conn_source="S3")
    c_down = connection_presentation(down)
    assert c_down.tone == "recovering"
    assert c_down.headline == "接口未连接"


# ------------------------------------------------ T15R：连接语义收紧


def test_connection_transport_down_is_recovering_not_danger():
    """普通网络问题：接口暂不可达，琥珀 recovering，不做最终 danger。"""
    conn = ConnectionSnapshot(source="S1", transport_ok=False, is_stale=False,
                              last_observed_at=T)
    c = connection_presentation(_snap(conn_struct=conn))
    assert c.headline == "接口暂不可达"
    assert c.tone == "recovering"


def test_connection_payload_invalid_is_explicit_danger():
    """payload_valid=False → 明确异常（danger），不是暂不可达。"""
    conn = ConnectionSnapshot(source="S1", transport_ok=True, payload_valid=False)
    c = connection_presentation(_snap(conn_struct=conn))
    assert "校验未通过" in c.headline
    assert c.tone == "danger"


def test_connection_transport_ok_but_payload_unknown_is_neutral():
    """transport 可达 ≠ payload 已验证：不能直接声称正常。"""
    conn = ConnectionSnapshot(source="S1", transport_ok=True, payload_valid=None,
                              is_stale=False)
    c = connection_presentation(_snap(conn_struct=conn, conn_healthy=True))
    assert c.headline == "接口可达，业务状态待核验"
    assert c.tone == "neutral"
    assert c.headline != "模型/接口连接正常"


def test_connection_success_requires_payload_valid():
    """只有 transport_ok=True + payload_valid=True 才可声称正常。"""
    conn = ConnectionSnapshot(source="S1", transport_ok=True, payload_valid=True,
                              is_stale=False)
    c = connection_presentation(_snap(conn_struct=conn))
    assert c.headline == "模型/接口连接正常"
    assert c.tone == "success"


def test_connection_short_label_keeps_stale_not_green():
    """Header 短文案由 authoritative headline 映射；stale 绝不映射成‘连接 正常’。"""
    from ui.status_presenter import connection_short_label

    assert connection_short_label("模型/接口连接正常") == "连接 正常"
    assert connection_short_label("接口未连接") == "连接 异常/未知"
    assert connection_short_label("连接状态可能已过期") == "连接 可能已过期"
    assert connection_short_label("接口暂不可达") == "连接 暂不可达"


# ------------------------------------------------ T15R：运行时长 / 队列阻塞


def test_format_runtime_uses_upstream_seconds():
    """运行时长只依赖上游 runtime_seconds；UI/展示层不自行计时。"""
    from ui.status_presenter import format_runtime

    assert format_runtime(3661) == "01:01:01"
    assert format_runtime(0) == "00:00:00"
    assert format_runtime(-5) == "00:00:00"
    assert format_runtime(None) == "未知"


def test_normal_watching_chain_all_todo():
    """普通 WATCHING（无中断/无进展）：7 节点全 todo，且不把冷却标成完成。"""
    s = _snap(phase="WATCHING")
    steps = recovery_steps(s)
    assert len(steps) == 7
    assert all(step.state == "todo" for step in steps)
    assert steps[6].state == "todo"   # 冷却


def test_recovered_watching_cooldown_still_todo():
    """已恢复执行：0..5 done；『冷却』(6) 保持 todo。"""
    s = _snap(phase="WATCHING", interruption_id="i-7", last_real_progress_at=T)
    steps = recovery_steps(s)
    assert [st.state for st in steps[:6]] == ["done"] * 6
    assert steps[6].state == "todo"
    assert steps[6].label == "冷却"


def test_queue_brief_exposes_blocked_reason():
    """队列摘要不得丢掉具体阻塞原因；未知原因做安全 fallback。"""
    from app.snapshots import QueueItemSnapshot

    s = _snap().replace_snapshot(
        waiting_task_count=27,
        queue_brief=(
            QueueItemSnapshot(task_id="t0", sequence=12, title="q0", state="BLOCKED",
                              blocked_reason="SESSION_MISSING"),
            QueueItemSnapshot(task_id="t1", sequence=13, title="q1", state="BLOCKED",
                              blocked_reason="UNKNOWN_CODE_X"),
            QueueItemSnapshot(task_id="t2", sequence=14, title="q2", state="BLOCKED"),
        ),
    )
    rows = queue_brief_rows(s, max_rows=3)
    assert rows[0] == "seq 12 · q0 · BLOCKED · 原会话记录缺失，需要人工核对"
    assert "UNKNOWN_CODE_X" in rows[1]      # 未知 reason 回退原始 code
    assert "原因待核对" in rows[2]           # 无 reason 且 BLOCKED → 待核对


def test_next_action_comes_only_from_snapshot():
    """next_action 只来自 next_check_at；缺失不造假时间，终态不显示。"""
    no_next = _snap(phase="WAIT_NETWORK", next_check_at=None)
    p = present_status(no_next)
    assert p.headline == "等待网络"
    assert p.next_action_text is None

    with_next = _snap(phase="WAIT_NETWORK", next_check_at=datetime(2026, 9, 10, 8, 0, 0))
    assert present_status(with_next).next_action_text == "下次检查 08:00:00"

    terminal_with_next = _snap(
        state="COMPLETED",
        phase="COOLDOWN",
        next_check_at=T,
    )
    assert present_status(terminal_with_next).next_action_text is None


def test_no_fake_percentage_across_presentations():
    """全部展示文案不得出现任何百分比。"""
    snapshots = [
        _snap(phase="WAIT_NETWORK", resume_total=7, next_check_at=T),
        _snap(phase="COOLDOWN", resume_total=7, consecutive_no_progress=3, next_check_at=T),
        _snap(state="COMPLETED", progress_stage="RESULT_DELIVERY", progress_completed=False),
        _snap(phase="BLOCKED", blocked_reason="CONFIGURATION"),
    ]
    text_parts: list[str] = []
    for s in snapshots:
        p = present_status(s)
        text_parts += [p.headline, p.detail]
        text_parts += [x.label for x in recovery_steps(s)]
        text_parts += [x.label for x in progress_presentation(s).stages]
        c = counters(s)
        text_parts += [c.total_text, c.consecutive_text]
        cc = connection_presentation(s)
        text_parts += [cc.headline, cc.detail]
    assert "%" not in " ".join(text_parts)


# ------------------------------------------------------------ 事件与队列


def test_events_are_truncated_to_max_six_and_ordered_desc():
    """右栏最近事件：倒序、最多截取 6 条（规格 4–6）。"""
    events = tuple(
        EventSnapshot(occurred_at=datetime(2026, 9, 10, 10, 0, i), event_code=f"E{i}", summary=f"s{i}")
        for i in range(8)
    )
    s = _snap()
    snap_with_events = s.replace_snapshot(events=events)
    rows = event_rows(snap_with_events, max_rows=6)
    assert [r.event_code for r in rows] == ["E7", "E6", "E5", "E4", "E3", "E2"]
    assert rows[0].time_text == "10:00:07"

    four = s.replace_snapshot(events=events[:4])
    assert len(event_rows(four)) == 4


def test_events_never_carry_body_or_secrets():
    """事件只含脱敏摘要：EventRowPresentation 无正文/prompt/token 字段。"""
    events = (EventSnapshot(occurred_at=None, event_code="E1", summary="摘要", tone=None),)
    s = _snap().replace_snapshot(events=events)
    row = event_rows(s)[0]
    assert row.summary == "摘要"
    assert row.tone == "neutral"
    assert not hasattr(row, "body")
    assert not hasattr(row, "prompt")
    assert not hasattr(row, "token")


def test_queue_brief_rows_respect_count_authority():
    """队列只列前 3 项做摘要；等待数量以 waiting_task_count 为权威。"""
    from app.snapshots import QueueItemSnapshot

    items = tuple(QueueItemSnapshot(task_id=f"t{i}", sequence=i, title=f"q{i}", state="QUEUED") for i in range(5))
    s = _snap().replace_snapshot(queue_brief=items, waiting_task_count=5)
    rows = queue_brief_rows(s, max_rows=3)
    assert len(rows) == 3
    assert rows[0].startswith("seq 0")
    assert s.waiting_task_count == 5  # 摘要截短不影响权威计数


# ------------------------------------------------------------ 静态隔离


def _ban_list(rel: str) -> list[str]:
    banned = {
        "sqlite3", "requests", "OpenChamber", "Reasonix", "TaskStore",
        "Database", "adapters", "controller", "commands", "core",
        "PySide6", "QTimer", "QObject", "QApplication",
    }
    src = Path(__file__).resolve().parents[1] / rel
    code = src.read_text(encoding="utf-8")
    names: set[str] = set()
    tokens: list[tuple[int, str]] = []
    for tok in _tokenize.tokenize(io.BytesIO(code.encode("utf-8")).readline):
        if tok.type in (_tokenize.COMMENT, _tokenize.STRING, _tokenize.ENCODING,
                        _tokenize.ENDMARKER, _tokenize.NL, _tokenize.NEWLINE,
                        _tokenize.INDENT, _tokenize.DEDENT):
            continue
        if tok.type == _tokenize.NAME:
            names.add(tok.string)
        tokens.append((tok.type, tok.string))
    hits = sorted(names & banned)
    # datetime.now( 调用检测：NAME 'now' 紧跟 OP '('
    for i in range(len(tokens) - 1):
        if tokens[i][1] == "now" and tokens[i + 1][1] == "(":
            hits.append("datetime.now 调用")
            break
    return hits


def test_presenter_and_snapshots_static_isolation():
    """快照层/展示层不得引用 DB/client/Qt，也不得调用 datetime.now() 判 TTL。"""
    violations: list[str] = []
    for rel in ("app/snapshots.py", "ui/status_presenter.py"):
        hits = _ban_list(rel)
        if hits:
            violations.append(f"{rel}: {hits}")
    assert not violations, f"存在依赖违规: {violations}"


def test_presenter_does_not_import_pyside6():
    """status_presenter 是纯展示层，绝不 import PySide6。

    在全新子进程中验证，避免被同进程其他文件（模块导入期）污染 sys.modules。
    """
    import os
    import subprocess

    root = str(Path(__file__).resolve().parents[1])
    probe = (
        "import sys; sys.path.insert(0, {root!r}); "
        "import ui.status_presenter; "
        "assert 'PySide6' not in sys.modules, 'presenter 引入了 PySide6'; "
        "print('PRESENTER_CLEAN')"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe.format(root=root)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr