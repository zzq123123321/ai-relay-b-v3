"""AI Relay B Lite 应用入口：QApplication → MainWindow → LiteController → wire_ui → show。

本轮把 UI 的 signal 接到 LiteController（全 fake 可测）。仍不启动后台
自动监听 / 自动压缩 / watchdog（那些属于后续阶段的后台 monitor）。
"""

import sys

from PySide6.QtWidgets import QApplication

from controller import LiteController
from ui.main_window import MainWindow


def wire_ui(window: MainWindow, controller: LiteController) -> None:
    """把 MainWindow 的 signal 接到 LiteController。

    全部是用户主动触发的本机同步操作，本轮不引入线程框架；
    后台自动逻辑（监听/自动压缩/watchdog）统一留给后续 monitor。
    """

    def on_refresh_session() -> None:
        session = controller.get_current_session()
        if session.valid:
            window.set_current_session(session.session_id)
            window.append_log("已获取当前激活会话")
        else:
            window.set_current_session(None)
            window.append_log("当前激活会话不可用")

    def on_test_model() -> None:
        result = controller.test_model_connection()
        if not result.connected:
            window.set_model_connection("已断开", oc_ms=None, first_response_ms=None)
            window.append_log("OpenChamber 服务不可达" + (f"（{result.error}）" if result.error else ""))
        elif result.ready:
            window.set_model_connection("正常", oc_ms=result.service_latency_ms, first_response_ms=None)
            window.append_log("大模型连接正常")
        else:
            window.set_model_connection("服务正常", oc_ms=result.service_latency_ms, first_response_ms=None)
            window.append_log("OpenChamber 服务正常，但当前会话模型配置不可用")

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

    def on_listening(_enabled: bool) -> None:
        window.append_log("自动监听将在后续阶段接入")

    def on_auto_compact(enabled: bool) -> None:
        window.append_log("自动压缩已设置为" + ("开" if enabled else "关") + "，后台策略将在后续阶段接入")

    window.refresh_session_requested.connect(on_refresh_session)
    window.test_model_requested.connect(on_test_model)
    window.manual_send_requested.connect(on_manual_send)
    window.compact_requested.connect(on_compact)
    window.listening_changed.connect(on_listening)
    window.auto_compact_changed.connect(on_auto_compact)


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("AI Relay B Lite")
    window = MainWindow()
    controller = LiteController()
    wire_ui(window, controller)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()