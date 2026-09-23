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
from openchamber_client import CompactResult, SendResult


class FakeClient:
    def __init__(
        self,
        validate=True,
        send=None,
        compact=None,
    ) -> None:
        self.validate_result = validate
        self.send_result = send if send is not None else SendResult(True, None, "msg_1")
        self.compact_result = compact if compact is not None else CompactResult(True, None)
        self.validated = []
        self.sent = []
        self.compacted = []

    def validate_session(self, session_id, directory=None):
        self.validated.append((session_id, directory))
        return self.validate_result

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