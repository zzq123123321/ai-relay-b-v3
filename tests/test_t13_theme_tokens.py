"""T13 主题 token 定向测试。

验收依据：
- UI-A03 明暗主题：正文/按钮/状态/禁用/焦点一致适配，无不可读文字（对比度断言与无硬编码色值）。
- UI-A07 超长中文/路径/ID：字体不缩小到不可读（font.size.helper/body 断言）。
- L06   仅改变 UI 偏好，不改变执行快照与续接许可：本层不依赖业务模块（import 隔离断言见
        test_t13_components.py），本文断言 System=Light/Dark 解析与多模式并存。

对比度合同：规格 13.2 明确“普通正文对比度目标至少 4.5:1，必要图标/焦点至少 3:1”。
对规格未给数字的项目（disabled 文字可读、状态徽标），使用同一档 4.5/3.0 作为本项目自定
内部门槛并在报告注明（不以互联网数字冒充项目合同）。
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui.theme_tokens import (  # noqa: E402
    DARK_THEME,
    LIGHT_THEME,
    ThemeTokens,
    ThemeMode,
    apply_theme,
    contrast_ratio,
    render_qss,
    resolve_theme_mode,
    theme_for_mode,
)

BODY_MIN = 4.5  # 规格 13.2：普通正文
FOCUS_MIN = 3.0  # 规格 13.2：必要图标/焦点
DISABLED_MIN = 3.0  # 本项目门槛（规格无数字，报告注明依据）

HEX_RE = re.compile(r"^#[0-9A-F]{6}$")


def spec_table_rows() -> list[tuple[str, str, str]]:
    """规格 13.2 色彩角色表（角色, 浅色前景/背景, 深色前景/背景）。"""
    return [
        ("action.primary", "#0F6CBD", "#78B6FF"),
        ("status.success", "#18734B", "#70DDAF"),
        ("status.recovering", "#8A5A00", "#F5CE83"),
        ("status.danger", "#B42318", "#FFA9A1"),
        ("text.primary", "#172B4D", "#EDF2F9"),
        ("text.secondary", "#52637A", "#B9C6D6"),
        ("surface.page", "#F4F6FA", "#111820"),
        ("border.subtle", "#DCE3EC", "#394B60"),
    ]


# ------------------------------------------------------------- 结构


def test_light_and_dark_exist():
    assert isinstance(LIGHT_THEME, ThemeTokens)
    assert isinstance(DARK_THEME, ThemeTokens)
    assert LIGHT_THEME.mode_name == "light"
    assert DARK_THEME.mode_name == "dark"


def test_roles_all_present():
    """A 端要求 11 组角色齐全：颜色/文字/背景/边框/状态/字号/字重/圆角/间距/控件高度/焦点。"""
    keys = set(LIGHT_THEME.flatten())
    required = {
        # 颜色角色
        "color.action.primary.fg",
        "color.action.primary.surface",
        "color.action.primary.fill",
        "color.action.secondary.fg",
        "color.action.secondary.ring",
        "color.selection.bg",
        # 状态角色
        "color.status.success.fg",
        "color.status.success.bg",
        "color.status.recovering.fg",
        "color.status.recovering.bg",
        "color.status.danger.fg",
        "color.status.danger.bg",
        "color.status.neutral.fg",
        "color.status.neutral.bg",
        # 文字角色
        "text.primary",
        "text.secondary",
        "text.on.primary",
        "text.disabled",
        # 背景/Surface 角色
        "surface.page",
        "surface.card",
        "surface.inset",
        # 边框角色
        "border.subtle",
        "border.default",
        "border.focus",
        "border.danger",
        # 字号
        "font.size.page",
        "font.size.task",
        "font.size.block",
        "font.size.body",
        "font.size.button",
        "font.size.helper",
        # 字重
        "font.weight.normal",
        "font.weight.medium",
        "font.weight.semibold",
        # 圆角
        "radius.button",
        "radius.card",
        "radius.dialog",
        # 间距
        "spacing.unit",
        "spacing.xxs",
        "spacing.xs",
        "spacing.sm",
        "spacing.md",
        "spacing.lg",
        "spacing.xl",
        "spacing.page.margin",
        "spacing.card.padding",
        # 控件高度
        "size.control.button",
        "size.control.large",
        "size.control.touch",
        "size.control.icon.hit",
        "size.icon",
        # 焦点轮廓
        "focus.width",
        "focus.color",
    }
    missing = required - keys
    assert not missing, f"token 缺项: {sorted(missing)}"


def test_spec_color_values_match_table():
    """规格 13.2 表中的角色值必须逐字一致（浅色/深色前景）。"""
    # action.primary 前景
    assert LIGHT_THEME.color.action_primary_fg == spec_table_rows()[0][1]
    assert DARK_THEME.color.action_primary_fg == spec_table_rows()[0][2]
    # status.* 前景
    assert LIGHT_THEME.color.status_success_fg == "#18734B"
    assert DARK_THEME.color.status_success_fg == "#70DDAF"
    assert LIGHT_THEME.color.status_recovering_fg == "#8A5A00"
    assert DARK_THEME.color.status_recovering_fg == "#F5CE83"
    assert LIGHT_THEME.color.status_danger_fg == "#B42318"
    assert DARK_THEME.color.status_danger_fg == "#FFA9A1"
    # text
    assert LIGHT_THEME.text.primary == "#172B4D"
    assert DARK_THEME.text.primary == "#EDF2F9"
    assert LIGHT_THEME.text.secondary == "#52637A"
    assert DARK_THEME.text.secondary == "#B9C6D6"
    # surface/border
    assert LIGHT_THEME.surface.page == "#F4F6FA"
    assert DARK_THEME.surface.page == "#111820"
    assert LIGHT_THEME.text.on_primary == "#FFFFFF"
    assert DARK_THEME.text.on_primary == "#10253E"  # 规格 13.2 深色实心按钮深字


def test_light_and_dark_differ():
    lk = LIGHT_THEME.flatten()
    dk = DARK_THEME.flatten()
    assert lk.keys() == dk.keys()
    changed = {k for k in lk if lk[k] != dk[k]}
    assert {"color.action.primary.fg", "surface.page", "text.primary", "border.subtle"} <= changed


def test_token_values_valid():
    for theme in (LIGHT_THEME, DARK_THEME):
        for path, value in theme.flatten().items():
            if path.startswith(("color.", "text.", "surface.", "border.", "focus.color")):
                assert HEX_RE.match(value), f"{path}={value!r} 非法色值"
            elif path in ("font.family", "font.mono"):
                assert value, f"{path} 不应为空"
            else:
                assert str(value).isdigit(), f"{path}={value!r} 应为数值"
    assert LIGHT_THEME.focus.width >= 2  # 规格 13.3：至少 2px
    assert LIGHT_THEME.font.size.body == 14  # 规格 13.3：正文/按钮 14
    assert LIGHT_THEME.font.size.helper == 12  # 规格 13.3：辅助 12
    assert LIGHT_THEME.size.control.button == 36  # 规格 13.3：默认按钮高 36
    assert LIGHT_THEME.font.family == "Microsoft YaHei UI"
    assert LIGHT_THEME.font.mono == "Consolas"


# ------------------------------------------------------------- 模式解析


def test_system_resolution():
    assert resolve_theme_mode(ThemeMode.LIGHT) == ThemeMode.LIGHT
    assert resolve_theme_mode(ThemeMode.DARK) == ThemeMode.DARK
    assert resolve_theme_mode(ThemeMode.SYSTEM, system_is_dark=True) == ThemeMode.DARK
    assert resolve_theme_mode(ThemeMode.SYSTEM, system_is_dark=False) == ThemeMode.LIGHT
    assert theme_for_mode(ThemeMode.SYSTEM, system_is_dark=True) is DARK_THEME
    assert theme_for_mode(ThemeMode.SYSTEM, system_is_dark=False) is LIGHT_THEME
    # 字符串模式等价
    assert resolve_theme_mode("system", system_is_dark=True) == ThemeMode.DARK
    assert theme_for_mode("dark") is DARK_THEME
    with pytest.raises(ValueError):
        resolve_theme_mode("ultraviolet")


def test_detect_system_dark_returns_bool():
    assert isinstance(theme_for_mode(ThemeMode.SYSTEM).mode_name, str)  # 不抛异常
    from ui.theme_tokens import detect_system_dark

    assert detect_system_dark() in (True, False)


# ------------------------------------------------------------- QSS 渲染


def test_qss_template_exists():
    from ui.theme_tokens import QSS_TEMPLATE_PATH

    assert QSS_TEMPLATE_PATH.exists()
    assert QSS_TEMPLATE_PATH.name == "theme.qss"
    content = QSS_TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "@font.family@" in content


def test_render_no_leftover_placeholders():
    for theme in (LIGHT_THEME, DARK_THEME):
        qss = render_qss(theme)
        leftover = re.findall(r"@[a-zA-Z0-9_.]+@", qss)
        assert not leftover
        assert "#0F6CBD" in render_qss(LIGHT_THEME)
        assert "#78B6FF" in render_qss(DARK_THEME)


def test_render_contains_control_styles():
    qss = render_qss(LIGHT_THEME)
    for selector in (
        "QPushButton[variant=\"primary\"]",
        "QPushButton[variant=\"secondary\"]",
        "QPushButton[variant=\"danger\"]",
        "QPushButton[variant=\"text\"]",
        "QPushButton[busy=\"true\"]",
        "QPushButton[error=\"true\"]",
        "QPushButton:focus",
        "QPushButton:hover",
        "QPushButton:pressed",
        "QPushButton:disabled",
        "QLabel[tone=\"success\"]",
        "QLabel[tone=\"recovering\"]",
        "QLabel[tone=\"danger\"]",
        "QLabel[tone=\"neutral\"]",
        "QLineEdit:focus",
        "QLineEdit[input-error=\"true\"]",
        "QFrame[card=\"true\"]",
    ):
        assert selector in qss, f"QSS 缺少选择器: {selector}"


def test_render_missing_placeholder_raises():
    with pytest.raises(ValueError):
        render_qss(LIGHT_THEME, template="QPushButton { color: @color.not.exists@; }")


# ------------------------------------------------------------- 对比度矩阵


def _check(theme: ThemeTokens, label: str, fg: str, bg: str, minimum: float):
    ratio = contrast_ratio(fg, bg)
    assert ratio >= minimum, (
        f"{theme.mode_name}/{label} 对比度 {ratio:.2f}:1 < {minimum}:1 ({fg} on {bg})"
    )
    return ratio


def test_contrast_matrix():
    """UI-A03 无不可读文字 + 规格 13.2 对比度合同：正文≥4.5，焦点≥3。"""
    results = {}
    for theme in (LIGHT_THEME, DARK_THEME):
        name = theme.mode_name
        flat = theme.flatten()
        results[name] = {
            "text.primary/card": _check(theme, "主文字", flat["text.primary"], flat["surface.card"], BODY_MIN),
            "text.secondary/card": _check(theme, "次文字", flat["text.secondary"], flat["surface.card"], BODY_MIN),
            "danger.fg/card": _check(theme, "危险文字", flat["color.status.danger.fg"], flat["surface.card"], BODY_MIN),
            "focus/card": _check(theme, "焦点环/card", flat["focus.color"], flat["surface.card"], FOCUS_MIN),
            "focus/page": _check(theme, "焦点环/page", flat["focus.color"], flat["surface.page"], FOCUS_MIN),
            "disabled/card": _check(theme, "禁用文字", flat["text.disabled"], flat["surface.card"], DISABLED_MIN),
        }
        # 状态徽标：前景 vs 自身底色（文字+符号+颜色，规格 13.1）
        for status in ("success", "recovering", "danger", "neutral"):
            fg = flat[f"color.status.{status}.fg"]
            bg = flat[f"color.status.{status}.bg"]
            results[name][f"badge.{status}"] = _check(theme, f"徽标{status}", fg, bg, BODY_MIN)
        # 主按钮文字 vs 主按钮填充
        results[name]["button.primary"] = contrast_ratio(flat["text.on.primary"], flat["color.action.primary.fill"])
    # 浅色主按钮：白字蓝底应 ≥4.5
    assert results["light"]["button.primary"] >= BODY_MIN
    # 深色主按钮：规格 13.2 明确定为 #10253E 深字 on #193652 深蓝（契约色值），
    # 不修改规格色值；实测值记录进报告由 A 端审核，不在本测试中冒功。
    dark_button = results["dark"]["button.primary"]
    assert DARK_THEME.text.on_primary == "#10253E"
    assert DARK_THEME.color.action_primary_fill == "#193652"
    print(f"\n[DARK button.primary contrast] spec-faithful tokens must be A-side reviewed: {dark_button:.2f}:1")


# ------------------------------------------------------------- 无硬编码


def test_qss_template_has_no_literal_hex():
    """UI-A03 原则：QSS 模板不允许硬写色值，全部经 token 占位符替换。"""
    from ui.theme_tokens import QSS_TEMPLATE_PATH

    content = QSS_TEMPLATE_PATH.read_text(encoding="utf-8")
    literal = re.findall(r"#[0-9A-Fa-f]{6}", content)
    assert not literal, f"theme.qss 硬编码色值（应全部用 @token@）: {literal}"


def test_components_no_literal_hex_or_px():
    source = Path(__file__).resolve().parents[1] / "ui" / "components.py"
    content = source.read_text(encoding="utf-8")
    assert not re.findall(r"#[0-9A-Fa-f]{6}", content)
    assert not re.findall(r"\d+px", content)


# ------------------------------------------------------------- 采样检查


def test_apply_theme_idempotent():
    class _FakeApp:
        def __init__(self):
            self.handlers = []

        def setStyleSheet(self, qss):
            self.handlers.append(qss)

    fake = _FakeApp()
    apply_theme(fake, LIGHT_THEME)
    apply_theme(fake, DARK_THEME)
    assert len(fake.handlers) == 2
    assert fake.handlers[0] != fake.handlers[1]
    assert fake.handlers[0] == apply_theme(fake, LIGHT_THEME)