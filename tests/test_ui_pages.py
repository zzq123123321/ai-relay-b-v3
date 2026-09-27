"""阶段 2C 三页 UI 测试（offscreen，全 fake，无真实 HTTP/剪切板）：

页面结构（主控/模型配置/模板与日志）、全部原按钮完整且触发原信号一次、
页签切换保留输入/草稿/开关且不重复接线、Ctrl+Enter 作用域、
主控页与配置页状态同步、填入模板不发送并回主控、默认/适配尺寸。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QLabel, QLineEdit, QPlainTextEdit, QPushButton

from main import wire_ui
from test_ui_wiring import FakeBridge, FakeController
from ui.main_window import COMMANDER_TEMPLATE, MainWindow

# 长内容样本（经 normalize_model_base_url 可接受的合法长地址 / 长错误提示）
LONG_ADDR = "http://127.0.0.1:57123/" + "gateway/" * 15 + "v1"
LONG_ERR = "地址保存失败：" + "配置文件所在目录不可写。" * 12
LONG_PENDING = "新地址已保存，重启后生效：" + LONG_ADDR


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _wired(qapp, bridge=None):
    w = MainWindow()
    w.show()
    wire_ui(w, FakeController(), bridge if bridge is not None else FakeBridge())
    return w


def _label(w, name: str) -> str:
    from PySide6.QtWidgets import QLabel

    return w.findChild(QLabel, name).text()


def test_three_pages_exist_and_default_main(qapp):
    w = _wired(qapp)
    tabs = [w._main_tabs.widget(i).objectName() for i in range(w._main_tabs.count())]
    assert tabs == ["page_main", "page_config", "page_templates"]
    titles = [w._main_tabs.tabText(i) for i in range(w._main_tabs.count())]
    assert titles == ["主控", "模型配置", "模板与日志"]
    assert w._main_tabs.currentWidget() is w._page_main  # 默认页=主控
    # 第三页内分 项目模板 / 运行日志 两个内容切换区
    assert w._tpl_tabs.count() == 2
    assert w._tpl_tabs.tabText(0) == "项目模板"
    assert w._tpl_tabs.tabText(1) == "运行日志"


def test_all_original_buttons_present(qapp):
    """2C 重排后全部原按钮仍在（原 objectName 不变），且新增两个入口。"""
    w = _wired(qapp)
    for name in (
        "btn_refresh", "btn_compact", "chk_auto",
        "btn_commander", "btn_wrapper", "btn_clear", "manual_input", "btn_send",
        "btn_listen",
        "model_address_edit", "btn_save_address",
        "btn_view_log",          # 新增：查看完整日志（跳第三页日志区）
        "btn_result_detail",     # 新增：最近结果完整详情（可点击，不只依赖悬停）
        "commander_restore", "commander_cancel", "commander_fill",  # 模板区（原对话框按钮改为内联）
    ):
        assert w.findChild(QPushButton, name) is not None or \
            w.findChild(QCheckBox, name) is not None or \
            w.findChild(QLineEdit, name) is not None or \
            w.findChild(QPlainTextEdit, name) is not None, f"缺少控件 {name}"


def test_each_button_fires_its_original_signal_once(qapp, tmp_path, monkeypatch):
    """每个原按钮触发原信号恰好一次（不重复接线）。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    w = _wired(qapp)
    fired: dict[str, list] = {
        "refresh": [], "compact": [], "send": [], "listen": [],
        "auto": [], "addr": [],
    }
    w.refresh_session_requested.connect(lambda: fired["refresh"].append(1))
    w.compact_requested.connect(lambda: fired["compact"].append(1))
    w.manual_send_requested.connect(lambda t: fired["send"].append(t))
    w.listening_changed.connect(lambda b: fired["listen"].append(b))
    w.auto_compact_changed.connect(lambda b: fired["auto"].append(b))
    w.model_address_changed.connect(lambda t: fired["addr"].append(t))

    w.findChild(QPushButton, "btn_refresh").click()
    w.findChild(QPushButton, "btn_compact").click()
    w.findChild(QPushButton, "btn_send").click()
    w.findChild(QPushButton, "btn_listen").click()
    w.findChild(QCheckBox, "chk_auto").toggle()
    edit = w.findChild(QLineEdit, "model_address_edit")
    edit.setText("http://127.0.0.1:57123")
    w.findChild(QPushButton, "btn_save_address").click()

    assert fired["refresh"] == [1]
    assert fired["compact"] == [1]
    assert fired["send"] == [w.findChild(QPlainTextEdit, "manual_input").toPlainText()]
    assert fired["listen"] == [True]
    assert fired["auto"] == [True]
    assert fired["addr"] == ["http://127.0.0.1:57123"]


def test_non_signal_buttons_keep_their_action(qapp):
    """无信号入口保持原动作：清空/首页模板跳转/查看完整日志。

    阶段 4A：「包装内容」改为即时包装信号入口，点击不再打开弹窗。
    """
    w = _wired(qapp)
    edit = w.findChild(QPlainTextEdit, "manual_input")
    edit.setPlainText("草稿")
    w.findChild(QPushButton, "btn_clear").click()
    assert edit.toPlainText() == ""

    assert w.findChild(QPushButton, "btn_wrapper_edit") is None
    w.findChild(QPushButton, "btn_wrapper").click()

    w.findChild(QPushButton, "btn_commander").click()
    assert w._main_tabs.currentWidget() is w._page_templates
    assert w._tpl_tabs.currentIndex() == 0  # 首页模板入口跳“项目模板”编辑区

    w.show_main_page()
    w.findChild(QPushButton, "btn_view_log").click()
    assert w._main_tabs.currentWidget() is w._page_templates
    assert w._tpl_tabs.currentIndex() == 1  # 查看完整日志跳“运行日志”区


def test_page_switching_keeps_inputs_drafts_and_toggles(qapp):
    w = _wired(qapp)
    manual = w.findChild(QPlainTextEdit, "manual_input")
    commander = w.findChild(QPlainTextEdit, "commander_edit")
    addr = w.findChild(QLineEdit, "model_address_edit")
    chk = w.findChild(QCheckBox, "chk_auto")
    manual.setPlainText("手动草稿 123")
    commander.setPlainText("模板草稿 draft")
    addr.setText("http://10.0.0.9:57123")
    chk.setChecked(True)
    w.set_recent_result("R1")

    for _ in range(3):  # 反复切页
        w._main_tabs.setCurrentIndex(1)
        w._main_tabs.setCurrentIndex(2)
        w._tpl_tabs.setCurrentIndex(1)
        w._tpl_tabs.setCurrentIndex(0)
        w._main_tabs.setCurrentIndex(0)

    assert manual.toPlainText() == "手动草稿 123"
    assert commander.toPlainText() == "模板草稿 draft"
    assert addr.text() == "http://10.0.0.9:57123"
    assert chk.isChecked() is True
    assert w.findChild(QPlainTextEdit, "result_box").toPlainText() == "R1"


def test_no_duplicate_wiring_or_timers_after_switching(qapp):
    w = _wired(qapp)
    sends = []
    w.manual_send_requested.connect(sends.append)
    timers_before = len(w.findChildren(QTimer))
    for i in range(6):
        w._main_tabs.setCurrentIndex(i % 3)
        w._tpl_tabs.setCurrentIndex(i % 2)
    assert len(w.findChildren(QTimer)) == timers_before  # 切页不重复创建 QTimer
    w.findChild(QPushButton, "btn_send").click()
    w.findChild(QPushButton, "btn_send").click()
    assert len(sends) == 2  # 每点一次恰好触发一次（无重复接线）


def test_ctrl_enter_scope_only_manual_input(qapp):
    """Ctrl+Enter 仅手动发送框聚焦时发送；模板/地址框聚焦不触发全局发送。"""
    w = _wired(qapp)
    sends = []
    w.manual_send_requested.connect(sends.append)

    manual = w.findChild(QPlainTextEdit, "manual_input")
    manual.setPlainText("send me")
    manual.setFocus()
    QTest.keyClick(manual, Qt.Key_Return, Qt.ControlModifier)
    assert sends == ["send me"]

    commander = w.findChild(QPlainTextEdit, "commander_edit")
    commander.setFocus()
    QTest.keyClick(commander, Qt.Key_Return, Qt.ControlModifier)
    assert sends == ["send me"]  # 模板框聚焦不发送

    addr = w.findChild(QLineEdit, "model_address_edit")
    addr.setFocus()
    QTest.keyClick(addr, Qt.Key_Return, Qt.ControlModifier)
    assert sends == ["send me"]  # 地址框聚焦不发送


def test_main_and_config_pages_share_model_state(qapp):
    """主控页与配置页用同一探测结果与生效地址刷新（镜像一致，不增加 HTTP）。"""
    w = _wired(qapp)
    w.set_model_base_url("http://127.0.0.1:57123")
    w.set_model_connection("已连接", 23)

    assert _label(w, "llm_status") == _label(w, "cfg_llm_status") == "● 已连接"
    assert _label(w, "llm_oc") == _label(w, "cfg_llm_oc") == "服务延迟：23 ms"
    assert _label(w, "llm_active") == _label(w, "cfg_llm_active") == "生效地址：http://127.0.0.1:57123"
    assert w.findChild(QLineEdit, "model_address_edit").text() == "http://127.0.0.1:57123"
    assert w.current_model_base_url() == "http://127.0.0.1:57123"

    w.set_model_connection("未连接", None)
    assert _label(w, "llm_status") == _label(w, "cfg_llm_status") == "● 未连接"
    assert _label(w, "llm_oc") == _label(w, "cfg_llm_oc") == "服务延迟：-- ms"


def test_config_page_pending_and_failure_notice(qapp):
    w = _wired(qapp)
    w._main_tabs.setCurrentIndex(1)  # 提示区在配置页
    w.set_model_pending("http://192.168.1.50:57123")
    pending = w.findChild(w._llm_pending.__class__, "llm_pending")
    assert pending.isVisible()
    assert pending.text() == "新地址已保存，重启后生效：http://192.168.1.50:57123"
    assert "#f5a623" in pending.styleSheet()  # 橙=待生效
    w.set_model_notice("地址保存失败：disk full")
    assert "#ef5350" in pending.styleSheet()  # 红=失败
    assert "disk full" in pending.text()


def test_commander_template_official_text_and_fill_flow(qapp):
    w = _wired(qapp)
    commander = w.findChild(QPlainTextEdit, "commander_edit")
    assert commander.toPlainText() == COMMANDER_TEMPLATE  # 模板原文不被替换
    sends = []
    w.manual_send_requested.connect(sends.append)
    w.findChild(QPushButton, "commander_fill").click()
    assert w.findChild(QPlainTextEdit, "manual_input").toPlainText() == COMMANDER_TEMPLATE
    assert sends == []  # 填入不发送
    assert w._main_tabs.currentWidget() is w._page_main  # 自动回主控
    w.findChild(QPushButton, "commander_restore").click()
    assert commander.toPlainText() == COMMANDER_TEMPLATE
    w.findChild(QPushButton, "commander_cancel").click()
    assert w._main_tabs.currentWidget() is w._page_main  # 取消/返回回主控


def test_worker_lifecycle_unaffected_by_page_switching(qapp):
    """切页不影响 worker/QTimer 生命周期（worker 启动/停止由 main 层唯一调用）。"""
    w = _wired(qapp)
    before = len(w.findChildren(QTimer))
    w._main_tabs.setCurrentIndex(1)
    w._main_tabs.setCurrentIndex(2)
    w.show_main_page()
    assert len(w.findChildren(QTimer)) == before


def test_long_content_does_not_stretch_window(qapp):
    """合法长地址（生效/待生效）与长错误不撑宽窗口：两尺寸 × 全部顶层页（含隐藏页）。

    断言实际 window.size 与 minimumSizeHint 有界、标签完整保留原文且控件在窗口边界内。
    """
    for size in ((720, 640), (680, 600)):
        w = _wired(qapp)
        w.resize(*size)
        w.set_model_base_url(LONG_ADDR)  # 长生效地址
        for page in (0, 1, 2):  # 遍历全部顶层页（QTabWidget 受隐藏页最小宽影响）
            w._main_tabs.setCurrentIndex(page)
        qapp.processEvents()
        w.resize(*size)
        assert w.width() == size[0], f"长生效地址撑宽窗口 {size}"
        assert w.minimumSizeHint().width() <= size[0]
        assert w.minimumSizeHint().height() <= size[1]
        for name, page in (("llm_active", w._page_main), ("cfg_llm_active", w._page_config)):
            w._main_tabs.setCurrentWidget(page)  # 隐藏页先切到使其在新尺寸下重排，再查边界
            qapp.processEvents()
            lbl = w.findChild(QLabel, name)
            assert LONG_ADDR in lbl.text()  # 完整原文不截断
            tl = w.mapFromGlobal(lbl.mapToGlobal(lbl.rect().topLeft()))
            br = w.mapFromGlobal(lbl.mapToGlobal(lbl.rect().bottomRight()))
            assert w.rect().contains(tl) and w.rect().contains(br), f"{name} 超出窗口边界"

        w.set_model_pending(LONG_ADDR)  # 长待生效提示
        w._main_tabs.setCurrentIndex(2)  # 提示所在配置页变隐藏页仍不撑宽
        qapp.processEvents()
        w.resize(*size)
        assert w.width() == size[0]
        assert w.minimumSizeHint().width() <= size[0]
        assert w.minimumSizeHint().height() <= size[1]
        pend = w.findChild(QLabel, "llm_pending")
        assert LONG_PENDING in pend.text()
        w._main_tabs.setCurrentIndex(1)
        qapp.processEvents()
        assert w.rect().contains(w.mapFromGlobal(pend.mapToGlobal(pend.rect().bottomRight())))

        w.set_model_notice(LONG_ERR)  # 长错误提示
        w._main_tabs.setCurrentIndex(0)
        qapp.processEvents()
        w.resize(*size)
        assert w.width() == size[0]
        assert w.minimumSizeHint().width() <= size[0]
        assert w.minimumSizeHint().height() <= size[1]
        assert LONG_ERR in pend.text()
        w.close()


def test_click_label_opens_full_text_dialog(qapp):
    """有界标签左点击开全文详情弹窗：长原文完整展示（不只 tooltip），可关闭。"""
    w = _wired(qapp)
    w.set_model_base_url(LONG_ADDR)
    from PySide6.QtTest import QTest as _QTest

    lbl = w.findChild(QLabel, "llm_active")  # 主控页镜像
    _QTest.mouseClick(lbl, Qt.LeftButton)
    dlg = w.findChild(QDialog, "full_text_dialog")
    assert dlg is not None and dlg.isVisible()
    assert dlg.findChild(QPlainTextEdit, "full_text").toPlainText() == "生效地址：" + LONG_ADDR
    dlg.findChild(QPushButton, "full_text_close").click()

    w._main_tabs.setCurrentIndex(1)
    w.set_model_pending(LONG_ADDR)
    pend = w.findChild(QLabel, "llm_pending")  # 配置页提示（长错误/长待生效同源入口）
    _QTest.mouseClick(pend, Qt.LeftButton)
    assert dlg.isVisible()
    assert dlg.findChild(QPlainTextEdit, "full_text").toPlainText() == LONG_PENDING


def test_result_detail_click_shows_full_long_result(qapp):
    """点击 [详情] 后完整长结果不丢失（可滚动只读区，不只摘要行/悬停）。"""
    w = _wired(qapp)
    long_result = "".join(f"第{i:03d}行结果内容" for i in range(200))
    w.set_recent_result(long_result)
    w.findChild(QPushButton, "btn_result_detail").click()
    dlg = w.findChild(QDialog, "result_detail_dialog")
    assert dlg is not None and dlg.isVisible()
    assert dlg.findChild(QPlainTextEdit, "result_detail").toPlainText() == long_result


def test_page_switching_with_running_probe_worker(qapp):
    """已启动 fake-probe worker + 生产 start_model_poll 真实 QTimer 运行期间切页：

    首帧（立即读）驱动 UI；其后两次改变 fake 探测结果（延迟 23→87→断开），真实
    timer tick 持续把新值刷到主控页与模型配置页两镜像（排除“只首帧生效”）；切页
    不额外 start/stop worker、不增删轮询 QTimer，窗口仍可交互（发送恰好一次）。
    last_result 调用次数只作辅助证据；核心断言是刷新效果（新值出现在两镜像）。
    """
    import time as _time

    from main import start_model_poll
    from model_probe import ModelProbeResult

    class RunningWorker:
        base_url = "http://127.0.0.1:57123"

        def __init__(self) -> None:
            self.started = 0
            self.stopped = 0
            self.results_read = 0
            self._result = ModelProbeResult(True, 23, None, self.base_url)

        def start(self) -> None:  # 生产语义：幂等
            if self.started:
                return
            self.started += 1

        def stop(self) -> None:
            self.stopped += 1

        def last_result(self):
            self.results_read += 1
            return self._result

    def _set_result(worker, **fields):
        worker._result = ModelProbeResult(base_url=worker.base_url, **fields)

    def _wait_until(predicate, timeout_ms=2000):
        """有超时的等待：spin 事件循环让真实 QTimer tick 发生，不固定死等。"""
        deadline = _time.monotonic() + timeout_ms / 1000
        while _time.monotonic() < deadline:
            if predicate():
                return True
            QTest.qWait(10)
        return False

    w = _wired(qapp)
    w.resize(720, 640)
    worker = RunningWorker()
    worker.start()
    timer = start_model_poll(w, worker, interval_ms=40)  # 生产入口：真实轮询计时器运行中
    assert "已连接" in w._llm_status.text() and "23 ms" in w._llm_oc.text()  # 首帧立即读已驱动 UI
    assert worker.results_read == 1  # 首帧恰好读一次；其后所有刷新必须来自 timer tick
    before = len(w.findChildren(QTimer))

    # 第一轮：改变 fake 结果（延迟 23→87，仍连接），切页后等真实 tick
    w._main_tabs.setCurrentIndex(1)
    _set_result(worker, connected=True, latency_ms=87, error=None)
    assert _wait_until(
        lambda: "87 ms" in w._llm_oc.text() and "87 ms" in w._cfg_llm_oc.text()
    ), "第一轮刷新未到达：新延迟值未出现在主控/配置两镜像"
    assert "已连接" in w._llm_status.text() and "已连接" in w._cfg_llm_status.text()

    # 第二轮：再改变一次返回值（断开），再切页后等真实 tick
    w._main_tabs.setCurrentIndex(2)
    w._tpl_tabs.setCurrentIndex(1)
    _set_result(worker, connected=False, latency_ms=None, error="ECONNREFUSED")
    assert _wait_until(
        lambda: "未连接" in w._llm_status.text() and "未连接" in w._cfg_llm_status.text()
    ), "第二轮刷新未到达：主控/配置两镜像未切为未连接"

    w.show_main_page()
    assert len(w.findChildren(QTimer)) == before  # 切页不增删轮询计时器
    assert worker.started == 1 and worker.stopped == 0  # 切页未额外 start/stop worker
    assert worker.results_read > 1  # 辅助证据：轮询确实多次读取结果
    fired = []
    w.manual_send_requested.connect(fired.append)
    w.findChild(QPushButton, "btn_send").click()
    assert len(fired) == 1  # 切页+轮询期间窗口仍可交互，发送恰好触发一次
    timer.stop()
    worker.stop()
    assert worker.stopped == 1  # 清理：timer 与 worker 均停止
    w.close()


def test_default_and_adapted_window_sizes(qapp):
    w = _wired(qapp)
    assert w.width() == 720 and w.height() == 640  # 2C 默认尺寸
    w.resize(680, 600)
    assert w.width() == 680 and w.height() == 600  # 680×600 必须可用
    assert w.minimumSizeHint().width() <= 680
    assert w.minimumSizeHint().height() <= 600


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
