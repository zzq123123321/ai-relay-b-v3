"""AI Relay B V3.0：OpenChamber CREATE_SESSION 安全协调器（T22-05A）。

职责（T22-05A §7-§21）：在 Operation Ledger 之上实现“一次建会话写权”与 durable
pre-create baseline，任何确定性决策只产生 at most 一次 /api/session create POST：
- 同一 authoritative decision 收敛到同一 operation_key 同一 operation；并发/重复
  调用绝不产生第二个 POST（UNKNOWN 交给 T22-05B 对账，本模块绝不自动重发）；
- 三次权威检查：①首次在任何 GET 之前；②第二次在同一 PREPARED transaction 内把
  baseline（directory / binding_mode / binding_revision / session list 快照 /
  create_title）写进 operations.pre_snapshot_json；③第三次 CAS PREPARED→SENDING
  （committed 之后才 POST）。POST 严格位于任何 SQLite 事务之外；
- 会话绑定语义（§5-§9）：FIXED_SESSION 永远禁止建会话（FIXED_SESSION_FORBIDS_CREATE，
  0 GET / 0 POST / 0 operation，auto_open_session 不得绕过）；lease ACTIVE 仅允许
  resolved_session_id 为空的初始创建；ROTATING 允许轮换创建；QUARANTINED 禁止；
  本模块不切换 lease 状态、不改 task/attempt/lease 的 epoch/state/binding；
- CREATE_SESSION operation 的 operations.session_id / remote_user_id 恒为 NULL；
  真实 created session id 只进入 evidence_json.created_session_id；
- 网络/transport 调用严格位于任何 SQLite 事务之外；总写入一次 = PREPARED→SENDING→
  ACCEPTED/REJECTED/UNKNOWN 账本活动，不引入第二套状态机。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from adapters.openchamber_create_session import (
    CreateOutcome,
    CreateAttempt,
    OpenChamberCreateTransportError,
)
from core.dispatch import SnapshotFailure, derive_operation_id
from core.domain import ExecutionSettingsSnapshot, SessionBindingMode
from infra.clock import Clock, SystemClock
from storage.database import Database, StorageError
from storage.operation_store import (
    AcquireOutcome,
    FinalizeOutcome,
    OperationStore,
    PrepareOutcome,
)

CREATE_SESSION_KIND = "CREATE_SESSION"
_FIXED_SESSION = SessionBindingMode.FIXED_SESSION.value
_PROJECT_ROTATING = SessionBindingMode.PROJECT_ROTATING.value


class CreateCoordinatorError(StorageError):
    """CREATE 协调系统级失败（事务故障注入/冲突等），不是业务结果。"""

    code = "create_coordinator_error"


class CreateSessionOutcome(str, Enum):
    """CREATE_SESSION 稳定业务结果（禁止中文字符串匹配）。

    ACCEPTED/REJECTED/UNKNOWN 是真实建会话结论；UNKNOWN_EXISTING 表示既有
    未决 create operation 已留待 T22-05B 对账，绝不第二次 POST。
    """

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    UNKNOWN_EXISTING = "unknown_existing"
    ALREADY_EXISTS = "already_exists"
    KEY_CONFLICT = "key_conflict"
    FIXED_SESSION_FORBIDS_CREATE = "fixed_session_forbids_create"
    INVALID_SNAPSHOT = "invalid_snapshot"
    ENDPOINT_MISMATCH = "endpoint_mismatch"
    AUTHORITY_REVOKED = "authority_revoked"
    PRECHECK_FAILED = "precheck_failed"
    PREPARED = "prepared"
    SENDING = "sending"
    INVALID_LEDGER = "invalid_ledger"


@dataclass(frozen=True, slots=True)
class CreateSessionResult:
    """一次 create_session 的结构化结果。detail 绝不包含 token / Authorization / raw body。"""

    outcome: CreateSessionOutcome
    operation_key: str
    operation_id: str | None = None
    state: str | None = None
    created_session_id: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class CreateSessionProposal:
    """一次建会话提案：operation_key 为业务幂等键，由本模块按确定性规则生成。

    binding_mode / binding_revision / resolved_session_id 只用于 authority 与
    baseline 判定，不进入 identity_tuple（与 operations 表 9 列身份指纹对齐，
    供 PrepareOutcome.EXISTING / KEY_CONFLICT 复用既有判定）。
    """

    operation_key: str
    task_key: str
    attempt_id: str
    authority_epoch: int
    control_revision: int
    endpoint: str
    project_key: str
    directory: str
    binding_mode: str
    binding_revision: int
    resolved_session_id: str | None = None
    interruption_id: str | None = None

    kind: str = CREATE_SESSION_KIND
    session_id: str | None = None  # CREATE_SESSION 恒 None，绝不写入 real session id

    def identity_tuple(self) -> tuple:
        return (
            self.kind, self.task_key, self.attempt_id, self.authority_epoch,
            self.control_revision, self.endpoint, self.session_id,
            self.project_key, self.interruption_id,
        )


@dataclass(frozen=True, slots=True)
class CreateAuthorityStatus:
    """一次建会话权威检查（task/attempt/project owner）的结构化结果。"""

    ok: bool
    detail: str = ""
    lease_state: str | None = None


def create_session_operation_key(
    *, project_key: str, attempt_id: str, authority_epoch: int,
    control_revision: int, binding_revision: int,
) -> str:
    """确定性幂等键：同一权威决策（epoch/control_revision/binding_revision）→ 同一 key。"""
    return (
        f"create_session:{project_key}:{attempt_id}:{authority_epoch}"
        f":{control_revision}:{binding_revision}"
    )


def build_create_proposal(
    *,
    operation_key: str,
    snapshot: ExecutionSettingsSnapshot,
    task_key: str,
    attempt_id: str,
    authority_epoch: int,
    control_revision: int,
    endpoint: str,
    interruption_id: str | None = None,
) -> CreateSessionProposal:
    """从权威执行快照构建提案：project/binding 全部来自 snapshot，绝不自造。"""
    return CreateSessionProposal(
        operation_key=operation_key,
        task_key=task_key,
        attempt_id=attempt_id,
        authority_epoch=authority_epoch,
        control_revision=control_revision,
        endpoint=endpoint,
        project_key=snapshot.receive.project_key,
        directory=snapshot.receive.directory,
        binding_mode=snapshot.receive.binding_mode.value,
        binding_revision=snapshot.binding_revision,
        resolved_session_id=snapshot.resolved_session_id,
        interruption_id=interruption_id,
    )


def _utc_iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat()


class OpenChamberCreateSessionCoordinator:
    """CREATE_SESSION 协调器：可以分步（prepare_create / acquire_create_right /
    send_prepared_create），并有 create_session 串联；网络调用不在任何事务内。
    """

    def __init__(
        self,
        db: Database,
        *,
        transport,
        operation_store: OperationStore | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._db = db
        self._transport = transport
        self._ops = operation_store if operation_store is not None else OperationStore(db)
        self._clock = clock if clock is not None else SystemClock()

    # ---------------------------------------------------------------- entry

    def create_session(
        self,
        *,
        task_key: str,
        attempt_id: str,
        authority_epoch: int,
        control_revision: int,
        snapshot: ExecutionSettingsSnapshot,
        endpoint: str,
        create_title: str,
        now: datetime | None = None,
    ) -> CreateSessionResult:
        """串联 prepare_create → acquire_create_right → send_prepared_create。

        同 operation_key 再次 create：ACCEPTED/REJECTED → 原结论（0 网络）；
        UNKNOWN → UNKNOWN_EXISTING（0 POST，留 T22-05B 对账）；SENDING（重启残留）
        → 收敛 UNKNOWN 且 0 POST；PREPARED → 继续取得创建权。
        """
        if not isinstance(snapshot, ExecutionSettingsSnapshot):
            return CreateSessionResult(
                CreateSessionOutcome.INVALID_SNAPSHOT,
                operation_key="",
                detail="snapshot 必须是 ExecutionSettingsSnapshot：拒绝建会话",
            )
        operation_key = create_session_operation_key(
            project_key=snapshot.receive.project_key,
            attempt_id=attempt_id,
            authority_epoch=authority_epoch,
            control_revision=control_revision,
            binding_revision=snapshot.binding_revision,
        )
        proposal = build_create_proposal(
            operation_key=operation_key,
            snapshot=snapshot,
            task_key=task_key,
            attempt_id=attempt_id,
            authority_epoch=authority_epoch,
            control_revision=control_revision,
            endpoint=endpoint,
        )
        existing = self._ops.read_by_key(proposal.operation_key)
        if existing is not None and existing.identity_tuple() != proposal.identity_tuple():
            return CreateSessionResult(
                CreateSessionOutcome.KEY_CONFLICT,
                operation_key=proposal.operation_key,
                operation_id=existing.operation_id,
                state=existing.state,
                detail="同 operation_key 但身份（task/attempt/epoch/revision）不一致：拒绝覆盖原 operation",
            )
        if existing is not None and existing.state == "SENDING":
            return self._recover_stale_sending(existing, now)
        prepared = self.prepare_create(
            proposal, create_title=create_title, now=now,
        )
        if prepared.outcome is not CreateSessionOutcome.PREPARED:
            return prepared
        granted = self.acquire_create_right(proposal, now=now)
        if granted.outcome is not CreateSessionOutcome.SENDING:
            return granted
        return self.send_prepared_create(proposal, now=now)

    # ---------------------------------------------------------------- phases

    def prepare_create(
        self,
        proposal: CreateSessionProposal,
        *,
        create_title: str,
        now: datetime | None = None,
    ) -> CreateSessionResult:
        """既有 operation 幂等收敛 → 输入/FIXED 校验 → 第一次权威检查（GET 前）
        → baseline GET（事务外）→ 第二次权威检查 + 写 PREPARED。”

        失败结论全部保证 GET/POST/operation 为 0：identity mismatch → KEY_CONFLICT；
        既有终态 → 原结论或 UNKNOWN_EXISTING；blank title → INVALID_LEDGER；
        FIXED_SESSION → FIXED_SESSION_FORBIDS_CREATE；权威失败 → AUTHORITY_REVOKED；
        baseline GET 失败 → PRECHECK_FAILED（明确未 POST）。
        """
        now_iso = _utc_iso(now if now is not None else self._clock.now())
        existing = self._ops.read_by_key(proposal.operation_key)
        if existing is not None:
            fast = self._existing_op_result(existing, proposal)
            if fast is not None:
                return fast
        if not isinstance(create_title, str) or not create_title.strip():
            return CreateSessionResult(
                CreateSessionOutcome.INVALID_LEDGER,
                operation_key=proposal.operation_key,
                detail="create_title 不能为空：无法构造合法 baseline（0 GET / 0 POST / 0 operation）",
            )
        if proposal.binding_mode == _FIXED_SESSION:
            return CreateSessionResult(
                CreateSessionOutcome.FIXED_SESSION_FORBIDS_CREATE,
                operation_key=proposal.operation_key,
                detail="FIXED_SESSION 绑定禁止建会话：auto_open_session 不得绕过（0 GET / 0 POST / 0 operation）",
            )
        if proposal.binding_mode != _PROJECT_ROTATING:
            return CreateSessionResult(
                CreateSessionOutcome.INVALID_SNAPSHOT,
                operation_key=proposal.operation_key,
                detail=f"binding_mode={proposal.binding_mode!r} 不在 FIXED_SESSION/PROJECT_ROTATING：拒绝建会话",
            )
        first = self._check_create_authority(
            self._db.connection, proposal, proposal.resolved_session_id
        )
        if not first.ok:
            return CreateSessionResult(
                CreateSessionOutcome.AUTHORITY_REVOKED,
                operation_key=proposal.operation_key,
                detail=f"首次权威检查未通过：{first.detail}",
            )
        try:
            snapshot = self._transport.capture_pre_create_snapshot(endpoint=proposal.endpoint)
        except OpenChamberCreateTransportError as exc:
            if exc.kind == "ENDPOINT_MISMATCH":
                return CreateSessionResult(
                    CreateSessionOutcome.ENDPOINT_MISMATCH,
                    operation_key=proposal.operation_key,
                    detail="pre-create 快照目标与构造冻结 base_url 不一致：拒绝快照（0 HTTP）",
                )
            return CreateSessionResult(
                CreateSessionOutcome.PRECHECK_FAILED,
                operation_key=proposal.operation_key,
                detail=f"建会话前只读快照失败（{exc.kind}）：明确未创建（0 POST）",
            )
        except SnapshotFailure:
            return CreateSessionResult(
                CreateSessionOutcome.PRECHECK_FAILED,
                operation_key=proposal.operation_key,
                detail="建会话前只读快照失败：副作用 POST 尚未开始，明确未创建（0 POST）",
            )
        baseline = {
            "directory": proposal.directory,
            "binding_mode": proposal.binding_mode,
            "binding_revision": proposal.binding_revision,
            "session_ids_before": snapshot.get("session_ids_before", []),
            "session_count_before": snapshot.get("session_count_before", 0),
            "create_title": create_title.strip(),
        }
        operation_id = derive_operation_id(proposal.operation_key)
        baseline_json = json.dumps(baseline, ensure_ascii=False, sort_keys=True)
        try:
            with self._db.transaction():
                conn = self._db.connection
                second = self._check_create_authority(
                    conn, proposal, proposal.resolved_session_id
                )
                if not second.ok:
                    return CreateSessionResult(
                        CreateSessionOutcome.AUTHORITY_REVOKED,
                        operation_key=proposal.operation_key,
                        detail=f"PREPARED 落库时权威检查未通过：{second.detail}",
                    )
                prepared = self._ops.prepare_in(
                    conn,
                    proposal=proposal,
                    operation_id=operation_id,
                    remote_user_id=None,
                    pre_snapshot_json=baseline_json,
                    prompt_text=None,
                    prompt_hash=None,
                    created_at=now_iso,
                )
                if prepared.outcome is PrepareOutcome.CONFLICT:
                    return CreateSessionResult(
                        CreateSessionOutcome.KEY_CONFLICT,
                        operation_key=proposal.operation_key,
                        operation_id=prepared.operation_id,
                        state=prepared.record.state if prepared.record else None,
                        detail="同 operation_key 但身份不一致：拒绝覆盖原 operation",
                    )
                assert prepared.record is not None
                return CreateSessionResult(
                    CreateSessionOutcome.PREPARED
                    if prepared.outcome is PrepareOutcome.CREATED
                    else CreateSessionOutcome.ALREADY_EXISTS,
                    operation_key=proposal.operation_key,
                    operation_id=prepared.operation_id,
                    state=prepared.record.state,
                    detail="CREATE_SESSION PREPARED 已 durable 写入（baseline 已落库）",
                )
        except sqlite3.IntegrityError as exc:
            raise CreateCoordinatorError(f"PREPARED 事务故障注入/冲突：{exc}") from exc

    def acquire_create_right(
        self, proposal: CreateSessionProposal, *, now: datetime | None = None
    ) -> CreateSessionResult:
        """第三次权威检查 + CAS PREPARED→SENDING（事务）。只有取得成功的一方拥有创建权。

        停止/epoch 变化先赢 → AUTHORITY_REVOKED（保持 PREPARED），POST 为 0。
        """
        now_iso = _utc_iso(now if now is not None else self._clock.now())
        existing = self._ops.read_by_key(proposal.operation_key)
        if existing is None:
            return CreateSessionResult(
                CreateSessionOutcome.AUTHORITY_REVOKED,
                operation_key=proposal.operation_key,
                detail="operation 不存在：没有可取得创建权的 PREPARED 操作",
            )
        if existing.identity_tuple() != proposal.identity_tuple():
            return CreateSessionResult(
                CreateSessionOutcome.KEY_CONFLICT,
                operation_key=proposal.operation_key,
                operation_id=existing.operation_id,
                state=existing.state,
            )
        if existing.state != "PREPARED":
            return CreateSessionResult(
                CreateSessionOutcome.ALREADY_EXISTS,
                operation_key=proposal.operation_key,
                operation_id=existing.operation_id,
                state=existing.state,
                detail="同 key 操作已非 PREPARED：创建权已被占用或已是终态",
            )
        try:
            with self._db.transaction():
                conn = self._db.connection
                third = self._check_create_authority(
                    conn, proposal, proposal.resolved_session_id
                )
                if not third.ok:
                    return CreateSessionResult(
                        CreateSessionOutcome.AUTHORITY_REVOKED,
                        operation_key=proposal.operation_key,
                        operation_id=existing.operation_id,
                        detail=f"取得创建权前权威检查未通过：{third.detail}",
                    )
                acquired = self._ops.acquire_send_right_in(
                    conn, proposal=proposal,
                    operation_id=existing.operation_id, now=now_iso,
                )
                if acquired.outcome is not AcquireOutcome.GRANTED:
                    return CreateSessionResult(
                        CreateSessionOutcome.ALREADY_EXISTS,
                        operation_key=proposal.operation_key,
                        operation_id=acquired.operation_id,
                        state=acquired.state,
                        detail="创建权 CAS 未成功（另一调用方先取得或已终态）",
                    )
                return CreateSessionResult(
                    CreateSessionOutcome.SENDING,
                    operation_key=proposal.operation_key,
                    operation_id=acquired.operation_id,
                    state=acquired.state,
                    detail="PREPARED→SENDING 已提交：本调用方获得唯一创建权",
                )
        except sqlite3.IntegrityError as exc:
            raise CreateCoordinatorError(f"取得创建权事务故障注入/冲突：{exc}") from exc

    def send_prepared_create(
        self, proposal: CreateSessionProposal, *, now: datetime | None = None
    ) -> CreateSessionResult:
        """执行这一份已获权的 create_once（事务外）并落最终态（事务）。

        创建权先赢后，即使停止/epoch 变化随后到达，仍按“可能已产生远端副作用”处理：
        完全不重发；远端未给结论或传输不明 → UNKNOWN。
        """
        now_iso = _utc_iso(now if now is not None else self._clock.now())
        existing = self._ops.read_by_key(proposal.operation_key)
        if existing is None:
            return CreateSessionResult(
                CreateSessionOutcome.AUTHORITY_REVOKED,
                operation_key=proposal.operation_key,
            )
        if existing.identity_tuple() != proposal.identity_tuple():
            return CreateSessionResult(
                CreateSessionOutcome.KEY_CONFLICT,
                operation_key=proposal.operation_key,
                operation_id=existing.operation_id,
                state=existing.state,
            )
        if existing.state != "SENDING":
            return CreateSessionResult(
                CreateSessionOutcome.ALREADY_EXISTS,
                operation_key=proposal.operation_key,
                operation_id=existing.operation_id,
                state=existing.state,
                detail="操作当前不是 SENDING：必须先 acquire_create_right 成功",
            )
        try:
            baseline = json.loads(existing.pre_snapshot_json or "{}")
        except (TypeError, ValueError):
            baseline = {}
        title = baseline.get("create_title") if isinstance(baseline, dict) else None
        if not isinstance(title, str) or not title.strip():
            return CreateSessionResult(
                CreateSessionOutcome.INVALID_LEDGER,
                operation_key=proposal.operation_key,
                operation_id=existing.operation_id,
                state=existing.state,
                detail="PREPARED baseline 缺少合法 create_title：禁止建会话（0 POST，fail-closed）",
            )
        try:
            attempt = self._transport.create_once(
                endpoint=existing.endpoint, create_title=title.strip()
            )
        except OpenChamberCreateTransportError as exc:
            if exc.kind == "ENDPOINT_MISMATCH":
                return CreateSessionResult(
                    CreateSessionOutcome.ENDPOINT_MISMATCH,
                    operation_key=proposal.operation_key,
                    operation_id=existing.operation_id,
                    state=existing.state,
                    detail="create 目标与构造冻结 base_url 不一致：拒绝 POST（0 HTTP）",
                )
            if exc.kind == "MISSING_CREATE_TITLE":
                return CreateSessionResult(
                    CreateSessionOutcome.INVALID_LEDGER,
                    operation_key=proposal.operation_key,
                    operation_id=existing.operation_id,
                    state=existing.state,
                    detail="create_title 缺失：拒绝 POST（0 HTTP）",
                )
            attempt = CreateAttempt(
                CreateOutcome.UNKNOWN,
                evidence={"classification": "create_unknown"},
            )
            return self._finalize_create(
                existing, attempt, now_iso,
                detail=f"create POST 结果不明（{exc.kind}）：禁止重试",
            )
        except Exception as exc:
            attempt = CreateAttempt(
                CreateOutcome.UNKNOWN,
                evidence={"classification": "create_unknown"},
            )
            return self._finalize_create(
                existing, attempt, now_iso,
                detail=f"create_once 异常，远端结果不明：{type(exc).__name__}: {exc}",
            )
        return self._finalize_create(existing, attempt, now_iso)

    # ---------------------------------------------------------------- internals

    def _existing_op_result(self, record, proposal: CreateSessionProposal):
        """同 key 已有 operation 的幂等结论（0 GET / 0 POST）。identity 不符 → KEY_CONFLICT。"""
        if record.identity_tuple() != proposal.identity_tuple():
            return CreateSessionResult(
                CreateSessionOutcome.KEY_CONFLICT,
                operation_key=proposal.operation_key,
                operation_id=record.operation_id,
                state=record.state,
                detail="同 operation_key 但身份不一致：拒绝覆盖原 operation",
            )
        if record.state == "ACCEPTED":
            return CreateSessionResult(
                CreateSessionOutcome.ACCEPTED,
                operation_key=proposal.operation_key,
                operation_id=record.operation_id,
                state=record.state,
                created_session_id=self._created_id_from_evidence(record),
                detail="已有 ACCEPTED create operation：复用既有 created_session_id（0 POST）",
            )
        if record.state == "REJECTED":
            return CreateSessionResult(
                CreateSessionOutcome.REJECTED,
                operation_key=proposal.operation_key,
                operation_id=record.operation_id,
                state=record.state,
                detail="已有 REJECTED create operation：确认未创建，不重试（0 POST）",
            )
        if record.state == "UNKNOWN":
            return CreateSessionResult(
                CreateSessionOutcome.UNKNOWN_EXISTING,
                operation_key=proposal.operation_key,
                operation_id=record.operation_id,
                state=record.state,
                detail="已有 UNKNOWN create operation：结果不明，留待 T22-05B 对账（0 POST）",
            )
        if record.state == "PREPARED":
            return CreateSessionResult(
                CreateSessionOutcome.PREPARED,
                operation_key=proposal.operation_key,
                operation_id=record.operation_id,
                state=record.state,
                detail="已有同身份 PREPARED create operation，幂等复用",
            )
        return CreateSessionResult(
            CreateSessionOutcome.ALREADY_EXISTS,
            operation_key=proposal.operation_key,
            operation_id=record.operation_id,
            state=record.state,
            detail="同 key create operation 当前状态不可再创建：不重复 POST",
        )

    def _recover_stale_sending(self, existing, now: datetime | None) -> CreateSessionResult:
        """"进程重启看到 SENDING：视为可能已创建 → 收敛 UNKNOWN，零 POST。"""
        now_iso = _utc_iso(now if now is not None else self._clock.now())
        with self._db.transaction():
            conn = self._db.connection
            self._ops.recover_sending_as_unknown_in(
                conn,
                operation_key=existing.operation_key,
                operation_id=existing.operation_id,
                now=now_iso,
                evidence='{"decision": "restart_recovery"}',
            )
        return CreateSessionResult(
            CreateSessionOutcome.ALREADY_EXISTS,
            operation_key=existing.operation_key,
            operation_id=existing.operation_id,
            state="UNKNOWN",
            detail="残留 SENDING 已作为“可能已创建”收敛到 UNKNOWN；不调用 transport",
        )

    def _check_create_authority(
        self, conn: sqlite3.Connection,
        proposal: CreateSessionProposal, resolved_session_id: str | None,
    ) -> CreateAuthorityStatus:
        """建会话权威条件：task=ACTIVE 且 active_attempt/epoch 匹配；attempt=OPEN 且
        epoch/control_revision 匹配；lease 由本 task+attempt+epoch 持有且
        state 允许建会话（ROTATING 允许轮换；ACTIVE 仅允许 resolved_session_id 为
        空的初始创建；QUARANTINED/缺失/owner 不符一律禁止）。
        """
        task = conn.execute(
            "SELECT state, active_attempt_id, authority_epoch FROM tasks WHERE task_key=?",
            (proposal.task_key,),
        ).fetchone()
        task_present = task is not None
        task_active = bool(task and task[0] == "ACTIVE")
        active_attempt_matches = bool(task and task[1] == proposal.attempt_id)
        task_epoch_matches = bool(task and task[2] == proposal.authority_epoch)
        task_state = task[0] if task else None

        attempt = conn.execute(
            "SELECT task_key, state, authority_epoch, control_revision FROM attempts"
            " WHERE attempt_id=?",
            (proposal.attempt_id,),
        ).fetchone()
        attempt_present = bool(attempt and attempt[0] == proposal.task_key)
        attempt_open = bool(attempt and attempt[1] == "OPEN")
        attempt_epoch_matches = bool(attempt and attempt[2] == proposal.authority_epoch)
        control_revision_matches = bool(attempt and attempt[3] == proposal.control_revision)
        attempt_state = attempt[1] if attempt else None

        lease = conn.execute(
            "SELECT owner_task_key, owner_attempt_id, authority_epoch, state"
            " FROM project_leases WHERE project_key=?",
            (proposal.project_key,),
        ).fetchone()
        lease_present = lease is not None
        lease_owner_matches = bool(
            lease and lease[0] == proposal.task_key and lease[1] == proposal.attempt_id
        )
        lease_epoch_matches = bool(lease and lease[2] == proposal.authority_epoch)
        lease_state = lease[3] if lease else None
        lease_allows_create = bool(
            lease_state == "ROTATING"
            or (lease_state == "ACTIVE" and resolved_session_id is None)
        )

        ok = all(
            (
                task_present, task_active, active_attempt_matches, task_epoch_matches,
                attempt_present, attempt_open, attempt_epoch_matches,
                control_revision_matches, lease_present, lease_owner_matches,
                lease_epoch_matches, lease_allows_create,
            )
        )
        if ok:
            return CreateAuthorityStatus(ok=True, lease_state=lease_state)
        issues: list[str] = []
        if not task_present:
            issues.append("task 不存在")
        else:
            if not task_active:
                issues.append(f"task 非 ACTIVE（task.state={task_state}）")
            if not active_attempt_matches:
                issues.append("task.active_attempt_id 与 proposal.attempt_id 不一致")
            if not task_epoch_matches:
                issues.append("task.authority_epoch 与 proposal 不一致")
        if not attempt_present:
            issues.append("attempt 不存在")
        else:
            if not attempt_open:
                issues.append(f"attempt 非 OPEN（attempt.state={attempt_state}）")
            if not attempt_epoch_matches:
                issues.append("attempt.authority_epoch 与 proposal 不一致")
            if not control_revision_matches:
                issues.append("attempt.control_revision 与 proposal 不一致")
        if not lease_present:
            issues.append("project lease 不存在")
        else:
            if not lease_owner_matches:
                issues.append("lease owner 与 task/attempt 不一致")
            if not lease_epoch_matches:
                issues.append("lease authority_epoch 与 proposal 不一致")
            if not lease_allows_create:
                issues.append(
                    f"lease state={lease_state!r} 不允许建会话"
                    "（仅 ROTATING 或 ACTIVE 且尚未解析会话）"
                )
        return CreateAuthorityStatus(ok=False, detail="；".join(issues), lease_state=lease_state)

    def _finalize_create(
        self,
        record,
        attempt: CreateAttempt,
        now_iso: str,
        *,
        detail: str = "",
    ) -> CreateSessionResult:
        """把 SENDING → ACCEPTED/REJECTED/UNKNOWN（事务），evidence 最小化。"""
        target = {
            CreateOutcome.ACCEPTED: "ACCEPTED",
            CreateOutcome.REJECTED: "REJECTED",
            CreateOutcome.UNKNOWN: "UNKNOWN",
        }[attempt.outcome]
        evidence = dict(attempt.evidence or {})
        if detail:
            evidence.setdefault("transport_error", detail)
        try:
            with self._db.transaction():
                conn = self._db.connection
                final = self._ops.finalize_in(
                    conn,
                    proposal=record,
                    operation_id=record.operation_id,
                    target_state=target,
                    evidence_json=json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                    finalized_at=now_iso,
                    now=now_iso,
                )
        except sqlite3.IntegrityError as exc:
            current = self._ops.read_by_key(record.operation_key)
            return CreateSessionResult(
                CreateSessionOutcome.ALREADY_EXISTS if current is not None
                else CreateSessionOutcome.AUTHORITY_REVOKED,
                operation_key=record.operation_key,
                operation_id=record.operation_id,
                state=current.state if current else None,
                detail=f"最终态事务故障注入/冲突（{type(exc).__name__}）：不覆盖，保持原状态",
            )
        if final.outcome is FinalizeOutcome.FINALIZED:
            outcome = CreateSessionOutcome(target.lower())
            return CreateSessionResult(
                outcome,
                operation_key=record.operation_key,
                operation_id=final.operation_id,
                state=final.state,
                created_session_id=(
                    evidence.get("created_session_id") if target == "ACCEPTED" else None
                ),
                detail=(detail or ("建会话已确认接受" if target == "ACCEPTED"
                                   else "建会话结论已落账本")),
            )
        if final.outcome in (FinalizeOutcome.ALREADY_FINAL, FinalizeOutcome.CAS_LOST):
            current = self._ops.read_by_key(record.operation_key)
            cur = current or record
            cur_state = cur.state
            existing_classified = {
                "ACCEPTED": CreateSessionOutcome.ACCEPTED,
                "REJECTED": CreateSessionOutcome.REJECTED,
                "UNKNOWN": CreateSessionOutcome.UNKNOWN_EXISTING,
            }.get(cur_state)
            return CreateSessionResult(
                existing_classified if existing_classified is not None
                else CreateSessionOutcome.ALREADY_EXISTS,
                operation_key=record.operation_key,
                operation_id=cur.operation_id,
                state=cur_state,
                created_session_id=(
                    self._created_id_from_evidence(cur) if cur_state == "ACCEPTED" else None
                ),
                detail="最终态 CAS 未成功或已终态：已回读当前账本结论（对账后可收敛）",
            )
        current = self._ops.read_by_key(record.operation_key)
        return CreateSessionResult(
            CreateSessionOutcome.ALREADY_EXISTS if current is not None
            else CreateSessionOutcome.AUTHORITY_REVOKED,
            operation_key=record.operation_key,
            operation_id=final.operation_id,
            state=current.state if current else final.state,
            detail="最终态未落账本，已回读当前状态",
        )

    @staticmethod
    def _created_id_from_evidence(record) -> str | None:
        try:
            evidence = json.loads(record.evidence_json or "{}")
        except (TypeError, ValueError):
            evidence = {}
        value = evidence.get("created_session_id") if isinstance(evidence, dict) else None
        return value if isinstance(value, str) and value.strip() else None