"""AI Relay B Lite 应用入口：QApplication → MainWindow → LiteController + ClipLinkBridge → wire_ui → show。

本轮把 UI 的 signal 接到 LiteController（全 fake 可测）+ ClipLinkBridge（A→B 事件读取、
B→A 结果回传、AIRelayLite 状态文件），并把 A端 RemoteTask 接到自动任务链（立即包装 +
首次自动发送）。仍不做模型 watchdog / 自动压缩 / 中断续接 / AI_RELAY_COMPLETE。
"""

import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from cliplink_bridge import ClipLinkBridge
from cliplink_status import snapshot
from controller import LiteController
from ui.main_window import MainWindow


def wire_ui(window: MainWindow, controller: LiteController, bridge: ClipLinkBridge) -> None:
    """把 MainWindow 的 signal 接到 LiteController + ClipLinkBridge。

    全部是用户主动触发的本机同步操作 + ClipLink 事件桥接线 + A端 RemoteTask 自动任务链
    （立即包装 + 首次发送），本轮不引入线程框架；模型 watchdog / 自动压缩留给后续 monitor。
    """

    def on_refresh_session() -> None:
        session = controller.get_current_session()
        if session.valid:
            window.set_current_session(session.session_id)
            window.append_log("已获取当前激活会话")
            bridge.set_session_id(session.session_id)
        else:
            window.set_current_session(None)
            window.append_log("当前激活会话不可用")
            bridge.set_session_id(None)

    def on_test_model() -> None:
        result = controller.test_model_connection()
        if not result.connected:
            window.set_model_connection("已断开", oc_ms=None, first_response_ms=None)
            window.append_log("OpenChamber 服务不可达" + (f"（{result.error}）" if result.error else ""))
            bridge.set_model_status("disconnected")
        elif result.ready:
            window.set_model_connection("正常", oc_ms=result.service_latency_ms, first_response_ms=None)
            window.append_log("大模型连接正常")
            bridge.set_model_status("ready", result.service_latency_ms)
        else:
            window.set_model_connection("服务正常", oc_ms=result.service_latency_ms, first_response_ms=None)
            window.append_log("OpenChamber 服务正常，但当前会话模型配置不可用")
            bridge.set_model_status("service_only", result.service_latency_ms)

    def on_manual_send(text: str) -> None:
        result = controller.manual_send(text)
        if result.accepted:
            window.append_log("手动内容已提交到当前会话")
            window.set_current_task("已提交，等待模型处理")
        else:
            window.append_log(f"手动发送失败：{result.error}")

    def on_compact() -> None:
        result = controller.compact_current_session()
        if result.success:
            window.append_log("已提交当前会话压缩")
        else:
            window.append_log(f"压缩失败：{result.error}")

    def on_listening(enabled: bool) -> None:
        bridge.set_listening(enabled)
        window.append_log("自动监听已开始" if enabled else "自动监听已停止")

    def on_remote_task(task) -> None:
        # A端任务到达 → 读当前包装模板 → 交给 controller 立即包装并尝试首发送。
        # 不再检查 A端是否仍连接：包装与发送只依赖模型可用性，A端只影响最终回传。
        intake = controller.receive_auto_task(task.event_id, task.text, window.wrapper_template())
        if not intake.accepted:
            window.append_log("自动任务未接收：已有任务正在处理")
        elif intake.submitted:
            window.set_current_task("已提交到大模型，等待执行")
            window.append_log("收到 A端新任务，已包装并提交到当前会话")
        else:
            window.set_current_task("已包装，等待大模型恢复")
            window.append_log("收到 A端新任务，已完成包装，等待大模型")

    def on_auto_compact(enabled: bool) -> None:
        window.append_log("自动压缩已设置为" + ("开" if enabled else "关") + "，后台策略将在后续阶段接入")

    bridge.on_remote_task = on_remote_task

    window.refresh_session_requested.connect(on_refresh_session)
    window.test_model_requested.connect(on_test_model)
    window.manual_send_requested.connect(on_manual_send)
    window.compact_requested.connect(on_compact)
    window.listening_changed.connect(on_listening)
    window.auto_compact_changed.connect(on_auto_compact)


def apply_cliplink_status(window: MainWindow, path=None, now_ms: int | None = None) -> None:
    """读一次 ClipLink 状态文件并刷新“A端连接”卡（文件缺失/损坏 → 未连接）。

    纯“读+判定+刷新”，无计时器，便于测试直接以固定 now_ms 调用。
    """
    status, peer, latency = snapshot(path, now_ms)
    window.set_a_connection(status, peer, latency)


def start_cliplink_poll(window: MainWindow, interval_ms: int = 2000, path=None) -> QTimer:
    """定时轮询 ClipLink 状态文件：GUI 线程 QTimer，非后台线程框架。

    首帧立即刷新一次，之后每 interval_ms 读一次；计时器挂到 window 下随窗口存活，
    返回计时器以便测试/关闭时 stop。
    """
    timer = QTimer(window)
    timer.setInterval(interval_ms)
    timer.timeout.connect(lambda: apply_cliplink_status(window, path))
    timer.start()
    apply_cliplink_status(window, path)
    return timer


def start_bridge_poll(window: MainWindow, bridge: ClipLinkBridge, interval_ms: int = 400) -> QTimer:
    """定时驱动 ClipLinkBridge：poll remote event + flush pending + 写状态文件。

    首帧立即 tick 一次，之后每 interval_ms 一轮；计时器挂到 window 下随窗口存活。
    """
    timer = QTimer(window)
    timer.setInterval(interval_ms)
    timer.timeout.connect(bridge.tick)
    timer.start()
    bridge.tick()
    return timer


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("AI Relay B Lite")
    window = MainWindow()
    controller = LiteController()
    bridge = ClipLinkBridge(
        clipboard_writer=lambda text: QApplication.clipboard().setText(text),
    )
    wire_ui(window, controller, bridge)
    start_cliplink_poll(window)
    start_bridge_poll(window, bridge)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()