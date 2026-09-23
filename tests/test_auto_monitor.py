"""AutoMonitor 测试（全 fake，不起 HTTP、不碰 Qt/剪贴板）。

覆盖后台 monitor 的运行时闭环：submit→worker 包装首发送→watchdog→结果识别→
first_response/complete/ambiguous/interrupted 事件→ack 释放→下一任务。用
FakeMonitorController 替身，确定性同步调用 run_once()；仅一条用例验证真实线程 start/stop。
"""

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from auto_monitor import AutoMonitor
from controller import AUTO_RUNNING, AutoTaskIntake, WatchdogTick
from openchamber_client import CompactResult, TaskResultResult


class FakeMonitorController:
    """LiteController 替身：只实现 AutoMonitor 依赖的 6 个方法，全部可脚本化。"""

    def __init__(self, intake=None, tick_action="none", result=None, begin_ok=True,
                 compact=None) -> None:
        self.intake = intake
        self.tick_action = tick_action
        self.result = result
        self.begin_ok = begin_ok
        self.compact_result = compact if compact is not None else CompactResult(True, None)
        self.receive_calls: list[tuple[str, str, str]] = []
        self.finish_calls: list[str] = []
        self.compact_calls: list[str] = []
        self.tick_calls = 0
        self.inspect_calls = 0
        self.begin_calls = 0
        self._event_id: str | None = None

    def receive_auto_task(self, event_id, text, wrapper_template):
        self.receive_calls.append((event_id, text, wrapper_template))
        if self.intake is not None:
            intake = self.intake
        elif self._event_id is not None:
            intake = AutoTaskIntake(False, False)  # busy：已有任务占用
        else:
            intake = AutoTaskIntake(True, True)
        if intake.accepted:
            self._event_id = event_id
        return intake

    def watchdog_tick(self, now_ms=None):
        self.tick_calls += 1
        return WatchdogTick(AUTO_RUNNING, AUTO_RUNNING, self.tick_action)

    def inspect_auto_task_result(self):
        self.inspect_calls += 1
        if callable(self.result):
            return self.result()
        if self.result is not None:
            return self.result
        return TaskResultResult(True, False, None, None, False, False, None)

    def begin_interrupted_recovery(self, now_ms=None):
        self.begin_calls += 1
        return self.begin_ok

    def finish_auto_task(self, event_id):
        self.finish_calls.append(event_id)
        if event_id == self._event_id:
            self._event_id = None
            return True
        return False

    def compact_auto_task(self, event_id):
        self.compact_calls.append(event_id)
        return self.compact_result


def _types(monitor) -> list[str]:
    return [e["type"] for e in monitor.drain_events()]


# 6. RemoteTask submit 后 receive_auto_task 在 run_once 中执行
def test_submit_runs_receive_in_worker():
    fc = FakeMonitorController()
    mon = AutoMonitor(fc, interval_seconds=0.01)
    mon.submit_remote_task("e1", "hello", "{content}")
    assert fc.receive_calls == []  # 尚未 run_once（主线程 0 HTTP）
    mon.run_once()
    assert fc.receive_calls == [("e1", "hello", "{content}")]
    assert "intake_running" in _types(mon)


# 7. intake READY 事件
def test_intake_ready_event():
    fc = FakeMonitorController(intake=AutoTaskIntake(True, False))
    mon = AutoMonitor(fc, 0.01)
    mon.submit_remote_task("e1", "x", "{content}")
    mon.run_once()
    assert _types(mon) == ["intake_ready"]


# 8. intake RUNNING 事件
def test_intake_running_event():
    fc = FakeMonitorController(intake=AutoTaskIntake(True, True))
    mon = AutoMonitor(fc, 0.01)
    mon.submit_remote_task("e1", "x", "{content}")
    mon.run_once()
    assert _types(mon) == ["intake_running"]


# 9. busy 事件（无活动任务 → 不跑 watchdog）
def test_intake_busy_event_no_watchdog():
    fc = FakeMonitorController(intake=AutoTaskIntake(False, False))
    mon = AutoMonitor(fc, 0.01)
    mon.submit_remote_task("e1", "x", "{content}")
    mon.run_once()
    assert _types(mon) == ["intake_busy"]
    assert fc.tick_calls == 0  # busy → 无活动任务 → 不 watchdog


# 10. watchdog action 正确转成 monitor event
def test_watchdog_action_maps_to_event():
    fc = FakeMonitorController(intake=AutoTaskIntake(True, True), tick_action="none")
    mon = AutoMonitor(fc, 0.01)
    mon.submit_remote_task("e1", "x", "{content}")
    mon.run_once()
    mon.drain_events()  # 清掉首轮 intake_running
    fc.tick_action = "resume_sent"
    mon.run_once()
    assert _types(mon) == ["resume_sent"]


# 11. first_response_ms 只发一次（后续轮询不重复）
def test_first_response_emitted_once():
    fr = TaskResultResult(True, False, None, 320, False, False, None)
    fc = FakeMonitorController(intake=AutoTaskIntake(True, True), result=fr)
    mon = AutoMonitor(fc, 0.01)
    mon.submit_remote_task("e1", "x", "{content}")
    mon.run_once()
    first = [e for e in mon.drain_events() if e["type"] == "first_response"]
    assert len(first) == 1 and first[0]["first_response_ms"] == 320
    mon.run_once()
    again = [e for e in mon.drain_events() if e["type"] == "first_response"]
    assert again == []  # 不重复发


# 12. complete 只发一次；ack 前重复 run_once 不重复发
def test_result_complete_once_no_repeat_before_ack():
    done = TaskResultResult(True, True, "DONE", 100, False, False, None)
    fc = FakeMonitorController(intake=AutoTaskIntake(True, True), result=done)
    mon = AutoMonitor(fc, 0.01)
    mon.submit_remote_task("e1", "x", "{content}")
    mon.run_once()
    evs = [e for e in mon.drain_events() if e["type"] == "result_complete"]
    assert len(evs) == 1 and evs[0]["text"] == "DONE" and evs[0]["event_id"] == "e1"
    # ack 前重复 run_once → paused → 不重复发、不再 inspect
    mon.run_once()
    mon.drain_events()
    mon.run_once()
    evs2 = [e for e in mon.drain_events() if e["type"] == "result_complete"]
    assert evs2 == []
    assert fc.inspect_calls == 1


# 13. ack 后调用 finish_auto_task → 下一任务可接收
def test_ack_releases_and_allows_next():
    done = TaskResultResult(True, True, "DONE", 100, False, False, None)
    fc = FakeMonitorController(intake=AutoTaskIntake(True, True), result=done)
    mon = AutoMonitor(fc, 0.01)
    mon.submit_remote_task("e1", "x", "{content}")
    mon.run_once()
    mon.drain_events()  # result_complete
    mon.acknowledge_result("e1")
    mon.run_once()  # worker 处理 ack → finish_auto_task("e1")
    mon.drain_events()
    assert fc.finish_calls == ["e1"]
    # 下一条可接收
    mon.submit_remote_task("e2", "y", "{content}")
    mon.run_once()
    assert fc.receive_calls == [("e1", "x", "{content}"), ("e2", "y", "{content}")]


# 13b. ack 的 event_id 不匹配当前任务 → 不 compact / 不 finish / 不清（绝不清错任务）
def test_ack_wrong_event_keeps_task():
    fc = FakeMonitorController()  # 默认 intake：无任务→接收，已有任务→busy
    mon = AutoMonitor(fc, 0.01)
    mon.submit_remote_task("e1", "x", "{content}")
    mon.run_once()
    mon.drain_events()
    mon.acknowledge_result("not_e1")
    mon.run_once()
    mon.drain_events()
    assert fc.finish_calls == []  # 迟到/错 ack：不 finish
    assert fc.compact_calls == []  # 也不 compact
    # 当前任务未被清：仍能收到重复 submit 的 busy
    mon.submit_remote_task("e9", "z", "{content}")
    mon.run_once()
    assert "intake_busy" in _types(mon)  # e1 仍占用


# 14. interrupted 触发 begin_interrupted_recovery → 不产生 result_complete
def test_interrupted_triggers_begin_no_result_complete():
    interrupted = TaskResultResult(True, False, None, None, False, True, None)  # interrupted=True
    fc = FakeMonitorController(intake=AutoTaskIntake(True, True), result=interrupted)
    mon = AutoMonitor(fc, 0.01)
    mon.submit_remote_task("e1", "x", "{content}")
    mon.run_once()
    mon.run_once()
    evs = [e["type"] for e in mon.drain_events()]
    assert fc.begin_calls >= 1  # 触发了 begin_interrupted_recovery
    assert "result_complete" not in evs
    assert "result_ambiguous" not in evs


# 15. ambiguous → 不产生 result_complete，只发一次冲突事件
def test_ambiguous_one_conflict_event_no_result_complete():
    ambiguous = TaskResultResult(True, False, None, None, True, False, None)  # ambiguous=True
    fc = FakeMonitorController(intake=AutoTaskIntake(True, True), result=ambiguous)
    mon = AutoMonitor(fc, 0.01)
    mon.submit_remote_task("e1", "x", "{content}")
    mon.run_once()
    first = [e for e in mon.drain_events() if e["type"] == "result_ambiguous"]
    assert len(first) == 1
    mon.run_once()  # paused → 不重复发
    second = [e for e in mon.drain_events() if e["type"] == "result_ambiguous"]
    assert second == []
    assert fc.begin_calls == 0  # ambiguous 不触发 interrupted recovery


# 16. monitor.start / stop 生命周期（真实线程）
def test_monitor_start_stop():
    fc = FakeMonitorController()
    mon = AutoMonitor(fc, interval_seconds=0.01)
    mon.start()
    try:
        assert mon._thread is not None and mon._thread.is_alive()
        mon.submit_remote_task("e1", "x", "{content}")
        deadline = time.time() + 2.0
        while not fc.receive_calls and time.time() < deadline:
            time.sleep(0.01)
        assert fc.receive_calls == [("e1", "x", "{content}")]  # worker 处理过
    finally:
        mon.stop()
    assert mon._thread is None


# ===================== L05-06 自动压缩冻结会话 =====================

DONE = TaskResultResult(True, True, "DONE", 100, False, False, None)


# 08. AutoMonitor 默认 auto_compact=False
def test_auto_compact_default_off():
    mon = AutoMonitor(FakeMonitorController(), 0.01)
    assert mon._auto_compact_enabled is False


# 09. set_auto_compact(True) → GUI 只入队，run_once 后 worker 更新；GUI 路径 0 HTTP
def test_set_auto_compact_updates_worker_state():
    fc = FakeMonitorController()
    mon = AutoMonitor(fc, 0.01)
    mon.set_auto_compact(True)
    assert mon._auto_compact_enabled is False  # 尚未 run_once，worker 未处理（GUI 无副作用）
    assert fc.compact_calls == []
    mon.run_once()  # worker 处理 set command
    assert mon._auto_compact_enabled is True
    mon.set_auto_compact(False)
    mon.run_once()
    assert mon._auto_compact_enabled is False


# 10. auto_compact OFF + 正确 ack → compact 0 次，finish 1 次
def test_auto_compact_off_ack_no_compact():
    fc = FakeMonitorController(intake=AutoTaskIntake(True, True), result=DONE)
    mon = AutoMonitor(fc, 0.01)
    mon.submit_remote_task("e1", "x", "{content}")
    mon.run_once()
    mon.drain_events()
    mon.acknowledge_result("e1")
    mon.run_once()
    assert fc.compact_calls == []
    assert fc.finish_calls == ["e1"]


# 11. auto_compact ON + 正确 ack → compact 1 次，finish 1 次
def test_auto_compact_on_ack_compacts_then_finishes():
    fc = FakeMonitorController(intake=AutoTaskIntake(True, True), result=DONE)
    mon = AutoMonitor(fc, 0.01)
    mon.set_auto_compact(True)
    mon.run_once()
    mon.submit_remote_task("e1", "x", "{content}")
    mon.run_once()
    mon.drain_events()
    mon.acknowledge_result("e1")
    mon.run_once()
    assert fc.compact_calls == ["e1"]
    assert fc.finish_calls == ["e1"]


# 12. 顺序：compact_auto_task 必须在 finish_auto_task 之前
def test_auto_compact_before_finish():
    seq: list[str] = []

    class OrderFC(FakeMonitorController):
        def compact_auto_task(self, eid):
            seq.append("compact")
            return super().compact_auto_task(eid)

        def finish_auto_task(self, eid):
            seq.append("finish")
            return super().finish_auto_task(eid)

    fc = OrderFC(intake=AutoTaskIntake(True, True), result=DONE)
    mon = AutoMonitor(fc, 0.01)
    mon.set_auto_compact(True)
    mon.run_once()
    mon.submit_remote_task("e1", "x", "{content}")
    mon.run_once()
    mon.drain_events()
    mon.acknowledge_result("e1")
    mon.run_once()
    assert seq == ["compact", "finish"]


# 13. compact success → compact_success 事件一次
def test_auto_compact_success_event():
    fc = FakeMonitorController(intake=AutoTaskIntake(True, True), result=DONE,
                               compact=CompactResult(True, None))
    mon = AutoMonitor(fc, 0.01)
    mon.set_auto_compact(True)
    mon.run_once()
    mon.submit_remote_task("e1", "x", "{content}")
    mon.run_once()
    mon.drain_events()
    mon.acknowledge_result("e1")
    mon.run_once()
    evs = [e for e in mon.drain_events() if e["type"] == "compact_success"]
    assert len(evs) == 1
    assert evs[0]["event_id"] == "e1"


# 14. compact failure → compact_failed 事件一次，且仍 finish 当前任务
def test_auto_compact_failed_event_still_finishes():
    fc = FakeMonitorController(intake=AutoTaskIntake(True, True), result=DONE,
                               compact=CompactResult(False, "http: 500"))
    mon = AutoMonitor(fc, 0.01)
    mon.set_auto_compact(True)
    mon.run_once()
    mon.submit_remote_task("e1", "x", "{content}")
    mon.run_once()
    mon.drain_events()
    mon.acknowledge_result("e1")
    mon.run_once()
    evs = [e for e in mon.drain_events() if e["type"] == "compact_failed"]
    assert len(evs) == 1
    assert evs[0]["event_id"] == "e1"
    assert evs[0]["error"] == "http: 500"
    assert fc.finish_calls == ["e1"]


# 15. compact failure 后 → flags reset，下一 RemoteTask 可接收
def test_auto_compact_failed_resets_allows_next():
    fc = FakeMonitorController(intake=AutoTaskIntake(True, True), result=DONE,
                               compact=CompactResult(False, "http: 500"))
    mon = AutoMonitor(fc, 0.01)
    mon.set_auto_compact(True)
    mon.run_once()
    mon.submit_remote_task("e1", "x", "{content}")
    mon.run_once()
    mon.drain_events()
    mon.acknowledge_result("e1")
    mon.run_once()
    mon.drain_events()
    mon.submit_remote_task("e2", "y", "{content}")
    mon.run_once()
    assert fc.receive_calls == [("e1", "x", "{content}"), ("e2", "y", "{content}")]


# 16. stale/wrong ack → compact 0 / finish 0 / active 任务保留
def test_stale_ack_no_compact_no_finish():
    fc = FakeMonitorController()
    mon = AutoMonitor(fc, 0.01)
    mon.set_auto_compact(True)
    mon.run_once()
    mon.submit_remote_task("e1", "x", "{content}")
    mon.run_once()
    mon.drain_events()
    mon.acknowledge_result("not_e1")
    mon.run_once()
    mon.drain_events()
    assert fc.compact_calls == []
    assert fc.finish_calls == []
    mon.submit_remote_task("e9", "z", "{content}")
    mon.run_once()
    assert "intake_busy" in _types(mon)  # e1 仍占用


# 17. ambiguous → 自动 compact 0 次（未产生 result_complete → 不 ack）
def test_ambiguous_no_auto_compact():
    ambiguous = TaskResultResult(True, False, None, None, True, False, None)
    fc = FakeMonitorController(intake=AutoTaskIntake(True, True), result=ambiguous,
                               compact=CompactResult(True, None))
    mon = AutoMonitor(fc, 0.01)
    mon.set_auto_compact(True)
    mon.run_once()
    mon.submit_remote_task("e1", "x", "{content}")
    mon.run_once()
    mon.run_once()
    mon.drain_events()
    assert fc.compact_calls == []
    assert fc.finish_calls == []


# 18. interrupted/recovering → 自动 compact 0 次
def test_interrupted_no_auto_compact():
    interrupted = TaskResultResult(True, False, None, None, False, True, None)
    fc = FakeMonitorController(intake=AutoTaskIntake(True, True), result=interrupted,
                               compact=CompactResult(True, None))
    mon = AutoMonitor(fc, 0.01)
    mon.set_auto_compact(True)
    mon.run_once()
    mon.submit_remote_task("e1", "x", "{content}")
    mon.run_once()
    mon.run_once()
    mon.drain_events()
    assert fc.compact_calls == []
    assert fc.finish_calls == []


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))