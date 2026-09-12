"""T13 角色化 Theme Token 与解析。

单一来源：所有色值/字号/间距都从这里取，控件与 QSS 禁止硬写。
色值与布局基线依据主规格第 13.2 / 13.3 节表格；派生色值（hover/pressed/
disabled/neutral/inset/border.default）在下方注释中标注“派生”，取值与
Fluent 2 中性层级一致，未写入规格的色值不冒充规格原文。

支持 LIGHT / DARK / SYSTEM（跟随系统明暗）。
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from pathlib import Path

QSS_TEMPLATE_PATH = Path(__file__).with_name("theme.qss")


class ThemeMode(str, Enum):
    LIGHT = "light"
    DARK = "dark"
    SYSTEM = "system"


# ---------------------------------------------------------------- 色值


@dataclass(frozen=True)
class _SelectionColor:
    bg: str
    fg: str


@dataclass(frozen=True)
class _ColorRole:
    action_primary_fg: str  # color.action.primary.fg     强调前景（规格表）
    action_primary_surface: str  # color.action.primary.surface 强调浅色衬底（规格表背景）
    action_primary_fill: str  # color.action.primary.fill  实心主按钮填充
    action_primary_hover: str  # 派生：主按钮 hover 填充
    action_primary_pressed: str  # 派生：主按钮 pressed 填充
    action_secondary_fg: str
    action_secondary_ring: str
    action_secondary_hover: str  # 派生：中性按钮 hover 衬底
    action_secondary_pressed: str  # 派生：中性按钮 pressed 衬底
    status_success_fg: str  # color.status.success.fg（规格表）
    status_success_bg: str
    status_recovering_fg: str  # 恢复/琥珀色（规格表）
    status_recovering_bg: str
    status_danger_fg: str  # 危险红（规格表）
    status_danger_bg: str
    status_neutral_fg: str  # 派生：停止后中性灰（规格 13.2 要求“停止后中性灰”）
    status_neutral_bg: str
    selection: _SelectionColor  # color.selection.bg / color.selection.fg


@dataclass(frozen=True)
class _TextRole:
    primary: str  # text.primary （规格表）
    secondary: str  # text.secondary（规格表）
    on_primary: str  # text.on.primary 实心按钮文字（规格 13.2 指定 #FFFFFF / #10253E）
    disabled: str  # 派生：禁用文字，保证对 card ≥3:1 可读


@dataclass(frozen=True)
class _SurfaceRole:
    page: str  # surface.page（规格表）
    card: str  # surface.card（规格表）
    inset: str  # 派生：内凹/hover 衬底


@dataclass(frozen=True)
class _BorderRole:
    subtle: str  # border.subtle（规格表）
    default: str  # 派生：输入框/常规边框，比 subtle 略深
    focus: str  # border.focus 焦点环用色（= action.primary，焦点对比≥3:1）
    danger: str  # border.danger（= status.danger.fg）


# ------------------------------------------------------------ 非色值


@dataclass(frozen=True)
class _FocusRole:
    width: int  # 焦点环厚度，规格 13.3：至少 2px
    color: str


@dataclass(frozen=True)
class _FontSize:
    page: int  # 24
    task: int  # 20
    block: int  # 16
    body: int  # 14
    button: int  # 14
    helper: int  # 12


@dataclass(frozen=True)
class _FontWeight:
    normal: int
    medium: int
    semibold: int


@dataclass(frozen=True)
class _FontRole:
    family: str  # 规格 13.3：Segoe UI / Microsoft YaHei
    mono: str  # 规格 13.3：ID/代码 Consolas
    size: _FontSize
    weight: _FontWeight


@dataclass(frozen=True)
class _PageMargin:
    margin: int  # spacing.page.margin


@dataclass(frozen=True)
class _CardPadding:
    padding: int  # spacing.card.padding


@dataclass(frozen=True)
class _SpacingRole:
    unit: int  # 4px 基础单元（规格 13.3）
    xxs: int
    xs: int
    sm: int
    md: int
    lg: int
    xl: int
    page: _PageMargin
    card: _CardPadding


@dataclass(frozen=True)
class _RadiusRole:
    button: int  # 6；规格 6-8
    control: int  # 6
    card: int  # 10；规格 10-12
    dialog: int  # 12；规格 12-14


@dataclass(frozen=True)
class _ControlSize:
    button: int  # 36（规格 13.3 默认按钮高）
    large: int  # 40（放大）
    touch: int  # 44（触控）
    icon_hit: int  # 32（图标命中区）


@dataclass(frozen=True)
class _SizeRole:
    icon: int  # 16（规格 13.3 图标 16-20）
    control: _ControlSize


@dataclass(frozen=True)
class ThemeTokens:
    color: _ColorRole
    text: _TextRole
    surface: _SurfaceRole
    border: _BorderRole
    focus: _FocusRole
    font: _FontRole
    spacing: _SpacingRole
    radius: _RadiusRole
    size: _SizeRole
    name: str = ""  # 仅用于识别 light/dark，不参与 flatten（见 skip）

    @property
    def mode_name(self) -> str:
        return self.name

    def flatten(self) -> dict[str, str]:
        """展开为 {路径: 值}，路径形如 color.action.primary.fg。"""
        out: dict[str, str] = {}
        _iter_flat("", self, out, skip={"name"})
        return out


def _iter_flat(prefix: str, node, out: dict[str, str], skip: set[str]) -> None:
    for f in fields(node):
        if f.name in skip:
            continue
        value = getattr(node, f.name)
        key = f"{prefix}.{f.name.replace('_', '.')}" if prefix else f.name.replace("_", ".")
        if is_dataclass(value) and not isinstance(value, (str, int)):
            _iter_flat(key, value, out, skip)
        else:
            out[key] = str(value)


# ------------------------------------------------------------------ 三套


def _light() -> ThemeTokens:
    return ThemeTokens(
        name="light",
        color=_ColorRole(
            action_primary_fg="#0F6CBD",
            action_primary_surface="#EAF2FB",
            action_primary_fill="#0F6CBD",
            action_primary_hover="#0A528F",
            action_primary_pressed="#0A4268",
            action_secondary_fg="#172B4D",
            action_secondary_ring="#DCE3EC",
            action_secondary_hover="#E9EEF5",
            action_secondary_pressed="#DDE5EF",
            status_success_fg="#18734B",
            status_success_bg="#EAF6EF",
            status_recovering_fg="#8A5A00",
            status_recovering_bg="#FFF6DE",
            status_danger_fg="#B42318",
            status_danger_bg="#FDEEEB",
            status_neutral_fg="#5F6B7A",
            status_neutral_bg="#EBEFF5",
            selection=_SelectionColor(bg="#0F6CBD", fg="#FFFFFF"),
        ),
        text=_TextRole(
            primary="#172B4D",
            secondary="#52637A",
            on_primary="#FFFFFF",
            disabled="#7E8CA0",
        ),
        surface=_SurfaceRole(page="#F4F6FA", card="#FFFFFF", inset="#EFF3F8"),
        border=_BorderRole(subtle="#DCE3EC", default="#C4CFDD", focus="#0F6CBD", danger="#B42318"),
        focus=_FocusRole(width=2, color="#0F6CBD"),
        font=_FontRole(
            family="Microsoft YaHei UI",
            mono="Consolas",
            size=_FontSize(page=24, task=20, block=16, body=14, button=14, helper=12),
            weight=_FontWeight(normal=400, medium=500, semibold=600),
        ),
        spacing=_SpacingRole(
            unit=4,
            xxs=4,
            xs=8,
            sm=12,
            md=16,
            lg=24,
            xl=32,
            page=_PageMargin(margin=24),
            card=_CardPadding(padding=20),
        ),
        radius=_RadiusRole(button=6, control=6, card=10, dialog=12),
        size=_SizeRole(icon=16, control=_ControlSize(button=36, large=40, touch=44, icon_hit=32)),
    )


def _dark() -> ThemeTokens:
    return ThemeTokens(
        name="dark",
        color=_ColorRole(
            action_primary_fg="#78B6FF",
            action_primary_surface="#193652",
            action_primary_fill="#193652",
            action_primary_hover="#245A80",
            action_primary_pressed="#19405A",
            action_secondary_fg="#EDF2F9",
            action_secondary_ring="#394B60",
            action_secondary_hover="#232E3D",
            action_secondary_pressed="#1B2735",
            status_success_fg="#70DDAF",
            status_success_bg="#163D31",
            status_recovering_fg="#F5CE83",
            status_recovering_bg="#43351D",
            status_danger_fg="#FFA9A1",
            status_danger_bg="#482A2C",
            status_neutral_fg="#AAB7C8",
            status_neutral_bg="#2A3542",
            selection=_SelectionColor(bg="#78B6FF", fg="#10253E"),
        ),
        text=_TextRole(
            primary="#EDF2F9",
            secondary="#B9C6D6",
            on_primary="#10253E",
            disabled="#6B7A8F",
        ),
        surface=_SurfaceRole(page="#111820", card="#1B2430", inset="#141D28"),
        border=_BorderRole(subtle="#394B60", default="#4A5C74", focus="#78B6FF", danger="#FFA9A1"),
        focus=_FocusRole(width=2, color="#78B6FF"),
        font=_FontRole(
            family="Microsoft YaHei UI",
            mono="Consolas",
            size=_FontSize(page=24, task=20, block=16, body=14, button=14, helper=12),
            weight=_FontWeight(normal=400, medium=500, semibold=600),
        ),
        spacing=_SpacingRole(
            unit=4,
            xxs=4,
            xs=8,
            sm=12,
            md=16,
            lg=24,
            xl=32,
            page=_PageMargin(margin=24),
            card=_CardPadding(padding=20),
        ),
        radius=_RadiusRole(button=6, control=6, card=10, dialog=12),
        size=_SizeRole(icon=16, control=_ControlSize(button=36, large=40, touch=44, icon_hit=32)),
    )


LIGHT_THEME: ThemeTokens = _light()
DARK_THEME: ThemeTokens = _dark()


# ------------------------------------------------------------- 系统解析


def detect_system_dark() -> bool:
    """Windows 跟随系统：读取 Personalize 键下 AppsUseLightTheme（0=深色）。

    读取失败（注册表缺失/非 Windows）时回退浅色；可被测试注入覆盖。
    """
    try:
        import winreg  # noqa: PLC0415 (仅 Windows 平台路径)

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return value == 0
    except (OSError, ValueError):
        return False


def resolve_theme_mode(mode: ThemeMode | str, system_is_dark: bool | None = None) -> ThemeMode:
    """把 LIGHT/DARK/SYSTEM 解析为实际 LIGHT 或 DARK。"""
    enum_mode = mode if isinstance(mode, ThemeMode) else ThemeMode(str(mode))
    if enum_mode == ThemeMode.SYSTEM:
        dark = detect_system_dark() if system_is_dark is None else bool(system_is_dark)
        return ThemeMode.DARK if dark else ThemeMode.LIGHT
    return enum_mode


def theme_for_mode(mode: ThemeMode | str, system_is_dark: bool | None = None) -> ThemeTokens:
    actual = resolve_theme_mode(mode, system_is_dark=system_is_dark)
    return DARK_THEME if actual == ThemeMode.DARK else LIGHT_THEME


# ------------------------------------------------------------- QSS 渲染


def render_qss(theme: ThemeTokens | None = None, template: str | None = None) -> str:
    """把 @路径@ 占位符替换为主题 token 值。

    模板中残留占位符视为错误显式抛出。theme.qss 是集中模板，控件不写散落样式。
    """
    theme = theme or LIGHT_THEME
    if template is None:
        template = QSS_TEMPLATE_PATH.read_text(encoding="utf-8")
    flat = theme.flatten()
    rendered = template
    for path, value in flat.items():
        rendered = rendered.replace(f"@{path}@", str(value))
    leftover = _LEFT_PLACEHOLDER.findall(rendered)
    if leftover:
        raise ValueError(f"主题模板存在未替换占位符: {sorted(set(leftover))}")
    return rendered


import re  # noqa: E402

_LEFT_PLACEHOLDER = re.compile(r"@[a-zA-Z0-9_.]+@")


def apply_theme(app, theme: ThemeTokens, template: str | None = None) -> str:
    """应用集中样式表到 QApplication。返回渲染后的 QSS（供测试断言）。"""
    qss = render_qss(theme, template=template)
    app.setStyleSheet(qss)
    return qss


# ------------------------------------------------------------- 对比度


def _channel_luminance(c: int) -> float:
    s = c / 255.0
    return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_color: str) -> float:
    """WCAG 相对亮度 (0,1)。hex 形如 #RRGGBB。"""
    value = hex_color.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"非法颜色值: {hex_color!r}")
    r = int(value[0:2], 16)
    g = int(value[2:4], 16)
    b = int(value[4:6], 16)
    return 0.2126 * _channel_luminance(r) + 0.7152 * _channel_luminance(g) + 0.0722 * _channel_luminance(b)


def contrast_ratio(fg: str, bg: str) -> float:
    """WCAG 对比度 (1, 21]，fg 为前景。"""
    l1 = relative_luminance(fg)
    l2 = relative_luminance(bg)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


# ------------------------------------------------------------- 交互辅助


def refresh_style(widget) -> None:
    """动态属性变化后安全重刷样式（unpolish/polish），不重复 setStyleSheet。"""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)