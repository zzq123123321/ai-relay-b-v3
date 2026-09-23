"""Lite 最小 Controller：把"当前会话读取 + 手动原样发送 + 立即压缩"串起来。

只编排既有底层（active_session_reader 只读取会话、openchamber_client 读写），
不引入 Scheduler/Operation/Attempt/Lease/Binding/UNKNOWN/CAS/SQLite/command
bus/repository/service 等任何额外层。依赖注入：client 与 active_session_reader
均可替换，测试全程 fake、不起 HTTP、不读真实 LevelDB。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from active_session_reader import read_active_session
from openchamber_client import (
    CompactResult,
    OpenChamberClient,
    SendResult,
    TaskResultResult,
)

_DEFAULT_BASE_URL = "http://127.0.0.1:57123"

# 自动任务最小状态机，无历史、无 OperationId、无队列。
AUTO_IDLE = "idle"
AUTO_READY = "ready_to_send"
AUTO_RUNNING = "running"
AUTO_MODEL_OFFLINE = "model_offline"
AUTO_RECOVER_CHECK = "recover_check"
AUTO_RESUME_SENT = "resume_sent"

# 中断恢复观察窗口：连续成功读到"无进展"满此值才允许发一次续接。
RECOVER_OBSERVE_MS = 5000
# 固定续接文本，与 wrapper / A端新消息 / A端连接状态完全无关。
RESUME_PROMPT = "继续执行刚才未完成的任务，从中断处继续，不要重新开始。"


@dataclass(frozen=True, slots=True)
class CurrentSession:
    """get_current_session() 的稳定返回：永不抛异常，失败信息收敛到 error。"""

    session_id: str | None
    directory: str | None
    source: str | None
    valid: bool
    error: str | None


@dataclass(frozen=True, slots=True)
class ModelConnectionResult:
    """test_model_connection() 的稳定返回：严格区分 服务可达 与 配置就绪。

    connected = OpenChamber 服务是否可达（probe）。
    service_latency_ms = probe 测得的服务延迟（服务不可达时也可能非空）。
    ready = 当前会话是否已有可复用的模型执行配置。
    本轮不向模型发 prompt，connected/ready 均不代表已完成一次真实推理。
    """

    connected: bool
    service_latency_ms: int | None
    ready: bool
    error: str | None


@dataclass
class AutoTask:
    """单任务自动链最小状态：IDLE/READY/RUNNING/MODEL_OFFLINE/RECOVER_CHECK/RESUME_SENT。

    包装在任务到达时一次性完成并固化到 wrapped_text；模型恢复/重试只重发
    这个已保存的 wrapped_text，绝不重新读取包装模板、重新包装 raw text。
    首次发送 accepted 后冻结 session_id/directory/message_id：watchdog、
    progress check、resume_prompt 全部回到原 session，不随 UI 当前会话切换。
    """

    event_id: str
    wrapped_text: str
    state: str = AUTO_IDLE
    message_id: str | None = None
    # 系统自动续接（resume_prompt）send accepted 后保存的 user message id：
    # 最终结果解析据此识别"这条后续 user 是自动续接，不是用户手动插的新任务"。
    resume_message_id: str | None = None
    error: str | None = None
    # 首次 accepted 后冻结的原执行 session 目标
    session_id: str | None = None
    directory: str | None = None
    # watchdog 中断恢复周期
    recover_baseline: str | None = None
    recover_started_ms: int | None = None
    resume_attempted: bool = False


@dataclass(frozen=True, slots=True)
class AutoTaskIntake:
    """receive_auto_task 的稳定返回：accepted=是否接收(非 busy)，submitted=是否已提交(→RUNNING)。"""

    accepted: bool
    submitted: bool


@dataclass(frozen=True, slots=True)
class WatchdogTick:
    """watchdog_tick() 的稳定小结果（供后续 UI 接线）：不含事件总线。

    action ∈ none/sent/offline/recover_check/resumed_automatically/resume_sent。
    """

    previous_state: str
    state: str
    action: str
    error: str | None = None


class LiteController:
    def __init__(
        self,
        client: OpenChamberClient | None = None,
        *,
        active_session_reader=None,
        base_url: str = _DEFAULT_BASE_URL,
    ) -> None:
        self._client = client if client is not None else OpenChamberClient(base_url)
        self._read_active_session = (
            active_session_reader if active_session_reader is not None else read_active_session
        )
        self._auto_task: AutoTask | None = None

    # ------------------------------------------------------------- 自动任务链
    @staticmethod
    def wrap_auto_content(raw_text: str, wrapper_template: str) -> str:
        """把 raw_text 套进 wrapper_template：空模板原样；含 {content} 全替换；否则模板+换行+原文。

        严格不改写 raw_text（不 strip、不换行、不追加系统提示）。
        """
        if wrapper_template == "":
            return raw_text
        if "{content}" in wrapper_template:
            return wrapper_template.replace("{content}", raw_text)
        return wrapper_template + "\n" + raw_text

    def receive_auto_task(self, event_id: str, raw_text: str, wrapper_template: str) -> AutoTaskIntake:
        """A端 RemoteTask 入口：立即包装固化 → READY_TO_SEND → 单次尝试发送。

        包装与模型/OpenChamber/A端连接是否在线完全无关：到达即固化 wrapped_text。
        只要已有 AutoTask 且 state != IDLE 即返回 busy（READY_TO_SEND / RUNNING /
        MODEL_OFFLINE / RECOVER_CHECK / RESUME_SENT 均算 busy），不覆盖、不重发。
        """
        if self._auto_task is not None and self._auto_task.state != AUTO_IDLE:
            return AutoTaskIntake(False, False)
        self._auto_task = AutoTask(
            event_id=event_id,
            wrapped_text=self.wrap_auto_content(raw_text, wrapper_template),
            state=AUTO_READY,
        )
        self.try_send_pending_auto_task()
        return AutoTaskIntake(True, self._auto_task.state == AUTO_RUNNING)

    def try_send_pending_auto_task(self) -> None:
        """仅处理 READY_TO_SEND：先探模型可用，再对已保存的 wrapped_text 发一次。

        模型不可用 → 保持 READY_TO_SEND（由 L05-02 watchdog 恢复时再调）。
        RUNNING 再调 → 0 次 POST（原任务不重发）。
        """
        task = self._auto_task
        if task is None or task.state != AUTO_READY:
            return
        conn = self.test_model_connection()
        if not (conn.connected and conn.ready):
            return
        session = self._current_session()
        if not session.valid:
            return
        result = self._client.send_text(session.session_id, session.directory, task.wrapped_text)
        if result.accepted:
            task.state = AUTO_RUNNING
            task.message_id = result.message_id
            # 冻结原执行 session：一旦 accepted，恢复目标不再随 UI 当前会话变化
            task.session_id = session.session_id
            task.directory = session.directory
            task.error = None
        else:
            task.error = result.error

    def inspect_auto_task_result(self) -> TaskResultResult:
        """只读：识别当前自动任务是否已产生可回传的最终结果。

        本轮只识别，不负责交付/清理（绝不把 AutoTask 清回 IDLE）。
        没有自动任务，或任务还没真正发出（无冻结 message_id）→ complete=False。
        有冻结目标时，用冻结的 session_id/directory/message_id 调
        client.get_task_result（不读 UI 当前激活会话）；allowed follow-up 在
        resume_message_id 有值时加入，避免把系统自动续接误判为用户手动新任务。
        """
        task = self._auto_task
        if task is None or task.message_id is None:
            return TaskResultResult(True, False, None, None, False, False, None)
        allowed = {task.resume_message_id} if task.resume_message_id else None
        return self._client.get_task_result(
            task.session_id, task.directory, task.message_id, allowed
        )

    # ------------------------------------------------------------- 模型 watchdog
    def watchdog_tick(self, now_ms: int | None = None) -> WatchdogTick:
        """纯控制入口：推进自动任务中断恢复状态机，本轮由测试/后续 worker 调用。

        不碰 A端 ClipLink 状态。返回上一状态/新状态/动作/错误的小结果。
        """
        task = self._auto_task
        if task is None:
            return WatchdogTick(AUTO_IDLE, AUTO_IDLE, "none")
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        prev = task.state
        action = "none"

        if task.state == AUTO_READY:
            self.try_send_pending_auto_task()
            if task.state == AUTO_RUNNING:
                action = "sent"

        elif task.state == AUTO_RUNNING:
            # 只 probe；OpenChamber 正常（哪怕 session status=idle）保持 RUNNING。
            # idle 很可能是正常完成，最终结果识别交给 L05-03，绝不因此发 resume。
            if not self._client.probe().connected:
                task.state = AUTO_MODEL_OFFLINE
                task.resume_attempted = False
                task.resume_message_id = None
                task.recover_baseline = None
                task.recover_started_ms = None
                action = "offline"

        elif task.state == AUTO_MODEL_OFFLINE:
            if self._client.probe().connected:
                prog = self._client.get_task_progress(task.session_id, task.directory, task.message_id)
                if prog.read_ok:
                    task.recover_baseline = prog.marker
                    task.recover_started_ms = now_ms
                    task.resume_attempted = False
                    task.resume_message_id = None
                    task.state = AUTO_RECOVER_CHECK
                    action = "recover_check"
                # 原 session/messages 暂时读不到 → 不发 resume，保持 MODEL_OFFLINE 等下一 tick

        elif task.state == AUTO_RECOVER_CHECK:
            action = self._recover_check_tick(task, now_ms)

        elif task.state == AUTO_RESUME_SENT:
            if not self._client.probe().connected:
                task.state = AUTO_MODEL_OFFLINE
                task.resume_attempted = False
                task.resume_message_id = None
                task.recover_baseline = None
                task.recover_started_ms = None
                action = "offline"
            else:
                progressed = self._observed_progress(task)
                if progressed:
                    task.state = AUTO_RUNNING
                    action = "resumed_automatically"
                # 仍 idle 且无变化 → 保持 RESUME_SENT，不重复发续接

        return WatchdogTick(prev, task.state, action, task.error)

    def _observed_progress(self, task) -> bool:
        """status busy/retry 或 progress marker 相对基线已变化 → 视为已自行恢复。

        任一读取失败都不算"无进展"（避免误判）；仅在成功读到且确无变化时返回 False。
        """
        st = self._client.get_session_status(task.session_id)
        if st.ok and st.status in ("busy", "retry"):
            return True
        prog = self._client.get_task_progress(task.session_id, task.directory, task.message_id)
        if prog.read_ok and task.recover_baseline is not None and prog.marker != task.recover_baseline:
            return True
        return False

    def _recover_check_tick(self, task, now_ms: int) -> str:
        """RECOVER_CHECK：观察是否自行恢复；连续确认无进展满窗口才发一次固定续接。"""
        st = self._client.get_session_status(task.session_id)
        prog = self._client.get_task_progress(task.session_id, task.directory, task.message_id)
        # 自动恢复：status busy/retry，或 marker 相对基线已变化
        auto = (st.ok and st.status in ("busy", "retry")) or (
            prog.read_ok and task.recover_baseline is not None and prog.marker != task.recover_baseline
        )
        if auto:
            task.state = AUTO_RUNNING
            return "resumed_automatically"
        if not (st.ok and prog.read_ok):
            # 读取失败 ≠ 无进展：不判定、不发 resume，继续观察
            return "none"
        if task.resume_attempted:
            # 本恢复周期最多发一次续接，绝不逐 tick 重复
            return "none"
        started = task.recover_started_ms if task.recover_started_ms is not None else now_ms
        if now_ms - started >= RECOVER_OBSERVE_MS:
            result = self._client.send_text(task.session_id, task.directory, RESUME_PROMPT)
            task.resume_attempted = True
            if result.accepted:
                task.state = AUTO_RESUME_SENT
                task.error = None
                if result.message_id:
                    task.resume_message_id = result.message_id
                return "resume_sent"
            task.error = result.error
        return "none"

    def _current_session(self) -> CurrentSession:
        session = self._read_active_session()
        if session is None:
            return CurrentSession(None, None, None, False, "当前激活会话不可用")
        if self._client.validate_session(session.session_id, session.directory):
            return CurrentSession(
                session.session_id, session.directory, session.source, True, None
            )
        return CurrentSession(
            session.session_id,
            session.directory,
            session.source,
            False,
            "会话校验失败：服务端未确认该会话可用",
        )

    def get_current_session(self) -> CurrentSession:
        """读取并核实当前激活会话；每次调用都重新读取，不缓存、不猜其它会话。"""
        return self._current_session()

    def manual_send(self, text: str) -> SendResult:
        """把 text 原样发送到当前激活会话（不包装、不修改文本）。

        空/纯空白文本直接拒绝；无有效会话则返回不可用；否则透传底层
        send_text 的 accepted/error/message_id。accepted != 模型执行完成。
        """
        if text is None or text.strip() == "":
            return SendResult(False, "文本为空", None)
        session = self._current_session()
        if not session.valid:
            return SendResult(False, session.error, None)
        return self._client.send_text(session.session_id, session.directory, text)

    def compact_current_session(self) -> CompactResult:
        """立即压缩当前激活会话（仅手动，无自动/计数/阈值/定时器）。"""
        session = self._current_session()
        if not session.valid:
            return CompactResult(False, session.error)
        return self._client.compact_session(session.session_id, session.directory)

    def test_model_connection(self) -> ModelConnectionResult:
        """无侵入检测大模型可用性：probe 服务可达 + 当前会话是否已有可复用模型配置。

        本轮不向模型发 prompt。connected 只反映 OpenChamber 服务可达，ready 只反映
        当前会话是否有可复用执行配置；二者都不等于“已真实完成一次模型推理”。
        """
        probe = self._client.probe()
        if not probe.connected:
            return ModelConnectionResult(False, probe.latency_ms, False, probe.error)
        session = self._current_session()
        if not session.valid:
            return ModelConnectionResult(True, probe.latency_ms, False, None)
        config = self._client.resolve_execution_config(session.session_id, session.directory)
        return ModelConnectionResult(True, probe.latency_ms, config is not None, None)