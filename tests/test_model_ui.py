"""阶段 2B UI 接入测试：本地模型地址 + probe 驱动的模型连接显示（全 fake，offscreen，无真实 HTTP）。

覆盖：启动地址一致性（probe worker 与业务客户端同一生效地址）、保存成功/无效/未变化/重启读取、
probe 连接/断开/恢复、旧任务事件不覆盖连接显示、GUI 轮询无 HTTP、阻塞探测时 UI 仍响应、退出清理。
配置全部重定向到 tmp LOCALAPPDATA，不碰真实 %LOCALAPPDATA%。
"""

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPlainTextEdit

from controller import LiteController
from main import apply_model_probe, process_monitor_events, start_model_poll, wire_ui
from model_probe import (
    DEFAULT_MODEL_BASE_URL,
    ModelProbeResult,
    ModelProbeWorker,
    load_model_base_url,
    save_model_base_url,
)
from openchamber_client import ProbeResult
from test_ui_wiring import FakeBridge, FakeController, FakeMonitor
from ui.main_window import MainWindow


class FakeProbeWorker:
    """ModelProbeWorker 替身：只实现 last_result（GUI 轮询唯一入口），没有 probe 方法。

    GUI 代码若误调 probe()/wait_for_result() 会 AttributeError 暴露问题（主线程 0 HTTP）。
    """

    def __init__(self, result=None) -> None:
        self._result = result
        self.last_calls = 0

    def last_result(self):
        self.last_calls += 1
        return self._result


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return tmp_path


def _label(w, name: str) -> str:
    return w.findChild(QLabel, name).text()


def _log(w) -> str:
    return w.findChild(QPlainTextEdit, "log_box").toPlainText()


def _wired(qapp, bridge=None, monitor=None):
    w = MainWindow()
    w.show()
    wire_ui(w, FakeController(), bridge if bridge is not None else FakeBridge(), monitor)
    return w


def test_probe_and_business_client_use_separate_addresses(qapp, appdata):
    """启动：探测模型接口，自动任务使用本机 OpenChamber API。"""
    base_url = load_model_base_url()
    assert base_url == DEFAULT_MODEL_BASE_URL
    window = MainWindow()
    window.set_model_base_url(base_url)
    worker = ModelProbeWorker(base_url, probe_fn=lambda: ProbeResult(True, 1, None))
    controller = LiteController()
    assert worker.base_url == base_url
    assert controller._client.base_url == "http://127.0.0.1:57123"
    assert window.current_model_base_url() == base_url
    assert _label(window, "llm_active") == f"生效地址：{base_url}"


def test_saved_custom_address_flows_only_to_model_worker(qapp, appdata):
    saved = save_model_base_url("http://127.0.0.1:60000/")
    assert saved == "http://127.0.0.1:60000"
    base_url = load_model_base_url()
    assert base_url == "http://127.0.0.1:60000"
    worker = ModelProbeWorker(base_url, probe_fn=lambda: ProbeResult(True, 1, None))
    controller = LiteController()
    assert worker.base_url == "http://127.0.0.1:60000"
    assert controller._client.base_url == "http://127.0.0.1:57123"


def test_corrupt_config_falls_back_and_still_consistent(qapp, appdata):
    (appdata / "AIRelayLite").mkdir(parents=True)
    (appdata / "AIRelayLite" / "settings.json").write_text("{corrupt", encoding="utf-8")
    base_url = load_model_base_url()
    assert base_url == DEFAULT_MODEL_BASE_URL
    worker = ModelProbeWorker(base_url, probe_fn=lambda: ProbeResult(True, 1, None))
    controller = LiteController()
    assert worker.base_url == DEFAULT_MODEL_BASE_URL
    assert controller._client.base_url == "http://127.0.0.1:57123"


def test_probe_connected_updates_card(qapp):
    w = MainWindow()
    w.show()
    worker = FakeProbeWorker(ModelProbeResult(True, 12, None, DEFAULT_MODEL_BASE_URL))
    apply_model_probe(w, worker)
    assert "已连接" in _label(w, "llm_status")
    assert "12 ms" in _label(w, "llm_oc")


def test_probe_disconnected_shows_dash_latency(qapp):
    w = MainWindow()
    w.show()
    worker = FakeProbeWorker(ModelProbeResult(False, None, "timeout", DEFAULT_MODEL_BASE_URL))
    apply_model_probe(w, worker)
    assert "未连接" in _label(w, "llm_status")
    assert "-- ms" in _label(w, "llm_oc")


def test_probe_disconnected_with_latency_still_shows_dash(qapp):
    """断开结果即使携带非空探测耗时（如 http: 503 的 37ms），也不得冒充“服务延迟”。"""
    w = MainWindow()
    w.show()
    worker = FakeProbeWorker(ModelProbeResult(False, 37, "http: 503", DEFAULT_MODEL_BASE_URL))
    apply_model_probe(w, worker)
    assert "未连接" in _label(w, "llm_status")
    assert _label(w, "llm_oc") == "服务延迟：-- ms"


def test_probe_no_result_keeps_initial_state(qapp):
    w = MainWindow()
    w.show()
    apply_model_probe(w, FakeProbeWorker(None))
    assert "未检测" in _label(w, "llm_status")
    assert "-- ms" in _label(w, "llm_oc")


def test_probe_reconnect_recovery_sequence(qapp):
    w = MainWindow()
    w.show()
    w.set_model_base_url(DEFAULT_MODEL_BASE_URL)
    apply_model_probe(w, FakeProbeWorker(ModelProbeResult(True, 9, None, DEFAULT_MODEL_BASE_URL)))
    assert "已连接" in _label(w, "llm_status")
    apply_model_probe(w, FakeProbeWorker(ModelProbeResult(False, None, "timeout", DEFAULT_MODEL_BASE_URL)))
    assert "未连接" in _label(w, "llm_status")
    assert "-- ms" in _label(w, "llm_oc")
    apply_model_probe(w, FakeProbeWorker(ModelProbeResult(True, 14, None, DEFAULT_MODEL_BASE_URL)))
    assert "已连接" in _label(w, "llm_status")
    assert "14 ms" in _label(w, "llm_oc")


def test_start_model_poll_first_frame_and_gui_no_http(qapp):
    w = MainWindow()
    w.show()
    worker = FakeProbeWorker(ModelProbeResult(True, 7, None, DEFAULT_MODEL_BASE_URL))
    timer = start_model_poll(w, worker, interval_ms=60_000)  # 长间隔，测试期间不重复触发
    assert timer.isActive()
    assert timer.parent() is w  # 计时器随窗口退出停止
    assert "已连接" in _label(w, "llm_status")  # 首帧立即读
    assert worker.last_calls >= 1
    assert not hasattr(worker, "probe")  # GUI 轮询只走 last_result，绝无 probe/同步 HTTP
    timer.stop()


def test_old_task_events_do_not_override_probe_display(qapp):
    """任务进展/首响应事件不得把 probe 的“已连接”显示覆盖成复杂状态。"""
    bridge = FakeBridge()
    mon = FakeMonitor()
    w = _wired(qapp, bridge, mon)
    worker = FakeProbeWorker(ModelProbeResult(True, 9, None, DEFAULT_MODEL_BASE_URL))
    apply_model_probe(w, worker)
    assert "已连接" in _label(w, "llm_status")

    mon.push({"type": "intake_running"})
    process_monitor_events(mon, w, bridge)
    mon.push({"type": "first_response", "first_response_ms": 400})
    process_monitor_events(mon, w, bridge)
    assert "已连接" in _label(w, "llm_status")  # 事件后仍为 probe 的“已连接”
    assert "9 ms" in _label(w, "llm_oc")  # 服务延迟不被旧事件改写


def test_probe_blocked_ui_still_responsive_and_exit_cleanup(qapp):
    """probe 阻塞在后台线程时：GUI 轮询/UI 操作仍快速返回；退出时 worker 有界停止。"""
    gate = threading.Event()
    entered = threading.Event()

    def probe():
        entered.set()
        gate.wait()
        return ProbeResult(True, 1, None)

    w = MainWindow()
    w.show()
    worker = ModelProbeWorker(DEFAULT_MODEL_BASE_URL, probe_fn=probe, interval_seconds=0.05)
    worker.start()
    timer = start_model_poll(w, worker, interval_ms=50)
    assert entered.wait(2.0), "后台线程未进入阻塞 probe"
    began = time.perf_counter()
    for _ in range(50):
        apply_model_probe(w, worker)  # 轮询只读 last_result，不被 probe 阻塞
    w.set_current_task("UI 仍然响应")
    w.append_log("still responsive")
    assert time.perf_counter() - began < 0.5

    gate.set()
    timer.stop()
    worker.stop()  # main() 退出时经 aboutToQuit 调用同一停止路径
    thread = worker._thread
    thread.join(timeout=2.0)
    assert not thread.is_alive()  # 退出清理：线程真实退出


# ---------------- 本地模型地址保存（wire_ui + tmp 配置） ----------------

def test_save_valid_address_persists_and_marks_pending(qapp, appdata):
    w = _wired(qapp)
    w.set_model_base_url(load_model_base_url())
    assert w.current_model_base_url() == DEFAULT_MODEL_BASE_URL
    w.findChild(QLineEdit, "model_address_edit").setText("http://192.168.1.50:57123")
    w.model_address_changed.emit("http://192.168.1.50:57123")
    assert load_model_base_url() == "http://192.168.1.50:57123"  # 已持久化
    assert "重启后生效" in _label(w, "llm_pending")
    assert f"本次启动生效地址：{DEFAULT_MODEL_BASE_URL}" in _log(w)
    assert _label(w, "llm_active") == f"生效地址：{DEFAULT_MODEL_BASE_URL}"  # 本次启动不变


def test_save_invalid_address_keeps_valid_config(qapp, appdata):
    w = _wired(qapp)
    w.set_model_base_url(load_model_base_url())
    w.model_address_changed.emit("ftp://not-http")
    assert load_model_base_url() == DEFAULT_MODEL_BASE_URL  # 无效输入不覆盖有效配置
    assert "地址无效" in _log(w)
    assert "地址无效" in _label(w, "llm_pending")  # 提示区明示，不虚报“重启后生效”
    assert "重启后生效" not in _label(w, "llm_pending")


def test_save_unchanged_address_clears_stale_notice(qapp, appdata):
    """磁盘地址与本次生效地址一致（如 A→B→A 回到 A）：清除过期“重启后生效”提示。"""
    w = _wired(qapp)
    w.set_model_base_url(load_model_base_url())  # 本次生效 A（默认）
    w.model_address_changed.emit("http://192.168.1.50:57123")  # 保存 B → 提示重启生效
    assert "重启后生效" in _label(w, "llm_pending")
    w.model_address_changed.emit(DEFAULT_MODEL_BASE_URL)  # 保存回 A（与本次生效一致）
    assert load_model_base_url() == DEFAULT_MODEL_BASE_URL
    assert _label(w, "llm_pending") == ""  # 过期 B 提示被清除
    assert w.findChild(QLabel, "llm_pending").isVisible() is False


def test_save_a_to_b_to_c_shows_latest_pending(qapp, appdata):
    """A→B→C：提示始终指向磁盘上最新保存的 C。"""
    w = _wired(qapp)
    w.set_model_base_url(load_model_base_url())
    w.model_address_changed.emit("http://192.168.1.50:57123")  # B
    assert "http://192.168.1.50:57123" in _label(w, "llm_pending")
    w.model_address_changed.emit("http://192.168.1.60:57123")  # C
    assert "重启后生效：http://192.168.1.60:57123" in _label(w, "llm_pending")
    assert "192.168.1.50" not in _label(w, "llm_pending")
    assert load_model_base_url() == "http://192.168.1.60:57123"


def test_save_normalized_equivalent_clears_stale_notice(qapp, appdata):
    """规范化等价（尾部斜杠差异）：视为同一地址，保存后清提示。"""
    w = _wired(qapp)
    effective = load_model_base_url()  # 默认，无尾斜杠
    w.set_model_base_url(effective)
    w.model_address_changed.emit("http://192.168.1.50:57123")  # B → 提示
    assert "重启后生效" in _label(w, "llm_pending")
    w.model_address_changed.emit(effective + "/")  # 保存等价形式 → 与生效一致
    assert load_model_base_url() == effective
    assert _label(w, "llm_pending") == ""
    assert "无需重启" in _log(w)


def test_save_oserror_keeps_config_and_shows_failure(qapp, appdata, monkeypatch):
    """写盘 OSError：提示失败，保持有效配置与本次生效地址，不清除/不虚报。"""
    import main as main_mod

    w = _wired(qapp)
    w.set_model_base_url(load_model_base_url())

    def boom(raw, path=None):
        raise OSError("disk full")

    monkeypatch.setattr(main_mod, "save_model_base_url", boom)
    w.model_address_changed.emit("http://192.168.1.50:57123")
    assert load_model_base_url() == DEFAULT_MODEL_BASE_URL  # 配置未被覆盖
    assert w.current_model_base_url() == DEFAULT_MODEL_BASE_URL
    assert "地址保存失败" in _label(w, "llm_pending")
    assert "disk full" in _label(w, "llm_pending")
    assert "地址保存失败" in _log(w)
    assert "重启后生效" not in _label(w, "llm_pending")


def test_restart_reads_saved_address(qapp, appdata):
    save_model_base_url("http://127.0.0.1:60000")
    # 模拟重启：新窗口 + 重新读取配置
    w = MainWindow()
    w.show()
    base_url = load_model_base_url()
    w.set_model_base_url(base_url)
    assert base_url == "http://127.0.0.1:60000"
    assert w.current_model_base_url() == "http://127.0.0.1:60000"
    assert _label(w, "llm_active") == "生效地址：http://127.0.0.1:60000"
    assert w.findChild(QLineEdit, "model_address_edit").text() == "http://127.0.0.1:60000"
    assert _label(w, "llm_pending") == ""  # 重启后待生效提示清除


def test_probe_display_independent_of_saved_address(qapp, appdata):
    """保存新地址不影响本次启动的探测/连接显示（旧地址结果不冒充新地址）。"""
    w = _wired(qapp)
    w.set_model_base_url(load_model_base_url())
    worker = FakeProbeWorker(ModelProbeResult(True, 8, None, DEFAULT_MODEL_BASE_URL))
    apply_model_probe(w, worker)
    assert "已连接" in _label(w, "llm_status")
    w.model_address_changed.emit("http://192.168.1.50:57123")  # 保存新地址（本次不生效）
    assert "已连接" in _label(w, "llm_status")  # 显示仍是本次启动地址的探测结果
    assert _label(w, "llm_active") == f"生效地址：{DEFAULT_MODEL_BASE_URL}"

def test_listening_does_not_replace_effective_address_with_session_provider(qapp, appdata):
    w = _wired(qapp)
    effective = load_model_base_url()
    w.set_model_base_url(effective)
    w.set_listening(True)
    apply_model_probe(w, FakeProbeWorker(ModelProbeResult(False, None, None, "https://api.example.com/v1", skipped=True)))
    assert _label(w, "llm_status") == "● 非本地模型（免检测）"
    assert _label(w, "llm_active") == _label(w, "cfg_llm_active") == f"生效地址：{effective}"
    apply_model_probe(w, FakeProbeWorker(ModelProbeResult(True, 8, None, "http://192.168.1.10:8080/v1")))
    assert _label(w, "llm_active") == _label(w, "cfg_llm_active") == f"生效地址：{effective}"


def test_dark_theme_ships_in_production_window(qapp):
    """§6 深色主题随生产窗口自带（不只在截图脚本套）；提示色语义：待生效=橙、失败/无效=红。"""
    w = MainWindow()
    assert "#1e1f22" in w.styleSheet()  # 深色底随 MainWindow 生效
    w.set_model_pending("http://192.168.1.50:57123")
    pending = w.findChild(QLabel, "llm_pending")
    assert "#f5a623" in pending.styleSheet()  # 橙=待生效
    w.set_model_notice("地址保存失败：disk full")
    assert "#ef5350" in pending.styleSheet()  # 红=失败
    w.set_model_notice("地址无效，未覆盖当前有效配置")
    assert "#ef5350" in pending.styleSheet()  # 红=无效


def _blank_bg_rgba(w):
    """真实渲染像素：窗口 grab 后取样空白背景点（四角空白区），返回 (r,g,b,a) 列表（不只查样式字符串）。"""
    img = w.grab().toImage()
    points = ((0, 0), (5, 5), (img.width() - 5, 5))
    return [
        ((img.pixel(x, y) >> 16) & 0xFF, (img.pixel(x, y) >> 8) & 0xFF,
         img.pixel(x, y) & 0xFF, (img.pixel(x, y) >> 24) & 0xFF)
        for (x, y) in points
    ]


def test_window_background_paints_opaque_dark(qapp):
    """真实渲染像素断言：正常/待生效窗口空白背景均绘制为不透明深灰 (30,31,34,255)。

    防回归：_DARK_STYLE 中 QWidget background:transparent 不得覆盖 QMainWindow/QDialog 深灰规则
    （Qt 同级选择器后声明者生效），否则窗口背景透明、显示依赖查看器合成。
    """
    w = MainWindow()
    w.show()
    qapp.processEvents()
    assert _blank_bg_rgba(w) == [(30, 31, 34, 255)] * 3  # 正常态
    w.set_model_pending("http://192.168.1.50:57123")
    qapp.processEvents()
    assert _blank_bg_rgba(w) == [(30, 31, 34, 255)] * 3  # 待生效态


def test_real_main_entry_starts_probe_once_and_stops_on_exit(qapp, appdata, monkeypatch):
    """调用真实 main() 入口：probe 恰启动一次；业务客户端与探测地址分离；
    退出（aboutToQuit）时真实 worker 线程有界停止且句柄保留（不手写重建生产流程）。"""
    import controller as controller_mod
    import main as main_mod

    from openchamber_client import OpenChamberClient

    save_model_base_url("http://127.0.0.1:65001")
    expected = "http://127.0.0.1:65001"

    class RecordingWorker(ModelProbeWorker):
        """真实线程生命周期，仅注入快速 fake probe（无真实 HTTP）并记录 start/stop 次数。"""

        last_instance = None
        start_calls = 0
        stop_calls = 0

        def __init__(self, base_url, probe_fn=None, interval_seconds=5.0, target_fn=None):
            super().__init__(
                base_url,
                probe_fn=probe_fn or (lambda: ProbeResult(True, 1, None)),
                interval_seconds=0.05,
                target_fn=lambda: None,
            )
            RecordingWorker.last_instance = self

        def start(self):
            RecordingWorker.start_calls += 1
            super().start()

        def stop(self):
            RecordingWorker.stop_calls += 1
            super().stop()

    class RecClient(OpenChamberClient):
        instances = []

        def __init__(self, base_url, **kw):
            super().__init__(base_url, **kw)
            RecClient.instances.append(self)

    monkeypatch.setattr(main_mod, "ModelProbeWorker", RecordingWorker)
    monkeypatch.setattr(main_mod, "AutoMonitor", FakeMonitor)
    monkeypatch.setattr(main_mod, "discover_zerotier_ip", lambda: None)
    monkeypatch.setattr(controller_mod, "OpenChamberClient", RecClient)

    def fake_exec():
        deadline = time.time() + 0.35
        while time.time() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        qapp.aboutToQuit.emit()  # 真实退出路径：app.aboutToQuit → worker.stop()
        return 0

    monkeypatch.setattr(qapp, "exec", fake_exec, raising=False)

    began = time.perf_counter()
    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    total = time.perf_counter() - began
    assert exc.value.code == 0
    assert total < 10.0  # 有界退出：不无限阻塞

    worker = RecordingWorker.last_instance
    assert worker is not None
    assert RecordingWorker.start_calls == 1  # 恰好启动一次（main 真正调用了 start）
    assert RecordingWorker.stop_calls == 1  # 退出停止接线真实生效
    assert worker.base_url == expected  # 探测地址 = 保存的生效地址
    assert RecClient.instances, "业务客户端未构造"
    assert all(c.base_url == "http://127.0.0.1:57123" for c in RecClient.instances)
    assert worker._thread is not None  # stop() 保留线程句柄（2A 补正语义）
    assert not worker._thread.is_alive()  # 真实线程已退出


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
