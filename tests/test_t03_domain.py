"""T03 领域测试：强类型 ID、状态转移、终态不可复活、不可变合同、ErrorCode。"""

import pytest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

from core.domain import (
    AttemptId,
    AttemptStatus,
    AuthoritativeResultRef,
    Command,
    CommandKind,
    Decision,
    DecisionAction,
    Observation,
    OperationId,
    OperationStatus,
    Result,
    ResultId,
    ResultStatus,
    TaskId,
    TaskStatus,
    require_active_attempt,
    transition_attempt,
    transition_operation,
    transition_task,
)
from core.errors import DomainError, ErrorCode

TZ = timezone.utc
FIXED_AT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=TZ)

TASK_ID = TaskId("task-1")
ATTEMPT_ID = AttemptId("attempt-1")
OPERATION_ID = OperationId("op-1")
RESULT_ID = ResultId("result-1")


# ---------------------------------------------------------------------------
# 强类型 ID
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", [TaskId, AttemptId, OperationId, ResultId])
def test_id_rejects_empty_and_blank(cls):
    for bad in ("", "   ", "\t\n"):
        with pytest.raises(DomainError) as err:
            cls(bad)
        assert err.value.code is ErrorCode.INVALID_ID
    assert cls("x").value == "x"


def test_different_id_types_never_confuse():
    assert TaskId("x") != AttemptId("x")
    assert TaskId("x") != OperationId("x")
    assert TaskId("x") != ResultId("x")
    assert hash(TaskId("x")) == hash(TaskId("x"))
    assert AttemptId("x") == AttemptId("x")
    assert TaskId("x") != TaskId("y")


def test_id_str_repr_loggable():
    assert str(TASK_ID) == "task-1"
    assert "task-1" in repr(TASK_ID)


# ---------------------------------------------------------------------------
# 任务状态
# ---------------------------------------------------------------------------


def test_task_terminal_semantics():
    for status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.STOPPED_BY_USER):
        assert status.is_terminal()
    for status in (TaskStatus.QUEUED, TaskStatus.ACTIVE, TaskStatus.BLOCKED):
        assert not status.is_terminal()


def test_task_legal_transitions():
    assert transition_task(TaskStatus.QUEUED, TaskStatus.ACTIVE) is TaskStatus.ACTIVE
    assert transition_task(TaskStatus.QUEUED, TaskStatus.STOPPED_BY_USER) is TaskStatus.STOPPED_BY_USER
    assert transition_task(TaskStatus.ACTIVE, TaskStatus.BLOCKED) is TaskStatus.BLOCKED
    assert transition_task(TaskStatus.ACTIVE, TaskStatus.COMPLETED) is TaskStatus.COMPLETED
    assert transition_task(TaskStatus.BLOCKED, TaskStatus.ACTIVE) is TaskStatus.ACTIVE
    # 断网/等待不是 FAILED。
    assert TaskStatus.ACTIVE.is_terminal() is False


def test_task_terminal_cannot_revive():
    for terminal in (
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.STOPPED_BY_USER,
    ):
        for revival in (TaskStatus.ACTIVE, TaskStatus.BLOCKED, TaskStatus.QUEUED):
            with pytest.raises(DomainError) as err:
                transition_task(terminal, revival)
            assert err.value.code is ErrorCode.INVALID_TRANSITION
            assert err.value.code is not ErrorCode.STALE_ATTEMPT


# ---------------------------------------------------------------------------
# Attempt 状态
# ---------------------------------------------------------------------------


def test_attempt_terminal_semantics():
    assert not AttemptStatus.OPEN.is_terminal()
    for status in (
        AttemptStatus.COMPLETED,
        AttemptStatus.FAILED,
        AttemptStatus.STOPPED,
        AttemptStatus.SUPERSEDED,
    ):
        assert status.is_terminal()


def test_attempt_legal_transitions():
    assert transition_attempt(AttemptStatus.OPEN, AttemptStatus.SUPERSEDED) is AttemptStatus.SUPERSEDED
    assert transition_attempt(AttemptStatus.OPEN, AttemptStatus.COMPLETED) is AttemptStatus.COMPLETED


def test_attempt_terminal_cannot_revive():
    for terminal in (
        AttemptStatus.COMPLETED,
        AttemptStatus.FAILED,
        AttemptStatus.STOPPED,
        AttemptStatus.SUPERSEDED,
    ):
        with pytest.raises(DomainError) as err:
            transition_attempt(terminal, AttemptStatus.OPEN)
        assert err.value.code is ErrorCode.INVALID_TRANSITION


def test_supersede_creates_new_attempt_identity():
    old = AttemptId("attempt-old")
    new = AttemptId("attempt-new")
    assert old != new
    # 新尝试从 OPEN 开始，旧尝试保持终态。
    transition_attempt(AttemptStatus.OPEN, AttemptStatus.SUPERSEDED)
    require_active_attempt(AttemptStatus.OPEN)
    with pytest.raises(DomainError) as err:
        require_active_attempt(AttemptStatus.SUPERSEDED)
    assert err.value.code is ErrorCode.STALE_ATTEMPT


# ---------------------------------------------------------------------------
# Operation 状态：UNKNOWN 可表达，不是失败、不能自动重发
# ---------------------------------------------------------------------------


def test_operation_unknown_expressible():
    assert OperationStatus.UNKNOWN in OperationStatus
    assert not OperationStatus.UNKNOWN.is_terminal()
    assert OperationStatus.UNKNOWN.value == "unknown"


def test_operation_transitions():
    assert transition_operation(OperationStatus.PREPARED, OperationStatus.SENDING) is OperationStatus.SENDING
    assert transition_operation(OperationStatus.SENDING, OperationStatus.UNKNOWN) is OperationStatus.UNKNOWN
    assert transition_operation(OperationStatus.UNKNOWN, OperationStatus.ACCEPTED) is OperationStatus.ACCEPTED
    assert transition_operation(OperationStatus.UNKNOWN, OperationStatus.REJECTED) is OperationStatus.REJECTED
    # ACCEPTED / REJECTED 为终态；UNKNOWN 待对账，不允许“REJECTED → SENDING”式自动重发。
    with pytest.raises(DomainError) as err:
        transition_operation(OperationStatus.ACCEPTED, OperationStatus.SENDING)
    assert err.value.code is ErrorCode.INVALID_TRANSITION
    with pytest.raises(DomainError) as err:
        transition_operation(OperationStatus.REJECTED, OperationStatus.SENDING)
    assert err.value.code is ErrorCode.INVALID_TRANSITION


# ---------------------------------------------------------------------------
# Result：不可变版本 + 当前权威引用
# ---------------------------------------------------------------------------

OTHER_RESULT_ID = ResultId("result-2")


def test_result_immutable_version_semantics():
    r1 = Result(TASK_ID, ATTEMPT_ID, RESULT_ID, 1, ResultStatus.AUTHORITATIVE, FIXED_AT)
    r2 = Result(TASK_ID, ATTEMPT_ID, OTHER_RESULT_ID, 1, ResultStatus.SUPERSEDED, FIXED_AT)
    assert r1.result_id != r2.result_id
    assert r1.is_authoritative()
    assert not r2.is_authoritative()
    with pytest.raises(DomainError) as err:
        Result(TASK_ID, ATTEMPT_ID, RESULT_ID, 0, ResultStatus.CANDIDATE, FIXED_AT)
    assert err.value.code is ErrorCode.INVALID_RESULT


def test_authoritative_ref_points_to_one_version():
    ref = AuthoritativeResultRef(TASK_ID, RESULT_ID, 3, authority_epoch=2)
    assert ref.result_id == RESULT_ID
    assert ref.revision == 3
    assert ref.authority_epoch == 2
    with pytest.raises(DomainError) as err:
        AuthoritativeResultRef(TASK_ID, RESULT_ID, 1, authority_epoch=0)
    assert err.value.code is ErrorCode.INVALID_RESULT


# ---------------------------------------------------------------------------
# 不可变合同对象
# ---------------------------------------------------------------------------


def test_domain_objects_are_frozen():
    obs = Observation("obs-1", TASK_ID, ATTEMPT_ID, "network", "unreachable", FIXED_AT)
    cmd = Command(
        "cmd-1",
        TASK_ID,
        ATTEMPT_ID,
        CommandKind.SEND_CONTINUE,
        FIXED_AT,
        operation_id=str(OPERATION_ID),
    )
    dec = Decision(
        "dec-1", TASK_ID, ATTEMPT_ID, DecisionAction.WAIT, FIXED_AT, reason_code="not_due"
    )
    for obj in (obs, cmd, dec, Result(TASK_ID, ATTEMPT_ID, RESULT_ID, 1, ResultStatus.CANDIDATE, FIXED_AT)):
        with pytest.raises(FrozenInstanceError):
            setattr(obj, "task_id", TaskId("hacked"))


def test_observation_carries_facts_only():
    obs = Observation("obs-1", TASK_ID, ATTEMPT_ID, "network", "unreachable", FIXED_AT, {"hint": "timeout"})
    assert obs.kind == "unreachable"
    assert obs.payload["hint"] == "timeout"
    with pytest.raises(DomainError) as err:
        Observation("", TASK_ID, ATTEMPT_ID, "network", "unreachable", FIXED_AT)
    assert err.value.code is ErrorCode.INVALID_OBSERVATION


def test_command_requires_valid_id():
    with pytest.raises(DomainError) as err:
        Command("", TASK_ID, ATTEMPT_ID, CommandKind.START_ATTEMPT, FIXED_AT)
    assert err.value.code is ErrorCode.INVALID_COMMAND
    assert Command("cmd-1", TASK_ID, ATTEMPT_ID, CommandKind.SEND_INITIAL, FIXED_AT).kind.value == "send_initial"


def test_decision_is_pure():
    dec = Decision(
        "dec-1",
        TASK_ID,
        ATTEMPT_ID,
        DecisionAction.PROPOSE_CONTINUE,
        FIXED_AT,
        detail="复查后建议续接",
    )
    assert dec.action is DecisionAction.PROPOSE_CONTINUE
    assert "续接" in dec.detail


def test_error_code_is_stable_machine_readable():
    assert ErrorCode.INVALID_ID.value == "invalid_id"
    assert ErrorCode.INVALID_TRANSITION.value == "invalid_transition"
    assert ErrorCode.STALE_EPOCH.value == "stale_epoch"
    err = DomainError(ErrorCode.INVALID_TRANSITION, "终态不可复活")
    assert err.code is ErrorCode.INVALID_TRANSITION
    assert "终态不可复活" in err.message
    assert err.code.value in str(err)