"""AI Relay B V3.0：发送两阶段调度与一次发送资格（T10）。

依据（主规格 07.2 / 04.3，T10 卡）：
- 流程：第一次权威检查 → 只读发送前快照（事务外）→ 第二次权威检查 + 写 PREPARED
  → 第三次权威检查 + CAS PREPARED→SENDING（事务）→ FakeTransport.send_once 恰好一次
  （事务外）→ 落 ACCEPTED/REJECTED/UNKNOWN（事务）。
- 网络/FakeTransport 调用严格位于任何 SQLite 事务之外；测试 Fake 记录调用时 DB 不在事务。
- operation_id 由 operation_key 稳定派生（SHA-256 前缀），并发重复 Proposal 不会因随机
  operation_id 差异产生伪 CONFLICT；wire prompt 统一携带 task/attempt/operation 标记。
- UNKNOWN 核心安全状态：本地无法证明远端是否接受；同 operation_key 再次 dispatch 一律
  ALREADY_EXISTS，绝对不产生第二次 send_once。REJECTED 才表示“确认未派发”。
- 本模块不实现 POST 自动重试、不创建/轮换会话、不实现恢复计数。

三次权威检查的裁决依据为 task/attempt/project owner 的 epoch、control_revision 与 ACTIVE
状态（见 OperationStore.check_authority_in），任何一次检查失败都禁止 snapshot/PREPARED/send。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

from infra.clock import Clock, SystemClock
from storage.database import Database, StorageError
from storage.operation_store import (
    AcquireOutcome,
    FinalizeOutcome,
    OperationStore,
    PrepareOutcome,
)

_SEND_KINDS = ("INITIAL_SEND", "CONTINUE")


class DispatchError(StorageError):
    """调度系统级失败（proposal 非法、事务故障等系统异常）。"""

    code = "dispatch_error"


class SendTransportError(Exception):
    """发送层可预期异常基类（不是业务结果）。"""


class SnapshotFailure(SendTransportError):
    """发送前只读快照失败：真正的副作用 POST 尚未开始，可安全声称未发送。"""


class TransportTimeoutError(SendTransportError):
    """客户端超时：远端可能已接受，不得自动重试。"""


class SendOutcome(str, Enum):
    ACCEPTED = "accepted"        # 明确接受
    REJECTED = "rejected"        # 明确未派发
    UNKNOWN = "unknown"          # 结果不明（超时/证据不足）


@dataclass(frozen=True, slots=True)
class SendAttempt:
    """一次 send_once 的结构化结果。"""

    outcome: SendOutcome
    remote_user_id: str | None = None
    evidence: dict = None  # type: ignore[assignment]


class SendTransport(Protocol):
    """发送层合同：只读快照 + 单次 POST。禁止 requests / 自动重试。"""

    def capture_pre_send_snapshot(
        self, *, endpoint: str, session_id: str, task_key: str
    ) -> dict: ...

    def send_once(
        self, *, endpoint: str, session_id: str, prompt_text: str, operation_id: str
    ) -> SendAttempt: ...


class DispatchOutcome(str, Enum):
    """dispatch 业务结果（稳定值，禁止中文字符串匹配）。

    ACCEPTED/REJECTED/UNKNOWN：真实发送结论；其余为拒绝/收敛类结论，
    任何拒绝结论都保证 send_once 调用为 0。
    """

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    PREPARED = "prepared"                    # 已登记可发送（尚未获得发送权）
    SENDING = "sending"                      # 已取得唯一发送权（PREPARED→SENDING 已提交）
    SEND_RIGHT_REVOKED = "send_right_revoked"  # 权威条件已失效（停止/epoch 变化先赢）
    PRECHECK_FAILED = "precheck_failed"      # 发送前快照失败（O02），send=0
    SESSION_UNRESOLVED = "session_unresolved"  # 依赖 session 但尚未解析（T10 不偷偷建会话）
    ALREADY_EXISTS = "already_exists"        # 同 key 同身份幂等收敛：读取既有操作，不重发
    KEY_CONFLICT = "key_conflict"            # 同 key 异身份：拒绝，原 operation 不修改
    SESSION_HAS_UNRESOLVED_OPERATION = "session_has_unresolved_operation"


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """dispatch 结果：调用方依据 outcome + 结构化字段判断。"""

    outcome: DispatchOutcome
    operation_key: str
    operation_id: str | None = None
    state: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DispatchProposal:
    """一次发送提案：operation_key 为业务幂等键，由调用方携带（T10 不替未来 Recovery
    Coordinator 硬编码恢复键算法；只负责同 key → 单 operation → 单次发送权）。"""

    operation_key: str
    kind: str
    task_key: str
    attempt_id: str
    authority_epoch: int
    control_revision: int
    endpoint: str
    session_id: str | None
    project_key: str
    interruption_id: str | None = None
    prompt_body: str = ""

    def __post_init__(self) -> None:
        if not self.operation_key or not self.operation_key.strip():
            raise DispatchError(f"operation_key 不能为空：{self.operation_key!r}")
        if self.kind not in _SEND_KINDS:
            raise DispatchError(f"T10 仅处理 INITIAL_SEND/CONTINUE 发送：kind={self.kind!r}")
        for name in ("task_key", "attempt_id", "endpoint", "project_key"):
            value = getattr(self, name)
            if not value or not value.strip():
                raise DispatchError(f"DispatchProposal.{name} 不能为空：{value!r}")

    def identity_tuple(self) -> tuple:
        return (
            self.kind, self.task_key, self.attempt_id, self.authority_epoch,
            self.control_revision, self.endpoint, self.session_id,
            self.project_key, self.interruption_id,
        )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def derive_operation_id(operation_key: str) -> str:
    """从 operation_key 稳定派生 operation_id（主规格 07.2③ / T10 方案 A）。

    并发重复 Proposal 使用同一 operation_key 时必然得到同一 operation_id，
    不会因随机 ID 不同导致 prompt_hash 误判为 CONFLICT。
    """
    return f"op-{sha256_hex(operation_key)[:32]}"


def _wire_markers(*, task_key: str, attempt_id: str, operation_id: str) -> str:
    return (
        f"[AI_RELAY_TASK_ID: {task_key}]\n"
        f"[AI_RELAY_ATTEMPT_ID: {attempt_id}]\n"
        f"[AI_RELAY_OPERATION_ID: {operation_id}]"
    )


def build_wire_prompt(*, task_key: str, attempt_id: str, operation_id: str, body: str) -> str:
    """在实际准备发送的 wire prompt 后追加独立 task/attempt/operation 标记（07.2③）。

    标记只辅助归属；原始业务正文仍保存在 tasks 表，不被标记覆盖。
    资料未给逐字固定格式，采用最小确定格式并在测试与报告中明确。
    """
    marker = f"\n\n{_wire_markers(task_key=task_key, attempt_id=attempt_id, operation_id=operation_id)}"
    return body + marker


def _utc_iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat()


class DispatchService:
    """两阶段发送调度：可以分步调用（prepare / acquire_send_right），也提供
    dispatch_once 串联。network/FakeTransport 调用永远不在事务内。
    """

    def __init__(
        self,
        db: Database,
        *,
        operation_store: OperationStore | None = None,
        clock: Clock | None = None,
        operation_id_factory=None,
    ) -> None:
        self._db = db
        self._ops = operation_store if operation_store is not None else OperationStore(db)
        self._clock = clock if clock is not None else SystemClock()
        self._op_id_factory = operation_id_factory or derive_operation_id

    # ---------------------------------------------------------------- phases

    def prepare(
        self,
        proposal: DispatchProposal,
        *,
        transport: SendTransport,
        now: datetime | None = None,
    ) -> DispatchResult:
        """第一次权威检查 → 只读快照（事务外）→ 第二次权威检查 + 写 PREPARED。

        O02：快照失败时 send 调用为 0、operations 为 0（记 PRECHECK_FAILED，不记 UNKNOWN）。
        """
        now_iso = _utc_iso(now if now is not None else self._clock.now())
        existing = self._ops.read_by_key(proposal.operation_key)
        if existing is not None:
            if existing.identity_tuple() != proposal.identity_tuple():
                return DispatchResult(
                    outcome=DispatchOutcome.KEY_CONFLICT,
                    operation_key=proposal.operation_key,
                    operation_id=existing.operation_id,
                    state=existing.state,
                    detail="同 operation_key 但身份（kind/task/attempt/epoch/revision/session）不一致",
                )
            if existing.state != "PREPARED":
                return DispatchResult(
                    outcome=DispatchOutcome.ALREADY_EXISTS,
                    operation_key=proposal.operation_key,
                    operation_id=existing.operation_id,
                    state=existing.state,
                    detail="同 operation_key 已有未决/终态操作：不做二次发送",
                )
            return DispatchResult(
                outcome=DispatchOutcome.PREPARED,
                operation_key=proposal.operation_key,
                operation_id=existing.operation_id,
                state=existing.state,
                detail="已有同身份 PREPARED operation，幂等复用",
            )
        if proposal.session_id is None:
            return DispatchResult(
                outcome=DispatchOutcome.SESSION_UNRESOLVED,
                operation_key=proposal.operation_key,
                detail="T10 不创建/轮换会话：发送依赖 session_id 但尚未解析，禁止以空会话发送",
            )
        first = self._ops.check_authority_in(self._db.connection, proposal)
        if not first.ok:
            return DispatchResult(
                outcome=DispatchOutcome.SEND_RIGHT_REVOKED,
                operation_key=proposal.operation_key,
                detail=f"首次权威检查未通过：{first.detail}",
            )
        try:
            snapshot = transport.capture_pre_send_snapshot(
                endpoint=proposal.endpoint, session_id=proposal.session_id,
                task_key=proposal.task_key,
            )
        except SnapshotFailure:
            return DispatchResult(
                outcome=DispatchOutcome.PRECHECK_FAILED,
                operation_key=proposal.operation_key,
                detail="发送前只读快照失败：副作用 POST 尚未开始，明确未发送（O02）",
            )
        operation_id = self._op_id_factory(proposal.operation_key)
        prompt = build_wire_prompt(
            task_key=proposal.task_key, attempt_id=proposal.attempt_id,
            operation_id=operation_id, body=proposal.prompt_body,
        )
        prompt_hash = sha256_hex(prompt)
        snapshot_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        try:
            with self._db.transaction():
                conn = self._db.connection
                second = self._ops.check_authority_in(conn, proposal)
                if not second.ok:
                    return DispatchResult(
                        outcome=DispatchOutcome.SEND_RIGHT_REVOKED,
                        operation_key=proposal.operation_key,
                        detail=f"PREPARED 落库时权威检查未通过：{second.detail}",
                    )
                prepared = self._ops.prepare_in(
                    conn,
                    proposal=proposal,
                    operation_id=operation_id,
                    pre_snapshot_json=snapshot_json,
                    prompt_text=prompt,
                    prompt_hash=prompt_hash,
                    created_at=now_iso,
                )
                if prepared.outcome is PrepareOutcome.CONFLICT:
                    return DispatchResult(
                        outcome=DispatchOutcome.KEY_CONFLICT,
                        operation_key=proposal.operation_key,
                        operation_id=prepared.operation_id,
                        state=prepared.record.state if prepared.record else None,
                        detail="同 operation_key 但身份不一致：拒绝覆盖原 operation",
                    )
                assert prepared.record is not None
                return DispatchResult(
                    outcome=(DispatchOutcome.PREPARED
                             if prepared.outcome is PrepareOutcome.CREATED
                             else DispatchOutcome.ALREADY_EXISTS),
                    operation_key=proposal.operation_key,
                    operation_id=prepared.operation_id,
                    state=prepared.record.state,
                )
        except sqlite3.IntegrityError as exc:
            raise DispatchError(f"PREPARED 事务故障注入/冲突：{exc}") from exc

    def acquire_send_right(
        self, proposal: DispatchProposal, *, now: datetime | None = None
    ) -> DispatchResult:
        """第三次权威检查 + CAS PREPARED→SENDING（事务）。只有取得成功的一方有发送权。

        停止先赢：epoch/control_revision/owner 已变化 → SEND_RIGHT_REVOKED（保持 PREPARED，
        T10 不做 CANCELLED 写入，见模块说明），send 调用为 0。
        """
        now_iso = _utc_iso(now if now is not None else self._clock.now())
        existing = self._ops.read_by_key(proposal.operation_key)
        if existing is None:
            return DispatchResult(
                outcome=DispatchOutcome.SEND_RIGHT_REVOKED,
                operation_key=proposal.operation_key,
                detail="operation 不存在：没有可取得发送权的 PREPARED 操作",
            )
        if existing.identity_tuple() != proposal.identity_tuple():
            return DispatchResult(
                outcome=DispatchOutcome.KEY_CONFLICT,
                operation_key=proposal.operation_key,
                operation_id=existing.operation_id,
                state=existing.state,
            )
        if existing.state != "PREPARED":
            return DispatchResult(
                outcome=DispatchOutcome.ALREADY_EXISTS,
                operation_key=proposal.operation_key,
                operation_id=existing.operation_id,
                state=existing.state,
                detail="同 key 操作已非 PREPARED：发送权已被占用或已是终态",
            )
        try:
            with self._db.transaction():
                conn = self._db.connection
                third = self._ops.check_authority_in(conn, proposal)
                if not third.ok:
                    return DispatchResult(
                        outcome=DispatchOutcome.SEND_RIGHT_REVOKED,
                        operation_key=proposal.operation_key,
                        operation_id=existing.operation_id,
                        detail=f"取得发送权前权威检查未通过：{third.detail}",
                    )
                acquired = self._ops.acquire_send_right_in(
                    conn, proposal=proposal,
                    operation_id=existing.operation_id, now=now_iso,
                )
                if acquired.outcome is AcquireOutcome.SESSION_HAS_UNRESOLVED:
                    return DispatchResult(
                        outcome=DispatchOutcome.SESSION_HAS_UNRESOLVED_OPERATION,
                        operation_key=proposal.operation_key,
                        operation_id=existing.operation_id,
                        detail="同 endpoint/session 已存在未决发送（SENDING/UNKNOWN），阻塞第二个发送",
                    )
                if acquired.outcome is not AcquireOutcome.GRANTED:
                    return DispatchResult(
                        outcome=DispatchOutcome.ALREADY_EXISTS,
                        operation_key=proposal.operation_key,
                        operation_id=acquired.operation_id,
                        state=acquired.state,
                        detail="发送权 CAS 未成功（另一调用方先取得或已终态）",
                    )
                return DispatchResult(
                    outcome=DispatchOutcome.SENDING,
                    operation_key=proposal.operation_key,
                    operation_id=acquired.operation_id,
                    state=acquired.state,
                    detail="PREPARED→SENDING 已提交：本调用方获得唯一发送权",
                )
        except sqlite3.IntegrityError as exc:
            return DispatchResult(
                outcome=DispatchOutcome.SESSION_HAS_UNRESOLVED_OPERATION,
                operation_key=proposal.operation_key,
                detail=f"并发同 session 未决发送已越过预检查，由部分唯一索引拦下：{exc}",
            )

    def send_prepared(
        self,
        proposal: DispatchProposal,
        *,
        operation_key: str,
        transport: SendTransport,
        now: datetime | None = None,
    ) -> DispatchResult:
        """执行这一份已获权的 send_once（事务外）并落最终态（事务）。

        发送权先赢后，即使停止/epoch 变化随后到达，仍按“可能已产生远端副作用”处理：
        完全不重发，若远端未给结论则记 UNKNOWN。
        """
        now_iso = _utc_iso(now if now is not None else self._clock.now())
        existing = self._ops.read_by_key(operation_key)
        if existing is None:
            return DispatchResult(
                outcome=DispatchOutcome.SEND_RIGHT_REVOKED,
                operation_key=operation_key,
            )
        if existing.state != "SENDING":
            return DispatchResult(
                outcome=DispatchOutcome.SEND_RIGHT_REVOKED,
                operation_key=operation_key,
                operation_id=existing.operation_id,
                state=existing.state,
                detail="操作当前不是 SENDING：必须先 acquire_send_right 成功",
            )
        try:
            attempt = transport.send_once(
                endpoint=existing.endpoint,
                session_id=existing.session_id,
                prompt_text=existing.prompt_text,
                operation_id=existing.operation_id,
            )
        except Exception as exc:
            unknown = SendAttempt(outcome=SendOutcome.UNKNOWN, evidence={})
            return self._finalize_sent(
                proposal, existing.operation_id, unknown, now_iso,
                detail=f"send_once 异常，远端结果不明：{type(exc).__name__}: {exc}",
            )
        return self._finalize_sent(proposal, existing.operation_id, attempt, now_iso)

    # ---------------------------------------------------------------- convenience

    def dispatch_once(
        self,
        proposal: DispatchProposal,
        *,
        transport: SendTransport,
        now: datetime | None = None,
    ) -> DispatchResult:
        """串联 prepare → acquire_send_right → send_prepared；已存在终态/UNDONE 时幂等收敛。

        同 operation_key 再次 dispatch：ACCEPTED/REJECTED/UNKNOWN → ALREADY_EXISTS（零发送）；
        SENDING（残留，如进程重启）→ 收敛为 UNKNOWN 且零发送；PREPARED → 继续取得发送权。
        更新 pending 的 now 使所有时间戳一致。
        """
        existing = self._ops.read_by_key(proposal.operation_key)
        if existing is not None and existing.identity_tuple() != proposal.identity_tuple():
            return DispatchResult(
                outcome=DispatchOutcome.KEY_CONFLICT,
                operation_key=proposal.operation_key,
                operation_id=existing.operation_id,
                state=existing.state,
            )
        if existing is not None and existing.state in ("ACCEPTED", "REJECTED", "UNKNOWN"):
            return DispatchResult(
                outcome=DispatchOutcome.ALREADY_EXISTS,
                operation_key=proposal.operation_key,
                operation_id=existing.operation_id,
                state=existing.state,
                detail="同 operation_key 已有终态/UNKNOWN 结论：绝不第二次 POST",
            )
        if existing is not None and existing.state == "SENDING":
            return self._recover_stale_sending(existing, now)
        prepared = self.prepare(proposal, transport=transport, now=now)
        if prepared.outcome not in (DispatchOutcome.PREPARED,):
            return prepared
        granted = self.acquire_send_right(proposal, now=now)
        if granted.outcome is not DispatchOutcome.SENDING:
            return granted
        return self.send_prepared(proposal, operation_key=proposal.operation_key,
                                  transport=transport, now=now)

    # ---------------------------------------------------------------- internals

    def _finalize_sent(
        self,
        proposal: DispatchProposal,
        operation_id: str,
        attempt: SendAttempt,
        now_iso: str,
        *,
        detail: str = "",
    ) -> DispatchResult:
        target = {
            SendOutcome.ACCEPTED: DispatchOutcome.ACCEPTED,
            SendOutcome.REJECTED: DispatchOutcome.REJECTED,
            SendOutcome.UNKNOWN: DispatchOutcome.UNKNOWN,
        }[attempt.outcome]
        evidence = dict(attempt.evidence or {})
        if detail:
            evidence.setdefault("transport_error", detail)
        try:
            with self._db.transaction():
                conn = self._db.connection
                final = self._ops.finalize_in(
                    conn,
                    proposal=proposal,
                    operation_id=operation_id,
                    target_state=target.value.upper(),
                    remote_user_id=attempt.remote_user_id,
                    evidence_json=json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                    finalized_at=now_iso,
                    now=now_iso,
                )
                if final.outcome is FinalizeOutcome.FINALIZED:
                    return DispatchResult(
                        outcome=target, operation_key=proposal.operation_key,
                        operation_id=final.operation_id, state=final.state,
                    )
                if final.outcome is FinalizeOutcome.ALREADY_FINAL:
                    return DispatchResult(
                        outcome=target, operation_key=proposal.operation_key,
                        operation_id=final.operation_id, state=final.state,
                    )
                current = self._ops.read_by_key(proposal.operation_key)
                return DispatchResult(
                    outcome=DispatchOutcome.ALREADY_EXISTS if final.outcome is FinalizeOutcome.CAS_LOST
                    else DispatchOutcome.SEND_RIGHT_REVOKED,
                    operation_key=proposal.operation_key,
                    operation_id=final.operation_id,
                    state=current.state if current else final.state,
                    detail="最终态 CAS 未成功，已回读当前状态（对账后可收敛）",
                )
        except sqlite3.IntegrityError as exc:
            raise DispatchError(f"最终态事务故障注入/冲突：{exc}") from exc

    def _recover_stale_sending(self, existing, now: datetime | None) -> DispatchResult:
        """进程重启看到 SENDING：视为可能已发送 → 收敛 UNKNOWN，零发送（主规格 07.2⑥）。"""
        now_iso = _utc_iso(now if now is not None else self._clock.now())
        with self._db.transaction():
            conn = self._db.connection
            self._ops.recover_sending_as_unknown_in(
                conn,
                operation_key=existing.operation_key,
                operation_id=existing.operation_id,
                now=now_iso,
            )
        return DispatchResult(
            outcome=DispatchOutcome.ALREADY_EXISTS,
            operation_key=existing.operation_key,
            operation_id=existing.operation_id,
            state="UNKNOWN",
            detail="残留 SENDING 已作为“可能已发送”收敛到 UNKNOWN；不调用 transport",
        )