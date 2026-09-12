"""T12：Fake 执行端的确定性完成与审计（SendTransport 合同 + 一键脚本）。

覆盖：
- SendTransport 合同：capture_pre_send_snapshot / send_once 严格在事务外，
  wire prompt 自带 task/attempt/operation 归属标记且与 operation_id 一致；
- 四种脚本：完整完成 / 明确拒绝 / 超时未知 / 完成读取故障；
- completion_for 确定性：同一 operation 反复调用逐字节一致，跨重启可重建；
- 完成结果与账本一致性校验：标记缺失/不匹配即显式报错，绝不静默拼凑。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from adapters.fake_executor import (
    FakeCompletionError,
    FakeCompletionReadError,
    FakeExecutor,
    FakeScript,
)
from core.dispatch import (
    SendOutcome,
    build_wire_prompt,
    derive_operation_id,
    TransportTimeoutError,
)
from storage.operation_store import OperationRecord

_T0 = "2026-10-01T09:00:00+00:00"
_WALL = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)

_TASK_KEY = "5:CHATGPT:t12-fake-001"
_ATTEMPT_ID = "attempt-042"
_OPERATION_KEY = f"INITIAL_SEND:{_ATTEMPT_ID}"
_OPERATION_ID = derive_operation_id(_OPERATION_KEY)


def _record(*, prompt_text: str | None = None) -> OperationRecord:
    return OperationRecord(
        operation_id=_OPERATION_ID,
        operation_key=_OPERATION_KEY,
        kind="INITIAL_SEND",
        task_key=_TASK_KEY,
        attempt_id=_ATTEMPT_ID,
        authority_epoch=1,
        control_revision=0,
        endpoint="http://127.0.0.1:57123",
        session_id="sess-fixed",
        project_key="p-fixed",
        interruption_id=None,
        state="ACCEPTED",
        pre_snapshot_json='{"message_ids": ["m1"]}',
        prompt_hash="h",
        prompt_text=prompt_text or build_wire_prompt(
            task_key=_TASK_KEY, attempt_id=_ATTEMPT_ID,
            operation_id=_OPERATION_ID, body="处理任务 t12-fake-001",
        ),
        remote_user_id="user-fake-001",
        evidence_json="{}",
        created_at=_T0,
        updated_at=_T0,
        finalized_at=_T0,
    )


class TestSnapshotContract:
    def test_capture_pre_send_snapshot_returns_dict(self):
        fake = FakeExecutor()
        snapshot = fake.capture_pre_send_snapshot(
            endpoint="http://e", session_id="s", task_key=_TASK_KEY)
        assert isinstance(snapshot, dict)
        assert fake.snapshot_calls == 1

    def test_send_outside_transaction_tracked(self):
        class Db:
            in_transaction = False

        fake = FakeExecutor(script=FakeScript.ACCEPT_AND_COMPLETE, db=Db())
        attempt = fake.send_once(
            endpoint="http://e", session_id="s",
            prompt_text=build_wire_prompt(
                task_key=_TASK_KEY, attempt_id=_ATTEMPT_ID,
                operation_id=_OPERATION_ID, body="b"),
            operation_id=_OPERATION_ID,
        )
        assert attempt.outcome is SendOutcome.ACCEPTED
        assert fake.send_in_txn == [False]
        assert fake.remote_received is True

    def test_send_captures_payload_and_per_task_count(self):
        fake = FakeExecutor()
        prompt = build_wire_prompt(
            task_key=_TASK_KEY, attempt_id=_ATTEMPT_ID,
            operation_id=_OPERATION_ID, body="正文")
        fake.send_once(endpoint="http://e", session_id="s",
                       prompt_text=prompt, operation_id=_OPERATION_ID)
        assert fake.send_calls == 1
        assert fake.send_calls_by_task == {_TASK_KEY: 1}
        payload = fake.sent_payloads[0]
        assert payload["task_key"] == _TASK_KEY
        assert payload["attempt_id"] == _ATTEMPT_ID
        assert payload["operation_id"] == _OPERATION_ID
        assert payload["prompt_text"] == prompt

    def test_send_with_missing_marker_raises_explicit_error(self):
        fake = FakeExecutor()
        with pytest.raises(FakeCompletionError):
            fake.send_once(endpoint="http://e", session_id="s",
                           prompt_text="无标记正文", operation_id=_OPERATION_ID)
        assert fake.send_calls == 1  # 调用发生了，但被明确拒绝归因

    def test_mismatched_operation_marker_raises(self):
        fake = FakeExecutor()
        prompt = build_wire_prompt(
            task_key=_TASK_KEY, attempt_id=_ATTEMPT_ID,
            operation_id="op-other", body="b")
        with pytest.raises(FakeCompletionError):
            fake.send_once(endpoint="http://e", session_id="s",
                           prompt_text=prompt, operation_id=_OPERATION_ID)


class TestScripts:
    def test_accept_and_complete_send(self):
        fake = FakeExecutor(FakeScript.ACCEPT_AND_COMPLETE)
        attempt = fake.send_once(
            endpoint="http://e", session_id="s",
            prompt_text=build_wire_prompt(
                task_key=_TASK_KEY, attempt_id=_ATTEMPT_ID,
                operation_id=_OPERATION_ID, body="b"),
            operation_id=_OPERATION_ID,
        )
        assert attempt.outcome is SendOutcome.ACCEPTED
        assert attempt.remote_user_id == "user-fake-001"
        assert fake.remote_received is True

    def test_reject_before_side_effect(self):
        fake = FakeExecutor(FakeScript.REJECT_BEFORE_SIDE_EFFECT)
        attempt = fake.send_once(
            endpoint="http://e", session_id="s",
            prompt_text=build_wire_prompt(
                task_key=_TASK_KEY, attempt_id=_ATTEMPT_ID,
                operation_id=_OPERATION_ID, body="b"),
            operation_id=_OPERATION_ID,
        )
        assert attempt.outcome is SendOutcome.REJECTED
        assert attempt.evidence == {"promptDispatched": False, "reason": "fake_reject"}
        assert fake.remote_received is False

    def test_unknown_after_side_effect_raises_timeout(self):
        fake = FakeExecutor(FakeScript.UNKNOWN_AFTER_SIDE_EFFECT)
        with pytest.raises(TransportTimeoutError):
            fake.send_once(
                endpoint="http://e", session_id="s",
                prompt_text=build_wire_prompt(
                    task_key=_TASK_KEY, attempt_id=_ATTEMPT_ID,
                    operation_id=_OPERATION_ID, body="b"),
                operation_id=_OPERATION_ID,
            )
        assert fake.remote_received is True  # 副作用已到达远端

    def test_fail_completion_read_send_still_accepted(self):
        fake = FakeExecutor(FakeScript.FAIL_COMPLETION_READ,
                            completion_read_fault=True)
        attempt = fake.send_once(
            endpoint="http://e", session_id="s",
            prompt_text=build_wire_prompt(
                task_key=_TASK_KEY, attempt_id=_ATTEMPT_ID,
                operation_id=_OPERATION_ID, body="b"),
            operation_id=_OPERATION_ID,
        )
        assert attempt.outcome is SendOutcome.ACCEPTED


class TestCompletionDeterminism:
    def test_completion_is_deterministic(self):
        fake = FakeExecutor()
        record = _record()
        first = fake.completion_for(record)
        second = fake.completion_for(record)
        assert first.final_body == second.final_body
        assert first.claim_message_id == second.claim_message_id
        assert first.result_state == "COMPLETED"
        assert first.remote_state == "IDLE_VERIFIED"
        assert fake.completion_calls == 2
        assert fake.completion_calls_by_task == {_TASK_KEY: 2}

    def test_completion_derives_from_ledger_only(self):
        fake = FakeExecutor()
        completion = fake.completion_for(_record())
        assert completion.task_key == _TASK_KEY
        assert completion.attempt_id == _ATTEMPT_ID
        assert completion.operation_id == _OPERATION_ID
        assert f"task={_TASK_KEY}" in completion.final_body
        assert completion.claim_message_id == (
            "fake-msg-" + __import__("core.result_commit", fromlist=["sha256_hex"])
            .sha256_hex(_OPERATION_ID)[:20]
        )

    def test_completion_raises_read_error_when_fault_set(self):
        fake = FakeExecutor(FakeScript.FAIL_COMPLETION_READ,
                            completion_read_fault=True)
        with pytest.raises(FakeCompletionReadError):
            fake.completion_for(_record())
        fake.completion_read_fault = False
        completion = fake.completion_for(_record())
        assert completion.result_state == "COMPLETED"

    def test_completion_rejects_missing_or_mismatched_markers(self):
        from dataclasses import replace

        fake = FakeExecutor()
        plain = _record(prompt_text="没有归属标记的正文")
        with pytest.raises(FakeCompletionError):
            fake.completion_for(plain)
        wrong = replace(plain, task_key="5:CHATGPT:other")
        with pytest.raises(FakeCompletionError):
            fake.completion_for(wrong)
        wrong_attempt = replace(plain, attempt_id="attempt-999")
        with pytest.raises(FakeCompletionError):
            fake.completion_for(wrong_attempt)
        with pytest.raises(FakeCompletionError):
            fake.completion_for(None)

    def test_completion_check_rejects_illegal_script(self):
        with pytest.raises(FakeCompletionError):
            FakeExecutor(script="UNKNOWN_SCRIPT")