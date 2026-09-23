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


@dataclass(frozen=True, slots=True)
class CurrentSession:
    """get_current_session() 的稳定返回：永不抛异常，失败信息收敛到 error。"""

    session_id: str | None
    directory: str | None
    source: str | None
    valid: bool
    error: str | None


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