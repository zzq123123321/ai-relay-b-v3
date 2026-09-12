"""T06：V1/legacy 协议解析、RESPONSE 封装与规范化内容摘要测试。

覆盖任务卡 T06，直接对应 P01 / P02 / P03 / P05 验收场景：
- P01：合法 V1/legacy 任务与中文多行代码 → 字段正确、原任务关联、正文按合同保留
- P02：缺头/重复头/空 body/无空行/非法 TYPE/TARGET 等 → 明确错误拒绝
- P03：ROUND 负值 / MAX_ROUNDS=0 / 越界 / 非整数 → 拒绝
- P05：同 ID 相同规范内容 digest 一致；执行语义不同（正文/WORKDIR/TARGET/ROUND/扩展头）digest 不同
大小限制按附录A（envelope 4MiB / body 2MiB，UTF-8 字节）验证边界。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.protocol_v1 import (
    DEFAULT_BODY_LIMIT_BYTES,
    DEFAULT_ENVELOPE_LIMIT_BYTES,
    MessageType,
    ProtocolError,
    ProtocolErrorCode,
    ProtocolFormat,
    RelayMessage,
    content_digest,
    parse_message,
    wrap_response,
)

FIXTURES = Path(__file__).parent / "fixtures" / "protocol"

V1_CHINESE_BODY = (
    "请检查当前项目的任务列表显示问题。\n"
    "def 你好():\n"
    '    value = "中文"\n'
    "\n"
    "    if value:\n"
    "        return value"
)

LEGACY_CODE_BODY = (
    "请分析并修复代码。\n"
    "def 你好():\n"
    '    value = "中文"\n'
    "\n"
    "    if value:\n"
    "        return value"
)


def _v1_text(
    body: str,
    *,
    message_id: str = "task-test-001",
    source: str = "CHATGPT",
    target: str = "OPENCHAMBER",
    message_type: str = "TASK",
    extra: str = "",
    crlf: bool = False,
) -> str:
    raw = "\n".join(
        (
            "AI_RELAY/1",
            f"MESSAGE_ID: {message_id}",
            f"SOURCE: {source}",
            f"TARGET: {target}",
            f"TYPE: {message_type}",
            *((extra,) if extra else ()),
            "",
            body,
        )
    )
    return raw.replace("\n", "\r\n") if crlf else raw


def _legacy_text(
    body: str,
    *,
    task_id: str = "task-legacy-001",
    content_inline: str = "",
    surround: bool = False,
) -> str:
    if content_inline:
        content_line = f"CONTENT: {content_inline}"
    else:
        content_line = "CONTENT:"
    lines = [
        "----- AI_RELAY_BEGIN -----",
        "SOURCE: CHATGPT",
        "TARGET: REASONIX",
        "TYPE: TASK",
        f"TASK_ID: {task_id}",
        "ROUND: 0",
        "MAX_ROUNDS: 3",
        content_line,
    ]
    if not content_inline:
        lines.append(body)
    text = "\n".join(lines)
    text += "\n----- AI_RELAY_END -----"
    if surround:
        text = "\n\n" + text + "\n\n"
    return text


class TestP01LegalMessages:
    """P01：V1/legacy 合法任务、中文多行代码、正文往返无损。"""

    def test_v1_fixture_chinese_code_roundtrip(self):
        text = (FIXTURES / "valid" / "v1_task_chinese.txt").read_text(encoding="utf-8")
        msg = parse_message(text)
        assert msg.protocol_format is ProtocolFormat.V1
        assert msg.message_id == "task-chinese-001"
        assert msg.source == "CHATGPT"
        assert msg.target == "OPENCHAMBER"
        assert msg.message_type is MessageType.TASK
        assert msg.round_number == 0
        assert msg.max_rounds == 3
        assert msg.workdir is None
        assert msg.in_reply_to is None
        assert msg.body == V1_CHINESE_BODY

    def test_v1_workdir_preserved(self):
        text = (FIXTURES / "valid" / "v1_task_workdir.txt").read_text(encoding="utf-8")
        msg = parse_message(text)
        assert msg.workdir == r"D:\AIwork\demo"
        assert msg.target == "EXECUTOR"
        assert msg.round_number == 1

    def test_v1_minimal_task_default_rounds(self):
        msg = parse_message(
            "AI_RELAY/1\nMESSAGE_ID: m-1\nSOURCE: CHATGPT\nTARGET: REASONIX\nTYPE: TASK\n\n你好"
        )
        assert msg.round_number == 0
        assert msg.max_rounds == 3
        assert msg.body == "你好"

    def test_v1_full_task_keeps_extension_header(self):
        msg = parse_message(
            "AI_RELAY/1\nMESSAGE_ID: m-2\nSOURCE: CHATGPT\nTARGET: EXECUTOR\nTYPE: TASK\n"
            "ROUND: 2\nMAX_ROUNDS: 2\nWORKDIR: /tmp/x\nX-REF: abc\n\n正文"
        )
        assert ("X-REF", "abc") in msg.extension_headers
        assert msg.workdir == "/tmp/x"
        assert msg.round_number == 2
        assert msg.max_rounds == 2

    def test_v1_headers_case_insensitive_values_normalized(self):
        msg = parse_message(
            "AI_RELAY/1\nmessage_id: m-3\nsource: chatgpt\ntarget: reasonix\ntype: task\n\n正文"
        )
        assert msg.source == "CHATGPT"
        assert msg.target == "REASONIX"
        assert msg.message_type is MessageType.TASK

    def test_v1_crlf_normalized_to_lf_in_body(self):
        text = _v1_text("第一行\n\n第二行", message_id="m-crlf", crlf=True)
        msg = parse_message(text)
        assert msg.body == "第一行\n\n第二行"
        assert "\r" not in msg.body

    def test_v1_body_leading_and_trailing_newline_kept(self):
        raw = "AI_RELAY/1\nMESSAGE_ID: m-4\nSOURCE: CHATGPT\nTARGET: OPENCHAMBER\nTYPE: TASK\n\n\n正文\n"
        msg = parse_message(raw)
        assert msg.body == "\n正文\n"
        assert msg.body.lstrip("\n").startswith("正文")

    def test_v1_tolower_body_whitespace_not_reflowed(self):
        body = "word1  word2\n\tindent\n  spaced\n"
        msg = parse_message(_v1_text(body))
        assert msg.body == body

    def test_v1_response_roundtrip_binds_original_task(self):
        wrapped = wrap_response(
            "任务状态：COMPLETED\n已完成。",
            in_reply_to="task-demo-001",
            protocol_format=ProtocolFormat.V1,
            response_message_id="resp-fixed-001",
        )
        msg = parse_message(wrapped)
        assert msg.message_type is MessageType.RESPONSE
        assert msg.message_id == "resp-fixed-001"
        assert msg.in_reply_to == "task-demo-001"
        assert msg.source == "EXECUTOR"
        assert msg.target == "CHATGPT"
        assert msg.body == "任务状态：COMPLETED\n已完成。"

    def test_legacy_fixture_code_roundtrip(self):
        text = (FIXTURES / "valid" / "legacy_task_code.txt").read_text(encoding="utf-8")
        msg = parse_message(text)
        assert msg.protocol_format is ProtocolFormat.LEGACY_WEB
        assert msg.message_id == "task-legacy-001"
        assert msg.source == "CHATGPT"
        assert msg.target == "REASONIX"
        assert msg.message_type is MessageType.TASK
        assert msg.body == LEGACY_CODE_BODY

    def test_legacy_response_roundtrip_binds_original_task(self):
        wrapped = wrap_response(
            "已完成。",
            in_reply_to="task-legacy-001",
            protocol_format=ProtocolFormat.LEGACY_WEB,
            timestamp="2026-09-12 10:00:00",
        )
        msg = parse_message(wrapped)
        assert msg.message_type is MessageType.RESPONSE
        assert msg.message_id == "task-legacy-001"
        assert msg.source == "EXECUTOR"
        assert msg.target == "CHATGPT"
        assert msg.body == "已完成。"

    @pytest.mark.parametrize(
        ("content_inline", "body", "expected"),
        [
            ("hello", "", "hello"),
            ("", "第一行\n\n第二行", "第一行\n\n第二行"),
        ],
    )
    def test_legacy_content_same_line_and_trailing_blank_rules(self, content_inline, body, expected):
        msg = parse_message(_legacy_text(body, content_inline=content_inline))
        assert msg.body == expected

    def test_legacy_leading_body_blank_lines_dropped(self):
        text = _legacy_text("\n\n正文")
        msg = parse_message(text)
        assert msg.body == "正文"

    def test_legacy_surrounding_blank_lines_tolerated(self):
        msg = parse_message(_legacy_text("正文", surround=True))
        assert msg.body == "正文"

    def test_legacy_trailing_body_blank_lines_stripped_by_contract(self):
        text = (
            "----- AI_RELAY_BEGIN -----\nSOURCE: CHATGPT\nTARGET: REASONIX\nTYPE: TASK\n"
            "TASK_ID: t-1\nCONTENT:\n正文\n\n\n----- AI_RELAY_END -----"
        )
        msg = parse_message(text)
        assert msg.body == "正文"


class TestP02MalformedRejected:
    """P02：缺头/重复头/空body/无空行/非法TYPE/TARGET 等明确拒绝。"""

    def _rejects(self, text, code: ProtocolErrorCode):
        with pytest.raises(ProtocolError) as exc_info:
            parse_message(text)
        assert exc_info.value.code is code

    def test_missing_marker(self):
        self._rejects("MESSAGE_ID: m\nSOURCE: CHATGPT\nTARGET: OPENCHAMBER\nTYPE: TASK\n\n正文",
                      ProtocolErrorCode.MISSING_MARKER)

    def test_plain_text_not_a_message(self):
        self._rejects("今天天气不错", ProtocolErrorCode.MISSING_MARKER)

    def test_missing_separator(self):
        self._rejects("AI_RELAY/1\nMESSAGE_ID: m\nSOURCE: CHATGPT\nTARGET: OPENCHAMBER\nTYPE: TASK\n正文",
                      ProtocolErrorCode.MISSING_SEPARATOR)

    def test_invalid_header_no_colon(self):
        self._rejects("AI_RELAY/1\nMESSAGE_ID: m\nSOURCE CHATGPT\nTARGET: OPENCHAMBER\nTYPE: TASK\n\n正文",
                      ProtocolErrorCode.INVALID_HEADER)

    def test_duplicate_header_fixture(self):
        text = (FIXTURES / "invalid" / "duplicate_header.txt").read_text(encoding="utf-8")
        self._rejects(text, ProtocolErrorCode.DUPLICATE_HEADER)

    def test_missing_required_headers(self):
        body_headers = "AI_RELAY/1\nTARGET: OPENCHAMBER\nTYPE: TASK\n\n正文"
        self._rejects(body_headers, ProtocolErrorCode.MISSING_HEADER)

    def test_unknown_type(self):
        self._rejects("AI_RELAY/1\nMESSAGE_ID: m\nSOURCE: CHATGPT\nTARGET: OPENCHAMBER\nTYPE: POLL\n\n正文",
                      ProtocolErrorCode.UNKNOWN_TYPE)

    def test_empty_body(self):
        self._rejects("AI_RELAY/1\nMESSAGE_ID: m\nSOURCE: CHATGPT\nTARGET: OPENCHAMBER\nTYPE: TASK\n\n",
                      ProtocolErrorCode.EMPTY_BODY)

    def test_whitespace_only_body(self):
        self._rejects("AI_RELAY/1\nMESSAGE_ID: m\nSOURCE: CHATGPT\nTARGET: OPENCHAMBER\nTYPE: TASK\n\n   \n\t",
                      ProtocolErrorCode.EMPTY_BODY)

    def test_unknown_target_for_task(self):
        self._rejects("AI_RELAY/1\nMESSAGE_ID: m\nSOURCE: CHATGPT\nTARGET: QWEN\nTYPE: TASK\n\n正文",
                      ProtocolErrorCode.UNKNOWN_TARGET)

    def test_control_character_in_message_id(self):
        self._rejects("AI_RELAY/1\nMESSAGE_ID: m\x07x\nSOURCE: CHATGPT\nTARGET: OPENCHAMBER\nTYPE: TASK\n\n正文",
                      ProtocolErrorCode.INVALID_ID)

    def test_v1_response_requires_in_reply_to(self):
        self._rejects("AI_RELAY/1\nMESSAGE_ID: r1\nSOURCE: EXECUTOR\nTARGET: CHATGPT\nTYPE: RESPONSE\n\n正文",
                      ProtocolErrorCode.INVALID_MESSAGE)

    def test_legacy_missing_end_marker(self):
        self._rejects("----- AI_RELAY_BEGIN -----\nSOURCE: CHATGPT\nTARGET: REASONIX\nTYPE: TASK\n"
                      "TASK_ID: t-1\nCONTENT:\n正文",
                      ProtocolErrorCode.MISSING_END_MARKER)

    def test_legacy_missing_content_header(self):
        self._rejects("----- AI_RELAY_BEGIN -----\nSOURCE: CHATGPT\nTARGET: REASONIX\nTYPE: TASK\n"
                      "TASK_ID: t-1\n----- AI_RELAY_END -----",
                      ProtocolErrorCode.MISSING_CONTENT_HEADER)

    def test_legacy_unknown_type(self):
        self._rejects("----- AI_RELAY_BEGIN -----\nSOURCE: CHATGPT\nTARGET: REASONIX\nTYPE: STATUS\n"
                      "TASK_ID: t-1\nCONTENT:\n正文\n----- AI_RELAY_END -----",
                      ProtocolErrorCode.UNKNOWN_TYPE)

    def test_legacy_duplicate_header_before_content(self):
        self._rejects("----- AI_RELAY_BEGIN -----\nSOURCE: CHATGPT\nSOURCE: CHATGPT\nTARGET: REASONIX\n"
                      "TYPE: TASK\nTASK_ID: t-1\nCONTENT:\n正文\n----- AI_RELAY_END -----",
                      ProtocolErrorCode.DUPLICATE_HEADER)

    def test_protocol_error_has_stable_code(self):
        with pytest.raises(ProtocolError) as exc_info:
            parse_message("not a message")
        assert exc_info.value.code == ProtocolErrorCode.MISSING_MARKER
        assert exc_info.value.code.value == "missing_marker"


class TestP03RoundValidation:
    """P03：ROUND/MAX_ROUNDS 校验。"""

    def test_round_half_value_accepted(self):
        msg = parse_message(_v1_text("正文", extra="ROUND: 1\nMAX_ROUNDS: 3"))
        assert (msg.round_number, msg.max_rounds) == (1, 3)

    @pytest.mark.parametrize(
        "extra",
        ["ROUND: 2\nMAX_ROUNDS: 2", "ROUND: 0\nMAX_ROUNDS: 1", "MAX_ROUNDS: 1\nROUND: 0"],
    )
    def test_boundary_accepted(self, extra):
        msg = parse_message(_v1_text("正文", extra=extra))
        assert 0 <= msg.round_number <= msg.max_rounds

    def test_negative_round_rejected(self):
        with pytest.raises(ProtocolError) as exc_info:
            parse_message(_v1_text("正文", extra="ROUND: -1\nMAX_ROUNDS: 3"))
        assert exc_info.value.code is ProtocolErrorCode.INVALID_ROUND_RANGE

    def test_max_rounds_zero_rejected(self):
        with pytest.raises(ProtocolError) as exc_info:
            parse_message(_v1_text("正文", extra="MAX_ROUNDS: 0"))
        assert exc_info.value.code is ProtocolErrorCode.INVALID_ROUND_RANGE

    def test_round_over_max_rejected(self):
        with pytest.raises(ProtocolError) as exc_info:
            parse_message(_v1_text("正文", extra="ROUND: 4\nMAX_ROUNDS: 3"))
        assert exc_info.value.code is ProtocolErrorCode.INVALID_ROUND_RANGE

    @pytest.mark.parametrize(
        "extra",
        ["ROUND: 1.5", "ROUND: abc", "MAX_ROUNDS: 3.0", "MAX_ROUNDS: x"],
    )
    def test_non_integer_round_rejected(self, extra):
        with pytest.raises(ProtocolError) as exc_info:
            parse_message(_v1_text("正文", extra=extra))
        assert exc_info.value.code is ProtocolErrorCode.INVALID_ROUND

    def test_round_error_is_not_resume_counter(self):
        # 文档要求 ROUND 只表示 A 端协议轮次，非法 ROUND 一律拒绝，不当作恢复计数
        with pytest.raises(ProtocolError):
            parse_message(_v1_text("正文", extra="ROUND: 5\nMAX_ROUNDS: 3"))


class TestP05ContentDigest:
    """P05：同 ID 内容摘要一致/冲突可区分。T06 只验证 hash，不处理数据库。"""

    def _parse(self, body, **kw) -> RelayMessage:
        return parse_message(_v1_text(body, **kw))

    def test_digest_deterministic_across_parses(self):
        a = self._parse("正文", message_id="task-x-1")
        b = self._parse("正文", message_id="task-x-1")
        assert content_digest(a) == content_digest(b)

    def test_digest_same_across_fresh_construction_and_identity_only_change(self):
        a = self._parse("正文", message_id="task-x-1")
        b = self._parse("正文", message_id="task-x-2")
        assert content_digest(a) == content_digest(b)

    def test_crlf_and_lf_same_digest(self):
        a = self._parse("第一行\n\n第二行", message_id="same-1")
        b = self._parse("第一行\n\n第二行", message_id="same-2", crlf=True)
        assert content_digest(a) == content_digest(b)

    def test_different_body_different_digest(self):
        a = self._parse("修复甲", message_id="task-x-1")
        b = self._parse("修复乙", message_id="task-x-1")
        assert content_digest(a) != content_digest(b)

    def test_one_extra_character_different_digest(self):
        a = self._parse("return value", message_id="task-x-1")
        b = self._parse("return values", message_id="task-x-1")
        assert content_digest(a) != content_digest(b)

    def test_different_workdir_different_digest(self):
        a = self._parse("正文", message_id="task-w-1", extra="WORKDIR: D:\\AIwork\\a")
        b = self._parse("正文", message_id="task-w-1", extra="WORKDIR: D:\\AIwork\\b")
        assert content_digest(a) != content_digest(b)

    def test_absent_vs_present_workdir_different_digest(self):
        a = self._parse("正文", message_id="task-w-1")
        b = self._parse("正文", message_id="task-w-1", extra="WORKDIR: D:\\AIwork\\a")
        assert content_digest(a) != content_digest(b)

    def test_different_target_different_digest(self):
        a = self._parse("正文", message_id="task-t-1", target="OPENCHAMBER")
        b = self._parse("正文", message_id="task-t-1", target="REASONIX")
        assert content_digest(a) != content_digest(b)

    def test_different_round_different_digest(self):
        a = self._parse("正文", message_id="task-r-1", extra="ROUND: 0\nMAX_ROUNDS: 3")
        b = self._parse("正文", message_id="task-r-1", extra="ROUND: 1\nMAX_ROUNDS: 3")
        assert content_digest(a) != content_digest(b)

    def test_different_extension_header_different_digest(self):
        a = self._parse("正文", message_id="task-e-1", extra="X-REF: aaa")
        b = self._parse("正文", message_id="task-e-1", extra="X-REF: bbb")
        assert content_digest(a) != content_digest(b)

    def test_extension_header_order_independent_digest(self):
        left = "AI_RELAY/1\nMESSAGE_ID: task-e-1\nSOURCE: CHATGPT\nTARGET: OPENCHAMBER\nTYPE: TASK\n" \
               "A-ONE: 1\nB-TWO: 2\n\n正文"
        right = "AI_RELAY/1\nMESSAGE_ID: task-e-1\nSOURCE: CHATGPT\nTARGET: OPENCHAMBER\nTYPE: TASK\n" \
                "B-TWO: 2\nA-ONE: 1\n\n正文"
        assert content_digest(parse_message(left)) == content_digest(parse_message(right))

    def test_legacy_and_v1_same_execution_semantics_same_digest(self):
        v1 = parse_message(_v1_text("正文", message_id="cross-1", target="REASONIX"))
        legacy = parse_message(
            "----- AI_RELAY_BEGIN -----\nSOURCE: CHATGPT\nTARGET: REASONIX\nTYPE: TASK\n"
            "TASK_ID: cross-1\nROUND: 0\nMAX_ROUNDS: 3\nCONTENT:\n正文\n----- AI_RELAY_END -----"
        )
        assert v1.target == legacy.target and v1.body == legacy.body
        assert content_digest(v1) == content_digest(legacy)

    def test_legacy_different_body_different_digest(self):
        a = parse_message(_legacy_text("修复甲", task_id="task-legacy-z"))
        b = parse_message(_legacy_text("修复乙", task_id="task-legacy-z"))
        assert content_digest(a) != content_digest(b)


class TestSizeLimits:
    """附录A：envelope 4MiB / body 2MiB，按 UTF-8 字节判定。"""

    def test_defaults_match_appendix_a(self):
        assert DEFAULT_ENVELOPE_LIMIT_BYTES == 4 * 1024 * 1024
        assert DEFAULT_BODY_LIMIT_BYTES == 2 * 1024 * 1024

    def _packet(self, body: str) -> str:
        return _v1_text(body)

    def test_envelope_at_limit_accepted(self):
        small_body = "ab"
        packet = self._packet(small_body)
        limit = len(packet.encode("utf-8"))
        assert parse_message(packet, envelope_limit_bytes=limit).body == small_body

    def test_envelope_over_limit_rejected(self):
        packet = self._packet("ab")
        limit = len(packet.encode("utf-8")) - 1
        with pytest.raises(ProtocolError) as exc_info:
            parse_message(packet, envelope_limit_bytes=limit)
        assert exc_info.value.code is ProtocolErrorCode.ENVELOPE_TOO_LARGE

    def test_body_at_limit_accepted_chinese_utf8_bytes(self):
        body = "啊" * 4
        packet = self._packet(body)
        body_limit = len(body.encode("utf-8"))
        assert body_limit == 12
        msg = parse_message(
            packet,
            envelope_limit_bytes=len(packet.encode("utf-8")),
            body_limit_bytes=body_limit,
        )
        assert msg.body == body

    def test_body_over_limit_by_one_utf8_byte_rejected(self):
        body = "啊" * 4
        packet = self._packet(body)
        with pytest.raises(ProtocolError) as exc_info:
            parse_message(
                packet,
                envelope_limit_bytes=None,
                body_limit_bytes=len(body.encode("utf-8")) - 1,
            )
        assert exc_info.value.code is ProtocolErrorCode.BODY_TOO_LARGE

    def test_default_envelope_limit_enforced_on_huge_text(self):
        text = "x" * (DEFAULT_ENVELOPE_LIMIT_BYTES + 1)
        with pytest.raises(ProtocolError) as exc_info:
            parse_message(text)
        assert exc_info.value.code is ProtocolErrorCode.ENVELOPE_TOO_LARGE

    def test_default_body_limit_enforced_within_envelope(self):
        body = "y" * (DEFAULT_BODY_LIMIT_BYTES + 1)
        packet = self._packet(body)
        with pytest.raises(ProtocolError) as exc_info:
            parse_message(packet)  # envelope 未超限，正文超
        assert exc_info.value.code is ProtocolErrorCode.BODY_TOO_LARGE

    def test_legacy_envelope_limit_applies(self):
        text = _legacy_text("正文")
        with pytest.raises(ProtocolError) as exc_info:
            parse_message(text, envelope_limit_bytes=10)
        assert exc_info.value.code is ProtocolErrorCode.ENVELOPE_TOO_LARGE