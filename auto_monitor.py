"""极小后台 AutoMonitor：把自动任务链的 OpenChamber HTTP 全部放进单个后台 worker 线程。

Qt 主线程绝不跑 watchdog / 结果轮询的 HTTP：GUI 通过 submit_remote_task / acknowledge_result
把命令丢进线程安全队列，worker 线程串行执行 controller 的自动任务状态机，并把简单事件
（intake_ready/intake_running/intake_busy/sent/offline/recover_check/resumed_automatically/
resume_sent/first_response/result_complete/result_ambiguous/compact_success/compact_failed）
放进事件队列供 GUI drain。

只用标准库 threading + queue，不引入 asyncio/multiprocessing/scheduler/SQLite。
单个 worker 线程保证同一时刻只有一个 watchdog HTTP 在执行。
"""

from __future__ import annotations

import queue
import threading

# watchdog action → 事件 type（none 不发事件）
_WORKDOG_EVENTS = frozenset(
    {"sent", "submit_confirmed", "offline", "recover_check", "resumed_automatically", "resume_sent"}
)


class AutoMonitor:
    """后台自动任务 monitor：单 worker 线程循环 run_once()。

    controller 由外部注入（依赖 LiteController 的 receive_auto_task / watchdog_tick /
    inspect_auto_task_result / begin_interrupted_recovery / compact_auto_task /
    finish_auto_task / identity_snapshot 七个方法）。
    monitor 自己不访问 ClipLink 状态、不写剪贴板、不操作 Qt、不调 bridge.deliver_result。
    """

    def __init__(self, controller, interval_seconds: float = 1.0) -> None:
        self._controller = controller
        self._interval = interval_seconds
        self._cmd: "queue.Queue[tuple]" = queue.Queue()
        self._events: "queue.Queue[dict]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # 当前任务运行时标志（仅 worker 线程读写；GUI 只读事件队列）
        self._active_event_id: str | None = None
        self._first_response_ms: int | None = None
        self._first_response_sent = False
        self._result_complete_sent = False
        self._result_ambiguous_sent = False
        self._result_interrupted_sent = False
        self._paused = False
        # 自动压缩运行时开关：仅 worker 线程读写，默认 OFF，不持久化。
        # GUI 经 set_auto_compact 入队更新；用 ack 处理时的最新值，不冻结到任务创建时。
        self._auto_compact_enabled = False

    # ---------------- GUI 线程调用（线程安全：只入队，不做 HTTP）----------------
    def submit_remote_task(self, event_id: str, text: str, wrapper_template: str,
                       attempt: int | None = None) -> None:
        """把 A端 RemoteTask 交给后台 worker 包装并首发送（主线程 0 HTTP）。

        attempt 是 Bridge 内部投递轮次令牌（阶段 3F-2）：worker 的 accepted/busy
        回执原样带回，GUI 据此匹配对应投递轮，防跨 busy 重试的旧回执误配；
        既有无令牌直接调用传 None，按 event_id 匹配，行为兼容。
        """
        self._cmd.put(("submit", event_id, text, wrapper_template, attempt))

    def set_auto_compact(self, enabled: bool) -> None:
        """GUI 线程调用：只把开关更新入队，不做任何 OpenChamber HTTP；worker 收到后生效。"""
        self._cmd.put(("set_auto_compact", bool(enabled)))

    def acknowledge_result(self, event_id: str) -> None:
        """GUI 已把最终结果交给 ClipLinkBridge 后调用 → 后台释放当前任务。"""
        self._cmd.put(("ack", event_id))

    def drain_events(self) -> list[dict]:
        """GUI 线程取走全部待处理事件（非阻塞），供 QTimer 周期性调用。"""
        out = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    # ---------------- 线程生命周期 ----------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 1.0)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self._drain_commands()
            self.run_once()
            self._stop.wait(self._interval)

    # ---------------- worker 线程 / 单测可同步调用 ----------------
    def run_once(self) -> None:
        """单轮：先处理命令，再（若有活动任务且未暂停）跑 watchdog + 结果识别。

        供后台线程循环调用；也可在单测里同步调用以获得确定性。
        """
        self._drain_commands()
        if self._active_event_id is None or self._paused:
            return
        action = self._controller.watchdog_tick().action
        if action in _WORKDOG_EVENTS:
            self._emit({"type": action})
        self._handle_result()

    def _drain_commands(self) -> None:
        while True:
            try:
                cmd = self._cmd.get_nowait()
            except queue.Empty:
                return
            if cmd[0] == "submit":
                self._handle_submit(cmd[1], cmd[2], cmd[3], cmd[4] if len(cmd) > 4 else None)
            elif cmd[0] == "ack":
                self._handle_ack(cmd[1])
            elif cmd[0] == "set_auto_compact":
                self._auto_compact_enabled = bool(cmd[1])

    def _handle_submit(self, event_id: str, text: str, template: str,
                   attempt: int | None = None) -> None:
        """worker：包装+首发送；busy 不替换当前任务、0 次模型 HTTP（busy 判定在 try_send 之前）。

        回执事件携带 event_id + attempt（阶段 3F-2）：GUI 匹配 Bridge 在途单槽后
        accepted 释放/记 consumed，busy 回队首（不标 consumed、不丢正文）。
        """
        intake = self._controller.receive_auto_task(event_id, text, template)
        if not intake.accepted:
            self._emit({"type": "intake_busy", "event_id": event_id, "attempt": attempt})
            return
        self._active_event_id = event_id
        self._first_response_ms = None
        self._first_response_sent = False
        self._result_complete_sent = False
        self._result_ambiguous_sent = False
        self._result_interrupted_sent = False
        self._paused = False
        if intake.superseded_event_id is not None:
            self._emit({"type": "previous_turn_superseded", "event_id": intake.superseded_event_id})
        self._emit(
            {
                "type": (
                    "intake_running" if intake.submitted else
                    "intake_unknown" if getattr(self._controller, "auto_task_submit_unknown", lambda: False)() else "intake_ready"
                ),
                "event_id": event_id,
                "attempt": attempt,
            }
        )

    def _handle_ack(self, event_id: str) -> None:
        """GUI 已把结果交给 Bridge 后释放任务：结果先回传，compact 才执行（绝不阻挡回传）。

        event_id 不匹配当前活动任务（迟到/错 ack）→ 不 compact / 不 finish / 不 reset，
        防压错、清错任务。auto_compact 开 → 用冻结 session compact 一次并发事件；
        compact 失败只记 compact_failed，仍 finish（compact 失败 != 任务失败）。
        """
        if event_id != self._active_event_id:
            return
        if self._auto_compact_enabled:
            result = self._controller.compact_auto_task(event_id)
            if result.success:
                self._emit({"type": "compact_success", "event_id": event_id})
            else:
                self._emit(
                    {"type": "compact_failed", "event_id": event_id, "error": result.error or ""}
                )
        if self._controller.finish_auto_task(event_id):
            self._reset_task_flags()

    def _reset_task_flags(self) -> None:
        self._active_event_id = None
        self._first_response_ms = None
        self._first_response_sent = False
        self._result_complete_sent = False
        self._result_ambiguous_sent = False
        self._result_interrupted_sent = False
        self._paused = False

    def _handle_result(self) -> None:
        result = self._controller.inspect_auto_task_result()
        if not result.read_ok:
            return
        # 首响应：每个 AutoTask 只发一次，后续 resume 的响应不得覆盖
        if result.first_response_ms is not None and not self._first_response_sent:
            self._first_response_sent = True
            self._first_response_ms = result.first_response_ms
            self._emit(
                {
                    "type": "first_response",
                    "event_id": self._active_event_id,
                    "first_response_ms": result.first_response_ms,
                }
            )
        if result.complete:
            if not self._result_complete_sent:
                self._result_complete_sent = True
                self._paused = True  # 等待 GUI ack 后再释放，绝不每秒重复发
                self._emit(
                    {
                        "type": "result_complete",
                        "event_id": self._active_event_id,
                        "text": result.text,
                        "first_response_ms": self._first_response_ms,
                        # 冻结身份快照（不可变值，与 event_id 一致）；身份无效时
                        # 原始结果仍交给 GUI 展示，但 GUI 不得据此 deliver/ack
                        "identity": self._controller.identity_snapshot(self._active_event_id),
                    }
                )
            return
        if result.ambiguous:
            # 结果冲突（同 session 有人工插入）→ 只发一次冲突事件，任务保持占用、不自动清理
            if not self._result_ambiguous_sent:
                self._result_ambiguous_sent = True
                self._paused = True
                self._emit({"type": "result_ambiguous", "event_id": self._active_event_id})
            return
        if result.interrupted:
            if getattr(self._controller, "auto_task_is_local", lambda: None)() is False:
                if not self._result_interrupted_sent:
                    self._result_interrupted_sent = True
                    self._paused = True
                    self._emit({"type": "result_interrupted", "event_id": self._active_event_id})
                return
            # 服务在线但本次执行已中断 → 复用 watchdog 恢复流程（observe 5s → resume 一次）
            self._controller.begin_interrupted_recovery()

    def _emit(self, event: dict) -> None:
        self._events.put(event)
