"""Lite 最小 Controller：把"当前会话读取 + 手动原样发送 + 立即压缩"串起来。

只编排既有底层（active_session_reader 只读取会话、openchamber_client 读写），
不引入 Scheduler/Operation/Attempt/Lease/Binding/UNKNOWN/CAS/SQLite/command
bus/repository/service 等任何额外层。依赖注入：client 与 active_session_reader
均可替换，测试全程 fake、不起 HTTP、不读真实 LevelDB。
"""

from __future__ import annotations

from dataclasses import dataclass

from active_session_reader import read_active_session
from openchamber_client import CompactResult, OpenChamberClient, SendResult

_DEFAULT_BASE_URL = "http://127.0.0.1:57123"

# 自动任务最小状态机：只有三态，无历史、无 OperationId、无队列。
AUTO_IDLE = "idle"
AUTO_READY = "ready_to_send"
AUTO_RUNNING = "running"


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
    """单任务自动链最小状态：只有 IDLE/READY_TO_SEND/RUNNING，可就地改 state。

    包装在任务到达时一次性完成并固化到 wrapped_text；模型恢复/重试只重发
    这个已保存的 wrapped_text，绝不重新读取包装模板、重新包装 raw text。
    """

    event_id: str
    wrapped_text: str
    state: str = AUTO_IDLE
    message_id: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AutoTaskIntake:
    """receive_auto_task 的稳定返回：accepted=是否接收(非 busy)，submitted=是否已提交(→RUNNING)。"""

    accepted: bool
    submitted: bool


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
        已有 READY_TO_SEND 或 RUNNING 任务时返回 busy，不覆盖、不重发。
        """
        if self._auto_task is not None and self._auto_task.state in (AUTO_READY, AUTO_RUNNING):
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
            task.error = None
        else:
            task.error = result.error

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