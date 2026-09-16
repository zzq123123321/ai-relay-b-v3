"""AI Relay B V3.0：OpenChamber UNKNOWN 对账纯 GET 读取器（T22-04）。

本模块只回答一个问题：本次 planned user messageID 是否已被远端会话持久观察到。
它是独立层的 GET-only readback，绝不与 T22-03 的 prompt_async 写传输耦合。

结构约束：
- 对外唯一 HTTP 能力是复用 OpenChamberReadTransport 的 GET（urllib，唯一 seam）；
- 不 import / 不调用 write opener、不调用任何 prompt_async / send_once / create，
  本模块不出现 POST / requests / 自动 retry；
- 构造冻结 base_url / directory / token / timeout / opener，生产目标仅精确
  loopback 白名单 {localhost, 127.0.0.1, ::1}，非白名单拒绝构造（0 HTTP）；
- 公共面只有一个 observe_once(...)：输入非法（endpoint 与冻结 base_url 不一致、
  非法 session id、非法 expected msg_ id）→ OpenChamberReconciliationInputError，
  0 HTTP（fail closed）；
- 单次观察最多 3 个 GET：session list / message list / status；不 polling、不 retry；
- session list + message list 为必要 readback；status 为 advisory —— 精确 user
  message 已观察到时，status 单独失败不抹除已获得的投递证据；
- 只读消息身份（info.id / role / parentID），不读取/持久化 message body。

接受语义（仅供上层判定）：exact_user_message_observed 仅当 session_exists 且
exact_id_match_count == 1 且 exact_user_match_count == 1（绝不把 last/newest/
marker 等启发式当证据）。absence 永远不能证明远端未收到。
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass

from adapters.openchamber import (
    OpenChamberReadError,
    OpenChamberReadTransport,
    is_loopback_base_url,
    is_valid_session_id,
)

_LOCAL_AUTH_HOSTS_TEXT = "{localhost, 127.0.0.1, ::1}"


class OpenChamberReconciliationInputError(Exception):
    """对账读取输入非法：fail closed，0 HTTP。

    kind 稳定分类：INVALID_URL / NON_LOOPBACK_TARGET / MISSING_DIRECTORY /
    ENDPOINT_MISMATCH / INVALID_SESSION_ID / MISSING_EXPECTED_ID /
    INVALID_EXPECTED_ID。detail 只含静态排查上下文，不含 token / 远端正文。
    """

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}".rstrip(": "))
        self.kind = kind


@dataclass(frozen=True, slots=True)
class ReconciliationObservation:
    """单轮只读观察结果（不可变，不含 message body / token）。

    - session_exists：session list 中是否存在目标 session；
    - message_count：message list 条数；
    - exact_id_match_count：info.id == expected_user_message_id 的消息数；
    - exact_user_match_count：其中 role == "user" 的消息数；
    - assistant_parent_match_count：role == "assistant" 且 parentID == expected 数；
    - assistant_child_message_ids：上述 assistant 的 id；
    - status_entry_present：None=status 未获得（endpoint 错误）；否则 bool(非空 dict)；
    - required_read_error：session list / message list 读取失败分类
      （TIMEOUT/CONNECTION/http/malformed_json/not_list/malformed_messages），
      非 None 时精确计数不可信，proof 一律为否；
    - status_read_error：status 单独失败分类（advisory，不反证 message proof）。
    """

    session_exists: bool = False
    message_count: int = 0
    exact_id_match_count: int = 0
    exact_user_match_count: int = 0
    assistant_parent_match_count: int = 0
    status_entry_present: bool | None = None
    assistant_child_message_ids: tuple[str, ...] = ()
    required_read_error: str | None = None
    status_read_error: str | None = None

    @property
    def exact_user_message_observed(self) -> bool:
        """接受判定主证据：session 存在 + 恰好一个精确 planned id + role == user。

        这证明本次 client-supplied messageID 已被远端持久观察到；
        仍不等于 task completed（parent 关系只是额外归属证据，非必要条件）。
        """
        return (
            self.required_read_error is None
            and self.session_exists
            and self.exact_id_match_count == 1
            and self.exact_user_match_count == 1
        )


class OpenChamberUnknownReconciliationReader:
    """UNKNOWN 对账纯 GET 读取器（公开面=observe_once）。

    构造冻结全部配置，绝不动态回读 SettingsService；HTTP 唯一 seam 复用
    OpenChamberReadTransport，本类不新增任何写能力。
    """

    def __init__(
        self,
        base_url: str,
        *,
        directory: str,
        token: str | None = None,
        timeout: float = 3.0,
        opener=None,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise OpenChamberReconciliationInputError(
                "INVALID_URL", "base_url 不能为空（0 HTTP）"
            )
        if not is_loopback_base_url(base_url):
            raise OpenChamberReconciliationInputError(
                "NON_LOOPBACK_TARGET",
                f"对账读取目标仅允许精确本机白名单 {_LOCAL_AUTH_HOSTS_TEXT}：拒绝构造（0 HTTP）",
            )
        if not isinstance(directory, str) or not directory.strip():
            raise OpenChamberReconciliationInputError(
                "MISSING_DIRECTORY", "directory 不能为空（0 HTTP）"
            )
        transport = OpenChamberReadTransport(
            base_url, token=token, timeout=timeout, opener=opener
        )
        self.base_url = transport.base_url
        self.directory = directory.strip()
        self._read = transport

    # ---------------------------------------------------------------- observe

    def observe_once(
        self,
        *,
        endpoint: str,
        session_id: str,
        expected_user_message_id: str,
    ) -> ReconciliationObservation:
        """执行单轮 GET-only 观察（最多 3 个 GET，不 retry，0 POST）。

        输入非法一律抛 OpenChamberReconciliationInputError（0 HTTP）。
        """
        if not isinstance(endpoint, str) or endpoint.rstrip("/") != self.base_url:
            raise OpenChamberReconciliationInputError(
                "ENDPOINT_MISMATCH",
                "endpoint 与构造冻结 base_url 不一致：拒绝观察（0 HTTP）",
            )
        if not is_valid_session_id(session_id):
            raise OpenChamberReconciliationInputError(
                "INVALID_SESSION_ID",
                "非法 session_id（只接受 ses_/sess_ 前缀，且前缀后仍有内容）：拒绝观察（0 HTTP）",
            )
        if not isinstance(expected_user_message_id, str) or not expected_user_message_id.strip():
            raise OpenChamberReconciliationInputError(
                "MISSING_EXPECTED_ID", "expected_user_message_id 为空：拒绝观察（0 HTTP）"
            )
        expected = expected_user_message_id.strip()
        if not expected.startswith("msg_"):
            raise OpenChamberReconciliationInputError(
                "INVALID_EXPECTED_ID",
                "expected_user_message_id 非 msg_ 前缀：拒绝观察（0 HTTP）",
            )

        query = urllib.parse.urlencode({"directory": self.directory})

        session_payload, serr = self._get_json("/api/session?" + query)
        if serr is not None:
            return ReconciliationObservation(required_read_error=serr)
        if not isinstance(session_payload, list):
            return ReconciliationObservation(required_read_error="not_list")
        session_exists = _session_in_list(session_payload, session_id)
        if not session_exists:
            return ReconciliationObservation(session_exists=False)

        message_path = (
            f"/api/session/{urllib.parse.quote(session_id, safe='')}/message?" + query
        )
        message_payload, merr = self._get_json(message_path)
        if merr is not None:
            return ReconciliationObservation(session_exists=True, required_read_error=merr)
        if not isinstance(message_payload, list):
            return ReconciliationObservation(session_exists=True, required_read_error="not_list")

        parsed = _parse_messages(message_payload, expected)
        if parsed.error is not None:
            return ReconciliationObservation(
                session_exists=True, required_read_error=parsed.error
            )

        status_payload, sterr = self._get_json("/api/session/status?" + query)
        if sterr is not None:
            return ReconciliationObservation(
                session_exists=True,
                message_count=parsed.message_count,
                exact_id_match_count=parsed.exact_id,
                exact_user_match_count=parsed.exact_user,
                assistant_parent_match_count=parsed.parent_match,
                status_entry_present=None,
                assistant_child_message_ids=tuple(parsed.child_ids),
                status_read_error=sterr,
            )
        if not isinstance(status_payload, dict):
            return ReconciliationObservation(
                session_exists=True,
                message_count=parsed.message_count,
                exact_id_match_count=parsed.exact_id,
                exact_user_match_count=parsed.exact_user,
                assistant_parent_match_count=parsed.parent_match,
                status_entry_present=None,
                assistant_child_message_ids=tuple(parsed.child_ids),
                status_read_error="not_dict",
            )
        return ReconciliationObservation(
            session_exists=True,
            message_count=parsed.message_count,
            exact_id_match_count=parsed.exact_id,
            exact_user_match_count=parsed.exact_user,
            assistant_parent_match_count=parsed.parent_match,
            status_entry_present=bool(status_payload),
            assistant_child_message_ids=tuple(parsed.child_ids),
        )

    # ---------------------------------------------------------------- internals

    def _get_json(self, path: str) -> tuple[object | None, str | None]:
        """一次 GET + JSON 解析；失败返回 (None, 分类)。传输异常分类原样上抛的 kind。"""
        try:
            resp = self._read.get(path, attach_auth=True)
        except OpenChamberReadError as exc:
            return None, exc.kind
        if not (200 <= resp.status < 300):
            return None, "http"
        try:
            payload = json.loads(resp.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None, "malformed_json"
        return payload, None


# --------------------------------------------------------------------- parsing


def _session_in_list(payload: list, session_id: str) -> bool:
    for entry in payload:
        if isinstance(entry, dict) and entry.get("id") == session_id:
            return True
    return False


@dataclass(frozen=True, slots=True)
class _MessageStats:
    message_count: int = 0
    exact_id: int = 0
    exact_user: int = 0
    parent_match: int = 0
    child_ids: tuple[str, ...] = ()
    error: str | None = None


def _parse_messages(payload: list, expected: str) -> _MessageStats:
    """按 T21 封板消息形状只读身份；malformed → error（连同已累计计数一律作废）。"""
    exact_id = 0
    exact_user = 0
    parent_match = 0
    child_ids: list[str] = []
    for item in payload:
        info = item.get("info") if isinstance(item, dict) else None
        if not isinstance(info, dict):
            return _MessageStats(message_count=len(payload), error="malformed_messages")
        mid = info.get("id")
        role = info.get("role")
        if not isinstance(mid, str) or not mid:
            return _MessageStats(message_count=len(payload), error="malformed_messages")
        if role is not None and not isinstance(role, str):
            return _MessageStats(message_count=len(payload), error="malformed_messages")
        if mid == expected:
            exact_id += 1
            if role == "user":
                exact_user += 1
        parent = info.get("parentID")
        if role == "assistant" and isinstance(parent, str) and parent == expected:
            parent_match += 1
            child_ids.append(mid)
    return _MessageStats(
        message_count=len(payload),
        exact_id=exact_id,
        exact_user=exact_user,
        parent_match=parent_match,
        child_ids=tuple(child_ids),
    )