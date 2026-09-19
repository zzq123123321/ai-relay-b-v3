"""AI Relay B V3.0：OpenChamber UNKNOWN CREATE_SESSION 对账纯 GET 读取器（T22-05B）。

本模块只回答一个问题：当前目录下的 /api/session session list 在观察时刻是什么。
它是独立层的 GET-only readback，绝不与 T22-05A 的 create_once 写传输耦合。

结构约束（与 T22-04 一致）：
- 对外唯一 HTTP 能力是复用 OpenChamberReadTransport 的 GET（urllib，唯一 seam）；
- 不 import / 不调用 write opener、不调用 create_once / send_once / prompt_async，
  本模块不出现 POST / requests / 自动 retry；
- 构造冻结 base_url / token / timeout / opener；生产目标仅精确 loopback 白名单
  {localhost, 127.0.0.1, ::1}，非白名单拒绝构造（0 HTTP）；
- 公共面只有一个 observe_sessions_once(...)：输入非法（endpoint 与冻结 base_url
  不一致、directory 为空）→ OpenChamberCreateReconciliationInputError，0 HTTP；
- 单次观察恰好 1 个 GET：GET /api/session?directory=<urlencoded>；不 polling、
  不 retry；只收集 session identity（id），不读取/持久化 message body / token /
  Authorization / cookie；
- directory 必须由调用方（协调器）从 operation.pre_snapshot_json 提供，绝不从
  当前 SettingsService 读取。

after list 校验（任一不满足 → read_error，协调器 READ_FAILED 且保持 UNKNOWN）：
顶层必须是数组、每个 entry 是对象、每个 id 必须是 nonblank string、全部唯一。
新候选 session 的 ses_/sess_ 前缀校验由协调器在【差集后】判定，不在本层执行，
因为旧 baseline session id 不强制要求前缀（T22-05A baseline contract 即如此）。
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass

from adapters.openchamber import (
    OpenChamberReadError,
    OpenChamberReadTransport,
    _endpoint_matches,
    is_loopback_base_url,
)

_LOCAL_AUTH_HOSTS_TEXT = "{localhost, 127.0.0.1, ::1}"


class OpenChamberCreateReconciliationInputError(Exception):
    """对账读取输入非法：fail closed，0 HTTP。

    kind 稳定分类：INVALID_URL / NON_LOOPBACK_TARGET / ENDPOINT_MISMATCH /
    MISSING_DIRECTORY。detail 只含静态排查上下文，不含 token / 远端正文。
    """

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}".rstrip(": "))
        self.kind = kind


@dataclass(frozen=True, slots=True)
class CreateReconciliationObservation:
    """单轮只读 session list 观察（不可变，不含 token / message body）。

    - session_ids_after：观察时刻目录内全部 session id（按远端返回次序冻结）；
    - session_count_after：条数；
    - read_error：读取失败分类（TIMEOUT/CONNECTION/http/malformed_json/
      not_list/malformed_session_item/duplicate_ids），非 None 时 after 不可信，
      协调器必须 READ_FAILED 且保持 UNKNOWN。
    """

    session_ids_after: tuple[str, ...] = ()
    session_count_after: int = 0
    read_error: str | None = None


class OpenChamberUnknownCreateReconciliationReader:
    """UNKNOWN CREATE_SESSION 对账纯 GET 读取器（公开面=observe_sessions_once）。

    构造冻结全部配置，绝不动态回读 SettingsService；HTTP 唯一 seam 复用
    OpenChamberReadTransport，本类不新增任何写能力。
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 3.0,
        opener=None,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise OpenChamberCreateReconciliationInputError(
                "INVALID_URL", "base_url 不能为空（0 HTTP）"
            )
        if not is_loopback_base_url(base_url):
            raise OpenChamberCreateReconciliationInputError(
                "NON_LOOPBACK_TARGET",
                f"对账读取目标仅允许精确本机白名单 {_LOCAL_AUTH_HOSTS_TEXT}：拒绝构造（0 HTTP）",
            )
        transport = OpenChamberReadTransport(
            base_url, token=token, timeout=timeout, opener=opener
        )
        self.base_url = transport.base_url
        self._read = transport

    # ---------------------------------------------------------------- observe

    def observe_sessions_once(
        self,
        *,
        endpoint: str,
        directory: str,
    ) -> CreateReconciliationObservation:
        """执行单轮 GET-only session list 观察（恰好 1 个 GET，不 retry，0 POST）。

        输入非法一律抛 OpenChamberCreateReconciliationInputError（0 HTTP）。
        """
        if not _endpoint_matches(self.base_url, endpoint):
            raise OpenChamberCreateReconciliationInputError(
                "ENDPOINT_MISMATCH",
                "endpoint 与构造冻结 base_url 不一致：拒绝观察（0 HTTP）",
            )
        if not isinstance(directory, str) or not directory.strip():
            raise OpenChamberCreateReconciliationInputError(
                "MISSING_DIRECTORY", "directory 不能为空（0 HTTP）"
            )
        query = urllib.parse.urlencode({"directory": directory.strip()})
        try:
            resp = self._read.get("/api/session?" + query, attach_auth=True)
        except OpenChamberReadError as exc:
            return CreateReconciliationObservation(read_error=exc.kind)
        if not (200 <= resp.status < 300):
            return CreateReconciliationObservation(read_error="http")
        try:
            payload = json.loads(resp.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return CreateReconciliationObservation(read_error="malformed_json")
        if not isinstance(payload, list):
            return CreateReconciliationObservation(read_error="not_list")
        ids, reason = _extract_session_ids(payload)
        if reason is not None:
            return CreateReconciliationObservation(read_error=reason)
        return CreateReconciliationObservation(
            session_ids_after=ids, session_count_after=len(ids)
        )


# --------------------------------------------------------------------- parsing


def _extract_session_ids(payload: list) -> tuple[tuple[str, ...], str | None]:
    """从 session list 提取 id 顺序投影；malformed → ((), 分类)。

    任一 entry 非对象 / id 缺失或空 → malformed_session_item；
    整体存在重复 id → duplicate_ids（不允许按部分列表作判断）。
    """
    ids: list[str] = []
    for entry in payload:
        if not isinstance(entry, dict):
            return (), "malformed_session_item"
        sid = entry.get("id")
        if not isinstance(sid, str) or not sid.strip():
            return (), "malformed_session_item"
        ids.append(sid.strip())
    if len(set(ids)) != len(ids):
        return (), "duplicate_ids"
    return tuple(ids), None