"""T21-03：OpenChamber 真实写合同（contracts/openchamber_write_contract.md）结构一致性测试。

只读断言合同文档文本；不访问网络；不依赖 .recovery 存在。
覆盖 T21-03 卡 §17 全部清单项 + 附加 hygiene 断言：
- 能力等级：create / prompt_async / message attribution read 均须 SUPPORTED_REAL；
- 精确端点、HTTP 200/204、204 != completed 不变式；
- ses_（OBSERVED_REAL）与 sess_（documented）区分；
- 嵌套 info schema / parentID attribution 不变式 / ATTRIBUTION_AMBIGUOUS /
  禁止 last message 启发；
- UNKNOWN = possibly delivered / never blind resend / UNKNOWN_CREATE・UNKNOWN_SEND no resend；
- durable intent before POST / T21 journal != T22 production ledger；
- 七状态独立；classic /message 与 v2 /prompt 保持非 real supported（DOCUMENTED_ONLY）；
- compact 保持 deferred 到 G4；
- hygiene：无真实 id（ses_/msg_ 后 alnum 长度 <= 7）、无 Bearer/Authorization 实值、
  无一次性 slug、无真实 run timestamp / run id / probe 路径。
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_CONTRACT_PATH = _REPO / "contracts" / "openchamber_write_contract.md"

_SEVEN_STATES = [
    "service_reachable",
    "api_authenticated",
    "capability_available",
    "session_exists",
    "session_attribution_valid",
    "execute_accepted",
    "execution_progressing",
]


def _text() -> str:
    assert _CONTRACT_PATH.is_file(), f"contract 不存在: {_CONTRACT_PATH}"
    return _CONTRACT_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------- 基础存在性


def test_contract_exists():
    assert _CONTRACT_PATH.is_file()


def test_contract_has_content():
    text = _text()
    assert len(text) > 2000


# ---------------------------------------------------------------------- 能力等级冻结


def test_session_create_marked_supported_real():
    assert "| session create | **SUPPORTED_REAL**" in _text()


def test_prompt_async_marked_supported_real():
    assert "| prompt_async send | **SUPPORTED_REAL**" in _text()


def test_message_attribution_read_marked_supported_real():
    assert "| message list/read（用于 attribution） | **SUPPORTED_REAL**" in _text()


def test_capability_levels_defined():
    text = _text()
    for level in ("SUPPORTED_REAL", "OBSERVED_READONLY", "DOCUMENTED_ONLY", "UNVERIFIED"):
        assert level in text, level


def test_route_presence_does_not_upgrade_level():
    text = _text()
    assert "静态源码存在 route 不得升级为 SUPPORTED_REAL" in text


# ---------------------------------------------------------------------- create 实证


def test_exact_create_endpoint_present():
    text = _text()
    assert "| Method | POST |" in text
    assert "| Path | `/api/session` |" in text


def test_create_directory_binding_present():
    assert "?directory=<probe_directory>" in _text()


def test_create_request_body_keys_only_actual():
    assert "`title`" in _text()


def test_create_http_200_evidence_present():
    assert "| Observed HTTP | 200 |" in _text()


def test_create_result_exactly_one_disposable_session():
    assert "exactly one disposable session" in _text()


def test_create_response_unknown_fields_stay_unverified():
    text = _text()
    assert "`id`" in text
    assert "其余 create 返回字段本轮未用于判定" in text


# ---------------------------------------------------------------------- session id 事实


def test_observed_prefix_ses_present():
    text = _text()
    assert "`ses_`" in text
    assert "OBSERVED_REAL" in text


def test_documented_prefix_sess_distinction_present():
    text = _text()
    assert "`sess_`" in text
    assert "compatibility / documented clue" in text


def test_prefix_not_identity_basis():
    text = _text()
    assert "session identity 不得仅靠 prefix 判定" in text
    assert "不得把 `sess_` 当作唯一真实前缀" in text


def test_unknown_create_reconciliation_decisive_conditions():
    text = _text()
    for token in ("unique frozen directory", "恰好 exactly one session", "非空合法 id"):
        assert token in text, token
    assert "GET session list" in text


# ---------------------------------------------------------------------- prompt_async 实证


def test_exact_prompt_async_endpoint_present():
    assert "`/api/session/{session_id}/prompt_async`" in _text()


def test_prompt_async_body_keys_only_actual():
    text = _text()
    for key in ("`messageID`", "`model`", "`agent`", "`variant`", "`parts`"):
        assert key in text, key
    assert "messageID 位置" in text


def test_prompt_async_model_agent_actually_sent():
    text = _text()
    assert "**实际发送**" in text
    assert "`{providerID, modelID}`" in text
    assert "`[{\"type\": \"text\", \"text\": \"<synthetic prompt>\"}]`" in text


def test_prompt_async_delivery_not_sent():
    text = _text()
    assert "未发送" in text
    assert "本轮不发送" in text


def test_prompt_async_http_204_evidence_present():
    assert "| Observed HTTP | 204 |" in _text()


def test_204_is_accepted_not_completed():
    text = _text()
    assert "204 = request accepted" in text
    assert "204 != completed" in text


def test_204_has_no_attributable_completion_result():
    assert "没有 attributable completion result" in _text()


def test_production_state_distinctions_present():
    text = _text()
    for state in ("execute_accepted", "execution_progressing", "result_observed",
                  "session_attribution_valid", "completed"):
        assert state in text, state


def test_post_success_not_task_completed_forbidden():
    assert "“POST 成功 → task completed”" in _text()


# ---------------------------------------------------------------------- message read schema


def test_message_read_endpoint_present():
    assert "`GET /api/session/{session_id}/message?directory=<probe_directory>`" in _text()


def test_nested_info_schema_present():
    text = _text()
    for field in ("`info.id`", "`info.role`", "`info.parentID`", "`parts`"):
        assert field in text, field


def test_info_shape_expression_present():
    assert "{info: {id, role, parentID, ...}, parts: [...]}" in _text()


def test_client_message_id_persistence_observed():
    assert "client supplied probe messageID == observed user message `info.id`" in _text()


def test_message_fields_not_mandatory_if_unused():
    assert "未用于 evidence 判定的其他字段不列为 mandatory" in _text()


# ---------------------------------------------------------------------- attribution


def test_attribution_four_conditions_present():
    text = _text()
    assert "| A |" in text
    assert "| B |" in text
    assert "| C |" in text
    assert "| D |" in text


def test_explicit_parent_relation_invariant_present():
    assert "assistant.info.parentID == user.info.id" in _text()


def test_attribution_ambiguous_present():
    assert "ATTRIBUTION_AMBIGUOUS" in _text()


def test_last_message_heuristic_explicitly_forbidden():
    text = _text()
    assert "**禁止**" in text
    assert "last assistant message" in text


def test_all_forbidden_heuristics_listed():
    text = _text()
    for heuristic in ("last assistant message", "newest message", "nearest timestamp",
                      "content looks similar", "only one assistant message"):
        assert heuristic in text, heuristic


def test_content_without_parent_relation_not_complete():
    text = _text()
    assert "无显式 parent/causal 关系" in text
    assert "不允许判 COMPLETE" in text


# ---------------------------------------------------------------------- UNKNOWN 写语义


def test_unknown_equals_possibly_delivered():
    assert "UNKNOWN = possibly delivered" in _text()


def test_never_blind_resend_present():
    assert "never blind resend" in _text()


def test_unknown_create_no_resend_present():
    text = _text()
    assert "UNKNOWN_CREATE" in text
    assert "禁止第二次 create POST" in text
    assert "只读（read only）reconciliation" in text


def test_unknown_send_no_resend_present():
    text = _text()
    assert "UNKNOWN_SEND" in text
    assert "禁止第二次 prompt POST" in text


def test_unknown_recovery_test_only_not_timeout_proof():
    text = _text()
    assert "尚未真实制造一次网络超时" in text
    assert "safety contract" in text
    assert "不是一次真实 timeout 事故证明" in text


def test_real_prefix_mismatch_incident_noted():
    text = _text()
    assert "真实 incident 佐证" in text
    assert "本地期待 `sess_`，实测 `ses_`" in text


# ---------------------------------------------------------------------- durable intent


def test_durable_intent_before_post_present():
    assert "durable attempt record must exist before POST" in _text()


def test_t21_journal_ne_t22_ledger():
    text = _text()
    assert "T21 probe journal != production T22 Operation Ledger" in text
    assert "T22+" in text


def test_operation_ledger_deferred():
    text = _text()
    assert "Operation Ledger" in text
    assert "idempotency-UNKNOWN authority" in text
    assert "留给 T22+ 裁决" in text


# ---------------------------------------------------------------------- 七状态


def test_seven_states_all_present():
    text = _text()
    for state in _SEVEN_STATES:
        assert state in text, state


def test_states_kept_independent_no_merging():
    text = _text()
    assert "禁止合成/合并" in text
    assert "connected/healthy/ready" in text


def test_execute_accepted_not_imply_attribution_valid():
    assert "`execute_accepted=true` 不 imply `session_attribution_valid=true`" in _text()


# ---------------------------------------------------------------------- deferred


def test_classic_message_not_real_supported():
    text = _text()
    assert "classic `POST /api/session/{session_id}/message`" in text
    assert "**DOCUMENTED_ONLY**" in text


def test_v2_prompt_not_real_supported():
    text = _text()
    assert "v2 `POST /api/session/{session_id}/prompt`" in text
    assert "**DOCUMENTED_ONLY**" in text


def test_compact_deferred_to_g4():
    text = _text()
    assert "G4 remains authority for POST_RESPONSE_COMPACT" in text
    assert "未调用 compact" in text
    assert "不替 G4 提前选路径" in text


def test_deferred_capabilities_listed():
    text = _text()
    for token in ("delivery steer / queue 语义", "interrupt / stop",
                  "permission approve / reject", "auto accept write",
                  "delete / archive", "compact final control path"):
        assert token in text, token


# ---------------------------------------------------------------------- provenance table


def test_provenance_table_classes_present():
    text = _text()
    for cls in ("REAL_SIDE_EFFECT", "REAL_READBACK", "STATIC_SOURCE", "AUTOMATED_SAFETY_TEST"):
        assert cls in text, cls


def test_provenance_four_sources_commits():
    text = _text()
    assert "7142615e0792f2627bd28c49e955876856db0bdc" in text    # T21-01
    assert "ee898485f19eade40cf7166bc174d44d441f4b94" in text    # T21-01F
    assert "909c8d7d64901055050dcdfb690ccfc0445a8175" in text    # T21-02
    assert ".recovery/t21_write_smoke_state.json" in text        # private evidence


def test_private_evidence_never_in_vcs_stated():
    text = _text()
    assert "不纳入版本库" in text


def test_historical_contract_not_rewritten():
    text = _text()
    assert "不回写、不修改历史只读合同" in text


# ---------------------------------------------------------------------- hygiene


def test_no_bearer_token_value():
    assert not re.search(r"Bearer\s+\S", _text())


def test_no_authorization_real_value():
    assert not re.search(r"Authorization\s*[:=]\s*\S", _text())


def test_no_long_real_looking_ids():
    text = _text()
    for m in re.finditer(r"(?i)\b(?:ses|sess|msg)_([A-Za-z0-9]+)", text):
        tail = m.group(1)
        assert len(tail) <= 7, f"疑似真实 id 尾部过长: {m.group(0)!r}"


def test_no_disposable_slug():
    assert not re.search(r"[a-z]{2,}-[a-z]{2,}", _text())


def test_no_real_run_timestamp():
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", _text())


def test_no_real_run_id():
    assert not re.search(r"t21_\d{8}T\d{6}Z", _text())


def test_no_probe_directory_run_id_path():
    assert not re.search(r"t21_write_smoke\\[^\\]+", _text())


def test_placeholders_only_allowed_shapes():
    text = _text()
    # 合同中出现的 ses_/msg_ 示例只允许合成占位形式
    for m in re.finditer(r"(?i)\b(?:ses|sess|msg)_[A-Za-z0-9]*", text):
        token = m.group(0)
        assert token in ("ses_", "sess_", "msg_", "ses_<synthetic>", "msg_<synthetic>"), token