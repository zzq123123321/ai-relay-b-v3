"""AI Relay B V3.0：Fake 执行端（T12 / G1 假闭环用）。

职责：
- 实现 T10 SendTransport 合同（capture_pre_send_snapshot / send_once），供
  DispatchService 做**恰好一次**发送，网络调用严格在事务外；
- completion_for(operation_record)：对已 ACCEPTED 的发送构造确定性完成结果
  （正常完成 = COMPLETED + IDLE_VERIFIED，触发 T11R 同事务安全释放项目 lease）；
  跨重启幂等：只依赖 operations 表已经冻结的 prompt_text/task_key/attempt_id/
  operation_id，不依赖任何进程内随机状态；
- 审计：记录 snapshot/send/completion 总次数与按 task 的 send/completion 次数、
  全部发送载荷；send 次数是验收“每任务恰好一次发送”的直接证据。

脚本（FakeScript）：
- ACCEPT_AND_COMPLETE：send 明确接受，completion_for 返回真完成；
- REJECT_BEFORE_SIDE_EFFECT：远端明确拒绝、确认未派发 → 不产生 completion；
- UNKNOWN_AFTER_SIDE_EFFECT：远端已收到但客户端超时 → Dispatch 记 UNKNOWN，
  completion 不允许做（绝不以“未知”拼凑完成）；
- FAIL_COMPLETION_READ：send 已接受，但 completion_read_fault 置位时
  completion_for 抛 FakeCompletionReadError → 结构化中断，操作保持 ACCEPTED，
  修复后可无重发地重建同一 completion（确定性）。

第一次发送后任务归属直接用 operations 表权威列；标记解析用于证明 wire prompt
确实携带 task/attempt/operation 归属标记且与账本一致（主规格 07.2③）。
"""

from __future__ import annotations

import re

from core.dispatch import (
    SendAttempt,
    SendOutcome,
    SendTransport,
    TransportTimeoutError,
    sha256_hex,
)

_TASK_MARKER = re.compile(r"\[AI_RELAY_TASK_ID: ([^\]]*)\]")
_ATTEMPT_MARKER = re.compile(r"\[AI_RELAY_ATTEMPT_ID: ([^\]]*)\]")
_OPERATION_MARKER = re.compile(r"\[AI_RELAY_OPERATION_ID: ([^\]]*)\]")

_REMOTE_USER_ID = "user-fake-001"
_REMOTE_MESSAGE_ID = "fake-msg-1"


class FakeCompletionError(Exception):
    """完成结果构造失败（标记缺失/与账本不一致等系统性异常）。"""


class FakeCompletionReadError(FakeCompletionError):
    """模拟完成读取故障（FAIL_COMPLETION_READ）：send 已接受但完成不可读。"""


class FakeScript(str):
    """脚本稳定值（完整/拒绝/未知/完成读取故障）。"""

    ACCEPT_AND_COMPLETE = "ACCEPT_AND_COMPLETE"
    REJECT_BEFORE_SIDE_EFFECT = "REJECT_BEFORE_SIDE_EFFECT"
    UNKNOWN_AFTER_SIDE_EFFECT = "UNKNOWN_AFTER_SIDE_EFFECT"
    FAIL_COMPLETION_READ = "FAIL_COMPLETION_READ"


class FakeCompletion:
    """确定性的完成结果：由 operation 账本派生，跨重启完全一致。"""

    __slots__ = (
        "task_key", "attempt_id", "operation_id", "result_state",
        "remote_state", "final_body", "claim_message_id",
    )

    def __init__(self, *, task_key: str, attempt_id: str, operation_id: str,
                 result_state: str, remote_state: str, final_body: str,
                 claim_message_id: str) -> None:
        self.task_key = task_key
        self.attempt_id = attempt_id
        self.operation_id = operation_id
        self.result_state = result_state
        self.remote_state = remote_state
        self.final_body = final_body
        self.claim_message_id = claim_message_id


def _marker(text: str, pattern: re.Pattern) -> str | None:
    match = pattern.search(text)
    if match is None:
        return None
    value = match.group(1)
    return value if value else None


class FakeExecutor:
    """一次发送资格 + 确定性完成的 Fake 执行端（T12）。

    script 决定远端行为；completion_read_fault 只在 FAIL_COMPLETION_READ 语义下
    被测试置位。全部完成判断用稳定字段，绝不使用 uuid4()/datetime.now()。
    """

    def __init__(self, script: str = FakeScript.ACCEPT_AND_COMPLETE, *,
                 db=None, completion_read_fault: bool = False) -> None:
        if script not in (FakeScript.ACCEPT_AND_COMPLETE,
                          FakeScript.REJECT_BEFORE_SIDE_EFFECT,
                          FakeScript.UNKNOWN_AFTER_SIDE_EFFECT,
                          FakeScript.FAIL_COMPLETION_READ):
            raise FakeCompletionError(f"非法 FakeScript：{script!r}")
        self.script = script
        self.db = db
        self.completion_read_fault = completion_read_fault

        self.snapshot_calls = 0
        self.send_calls = 0
        self.completion_calls = 0
        self.sent_payloads: list[dict] = []
        self.send_calls_by_task: dict[str, int] = {}
        self.completion_calls_by_task: dict[str, int] = {}
        self.snapshot_in_txn: list[bool] = []
        self.send_in_txn: list[bool] = []
        self.remote_received = False

    # ------------------------------------------------------------ SendTransport

    def capture_pre_send_snapshot(self, *, endpoint: str, session_id: str,
                                  task_key: str) -> dict:
        self.snapshot_calls += 1
        if self.db is not None:
            self.snapshot_in_txn.append(self.db.in_transaction)
        return {"message_ids": ["fake-pre-snapshot"], "endpoint": endpoint}

    def send_once(self, *, endpoint: str, session_id: str, prompt_text: str,
                  operation_id: str) -> SendAttempt:
        self.send_calls += 1
        if self.db is not None:
            self.send_in_txn.append(self.db.in_transaction)
        task_key = _marker(prompt_text, _TASK_MARKER)
        attempt_id = _marker(prompt_text, _ATTEMPT_MARKER)
        wire_operation_id = _marker(prompt_text, _OPERATION_MARKER)
        if not (task_key and attempt_id and wire_operation_id):
            raise FakeCompletionError(
                "wire prompt 缺少 task/attempt/operation 归属标记，无法审计本次发送"
            )
        if wire_operation_id != operation_id:
            raise FakeCompletionError(
                "wire prompt 的 OPERATION_ID 标记与 send_once 的 operation_id 不一致"
            )
        self.sent_payloads.append(
            {
                "endpoint": endpoint, "session_id": session_id,
                "prompt_text": prompt_text, "operation_id": operation_id,
                "task_key": task_key, "attempt_id": attempt_id,
            }
        )
        self.send_calls_by_task[task_key] = self.send_calls_by_task.get(task_key, 0) + 1
        if self.script is FakeScript.REJECT_BEFORE_SIDE_EFFECT:
            return SendAttempt(
                outcome=SendOutcome.REJECTED,
                evidence={"promptDispatched": False, "reason": "fake_reject"},
            )
        self.remote_received = True  # 拒绝之外，POST 均已到达远端
        if self.script is FakeScript.UNKNOWN_AFTER_SIDE_EFFECT:
            raise TransportTimeoutError("Fake 模拟远端已收到但客户端超时（结果不明）")
        return SendAttempt(
            outcome=SendOutcome.ACCEPTED,
            remote_user_id=_REMOTE_USER_ID,
            evidence={"message_id": _REMOTE_MESSAGE_ID},
        )

    # ------------------------------------------------------------ completion

    def completion_for(self, operation_record) -> FakeCompletion:
        """由 operations 权威行构造确定性完成结果。

        标记解析 + 与账本一致性校验失败 → FakeCompletionError；
        completion_read_fault 置位（FAIL_COMPLETION_READ）→ FakeCompletionReadError。
        同一 operation_record 每次调用得到逐字节相同的完成结果。
        """
        if operation_record is None:
            raise FakeCompletionError("completion_for 收到 None operation_record")
        if self.completion_read_fault:
            raise FakeCompletionReadError(
                "FakeCompletionReadError：模拟完成读取故障（operation 保持 ACCEPTED）"
            )
        prompt = operation_record.prompt_text or ""
        task_key = _marker(prompt, _TASK_MARKER)
        attempt_id = _marker(prompt, _ATTEMPT_MARKER)
        wire_operation_id = _marker(prompt, _OPERATION_MARKER)
        if task_key is None or task_key != operation_record.task_key:
            raise FakeCompletionError(
                f"completion 标记 task_key={task_key!r} 与账本"
                f" {operation_record.task_key!r} 不一致"
            )
        if attempt_id is None or attempt_id != operation_record.attempt_id:
            raise FakeCompletionError(
                f"completion 标记 attempt_id={attempt_id!r} 与账本"
                f" {operation_record.attempt_id!r} 不一致"
            )
        if wire_operation_id is None or wire_operation_id != operation_record.operation_id:
            raise FakeCompletionError(
                f"completion 标记 operation_id={wire_operation_id!r} 与账本"
                f" {operation_record.operation_id!r} 不一致"
            )
        self.completion_calls += 1
        self.completion_calls_by_task[task_key] = (
            self.completion_calls_by_task.get(task_key, 0) + 1
        )
        final_body = (
            f"Fake 完成报告 | task={task_key} | attempt={attempt_id}"
            f" | op={operation_record.operation_id}"
        )
        claim_message_id = "fake-msg-" + sha256_hex(operation_record.operation_id)[:20]
        return FakeCompletion(
            task_key=task_key,
            attempt_id=attempt_id,
            operation_id=operation_record.operation_id,
            result_state="COMPLETED",
            remote_state="IDLE_VERIFIED",
            final_body=final_body,
            claim_message_id=claim_message_id,
        )