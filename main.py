"""AI Relay B Lite 应用入口：QApplication → MainWindow → LiteController + ClipLinkBridge
→ AutoMonitor → wire_ui → show。

A端 RemoteTask 经 ClipLinkClient 的 sink 同步交 ClipLinkBridge 入站 FIFO（阶段 3F-2：
至多一项在途、busy 回队首不丢正文、COMPLETE 控制事件旁路）；被投递项提交给后台
AutoMonitor（单 worker 线程）串行跑自动链的 OpenChamber HTTP：包装→首发送→watchdog
中断恢复→结果识别；worker 的 accepted/busy 回执（带 event_id/attempt）由 GUI 匹配
Bridge 在途槽。结果确认后由 GUI 主线程交给 ClipLinkBridge 回传单槽，仅在本机剪贴板
写入成功（同步在线写或恢复后 flush 成功）后才释放任务（唯一 ack 路径 on_local_write，
按 event_id 匹配）。Qt 主线程绝不跑 watchdog/结果轮询 HTTP。
自动任务完成且结果已交 Bridge 后，若"自动压缩会话"开，则后台对该任务冻结的 session
做一次 compact（失败只记日志、不阻挡回传、不影响任务释放）；AI_RELAY_COMPLETE 严格停 inbound。
"""

import sys
import uuid

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from auto_monitor import AutoMonitor
from cliplink_bridge import ClipLinkBridge, envelope_route, extract_envelope, is_nested_return
from cliplink_client import ClipLinkClient, discover_zerotier_ip
from cliplink_status import snapshot
from controller import LiteController, is_usable_identity, wrap_auto_response
from model_probe import (
    ModelProbeWorker,
    OPENCHAMBER_BASE_URL,
    load_model_base_url,
    save_model_base_url,
)
from ui.main_window import MainWindow


def wire_ui(
    window: MainWindow,
    controller: LiteController,
    bridge: ClipLinkBridge,
    monitor: AutoMonitor | None = None,
    clipboard=None,
) -> None:
    """把 MainWindow 的 signal 接到 LiteController + ClipLinkBridge + AutoMonitor。

    刷新会话/监听/自动压缩可由启动流程自动触发，手动操作仍可独立使用；
    阶段 4A：手动发送/包装内容 = 立即套正式回包格式并写本机剪贴板（恰一次），
    全程 0 HTTP、不调 controller.manual_send、不 ack/finish、不改监听与自动队列；
    clipboard 为可注入的本机剪贴板读写器（生产为 QApplication.clipboard()）。
    模型测试保留为内部入口（无 UI 按钮），其结果不覆盖 probe 驱动的模型连接显示。
    A端 RemoteTask 只 submit 给后台 monitor（主线程 0 HTTP），真正的 READY/RUNNING/
    结果等由 monitor 事件经 process_monitor_events 回来更新 UI。
    """
    # 主线程维护的极小模型 UI 运行时状态（首响应事件据此保留内部数据；
    # 连接显示本身只由 probe 轮询驱动，这些旧事件不再覆盖 llm_status/llm_oc）
    model_state = {"status": "未检测", "oc_ms": None, "first_response_ms": None}
    window._model_state = model_state

    def on_refresh_session() -> None:
        try:
            session = controller.get_current_session()
        except Exception as exc:
            window.set_session_notice(f"获取当前激活会话失败：{exc}", False)
            window.append_log(f"获取当前激活会话失败：{exc}")
            return
        if session.valid:
            window.set_current_session(session.session_id)
            window.set_session_notice(f"已获取当前激活会话：{session.session_id}", True)
            window.append_log("已获取当前激活会话")
            bridge.set_session_id(session.session_id)
        else:
            window.set_current_session(None)
            window.set_session_notice(f"未获取当前激活会话：{session.error or '当前无可用会话'}", False)
            window.append_log("当前激活会话不可用")
            bridge.set_session_id(None)

    def on_test_model() -> None:
        # 内部手动测试（UI 已无入口）：只更新内部状态/日志/Bridge，
        # 不覆盖 probe 驱动的“已连接/未连接”显示，避免 GUI 同步 HTTP 结果干扰连接卡。
        result = controller.test_model_connection()
        if not result.connected:
            model_state["status"] = "已断开"
            model_state["oc_ms"] = None
            model_state["first_response_ms"] = None
            window.append_log("OpenChamber 服务不可达" + (f"（{result.error}）" if result.error else ""))
            bridge.set_model_status("disconnected")
        elif result.ready:
            model_state["status"] = "正常"
            model_state["oc_ms"] = result.service_latency_ms
            window.append_log("大模型连接正常")
            bridge.set_model_status("ready", result.service_latency_ms)
        else:
            model_state["status"] = "服务正常"
            model_state["oc_ms"] = result.service_latency_ms
            window.append_log("OpenChamber 服务正常，但当前会话模型配置不可用")
            bridge.set_model_status("service_only", result.service_latency_ms)

    def on_save_model_address(text: str) -> None:
        # 保存本地模型地址（重启生效）；非法输入/写盘失败不覆盖有效配置、不虚报成功。
        # 提示统一走 set_model_notice：成功且与本次生效地址一致（含规范化等价，如 A→B→A）时
        # 清除过期“重启后生效”提示，避免磁盘已回到生效地址却仍提示旧地址。
        try:
            saved = save_model_base_url(text)
        except OSError as exc:
            window.set_model_notice(f"地址保存失败：{exc}")
            window.append_log(f"地址保存失败：{exc}（保持当前有效配置 {window.current_model_base_url()}）")
            return
        if saved is None:
            window.set_model_notice("地址无效，未覆盖当前有效配置")
            window.append_log("地址无效，未覆盖当前有效配置")
            return
        if saved == window.current_model_base_url():
            window.set_model_notice("")  # 磁盘与本次生效地址一致 → 无需重启，清掉过期提示
            window.append_log(f"地址已保存，与本次启动生效地址一致，无需重启：{saved}")
        else:
            window.set_model_pending(saved)
            window.append_log(
                f"地址已保存，重启后生效；本次启动生效地址：{window.current_model_base_url()}"
            )

    def _immediate_wrap_copy(source_text: str, origin: str) -> None:
        """阶段 4A：立即套正式回包格式并写本机剪贴板（发送/包装内容两入口共用）。

        顺序：两个人工入口均不判断自动任务/回传单槽 → 剪贴板可用性
        → 读源文本（剪贴板入口才读；手动入口用输入框正文）
        → 空文本不写 → 纯封装（复用 wrap_auto_response：独立 manual 事件 ID +
        默认轮次 0/3，不冒用自动任务身份）→ 恰一次 clipboard.setText。
        全程 0 HTTP、不调 controller.manual_send、不 ack/finish、不改监听状态
        与自动队列。成功只称“已包装并复制到剪贴板”（本机写入成功，不宣称 A 已
        收到）；读取/包装/写入失败均有当前页可见提示，不虚报成功。
        """
        if clipboard is None:
            window.set_current_task("操作失败：剪贴板不可用")
            window.append_log("操作失败：剪贴板不可用")
            return
        if origin == "剪贴板":
            try:
                source_text = clipboard.text() or ""
            except Exception as exc:
                window.set_current_task(f"操作失败：读取剪贴板失败：{exc}")
                window.append_log(f"操作失败：读取剪贴板失败：{exc}")
                return
        if not isinstance(source_text, str) or not source_text.strip():
            message = "剪贴板无文本，未包装" if origin == "剪贴板" else "输入内容为空，未包装"
            window.set_current_task(message)
            window.append_log(message)
            return
        wrapped = wrap_auto_response(source_text, f"manual-{uuid.uuid4().hex}", 0, 3)
        if wrapped is None:
            window.set_current_task("包装失败：内容不适合正式回包格式")
            window.append_log("包装失败：内容不适合正式回包格式")
            return
        try:
            clipboard.setText(wrapped)
        except Exception as exc:
            window.set_current_task(f"写入剪贴板失败：{exc}")
            window.append_log(f"写入剪贴板失败：{exc}")
            return
        window.set_current_task("已包装并复制到剪贴板")
        window.append_log("已包装并复制到剪贴板")

    def on_manual_send(text: str) -> None:
        # 阶段 4A：手动输入框正文 → 立即套正式回包格式 → 写本机剪贴板（恰一次）。
        # 不再调用 controller.manual_send、不请求 OpenChamber/模型、不发 HTTP；
        # 不把剪贴板当输入框；输入框保留原文。
        _immediate_wrap_copy(text, "手动输入框")

    def on_wrap_clipboard() -> None:
        # 阶段 4A：读当前剪贴板文本 → 同一正式包装 → 写回剪贴板（恰一次，不弹窗）。
        _immediate_wrap_copy("", "剪贴板")

    def on_compact() -> None:
        result = controller.compact_current_session()
        if result.success:
            window.append_log("已提交当前会话压缩")
        else:
            window.append_log(f"压缩失败：{result.error}")

    def on_listening(enabled: bool) -> None:
        bridge.set_listening(enabled)
        window.append_log("自动监听已开始" if enabled else "自动监听已停止")

    def on_remote_task(task) -> str:
        # A端任务到达（Bridge 入站 FIFO 在 GUI 线程投递）。返回值契约：
        # "submitted"=已过提交点（monitor 命令入队，保持 Bridge 在途等 worker 回执）；
        # "ignored"=终态非任务（COMPLETE/普通文本/空白包），Bridge 记 consumed。
        # 提交点前的异常（3B 判定/清首响应）→ 抛给 Bridge 回队首下轮重试；
        # 提交点后的 UI 异常 → 本地日志收敛，不重投，保持在途等回执。
        if monitor is None:
            return "ignored"
        # A-REPLY-WRAPPER-FIX1: 先做 envelope extraction，从任意外层文本中
        # 提取 AI_RELAY_BEGIN/END 包；外层 ChatGPT 包装/杂文本全部忽略。
        payload = extract_envelope(task.text)
        if payload is None:
            # 无完整包（含两类半包）：仅严格裸文本 AI_RELAY_COMPLETE 联动停止；
            # 其余普通剪贴板文本不是任务 → 直接忽略，不 submit、不清首响应、
            # 不改当前任务/结果/监听状态、不写剪切板（只记简短通用日志）。
            if task.text == "AI_RELAY_COMPLETE":
                window.set_current_task("项目已完成，自动联动已停止")
                window.append_log("收到 AI_RELAY_COMPLETE，自动联动已停止")
                window.set_listening(False)
            else:
                window.append_log("已忽略普通剪贴板内容（非完整 AI_RELAY 包）")
            return "ignored"
        # 交付1: 严格识别完整包内 AI_RELAY_COMPLETE（包装之前，绝不发给 OpenChamber）。
        # 只停 inbound 监听（保留等待任务，重新监听后继续投递）；不 stop monitor 线程 /
        # 不清 Controller 任务 / 不关 ClipLink / 不中断活动任务的结果回传。
        if payload.strip() == "AI_RELAY_COMPLETE":
            window.set_current_task("项目已完成，自动联动已停止")
            window.append_log("收到 AI_RELAY_COMPLETE，自动联动已停止")
            window.set_listening(False)
            return "ignored"
        # 空白包（BEGIN/END 间无有效内容）不是任务 → 忽略，不改任何状态。
        if not payload.strip():
            window.append_log("剪贴板包为空，已忽略")
            return "ignored"
        if envelope_route(payload) != ("CHATGPT", "EXECUTOR", "TASK"):
            if envelope_route(payload) == ("EXECUTOR", "CHATGPT", "RESPONSE"):
                window.append_log("已忽略 B端回传的结果，避免重复执行")
            else:
                window.append_log("已忽略非 A→B 任务包")
            return "ignored"
        if is_nested_return(payload):
            window.append_log("已忽略被 A端再次包装的 B端回复")
            return "ignored"
        protocol_text = payload
        # 交付3: 普通新任务 → 清旧首响应（内部数据 + Bridge + 隐藏显示；连接显示归 probe 管）
        state = window._model_state
        state["first_response_ms"] = None
        bridge.set_model_first_response(None)
        window.set_model_first_response_display(None)
        # 提交点：monitor 命令入队即接管成立（入队不会抛错）；attempt 透传内部
        # 投递轮次，worker 回执按 (event_id, attempt) 匹配对应轮，防旧回执误配。
        # submit 之前入 _cmd 不是接管成功；真正的接管由 worker accepted 回执确认。
        monitor.submit_remote_task(task.event_id, protocol_text, "{content}",
                                   bridge.inflight_attempt)
        try:
            if getattr(window, "_busy_waiting_event_id", None) != task.event_id:
                window.set_current_task("已收到A端任务，正在处理")
                window.append_log("收到 A端任务，正在后台处理")
        except Exception as exc:
            # 提交点之后的 UI 异常：本地日志收敛，不重投、不丢在途，等 worker 回执
            try:
                window.append_log(f"任务接管后界面刷新异常（不影响接管）：{exc}")
            except Exception:
                pass
        return "submitted"

    def on_auto_compact(enabled: bool) -> None:
        # GUI 线程：只让 monitor 入队更新运行时开关（0 HTTP）；monitor=None 时不崩。
        if monitor is not None:
            monitor.set_auto_compact(enabled)
        window.append_log("自动压缩已开启" if enabled else "自动压缩已关闭")

    def on_local_write(event_id: str | None) -> None:
        # 本机剪贴板写入成功（同步在线写或恢复在线后 flush 成功）→ 唯一的自动任务
        # ack 路径：按 event_id 严格匹配当前活动任务才释放；无关联/迟到/错 id 通知
        # 绝不释放错误任务。只证明 B 端本机写入，不证明 ClipLink 采样或 A 端收到。
        if not event_id or monitor is None:
            return
        task = getattr(controller, "_auto_task", None)
        if task is None or getattr(task, "event_id", None) != event_id:
            window.append_log("本机写入完成通知与活动任务不符，未释放任务")
            return
        monitor.acknowledge_result(event_id)
        window.set_current_task("自动任务已完成，结果已进入回传链路")
        window.append_log("本机剪贴板写入成功，自动任务已释放")

    bridge.on_remote_task = on_remote_task
    bridge.on_local_write = on_local_write

    window.refresh_session_requested.connect(on_refresh_session)
    window.test_model_requested.connect(on_test_model)
    window.manual_send_requested.connect(on_manual_send)
    window.wrap_clipboard_requested.connect(on_wrap_clipboard)
    window.compact_requested.connect(on_compact)
    window.listening_changed.connect(on_listening)
    window.auto_compact_changed.connect(on_auto_compact)
    window.model_address_changed.connect(on_save_model_address)


def initialize_startup(window: MainWindow) -> None:
    window.refresh_session_requested.emit()
    window.set_listening(True)
    window.set_auto_compact(True)

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


def apply_model_probe(window: MainWindow, worker) -> None:
    """GUI 线程：只读 probe worker 的最近结果刷新“大模型连接”卡，绝不调 probe/HTTP。

    未产出过结果（worker 尚未探完第一轮）→ 保持原样（初始“未检测”）。
    断开原因只在“已连接→断开”跳变时记一次日志，避免周期轮询刷屏。
    """
    result = worker.last_result()
    if result is None:
        return
    previous = window._llm_status.text()
    if result.skipped:
        window.set_model_connection("非本地模型（免检测）")
        return
    if result.connected:
        window.set_model_connection("已连接", latency_ms=result.latency_ms)
    else:
        window.set_model_connection("未连接", latency_ms=result.latency_ms)
        if "已连接" in previous:
            window.append_log("模型服务断开" + (f"（{result.error}）" if result.error else ""))


def start_model_poll(window: MainWindow, worker, interval_ms: int = 5000) -> QTimer:
    """定时读取 probe worker 结果（GUI 线程 QTimer，不调 probe/同步 HTTP/wait_for_result）。

    首帧立即读一次；计时器挂到 window 下随窗口退出停止。
    """
    timer = QTimer(window)
    timer.setInterval(interval_ms)
    timer.timeout.connect(lambda: apply_model_probe(window, worker))
    timer.start()
    apply_model_probe(window, worker)
    return timer


def start_bridge_poll(window: MainWindow, bridge: ClipLinkBridge, interval_ms: int = 400) -> QTimer:
    """定时驱动 ClipLinkBridge：入站队列投递（sink 为主事件源）+ flush pending + 写状态文件。

    首帧立即 tick 一次，之后每 interval_ms 一轮；计时器挂到 window 下随窗口存活。
    """
    timer = QTimer(window)
    timer.setInterval(interval_ms)
    last_waiting = 0

    def tick_and_show_waiting() -> None:
        nonlocal last_waiting
        bridge.tick()
        waiting = bridge.queue_size()
        if waiting and not window._listening and waiting != last_waiting:
            message = f"已收到 A端消息，自动监听未开启（等待 {waiting} 条）；点击“开始监听”后处理"
            window.set_current_task(message)
            window.append_log(message)
        last_waiting = waiting

    timer.timeout.connect(tick_and_show_waiting)
    timer.start()
    tick_and_show_waiting()
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
    if t == "previous_turn_superseded":
        window.append_log("上一轮未形成可回传结果，新一轮到达后已释放旧状态")
    elif t == "intake_ready":
        window._busy_waiting_event_id = None
        window.set_current_task("已包装，等待大模型恢复")
        window.append_log("收到 A端新任务，已完成包装，等待大模型")
        # 接管回执：accepted → 释放 Bridge 在途槽并记 consumed（绝不释放活动任务）
        bridge.resolve_inflight(ev.get("event_id"), True, ev.get("attempt"))
    elif t == "intake_running":
        window._busy_waiting_event_id = None
        window.set_current_task("已提交到大模型，等待执行")
        window.append_log("收到 A端新任务，已包装并提交到当前会话")
        bridge.resolve_inflight(ev.get("event_id"), True, ev.get("attempt"))
    elif t == "intake_unknown":
        window.set_current_task("任务提交状态不确定，请在 OpenChamber 核验；不会自动重试")
        window.append_log("A端任务提交状态不确定，已暂停自动重试，避免重复执行")
        bridge.resolve_inflight(ev.get("event_id"), True, ev.get("attempt"))
    elif t == "intake_busy":
        busy_event_id = ev.get("event_id") or "<unknown>"
        if getattr(window, "_busy_waiting_event_id", None) != busy_event_id:
            window._busy_waiting_event_id = busy_event_id
            window.set_current_task("已有自动任务等待处理；A端新消息已排队，尚未执行")
            window.append_log("自动任务未接收：已有任务正在处理，新消息排队等待")
        # busy → 回队首（不标 consumed、不丢正文），下一轮 tick 继续从队首投递
        bridge.resolve_inflight(ev.get("event_id"), False, ev.get("attempt"))
    elif t == "sent":
        window.set_current_task("已提交到大模型，等待执行")
    elif t == "submit_confirmed":
        window.set_current_task("已核对到原任务，正在等待结果")
        window.append_log("已在原会话核对到提交的任务，继续自动处理（未重复发送）")
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
        # 首响应只写内部状态 + Bridge（status.json 同一真实测量值）+ 隐藏显示；
        # 绝不把“已连接/未连接 + 服务延迟”的 probe 显示覆盖成复杂状态。
        model_state["first_response_ms"] = ev.get("first_response_ms")
        bridge.set_model_first_response(ev.get("first_response_ms"))
        window.set_model_first_response_display(ev.get("first_response_ms"))
    elif t == "result_complete":
        # 严格校验在前：非字符串结果不交 Qt；身份对象先验类型再访问属性；
        # 异常只收敛到本事件（局部 try），不吞同批后续正常事件。
        text = ev.get("text")
        identity = ev.get("identity")
        identity_ok = is_usable_identity(identity)
        text_ok = isinstance(text, str)
        wrapped = None
        try:
            if text_ok:
                window.set_recent_result(text)  # 安全字符串原文逐字展示
            if identity_ok and text_ok and text.strip():
                # 只用任务到达时冻结的原 TASK_ID/ROUND/MAX_ROUNDS；封装成功才交付
                wrapped = wrap_auto_response(
                    text, identity.task_id, identity.round_number, identity.max_rounds
                )
        except Exception as exc:  # 局部收敛：零 deliver/ack，后续事件仍处理
            wrapped = None
            window.append_log(f"结果回包处理异常：{exc}")
        if wrapped is not None:
            # 先交 Bridge 回传单槽（flushed/pending/rejected/duplicate 是不同事实）；
            # 绝不在“已接管 pending”时 ack——释放只由 on_local_write 在本机写入
            # 成功后经唯一 ack 路径完成（同步在线写或恢复后 flush 成功）。
            outcome = bridge.deliver_result(wrapped, event_id=ev.get("event_id"))
            if outcome == "flushed":
                window.set_current_task("自动任务已完成，结果已进入回传链路")
                window.append_log("大模型任务完成")
            elif outcome == "pending":
                window.set_current_task("结果已就绪，等待A端在线后写回本机剪贴板")
                window.append_log("大模型任务完成，结果暂存待回传（A端未在线，任务保持占用）")
            else:
                window.set_current_task("结果已就绪，回传单槽被占用，未回传")
                window.append_log(
                    f"结果未自动回传：回传单槽被占用（{outcome}），任务保持占用"
                )
        else:
            # 身份无效/缺失/字段非法/结果空/非字符串/封装失败：零 deliver/零 ack
            # （任务保持占用），原始结果仍可查看，给可见状态与简短原因
            if identity is None:
                reason = "原任务身份缺失（无冻结身份快照）"
            elif not identity_ok:
                reason = f"原任务身份无效：{getattr(identity, 'error', None) or '类型或字段非法'}"
            elif not text_ok:
                reason = "结果正文不是字符串，未展示未回传"
            elif not text.strip():
                reason = "结果正文为空"
            else:
                reason = "结果回包封装失败"
            window.set_current_task("结果已就绪，但原任务身份无效，未自动回传")
            window.append_log(f"结果未自动回传：{reason}")
    elif t == "result_ambiguous":
        window.set_current_task("结果识别存在冲突，已暂停自动回传")
        window.append_log("检测到同一会话存在额外用户消息，未自动回传")
    elif t == "result_interrupted":
        window.set_current_task("非本地模型执行中断，未自动续接；请在 OpenChamber 核验")
        window.append_log("非本地模型执行中断，未自动续接，任务保持占用")
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
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("AI Relay B Lite")

    # 本次启动的模型监测地址也是自动任务判定本地模型的唯一地址依据。
    # 保存新地址后重启生效；自动任务业务仍连接本机 OpenChamber API。
    model_base_url = load_model_base_url()
    window = MainWindow()
    window.set_model_base_url(model_base_url)
    controller = LiteController(base_url=OPENCHAMBER_BASE_URL, model_base_url=model_base_url)
    probe_controller = LiteController(base_url=OPENCHAMBER_BASE_URL, model_base_url=model_base_url)
    model_probe_worker = ModelProbeWorker(
        model_base_url, target_fn=probe_controller.get_current_model_target
    )
    clipboard = app.clipboard()

    # 入站 FIFO 启动顺序（阶段 3F-2）：建 Bridge（恢复 consumed 状态 + 导入一次
    # 未消费旧快照）→ 完成 UI/monitor 接线（注册 on_remote_task）→ 注册 sink 后
    # 才 client.start()，保证新帧一律经 sink 接管、旧快照先于新帧，无接线竞态窗口。
    bridge = ClipLinkBridge(
        clipboard_writer=lambda text: clipboard.setText(text),
    )
    monitor = AutoMonitor(controller, interval_seconds=1.0)
    wire_ui(window, controller, bridge, monitor, clipboard=clipboard)
    initialize_startup(window)

    # 启动内置 ClipLink TCP 客户端（B 端监听，等 A 连入）
    zt_ip = discover_zerotier_ip()
    clip_client: ClipLinkClient | None = None
    if not zt_ip:
        print("未检测到 ZeroTier IP，ClipLink 网络层未启动（A 端无法连接）")
    else:
        clip_client = ClipLinkClient(
            listen_ip=zt_ip,
            port=45888,
            clipboard_getter=lambda: clipboard.text() or None,
            clipboard_setter=lambda text: clipboard.setText(text),
        )
        clip_client.set_inbound_sink(bridge.enqueue_remote_task)  # sink 先于 start 注册
        clip_client.start()

    # GUI 主线程定时驱动：剪贴板线程安全桥接
    if clip_client is not None:
        clip_poll_timer = QTimer(window)
        clip_poll_timer.setInterval(300)
        clip_poll_timer.timeout.connect(clip_client.gui_poll)
        clip_poll_timer.start()

    start_cliplink_poll(window)
    start_bridge_poll(window, bridge)
    start_monitor_poll(window, monitor, bridge)
    start_model_poll(window, model_probe_worker)
    model_probe_worker.start()  # 启动即运行后台探测；start() 幂等，重复调用不会双线程
    monitor.start()
    app.aboutToQuit.connect(monitor.stop)
    app.aboutToQuit.connect(model_probe_worker.stop)
    if clip_client is not None:
        app.aboutToQuit.connect(clip_client.stop)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
