"""T17-B2：设置页草稿界面（独立页码，不接真实 SettingsService/MainWindow）。

职责边界（与 T17-A/B1 合同一致）：
- 只维护：完整 draft / committed baseline / dirty / 五 Tab / 候选 request-result seam /
  目录选择草稿语义 / 固定保存栏 / revision 解释。
- 绝不：调用 SettingsService/SettingsStore/数据库、网络请求、创建 session、
  修改 active task / ApplicationSnapshot，也不在本页 apply_theme。

页面权威 = 从控件实时构建的完整 SettingsDraft（build_draft）；Qt 控件
不是权威。dirty = build_draft() != draft_base（深层值比较，"改回原值"回到 False）。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum
from typing import Callable
from uuid import uuid4

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.commands import (
    CandidateKind,
    CandidateRequest,
    CandidateRegion,
    CandidateResult,
    candidate_result_is_stale,
)
from app.snapshots import ApplicationSnapshot, empty_snapshot
from core.domain import (
    BusyResumePolicy,
    SessionBindingMode,
    SettingsDraft,
    TargetExecutor,
    TransportProfile,
    UiTheme,
    config_to_dict,
)
from ui.components import PrimaryButton, SecondaryButton, StatusBadge, TextButton, TextInput
from ui.status_presenter import present_settings_revision


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """声明式字段注册：UI 只做结构转换，业务范围裁决留给 SettingsService。

    path 固定为 “section.leaf” 两级（配置树不超过两层）。
    kind ∈ enum/bool/int/int_none/float/list/str/combo/readonly/directory。
    """

    path: str
    tab: str
    kind: str
    label: str
    section: str = ""  # 卡片分组标题；空则并入对应 tab 的单卡
    enum_cls: object | None = None


FIELDS: tuple[FieldSpec, ...] = (
    # ------------------------------------------------------- 执行端
    FieldSpec("schema_version", "执行端", "readonly", "配置 schema 版本"),
    FieldSpec("default_target", "执行端", "enum", "默认执行端", enum_cls=TargetExecutor),
    FieldSpec("openchamber.url", "执行端", "str", "OpenChamber 接口地址"),
    FieldSpec("openchamber.directory", "执行端", "directory", "工作目录"),
    FieldSpec("openchamber.session_policy", "执行端", "enum", "会话绑定模式", enum_cls=SessionBindingMode),
    FieldSpec("openchamber.session_id", "执行端", "combo", "会话 ID"),
    FieldSpec("openchamber.agent", "执行端", "combo", "Agent"),
    FieldSpec("openchamber.model", "执行端", "combo", "模型"),
    FieldSpec("openchamber.capability_profile", "执行端", "readonly", "能力档案"),
    FieldSpec("openchamber.auto_open_session", "执行端", "bool", "自动开启会话"),
    # ------------------------------------------------------- 续接策略（常用）
    FieldSpec("recovery.automatic_resume", "续接策略", "bool", "自动续接", section="常用"),
    FieldSpec("recovery.resume_after_restart", "续接策略", "bool", "重启后自动续接", section="常用"),
    FieldSpec("recovery.completion_timeout_seconds", "续接策略", "float", "完成超时（秒）", section="常用"),
    FieldSpec("recovery.poll_interval_seconds", "续接策略", "float", "轮询间隔（秒）", section="常用"),
    FieldSpec("recovery.completion_grace_seconds", "续接策略", "float", "完成宽限（秒）", section="常用"),
    FieldSpec("recovery.idle_confirmations", "续接策略", "int", "空闲确认次数", section="常用"),
    FieldSpec("recovery.recovery_delay_seconds", "续接策略", "float", "恢复延迟（秒）", section="常用"),
    FieldSpec("recovery.awaiting_progress_seconds", "续接策略", "float", "待进展等待（秒）", section="常用"),
    FieldSpec("recovery.no_progress_cooldown_threshold", "续接策略", "int", "无进展冷却阈值（次）", section="常用"),
    FieldSpec("recovery.cooldown_seconds", "续接策略", "float", "冷却时间（秒）", section="常用"),
    FieldSpec("recovery.busy_stale_log_seconds", "续接策略", "float", "忙碌日志过期（秒）", section="常用"),
    FieldSpec("recovery.busy_review_seconds", "续接策略", "float", "忙碌复查（秒）", section="常用"),
    FieldSpec("recovery.busy_resume_policy", "续接策略", "enum", "忙碌恢复策略", section="常用", enum_cls=BusyResumePolicy),
    FieldSpec("recovery.resume_truncated_output", "续接策略", "bool", "恢复截断输出", section="常用"),
    FieldSpec("recovery.prompt_version", "续接策略", "readonly", "续接提示词版本", section="常用"),
    # ------------------------------------------------------- 续接策略（高级）
    FieldSpec("recovery.cumulative_resume_limit", "续接策略", "int_none", "累计续接上限（可空）", section="高级"),
    FieldSpec("recovery.fast_read_retry_delays_seconds", "续接策略", "list", "快读重试延迟（秒，逗号分隔）", section="高级"),
    FieldSpec("recovery.sustained_read_delays_seconds", "续接策略", "list", "持续读延迟（秒，逗号分隔）", section="高级"),
    FieldSpec("http.connect_timeout_seconds", "续接策略", "float", "连接超时（秒）", section="高级"),
    FieldSpec("http.read_timeout_seconds", "续接策略", "float", "读取超时（秒）", section="高级"),
    FieldSpec("http.operation_budget_seconds", "续接策略", "float", "操作预算（秒）", section="高级"),
    FieldSpec("http.automatic_post_retries", "续接策略", "int", "自动重试次数", section="高级"),
    FieldSpec("network.model_probe_target", "续接策略", "str", "模型探测目标", section="高级"),
    FieldSpec("network.interval_seconds", "续接策略", "float", "探测间隔（秒）", section="高级"),
    FieldSpec("network.recovery_successes", "续接策略", "int", "恢复成功确认数", section="高级"),
    FieldSpec("network.zerotier_enabled", "续接策略", "bool", "启用 ZeroTier", section="高级"),
    FieldSpec("network.external_bridge_enabled", "续接策略", "bool", "启用外部网桥", section="高级"),
    FieldSpec("network.external_bridge_path", "续接策略", "str", "外部网桥路径", section="高级"),
    FieldSpec("network.sample_ttl_seconds", "续接策略", "float", "采样有效时间（秒）", section="高级"),
    FieldSpec("delivery.profile", "续接策略", "enum", "交付协议", section="高级", enum_cls=TransportProfile),
    FieldSpec("delivery.automatic_ack", "续接策略", "bool", "自动确认交付", section="高级"),
    FieldSpec("delivery.max_unconfirmed_offers", "续接策略", "int", "未确认交付上限", section="高级"),
    FieldSpec("limits.incoming_body_bytes", "续接策略", "int", "单条正文上限（字节）", section="高级"),
    FieldSpec("limits.incoming_envelope_bytes", "续接策略", "int", "信封上限（字节）", section="高级"),
    FieldSpec("limits.queued_tasks", "续接策略", "int", "队列任务上限", section="高级"),
    # ------------------------------------------------------- 轮换
    FieldSpec("rotation.enabled", "轮换", "bool", "启用目标轮换"),
    FieldSpec("rotation.success_threshold", "轮换", "int", "轮换成功阈值"),
    FieldSpec("rotation.inherit_auto_accept", "轮换", "bool", "继承自动接受"),
    # ------------------------------------------------------- 外观
    FieldSpec("ui.theme", "外观", "enum", "主题", enum_cls=UiTheme),
    FieldSpec("ui.density", "外观", "str", "界面密度"),
    FieldSpec("ui.always_on_top", "外观", "bool", "置顶窗口"),
    FieldSpec("ui.close_action", "外观", "str", "关闭窗口动作"),
    FieldSpec("ui.auto_start_with_windows", "外观", "bool", "随系统启动"),
    FieldSpec("ui.quit_warning_seconds", "外观", "float", "退出确认等待（秒）"),
    # ------------------------------------------------------- 日志诊断
    FieldSpec("logs.event_retention_days", "日志诊断", "int", "事件保留天数"),
    FieldSpec("logs.event_budget_bytes", "日志诊断", "int", "事件容量上限（字节）"),
    FieldSpec("logs.debug_enabled", "日志诊断", "bool", "启用调试日志"),
    FieldSpec("logs.debug_file_bytes", "日志诊断", "int", "单个调试日志上限（字节）"),
    FieldSpec("logs.debug_files", "日志诊断", "int", "调试日志份数"),
    FieldSpec("logs.debug_retention_days", "日志诊断", "int", "调试日志保留天数"),
    FieldSpec("logs.include_task_body", "日志诊断", "bool", "日志包含任务正文"),
    FieldSpec("logs.include_model_response", "日志诊断", "bool", "日志包含模型返回"),
)


def _numberify(value: float) -> int | float:
    """数值规范化：整数型 float 转 int，避免“2”与“2.0”误判 dirty。"""
    return int(value) if float(value).is_integer() else float(value)


def _parse_float(text: str) -> int | float | str:
    s = text.strip()
    try:
        return _numberify(float(s))
    except ValueError:
        return s


def _parse_int(text: str) -> int | str:
    s = text.strip()
    try:
        return int(s)
    except ValueError:
        return s


def _parse_float_list(text: str) -> list | str:
    s = text.strip()
    if not s:
        return []
    parts = [p.strip() for p in s.replace("，", ",").split(",") if p.strip()]
    try:
        return [_numberify(float(p)) for p in parts]
    except ValueError:
        return s


class SettingsPage(QWidget):
    """设置页：五 Tab 草稿表单 + 固定保存栏 + 异步候选 seam。"""

    save_requested = Signal(object, object)  # (SettingsDraft, base_revision: int|None)
    candidate_refresh_requested = Signal(object)  # CandidateRequest

    def __init__(
        self,
        committed_snapshot=None,
        snapshot: ApplicationSnapshot | None = None,
        *,
        request_id_factory: Callable[[], str] | None = None,
        directory_picker: Callable[[], str | None] | None = None,
        project_key_resolver: Callable[[str], str | None] | None = None,
    ) -> None:
        super().__init__()
        self._committed = committed_snapshot
        self._snapshot = snapshot if snapshot is not None else empty_snapshot()
        self._request_id_factory = request_id_factory or (lambda: str(uuid4()))
        self._directory_picker = directory_picker if directory_picker is not None else self._default_directory_picker
        self._project_key_resolver = project_key_resolver

        self._draft_base: SettingsDraft = SettingsDraft.defaults()
        self._loading = False
        self._pending_requests: dict[CandidateKind, CandidateRequest] = {}
        self._candidate_source: dict[CandidateKind, str] = {}
        self._candidate_region: dict[CandidateKind, CandidateRegion] = {}
        self._session_candidate_region: CandidateRegion | None = None
        self._last_context: tuple[str, str] = ("", "")
        self._dirty = False

        self._build_ui()

        self._loading = True
        self._rebase(self._committed)
        self._restore_controls()
        self._loading = False
        self._recompute_dirty()

        self.session_combo = self._widgets["openchamber.session_id"]
        self.agent_combo = self._widgets["openchamber.agent"]
        self.model_combo = self._widgets["openchamber.model"]

    # ------------------------------------------------------------------ UI 结构

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        self._revision_label = QLabel()
        self._revision_label.setWordWrap(True)
        refresh_heading = self._revision_label
        refresh_heading.setProperty("heading", "true")
        root.addWidget(refresh_heading)

        draft_row = QHBoxLayout()
        self._draft_badge = StatusBadge("", "neutral")
        self._session_hint_label = QLabel("")
        self._session_hint_label.setWordWrap(True)
        draft_row.addWidget(self._draft_badge)
        draft_row.addWidget(self._session_hint_label)
        draft_row.addStretch(1)
        root.addLayout(draft_row)

        self.tabs = QTabWidget()
        self._scrolls: dict[str, QScrollArea] = {}
        self._forms: list[QFormLayout] = []
        self._widgets: dict[str, object] = {}  # path -> 主控件
        self._candidate_source_labels: dict[CandidateKind, QLabel] = {}

        for tab_title in ("执行端", "续接策略", "轮换", "外观", "日志诊断"):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            inner = QWidget()
            inner_layout = QVBoxLayout(inner)
            inner_layout.setContentsMargins(12, 12, 12, 12)
            inner_layout.setSpacing(12)
            card_groups: dict[str, QVBoxLayout] = {}
            for spec in FIELDS:
                if spec.tab != tab_title:
                    continue
                group = card_groups.setdefault(
                    spec.section or tab_title,
                    self._make_card(inner_layout, spec.section or tab_title),
                )
                form = QFormLayout()
                form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
                form.setLabelAlignment(Qt.AlignRight | Qt.AlignTrailing | Qt.AlignVCenter)
                form.setRowWrapPolicy(QFormLayout.DontWrapRows)
                self._forms.append(form)
                group.addLayout(form)
                self._add_field_row(form, spec)
            if tab_title == "续接策略":
                self._build_meta_area(inner_layout)
            scroll.setWidget(inner)
            self._scrolls[tab_title] = scroll
            self.tabs.addTab(scroll, tab_title)
        root.addWidget(self.tabs, stretch=1)

        save_bar = QHBoxLayout()
        self.save_bar_widget = QWidget()
        save_bar.addWidget(self._draft_status_note())
        save_bar.addStretch(1)
        self.discard_button = SecondaryButton("放弃更改")
        self.discard_button.clicked.connect(self.discard_changes)
        self.save_button = PrimaryButton("保存设置")
        self.save_button.clicked.connect(self._on_save_clicked)
        save_bar.addWidget(self.discard_button)
        save_bar.addWidget(self.save_button)
        self.save_bar_layout = save_bar
        bar_container = QVBoxLayout()
        bar_container.addLayout(save_bar)
        self._save_feedback = StatusBadge("", "neutral")
        bar_container.addWidget(self._save_feedback)
        root.addLayout(bar_container)

        self.focus_target = self._widgets["default_target"]

    def _make_card(self, parent_layout: QVBoxLayout, title: str):
        from ui.components import Card

        card = Card(title)
        parent_layout.addWidget(card)
        return card._layout

    def _draft_status_note(self) -> QLabel:
        label = QLabel("")
        label.setObjectName("save_bar_note")
        return label

    def _add_field_row(self, form: QFormLayout, spec: FieldSpec) -> None:
        widget = self._create_field_widget(spec)
        self._widgets[spec.path] = widget
        if spec.kind == "directory":
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)
            row_layout.addWidget(widget, stretch=1)
            pick = TextButton("选择文件夹…")
            pick.clicked.connect(self._on_pick_directory)
            row_layout.addWidget(pick)
            form.addRow(spec.label, row)
            return
        if spec.kind == "combo":
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)
            row_layout.addWidget(widget, stretch=1)
            kind = self._combo_kind(spec.path)
            src = QLabel("")
            refresh = TextButton("刷新候选")
            refresh.clicked.connect(lambda _=False, k=kind: self._on_refresh(k))
            row_layout.addWidget(src)
            row_layout.addWidget(refresh)
            self._candidate_source_labels[kind] = src
            form.addRow(spec.label, row)
            return
        form.addRow(spec.label, widget)

    def _combo_kind(self, path: str) -> CandidateKind:
        if path == "openchamber.session_id":
            return CandidateKind.SESSION
        if path == "openchamber.agent":
            return CandidateKind.AGENT
        return CandidateKind.MODEL

    def _create_field_widget(self, spec: FieldSpec):
        if spec.kind == "enum":
            combo = QComboBox()
            for member in spec.enum_cls:  # type: ignore[attr-defined]
                combo.addItem(str(member.value))
            combo.currentTextChanged.connect(lambda _t, p=spec.path: self._on_value_changed(p))
            self._bind_visible(combo, spec.path)
            return combo
        if spec.kind == "bool":
            check = QCheckBox()
            check.toggled.connect(lambda _f, p=spec.path: self._on_value_changed(p))
            return check
        if spec.kind in ("int", "int_none", "float", "list", "str"):
            field = TextInput()
            field.textChanged.connect(lambda _t, p=spec.path: self._on_value_changed(p))
            self._bind_visible(field, spec.path)
            return field
        if spec.kind == "combo":
            combo = QComboBox()
            combo.setEditable(True)
            combo.setInsertPolicy(QComboBox.NoInsert)
            combo.currentTextChanged.connect(lambda _t, p=spec.path: self._on_value_changed(p))
            self._bind_visible(combo, spec.path)
            return combo
        if spec.kind == "directory":
            field = TextInput()
            field.textChanged.connect(lambda _t, p=spec.path: self._on_value_changed(p))
            self._bind_visible(field, spec.path)
            return field
        if spec.kind == "readonly":
            field = QLineEdit()
            field.setReadOnly(True)
            return field
        raise AssertionError(f"未知字段类型：{spec.kind!r}")

    def _bind_visible(self, widget, path: str) -> None:
        widget.setAccessibleName(path)

    def _build_meta_area(self, inner_layout: QVBoxLayout) -> None:
        from ui.components import Card

        card = Card("信息（Meta）")
        inner_layout.addWidget(card)
        row = QHBoxLayout()
        btn = TextButton("刷新 Meta")
        btn.clicked.connect(lambda _f=False: self._on_refresh(CandidateKind.META))
        row.addWidget(btn)
        self._candidate_source_labels[CandidateKind.META] = QLabel("")
        row.addWidget(self._candidate_source_labels[CandidateKind.META])
        row.addStretch(1)
        card.add_layout(row)
        self._meta_detail = QLabel("")
        self._meta_detail.setWordWrap(True)
        card.add(self._meta_detail)

    # ------------------------------------------------------------ 数据流

    def _split_path(self, path: str) -> tuple[str, str]:
        """path 支持 “section.leaf” 或顶层单段（schema_version / default_target）。"""
        parts = path.split(".", 1)
        if len(parts) == 1:
            return "", path
        return parts[0], parts[1]

    def _leaf_value(self, path: str):
        section, leaf = self._split_path(path)
        if not section:
            return self._draft_base[leaf]
        return self._draft_base[section][leaf]

    def _parse_leaf(self, path: str, widget) -> object:
        spec = {f.path: f for f in FIELDS}[path]
        kind = spec.kind
        if kind == "enum":
            return widget.currentText()
        if kind == "bool":
            return widget.isChecked()
        if kind == "int":
            return _parse_int(widget.text())
        if kind == "int_none":
            text = widget.text().strip()
            if not text:
                return None
            return _parse_int(text)
        if kind == "float":
            return _parse_float(widget.text())
        if kind == "list":
            return _parse_float_list(widget.text())
        if kind in ("str", "combo", "directory"):
            return widget.currentText() if isinstance(widget, QComboBox) else widget.text()
        return self._leaf_value(path)

    def build_draft(self) -> SettingsDraft:
        """完整独立副本：全部 section/leaf 都在；只读字段保留 base，可写字段读控件。

        未改动的字段原样沿用 base（类型保真：默认 60.0 仍是 float），
        只有真正偏离 base 的字段才走控件解析，避免“60.0”/“60”误判 dirty。
        """
        draft = copy.deepcopy(self._draft_base)
        for spec in FIELDS:
            if spec.kind == "readonly":
                continue
            widget = self._widgets[spec.path]
            section, leaf = self._split_path(spec.path)
            value = (
                self._leaf_value(spec.path)
                if self._widget_matches_base(spec, widget)
                else self._parse_leaf(spec.path, widget)
            )
            if not section:
                draft[leaf] = value
            else:
                draft[section][leaf] = value
        return SettingsDraft.from_mapping(copy.deepcopy(draft))

    def _widget_matches_base(self, spec: FieldSpec, widget) -> bool:
        base = self._leaf_value(spec.path)
        if spec.kind == "bool":
            return widget.isChecked() == bool(base)
        if spec.kind == "enum":
            return widget.currentText() == str(base)
        if spec.kind == "list":
            expected = ", ".join(str(v) for v in base)
            return widget.text() == expected
        if spec.kind in ("int", "int_none", "float", "str", "directory", "readonly"):
            expected = "" if base is None else str(base)
            return widget.text() == expected
        if spec.kind == "combo":
            expected = "" if base is None else str(base)
            return widget.currentText() == expected
        return False

    def _on_value_changed(self, path: str) -> None:
        if self._loading:
            return
        if path == "openchamber.session_id":
            # 手工录入与“从候选列表选中”区分：只有与当前候选项文本一致且选中它
            # 才保持 candidate-derived 姿态；否则视为手工输入。
            combo = self._widgets["openchamber.session_id"]
            index = combo.currentIndex()
            if index >= 0 and combo.currentText() == combo.itemText(index):
                self._session_candidate_region = self._candidate_region.get(
                    CandidateKind.SESSION
                )
            else:
                self._session_candidate_region = None
        self._check_context_gate(path)
        self._recompute_dirty()

    def _restore_controls(self) -> None:
        for spec in FIELDS:
            widget = self._widgets[spec.path]
            value = self._leaf_value(spec.path)
            self._set_widget_value(spec.kind, widget, value)
        self._last_context = (self._draft_base["openchamber"]["url"], self._draft_base["openchamber"]["directory"])
        self._pending_requests.clear()

    def _set_widget_value(self, kind: str, widget, value) -> None:
        if kind == "enum":
            text = str(value)
            widget.setCurrentText(text)
        elif kind == "bool":
            widget.setChecked(bool(value))
        elif kind in ("int", "int_none", "float", "str", "directory", "readonly"):
            widget.setText("" if value is None else str(value))
        elif kind == "list":
            widget.setText(", ".join(str(v) for v in value))
        elif kind == "combo":
            widget.setCurrentText("" if value is None else str(value))

    def _recompute_dirty(self) -> None:
        current = self.build_draft()
        self._dirty = current != self._draft_base
        if self._dirty:
            self._draft_badge.set_tone("recovering")
            self._draft_badge.set_value("有未保存修改")
        elif self._committed is None:
            self._draft_badge.set_tone("neutral")
            self._draft_badge.set_value("默认草稿尚未保存")
        else:
            self._draft_badge.set_tone("success")
            self._draft_badge.set_value("已保存")
        self.save_button.setEnabled(self.needs_save)
        self.discard_button.setEnabled(self._dirty)

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def needs_save(self) -> bool:
        return self._dirty or self._committed is None

    @property
    def base_revision(self) -> int | None:
        return self._committed.revision if self._committed is not None else None

    # ------------------------------------------------------------ committed / render

    def set_committed_snapshot(self, snapshot_or_none) -> None:
        """更新 baseline（B3 保存成功后调用）：重建 base，控件回显，dirty 清零。"""
        self._committed = snapshot_or_none
        self._loading = True
        self._rebase(snapshot_or_none)
        self._restore_controls()
        self._loading = False
        self._clear_candidate_context(clear_session_hint=True)
        self._recompute_dirty()
        self._render_revision()

    def _rebase(self, snapshot_or_none) -> None:
        if snapshot_or_none is None:
            self._draft_base = SettingsDraft.defaults()
        else:
            self._draft_base = SettingsDraft.from_mapping(
                copy.deepcopy(config_to_dict(snapshot_or_none.config))
            )

    def render(self, snapshot: ApplicationSnapshot) -> None:
        """只更新 revision/active task 解释；不得重建 draft、不得清 dirty、不得切 Tab。"""
        self._snapshot = snapshot
        self._render_revision()

    def _render_revision(self) -> None:
        committed_revision = self._committed.revision if self._committed is not None else None
        presentation = present_settings_revision(self._snapshot, committed_revision)
        self._revision_label.setText(f"{presentation.headline}\n{presentation.detail}")

    # ------------------------------------------------------------ 保存 / 放弃

    def _on_save_clicked(self) -> None:
        base_revision = self.base_revision
        self.save_requested.emit(self.build_draft(), base_revision)
        # B2 禁止调用 SettingsService：dirty/draft/committed 均保持，等 B3 controller 回结果。

    def discard_changes(self) -> None:
        """ACT22：纯 UI 还原草稿；不修改 committed、不调用 service、不修改任务。"""
        if self._committed is None:
            self._draft_base = SettingsDraft.defaults()
        else:
            self._draft_base = SettingsDraft.from_mapping(
                copy.deepcopy(config_to_dict(self._committed.config))
            )
        self._loading = True
        self._restore_controls()
        self._loading = False
        self._clear_candidate_context(clear_session_hint=True)
        self._recompute_dirty()

    def show_save_feedback(self, message: str, tone: str = "neutral") -> None:
        self._save_feedback.set_tone(tone)
        self._save_feedback.set_value(message)

    # ------------------------------------------------------------ 目录 / 上下文

    def _on_pick_directory(self) -> None:
        picked = self._directory_picker()
        if not picked:  # 用户取消：无变化
            return
        widget = self._widgets["openchamber.directory"]
        self._loading = True
        widget.setText(picked)
        self._loading = False
        self._on_value_changed("openchamber.directory")

    def _check_context_gate(self, path: str) -> None:
        if path not in ("openchamber.url", "openchamber.directory"):
            return
        url = self._display_text("openchamber.url")
        directory = self._display_text("openchamber.directory")
        current = (url, directory)
        if current == self._last_context:
            return
        self._last_context = current
        self._handle_session_region_change(directory, url)
        self._clear_candidate_context()

    def _display_text(self, path: str) -> str:
        widget = self._widgets[path]
        return widget.currentText() if isinstance(widget, QComboBox) else widget.text()

    def _handle_session_region_change(self, directory: str, endpoint: str) -> None:
        region = self._session_candidate_region
        manual_value = self._display_text("openchamber.session_id")
        if region is None:
            # 手填会话：保留 + 标记“会话待核验”，绝不凭目录改变清空
            if manual_value:
                self._session_hint_label.setText("会话待核验")
            return
        if (region.directory, region.endpoint) != (directory, endpoint):
            # 有证据的失配：旧候选目录/端点候选会话失效，可清除（只改控件草稿，不动 baseline）
            session_widget = self._widgets["openchamber.session_id"]
            self._loading = True
            session_widget.setCurrentText("")
            self._loading = False
            self._session_candidate_region = None
            self._session_hint_label.setText("旧目录候选会话已失效，请重新选择")

    # ------------------------------------------------------------ 候选刷新

    def _default_directory_picker(self) -> str | None:
        """生产环境默认：无注入 picker 时使用 QFileDialog。"""
        return QFileDialog.getExistingDirectory(self, "选择工作目录") or None

    def _replace_candidate_items(self, kind: CandidateKind, values) -> None:
        """原子替换某区域的候选列表，同时保留当前控件编辑文本。"""
        combo = self._combo_for_kind(kind)
        current = combo.currentText()
        self._loading = True
        combo.blockSignals(True)
        combo.clear()
        for item in values:
            combo.addItem(item)
        combo.setCurrentText(current)
        combo.blockSignals(False)
        self._loading = False

    def _clear_candidate_context(self, clear_session_hint: bool = False) -> None:
        """上下文变动 / rebbase 时清空所有候选痕迹，保留人工编辑文本。"""
        self._pending_requests.clear()
        for kind in (CandidateKind.SESSION, CandidateKind.AGENT, CandidateKind.MODEL):
            self._replace_candidate_items(kind, ())
        self._candidate_region.clear()
        self._candidate_source.clear()
        for label in self._candidate_source_labels.values():
            label.setText("")
        self._meta_detail.setText("")
        self._session_candidate_region = None
        if clear_session_hint:
            self._session_hint_label.setText("")

    def _on_refresh(self, kind: CandidateKind) -> None:
        url = self._display_text("openchamber.url")
        directory = self._display_text("openchamber.directory")
        project_key = (
            self._project_key_resolver(directory)
            if self._project_key_resolver is not None
            else None
        )
        region = CandidateRegion(
            kind=kind,
            endpoint=url,
            directory=directory,
            project_key=project_key,
        )
        request = CandidateRequest(request_id=self._request_id_factory(), region=region)
        self._pending_requests[kind] = request
        self.candidate_refresh_requested.emit(request)

    def apply_candidate_result(self, result: CandidateResult) -> bool:
        """异步结果 seam：stale 即拒绝；current 则只更新该区域并终结 pending。

        绝不修改 draft（候选不是权威枚举），绝不覆盖人工值。
        """
        pending = self._pending_requests.get(result.region.kind)
        if candidate_result_is_stale(pending, result):
            return False
        self._pending_requests.pop(result.region.kind, None)

        if result.region.kind == CandidateKind.META:
            self._candidate_source[CandidateKind.META] = result.source
            self._candidate_source_labels[CandidateKind.META].setText(
                _meta_source_text(result)
            )
            self._meta_detail.setText(_meta_detail_text(result))
            if result.error:
                self._meta_detail.setText(f"Meta 刷新失败：{result.error}")
            self._recompute_dirty()
            return True

        if result.error:
            # 失败：保留旧候选与旧 candidate_region，仅显示刷新失败
            self._candidate_source_labels[result.region.kind].setText(
                f"刷新失败：{result.error}"
            )
            # 保留原候选与当前草稿
            self._recompute_dirty()
            return True

        self._replace_candidate_items(result.region.kind, result.values)
        self._candidate_region[result.region.kind] = result.region

        if result.region.kind == CandidateKind.SESSION:
            if not result.values:
                self._session_hint_label.setText("会话候选为空，未发现候选")
            manual = self._display_text("openchamber.session_id")
            if manual and result.values and manual not in result.values:
                self._session_hint_label.setText(
                    "当前手填会话未出现在本次候选中，请核对"
                )
        source = result.source or "未知来源"
        self._candidate_source_labels[result.region.kind].setText(f"候选来源：{source}")
        self._candidate_source[result.region.kind] = source
        # Candidate 更新绝不改变 draft / dirty
        self._recompute_dirty()
        return True

    def _combo_for_kind(self, kind: CandidateKind):
        path = {
            CandidateKind.SESSION: "openchamber.session_id",
            CandidateKind.AGENT: "openchamber.agent",
            CandidateKind.MODEL: "openchamber.model",
        }[kind]
        return self._widgets[path]

    # ------------------------------------------------------------ 响应式

    def set_single_column(self, flag: bool) -> None:
        policy = QFormLayout.WrapAllRows if flag else QFormLayout.DontWrapRows
        for form in self._forms:
            form.setRowWrapPolicy(policy)


def _meta_source_text(result: CandidateResult) -> str:
    return f"META 来源：{result.source or '未知来源'}"


def _meta_detail_text(result: CandidateResult) -> str:
    lines = [f"{key}: {value}" for key, value in result.metadata]
    return "\n".join(lines) if lines else ""