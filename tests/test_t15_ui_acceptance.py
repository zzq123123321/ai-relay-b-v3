"""T15-B2 工作台 UI 验收测试（真实 MainWindow + Dashboard 接缝）。

验收依据：UI-A02 / UI-B01 / UI-B02 / UI-B03（17.5 / 149 项），以及
T15-B2 的迟到回调 / 新 sequence / 低 epoch 拦截与 PAGE01 接缝要求。

关键点：
- UI-B01/B03 用同一 Dashboard 连续 render（不经重建 widget）；
- update_snapshot 入口会先经 MainWindow 快照级 gate 拦截过期快照。
"""

import io
import os
import sys
import tokenize as _tokenize
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.snapshots import (  # noqa: E402
    RecoverySnapshot,
    empty_snapshot,
    fake_snapshot,
)
from ui.dashboard import Dashboard  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402

T = datetime(2026, 9, 10, 12, 34, 56)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    app.setStyleSheet("")


def _make(snapshot=None, size=(1280, 820)):
    win = MainWindow(snapshot or fake_snapshot(), mode="light")
    win.resize(*size)
    win.show()
    QApplication.processEvents()
    return win


def _snap(task_id="task-1", sequence=1, attempt_id="a1", epoch=1, state="ACTIVE",
          phase="WATCHING", resume_total=0, consecutive_no_progress=0,
          next_check_at=None, last_real_progress_at=None, conn_healthy=True,
          paused=False, enabled=False, **kw):
    rec = RecoverySnapshot(
        phase=phase, resume_total=resume_total,
        consecutive_no_progress=consecutive_no_progress,
        next_check_at=next_check_at, last_real_progress_at=last_real_progress_at,
    )
    return fake_snapshot(
        task_id=task_id, sequence=sequence, attempt_id=attempt_id,
        authority_epoch=epoch, state=state, connection_healthy=conn_healthy,
        recovery=rec, auto_resume=None,
        progress_stage=kw.pop("progress_stage", None),
        progress_stage_completed=kw.pop("progress_stage_completed", None),
        waiting_task_count=kw.pop("waiting_task_count", None),
    )


def test_page01_seam_uses_dashboard(qapp):
    """PAGE01 接缝：MainWindow.workbench_page 是 Dashboard。"""
    win = _make()
    assert isinstance(win.workbench_page, Dashboard)
    win.close()


# ---------------------------------------------------------------- UI-A02


def test_ui_a02_wait_network_is_recovering_not_failed(qapp):
    """真实主窗：WAIT_NETWORK 显示为等待网络琥珀、任务不判失败。"""
    win = _make(_snap(phase="WAIT_NETWORK", next_check_at=T))
    wb = win.workbench_page
    assert "等待网络" in wb.recovery_headline_text()
    assert wb._recovery_headline.property("tone") == "recovering"  # noqa: SLF001
    assert "网络暂不可达" in wb._recovery_detail.text()  # noqa: SLF001
    assert "最终失败" not in wb._recovery_detail.text()
    # 任务状态值仍是 ACTIVE（不得变成 FAILED），整体琥珀表达等待网络
    assert wb._state_badge.text().endswith("ACTIVE")  # noqa: SLF001
    assert "FAILED" not in wb._state_badge.text()  # noqa: SLF001
    assert win.snapshot.active_task.state == "ACTIVE"
    win.close()


def test_ui_a02_also_covers_verifying_awaiting_cooldown(qapp):
    """同一主窗连续覆盖 VERIFYING / AWAITING_PROGRESS / COOLDOWN。"""
    win = _make(_snap(phase="VERIFYING"))
    wb = win.workbench_page
    assert wb.recovery_headline_text() == "⚠ 恢复核对"

    win.update_snapshot(_snap(phase="AWAITING_PROGRESS", next_check_at=T))
    assert wb.recovery_headline_text() == "⚠ 续接待进展"
    assert "等待真实新进展" in wb._recovery_detail.text()  # noqa: SLF001

    win.update_snapshot(_snap(phase="COOLDOWN", resume_total=7, consecutive_no_progress=3, next_check_at=T))
    assert wb.recovery_headline_text() == "⚠ 冷却中"
    assert wb._recovery_headline.property("tone") == "recovering"  # noqa: SLF001
    win.close()


# ------------------------------------------------ T15R：Header/工作台 统一连接


def test_t15r_stale_connection_never_green_in_header_nor_dashboard(qapp):
    """legacy connection_healthy=True + 结构化 is_stale=True → Header 与工作台都不绿。"""
    from app.snapshots import ConnectionSnapshot

    snap = fake_snapshot(connection_healthy=True).replace_snapshot(
        connection=ConnectionSnapshot(
            source="S1", transport_ok=True, payload_valid=True,
            last_observed_at=T, is_stale=True,
        ),
    )
    win = _make(snap)
    wb = win.workbench_page
    assert win._conn_badge.property("tone") == "recovering"   # noqa: SLF001
    assert "可能已过期" in win._conn_badge.text()             # noqa: SLF001
    assert win._conn_badge.text() != "连接 正常"              # noqa: SLF001
    assert wb._conn_badge.property("tone") == "recovering"    # noqa: SLF001
    assert "可能已过期" in wb._conn_badge.text()
    win.close()


def test_t15r_header_follows_transport_down_and_payload_pending(qapp):
    """Header 短文案跟随 authoritative headline；暂不可达/待核验都不映射成正常。"""
    from app.snapshots import ConnectionSnapshot

    down = fake_snapshot().replace_snapshot(
        connection=ConnectionSnapshot(source="S1", transport_ok=False, is_stale=False),
    )
    w1 = _make(down)
    assert w1._conn_badge.property("tone") == "recovering"  # noqa: SLF001
    assert "暂不可达" in w1._conn_badge.text()  # noqa: SLF001
    w1.close()

    pending = fake_snapshot().replace_snapshot(
        connection=ConnectionSnapshot(source="S1", transport_ok=True, payload_valid=None),
    )
    w2 = _make(pending)
    assert w2._conn_badge.property("tone") != "success"  # noqa: SLF001
    assert w2._conn_badge.text() != "连接 正常"  # noqa: SLF001
    w2.close()


# ---------------------------------------------------------------- UI-B01


def test_ui_b01_same_dashboard_never_claims_success_before_progress(qapp):
    """同一个 Dashboard 连演 WAIT→VERIFY→SUSPECTED/SENDING→AWAIT→WATCHING+进展。"""
    win = _make(_snap(phase="WAIT_NETWORK"))
    wb = win.workbench_page

    sequence = [
        ("VERIFYING", "恢复核对"),
        ("SUSPECTED", "计划复查"),
        ("SENDING", "续接发送"),
        ("AWAITING_PROGRESS", "续接待进展"),
    ]
    for phase, headline in sequence:
        win.update_snapshot(_snap(phase=phase))
        assert wb.recovery_headline_text() == f"⚠ {headline}", phase
        assert not wb.recovery_headline_text().startswith("✓"), phase  # 未到真实进展前不得声称已恢复
        assert "已恢复执行" not in wb._recovery_detail.text(), phase  # noqa: SLF001

    before_progress = _snap(phase="AWAITING_PROGRESS", next_check_at=T)
    win.update_snapshot(before_progress)
    assert "等待真实新进展" in wb._recovery_detail.text()  # noqa: SLF001
    assert not wb.recovery_headline_text().startswith("✓")

    # 只有真实进展快照才显示“已恢复执行”
    real = _snap(phase="WATCHING", interruption=True, last_real_progress_at=T)
    win.update_snapshot(real.replace_snapshot(
        recovery=RecoverySnapshot(
            phase="WATCHING", interruption_id="i-7", last_real_progress_at=T,
        ),
    ))
    assert wb.recovery_headline_text() == "✓ 已恢复执行"
    nodes = wb.recovery_node_texts()
    assert nodes[5].startswith("✓")   # 已恢复执行 done
    assert nodes[6].startswith("○")   # 冷却 不得标成已走过
    win.close()


# ---------------------------------------------------------------- UI-B02


def test_ui_b02_seven_total_zero_consecutive_still_resuming(qapp):
    """累计 7 次 + 连续 0 次 + 再次断网 → 仍在等待/续接，不耗尽。"""
    win = _make(_snap(phase="WAIT_NETWORK", resume_total=7, consecutive_no_progress=0))
    wb = win.workbench_page
    total, consecutive = wb.counter_texts()
    assert total == "累计续接 7 次"
    assert consecutive == "连续未恢复 0 次"
    assert wb.recovery_headline_text() == "⚠ 等待网络"
    for forbidden in ("耗尽", "达到最大", "停止自动续接", "连续失败 7"):
        assert forbidden not in total + consecutive
    win.close()


# ---------------------------------------------------------------- UI-B03


def test_ui_b03_cooldown_before_then_real_progress_after(qapp):
    """同一 Dashboard：冷却(7/3) → 真实进展(8/0)，只清零连续、累计递增。"""
    win = _make(_snap(
        phase="COOLDOWN", resume_total=7, consecutive_no_progress=3, next_check_at=T,
    ))
    wb = win.workbench_page
    total, consecutive = wb.counter_texts()
    assert total == "累计续接 7 次"
    assert consecutive == "连续未恢复 3 次"
    assert wb.recovery_headline_text() == "⚠ 冷却中"
    assert "12:34:56" in wb.rail_next_action_text()

    after = _snap(phase="WATCHING", resume_total=8, consecutive_no_progress=0,
                  last_real_progress_at=T)
    win.update_snapshot(after.replace_snapshot(
        recovery=RecoverySnapshot(
            phase="WATCHING", interruption_id="i-8", resume_total=8,
            consecutive_no_progress=0, last_real_progress_at=T,
        ),
    ))
    total2, consecutive2 = wb.counter_texts()
    assert total2 == "累计续接 8 次"
    assert consecutive2 == "连续未恢复 0 次"
    assert wb.recovery_headline_text() == "✓ 已恢复执行"
    assert "连续未恢复 3 次" not in consecutive2
    win.close()


# ------------------------------------------------ 迟到回调 / 身份守卫接缝


def test_late_task_callback_is_rejected_and_current_kept(qapp):
    """current task-B seq20，迟到的 task-A seq19 不得覆盖；stop 目标仍 B。"""
    current = _snap(task_id="task-B", sequence=20, attempt_id="B", epoch=2)
    win = _make(current)
    assert win.snapshot.active_task.task_id == "task-B"
    assert "task-B" in win.stop_button.toolTip()

    late = _snap(task_id="task-A", sequence=19, attempt_id="A", epoch=1)
    win.update_snapshot(late)
    QApplication.processEvents()
    assert win.snapshot.active_task.task_id == "task-B"
    assert "task-B" in win.workbench_page._task_id.text()  # noqa: SLF001
    assert "task-B" in win.stop_button.toolTip()
    assert win.workbench_page._state_badge.text() == "✓ ACTIVE"  # noqa: SLF001 仍是 B


def test_newer_sequence_can_replace_task(qapp):
    """task-C seq21 是更新任务，允许切换。"""
    win = _make(_snap(task_id="task-B", sequence=20, attempt_id="B", epoch=2))
    newer = _snap(task_id="task-C", sequence=21, attempt_id="C", epoch=2)
    win.update_snapshot(newer)
    QApplication.processEvents()
    assert win.snapshot.active_task.task_id == "task-C"
    assert "task-C" in win.workbench_page._task_id.text()  # noqa: SLF001


def test_same_sequence_lower_epoch_cannot_overwrite(qapp):
    """同 sequence 低 epoch 不得覆盖（旧代回调）。"""
    win = _make(_snap(task_id="task-B", sequence=20, attempt_id="B", epoch=2))
    older_epoch = _snap(task_id="task-B", sequence=20, attempt_id="B", epoch=1)
    win.update_snapshot(older_epoch)
    QApplication.processEvents()
    assert win.snapshot.active_task.authority_epoch == 2
    assert win.snapshot.active_task.attempt_id == "B"


def test_empty_to_new_task_is_allowed(qapp):
    """无任务 → 新任务（无 sequence 也有）必须允许。"""
    win = _make(empty_snapshot())
    assert win.snapshot.active_task is None
    first = _snap(task_id="task-A", sequence=1)
    win.update_snapshot(first)
    assert win.snapshot.active_task.task_id == "task-A"


def test_completed_then_newer_task_boundary(qapp):
    """Task-A 终态 COMPLETED 后，Task-B seq 更大 可正常切换。"""
    terminal = _snap(task_id="task-A", sequence=10, state="COMPLETED",
                     progress_stage="RESULT_DELIVERY", progress_stage_completed=False)
    win = _make(terminal)
    assert win.workbench_page.stage_texts()[3].startswith("●")  # 交付段未完成
    newer = _snap(task_id="task-B", sequence=11)
    win.update_snapshot(newer)
    assert win.snapshot.active_task.task_id == "task-B"


# ------------------------------------------------------ 静态隔离（本轮新增 diff）

_BANNED = {
    "sqlite3", "requests", "OpenChamber", "Reasonix", "TaskStore", "Database",
    "adapters", "controller", "commands", "core", "Dispatch", "QTimer",
}


def _banned_in(rel: str) -> list[str]:
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
    hits = sorted(names & _BANNED)
    for i in range(len(tokens) - 1):
        if tokens[i][1] == "send" and tokens[i + 1][1] == "(":
            hits.append("send(")
        if tokens[i][1] == "continue" and tokens[i + 1][1] == "(":
            hits.append("continue(")
    return hits


def test_t15_ui_no_business_dependency_introduced(qapp):
    """Dashboard/展示层不含 DB/client/Qt 业务依赖；main_window 未引入新违规。"""
    files = [
        "ui/dashboard.py",
        "ui/status_presenter.py",
        "app/snapshots.py",
        "ui/main_window.py",
    ]
    violations: list[str] = []
    for rel in files:
        hits = _banned_in(rel)
        if hits:
            violations.append(f"{rel}: {hits}")
    assert not violations, f"存在依赖违规: {violations}"


def test_dashboard_has_no_business_buttons_and_no_timer(qapp):
    """Dashboard 无业务按钮、无 timer（BLOCKED 只给文字建议）。"""
    win = _make()
    from PySide6.QtWidgets import QPushButton

    assert win.workbench_page.findChildren(QPushButton) == []
    assert not hasattr(win.workbench_page, "timer")
    win.close()