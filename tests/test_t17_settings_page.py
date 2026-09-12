"""T17-B2：SettingsPage 单元测试（全部 offscreen，禁止真实 Dialog/DB/网络）。

测试 46 项（可以多于 46）覆盖 B2 任务书 §32 清单。"""
from __future__ import annotations

import os
import sys
import textwrap
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFileDialog

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_APP_INSTANCE = QApplication.instance() or QApplication([])

from app.commands import CandidateKind, CandidateRequest, CandidateRegion, CandidateResult  # noqa: E402
from app.snapshots import fake_snapshot, empty_snapshot  # noqa: E402
from core.domain import SettingsDraft, SettingsSnapshot, config_to_dict  # noqa: E402
from core.settings_service import validate_config  # noqa: E402
from ui.settings_page import SettingsPage, FIELDS  # noqa: E402

_TAB_TITLES = ("执行端", "续接策略", "轮换", "外观", "日志诊断")


def _snapshot(revision: int = 7) -> SettingsSnapshot:
    body = validate_config(SettingsDraft.defaults())
    return SettingsSnapshot(revision=revision, created_at="2026-01-01T00:00", config=body)


def _base_draft() -> SettingsDraft:
    return SettingsDraft.from_mapping(deepcopy(dict(SettingsDraft.defaults())))


def _default_url() -> str:
    return SettingsDraft.defaults()["openchamber"]["url"]


def _region(kind: CandidateKind, directory: str = "/proj", endpoint: str | None = None) -> CandidateRegion:
    return CandidateRegion(
        kind=kind,
        directory=directory,
        endpoint=endpoint if endpoint is not None else _default_url(),
    )


class TestTabOrder:
    def test_five_tabs_in_correct_order(self):
        page = SettingsPage()
        assert page.tabs.count() == 5
        for i, title in enumerate(_TAB_TITLES):
            assert page.tabs.tabText(i) == title


class TestDraftCompleteness:
    def test_with_committed(self):
        snap = _snapshot()
        page = SettingsPage(committed_snapshot=snap)
        result = page.build_draft()
        assert result == config_to_dict(snap.config)

    def test_without_committed(self):
        page = SettingsPage()
        result = page.build_draft()
        assert result == _base_draft()

    def test_has_all_top_level_sections(self):
        page = SettingsPage()
        result = page.build_draft()
        for key in ("schema_version", "default_target", "openchamber", "recovery", "http",
                     "network", "rotation", "delivery", "limits", "logs", "ui"):
            assert key in result

    def test_no_partial_leaves_lost(self):
        page = SettingsPage()
        result = page.build_draft()
        assert isinstance(result["recovery"]["fast_read_retry_delays_seconds"], list)
        assert result["limits"]["queued_tasks"] == 1000


class TestDirty:
    def test_single_change_makes_dirty(self):
        page = SettingsPage()
        page.model_combo.setCurrentText("m")
        assert page.dirty is True

    def test_revert_makes_clean(self):
        page = SettingsPage()
        base_model = page.build_draft()["openchamber"]["model"]
        page.model_combo.setCurrentText("X")
        assert page.dirty is True
        page.model_combo.setCurrentText(base_model)
        assert page.dirty is False

    def test_tab_switch_keeps_dirty(self):
        page = SettingsPage()
        page.model_combo.setCurrentText("b")
        assert page.dirty is True
        page.tabs.setCurrentIndex(1)
        assert page.dirty is True

    def test_render_does_not_reset_dirty(self):
        page = SettingsPage()
        page.model_combo.setCurrentText("b")
        assert page.dirty is True
        page.render(fake_snapshot())
        assert page.dirty is True

    def test_candidate_result_does_not_change_dirty(self):
        page = SettingsPage()
        orig_dirty = page.dirty
        req = CandidateRequest(request_id="r", region=_region(CandidateKind.MODEL))
        page._pending_requests[CandidateKind.MODEL] = req
        page.apply_candidate_result(CandidateResult(request_id="r", region=_region(CandidateKind.MODEL), values=("m1",), source="X"))
        assert page.dirty == orig_dirty

    def test_initial_clean_with_committed(self):
        page = SettingsPage(committed_snapshot=_snapshot())
        assert page.dirty is False

    def test_first_save_clean_dirty(self):
        page = SettingsPage()
        assert page.dirty is False


class TestNeedsSave:
    def test_first_save_needs_save(self):
        page = SettingsPage()
        assert page.needs_save is True

    def test_needs_save_when_dirty(self):
        page = SettingsPage(committed_snapshot=_snapshot())
        page.model_combo.setCurrentText("z")
        assert page.needs_save is True

    def test_clean_with_committed_not_needing_save(self):
        page = SettingsPage(committed_snapshot=_snapshot())
        assert page.needs_save is False


class TestSaveButton:
    def test_first_save_button_enabled(self):
        page = SettingsPage()
        assert page.save_button.isEnabled() is True

    def test_emit_full_draft_and_base_revision(self):
        page = SettingsPage(committed_snapshot=_snapshot(3))
        mock = MagicMock()
        page.save_requested.connect(mock)
        page.model_combo.setCurrentText("M-new")
        page._on_save_clicked()
        draft_arg, base_arg = mock.call_args[0]
        assert isinstance(draft_arg, SettingsDraft)
        assert base_arg == 3

    def test_emit_does_not_clear_dirty(self):
        page = SettingsPage(committed_snapshot=_snapshot())
        page.model_combo.setCurrentText("V")
        mock = MagicMock()
        page.save_requested.connect(mock)
        page._on_save_clicked()
        assert page.dirty is True

    def test_emit_does_not_set_committed(self):
        page = SettingsPage(committed_snapshot=_snapshot())
        old_committed = page._committed
        mock = MagicMock()
        page.save_requested.connect(mock)
        page._on_save_clicked()
        assert page._committed is old_committed


class TestDiscard:
    def test_discard_committed(self):
        snap = _snapshot()
        page = SettingsPage(committed_snapshot=snap)
        page.model_combo.setCurrentText("X")
        page.discard_changes()
        assert page.dirty is False
        assert page.build_draft() == config_to_dict(snap.config)

    def test_discard_first_save(self):
        page = SettingsPage()
        page.model_combo.setCurrentText("X")
        page.discard_changes()
        assert page.build_draft() == _base_draft()
        assert page.needs_save is True


class TestDirectoryPicker:
    def test_cancel_no_change(self):
        picker = MagicMock(return_value=None)
        page = SettingsPage(directory_picker=picker)
        base_dir = page.build_draft()["openchamber"]["directory"]
        page._on_pick_directory()
        assert page.build_draft()["openchamber"]["directory"] == base_dir

    def test_select_only_updates_draft(self):
        picker = MagicMock(return_value="/new")
        page = SettingsPage(directory_picker=picker)
        mock = MagicMock()
        page.save_requested.connect(mock)
        page._on_pick_directory()
        assert page.build_draft()["openchamber"]["directory"] == "/new"
        assert page.dirty is True
        mock.assert_not_called()


class TestSessionProtection:
    def _apply_session(self, page, region):
        page._pending_requests[CandidateKind.SESSION] = CandidateRequest(
            request_id="r", region=region
        )
        result = CandidateResult(
            request_id="r", region=region, values=("S-cand",), source="s"
        )
        assert page.apply_candidate_result(result) is True

    def test_manual_session_preserves_on_dir_change(self):
        page = SettingsPage(directory_picker=lambda: "/new")
        page.session_combo.setCurrentText("S-user")
        page._on_pick_directory()
        assert page.session_combo.currentText() == "S-user"
        assert page._session_hint_label.text() == "会话待核验"

    def test_candidate_derived_session_cleared_on_mismatch(self):
        page = SettingsPage(directory_picker=lambda: "/new")
        self._apply_session(page, _region(CandidateKind.SESSION, directory="/old"))
        page.session_combo.setCurrentIndex(page.session_combo.findText("S-cand"))  # 从候选列表选中 → derived
        assert page._session_candidate_region is not None
        page._on_pick_directory()
        assert page.session_combo.currentText() == ""
        assert "旧目录候选会话已失效" in page._session_hint_label.text()

    def test_same_context_no_change(self):
        page = SettingsPage(directory_picker=lambda: "/new")
        self._apply_session(page, _region(CandidateKind.SESSION, directory="/new"))
        index = page.session_combo.findText("S-cand")
        page.session_combo.setCurrentIndex(index)
        assert page._session_candidate_region is not None
        page._on_pick_directory()
        assert page.session_combo.currentText() == "S-cand"
        assert page._session_hint_label.text() == ""


class TestContextGate:
    def test_url_change_clears_pending(self):
        page = SettingsPage()
        page._pending_requests[CandidateKind.AGENT] = CandidateRequest(
            request_id="old", region=_region(CandidateKind.AGENT)
        )
        page._widgets["openchamber.url"].setText("http://new")
        assert CandidateKind.AGENT not in page._pending_requests

    def test_stale_result_rejected(self):
        page = SettingsPage()
        req = CandidateRequest(request_id="r1", region=_region(CandidateKind.SESSION))
        page._pending_requests[CandidateKind.SESSION] = req
        page._widgets["openchamber.url"].setText("http://new")
        result = CandidateResult(request_id="r1", region=_region(CandidateKind.SESSION), source="s")
        assert page.apply_candidate_result(result) is False


class TestCandidateResult:
    def test_manual_value_preserved(self):
        page = SettingsPage()
        page.model_combo.setCurrentText("M-user")
        req = CandidateRequest(request_id="r", region=_region(CandidateKind.MODEL))
        page._pending_requests[CandidateKind.MODEL] = req
        page.apply_candidate_result(CandidateResult(request_id="r", region=_region(CandidateKind.MODEL), values=("m1", "m2"), source="s"))
        assert page.model_combo.currentText() == "M-user"
        assert page.model_combo.findText("m1") >= 0

    def test_empty_values_no_clear(self):
        page = SettingsPage()
        page.session_combo.setCurrentText("S-u")
        req = CandidateRequest(request_id="r", region=_region(CandidateKind.SESSION))
        page._pending_requests[CandidateKind.SESSION] = req
        page.apply_candidate_result(CandidateResult(request_id="r", region=_region(CandidateKind.SESSION), values=(), source="s"))
        assert page.session_combo.currentText() == "S-u"

    def test_error_keeps_old(self):
        page = SettingsPage()
        page.model_combo.setCurrentText("m1")
        req = CandidateRequest(request_id="r", region=_region(CandidateKind.MODEL))
        page._pending_requests[CandidateKind.MODEL] = req
        ok = page.apply_candidate_result(CandidateResult(request_id="r", region=_region(CandidateKind.MODEL), error="网络不可达", source="x"))
        assert ok is True
        assert page.model_combo.currentText() == "m1"

    def test_source_label_updated(self):
        page = SettingsPage()
        req = CandidateRequest(request_id="r", region=_region(CandidateKind.AGENT))
        page._pending_requests[CandidateKind.AGENT] = req
        page.apply_candidate_result(CandidateResult(request_id="r", region=_region(CandidateKind.AGENT), values=("a",), source="Fake"))
        assert "Fake" in page._candidate_source_labels[CandidateKind.AGENT].text()

    def test_stale_by_request_id(self):
        page = SettingsPage()
        req = CandidateRequest(request_id="r2", region=_region(CandidateKind.MODEL))
        page._pending_requests[CandidateKind.MODEL] = req
        result = CandidateResult(request_id="r1", region=_region(CandidateKind.MODEL), values=("m",), source="x")
        assert page.apply_candidate_result(result) is False

    def test_stale_by_directory(self):
        page = SettingsPage()
        req = CandidateRequest(request_id="r", region=_region(CandidateKind.MODEL, directory="/old"))
        page._pending_requests[CandidateKind.MODEL] = req
        result = CandidateResult(request_id="r", region=_region(CandidateKind.MODEL, directory="/new"), values=(), source="x")
        assert page.apply_candidate_result(result) is False

    def test_stale_by_project(self):
        page = SettingsPage()
        req = CandidateRequest(request_id="r", region=_region(CandidateKind.MODEL, directory="/proj"))
        page._pending_requests[CandidateKind.MODEL] = req
        result = CandidateResult(request_id="r", region=CandidateRegion(kind=CandidateKind.MODEL, directory="/proj", endpoint="http://127.0.0.1:57123", project_key="other"), values=(), source="x")
        assert page.apply_candidate_result(result) is False

    def test_session_meta_independent(self):
        page = SettingsPage()
        sr = CandidateRequest(request_id="s", region=_region(CandidateKind.SESSION))
        mr = CandidateRequest(request_id="m", region=_region(CandidateKind.META))
        page._pending_requests.update({CandidateKind.SESSION: sr, CandidateKind.META: mr})
        assert page.apply_candidate_result(CandidateResult(request_id="m", region=_region(CandidateKind.META), source="s")) is True
        assert CandidateKind.SESSION in page._pending_requests
        assert CandidateKind.META not in page._pending_requests

    def test_meta_response_no_affect_revision(self):
        page = SettingsPage(committed_snapshot=_snapshot())
        snap = fake_snapshot(config_revision="10")
        page.render(snap)
        rev_before = page._revision_label.text()
        req = CandidateRequest(request_id="r", region=_region(CandidateKind.META))
        page._pending_requests[CandidateKind.META] = req
        page.apply_candidate_result(CandidateResult(request_id="r", region=_region(CandidateKind.META), source="s"))
        assert page._revision_label.text() == rev_before

    def test_manual_session_not_in_candidates_hint(self):
        page = SettingsPage()
        page.session_combo.setCurrentText("S-u")
        req = CandidateRequest(request_id="r", region=_region(CandidateKind.SESSION))
        page._pending_requests[CandidateKind.SESSION] = req
        page.apply_candidate_result(CandidateResult(request_id="r", region=_region(CandidateKind.SESSION), values=("S1",), source="s"))
        assert "核对" in page._session_hint_label.text()


class TestReadonly:
    def test_capability_profile_readonly(self):
        page = SettingsPage()
        assert page._widgets["openchamber.capability_profile"].isReadOnly() is True

    def test_prompt_version_readonly(self):
        page = SettingsPage()
        assert page._widgets["recovery.prompt_version"].isReadOnly() is True


class TestEditableCombos:
    def test_model_editable(self):
        page = SettingsPage()
        assert page.model_combo.isEditable() is True

    def test_agent_editable(self):
        page = SettingsPage()
        assert page.agent_combo.isEditable() is True

    def test_session_editable(self):
        page = SettingsPage()
        assert page.session_combo.isEditable() is True

    def test_no_allowlist_restriction(self):
        page = SettingsPage()
        page.model_combo.setCurrentText("wildcard-模型-123")
        assert page.model_combo.currentText() == "wildcard-模型-123"
        assert page.dirty is True


class TestLayoutStructure:
    def test_save_bar_outside_scroll(self):
        page = SettingsPage()
        inside = False
        parent = page.save_button.parent()
        while parent is not None:
            if parent in page._scrolls.values():
                inside = True
                break
            parent = parent.parent()
        assert not inside

    def test_save_button_visible(self):
        page = SettingsPage()
        page.resize(1200, 800)
        page.show()
        _APP_INSTANCE.processEvents()
        assert page.save_button.isVisible() is True

    def test_480_horizontal_policy(self):
        page = SettingsPage()
        page.resize(480, 800)
        assert page._scrolls["执行端"].horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff


class TestSingleColumn:
    def test_wrap_all_rows(self):
        page = SettingsPage()
        from PySide6.QtWidgets import QFormLayout
        page.set_single_column(True)
        for form in page._forms:
            assert form.rowWrapPolicy() == QFormLayout.WrapAllRows

    def test_dont_wrap(self):
        page = SettingsPage()
        from PySide6.QtWidgets import QFormLayout
        page.set_single_column(False)
        for form in page._forms:
            assert form.rowWrapPolicy() == QFormLayout.DontWrapRows


class TestFocusTarget:
    def test_set_to_default_target(self):
        page = SettingsPage()
        assert page.focus_target is page._widgets["default_target"]
        assert page.focus_target.focusPolicy() != Qt.NoFocus


class TestSafety:
    def test_no_theme_apply(self):
        src = Path("ui/settings_page.py").read_text(encoding="utf-8")
        assert "apply_theme(" not in src
        assert "setStyleSheet" not in src

    def test_no_storage_import(self):
        tree = __import__("ast").parse(Path("ui/settings_page.py").read_text(encoding="utf-8"))
        modules = set()
        for node in __import__("ast").walk(tree):
            if isinstance(node, __import__("ast").ImportFrom) and node.module:
                modules.add(node.module)
        assert "storage.settings_store" not in modules
        assert "storage.database" not in modules
        assert "core.settings_service" not in modules

    def test_no_real_network(self):
        src = Path("ui/settings_page.py").read_text(encoding="utf-8").lower()
        assert "import requests" not in src
        assert "import urllib" not in src
        assert "import socket" not in src


# ---------------------------------------------------------------- B2R helpers

def _items(combo):
    return [combo.itemText(i) for i in range(combo.count())]


def _apply(page, kind, values, region=None, request_id="r", source="s", error=None):
    region = region or _region(kind)
    page._pending_requests[kind] = CandidateRequest(request_id=request_id, region=region)
    page.apply_candidate_result(
        CandidateResult(
            request_id=request_id,
            region=region,
            values=values,
            source=source,
            error=error,
        )
    )


def _flatten(mapping, prefix=""):
    out = set()
    for k, v in mapping.items():
        p = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out |= _flatten(v, p)
        else:
            out.add(p)
    return out


# ---------------------------------------------------------------- B2R tests

class TestDirectoryPickerDefault:
    def test_production_default_picker(self, monkeypatch):
        page = SettingsPage()
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: "D:\\work")
        page._on_pick_directory()
        assert page.build_draft()["openchamber"]["directory"] == "D:\\work"

    def test_production_picker_cancel(self, monkeypatch):
        page = SettingsPage()
        base = page.build_draft()["openchamber"]["directory"]
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: "")
        page._on_pick_directory()
        assert page.build_draft()["openchamber"]["directory"] == base


class TestCandidateReplacement:
    def test_model_replacement(self):
        page = SettingsPage()
        _apply(page, CandidateKind.MODEL, ("A1", "A2"))
        page.model_combo.setCurrentText("M-user")
        _apply(page, CandidateKind.MODEL, ("B1", "B2"))
        assert _items(page.model_combo) == ["B1", "B2"]
        assert page.model_combo.currentText() == "M-user"

    def test_agent_replacement(self):
        page = SettingsPage()
        _apply(page, CandidateKind.AGENT, ("A1", "A2"))
        _apply(page, CandidateKind.AGENT, ("B1", "B2"))
        assert _items(page.agent_combo) == ["B1", "B2"]

    def test_session_replacement(self):
        page = SettingsPage()
        _apply(page, CandidateKind.SESSION, ("S1", "S2"))
        _apply(page, CandidateKind.SESSION, ("S3",))
        assert _items(page.session_combo) == ["S3"]

    def test_empty_clears_old(self):
        page = SettingsPage()
        _apply(page, CandidateKind.MODEL, ("M1", "M2"))
        assert _items(page.model_combo) == ["M1", "M2"]
        _apply(page, CandidateKind.MODEL, ())
        assert _items(page.model_combo) == []
        assert page.dirty is False

    def test_error_preserves_old_items_and_region(self):
        page = SettingsPage()
        old_region = _region(CandidateKind.MODEL, directory="/proj")
        _apply(page, CandidateKind.MODEL, ("M1",), region=old_region)
        assert page._candidate_region[CandidateKind.MODEL] == old_region
        _apply(page, CandidateKind.MODEL, (), source="x", error="网络不可达")
        assert _items(page.model_combo) == ["M1"]
        assert page._candidate_region[CandidateKind.MODEL] == old_region


class TestContextChangeClearsCandidate:
    def _prepopulate(self, page):
        _apply(page, CandidateKind.MODEL, ("M1",), source="m-src")
        _apply(page, CandidateKind.AGENT, ("A1",), source="a-src")
        _apply(page, CandidateKind.SESSION, ("S1",), source="s-src")
        page._meta_detail.setText("k: v")

    def test_directory_change_clears_all(self):
        page = SettingsPage(directory_picker=lambda: "/new")
        self._prepopulate(page)
        page.model_combo.setCurrentText("M-man")
        page._on_pick_directory()
        assert _items(page.model_combo) == []
        assert _items(page.agent_combo) == []
        assert _items(page.session_combo) == []
        assert page._candidate_region == {}
        assert page._meta_detail.text() == ""
        assert page.model_combo.currentText() == "M-man"
        assert page.agent_combo.currentText() == _base_draft()["openchamber"]["agent"]

    def test_endpoint_change_clears_all(self):
        page = SettingsPage()
        self._prepopulate(page)
        page.model_combo.setCurrentText("M-man")
        page._widgets["openchamber.url"].setText("http://new")
        assert _items(page.model_combo) == []
        assert page._candidate_region == {}
        assert page._meta_detail.text() == ""
        assert page.model_combo.currentText() == "M-man"


class TestRebaseClearsCandidate:
    def _prepopulate(self, page):
        _apply(page, CandidateKind.MODEL, ("M1",), source="src")
        _apply(page, CandidateKind.SESSION, ("S1",), source="src")
        page._session_hint_label.setText("some hint")

    def test_set_committed_clears(self):
        page = SettingsPage()
        self._prepopulate(page)
        snap = _snapshot()
        page.set_committed_snapshot(snap)
        assert _items(page.model_combo) == []
        assert _items(page.session_combo) == []
        assert page._candidate_region == {}
        assert page._session_hint_label.text() == ""
        assert page.build_draft() == config_to_dict(snap.config)

    def test_discard_clears(self):
        page = SettingsPage()
        self._prepopulate(page)
        page.model_combo.setCurrentText("X")
        page.discard_changes()
        assert _items(page.model_combo) == []
        assert page._candidate_region == {}
        assert page._session_hint_label.text() == ""
        assert page.dirty is False


class TestRegistryContract:
    def test_fields_match_defaults_paths(self):
        fields = {f.path for f in FIELDS}
        leaves = _flatten(dict(SettingsDraft.defaults()))
        assert leaves == fields
        assert len(fields) == 62
