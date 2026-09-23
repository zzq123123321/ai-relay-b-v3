"""AI Relay B Lite 应用入口：QApplication → MainWindow → LiteController + ClipLinkBridge
→ AutoMonitor → wire_ui → show。

A端 RemoteTask 到达即交给后台 AutoMonitor（单 worker 线程）串行跑自动链的 OpenChamber
HTTP：包装→首发送→watchdog 中断恢复→结果识别；结果确认后由 GUI 主线程交给
ClipLinkBridge 回传，再让 controller 释放任务。Qt 主线程绝不跑 watchdog/结果轮询 HTTP。
自动任务完成且结果已交 Bridge 后，若"自动压缩会话"开，则后台对该任务冻结的 session
做一次 compact（失败只记日志、不阻挡回传、不影响任务释放）；AI_RELAY_COMPLETE 严格停 inbound。
"""

import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from auto_monitor import AutoMonitor
from cliplink_bridge import ClipLinkBridge
from cliplink_status import snapshot
from controller import LiteController
from ui.main_window import MainWindow


def wire_ui(
    window: MainWindow,
    controller: LiteController,
    bridge: ClipLinkBridge,
    monitor: AutoMonitor | None = None,
) -> None:
    """把 MainWindow 的 signal 接到 LiteController + ClipLinkBridge + AutoMonitor。

    手动操作（刷新会话/测模型/手动发送/压缩/监听）是用户主动触发的本机同步操作；
    A端 RemoteTask 只 submit 给后台 monitor（主线程 0 HTTP），真正的 READY/RUNNING/
    结果等由 monitor 事件经 process_monitor_events 回来更新 UI。
    """
    # 主线程维护的极小模型 UI 运行时状态（首响应事件据此保留 OC 服务延迟）
    model_state = {"status": "未检测", "oc_ms": None, "first_response_ms": None}
    window._model_state = model_state

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
            model_state["status"] = "已断开"
            model_state["oc_ms"] = None
            model_state["first_response_ms"] = None
            window.set_model_connection("已断开", oc_ms=None, first_response_ms=None)
            window.append_log("OpenChamber 服务不可达" + (f"（{result.error}）" if result.error else ""))
            bridge.set_model_status("disconnected")
        elif result.ready:
            model_state["status"] = "正常"
            model_state["oc_ms"] = result.service_latency_ms
            window.set_model_connection(
                "正常", oc_ms=result.service_latency_ms, first_response_ms=model_state.get("first_response_ms")
            )
            window.append_log("大模型连接正常")
            bridge.set_model_status("ready", result.service_latency_ms)
        else:
            model_state["status"] = "服务正常"
            model_state["oc_ms"] = result.service_latency_ms
            window.set_model_connection(
                "服务正常", oc_ms=result.service_latency_ms, first_response_ms=model_state.get("first_response_ms")
            )
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
        # A端任务到达 → 立即包装+首发送交给后台 worker（主线程 0 HTTP）。
        # 真正的 READY/RUNNING/结果文案由 monitor 事件回来后更新。
        if monitor is None:
            return
        # 交付1: 严格识别 A→B 的 AI_RELAY_COMPLETE（包装之前，绝不发给 OpenChamber）。
        # 只停 inbound 监听；不 stop monitor 线程 / 不清 Controller 任务 / 不关 ClipLink。
        if task.text == "AI_RELAY_COMPLETE":
            window.set_current_task("项目已完成，自动联动已停止")
            window.append_log("收到 AI_RELAY_COMPLETE，自动联动已停止")
            window.set_listening(False)
            return
        # 交付3: 普通新任务 → 清旧首响应（保留模型状态与 OC 服务延迟）
        state = window._model_state
        state["first_response_ms"] = None
        bridge.set_model_first_response(None)
        window.set_model_connection(state["status"], state["oc_ms"])
        monitor.submit_remote_task(task.event_id, task.text, window.wrapper_template())
        window.set_current_task("已收到A端任务，正在处理")
        window.append_log("收到 A端任务，正在后台处理")

    def on_auto_compact(enabled: bool) -> None:
        # GUI 线程：只让 monitor 入队更新运行时开关（0 HTTP）；monitor=None 时不崩。
        if monitor is not None:
            monitor.set_auto_compact(enabled)
        window.append_log("自动压缩已开启" if enabled else "自动压缩已关闭")

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


def process_monitor_events(monitor: AutoMonitor, window: MainWindow, bridge: ClipLinkBridge) -> None:
    """GUI 线程：取走 AutoMonitor 事件并更新 UI / 交给 ClipLinkBridge。

    绝不做 OpenChamber HTTP（HTTP 全在 monitor worker 线程）；也不直接 setText 之外的
    网络/剪贴板操作。QTimer 周期性调用本函数即可。
    """
    model_state = getattr(window, "_model_state", None) or {
        "status": "未检测",
        "oc_ms": None,
        "first_response_ms": None,
    }
    for ev in monitor.drain_events():
        _dispatch_monitor_event(monitor, window, bridge, model_state, ev)


def _dispatch_monitor_event(
    monitor: AutoMonitor, window: MainWindow, bridge: ClipLinkBridge, model_state: dict, ev: dict
) -> None:
    t = ev.get("type")
    if t == "intake_ready":
        window.set_current_task("已包装，等待大模型恢复")
        window.append_log("收到 A端新任务，已完成包装，等待大模型")
    elif t == "intake_running":
        window.set_current_task("已提交到大模型，等待执行")
        window.append_log("收到 A端新任务，已包装并提交到当前会话")
    elif t == "intake_busy":
        window.append_log("自动任务未接收：已有任务正在处理")
    elif t == "sent":
        window.set_current_task("已提交到大模型，等待执行")
    elif t == "offline":
        window.set_current_task("大模型连接中断，等待恢复")
        bridge.set_model_status("disconnected")
    elif t == "recover_check":
        window.set_current_task("大模型已恢复，正在检查断点")
    elif t == "resumed_automatically":
        window.set_current_task("大模型已自行恢复执行")
    elif t == "resume_sent":
        window.set_current_task("已发送断点续接指令")
        window.append_log("大模型未自行继续，已发送一次续接指令")
    elif t == "first_response":
        # 保留已有的 status + OC 服务延迟，只补上模型首响应，绝不把 OC 延迟清成 --
        model_state["first_response_ms"] = ev.get("first_response_ms")
        window.set_model_connection(
            model_state.get("status", "未检测"),
            oc_ms=model_state.get("oc_ms"),
            first_response_ms=ev.get("first_response_ms"),
        )
        # 交付3: UI 与 AIRelayLite/status.json 用同一真实测量值（不额外 ping）
        bridge.set_model_first_response(ev.get("first_response_ms"))
    elif t == "result_complete":
        # 顺序关键：先交 Bridge（回传/接管 pending），再让 monitor 释放 Controller 任务
        window.set_recent_result(ev.get("text"))
        bridge.deliver_result(ev.get("text"))
        monitor.acknowledge_result(ev.get("event_id"))
        window.set_current_task("自动任务已完成，结果已进入回传链路")
        window.append_log("大模型任务完成")
    elif t == "result_ambiguous":
        window.set_current_task("结果识别存在冲突，已暂停自动回传")
        window.append_log("检测到同一会话存在额外用户消息，未自动回传")
    elif t == "compact_success":
        # compact 成功：只记日志，不改最近结果/当前任务文案，不弹 modal
        window.append_log("自动任务会话压缩完成")
    elif t == "compact_failed":
        # compact 失败 != 任务失败：只记日志，不把已完成的"结果已进入回传链路"改成失败
        window.append_log(f"自动压缩失败：{ev.get('error', '')}")


def start_monitor_poll(
    window: MainWindow, monitor: AutoMonitor, bridge: ClipLinkBridge, interval_ms: int = 100
) -> QTimer:
    """GUI 线程 QTimer：周期性 drain AutoMonitor 事件更新 UI（不做 OpenChamber HTTP）。

    首帧立即 drain 一次；计时器挂到 window 下随窗口存活。
    """
    timer = QTimer(window)
    timer.setInterval(interval_ms)
    timer.timeout.connect(lambda: process_monitor_events(monitor, window, bridge))
    timer.start()
    process_monitor_events(monitor, window, bridge)
    return timer


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("AI Relay B Lite")
    window = MainWindow()
    controller = LiteController()
    bridge = ClipLinkBridge(
        clipboard_writer=lambda text: QApplication.clipboard().setText(text),
    )
    monitor = AutoMonitor(controller, interval_seconds=1.0)
    wire_ui(window, controller, bridge, monitor)
    start_cliplink_poll(window)
    start_bridge_poll(window, bridge)
    start_monitor_poll(window, monitor, bridge)
    monitor.start()
    app.aboutToQuit.connect(monitor.stop)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()