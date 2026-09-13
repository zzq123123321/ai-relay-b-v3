"""T18 命令 UI 触点定向测试。

验收依据（T18-B2）：
- MainWindow：stop→DLG02→ui_command_requested outward seam；点击本身不发命令。
- TaskDetail：人工包装 DLG01 / 新会话重试 DLG03 在"高级操作"区；无详情禁用。
- TaskRecordsPage→MainWindow：同一个 request 原对象上浮。
- UI-B06：epoch7→epoch8→late7 回包被拒，Fake Command 自身不推进 epoch。
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.commands import UiCommandKind  # noqa: E402
from app.snapshots import fake_snapshot  # noqa: E402
from storage.task_queries import TaskDetail, TaskPageResult  # noqa: E402
from ui.dialogs import ManualWrapDialog, NewSessionRetryDialog, StopTaskDialog  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.task_detail import TaskDetailPanel  # noqa: E402
from ui.task_records import TaskRecordsPage  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    app.setStyleSheet("")


def _win(snapshot=None):
    win = MainWindow(snapshot or fake_snapshot(task_id="task-123"))
    win.resize(1280, 820)
    win.show()
    QApplication.processEvents()
    return win


def _stop_snap(epoch: int, sequence: int, task_id: str = "task-123"):
    return fake_snapshot(
        task_id=task_id,
        state="ACTIVE",
        stop_available=True,
        sequence=sequence,
        attempt_id="a1",
        authority_epoch=epoch,
    )


def _fake_detail(**over):
    kwargs = dict(
        task_key="CHATGPT:task-B2",
        peer_id="CHATGPT",
        task_id="task-B2",
        sequence=7,
        protocol_format="V1",
        state="ACTIVE",
        blocked_reason=None,
        received_at="2026-10-01T09:00:00+00:00",
        authority_epoch=7,
        active_attempt_id="a2",
        current_result_revision=0,
        raw_message="<raw>",
        body="很长的中文正文：任务内容一脉相承地延续……task-B2",
        canonical_hash="abc",
        project_key="proj-alpha",
        directory=None,
        requested_model="qwen3.8-27b",
        frozen_session_id="sess-b2",
        config_revision=5,
        attempts=(),
        corrupt_reasons=(),
    )
    kwargs.update(over)
    return TaskDetail(**kwargs)


class _Provider:
    """PAGE02 只读 Provider 最小 fake（list/detail/versions/result）。"""

    def __init__(self, detail=None):
        self._detail = detail

    def list_tasks(self, **kw):
        return TaskPageResult(items=(), next_cursor=None, has_more=False, total_count=0)

    def get_task_detail(self, task_key):
        return self._detail

    def list_task_result_versions(self, task_key):
        return ()

    def get_result(self, result_id):
        return None


def test_01_main_window_has_ui_command_requested_signal(qapp):
    win = _win()
    assert hasattr(win, "ui_command_requested")
    got = []
    win.ui_command_requested.connect(got.append)
    win.close()


def test_02_stop_click_opens_dlg02(qapp):
    win = _win(_stop_snap(epoch=7, sequence=1))
    win.stop_button.click()
    QApplication.processEvents()
    assert isinstance(win._stop_dialog, StopTaskDialog)  # noqa: SLF001
    assert win._stop_dialog.isVisible()  # noqa: SLF001
    win._stop_dialog.reject()  # noqa: SLF001
    win.close()


def test_03_stop_click_alone_emits_zero_command(qapp):
    win = _win(_stop_snap(epoch=7, sequence=1))
    cmds = []
    win.ui_command_requested.connect(cmds.append)
    win.stop_button.click()
    QApplication.processEvents()
    assert cmds == []
    win._stop_dialog.reject()  # noqa: SLF001
    win.close()


def test_04_stop_cancel_emits_zero_command(qapp):
    win = _win(_stop_snap(epoch=7, sequence=1))
    cmds = []
    win.ui_command_requested.connect(cmds.append)
    win.stop_button.click()
    QApplication.processEvents()
    dlg = win._stop_dialog  # noqa: SLF001
    dlg._cancel_button.click()  # noqa: SLF001 真实取消路径
    QApplication.processEvents()
    assert cmds == []
    assert win._stop_dialog is None  # noqa: SLF001 finished 后清引用
    win.close()


def test_05_stop_confirm_outward_exactly_once(qapp):
    win = _win(_stop_snap(epoch=7, sequence=1))
    cmds = []
    win.ui_command_requested.connect(cmds.append)
    win.stop_button.click()
    QApplication.processEvents()
    dlg = win._stop_dialog  # noqa: SLF001
    dlg._confirm_button.click()  # noqa: SLF001
    QApplication.processEvents()
    assert len(cmds) == 1
    win.close()


def test_06_stop_request_kind_target(qapp):
    win = _win(_stop_snap(epoch=7, sequence=1))
    cmds = []
    win.ui_command_requested.connect(cmds.append)
    win.stop_button.click()
    QApplication.processEvents()
    dlg = win._stop_dialog  # noqa: SLF001
    dlg._confirm_button.click()  # noqa: SLF001
    QApplication.processEvents()
    req = cmds[0]
    assert req.kind == UiCommandKind.STOP_TASK
    assert req.target_id == "task-123"
    win.close()


def test_07_stop_context_frozen_epoch_attempt_sequence(qapp):
    win = _win(_stop_snap(epoch=7, sequence=1))
    win.stop_button.click()
    QApplication.processEvents()
    ctx = dict(win._stop_dialog.request.context)  # noqa: SLF001
    assert ctx["authority_epoch"] == "7"
    assert ctx["attempt_id"] == "a1"
    assert ctx["sequence"] == "1"
    assert ctx["task_id"] == "task-123"
    assert ctx["page_id"] == "PAGE01"
    win._stop_dialog.reject()  # noqa: SLF001
    win.close()


def test_08_repeated_stop_trigger_reuses_single_dialog(qapp):
    win = _win(_stop_snap(epoch=7, sequence=1))
    win.stop_button.click()
    QApplication.processEvents()
    dlg = win._stop_dialog  # noqa: SLF001
    win.stop_button.click()
    QApplication.processEvents()
    assert win._stop_dialog is dlg  # noqa: SLF001 不创建第二个
    # 只存在一个 StopTaskDialog
    dialogs = [w for w in win.findChildren(StopTaskDialog)]
    assert len(dialogs) == 1
    win._stop_dialog.reject()  # noqa: SLF001
    win.close()


def test_09_confirmed_stop_does_not_mutate_snapshot_or_state(qapp):
    win = _win(_stop_snap(epoch=7, sequence=1))
    snap_id = id(win.snapshot)
    win.stop_button.click()
    QApplication.processEvents()
    dlg = win._stop_dialog  # noqa: SLF001
    dlg._confirm_button.click()  # noqa: SLF001
    QApplication.processEvents()
    assert id(win.snapshot) == snap_id
    assert win.snapshot.active_task.task_id == "task-123"
    assert win.snapshot.active_task.state == "ACTIVE"
    win.close()


def test_10_legacy_stop_requested_only_after_confirm_once(qapp):
    win = _win(_stop_snap(epoch=7, sequence=1))
    stops = []
    win.stop_requested.connect(stops.append)
    win.stop_button.click()
    QApplication.processEvents()
    assert stops == []
    dlg = win._stop_dialog  # noqa: SLF001
    dlg._confirm_button.click()  # noqa: SLF001
    QApplication.processEvents()
    assert stops == ["PAGE01"]
    dlg._confirm_button.click()  # noqa: SLF001 已提交锁定，不得多发
    QApplication.processEvents()
    assert stops == ["PAGE01"]
    win.close()


def test_11_task_detail_actions_disabled_without_detail(qapp):
    panel = TaskDetailPanel()
    assert not panel.manual_wrap_button.isEnabled()
    assert not panel.new_session_button.isEnabled()
    panel.deleteLater()


def test_12_actions_enabled_after_render_detail(qapp):
    panel = TaskDetailPanel()
    panel.render_detail(_fake_detail())
    assert panel.manual_wrap_button.isEnabled()
    assert panel.new_session_button.isEnabled()
    panel.clear()
    assert not panel.manual_wrap_button.isEnabled()
    assert not panel.new_session_button.isEnabled()
    panel.deleteLater()


def test_13_manual_wrap_dlg01_confirm_outward_exactly_once(qapp):
    panel = TaskDetailPanel()
    panel.render_detail(_fake_detail())
    cmds = []
    panel.ui_command_requested.connect(cmds.append)
    panel.manual_wrap_button.click()
    QApplication.processEvents()
    dlg = panel._command_dialog  # noqa: SLF001
    assert isinstance(dlg, ManualWrapDialog)
    dlg._confirm_check.setChecked(True)  # noqa: SLF001
    dlg._confirm_button.click()  # noqa: SLF001
    QApplication.processEvents()
    assert len(cmds) == 1
    req = cmds[0]
    assert req.kind == UiCommandKind.MANUAL_WRAP
    assert req.target_id == "task-B2"
    ctx = dict(req.context)
    assert ctx["task_key"] == "CHATGPT:task-B2"
    assert ctx["authority_epoch"] == "7"
    assert ctx["active_attempt_id"] == "a2"
    assert ctx["frozen_session_id"] == "sess-b2"
    panel.deleteLater()


def test_14_new_session_in_advanced_area_and_enabled_after_render(qapp):
    panel = TaskDetailPanel()
    panel.render_detail(_fake_detail())
    # 入口位于"高级操作"Card
    assert panel.manual_wrap_button.parent() is panel.advanced_card
    assert panel.new_session_button.parent() is panel.advanced_card
    note = panel.advanced_card.findChild(QLabel, "advanced_op_warning")
    assert note is not None
    assert "高级操作会改变执行路径" in note.text()
    assert panel.new_session_button.isEnabled()
    panel.deleteLater()


def test_15_new_session_confirm_kind_no_real_session(qapp):
    panel = TaskDetailPanel()
    panel.render_detail(_fake_detail())
    cmds = []
    panel.ui_command_requested.connect(cmds.append)
    panel.new_session_button.click()
    QApplication.processEvents()
    dlg = panel._command_dialog  # noqa: SLF001
    assert isinstance(dlg, NewSessionRetryDialog)
    dlg._confirm_button.click()  # noqa: SLF001
    QApplication.processEvents()
    assert len(cmds) == 1
    req = cmds[0]
    assert req.kind == UiCommandKind.NEW_SESSION_RETRY
    # 只保留身份字段，不伪造已解析 session / 不背办真实会话
    assert dict(req.context).get("frozen_session_id") == "sess-b2"
    assert "resolved_session_id" not in dict(req.context)
    panel.deleteLater()


def test_16_same_request_object_survives_full_chain(qapp):
    detail = _fake_detail()
    prov = _Provider(detail)
    win = MainWindow(
        fake_snapshot(task_id="task-123"),
        task_history_provider=prov,
    )
    win.resize(1280, 820)
    win.show()
    QApplication.processEvents()
    # 正式 TaskRecordsPage 已由 _make_tasks_page 创建并接上转发链
    assert isinstance(win.tasks_page, TaskRecordsPage)
    out = []
    win.ui_command_requested.connect(out.append)
    panel = win.tasks_page.detail_panel
    panel.render_detail(detail)
    panel.manual_wrap_button.click()
    QApplication.processEvents()
    dlg = panel._command_dialog  # noqa: SLF001
    dlg._confirm_check.setChecked(True)  # noqa: SLF001
    dlg._confirm_button.click()  # noqa: SLF001
    QApplication.processEvents()
    assert len(out) == 1
    req = out[0]
    assert req is dlg.request  # 同一个 request 对象，未重建
    assert req.kind == UiCommandKind.MANUAL_WRAP
    win.close()


def test_17_stop_epoch7_to_8_blocks_late_epoch7(qapp):
    win = _win(_stop_snap(epoch=7, sequence=1))
    cmds = []
    win.ui_command_requested.connect(cmds.append)
    win.stop_button.click()
    QApplication.processEvents()
    dlg = win._stop_dialog  # noqa: SLF001
    assert dict(dlg.request.context)["authority_epoch"] == "7"
    # 模拟权威推进 epoch=8
    snap8 = _stop_snap(epoch=8, sequence=2)
    win.update_snapshot(snap8)
    QApplication.processEvents()
    assert win.snapshot.active_task.authority_epoch == 8
    # Confirm 仍发出冻结的 epoch=7 request
    dlg._confirm_button.click()  # noqa: SLF001
    QApplication.processEvents()
    assert len(cmds) == 1
    assert dict(cmds[0].context)["authority_epoch"] == "7"
    # late epoch=7 callback 被拒，保持 epoch=8
    late = _stop_snap(epoch=7, sequence=1)
    win.update_snapshot(late)
    QApplication.processEvents()
    assert win.snapshot.active_task.authority_epoch == 8
    win.close()


def test_18_manual_wrap_epoch7_to_8_blocks_late_epoch7(qapp):
    panel = TaskDetailPanel()
    panel.render_detail(_fake_detail(authority_epoch=7, active_attempt_id="a1"))
    panel.manual_wrap_button.click()
    QApplication.processEvents()
    dlg = panel._command_dialog  # noqa: SLF001
    assert dict(dlg.request.context)["authority_epoch"] == "7"
    # 权威状态推进 epoch=8（DIALOG request 冻结不变）
    win = _win(_stop_snap(epoch=8, sequence=2, task_id="task-B2"))
    assert win.snapshot.active_task.authority_epoch == 8
    dlg._confirm_check.setChecked(True)  # noqa: SLF001
    out = []
    panel.ui_command_requested.connect(out.append)
    dlg._confirm_button.click()  # noqa: SLF001
    QApplication.processEvents()
    assert dict(out[0].context)["authority_epoch"] == "7"
    # late epoch=7 callback 不得覆盖 epoch=8
    late = _stop_snap(epoch=7, sequence=1, task_id="task-B2")
    win.update_snapshot(late)
    QApplication.processEvents()
    assert win.snapshot.active_task.authority_epoch == 8
    win.close()
    panel.deleteLater()


def test_19_frozen_request_unchanged_after_detail_switch_and_epoch_change(qapp):
    panel = TaskDetailPanel()
    panel.render_detail(_fake_detail())
    panel.manual_wrap_button.click()
    QApplication.processEvents()
    dlg = panel._command_dialog  # noqa: SLF001
    frozen = (dlg.request.target_id, dlg.request.context)
    # 详情切换/epoch 变化不回写已打开 Dialog 的 request
    panel.render_detail(_fake_detail(task_id="task-other", authority_epoch=9, task_key="CHATGPT:task-other"))
    panel.clear()
    QApplication.processEvents()
    assert (dlg.request.target_id, dlg.request.context) == frozen
    # Confirm 发出的仍是打开瞬间冻结的对象
    out = []
    panel.ui_command_requested.connect(out.append)
    dlg._confirm_check.setChecked(True)  # noqa: SLF001
    dlg._confirm_button.click()  # noqa: SLF001
    QApplication.processEvents()
    assert len(out) == 1
    assert out[0] is dlg.request
    panel.deleteLater()