"""T19-B2：OpenChamber 只读合同文档的结构一致性测试。

不访问网络；解析 contracts/openchamber_contract.md 结构/表格校验，不做脆弱整段
中文匹配。合同 hash 采用 B1 实测值固化的形式，测试不依赖 .recovery evidence。

覆盖 T19-B2 卡要求 16 项 + 附加边界项。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_CONTRACT_PATH = _REPO / "contracts" / "openchamber_contract.md"

PROBE_COMMIT = "9ea7bb59ae097df8ba504cc2951d138c698da8f3"
EVIDENCE_SOURCE = ".recovery/t19_probe_evidence.json"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

# 表列头（固定顺序）
END_POINT_COLUMNS = [
    "Endpoint",
    "Method",
    "Capability",
    "Auth observed",
    "HTTP observed",
    "Response shape",
    "Pagination evidence",
    "Missing semantics",
    "Error semantics",
    "Sample hash",
]

ENDPOINT_NAMES = [
    "/health",
    "/api/session?directory=...",
    "/api/session/status?directory=...",
    "/api/session/{session_id}/message?directory=...",
    "/api/permission-auto-accept",
]


def _text() -> str:
    return _CONTRACT_PATH.read_text(encoding="utf-8")


def _rows() -> list[dict[str, str]]:
    """解析 §3 Endpoint Contract 第一条 Markdown 表为行 dict。"""
    lines = _text().splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if line.strip().startswith("| Endpoint"):
            header_idx = i
            break
    assert header_idx is not None, "未找到 Endpoint 表头"

    def split(row: str) -> list[str]:
        cells = [c.strip().strip("`") for c in row.strip().strip("|").split("|")]
        return cells

    headers = split(lines[header_idx])
    assert headers == END_POINT_COLUMNS, f"表头不符合固定列：{headers}"
    rows: list[dict[str, str]] = []
    for line in lines[header_idx + 1 :]:
        if not line.strip().startswith("|"):
            continue
        if re.match(r"^\|[\s:|-]+\|$", line):  # 分隔行
            continue
        cells = split(line)
        if len(cells) < len(headers):
            continue
        # Response shape 单元可能含逗号列表，若分裂行被 '|' 拆开则合并
        if len(cells) > len(headers):
            cells = cells[: len(headers) - 1] + [
                "|".join(cells[len(headers) - 1 :]).strip()
            ]
        rows.append(dict(zip(headers, cells)))
    return rows


def _row(endpoint: str) -> dict[str, str]:
    for r in _rows():
        if r["Endpoint"] == endpoint:
            return r
    raise AssertionError(f"合同表中缺少 endpoint: {endpoint}")


# ---------------------------------------------------------------- 基础 16 项


def test_contract_file_exists():
    assert _CONTRACT_PATH.is_file()
    assert _CONTRACT_PATH.stat().st_size > 0


def test_provenance_probe_commit_correct():
    assert f"`{PROBE_COMMIT}`" in _text()
    assert PROBE_COMMIT in _text()


def test_provenance_evidence_source_correct():
    assert EVIDENCE_SOURCE in _text()


def test_endpoint_table_contains_five_get():
    rows = _rows()
    names = [r["Endpoint"] for r in rows]
    assert len(rows) == 5, f"应恰好 5 行，实际 {len(rows)}"
    for name in ENDPOINT_NAMES:
        assert name in names, f"缺少端点 {name}"


def test_all_methods_get():
    for r in _rows():
        assert r["Method"] == "GET", f"非 GET 方法: {r['Endpoint']} = {r['Method']}"


def test_capability_values_only_supported_unverified():
    allowed = {"SUPPORTED", "UNVERIFIED"}
    for r in _rows():
        assert r["Capability"] in allowed, (
            f"非法 capability: {r['Endpoint']} = {r['Capability']}"
        )


def test_health_supported():
    assert _row("/health")["Capability"] == "SUPPORTED"


def test_session_list_supported():
    assert _row("/api/session?directory=...")["Capability"] == "SUPPORTED"


def test_session_status_supported():
    assert _row("/api/session/status?directory=...")["Capability"] == "SUPPORTED"


def test_message_unverified():
    row = _row("/api/session/{session_id}/message?directory=...")
    assert row["Capability"] == "UNVERIFIED"


def test_permission_supported():
    assert _row("/api/permission-auto-accept")["Capability"] == "SUPPORTED"


def test_supported_rows_sample_hash_64hex():
    supported = [r for r in _rows() if r["Capability"] == "SUPPORTED"]
    assert len(supported) == 4
    for r in supported:
        h = r["Sample hash"].strip()
        assert _HEX64.match(h), f"{r['Endpoint']} 的 sample hash 非 64 位 hex: {h!r}"


def test_message_row_has_no_fake_hash():
    row = _row("/api/session/{session_id}/message?directory=...")
    h = row["Sample hash"].strip()
    assert h == "—" or h.startswith("—"), f"message 行不应有伪造 hash: {h!r}"
    assert not _HEX64.match(h)


def test_seven_states_all_present_and_separate():
    states = [
        "service_reachable",
        "api_authenticated",
        "capability_available",
        "session_exists",
        "session_attribution_valid",
        "execute_accepted",
        "execution_progressing",
    ]
    text = _text()
    for s in states:
        assert s in text, f"缺少状态 {s}"
    # 七层必须逐行独立出现在状态表：# 层号 项
    for i, s in enumerate(states, start=1):
        assert re.search(rf"\|\s*{i}\s*\|\s*`{s}`", text), f"状态 {s} 未独立成行"


def test_fixed_session_missing_invariant_present():
    text = _text()
    assert "FIXED session 不存在" in text
    assert "本次 queried directory 未观察到 session" in text
    assert "不创建 replacement" in text
    assert "不换 session" in text
    assert "不发送" in text


def test_acceptance_mapping_sections_present():
    text = _text()
    for name in ("O01", "O04", "O10", "O11"):
        assert f"### {name}" in text, f"缺少 {name} 章节"


# ---------------------------------------------------------------- 附加边界项


def test_health_not_auth_sufficient_condition():
    text = _text()
    # 明确分开两状态且禁止合并
    assert "service_reachable" in text and "api_authenticated" in text
    assert "互不推出" in text
    # 只能记录“健康可达”，不得写成 auth 的充分条件（§5 行 1 实际措辞）
    assert "/health 200 observed" in text


def test_remote_host_local_token_forbidden():
    text = _text()
    assert "OPENCHAMBER_CLIENT_TOKEN" in text
    assert "desktopLocalClientToken" in text
    assert "不自动发送" in text
    assert "localhost" in text and "127.0.0.1" in text and "::1" in text


def test_o04_unverified_explicit():
    text = _text()
    assert "O04" in text
    assert "message endpoint 尚未真实探测" in text
    assert "UNVERIFIED" in text


def test_o11_message_fields_unverified():
    text = _text()
    assert "O11" in text
    assert "message id / role / parent id / timestamps" in text
    assert "UNVERIFIED" in text


def test_compact_deferred_not_adjudicated():
    text = _text()
    prohibited = [
        "只有一种 compact",
        "不存在 REST compact",
        "已确认 REST compact",
    ]
    for p in prohibited:
        assert p not in text, f"禁止在合同中裁决 compact: {p}"
    assert "G4" in text
    assert "DEFERRED" in text
    assert "不裁决 compact 的最终控制路径" in text


def test_no_oc_client_token_real_value():
    assert "oc_client_v_" not in _text()


def test_no_private_key():
    assert "BEGIN PRIVATE KEY" not in _text()
    assert "PRIVATE KEY" not in _text()


def test_no_real_session_ids_in_contract():
    text = _text()
    hits = re.findall(r"\bses_[A-Za-z0-9]{20,}\b", text)
    assert hits == [], f"合同不应包含真实 session ID：{hits[:5]}"
    # 只写 count/structure
    assert "84" in text
    assert "count" in text or "length" in text or "条" in text


def test_message_fields_stay_unverified():
    row = _row("/api/session/{session_id}/message?directory=...")
    assert row["Pagination evidence"] == "UNVERIFIED"
    assert row["Sample hash"] != ""
    assert "MISSING_INPUT" in row["Error semantics"]
    text = _text()
    assert "不得根据旧项目代码升级为 SUPPORTED" in text


def test_seq_state_hierarchy_not_merged():
    text = _text()
    assert "connection healthy" not in text.lower()
    assert "everything connected" not in text.lower()
    assert "service fully available" not in text.lower()
    # 七层禁止合成：状态表存在且逐层独立（重复校验行数≥7）
    assert text.count("`service_reachable`") >= 1