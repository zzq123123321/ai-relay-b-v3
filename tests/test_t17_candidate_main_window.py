"""T17-B3-R4：Candidate 异步刷新 MainWindow 接缝测试。

只建立 MainWindow 异步边界（request → outward signal；result → page），
stale 判定权威在 SettingsPage.apply_candidate_result，本套测试不复制第二套状态机。
刷新过程必须无任何副作用：不保存、不清 dirty、不改 snapshot/active task、不建 session。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.commands import (  # noqa: E402
    CandidateKind,
    CandidateRegion,
    CandidateRequest,
    CandidateResult,
)
from app.snapshots import fake_snapshot  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402


def _region(kind: CandidateKind, *, directory="/proj", endpoint="http://localhost:8080", project_key=None) -> CandidateRegion:
    return CandidateRegion(kind=kind, endpoint=endpoint, directory=directory, project_key=project_key)


class _RecordOnlySaveCtrl:
    """只记录 save 调用；current 为 None。用于证明刷新不触发保存。"""

    def __init__(self) -> None:
        self.save_calls = 0
        self.current = None

    def save(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN401
        self.save_calls += 1
        return None


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    app.setStyleSheet("")


def _make_win(snapshot=None, save_ctrl=None) -> MainWindow:
    win = MainWindow(
        fake_snapshot(config_revision="7") if snapshot is None else snapshot,
        settings_save_controller=save_ctrl,
    )
    win.show()
    win.activateWindow()
    QApplication.processEvents()
    return win


def _outbound(win) -> list:
    seen = []
    win.settings_candidate_refresh_requested.connect(seen.append)
    return seen


def _set_text(page, path: str, text: str) -> None:
    page._widgets[path].setText(text)
    QApplication.processEvents()


def _refresh(page, kind, request_id, *, url="http://localhost:8080", directory="/proj", project_key=None) -> CandidateRequest:
    _set_text(page, "openchamber.url", url)
    _set_text(page, "openchamber.directory", directory)
    page._request_id_factory = lambda: request_id
    captured = []
    page.candidate_refresh_requested.connect(captured.append)
    page._on_refresh(kind)
    QApplication.processEvents()
    return captured[-1]


def _combo_items(page, kind) -> list[str]:
    combo = page._combo_for_kind(kind)
    return [combo.itemText(i) for i in range(combo.count())]


# ================================================================ A/B: 请求转发


class TestRequestForwarding:
    def test_outward_signal_forwarded_same_object(self, qapp):
        win = _make_win()
        outbound = _outbound(win)
        req = _refresh(win.settings_page, CandidateKind.SESSION, "r1")
        assert len(outbound) == 1
        forwarded = outbound[0]
        assert forwarded is req
        assert forwarded.request_id == "r1"
        assert forwarded.region is req.region
        assert forwarded.region.kind is CandidateKind.SESSION

    def test_others_kinds_forwarded_identically(self, qapp):
        win = _make_win()
        outbound = _outbound(win)
        for kind, rid in (
            (CandidateKind.AGENT, "ra"),
            (CandidateKind.MODEL, "rm"),
            (CandidateKind.META, "rx"),
        ):
            req = _refresh(win.settings_page, kind, rid)
            assert outbound[-1] is req
            assert outbound[-1].request_id == rid
            assert outbound[-1].region is req.region


# ================================================================ B/C/D/E: result 回入口


class TestResultIngress:
    def test_current_result_applies(self, qapp):
        win = _make_win()
        req = _refresh(win.settings_page, CandidateKind.AGENT, "r1")
        result = CandidateResult(
            request_id="r1",
            region=req.region,
            values=("agent-a", "agent-b"),
            source="Fake",
        )
        assert win.apply_settings_candidate_result(result) is True
        assert _combo_items(win.settings_page, CandidateKind.AGENT) == ["agent-a", "agent-b"]

    def test_stale_result_rejected_not_overwritten(self, qapp):
        win = _make_win()
        page = win.settings_page
        req = _refresh(page, CandidateKind.MODEL, "r1")
        assert win.apply_settings_candidate_result(
            CandidateResult(request_id="r1", region=req.region, values=("m1",), source="Fake")
        ) is True
        assert win.apply_settings_candidate_result(
            CandidateResult(request_id="rNOPE", region=req.region, values=("bad",), source="Fake")
        ) is False
        assert _combo_items(page, CandidateKind.MODEL) == ["m1"]

    def test_same_kind_out_of_order_rejects_old(self, qapp):
        win = _make_win()
        page = win.settings_page
        req1 = _refresh(page, CandidateKind.SESSION, "r1", directory="/proj")
        req2 = _refresh(page, CandidateKind.SESSION, "r2", directory="/proj")
        old = CandidateResult(request_id="r1", region=req1.region, values=("old-session",), source="Fake")
        new = CandidateResult(request_id="r2", region=req2.region, values=("new-session",), source="Fake")
        assert win.apply_settings_candidate_result(old) is False
        assert win.apply_settings_candidate_result(new) is True
        assert _combo_items(page, CandidateKind.SESSION) == ["new-session"]

    def test_directory_change_old_result_rejected_new_kept(self, qapp):
        win = _make_win()
        page = win.settings_page
        req_a = _refresh(page, CandidateKind.MODEL, "rA", directory="/projA")
        _refresh(page, CandidateKind.MODEL, "rB", directory="/projB")
        result_a = CandidateResult(request_id="rA", region=req_a.region, values=("model-a",), source="Fake")
        result_b = CandidateResult(
            request_id="rB",
            region=_region(CandidateKind.MODEL, directory="/projB"),
            values=("model-b",),
            source="Fake",
        )
        # pending 仍是 rB：rA（区域/request 均不同）必须被拒
        assert win.apply_settings_candidate_result(result_a) is False
        assert win.apply_settings_candidate_result(result_b) is True
        assert _combo_items(page, CandidateKind.MODEL) == ["model-b"]
        # 迟到旧结果再送 → 仍 False，B 结果保持
        assert win.apply_settings_candidate_result(result_a) is False
        assert _combo_items(page, CandidateKind.MODEL) == ["model-b"]

    def test_session_and_meta_independent_cross_order(self, qapp):
        win = _make_win()
        page = win.settings_page
        s_req = _refresh(page, CandidateKind.SESSION, "rs")
        m_req = _refresh(page, CandidateKind.META, "rm")
        meta_result = CandidateResult(request_id="rm", region=m_req.region, source="MetaFake", metadata=(("状态", "正常"),))
        session_result = CandidateResult(request_id="rs", region=s_req.region, values=("sess-1",), source="Fake")
        assert win.apply_settings_candidate_result(meta_result) is True
        assert win.apply_settings_candidate_result(session_result) is True
        assert _combo_items(page, CandidateKind.SESSION) == ["sess-1"]
        assert "MetaFake" in page._candidate_source_labels[CandidateKind.META].text()

    def test_agent_and_model_independent_cross_order(self, qapp):
        win = _make_win()
        page = win.settings_page
        a_req = _refresh(page, CandidateKind.AGENT, "ra")
        m_req = _refresh(page, CandidateKind.MODEL, "rm")
        assert win.apply_settings_candidate_result(
            CandidateResult(request_id="rm", region=m_req.region, values=("model-x",), source="M")
        ) is True
        assert win.apply_settings_candidate_result(
            CandidateResult(request_id="ra", region=a_req.region, values=("agent-x",), source="A")
        ) is True
        assert _combo_items(page, CandidateKind.AGENT) == ["agent-x"]
        assert _combo_items(page, CandidateKind.MODEL) == ["model-x"]


# ================================================================ F: 无副作用


class TestNoSideEffects:
    def test_snapshot_and_active_task_unchanged(self, qapp):
        snap = fake_snapshot(config_revision="7")
        win = _make_win(snapshot=snap)
        page = win.settings_page
        before = win.snapshot
        active_before = win.snapshot.active_task
        req = _refresh(page, CandidateKind.SESSION, "r1", directory="/proj")
        assert win.apply_settings_candidate_result(
            CandidateResult(request_id="r1", region=req.region, values=("s",), source="Fake")
        ) is True
        req = _refresh(page, CandidateKind.META, "m1", directory="/proj")
        assert win.apply_settings_candidate_result(
            CandidateResult(request_id="m1", region=req.region, source="MetaFake")
        ) is True
        assert win.snapshot is before
        assert win.snapshot.active_task is active_before
        assert win.snapshot.active_task.config_revision == "7"
        assert win.snapshot.active_task.task_id == "task-123"

    def test_refresh_does_not_save_or_clear_dirty(self, qapp):
        recording = _RecordOnlySaveCtrl()
        win = _make_win(save_ctrl=recording)
        page = win.settings_page
        _set_text(page, "openchamber.url", "http://10.0.0.5:8080")
        assert page.dirty is True
        req = _refresh(page, CandidateKind.AGENT, "r1", url="http://10.0.0.5:8080")
        assert win.apply_settings_candidate_result(
            CandidateResult(request_id="r1", region=req.region, values=("a",), source="Fake")
        ) is True
        assert recording.save_calls == 0
        assert page.dirty is True
        assert page.build_draft()["openchamber"]["url"] == "http://10.0.0.5:8080"

    def test_refresh_only_emits_candidate_request(self, qapp):
        recording = _RecordOnlySaveCtrl()
        win = _make_win(save_ctrl=recording)
        outbound = _outbound(win)
        page = win.settings_page
        req = _refresh(page, CandidateKind.SESSION, "r1")
        assert len(outbound) == 1
        assert isinstance(outbound[0], CandidateRequest)
        assert outbound[0] is req
        assert recording.save_calls == 0
        assert win.apply_settings_candidate_result(
            CandidateResult(request_id="r1", region=req.region, values=("s",), source="Fake")
        ) is True
        assert len(outbound) == 1

    def test_meta_result_cannot_touch_task_or_other_areas(self, qapp):
        snap = fake_snapshot(config_revision="7")
        win = _make_win(snapshot=snap)
        page = win.settings_page
        s_req = _refresh(page, CandidateKind.SESSION, "s1")
        assert win.apply_settings_candidate_result(
            CandidateResult(request_id="s1", region=s_req.region, values=("sess-keep",), source="Fake")
        ) is True
        m_req = _refresh(page, CandidateKind.META, "m1")
        assert win.apply_settings_candidate_result(
            CandidateResult(request_id="m1", region=m_req.region, source="MetaSrc", metadata=(("状态", "正常"),))
        ) is True
        # META 只进 META 区域，不覆盖 SESSION 候选
        assert _combo_items(page, CandidateKind.SESSION) == ["sess-keep"]
        assert "MetaSrc" in page._candidate_source_labels[CandidateKind.META].text()
        # META 不影响任务/高优先级信息
        assert win.snapshot is snap
        assert win.snapshot.active_task.task_id == "task-123"
        assert win.snapshot.active_task.config_revision == "7"