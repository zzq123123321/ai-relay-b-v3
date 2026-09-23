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
from controller import LiteController
from openchamber_client import CompactResult, ExecutionConfig, ProbeResult, SendResult


class FakeClient:
    def __init__(
        self,
        validate=True,
        send=None,
        compact=None,
        probe_connected=True,
        latency_ms=1,
        config=None,
    ) -> None:
        self.validate_result = validate
        self.send_result = send if send is not None else SendResult(True, None, "msg_1")
        self.compact_result = compact if compact is not None else CompactResult(True, None)
        self.probe_connected = probe_connected
        self.latency_ms = latency_ms
        self.config = config  # ExecutionConfig 或 None
        self.validated = []
        self.sent = []
        self.compacted = []
        self.resolved = []

    def validate_session(self, session_id, directory=None):
        self.validated.append((session_id, directory))
        return self.validate_result

    def probe(self):
        return ProbeResult(self.probe_connected, self.latency_ms, None if self.probe_connected else "timeout")

    def resolve_execution_config(self, session_id, directory=None):
        self.resolved.append((session_id, directory))
        return self.config

    def send_text(self, session_id, directory, text):
        self.sent.append((session_id, directory, text))
        return self.send_result

    def compact_session(self, session_id, directory):
        self.compacted.append((session_id, directory))
        return self.compact_result


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