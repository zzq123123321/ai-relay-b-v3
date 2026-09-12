"""T16-B3：HistoryCopyService 服务级测试（独立于 DeliveryService / DB）。

验收覆盖：exact result_id 复制 protocol_text（U05）、missing 不写剪贴板、
读/写失败语义、普通完整值逐字复制、空值 unavailable、UI-B15「未确认交付」文案。
此处不触碰真实 DB / Outbox；DB 只读隔离由 integration 级验证。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.history_copy import HistoryCopyService


@dataclass
class ResultDetail:
    result_id: str
    protocol_text: str
    final_body: str


class FakeResultReader:
    def __init__(self, results=None) -> None:
        self._results = dict(results or {})
        self.calls: list[str] = []
        self.fail = False

    def get_result(self, result_id: str):
        self.calls.append(result_id)
        if self.fail:
            raise RuntimeError("reader down")
        return self._results.get(result_id)


class FakeSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.writes: list[str] = []
        self.fail = fail

    def write_text(self, *, text: str) -> None:
        if self.fail:
            raise OSError("clipboard down")
        self.writes.append(text)


def _service(reader=None, sink=None):
    reader = reader if reader is not None else FakeResultReader()
    sink = sink if sink is not None else FakeSink()
    return HistoryCopyService(result_reader=reader, clipboard_sink=sink)


def test_01_exact_result_id_copies_protocol_text():
    reader = FakeResultReader(
        {"res-a-2": ResultDetail("res-a-2", "AI_RELAY/1\n\nPROTO-A2", "乙失败正文")}
    )
    sink = FakeSink()
    svc = _service(reader, sink)
    result = svc.copy_result("res-a-2")
    assert result.outcome == "success"
    assert result.result_id == "res-a-2"
    assert reader.calls == ["res-a-2"]
    assert sink.writes == ["AI_RELAY/1\n\nPROTO-A2"]
    assert sink.writes[0] != "乙失败正文"


def test_02_missing_no_clipboard_write():
    reader = FakeResultReader({})
    sink = FakeSink()
    svc = _service(reader, sink)
    result = svc.copy_result("res-gone")
    assert result.outcome == "missing"
    assert result.message == "此版本无法读取"
    assert sink.writes == []


def test_03_read_failure_is_failed_no_write():
    reader = FakeResultReader()
    reader.fail = True
    sink = FakeSink()
    svc = _service(reader, sink)
    result = svc.copy_result("res-a-2")
    assert result.outcome == "failed"
    assert result.message.startswith("读取失败")
    assert sink.writes == []


def test_04_clipboard_failure_is_failed():
    sink = FakeSink(fail=True)
    svc = _service(
        FakeResultReader({"res-a": ResultDetail("res-a", "P", "B")}), sink
    )
    result = svc.copy_result("res-a")
    assert result.outcome == "failed"
    assert result.message == "写入剪贴板失败"
    assert sink.writes == []
    result_value = svc.copy_value(kind="task_id", value="long-value")
    assert result_value.outcome == "failed"
    assert result_value.message == "写入剪贴板失败"


def test_05_copy_full_value_verbatim():
    long_value = "T" * 140
    svc = _service(FakeResultReader(), FakeSink())
    result = svc.copy_value(kind="task_id", value=long_value)
    assert result.outcome == "success"
    assert result.message == "已复制到剪贴板"
    assert svc._clipboard.writes == [long_value]


def test_06_empty_value_unavailable_no_write():
    svc = _service(FakeResultReader(), FakeSink())
    result = svc.copy_value(kind="task_id", value="")
    assert result.outcome == "unavailable"
    assert result.message == "内容不可用"
    assert svc._clipboard.writes == []


def test_07_success_message_never_delivery_claim():
    svc = _service(
        FakeResultReader({"res-a": ResultDetail("res-a", "P", "B")}), FakeSink()
    )
    result = svc.copy_result("res-a")
    assert result.message == "已写入剪贴板（未确认交付）"
    forbidden = ("A端已收到", "已发送", "已交付", "ACK成功")
    assert not any(word in result.message for word in forbidden)