"""AI Relay B V3.0：OpenChamber CREATE_SESSION 写 Transport（T22-05A）。

用途限定：本类只做两件事，任何其他能力都不存在：
- capture_pre_create_snapshot(...)：POST 之前对 /api/session 做一次 GET-only 基线快照，
  只收集 session identity（id），绝不保存 message body / token / Authorization / cookie；
- create_once(...)：唯一 side-effect 路径 = POST /api/session?directory=<frozen>，
  body 严格只含 {"title": "<frozen create_title>"}。

结构约束与 T22-03/04 一致：
- 构造冻结 base_url/directory/token/timeout；写目标 fail-closed：仅允许精确 loopback
  白名单 {localhost, 127.0.0.1, ::1}（不扩展 127/8、不逐字节扩张 ipaddress.is_loopback），
  拒绝 127.0.0.2 / 127.1.2.3 / 远程 / URL 内嵌凭据（构造期 0 HTTP）；
- 不暴露 generic post()/request(method=...)/write(...)；不 import / 不调用 prompt_async；
- create POST：no redirect follow、no automatic retry、单次调用 opener 调用 <= 1；
- HTTP 分类（T22-05A §19）：
    200 + JSON object + id 合法（ses_/sess_ 前缀） → ACCEPTED；
    200 但 id 缺失/非法 / 200 body 不可读 / 其他 2xx / 3xx / 5xx → UNKNOWN（side effect
    可能已发生）；4xx → REJECTED；
    timeout / connection reset / URLError / OSError → OpenChamberCreateTransportError；
- evidence 只保存 http_status / created_session_id / classification，绝不保存 raw response
  body；token 只进 Authorization 头，绝不落入 evidence / 异常正文 / repr。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum

from adapters.openchamber import (
    MAX_BODY_BYTES,
    OpenChamberReadError,
    OpenChamberReadTransport,
    _endpoint_matches,
    _make_urllib_write_call,
    _read_bounded,
    _validate_base_url,
    is_loopback_base_url,
    is_valid_session_id,
)
from core.dispatch import SendTransportError, SnapshotFailure

LOCAL_AUTH_HOSTS_TEXT = "{localhost, 127.0.0.1, ::1}"


class OpenChamberCreateTransportError(SendTransportError):
    """CREATE_SESSION 传输层错误：结果不明（可能已发生 side effect），abort 不重试。

    kind 稳定分类：TIMEOUT / CONNECTION / ENDPOINT_MISMATCH / NON_LOOPBACK_TARGET /
    MISSING_CONFIG / MISSING_CREATE_TITLE。detail 只含静态排查上下文，绝不携带
    远端 response body、token 或 Authorization。
    """

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}".rstrip(": "))
        self.kind = kind


class CreateOutcome(str, Enum):
    ACCEPTED = "create_accepted"
    REJECTED = "create_rejected"
    UNKNOWN = "create_unknown"


@dataclass(frozen=True, slots=True)
class CreateAttempt:
    """一次 create_once 的结构化结果。evidence 只含最小字段，绝不含 raw body。"""

    outcome: CreateOutcome
    created_session_id: str | None = None
    evidence: dict = None  # type: ignore[assignment]

    @property
    def classification(self) -> str:
        return self.outcome.value


class OpenChamberCreateSessionTransport:
    """CREATE_SESSION 生产 Transport（公开面 = capture_pre_create_snapshot / create_once）。

    构造冻结全部配置，绝不动态回读 SettingsService；测试 seam 注入 read_opener 与
    write_opener（均可调用，入参 urllib Request）。
    """

    def __init__(
        self,
        base_url: str,
        directory: str,
        *,
        token: str | None = None,
        timeout: float = 3.0,
        read_opener=None,
        write_opener=None,
    ) -> None:
        self.base_url = _validate_base_url(base_url)
        if not is_loopback_base_url(self.base_url):
            raise OpenChamberCreateTransportError(
                "NON_LOOPBACK_TARGET",
                f"生产 create 目标仅允许精确本机白名单 {LOCAL_AUTH_HOSTS_TEXT}：拒绝构造（0 HTTP）",
            )
        self.directory = _require_nonblank(directory, "directory")
        self.timeout = float(timeout)
        self._token = token.strip() if isinstance(token, str) and token.strip() else None
        self._read = OpenChamberReadTransport(
            self.base_url, token=self._token, timeout=self.timeout, opener=read_opener
        )
        self._write_call = (
            write_opener if write_opener is not None else _make_urllib_write_call(self.timeout)
        )

    # ------------------------------------------------- pre-create baseline（GET-only）

    def capture_pre_create_snapshot(self, *, endpoint: str) -> dict:
        """POST 前只读基线：一次 GET /api/session，只保留 session identity。

        返回 {"session_ids_before": [...], "session_count_before": n}。
        list 顶层必须合法、被采纳的 id 非空、无重复；任一不满足 → SnapshotFailure
        （0 POST，create side effect 尚未开始）。传输/JSON 失败同样 → SnapshotFailure。
        """
        if not _endpoint_matches(self.base_url, endpoint):
            raise OpenChamberCreateTransportError(
                "ENDPOINT_MISMATCH",
                "pre-create 快照目标与构造冻结 base_url 不一致：拒绝快照（0 HTTP）",
            )
        try:
            resp = self._read.get(
                "/api/session?" + self._directory_query(), attach_auth=True
            )
        except OpenChamberReadError as exc:
            raise SnapshotFailure(
                f"session list GET 失败（{exc.kind}）：基线快照中止（0 POST）"
            ) from exc
        if not (200 <= resp.status < 300):
            raise SnapshotFailure(
                f"session list GET HTTP {resp.status}：基线快照中止（0 POST）"
            )
        try:
            payload = json.loads(resp.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise SnapshotFailure(
                "session list GET 响应非合法 JSON：基线快照中止（0 POST）"
            ) from exc
        if not isinstance(payload, list):
            raise SnapshotFailure("session list 顶层不是数组：基线快照中止（0 POST）")
        ids: list[str] = []
        for index, entry in enumerate(payload):
            if not isinstance(entry, dict):
                raise SnapshotFailure(
                    f"session[{index}] 不是对象：基线快照中止（0 POST）"
                )
            sid = entry.get("id")
            if not isinstance(sid, str) or not sid.strip():
                raise SnapshotFailure(
                    f"session[{index}] id 缺失/为空：基线快照中止（0 POST）"
                )
            ids.append(sid.strip())
        if len(set(ids)) != len(ids):
            raise SnapshotFailure("session ids 存在重复：基线快照中止（0 POST）")
        return {"session_ids_before": ids, "session_count_before": len(ids)}

    # ------------------------------------------------- create_once（唯一写入口）

    def create_once(self, *, endpoint: str, create_title: str) -> CreateAttempt:
        """执行单次 CREATE POST（最多 1 次 write opener 调用，不重试、不 redirect-follow）。

        精确 contract：POST /api/session?directory=<urlencoded frozen directory>，
        body 顶层严格只含 title。
        """
        if not _endpoint_matches(self.base_url, endpoint):
            raise OpenChamberCreateTransportError(
                "ENDPOINT_MISMATCH",
                "create 目标与构造冻结 base_url 不一致：拒绝 POST（0 HTTP）",
            )
        title = _require_nonblank(create_title, "create_title")
        url = f"{self.base_url}/api/session?{self._directory_query()}"
        body = {"title": title}
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(
            url,
            method="POST",
            headers=headers,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        )
        status, raw_body = self._post(req)
        return _create_attempt_from_status(status, raw_body)

    # ---------------------------------------------------------------- internals

    def _directory_query(self) -> str:
        return urllib.parse.urlencode({"directory": self.directory})

    def _post(self, req: urllib.request.Request) -> tuple[int, bytes]:
        try:
            raw = self._write_call(req)
            status = int(getattr(raw, "status", None) or raw.getcode() or 0)
            body, _truncated = _read_bounded(raw, MAX_BODY_BYTES)
            return status, body
        except urllib.error.HTTPError as exc:
            body, _truncated = _read_bounded(exc, MAX_BODY_BYTES)
            return exc.code, body
        except socket.timeout as exc:
            raise OpenChamberCreateTransportError(
                "TIMEOUT", "CREATE POST 超时：结果不明（可能已建会话），禁止重试"
            ) from exc
        except urllib.error.URLError as exc:
            raise OpenChamberCreateTransportError(
                "CONNECTION", f"{type(exc).__name__}: {exc.reason}"
            ) from exc
        except OSError as exc:
            raise OpenChamberCreateTransportError(
                "CONNECTION", f"{type(exc).__name__}: {exc}"
            ) from exc


def _create_attempt_from_status(status: int, raw_body: bytes) -> CreateAttempt:
    """HTTP status → CreateOutcome（T22-05A §19）。raw body 绝不进入 evidence。"""
    if status == 200:
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return CreateAttempt(
                CreateOutcome.UNKNOWN,
                evidence={"http_status": 200, "classification": "create_unknown"},
            )
        if isinstance(payload, dict) and is_valid_session_id(payload.get("id")):
            sid = payload["id"]
            return CreateAttempt(
                CreateOutcome.ACCEPTED,
                created_session_id=sid,
                evidence={
                    "http_status": 200,
                    "created_session_id": sid,
                    "classification": "create_accepted",
                },
            )
        return CreateAttempt(
            CreateOutcome.UNKNOWN,
            evidence={"http_status": 200, "classification": "create_unknown"},
        )
    if 400 <= status < 500:
        return CreateAttempt(
            CreateOutcome.REJECTED,
            evidence={"http_status": status, "classification": "create_rejected"},
        )
    return CreateAttempt(
        CreateOutcome.UNKNOWN,
        evidence={"http_status": status, "classification": "create_unknown"},
    )


def _require_nonblank(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OpenChamberCreateTransportError(
            "MISSING_CREATE_TITLE" if name == "create_title" else "MISSING_CONFIG",
            f"{name} 不能为空（任何 HTTP 之前拒绝）",
        )
    return value.strip()