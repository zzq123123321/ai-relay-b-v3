"""AI_RELAY V1 与 legacy 协议：严格解析、RESPONSE 封装与规范化内容摘要。

协议合同依据主规格第05章、17.2、附录A（max_body/envelope=2MiB/4MiB）：

- 格式识别：正文以已替换 CRLF 的文本判断；V1 首行必须严格为 marker，legacy
  允许 marker 前后存在外围空白。
- 正文保持：V1 正文仅做 CRLF→LF 规范化，绝不 strip/重排；
  legacy 按旧约定处理 CONTENT 段（外围空白裁剪、正文两端空白按约定清理）。
- 身份：V1 用 MESSAGE_ID，legacy 用 TASK_ID；id 大小写敏感、禁止控制字符。
- 未知扩展头：保留并参与内容摘要，不执行未知语义。
- TARGET 白名单仅约束 TASK（REASONIX/OPENCHAMBER/EXECUTOR）；RESPONSE 的
  TARGET 为 CHATGPT，不适用执行端白名单。
- RESPONSE 封装：V1 以 IN_REPLY_TO 关联原 task_id；legacy 以 TASK_ID 关联。
- content_digest：对执行语义字段做确定性 SHA-256 摘要（详见函数文档）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from uuid import uuid4

PROTOCOL_MARKER = "AI_RELAY/1"
LEGACY_BEGIN = "----- AI_RELAY_BEGIN -----"
LEGACY_END = "----- AI_RELAY_END -----"

DEFAULT_ROUND = 0
DEFAULT_MAX_ROUNDS = 3

DEFAULT_ENVELOPE_LIMIT_BYTES = 4 * 1024 * 1024
DEFAULT_BODY_LIMIT_BYTES = 2 * 1024 * 1024

_ALLOWED_TASK_TARGETS = frozenset(("REASONIX", "OPENCHAMBER", "EXECUTOR"))

_KNOWN_V1_HEADERS = frozenset(
    ("MESSAGE_ID", "SOURCE", "TARGET", "TYPE", "IN_REPLY_TO", "ROUND", "MAX_ROUNDS", "WORKDIR")
)
_KNOWN_LEGACY_HEADERS = frozenset(
    ("TASK_ID", "SOURCE", "TARGET", "TYPE", "IN_REPLY_TO", "ROUND", "MAX_ROUNDS", "WORKDIR", "CONTENT", "TIME")
)


class ProtocolFormat(Enum):
    V1 = "v1"
    LEGACY_WEB = "legacy_web"


class MessageType(Enum):
    TASK = "TASK"
    RESPONSE = "RESPONSE"


class ProtocolErrorCode(Enum):
    MISSING_MARKER = "missing_marker"
    MISSING_SEPARATOR = "missing_separator"
    MISSING_END_MARKER = "missing_end_marker"
    MISSING_CONTENT_HEADER = "missing_content_header"
    MISSING_HEADER = "missing_header"
    INVALID_HEADER = "invalid_header"
    DUPLICATE_HEADER = "duplicate_header"
    UNKNOWN_TYPE = "unknown_type"
    UNKNOWN_TARGET = "unknown_target"
    INVALID_ROUND = "invalid_round"
    INVALID_ROUND_RANGE = "invalid_round_range"
    INVALID_ID = "invalid_id"
    EMPTY_BODY = "empty_body"
    ENVELOPE_TOO_LARGE = "envelope_too_large"
    BODY_TOO_LARGE = "body_too_large"
    INVALID_MESSAGE = "invalid_message"


class ProtocolError(ValueError):
    """协议解析/封装错误。携带稳定 code，禁止用字符串匹配判断错误类别。"""

    def __init__(self, code: ProtocolErrorCode, reason: str) -> None:
        super().__init__(f"[{code.value}] {reason}")
        self.code = code
        self.reason = reason


@dataclass(frozen=True, slots=True)
class RelayMessage:
    message_id: str
    source: str
    target: str
    message_type: MessageType
    body: str
    protocol_format: ProtocolFormat
    round_number: int = DEFAULT_ROUND
    max_rounds: int = DEFAULT_MAX_ROUNDS
    in_reply_to: str | None = None
    workdir: str | None = None
    extension_headers: tuple[tuple[str, str], ...] = ()


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n")


def _to_utf8_bytes(value: str) -> int:
    return len(value.encode("utf-8"))


def _check_envelope_size(normalized: str, envelope_limit_bytes: int) -> None:
    if envelope_limit_bytes is not None and _to_utf8_bytes(normalized) > envelope_limit_bytes:
        raise ProtocolError(
            ProtocolErrorCode.ENVELOPE_TOO_LARGE,
            f"消息信封超过大小限制 {envelope_limit_bytes} 字节（UTF-8）",
        )


def _check_body_size(body: str, body_limit_bytes: int) -> None:
    if body_limit_bytes is not None and _to_utf8_bytes(body) > body_limit_bytes:
        raise ProtocolError(
            ProtocolErrorCode.BODY_TOO_LARGE,
            f"正文超过大小限制 {body_limit_bytes} 字节（UTF-8）",
        )


def parse_message(
    text: str,
    *,
    envelope_limit_bytes: int = DEFAULT_ENVELOPE_LIMIT_BYTES,
    body_limit_bytes: int = DEFAULT_BODY_LIMIT_BYTES,
) -> RelayMessage:
    """解析一条协议消息为不可变 RelayMessage。

    解析前先做 CRLF→LF 规范化并按 UTF-8 字节数执行信封大小保护；
    正文限制按规范化的 body 字节数执行。协议层不做路由、不访问设置数据库。
    """
    normalized = _normalize(text)
    _check_envelope_size(normalized, envelope_limit_bytes)
    if normalized.lstrip().startswith(LEGACY_BEGIN):
        return _parse_legacy_web_message(normalized, body_limit_bytes)
    return _parse_v1_message(normalized, body_limit_bytes)


def _validate_id(key: str, value: str) -> None:
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ProtocolError(
            ProtocolErrorCode.INVALID_ID, f"{key} 含控制字符，不允许"
        )


def _parse_v1_message(normalized: str, body_limit_bytes: int) -> RelayMessage:
    header, separator, body = normalized.partition("\n\n")
    lines = header.split("\n")
    if not lines or lines[0].strip() != PROTOCOL_MARKER:
        raise ProtocolError(ProtocolErrorCode.MISSING_MARKER, "首行必须为 AI_RELAY/1")
    if not separator:
        raise ProtocolError(
            ProtocolErrorCode.MISSING_SEPARATOR, "缺少 header 与正文之间的空行"
        )

    fields: dict[str, str] = {}
    extensions: list[tuple[str, str]] = []
    for line in lines[1:]:
        key, colon, raw_value = line.partition(":")
        if not (colon and key.strip() and raw_value.strip()):
            raise ProtocolError(ProtocolErrorCode.INVALID_HEADER, f"非法 header 行：{line!r}")
        normalized_key = key.strip().upper()
        value = raw_value.strip()
        if normalized_key in fields:
            raise ProtocolError(ProtocolErrorCode.DUPLICATE_HEADER, f"重复 header：{normalized_key}")
        fields[normalized_key] = value
        if normalized_key not in _KNOWN_V1_HEADERS:
            extensions.append((normalized_key, value))

    return _build_message(
        fields,
        protocol_format=ProtocolFormat.V1,
        id_name="MESSAGE_ID",
        body=body,
        body_limit_bytes=body_limit_bytes,
        extensions=extensions,
    )


def _parse_legacy_web_message(normalized: str, body_limit_bytes: int) -> RelayMessage:
    stripped = normalized.strip()
    if not stripped.startswith(LEGACY_BEGIN):
        raise ProtocolError(ProtocolErrorCode.MISSING_MARKER, "缺少 legacy BEGIN marker")
    if not stripped.endswith(LEGACY_END):
        raise ProtocolError(ProtocolErrorCode.MISSING_END_MARKER, "缺少 legacy END marker")

    payload = stripped[len(LEGACY_BEGIN):-len(LEGACY_END)].strip("\n")

    fields: dict[str, str] = {}
    extensions: list[tuple[str, str]] = []
    body_lines: list[str] | None = None

    for line in payload.split("\n"):
        if body_lines is not None:
            body_lines.append(line)
            continue
        key, colon, raw_value = line.partition(":")
        normalized_key = key.strip().upper()
        if not (colon and normalized_key):
            raise ProtocolError(ProtocolErrorCode.INVALID_HEADER, f"非法 legacy header 行：{line!r}")
        if normalized_key == "CONTENT":
            body_lines = [raw_value.lstrip()] if raw_value.strip() else []
        elif normalized_key in fields:
            raise ProtocolError(ProtocolErrorCode.DUPLICATE_HEADER, f"重复 legacy header：{normalized_key}")
        else:
            fields[normalized_key] = raw_value.strip()
            if normalized_key not in _KNOWN_LEGACY_HEADERS:
                extensions.append((normalized_key, raw_value.strip()))

    if body_lines is None:
        raise ProtocolError(ProtocolErrorCode.MISSING_CONTENT_HEADER, "缺少 legacy header：CONTENT")

    body = "\n".join(body_lines).strip()
    if not body:
        raise ProtocolError(ProtocolErrorCode.EMPTY_BODY, "消息正文必须非空")

    _check_body_size(body, body_limit_bytes)

    message = _build_message(
        fields,
        protocol_format=ProtocolFormat.LEGACY_WEB,
        id_name="TASK_ID",
        body=body,
        body_limit_bytes=None,
        extensions=extensions,
    )
    return message


def _build_message(
    fields: dict[str, str],
    *,
    protocol_format: ProtocolFormat,
    id_name: str,
    body: str,
    body_limit_bytes: int | None,
    extensions: list[tuple[str, str]],
) -> RelayMessage:
    required = (id_name, "SOURCE", "TARGET", "TYPE")
    missing = [name for name in required if not fields.get(name)]
    if missing:
        raise ProtocolError(
            ProtocolErrorCode.MISSING_HEADER, f"缺少必填 header：{', '.join(missing)}"
        )

    message_id = fields[id_name]
    _validate_id(id_name, message_id)

    if not body.strip():
        raise ProtocolError(ProtocolErrorCode.EMPTY_BODY, "消息正文必须非空")
    _check_body_size(body, body_limit_bytes)

    try:
        message_type = MessageType(fields["TYPE"].upper())
    except ValueError as exc:
        raise ProtocolError(
            ProtocolErrorCode.UNKNOWN_TYPE, f"不支持的消息类型：{fields['TYPE']}"
        ) from exc

    target = fields["TARGET"].upper()
    if message_type is MessageType.TASK and target not in _ALLOWED_TASK_TARGETS:
        raise ProtocolError(
            ProtocolErrorCode.UNKNOWN_TARGET,
            f"TASK 的 TARGET 只允许 REASONIX/OPENCHAMBER/EXECUTOR，实际：{fields['TARGET']}",
        )

    round_number, max_rounds = _parse_rounds(fields)

    in_reply_to = fields.get("IN_REPLY_TO")
    workdir_value = fields.get("WORKDIR")
    workdir = workdir_value if workdir_value is not None and workdir_value != "" else None

    if message_type is MessageType.RESPONSE and protocol_format is ProtocolFormat.V1:
        if not in_reply_to:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_MESSAGE, "V1 RESPONSE 必须带 IN_REPLY_TO 关联原任务"
            )
        _validate_id("IN_REPLY_TO", in_reply_to)

    return RelayMessage(
        message_id=message_id,
        source=fields["SOURCE"].upper(),
        target=target,
        message_type=message_type,
        body=body,
        protocol_format=protocol_format,
        round_number=round_number,
        max_rounds=max_rounds,
        in_reply_to=in_reply_to,
        workdir=workdir,
        extension_headers=tuple(extensions),
    )


def _parse_rounds(fields: dict[str, str]) -> tuple[int, int]:
    try:
        round_number = int(fields.get("ROUND", str(DEFAULT_ROUND)))
        max_rounds = int(fields.get("MAX_ROUNDS", str(DEFAULT_MAX_ROUNDS)))
    except (TypeError, ValueError) as exc:
        raise ProtocolError(
            ProtocolErrorCode.INVALID_ROUND, "ROUND 和 MAX_ROUNDS 必须是整数"
        ) from exc

    if round_number < 0 or max_rounds < 1 or round_number > max_rounds:
        raise ProtocolError(
            ProtocolErrorCode.INVALID_ROUND_RANGE,
            f"ROUND/MAX_ROUNDS 越界：ROUND={round_number}，MAX_ROUNDS={max_rounds}",
        )

    return round_number, max_rounds


def _validate_response_args(
    body: str, in_reply_to: str, round_number: int, max_rounds: int
) -> str:
    if not body.strip():
        raise ProtocolError(ProtocolErrorCode.EMPTY_BODY, "响应正文必须非空")
    if not in_reply_to.strip():
        raise ProtocolError(ProtocolErrorCode.MISSING_HEADER, "in_reply_to 不能为空")
    if round_number < 0 or max_rounds < 1 or round_number > max_rounds:
        raise ProtocolError(
            ProtocolErrorCode.INVALID_ROUND_RANGE,
            f"ROUND/MAX_ROUNDS 越界：ROUND={round_number}，MAX_ROUNDS={max_rounds}",
        )
    return _normalize(body)


def wrap_response(
    body: str,
    *,
    in_reply_to: str,
    protocol_format: ProtocolFormat = ProtocolFormat.V1,
    round_number: int = DEFAULT_ROUND,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    response_message_id: str | None = None,
    timestamp: str | None = None,
) -> str:
    """封装 RESPONSE 协议文本，绑定原 task_id。

    V1 以 IN_REPLY_TO 关联原任务；legacy 以 TASK_ID 关联（同一原任务 id）。
    response_message_id 与 timestamp 可由调用方注入以保证测试可确定重放；
    默认 V1 生成新 MESSAGE_ID，legacy 默认不带 TIME 头。
    """
    normalized_body = _validate_response_args(body, in_reply_to, round_number, max_rounds)

    if protocol_format is ProtocolFormat.LEGACY_WEB:
        header_lines = [
            LEGACY_BEGIN,
            "SOURCE: EXECUTOR",
            "TARGET: CHATGPT",
            "TYPE: RESPONSE",
            f"TASK_ID: {in_reply_to}",
            f"ROUND: {round_number}",
            f"MAX_ROUNDS: {max_rounds}",
        ]
        if timestamp is not None:
            header_lines.append(f"TIME: {timestamp}")
        return "\n".join((*header_lines, "CONTENT:", normalized_body, LEGACY_END))

    message_id = response_message_id if response_message_id is not None else str(uuid4())
    return "\n".join(
        (
            PROTOCOL_MARKER,
            f"MESSAGE_ID: {message_id}",
            f"IN_REPLY_TO: {in_reply_to}",
            "SOURCE: EXECUTOR",
            "TARGET: CHATGPT",
            "TYPE: RESPONSE",
            f"ROUND: {round_number}",
            f"MAX_ROUNDS: {max_rounds}",
            "",
            normalized_body,
        )
    )


def content_digest(message: RelayMessage) -> str:
    """规范化内容摘要（SHA-256）。

    输入为执行语义字段的安全序列化：
    - type / target / round / max_rounds / workdir / 排序后的扩展头 / body
    依据：这些字段只要变化就改变远端要执行的语义，T07 必须识别为 ID 冲突；
    协议格式、source、message_id/in_reply_to 不进入，因为它们不改变同一任务
    的执行内容。json.dumps(sort_keys=True) 保证与字段构造顺序无关、跨进程一致，
    不依赖 Python 内置 hash()。正文已规范化为 LF。
    """
    canonical: dict[str, object] = {
        "type": message.message_type.value,
        "target": message.target,
        "round": message.round_number,
        "max_rounds": message.max_rounds,
        "extension_headers": sorted(
            ((key, value) for key, value in message.extension_headers),
            key=lambda pair: pair[0],
        ),
    }
    if message.workdir is not None:
        canonical["workdir"] = message.workdir
    canonical["body"] = message.body
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()