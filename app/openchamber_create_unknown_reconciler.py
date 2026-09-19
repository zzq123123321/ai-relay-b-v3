"""AI Relay B V3.0：OpenChamber UNKNOWN CREATE_SESSION 对账协调器（T22-05B）。

对既有真实 CREATE_SESSION operation（state=UNKNOWN）做 GET-only after-before
session 差集对账：
- 只处理 kind=CREATE_SESSION；INITIAL_SEND / CONTINUE / SET_PERMISSION 等 →
  UNSUPPORTED_KIND，0 GET；
- 只处理 state=UNKNOWN；ACCEPTED → ALREADY_ACCEPTED，其他 state → NOT_UNKNOWN，
  全部 0 GET；本层不负责把 SENDING 转 UNKNOWN（T22-05A restart 路径已负责）；
- ledger 必备字段（operation_key / endpoint nonblank、session_id/remote_user_id
  恒 NULL、pre_snapshot_json 与 evidence_json 可解析为对象）+ authoritative
  baseline（directory / PROJECT_ROTATING / binding_revision / create_title /
  session_ids_before 非空去重 / session_count_before 精确）任一异常 →
  INVALID_LEDGER，0 GET，不修补、不现场生成数据；
- GET observation 严格位于任何 SQLite 事务之外（reader 注入，单轮单 GET、不
  retry、0 POST）；authoritative before 一律来自 pre_snapshot_json，绝不从
  当前 SettingsService 读取；
- 核心差集：before ⊆ after 且 after-before 恰好一个合法 ses_/sess_ session
  → UNKNOWN→ACCEPTED；出现任何 missing old id / zero new / multiple new /
  非法 new id → STILL_UNKNOWN，绝不自动 REJECTED，绝不把新 session 强行归属；
- 不使用 title / slug / newest / timestamp / list-last 任何启发式归属；
- 收敛 ACCEPTED 时：原 evidence 完整保留，顶层追加 created_session_id（T22-05A
  ACCEPTED 快速路径据此恢复 real id），并附加最小 unknown_create_reconciliation
  嵌套块（source=GET_ONLY_SESSION_DIFF + 计数）；operations.session_id /
  remote_user_id / pre_snapshot_json 一律不改；
- 并发防护：另一 worker 已 UNKNOWN→ACCEPTED → ALREADY_ACCEPTED（幂等不覆盖）；
  已变成 REJECTED / 身份异常 / 消失 → CONCURRENT_CHANGE，绝不反向转换。

Authority 语义（与 T22-04 一致）：对账是历史事实确认，不产生远端副作用，
因此不重新检查 task ACTIVE / attempt OPEN / lease 状态；即使任务后来 STOP，
若历史 create 已确实发生，也应允许本层只以 operation_id + kind + state +
CAS 收敛。本模块不修改 task/attempt/lease/result/binding/ExecutionSettingsSnapshot。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from adapters.openchamber import is_valid_session_id
from adapters.openchamber_create_reconciliation import (
    OpenChamberCreateReconciliationInputError,
    OpenChamberUnknownCreateReconciliationReader,
)
from core.domain import SessionBindingMode
from infra.clock import Clock, SystemClock
from storage.database import Database
from storage.operation_store import FinalizeOutcome, OperationStore

CREATE_SESSION_KIND = "CREATE_SESSION"
_PROJECT_ROTATING = SessionBindingMode.PROJECT_ROTATING.value


class CreateReconciliationOutcome(str, Enum):
    """对账稳定结果枚举（与 T22-04 §21 / T22-05B §24 对齐）。"""

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
class CreateSessionDelta:
    """T22-05B §17：after-before 精确差集（顺序不作为 identity 语义）。

    clean_single_new_session 仅当 missing_old 为空、new 恰好一个且该 id 合法。
    """

    new_session_ids: tuple[str, ...] = ()
    missing_old_session_ids: tuple[str, ...] = ()
    clean_single_new_session: bool = False
    candidate_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class CreateReconciliationResult:
    """一次 reconcile 的结构化结果。detail 绝不包含 token / Authorization / raw body。"""

    outcome: CreateReconciliationOutcome
    operation_id: str | None
    state: str | None = None
    created_session_id: str | None = None
    new_session_count: int = 0
    missing_old_session_count: int = 0
    detail: str = ""


def _utc_iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat()


class OpenChamberUnknownCreateReconciler:
    """UNKNOWN CREATE_SESSION operation 的 GET-only after-before reconciliation。"""

    def __init__(
        self,
        db: Database,
        *,
        reader: OpenChamberUnknownCreateReconciliationReader,
        operation_store: OperationStore | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._db = db
        self._reader = reader
        self._ops = operation_store if operation_store is not None else OperationStore(db)
        self._clock = clock if clock is not None else SystemClock()

    # ---------------------------------------------------------------- reconcile

    def reconcile(self, operation_id: str) -> CreateReconciliationResult:
        """按 OperationId 执行单轮对账（一次调用一轮 GET observation，不循环）。"""
        record = self._ops.read_by_id(operation_id)
        if record is None:
            return CreateReconciliationResult(
                CreateReconciliationOutcome.NOT_FOUND,
                operation_id=operation_id,
                detail="operation 不存在",
            )
        if record.kind != CREATE_SESSION_KIND:
            return CreateReconciliationResult(
                CreateReconciliationOutcome.UNSUPPORTED_KIND,
                operation_id=operation_id,
                state=record.state,
                detail=f"kind={record.kind} 非 CREATE_SESSION，不处理（0 HTTP）",
            )
        if record.state == "ACCEPTED":
            return CreateReconciliationResult(
                CreateReconciliationOutcome.ALREADY_ACCEPTED,
                operation_id=operation_id,
                state=record.state,
                detail="已是 ACCEPTED：无需对账",
            )
        if record.state != "UNKNOWN":
            return CreateReconciliationResult(
                CreateReconciliationOutcome.NOT_UNKNOWN,
                operation_id=operation_id,
                state=record.state,
                detail=f"state={record.state} 不为 UNKNOWN：不发 reconciliation GET",
            )
        baseline, ledger_err = self._ledger_and_baseline(record)
        if ledger_err is not None:
            return CreateReconciliationResult(
                CreateReconciliationOutcome.INVALID_LEDGER,
                operation_id=operation_id,
                state=record.state,
                detail=ledger_err,
            )

        try:
            obs = self._reader.observe_sessions_once(
                endpoint=record.endpoint,
                directory=baseline["directory"],
            )
        except OpenChamberCreateReconciliationInputError as exc:
            return CreateReconciliationResult(
                CreateReconciliationOutcome.INVALID_LEDGER,
                operation_id=operation_id,
                state=record.state,
                detail=f"endpoint/身份与冻结目标不一致（{exc.kind}）：fail closed，0 HTTP",
            )
        if obs.read_error is not None:
            return CreateReconciliationResult(
                CreateReconciliationOutcome.READ_FAILED,
                operation_id=operation_id,
                state=record.state,
                detail=f"session list GET 失败（{obs.read_error}）：仍 UNKNOWN，0 POST",
            )
        delta = self._delta(baseline, obs)
        if not delta.clean_single_new_session:
            return CreateReconciliationResult(
                CreateReconciliationOutcome.STILL_UNKNOWN,
                operation_id=operation_id,
                state=record.state,
                new_session_count=len(delta.new_session_ids),
                missing_old_session_count=len(delta.missing_old_session_ids),
                detail=(
                    "after-before 差集不满足单独新增唯一合法 session"
                    "（missing old / 0 new / >1 new / 非法 new 任一出现）："
                    "absence 不等于 REJECTED，保持 UNKNOWN"
                ),
            )
        return self._finalize_accepted(
            record, baseline, candidate=delta.candidate_session_id, after=obs
        )

    # ---------------------------------------------------------------- internals

    def _ledger_and_baseline(self, record) -> tuple[dict, str | None]:
        """T22-05B §9-§10：ledger 必备字段 + authoritative baseline 校验。

        任一异常 → (None, error)；不修补、不现场生成数据。
        """
        if not isinstance(record.operation_key, str) or not record.operation_key.strip():
            return {}, "operation_key 缺失：INVALID_LEDGER（0 HTTP）"
        if not isinstance(record.endpoint, str) or not record.endpoint.strip():
            return {}, "endpoint 缺失：INVALID_LEDGER（0 HTTP）"
        if record.session_id is not None:
            return {}, "CREATE_SESSION 的 operations.session_id 必须保持 NULL：INVALID_LEDGER（0 HTTP）"
        if record.remote_user_id is not None:
            return {}, "CREATE_SESSION 的 operations.remote_user_id 必须保持 NULL：INVALID_LEDGER（0 HTTP）"
        try:
            pre = json.loads(record.pre_snapshot_json or "{}")
        except (TypeError, ValueError):
            return {}, "pre_snapshot_json 非法 JSON：INVALID_LEDGER（0 HTTP）"
        try:
            json.loads(record.evidence_json or "{}")
        except (TypeError, ValueError):
            return {}, "evidence_json 非法 JSON：INVALID_LEDGER（0 HTTP）"
        if not isinstance(pre, dict):
            return {}, "pre_snapshot_json 顶层不是对象：INVALID_LEDGER（0 HTTP）"
        error = self._baseline_error(pre)
        if error is not None:
            return {}, error
        return pre, None

    @staticmethod
    def _baseline_error(pre: dict) -> str | None:
        directory = pre.get("directory")
        if not isinstance(directory, str) or not directory.strip():
            return "baseline.directory 缺失/为空：INVALID_LEDGER（0 HTTP）"
        if pre.get("binding_mode") != _PROJECT_ROTATING:
            return "baseline.binding_mode 必须为 PROJECT_ROTATING：INVALID_LEDGER（0 HTTP）"
        if not isinstance(pre.get("binding_revision"), int):
            return "baseline.binding_revision 非法整数：INVALID_LEDGER（0 HTTP）"
        if not isinstance(pre.get("create_title"), str) or not pre["create_title"].strip():
            return "baseline.create_title 缺失/为空：INVALID_LEDGER（0 HTTP）"
        before = pre.get("session_ids_before")
        if not isinstance(before, list):
            return "baseline.session_ids_before 必须是 list：INVALID_LEDGER（0 HTTP）"
        for item in before:
            if not isinstance(item, str) or not item.strip():
                return "baseline.session_ids_before 存在空/非字符串项：INVALID_LEDGER（0 HTTP）"
        if len(set(before)) != len(before):
            return "baseline.session_ids_before 存在重复 id：INVALID_LEDGER（0 HTTP）"
        count = pre.get("session_count_before")
        if not isinstance(count, int) or count != len(before):
            return "baseline.session_count_before 与 session_ids_before 长度不一致：INVALID_LEDGER（0 HTTP）"
        return None

    def _delta(self, baseline: dict, obs) -> CreateSessionDelta:
        """after-before 精确差集（T22-05B §12-§13）。

        before 来自 ledger baseline，after 来自 GET observation；顺序不作为
        identity 语义。clean 仅当 before ⊆ after 且恰好一个新 id 且该新 id 是
        合法 ses_/sess_ session。
        """
        before = set(baseline["session_ids_before"])
        after = set(obs.session_ids_after)
        new_ids = tuple(sorted(after - before))
        missing_old = tuple(sorted(before - after))
        candidate: str | None = None
        if not missing_old and len(new_ids) == 1 and is_valid_session_id(new_ids[0]):
            candidate = new_ids[0]
        return CreateSessionDelta(
            new_session_ids=new_ids,
            missing_old_session_ids=missing_old,
            clean_single_new_session=(candidate is not None),
            candidate_session_id=candidate,
        )

    def _finalize_accepted(
        self, record, baseline: dict, *, candidate: str, after,
    ) -> CreateReconciliationResult:
        """clean exactly-one：SQLite 事务内 CAS UNKNOWN→ACCEPTED。

        evidence 合并原 evidence（不擦除 timeout/transport 历史 classification），
        顶层追加 created_session_id，附加最小 unknown_create_reconciliation；
        不存 raw body。operation identity 列一律不变。
        """
        now_iso = _utc_iso(self._clock.now())
        try:
            old = json.loads(record.evidence_json or "{}")
        except (TypeError, ValueError):
            old = {}
        merged = dict(old) if isinstance(old, dict) else {}
        merged["created_session_id"] = candidate
        merged["unknown_create_reconciliation"] = {
            "decision": "accepted_confirmed",
            "source": "GET_ONLY_SESSION_DIFF",
            "session_count_before": baseline["session_count_before"],
            "session_count_after": after.session_count_after,
            "new_session_count": 1,
            "missing_old_session_count": 0,
        }
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
            return CreateReconciliationResult(
                CreateReconciliationOutcome.CONCURRENT_CHANGE,
                operation_id=record.operation_id,
                state=record.state,
                detail=f"finalize 事务故障：不覆盖，保持原状态（{type(exc).__name__}）",
            )
        if final.outcome is FinalizeOutcome.FINALIZED:
            return CreateReconciliationResult(
                CreateReconciliationOutcome.ACCEPTED_CONFIRMED,
                operation_id=final.operation_id,
                state=final.state,
                created_session_id=candidate,
                new_session_count=1,
                missing_old_session_count=0,
                detail="after-before 精确差集确认唯一新增合法 session：UNKNOWN→ACCEPTED",
            )
        if final.outcome is FinalizeOutcome.ALREADY_FINAL and final.state == "ACCEPTED":
            return CreateReconciliationResult(
                CreateReconciliationOutcome.ALREADY_ACCEPTED,
                operation_id=final.operation_id,
                state=final.state,
                created_session_id=self._created_id_from_evidence(record) or candidate,
                detail="并发下另一 worker 已把该 operation 收敛为 ACCEPTED（幂等）",
            )
        current = self._ops.read_by_key(record.operation_key)
        cur_state = current.state if current is not None else final.state
        if cur_state == "ACCEPTED":
            return CreateReconciliationResult(
                CreateReconciliationOutcome.ALREADY_ACCEPTED,
                operation_id=record.operation_id,
                state=cur_state,
                created_session_id=(
                    self._created_id_from_evidence(current) if current is not None else None
                ),
                new_session_count=1,
                missing_old_session_count=0,
                detail="并发下已回读为 ACCEPTED：不覆盖",
            )
        return CreateReconciliationResult(
            CreateReconciliationOutcome.CONCURRENT_CHANGE,
            operation_id=record.operation_id,
            state=cur_state,
            detail="并发下状态已变化（不覆盖、不反向转换）：回读后保持现状",
        )

    @staticmethod
    def _created_id_from_evidence(record) -> str | None:
        value: Any = None
        try:
            evidence = json.loads(record.evidence_json or "{}")
        except (TypeError, ValueError):
            evidence = {}
        if isinstance(evidence, dict):
            value = evidence.get("created_session_id")
        if isinstance(value, str) and value.strip():
            return value
        return None