"""AI Relay B V3.0：OpenChamber UNKNOWN 发送对账协调器（T22-04）。

对既有真实 send operation（state=UNKNOWN）做历史事实对账：
- 只处理 INITIAL_SEND / CONTINUE；CREATE_SESSION / SET_PERMISSION 等 → UNSUPPORTED_KIND；
- 只处理 state=UNKNOWN；其他 state → NOT_UNKNOWN / ALREADY_ACCEPTED，0 GET；
- ledger 必备字段（session_id / remote_user_id(planned) / evidence_json 可解析）缺失
  → INVALID_LEDGER，0 GET，不现场生成 messageID；
- GET observation 严格位于任何 SQLite 事务之外（reader 注入，单轮、不 retry、0 POST）；
- 精确 planned user message 被观察到 → SQLite 事务内 CAS UNKNOWN→ACCEPTED
  （复用 OperationStore.finalize_in，不新建第二套状态机）；
- 无精确证据 → 保持 UNKNOWN；absence 不等于 REJECTED，本层绝不自动 REJECTED；
- 并发防护：另一 worker 已收敛（ACCEPTED/REJECTED 等）→ ALREADY_ACCEPTED /
  CONCURRENT_CHANGE，绝不覆盖、绝不反向转换。

Authority 语义：对账不产生远端 side effect，只是把已发生的历史事实收敛；
因此不复用“发送权”检查来阻止对账，但用 operation_id + UNKNOWN CAS 防止迟到
reconciler 覆盖已被其他路径收敛的 operation。本模块不修改 task/attempt/
result/lease 状态，不创建 Result，不重发 prompt。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from adapters.openchamber_reconciliation import (
    OpenChamberReconciliationInputError,
    OpenChamberUnknownReconciliationReader,
    ReconciliationObservation,
)
from infra.clock import Clock, SystemClock
from storage.database import Database
from storage.operation_store import FinalizeOutcome, OperationStore

_SEND_KINDS = ("INITIAL_SEND", "CONTINUE")


class ReconciliationOutcome(str, Enum):
    """对账稳定结果枚举（与 T22-04 §21 对齐）。"""

    ACCEPTED_CONFIRMED = "accepted_confirmed"
    STILL_UNKNOWN = "still_unknown"
    ALREADY_ACCEPTED = "already_accepted"
    NOT_UNKNOWN = "not_unknown"
    NOT_FOUND = "not_found"
    UNSUPPORTED_KIND = "unsupported_kind"
    INVALID_LEDGER = "invalid_ledger"
    READ_FAILED = "read_failed"
    CONCURRENT_CHANGE = "concurrent_change"


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """一次 reconcile 的结构化结果。detail 绝不包含 token / Authorization / message body。"""

    outcome: ReconciliationOutcome
    operation_id: str | None
    state: str | None
    exact_user_match_count: int = 0
    assistant_parent_match_count: int = 0
    detail: str = ""


def _utc_iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat()


class OpenChamberUnknownReconciler:
    """UNKNOWN send operation 的 GET-only reconciliation 协调器。"""

    def __init__(
        self,
        db: Database,
        *,
        reader: OpenChamberUnknownReconciliationReader,
        operation_store: OperationStore | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._db = db
        self._reader = reader
        self._ops = operation_store if operation_store is not None else OperationStore(db)
        self._clock = clock if clock is not None else SystemClock()

    # ---------------------------------------------------------------- reconcile

    def reconcile(self, operation_id: str) -> ReconciliationResult:
        """按 OperationId 执行单轮对账（一次调用一轮 observation，不循环）。"""
        record = self._ops.read_by_id(operation_id)
        if record is None:
            return ReconciliationResult(
                ReconciliationOutcome.NOT_FOUND,
                operation_id=operation_id,
                detail="operation 不存在",
            )
        if record.kind not in _SEND_KINDS:
            return ReconciliationResult(
                ReconciliationOutcome.UNSUPPORTED_KIND,
                operation_id=operation_id,
                state=record.state,
                detail=f"kind={record.kind} 非发送类，不处理（0 HTTP）",
            )
        if record.state == "ACCEPTED":
            return ReconciliationResult(
                ReconciliationOutcome.ALREADY_ACCEPTED,
                operation_id=operation_id,
                state=record.state,
                detail="已是 ACCEPTED：无需对账",
            )
        if record.state != "UNKNOWN":
            return ReconciliationResult(
                ReconciliationOutcome.NOT_UNKNOWN,
                operation_id=operation_id,
                state=record.state,
                detail=f"state={record.state} 不为 UNKNOWN：不发 reconciliation GET",
            )
        ledger_err = self._ledger_error(record)
        if ledger_err is not None:
            return ReconciliationResult(
                ReconciliationOutcome.INVALID_LEDGER,
                operation_id=operation_id,
                state=record.state,
                detail=ledger_err,
            )

        try:
            obs = self._reader.observe_once(
                endpoint=record.endpoint,
                session_id=record.session_id,
                expected_user_message_id=record.remote_user_id,
            )
        except OpenChamberReconciliationInputError as exc:
            return ReconciliationResult(
                ReconciliationOutcome.INVALID_LEDGER,
                operation_id=operation_id,
                state=record.state,
                detail=f"endpoint/身份与冻结目标不一致（{exc.kind}）：fail closed，0 HTTP",
            )
        if obs.required_read_error is not None:
            return ReconciliationResult(
                ReconciliationOutcome.READ_FAILED,
                operation_id=operation_id,
                state=record.state,
                exact_user_match_count=obs.exact_user_match_count,
                assistant_parent_match_count=obs.assistant_parent_match_count,
                detail=f"必要 readback 失败（{obs.required_read_error}）：仍 UNKNOWN，0 POST",
            )
        if not obs.exact_user_message_observed:
            return ReconciliationResult(
                ReconciliationOutcome.STILL_UNKNOWN,
                operation_id=operation_id,
                state=record.state,
                exact_user_match_count=obs.exact_user_match_count,
                assistant_parent_match_count=obs.assistant_parent_match_count,
                detail="无精确 planned user message 投递证据：absence 不等于 REJECTED，保持 UNKNOWN",
            )
        return self._finalize_accepted(record, obs)

    # ---------------------------------------------------------------- internals

    def _ledger_error(self, record) -> str | None:
        """T22-04 §18：UNKNOWN send operation 必备字段校验（0 HTTP，不现场生成 id）。"""
        if not isinstance(record.session_id, str) or not record.session_id.strip():
            return "session_id 缺失：INVALID_LEDGER（0 HTTP）"
        if not isinstance(record.remote_user_id, str) or not record.remote_user_id.strip():
            return "remote_user_id(planned) 缺失：INVALID_LEDGER（0 HTTP）"
        try:
            json.loads(record.evidence_json or "{}")
        except (TypeError, ValueError):
            return "evidence_json 非法 JSON：INVALID_LEDGER（0 HTTP）"
        return None

    def _finalize_accepted(self, record, obs: ReconciliationObservation) -> ReconciliationResult:
        """精确 identity 确认投递：SQLite 事务内 CAS UNKNOWN→ACCEPTED。

        evidence 合并原 evidence（不擦除 timeout/transport 历史），增加最小
        unknown_reconciliation 字段；不存 message body。planned remote_user_id、
        prompt、operation_id、operation_key 一律不变。
        """
        now_iso = _utc_iso(self._clock.now())
        try:
            old = json.loads(record.evidence_json or "{}")
        except (TypeError, ValueError):
            old = {}
        merged = dict(old) if isinstance(old, dict) else {}
        merged["unknown_reconciliation"] = {
            "decision": "accepted_confirmed",
            "source": "GET_ONLY",
            "session_exists": True,
            "message_count": obs.message_count,
            "exact_id_match_count": obs.exact_id_match_count,
            "exact_user_match_count": obs.exact_user_match_count,
            "assistant_parent_match_count": obs.assistant_parent_match_count,
            "status_entry_present": obs.status_entry_present,
        }
        if obs.status_read_error is not None:
            merged["unknown_reconciliation"]["status_read_error"] = obs.status_read_error
        evidence_json = json.dumps(merged, ensure_ascii=False, sort_keys=True)
        try:
            with self._db.transaction():
                conn = self._db.connection
                final = self._ops.finalize_in(
                    conn,
                    proposal=record,
                    operation_id=record.operation_id,
                    target_state="ACCEPTED",
                    evidence_json=evidence_json,
                    finalized_at=now_iso,
                    now=now_iso,
                )
        except sqlite3.IntegrityError as exc:
            return ReconciliationResult(
                ReconciliationOutcome.CONCURRENT_CHANGE,
                operation_id=record.operation_id,
                state=record.state,
                detail=f"finalize 事务故障：不覆盖，保持原状态（{type(exc).__name__}）",
            )
        if final.outcome is FinalizeOutcome.FINALIZED:
            return ReconciliationResult(
                ReconciliationOutcome.ACCEPTED_CONFIRMED,
                operation_id=final.operation_id,
                state=final.state,
                exact_user_match_count=obs.exact_user_match_count,
                assistant_parent_match_count=obs.assistant_parent_match_count,
                detail="精确 planned user message 已持久观察到：UNKNOWN→ACCEPTED（不等于 completed）",
            )
        if final.outcome is FinalizeOutcome.ALREADY_FINAL and final.state == "ACCEPTED":
            return ReconciliationResult(
                ReconciliationOutcome.ALREADY_ACCEPTED,
                operation_id=final.operation_id,
                state=final.state,
                detail="并发下另一 worker 已把该 operation 收敛为 ACCEPTED（幂等）",
            )
        current = self._ops.read_by_key(record.operation_key)
        cur_state = current.state if current is not None else final.state
        if cur_state == "ACCEPTED":
            return ReconciliationResult(
                ReconciliationOutcome.ALREADY_ACCEPTED,
                operation_id=record.operation_id,
                state=cur_state,
                detail="并发下已回读为 ACCEPTED：不覆盖",
            )
        return ReconciliationResult(
            ReconciliationOutcome.CONCURRENT_CHANGE,
            operation_id=record.operation_id,
            state=cur_state,
            detail="并发下状态已变化（不覆盖、不反向转换）：回读后保持现状",
        )