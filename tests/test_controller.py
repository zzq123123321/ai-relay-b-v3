"""LiteController 测试（全内存假接口：不起 HTTP、不读真实 LevelDB）。

覆盖交付1 get_current_session + 交付2 manual_send + 交付3 compact_current_session：
   1.  reader=None → 当前会话 unavailable（不触发 validate）
   2.  reader 返回会话但 validate=false → unavailable
   3.  reader + validate=true → 返回正确 session_id/directory/source
   4.  manual_send 空/纯空白文本 → 不调用 client.send_text
   5.  manual_send 无有效会话 → 不发送
   6.  manual_send 有效 → text 完全原样传给 client
   7.  send accepted/error 原样映射
   8.  compact 无有效会话 → 不调用 compact
   9.  compact 有效 → 正确 session_id/directory
   10. compact success/error 原样映射
"""

from active_session_reader import ActiveSession
from controller import (
    AUTO_IDLE,
    AUTO_READY,
    AUTO_MODEL_OFFLINE,
    AUTO_RECOVER_CHECK,
    AUTO_RESUME_SENT,
    AUTO_RUNNING,
    RECOVER_OBSERVE_MS,
    RESUME_PROMPT,
    LiteController,
)
from openchamber_client import (
    CompactResult,
    ExecutionConfig,
    ProbeResult,
    SendResult,
    SessionStatusResult,
    TaskProgressResult,
    TaskResultResult,
)


class FakeClient:
    def __init__(
        self,
        validate=True,
        send=None,
        compact=None,
        probe_connected=True,
        latency_ms=1,
        config=None,
        status=None,
        progress=None,
        status_fn=None,
        progress_fn=None,
        result=None,
        result_fn=None,
        probe_fn=None,
        send_fn=None,
    ) -> None:
        self.validate_result = validate
        self.send_result = send if send is not None else SendResult(True, None, "msg_1")
        self.compact_result = compact if compact is not None else CompactResult(True, None)
        self.probe_connected = probe_connected
        self.latency_ms = latency_ms
        self.config = config  # ExecutionConfig 或 None
        self.status_result = status if status is not None else SessionStatusResult(True, "busy", None)
        self.progress_result = progress if progress is not None else TaskProgressResult(True, "marker0", True)
        self.status_fn = status_fn
        self.progress_fn = progress_fn
        self.result = result
        self.result_fn = result_fn
        self.probe_fn = probe_fn
        self.send_fn = send_fn
        self.validated = []
        self.sent = []
        self.compacted = []
        self.resolved = []
        self.status_calls = []
        self.progress_calls = []
        self.result_calls = []

    def validate_session(self, session_id, directory=None):
        self.validated.append((session_id, directory))
        return self.validate_result

    def probe(self):
        if self.probe_fn is not None:
            return self.probe_fn()
        return ProbeResult(self.probe_connected, self.latency_ms, None if self.probe_connected else "timeout")

    def resolve_execution_config(self, session_id, directory=None):
        self.resolved.append((session_id, directory))
        return self.config

    def send_text(self, session_id, directory, text):
        self.sent.append((session_id, directory, text))
        if self.send_fn is not None:
            return self.send_fn(session_id, directory, text)
        return self.send_result

    def compact_session(self, session_id, directory):
        self.compacted.append((session_id, directory))
        return self.compact_result

    def get_session_status(self, session_id):
        self.status_calls.append(session_id)
        if self.status_fn is not None:
            return self.status_fn(session_id)
        return self.status_result

    def get_task_progress(self, session_id, directory, user_message_id):
        self.progress_calls.append((session_id, directory, user_message_id))
        if self.progress_fn is not None:
            return self.progress_fn(session_id, directory, user_message_id)
        return self.progress_result

    def get_task_result(self, session_id, directory, message_id, allowed_followup_user_ids=None):
        self.result_calls.append((session_id, directory, message_id, allowed_followup_user_ids))
        if self.result_fn is not None:
            return self.result_fn(session_id, directory, message_id, allowed_followup_user_ids)
        if self.result is not None:
            return self.result
        return TaskResultResult(True, False, None, None, False, False, None)


SESSION = ActiveSession(
    session_id="ses_x", directory=r"C:\work", source="persisted-last-active"
)


def _ctrl(client, reader_session):
    return LiteController(client=client, active_session_reader=lambda: reader_session)


def test_reader_none_unavailable():
    client = FakeClient()
    result = _ctrl(client, None).get_current_session()
    assert result.valid is False
    assert result.session_id is None
    assert result.directory is None
    assert result.source is None
    assert "不可用" in (result.error or "")
    assert client.validated == []  # 未走 validate


def test_validate_false_unavailable():
    client = FakeClient(validate=False)
    result = _ctrl(client, SESSION).get_current_session()
    assert result.valid is False
    assert result.session_id == "ses_x"
    assert result.directory == r"C:\work"
    assert client.validated == [("ses_x", r"C:\work")]


def test_validate_true_returns_session():
    client = FakeClient(validate=True)
    result = _ctrl(client, SESSION).get_current_session()
    assert result.valid is True
    assert result.session_id == "ses_x"
    assert result.directory == r"C:\work"
    assert result.source == "persisted-last-active"
    assert result.error is None


def test_manual_send_empty_not_sent():
    client = FakeClient()
    ctrl = _ctrl(client, SESSION)
    assert ctrl.manual_send("").accepted is False
    assert ctrl.manual_send("   \n\t  ").accepted is False
    assert client.sent == []


def test_manual_send_no_valid_session_not_sent():
    client = FakeClient()
    ctrl = _ctrl(client, None)  # reader None
    result = ctrl.manual_send("hello")
    assert result.accepted is False
    assert client.sent == []
    assert client.validated == []

    client2 = FakeClient(validate=False)
    result2 = _ctrl(client2, SESSION).manual_send("hello")
    assert result2.accepted is False
    assert client2.sent == []


def test_manual_send_valid_text_preserved():
    client = FakeClient(validate=True, send=SendResult(True, None, "msg_9"))
    text = "带空白 和 换行\n\t制表"
    result = _ctrl(client, SESSION).manual_send(text)
    assert result.accepted is True
    assert result.message_id == "msg_9"
    assert client.sent == [("ses_x", r"C:\work", text)]  # 原样，不包装


def test_manual_send_maps_accepted():
    client = FakeClient(validate=True, send=SendResult(True, None, "m_ok"))
    assert _ctrl(client, SESSION).manual_send("x").message_id == "m_ok"


def test_manual_send_maps_error():
    client = FakeClient(validate=True, send=SendResult(False, "http: 500", None))
    result = _ctrl(client, SESSION).manual_send("x")
    assert result.accepted is False
    assert result.error == "http: 500"


def test_compact_no_valid_session_not_called():
    client = FakeClient()
    result = _ctrl(client, None).compact_current_session()
    assert result.success is False
    assert client.compacted == []


def test_compact_valid_correct_target():
    client = FakeClient(validate=True)
    result = _ctrl(client, SESSION).compact_current_session()
    assert client.compacted == [("ses_x", r"C:\work")]
    assert result.success is True


def test_compact_maps_success():
    client = FakeClient(validate=True, compact=CompactResult(True, None))
    assert _ctrl(client, SESSION).compact_current_session().success is True


def test_compact_maps_error():
    client = FakeClient(validate=True, compact=CompactResult(False, "http: 404"))
    result = _ctrl(client, SESSION).compact_current_session()
    assert result.success is False
    assert result.error == "http: 404"


# ============================ L05-01 自动任务链 =============================

CFG = ExecutionConfig(agent="build", provider_id="prov", model_id="mod", variant=None, source="assistant")


# --- 包装规则（纯函数） ---
def test_wrap_empty_returns_raw():
    assert LiteController.wrap_auto_content("  原始  ", "") == "  原始  "


def test_wrap_placeholder_replaces():
    assert LiteController.wrap_auto_content("A", "X{content}Y") == "XAY"


def test_wrap_multiple_placeholder_all_replaced():
    assert LiteController.wrap_auto_content("AB", "{content}-{content}") == "AB-AB"


def test_wrap_no_placeholder_appends_newline():
    assert LiteController.wrap_auto_content("R", "W") == "W\nR"


def test_wrap_preserves_raw_exactly():
    raw = "中文\n换行\t制表"
    assert LiteController.wrap_auto_content(raw, "前{content}后") == "前中文\n换行\t制表后"


# --- 模型离线：包装已固化，send_text=0 ---
def test_receive_model_offline_wraps_and_waits():
    client = FakeClient(probe_connected=False, config=CFG)
    ctrl = _ctrl(client, SESSION)
    intake = ctrl.receive_auto_task("e1", "RAW", "W{content}")
    assert intake.accepted is True
    assert intake.submitted is False
    assert client.sent == []  # 0 次 POST
    assert ctrl._auto_task.wrapped_text == "WRAW"
    assert ctrl._auto_task.state == "ready_to_send"


# --- 服务在线但无 session/config：READY，send=0 ---
def test_receive_online_no_config_waits():
    client = FakeClient(probe_connected=True, config=None)
    ctrl = _ctrl(client, SESSION)
    intake = ctrl.receive_auto_task("e1", "RAW", "")
    assert intake.accepted is True
    assert intake.submitted is False
    assert client.sent == []
    assert ctrl._auto_task.state == "ready_to_send"
    assert ctrl._auto_task.wrapped_text == "RAW"


# --- 模型 ready：发送 wrapped，accepted → RUNNING ---
def test_receive_model_ready_sends_wrapped():
    client = FakeClient(probe_connected=True, config=CFG, send=SendResult(True, None, "m_ok"))
    ctrl = _ctrl(client, SESSION)
    intake = ctrl.receive_auto_task("e1", "RAW", "W{content}")
    assert intake.accepted is True
    assert intake.submitted is True
    assert client.sent == [("ses_x", r"C:\work", "WRAW")]
    assert ctrl._auto_task.state == "running"
    assert ctrl._auto_task.message_id == "m_ok"
    assert ctrl._auto_task.error is None


# --- 模板事后被改：try_send 仍发旧的已保存 wrapped ---
def test_try_send_uses_saved_wrapped_not_current_template():
    client = FakeClient(probe_connected=False, config=CFG)
    ctrl = _ctrl(client, SESSION)
    ctrl.receive_auto_task("e1", "RAW", "OLD{content}")  # 离线 → wrapped="OLDRAW"
    assert client.sent == []
    client.probe_connected = True
    client.send_result = SendResult(True, None, "m2")
    ctrl.try_send_pending_auto_task()
    assert client.sent == [("ses_x", r"C:\work", "OLDRAW")]


# --- RUNNING 后再 try_send：0 次新增 POST ---
def test_running_resend_no_extra_post():
    client = FakeClient(probe_connected=True, config=CFG, send=SendResult(True, None, "m1"))
    ctrl = _ctrl(client, SESSION)
    ctrl.receive_auto_task("e1", "RAW", "{content}")  # → RUNNING
    assert ctrl._auto_task.state == "running"
    before = len(client.sent)
    ctrl.try_send_pending_auto_task()
    assert len(client.sent) == before


# --- 发送失败：保持 READY，error 保存 ---
def test_send_failure_stays_ready_with_error():
    client = FakeClient(probe_connected=True, config=CFG, send=SendResult(False, "http: 500", None))
    ctrl = _ctrl(client, SESSION)
    intake = ctrl.receive_auto_task("e1", "RAW", "{content}")
    assert intake.accepted is True
    assert intake.submitted is False
    assert ctrl._auto_task.state == "ready_to_send"
    assert ctrl._auto_task.error == "http: 500"
    assert client.sent == [("ses_x", r"C:\work", "RAW")]


# --- READY 时来新任务：busy，原任务不覆盖 ---
def test_new_task_while_ready_is_busy():
    client = FakeClient(probe_connected=False, config=CFG)
    ctrl = _ctrl(client, SESSION)
    ctrl.receive_auto_task("e1", "RAW1", "{content}")
    assert ctrl._auto_task.state == "ready_to_send"
    intake2 = ctrl.receive_auto_task("e2", "RAW2", "{content}")
    assert intake2.accepted is False
    assert ctrl._auto_task.event_id == "e1"
    assert ctrl._auto_task.wrapped_text == "RAW1"
    assert client.sent == []


# --- RUNNING 时来新任务：busy ---
def test_new_task_while_running_is_busy():
    client = FakeClient(probe_connected=True, config=CFG, send=SendResult(True, None, "m1"))
    ctrl = _ctrl(client, SESSION)
    ctrl.receive_auto_task("e1", "RAW1", "{content}")  # → RUNNING
    intake2 = ctrl.receive_auto_task("e2", "RAW2", "{content}")
    assert intake2.accepted is False
    assert ctrl._auto_task.event_id == "e1"
    assert ctrl._auto_task.wrapped_text == "RAW1"


# ============================ L05-02 模型 watchdog ==========================

ONLINE = dict(probe_connected=True, config=CFG)
IDLE_ST = SessionStatusResult(True, "idle", None)
BUSY_ST = SessionStatusResult(True, "busy", None)
PROG_BASE = TaskProgressResult(True, "base_marker", True)
PROBE_ON = ProbeResult(True, 1, None)
PROBE_OFF = ProbeResult(False, 1, "timeout")


def _make_running(client):
    ctrl = _ctrl(client, SESSION)
    ctrl.receive_auto_task("e1", "RAW", "{content}")  # probe ok + config + accepted → RUNNING
    assert ctrl._auto_task.state == AUTO_RUNNING
    return ctrl


def test_watchdog_no_task_is_idle():
    client = FakeClient()
    ctrl = _ctrl(client, SESSION)
    result = ctrl.watchdog_tick()
    assert result.previous_state == AUTO_IDLE
    assert result.state == AUTO_IDLE
    assert result.action == "none"


# 6. 首次 accepted 后冻结 session_id/directory/message_id
def test_frozen_session_target_after_accepted():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"))
    ctrl = _make_running(client)
    task = ctrl._auto_task
    assert task.session_id == "ses_x"
    assert task.directory == r"C:\work"
    assert task.message_id == "m1"


# 7. READY_TO_SEND 模型恢复 → 用已保存 wrapped_text → RUNNING
def test_watchdog_ready_recovers_saved_wrapped():
    client = FakeClient(probe_connected=False, config=CFG)
    ctrl = _ctrl(client, SESSION)
    ctrl.receive_auto_task("e1", "RAW", "W{content}")  # 离线 → READY, wrapped="WRAW"
    assert ctrl._auto_task.state == "ready_to_send"
    client.probe_connected = True  # 模型恢复
    result = ctrl.watchdog_tick()
    assert result.state == AUTO_RUNNING
    assert result.action == "sent"
    assert client.sent == [("ses_x", r"C:\work", "WRAW")]


# 8. RUNNING probe 失败 → MODEL_OFFLINE
def test_watchdog_running_offline():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"))
    ctrl = _make_running(client)
    client.probe_connected = False
    result = ctrl.watchdog_tick()
    assert result.previous_state == AUTO_RUNNING
    assert result.state == AUTO_MODEL_OFFLINE
    assert result.action == "offline"


# 9. MODEL_OFFLINE 仍失败 → 不发任何内容
def test_watchdog_model_offline_still_offline():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"))
    ctrl = _make_running(client)
    ctrl._auto_task.state = AUTO_MODEL_OFFLINE
    client.probe_connected = False
    result = ctrl.watchdog_tick()
    assert result.state == AUTO_MODEL_OFFLINE
    assert result.action == "none"
    assert len(client.sent) == 1  # 仅首次 wrapped，无新发送


# 10. MODEL_OFFLINE 恢复 → RECOVER_CHECK → 记录 baseline
def test_watchdog_model_offline_to_recover_check():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"), status=IDLE_ST, progress=PROG_BASE)
    ctrl = _make_running(client)
    ctrl._auto_task.state = AUTO_MODEL_OFFLINE
    client.probe_connected = True
    result = ctrl.watchdog_tick(now_ms=1000)
    assert result.state == AUTO_RECOVER_CHECK
    assert result.action == "recover_check"
    assert ctrl._auto_task.recover_baseline == "base_marker"
    assert ctrl._auto_task.recover_started_ms == 1000
    assert ctrl._auto_task.resume_attempted is False
    assert len(client.sent) == 1  # 无 resume


# 11. RECOVER_CHECK status=busy → RUNNING，resume 0
def test_recover_check_busy_auto_running():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"), status=BUSY_ST, progress=PROG_BASE)
    ctrl = _make_running(client)
    task = ctrl._auto_task
    task.state = AUTO_RECOVER_CHECK
    task.recover_baseline = "base_marker"
    task.recover_started_ms = 0
    result = ctrl.watchdog_tick(now_ms=99999)
    assert result.state == AUTO_RUNNING
    assert result.action == "resumed_automatically"
    assert len(client.sent) == 1


# 12. RECOVER_CHECK marker 变化 → RUNNING，resume 0
def test_recover_check_marker_change_auto_running():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"), status=IDLE_ST,
                        progress=TaskProgressResult(True, "NEW_marker", True))
    ctrl = _make_running(client)
    task = ctrl._auto_task
    task.state = AUTO_RECOVER_CHECK
    task.recover_baseline = "OLD_marker"  # 与当前 marker 不同
    task.recover_started_ms = 0
    result = ctrl.watchdog_tick(now_ms=99999)
    assert result.state == AUTO_RUNNING
    assert result.action == "resumed_automatically"
    assert len(client.sent) == 1


# 13. RECOVER_CHECK 未满 5 秒 → resume 0
def test_recover_check_under_window_no_resume():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"), status=IDLE_ST, progress=PROG_BASE)
    ctrl = _make_running(client)
    task = ctrl._auto_task
    task.state = AUTO_RECOVER_CHECK
    task.recover_baseline = "base_marker"  # 无变化
    task.recover_started_ms = 9000
    result = ctrl.watchdog_tick(now_ms=9500)  # 仅 500ms
    assert result.state == AUTO_RECOVER_CHECK
    assert result.action == "none"
    assert len(client.sent) == 1


# 14. progress read 失败 → 即使超过 5 秒也不发 resume
def test_recover_check_progress_read_failure_no_resume():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"), status=IDLE_ST,
                        progress=TaskProgressResult(False, None, False, "connection: refused"))
    ctrl = _make_running(client)
    task = ctrl._auto_task
    task.state = AUTO_RECOVER_CHECK
    task.recover_baseline = "base_marker"
    task.recover_started_ms = 0
    result = ctrl.watchdog_tick(now_ms=99999)  # 远超 5s
    assert result.state == AUTO_RECOVER_CHECK
    assert result.action == "none"
    assert len(client.sent) == 1


# 15. 满 5 秒且确认无进度 → 固定 resume_prompt 发送一次 → RESUME_SENT
def test_recover_check_full_window_sends_resume_once():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m_resume"), status=IDLE_ST, progress=PROG_BASE)
    ctrl = _make_running(client)
    task = ctrl._auto_task
    task.state = AUTO_RECOVER_CHECK
    task.recover_baseline = "base_marker"
    task.recover_started_ms = 0
    result = ctrl.watchdog_tick(now_ms=RECOVER_OBSERVE_MS)  # 恰好满
    assert result.state == AUTO_RESUME_SENT
    assert result.action == "resume_sent"
    assert client.sent[-1] == ("ses_x", r"C:\work", RESUME_PROMPT)
    assert task.resume_attempted is True


# 16. resume 用 frozen session（active reader 已切换仍发旧 session）
def test_resume_uses_frozen_session():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"), status=IDLE_ST, progress=PROG_BASE)
    ctrl = _ctrl(client, SESSION)
    ctrl.receive_auto_task("e1", "RAW", "{content}")  # RUNNING on ses_x
    other = ActiveSession(session_id="ses_other", directory=r"D:\other", source="x")
    ctrl._read_active_session = lambda: other  # UI 已切到别的会话
    task = ctrl._auto_task
    task.state = AUTO_RECOVER_CHECK
    task.recover_baseline = "base_marker"
    task.recover_started_ms = 0
    ctrl.watchdog_tick(now_ms=RECOVER_OBSERVE_MS)
    assert client.sent[-1] == ("ses_x", r"C:\work", RESUME_PROMPT)  # 仍是原 session


# 17. RESUME_SENT 下一 tick → 不重复 resume
def test_resume_sent_next_tick_no_repeat():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"), status=IDLE_ST, progress=PROG_BASE)
    ctrl = _make_running(client)
    task = ctrl._auto_task
    task.state = AUTO_RESUME_SENT
    task.resume_attempted = True
    task.recover_baseline = "base_marker"
    n = len(client.sent)
    result = ctrl.watchdog_tick(now_ms=RECOVER_OBSERVE_MS + 5000)
    assert result.state == AUTO_RESUME_SENT
    assert result.action == "none"
    assert len(client.sent) == n


# 18. RESUME_SENT 观察到进度 → RUNNING
def test_resume_sent_observes_progress():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"), status=BUSY_ST,
                        progress=TaskProgressResult(True, "NEW", True))
    ctrl = _make_running(client)
    task = ctrl._auto_task
    task.state = AUTO_RESUME_SENT
    task.recover_baseline = "OLD"
    result = ctrl.watchdog_tick(now_ms=RECOVER_OBSERVE_MS + 5000)
    assert result.state == AUTO_RUNNING
    assert result.action == "resumed_automatically"


# 19. RUNNING 正常看到 idle → 仍 RUNNING，不误发 resume
def test_running_idle_stays_running():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"), status=IDLE_ST, progress=PROG_BASE)
    ctrl = _make_running(client)
    n = len(client.sent)
    result = ctrl.watchdog_tick(now_ms=RECOVER_OBSERVE_MS + 99999)
    assert result.state == AUTO_RUNNING
    assert result.action == "none"
    assert len(client.sent) == n


# 20. 整个 watchdog 不读取/依赖 A端 ClipLink 状态
def test_watchdog_independent_of_a_connection():
    import controller as mod

    # 模块不 import 任何 cliplink 符号；controller 实例不持有 cliplink 引用
    assert not any(name.startswith("cliplink") for name in vars(mod))
    ctrl = LiteController(client=FakeClient(), active_session_reader=lambda: SESSION)
    assert not any(attr.startswith("cliplink") for attr in vars(ctrl))


# ===================== L05-02-FIX1 恢复三态 busy 保护 =====================

def _snapshot(task):
    return (
        task.event_id,
        task.wrapped_text,
        task.state,
        task.message_id,
        task.error,
        task.session_id,
        task.directory,
        task.recover_baseline,
        task.recover_started_ms,
        task.resume_attempted,
    )


# 1-4. MODEL_OFFLINE / RECOVER_CHECK / RESUME_SENT 收新任务 → busy 且旧任务完全不变
def test_recovering_states_busy_preserve_old_task():
    for state, attempted in (
        (AUTO_MODEL_OFFLINE, False),
        (AUTO_RECOVER_CHECK, False),
        (AUTO_RESUME_SENT, True),
    ):
        client = FakeClient(
            **ONLINE, send=SendResult(True, None, "m1"),
            status=IDLE_ST, progress=PROG_BASE,
        )
        ctrl = _make_running(client)
        task = ctrl._auto_task
        task.state = state
        task.recover_baseline = "base_marker"
        task.recover_started_ms = 12345
        task.resume_attempted = attempted
        before = _snapshot(task)
        n_sent = len(client.sent)
        intake = ctrl.receive_auto_task("e_new", "OTHER", "{content}")
        # busy
        assert intake.accepted is False
        assert intake.submitted is False
        # 旧任务对象未被替换
        assert ctrl._auto_task is task
        # 全部字段保持（含 frozen session/message/recovery baseline）
        assert _snapshot(task) == before
        assert task.event_id == "e1"  # A端新 event_id 未替换
        assert task.session_id == "ses_x"
        assert task.directory == r"C:\work"
        assert task.message_id == "m1"
        # 无额外 send_text
        assert len(client.sent) == n_sent


# 5. IDLE（无任务）仍允许正常接收新任务
def test_idle_still_accepts_new_task():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"))
    ctrl = _ctrl(client, SESSION)
    assert ctrl._auto_task is None  # 无任务 → 等价 IDLE
    intake = ctrl.receive_auto_task("e1", "RAW", "{content}")
    assert intake.accepted is True
    assert intake.submitted is True  # 在线 → 直接 RUNNING
    assert client.sent == [("ses_x", r"C:\work", "RAW")]


# ===================== L05-03 交付3：结果检查入口 =====================

# 19. resume accepted 后 Controller 保存 resume_message_ids（累计）
def test_resume_accepted_saves_resume_message_id():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m_resume"), status=IDLE_ST, progress=PROG_BASE)
    ctrl = _make_running(client)
    task = ctrl._auto_task
    assert task.resume_message_ids == set()  # 首次发送后尚未续接
    task.state = AUTO_RECOVER_CHECK
    task.recover_baseline = PROG_BASE.marker  # 无进展
    task.recover_started_ms = 0
    result = ctrl.watchdog_tick(now_ms=RECOVER_OBSERVE_MS)
    assert result.action == "resume_sent"
    assert task.resume_message_ids == {"m_resume"}
    # resume 用冻结 session，发固定续接
    assert client.sent[-1] == ("ses_x", r"C:\work", RESUME_PROMPT)


# 20. inspect 使用 frozen session，即使 UI active session 已切换也不受影响
def test_inspect_result_uses_frozen_session():
    complete = TaskResultResult(True, True, "DONE", 120, False, False, None)
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"), result=complete)
    ctrl = _make_running(client)
    other = ActiveSession(session_id="ses_other", directory=r"D:\other", source="x")
    ctrl._read_active_session = lambda: other  # UI 已切到别的会话
    task = ctrl._auto_task
    task.resume_message_ids = {"m_resume"}  # 模拟已发过自动续接
    view = ctrl.inspect_auto_task_result()
    assert view is complete  # 透传 client 识别结果
    # 调用用的是冻结目标 + 允许的 resume_message_ids，而非 UI 当前会话
    assert client.result_calls == [("ses_x", r"C:\work", "m1", {"m_resume"})]


# 21. inspect complete 后 AutoTask 本轮仍保持原状态，不擅自清为 IDLE
def test_inspect_does_not_clear_task():
    complete = TaskResultResult(True, True, "DONE", 120, False, False, None)
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"), result=complete)
    ctrl = _make_running(client)
    task = ctrl._auto_task
    assert task.state == AUTO_RUNNING
    view = ctrl.inspect_auto_task_result()
    assert view.complete is True
    # 本轮只识别，不负责交付/清理：仍持有同一 AutoTask 且状态不变
    assert ctrl._auto_task is task
    assert task.state == AUTO_RUNNING
    assert task.message_id == "m1"
    assert task.session_id == "ses_x"
    assert task.directory == r"C:\work"


# 补充：无任务 或 还没真正发出（无冻结 message_id）→ complete=False 且不调 client
def test_inspect_no_task_or_not_sent():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"))
    ctrl = _ctrl(client, SESSION)
    view = ctrl.inspect_auto_task_result()
    assert view.complete is False
    assert client.result_calls == []  # 无任务 → 未调用 client

    # 任务仍停在 READY（send 失败，无 message_id）
    client2 = FakeClient(**ONLINE, send=SendResult(False, "offline", None))
    ctrl2 = _ctrl(client2, SESSION)
    ctrl2.receive_auto_task("e1", "RAW", "{content}")
    task2 = ctrl2._auto_task
    assert task2.state == AUTO_READY
    assert task2.message_id is None
    view2 = ctrl2.inspect_auto_task_result()
    assert view2.complete is False
    assert client2.result_calls == []  # 还没真正发出 → 不调 client


# ===================== L05-03-FIX1：多次中断的 resume 历史 =====================

# 2. 第一次 resume 后再次进入 MODEL_OFFLINE → U1 仍保留；7. 新恢复周期只重置
#    resume_attempted，不删除历史 IDs；3. 第二次 resume accepted → ids={U1,U2}
def test_multi_resume_history_preserved():
    sends = iter([SendResult(True, None, "U0"), SendResult(True, None, "U1"),
                  SendResult(True, None, "U2")])
    probe = {"on": True}
    client = FakeClient(**ONLINE, status=IDLE_ST, progress=PROG_BASE,
                        probe_fn=lambda: PROBE_ON if probe["on"] else PROBE_OFF,
                        send_fn=lambda s, d, t: next(sends))
    ctrl = _make_running(client)
    task = ctrl._auto_task
    assert task.message_id == "U0"
    assert task.resume_message_ids == set()

    # 第一次中断 → resume U1
    probe["on"] = False
    assert ctrl.watchdog_tick(now_ms=0).action == "offline"          # RUNNING→MODEL_OFFLINE
    probe["on"] = True
    assert ctrl.watchdog_tick(now_ms=0).action == "recover_check"    # →RECOVER_CHECK
    assert task.resume_attempted is False
    assert task.resume_message_ids == set()
    assert ctrl.watchdog_tick(now_ms=RECOVER_OBSERVE_MS).action == "resume_sent"  # →RESUME_SENT
    assert task.resume_message_ids == {"U1"}
    assert task.resume_attempted is True

    # 第二次中断：新恢复周期只重置 resume_attempted，绝不删除历史 U1
    probe["on"] = False
    assert ctrl.watchdog_tick(now_ms=0).action == "offline"          # RESUME_SENT→MODEL_OFFLINE
    assert task.resume_attempted is False
    assert task.resume_message_ids == {"U1"}
    probe["on"] = True
    assert ctrl.watchdog_tick(now_ms=0).action == "recover_check"    # →RECOVER_CHECK
    assert task.resume_attempted is False
    assert task.resume_message_ids == {"U1"}
    # 第二次 resume → 追加 U2（历史保留）
    assert ctrl.watchdog_tick(now_ms=RECOVER_OBSERVE_MS).action == "resume_sent"
    assert task.resume_message_ids == {"U1", "U2"}


# 4. inspect_auto_task_result() → allowed_followup_user_ids 同时包含全部 resume IDs
def test_inspect_allows_all_resume_ids():
    complete = TaskResultResult(True, True, "DONE", 120, False, False, None)
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"), result=complete)
    ctrl = _make_running(client)
    task = ctrl._auto_task
    task.resume_message_ids = {"U1", "U2", "U3"}  # 模拟经历多次中断续接
    ctrl.inspect_auto_task_result()
    assert client.result_calls == [("ses_x", r"C:\work", "m1", {"U1", "U2", "U3"})]


# ===================== L05-04：interrupted 恢复 + 安全释放任务 =====================

# 3. interrupted + progress 基线读取成功 → RECOVER_CHECK（保留冻结目标/message_id）
def test_interrupted_recovery_from_running():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"))
    ctrl = _make_running(client)
    task = ctrl._auto_task
    assert task.state == AUTO_RUNNING
    ok = ctrl.begin_interrupted_recovery(now_ms=1000)
    assert ok is True
    assert task.state == AUTO_RECOVER_CHECK
    assert task.recover_baseline == "marker0"  # 默认 progress marker
    assert task.recover_started_ms == 1000
    assert task.resume_attempted is False
    assert task.message_id == "m1"  # 冻结目标不变


# 4. interrupted baseline 读取失败 → 不发 resume、状态不变、返回 False
def test_interrupted_recovery_progress_read_failure():
    client = FakeClient(
        **ONLINE, send=SendResult(True, None, "m1"),
        progress=TaskProgressResult(False, "marker0", False),  # read_ok=False
    )
    ctrl = _make_running(client)
    task = ctrl._auto_task
    ok = ctrl.begin_interrupted_recovery(now_ms=1000)
    assert ok is False
    assert task.state == AUTO_RUNNING  # 未进入 RECOVER_CHECK
    assert task.recover_baseline is None
    assert len(client.sent) == 1  # 只有首次 wrapped，没发 resume


# 5. interrupted recovery 保留全部 resume_message_ids（历史不丢）
def test_interrupted_recovery_preserves_resume_ids():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"))
    ctrl = _make_running(client)
    task = ctrl._auto_task
    task.state = AUTO_RESUME_SENT
    task.resume_message_ids = {"U1", "U2"}  # 经历两次续接的历史
    ok = ctrl.begin_interrupted_recovery(now_ms=1000)
    assert ok is True
    assert task.state == AUTO_RECOVER_CHECK
    assert task.resume_message_ids == {"U1", "U2"}


# 非 RUNNING/RESUME_SENT 状态不允许进入（已在恢复中 / 无任务态）
def test_interrupted_recovery_rejects_other_states():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"))
    ctrl = _make_running(client)
    task = ctrl._auto_task
    task.state = AUTO_RECOVER_CHECK
    assert ctrl.begin_interrupted_recovery(now_ms=1000) is False
    task.state = AUTO_IDLE
    assert ctrl.begin_interrupted_recovery(now_ms=1000) is False


# 1. finish_auto_task 正确 event_id → 清任务
def test_finish_auto_task_correct_event_id():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"))
    ctrl = _make_running(client)
    assert ctrl._auto_task is not None
    assert ctrl.finish_auto_task("e1") is True
    assert ctrl._auto_task is None


# 2. finish_auto_task 错 event_id → 绝不清任务
def test_finish_auto_task_wrong_event_id_keeps_task():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"))
    ctrl = _make_running(client)
    task = ctrl._auto_task
    assert ctrl.finish_auto_task("other_event") is False
    assert ctrl._auto_task is task  # 未清


# 当前无任务 → False
def test_finish_auto_task_no_task():
    client = FakeClient()
    ctrl = _ctrl(client, SESSION)
    assert ctrl.finish_auto_task("e1") is False
    assert ctrl._auto_task is None


# ===================== L05-06 自动压缩冻结会话 =====================

# 01. 无任务 → failure，不调用 compact_session
def test_compact_auto_task_no_task():
    client = FakeClient()
    ctrl = _ctrl(client, SESSION)
    result = ctrl.compact_auto_task("e1")
    assert result.success is False
    assert client.compacted == []


# 02. event_id 不匹配 → failure，不调用 compact_session
def test_compact_auto_task_wrong_event_id():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"))
    ctrl = _make_running(client)
    result = ctrl.compact_auto_task("other_event")
    assert result.success is False
    assert client.compacted == []


# 03. 任务尚未冻结 session（仍 READY，发送失败）→ failure，不调用 compact_session
def test_compact_auto_task_no_frozen_session():
    client = FakeClient(**ONLINE, send=SendResult(False, "offline", None))
    ctrl = _ctrl(client, SESSION)
    ctrl.receive_auto_task("e1", "RAW", "{content}")
    assert ctrl._auto_task.session_id is None
    result = ctrl.compact_auto_task("e1")
    assert result.success is False
    assert client.compacted == []


# 04. frozen session 正确 → compact_session(冻结 session_id, 冻结 directory)
def test_compact_auto_task_uses_frozen_target():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"))
    ctrl = _make_running(client)  # 冻结 ses_x / C:\work
    result = ctrl.compact_auto_task("e1")
    assert result.success is True
    assert client.compacted == [("ses_x", r"C:\work")]


# 05. active reader 已切到别的会话 → 仍用冻结 session compact（不读当前 UI 会话）
def test_compact_auto_task_ignores_active_session_switch():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"))
    ctrl = _ctrl(client, SESSION)
    ctrl.receive_auto_task("e1", "RAW", "{content}")  # 冻结 ses_x
    other = ActiveSession(session_id="ses_other", directory=r"D:\other", source="x")
    ctrl._read_active_session = lambda: other  # UI 已切到别的会话
    ctrl.compact_auto_task("e1")
    assert client.compacted == [("ses_x", r"C:\work")]  # 仍是冻结目标


# 06. client compact success → 透传 success=True
def test_compact_auto_task_maps_success():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"), compact=CompactResult(True, None))
    ctrl = _make_running(client)
    assert ctrl.compact_auto_task("e1").success is True


# 07. client compact fail → 返回 failure，且 compact_auto_task 自身不清 AutoTask
def test_compact_auto_task_fail_keeps_task():
    client = FakeClient(**ONLINE, send=SendResult(True, None, "m1"),
                        compact=CompactResult(False, "http: 404"))
    ctrl = _make_running(client)
    task = ctrl._auto_task
    result = ctrl.compact_auto_task("e1")
    assert result.success is False
    assert result.error == "http: 404"
    # compact_auto_task 不 clear 任务：仍持有同一 AutoTask（释放由 finish_auto_task 负责）
    assert ctrl._auto_task is task
    assert task.session_id == "ses_x"
    assert task.message_id == "m1"
    assert task.state == AUTO_RUNNING