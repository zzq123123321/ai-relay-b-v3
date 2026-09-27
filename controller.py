"""Lite 最小 Controller：把"当前会话读取 + 手动原样发送 + 立即压缩"串起来。

只编排既有底层（active_session_reader 只读取会话、openchamber_client 读写），
不引入 Scheduler/Operation/Attempt/Lease/Binding/UNKNOWN/CAS/SQLite/command
bus/repository/service 等任何额外层。依赖注入：client 与 active_session_reader
均可替换，测试全程 fake、不起 HTTP、不读真实 LevelDB。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from active_session_reader import read_active_session
from cliplink_bridge import AI_RELAY_END_MARKER, AI_RELAY_BEGIN_MARKER
from openchamber_client import (
    CompactResult,
    OpenChamberClient,
    ModelTarget,
    SendResult,
    TaskResultResult,
)
from model_probe import load_model_base_url, probe_model_endpoint

# legacy 任务身份解析规则（对齐旧版 _parse_legacy_web_message/_parse_rounds，
# 旧版 core/protocol.py 仅作参考，不作运行时依赖）
_LEGACY_REQUIRED_KEYS = ("TASK_ID", "SOURCE", "TARGET", "TYPE")
_LEGACY_KNOWN_TYPES = ("TASK", "RESPONSE")
# str.splitlines 认知的全部行分隔符（解析副本语义 + 纯封装 id 注入防护共用）
_LINE_BREAK_CHARS = frozenset("\r\n\u000b\u000c\u001c\u001d\u001e\u0085\u2028\u2029")

_DEFAULT_BASE_URL = "http://127.0.0.1:57123"

# 自动任务最小状态机，无历史、无 OperationId、无队列。
AUTO_IDLE = "idle"
AUTO_READY = "ready_to_send"
AUTO_SUBMIT_UNKNOWN = "submit_unknown"
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

    包装在任务到达时一次性完成并固化到 wrapped_text；原任务身份
    （TASK_ID/ROUND/MAX_ROUNDS，见 parse_auto_task_identity）在套模板之前
    解析一次并冻结到 task_identity：busy 拒绝不覆盖、切会话/改模板/续接不变。
    模型恢复/重试只重发这个已保存的 wrapped_text，绝不重新读取包装模板、
    重新包装 raw text。
    首次发送 accepted 后冻结 session_id/directory/message_id：watchdog、
    progress check、resume_prompt 全部回到原 session，不随 UI 当前会话切换。
    """

    event_id: str
    wrapped_text: str
    state: str = AUTO_IDLE
    message_id: str | None = None
    uncertain_baseline_user_id: str | None = None
    can_reconcile_submission: bool = False
    local_model: bool | None = None
    local_model_url: str | None = None
    # 任务到达时冻结的原任务身份（不可变值）；无效身份不得用于结果回传
    task_identity: "AutoTaskIdentity | None" = None
    # 系统自动续接（resume_prompt）send accepted 后累计保存的全部 user message id：
    # 任务可能经历多次中断→续接，历史必须跨恢复周期保留（整个 AutoTask 生命周期有效），
    # 最终结果解析据此把"这些后续 user 都是自动续接，不是用户手动插的新任务"。
    resume_message_ids: set[str] = field(default_factory=set)
    error: str | None = None
    # 首次 accepted 后冻结的原执行 session 目标
    session_id: str | None = None
    directory: str | None = None
    # watchdog 中断恢复周期
    recover_baseline: str | None = None
    recover_started_ms: int | None = None
    resume_attempted: bool = False


@dataclass(frozen=True, slots=True)
class AutoTaskIdentity:
    """冻结的原任务身份快照（不可变值，事件里按值传递，不是可变 AutoTask 引用）。

    valid=False 时 task_id 为空、轮次为 0，error 带简短原因；绝不猜原 TASK_ID。
    """

    task_id: str
    round_number: int
    max_rounds: int
    valid: bool
    error: str | None = None


def _invalid_identity(reason: str) -> AutoTaskIdentity:
    return AutoTaskIdentity("", 0, 0, False, reason)


def parse_auto_task_identity(payload: str) -> AutoTaskIdentity:
    """从 legacy 任务 payload（extract_envelope 内层）解析一次并冻结原任务身份。

    头区 = 第一个 "CONTENT:" 行之前的行（key 大写归一）；CONTENT 之后的同名行
    属正文，不参与识别。分行按旧版 legacy 的 splitlines 语义（解析副本，覆盖
    LF/CRLF/CR/\\v/\\f/\\x1c-\\x1e/\\u0085/\\u2028/\\u2029），不改原 payload。
    缺 TASK_ID/SOURCE/TARGET/TYPE/CONTENT、头区重复 key、无效 TYPE、空正文或
    非法轮次（ROUND 缺省 0、MAX_ROUNDS 缺省 3、须 0<=ROUND<=MAX_ROUNDS 且
    MAX_ROUNDS>=1）均判身份无效并给原因；永不抛异常、永不静默填补原 TASK_ID。
    """
    if not isinstance(payload, str):
        return _invalid_identity("payload 非字符串")
    fields: dict[str, str] = {}
    body_lines: list[str] | None = None
    for line in payload.strip().splitlines():
        if not fields and body_lines is None and line.strip() == "AI_RELAY/1":
            continue
        if body_lines is not None:
            body_lines.append(line)
            continue
        key, colon, value = line.partition(":")
        k = key.strip().upper()
        if not (colon and k):
            return _invalid_identity(f"无法解析的头部：{line}")
        if k == "CONTENT":
            body_lines = [value] if value.strip() else []
        elif k in fields:
            return _invalid_identity(f"头部重复：{k}")
        else:
            fields[k] = value.strip()
    if body_lines is None:
        return _invalid_identity("缺少头部：CONTENT")
    missing = [k for k in _LEGACY_REQUIRED_KEYS if not fields.get(k)]
    if missing:
        return _invalid_identity("缺少头部：" + ", ".join(missing))
    if fields["TYPE"].upper() not in _LEGACY_KNOWN_TYPES:
        return _invalid_identity(f"无效消息类型：{fields['TYPE']}")
    if not "\n".join(body_lines).strip():
        return _invalid_identity("任务正文为空")
    try:
        round_number = int(fields.get("ROUND", "0"))
        max_rounds = int(fields.get("MAX_ROUNDS", "3"))
    except ValueError:
        return _invalid_identity("ROUND/MAX_ROUNDS 必须为整数")
    if round_number < 0 or max_rounds < 1 or round_number > max_rounds:
        return _invalid_identity("非法 ROUND/MAX_ROUNDS")
    return AutoTaskIdentity(fields["TASK_ID"], round_number, max_rounds, True, None)


def wrap_auto_response(
    body: str,
    task_id: str,
    round_number: int,
    max_rounds: int,
    now: datetime | None = None,
) -> str | None:
    """纯封装：按旧版 wrap_response 的 LEGACY_WEB 分支逐行生成 legacy 回包。

    BEGIN / SOURCE: EXECUTOR / TARGET: CHATGPT / TYPE: RESPONSE / TASK_ID: 原id /
    ROUND / MAX_ROUNDS / TIME(本地 %Y-%m-%d %H:%M:%S) / CONTENT: / 原结果正文 / END。
    正文只查非空，不 strip、不统一 CRLF、不 extract 内层包。
    约定范围外的输入（非字符串正文/id、bool 或非法轮次、含行分隔符的 id）
    → None 安全失败，绝不抛异常、不输出 True/False 轮次或可拆出额外头行的 id。
    """
    if not isinstance(body, str) or not body.strip():
        return None
    if not isinstance(task_id, str) or not task_id.strip():
        return None
    if any(ch in task_id for ch in _LINE_BREAK_CHARS):
        return None
    if isinstance(round_number, bool) or isinstance(max_rounds, bool):
        return None
    if not isinstance(round_number, int) or not isinstance(max_rounds, int):
        return None
    if round_number < 0 or max_rounds < 1 or round_number > max_rounds:
        return None
    try:
        t = (now if now is not None else datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
        return "\n".join(
            (
                AI_RELAY_BEGIN_MARKER,
                "SOURCE: EXECUTOR",
                "TARGET: CHATGPT",
                "TYPE: RESPONSE",
                f"TASK_ID: {task_id}",
                f"ROUND: {round_number}",
                f"MAX_ROUNDS: {max_rounds}",
                f"TIME: {t}",
                "CONTENT:",
                body,
                AI_RELAY_END_MARKER,
            )
        )
    except Exception:  # 收敛封装异常：安全失败，调用方零 deliver/ack
        return None


def is_usable_identity(identity: object) -> bool:
    """GUI 封装前的严格身份校验：类型/valid 真布尔/task_id 字符串/轮次非 bool 整数。

    bool 不当协议整数；valid 必须实际是布尔 True；不猜/不强转任何字段。
    """
    return (
        isinstance(identity, AutoTaskIdentity)
        and identity.valid is True
        and isinstance(identity.task_id, str)
        and bool(identity.task_id.strip())
        and not isinstance(identity.round_number, bool)
        and not isinstance(identity.max_rounds, bool)
        and isinstance(identity.round_number, int)
        and isinstance(identity.max_rounds, int)
        and identity.max_rounds >= 1
        and identity.round_number <= identity.max_rounds
    )


@dataclass(frozen=True, slots=True)
class AutoTaskIntake:
    """receive_auto_task 的稳定返回：accepted=是否接收(非 busy)，submitted=是否已提交(→RUNNING)。"""

    accepted: bool
    submitted: bool
    superseded_event_id: str | None = None


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
        model_base_url: str | None = None,
    ) -> None:
        self._client = client if client is not None else OpenChamberClient(base_url)
        self._model_base_url = model_base_url if model_base_url is not None else load_model_base_url()
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
        """A端 RemoteTask 入口：到达即解析冻结原任务身份 → 立即包装固化 → READY_TO_SEND → 单次尝试发送。

        包装与模型/OpenChamber/A端连接是否在线完全无关：到达即固化 wrapped_text。
        新 event 可接管提交身份不确定的旧轮；旧轮仍标 RUNNING 但服务已空闲、
        且没有可回传结果时，也允许新轮接管。正在执行或有结果待回传时
        不替换，以免把旧轮的回复误认为新轮。
        """
        superseded_event_id = None
        if self._auto_task is not None and self._auto_task.state != AUTO_IDLE:
            previous = self._auto_task
            if previous.event_id == event_id:
                return AutoTaskIntake(False, False)
            if previous.state == AUTO_RUNNING:
                status = self._client.get_session_status(previous.session_id)
                result = self.inspect_auto_task_result()
                if (not status.ok or status.status != "idle" or not result.read_ok
                        or result.complete or result.ambiguous or result.interrupted):
                    return AutoTaskIntake(False, False)
            elif previous.state != AUTO_SUBMIT_UNKNOWN:
                return AutoTaskIntake(False, False)
            superseded_event_id = previous.event_id
        self._auto_task = AutoTask(
            event_id=event_id,
            wrapped_text=self.wrap_auto_content(raw_text, wrapper_template),
            state=AUTO_READY,
            task_identity=parse_auto_task_identity(raw_text),
        )
        self.try_send_pending_auto_task()
        return AutoTaskIntake(True, self._auto_task.state == AUTO_RUNNING, superseded_event_id)

    def identity_snapshot(self, event_id: str) -> "AutoTaskIdentity | None":
        """只读身份快照：event_id 匹配当前自动任务 → 返回其冻结身份（不可变值）。

        无当前任务 / event_id 不匹配 / 无身份 → None。GUI/monitor 凭此判回传，
        绝不事后解析结果正文、模板或当前会话来猜原 TASK_ID。
        """
        task = self._auto_task
        if task is None or task.event_id != event_id or task.task_identity is None:
            return None
        return task.task_identity

    def try_send_pending_auto_task(self) -> None:
        """仅处理 READY_TO_SEND：先探模型可用，再对已保存的 wrapped_text 发一次。

        模型不可用 → 保持 READY_TO_SEND（由 L05-02 watchdog 恢复时再调）。
        RUNNING 再调 → 0 次 POST（原任务不重发）。
        """
        task = self._auto_task
        if task is None or task.state != AUTO_READY:
            return
        session = self._current_session()
        if not session.valid:
            return
        target = self._selected_model_target(session)
        if hasattr(self._client, "selected_model_target"):
            if target is None:
                return
            if target.local and (
                not target.base_url or not probe_model_endpoint(target.base_url).connected
                or not self._client.probe().connected
            ):
                return
        else:
            conn = self.test_model_connection()
            if not (conn.connected and conn.ready):
                return
        result = self._client.send_text(session.session_id, session.directory, task.wrapped_text)
        if target is not None:
            task.local_model = target.local
            task.local_model_url = target.base_url if target.local else None
        if result.accepted:
            task.state = AUTO_RUNNING
            task.message_id = result.message_id
            # 冻结原执行 session：一旦 accepted，恢复目标不再随 UI 当前会话变化
            task.session_id = session.session_id
            task.directory = session.directory
            task.error = None
        else:
            task.error = result.error
            if isinstance(result.error, str) and result.error.startswith("uncertain:"):
                task.state = AUTO_SUBMIT_UNKNOWN
                task.session_id = session.session_id
                task.directory = session.directory
                task.uncertain_baseline_user_id = result.baseline_user_id
                task.can_reconcile_submission = result.can_reconcile

    def inspect_auto_task_result(self) -> TaskResultResult:
        """只读：识别当前自动任务是否已产生可回传的最终结果。

        本轮只识别，不负责交付/清理（绝不把 AutoTask 清回 IDLE）。
        没有自动任务，或任务还没真正发出（无冻结 message_id）→ complete=False。
        有冻结目标时，用冻结的 session_id/directory/message_id 调
        client.get_task_result（不读 UI 当前激活会话）；allowed follow-up 加入
        全部 resume_message_ids，避免把系统自动续接（可能有多次）误判为用户手动新任务。
        """
        task = self._auto_task
        if task is None or task.message_id is None:
            return TaskResultResult(True, False, None, None, False, False, None)
        allowed = task.resume_message_ids if task.resume_message_ids else None
        return self._client.get_task_result(
            task.session_id, task.directory, task.message_id, allowed
        )

    def finish_auto_task(self, event_id: str) -> bool:
        """释放当前自动任务。只能在"最终结果已交给 ClipLinkBridge"之后调用：
        无论 Bridge 是直接回传还是因 A 离线把结果存成 pending_result，都算已接管结果。

        当前无任务 → False；event_id 不匹配当前任务 → False（绝不清错任务）；
        匹配 → 清 _auto_task → True。
        """
        task = self._auto_task
        if task is None:
            return False
        if task.event_id != event_id:
            return False
        self._auto_task = None
        return True

    def compact_auto_task(self, event_id: str) -> CompactResult:
        """对已完成自动任务冻结的 session 做一次 compact（须由结果已交给 Bridge 后调用）。

        无任务 / event_id 不匹配 / 任务尚未冻结 session_id → 失败且不调用 compact_session。
        合法 → 直接用 AutoTask 冻结的 session_id/directory 调 client.compact_session，
        绝不读 UI 当前激活会话。不 clear AutoTask、不变 state、不改 message_id/resume 历史；
        任务释放仍由 finish_auto_task 单独负责。
        """
        task = self._auto_task
        if task is None:
            return CompactResult(False, "无活动自动任务")
        if task.event_id != event_id:
            return CompactResult(False, "event_id 不匹配当前自动任务")
        if task.session_id is None:
            return CompactResult(False, "自动任务尚未冻结执行会话")
        return self._client.compact_session(task.session_id, task.directory)

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

        elif task.state == AUTO_SUBMIT_UNKNOWN and task.can_reconcile_submission:
            message_id = self._client.confirm_uncertain_submission(
                task.session_id, task.directory, task.wrapped_text, task.uncertain_baseline_user_id
            )
            if message_id:
                task.message_id = message_id
                task.state = AUTO_RUNNING
                task.error = None
                task.can_reconcile_submission = False
                action = "submit_confirmed"

        elif task.state == AUTO_RUNNING:
            # 只 probe；OpenChamber 正常（哪怕 session status=idle）保持 RUNNING。
            # idle 很可能是正常完成，最终结果识别交给 L05-03，绝不因此发 resume。
            if task.local_model is not False and not self._task_model_connected(task):
                task.state = AUTO_MODEL_OFFLINE
                task.resume_attempted = False
                task.recover_baseline = None
                task.recover_started_ms = None
                action = "offline"

        elif task.state == AUTO_MODEL_OFFLINE:
            if self._task_model_connected(task):
                prog = self._client.get_task_progress(task.session_id, task.directory, task.message_id)
                if prog.read_ok:
                    task.recover_baseline = prog.marker
                    task.recover_started_ms = now_ms
                    task.resume_attempted = False
                    task.state = AUTO_RECOVER_CHECK
                    action = "recover_check"
                # 原 session/messages 暂时读不到 → 不发 resume，保持 MODEL_OFFLINE 等下一 tick

        elif task.state == AUTO_RECOVER_CHECK:
            action = self._recover_check_tick(task, now_ms)

        elif task.state == AUTO_RESUME_SENT:
            if task.local_model is not False and not self._task_model_connected(task):
                task.state = AUTO_MODEL_OFFLINE
                task.resume_attempted = False
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

    def _task_model_connected(self, task: AutoTask) -> bool:
        if task.local_model_url and not probe_model_endpoint(task.local_model_url).connected:
            return False
        return self._client.probe().connected

    def auto_task_is_local(self) -> bool | None:
        return self._auto_task.local_model if self._auto_task is not None else None

    def _selected_model_target(self, session: CurrentSession) -> ModelTarget | None:
        resolver = getattr(self._client, "selected_model_target", None)
        return resolver(session.session_id, session.directory, self._model_base_url) if resolver is not None else None

    def get_current_model_target(self) -> ModelTarget | None:
        session = self._current_session()
        return self._selected_model_target(session) if session.valid else None

    def begin_interrupted_recovery(self, now_ms: int | None = None) -> bool:
        """interrupted 结果（服务在线但本次执行已 settled/中断、无安全最终答案）→
        用当前任务已冻结的 session_id/directory/message_id 读一次 progress 建立恢复基线。

        只允许 RUNNING / RESUME_SENT 进入；成功 → 进入 RECOVER_CHECK（复用 watchdog
        "观察 5s → 自行恢复不发 / 无进展才 resume 一次"），并保留全部 resume_message_ids。
        读取失败 → 返回 False、不发 resume、不把读取失败当成"无进展"，由后台下一轮再试。
        """
        task = self._auto_task
        if task is None or task.state not in (AUTO_RUNNING, AUTO_RESUME_SENT):
            return False
        if task.message_id is None:
            return False
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        prog = self._client.get_task_progress(task.session_id, task.directory, task.message_id)
        if not prog.read_ok:
            return False
        task.recover_baseline = prog.marker
        task.recover_started_ms = now_ms
        task.resume_attempted = False
        task.state = AUTO_RECOVER_CHECK
        return True

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
                    task.resume_message_ids.add(result.message_id)
                return "resume_sent"
            task.error = result.error
        return "none"

    def _current_session(self) -> CurrentSession:
        session = self._read_active_session()
        if session is None:
            return CurrentSession(None, None, None, False, "当前激活会话不可用")
        if self._client.validate_session(session.session_id, session.directory):
            directory = session.directory
            if directory is None:
                resolver = getattr(self._client, "resolve_session_directory", None)
                if resolver is not None:
                    directory = resolver(session.session_id)
            return CurrentSession(
                session.session_id, directory, session.source, True, None
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

    def auto_task_active(self) -> bool:
        """只读：是否有进行中的自动任务（state != IDLE）。

        仅供任务状态诊断；人工包装入口不依赖此状态，不修改任何状态。
        """
        return self._auto_task is not None and self._auto_task.state != AUTO_IDLE

    def auto_task_submit_unknown(self) -> bool:
        return self._auto_task is not None and self._auto_task.state == AUTO_SUBMIT_UNKNOWN

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
        session = self._current_session()
        target = self._selected_model_target(session) if session.valid else None
        if target is not None and not target.local:
            return ModelConnectionResult(True, None, True, None)
        probe = self._client.probe()
        if not probe.connected:
            return ModelConnectionResult(False, probe.latency_ms, False, probe.error)
        if not session.valid:
            return ModelConnectionResult(True, probe.latency_ms, False, None)
        if target is not None and target.local:
            local_probe = probe_model_endpoint(target.base_url) if target.base_url else None
            return ModelConnectionResult(
                True, probe.latency_ms, bool(local_probe and local_probe.connected),
                local_probe.error if local_probe else "unavailable: 本地模型地址不可用",
            )
        config = self._client.resolve_execution_config(session.session_id, session.directory)
        return ModelConnectionResult(True, probe.latency_ms, config is not None, None)
