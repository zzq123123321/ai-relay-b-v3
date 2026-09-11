"""V3 领域错误码与领域异常（T03）。

错误判断一律依赖稳定的 ErrorCode（英文小写字符串），不使用中文字符串做程序判断；
中文 message 仅用于 UI/日志展示。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping


class ErrorCode(str, Enum):
    """稳定的机器可读错误码，value 固定为英文小写，不得修改。"""

    INVALID_ID = "invalid_id"
    INVALID_TRANSITION = "invalid_transition"
    STALE_EPOCH = "stale_epoch"
    STALE_ATTEMPT = "stale_attempt"
    INVALID_COMMAND = "invalid_command"
    INVALID_OBSERVATION = "invalid_observation"
    INVALID_DECISION = "invalid_decision"
    INVALID_RESULT = "invalid_result"
    CLOCK_ERROR = "clock_error"


class DomainError(Exception):
    """携带稳定错误码的领域异常；message 为中文，供 UI/日志使用。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = dict(context or {})

    def __str__(self) -> str:
        return f"[{self.code.value}] {self.message}"