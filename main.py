"""AI Relay B V3.0 最小启动骨架（T02）。

职责仅限：安全导入、--self-check 自检、最小占位窗口。
不连接执行端、不监听剪贴板、不创建数据库、不访问旧项目。
正式 UI 自 T13 开始，正式模块（core/storage/ui/workers）自后续任务建立。
"""

import argparse
import sys

APP_NAME = "AI Relay B V3.0"
APP_TITLE = "AI Relay B V3.0"
APP_VERSION = "3.0.0.dev0"
SELF_CHECK_MARKER = "AI_RELAY_B_V3_SELF_CHECK_OK"


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def run_self_check() -> int:
    print(f"{APP_NAME} {APP_VERSION} 基线自检成功（无执行端连接/无剪贴板/无数据库）：{SELF_CHECK_MARKER}")
    return 0


def run_gui() -> int:
    from PySide6.QtWidgets import QApplication, QLabel, QMainWindow

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)

    window = QMainWindow()
    window.setWindowTitle(APP_TITLE)
    label = QLabel("AI Relay B V3.0\n重新开发中")
    label.setAlignment(label.alignmentFlag() | label.AlignCenter)
    window.setCentralWidget(label)
    window.resize(480, 160)
    window.show()
    return app.exec()


def main() -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(prog="ai-relay-b", description=APP_NAME)
    parser.add_argument("--self-check", action="store_true", help="最小自检后退出，返回码0")
    parser.add_argument("--version", action="store_true", help="打印版本后退出")
    args = parser.parse_args()

    if args.version:
        print(f"{APP_NAME} {APP_VERSION}")
        return 0
    if args.self_check:
        return run_self_check()
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())