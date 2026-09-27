"""ClipLink 远程剪贴板事件桥：A→B 入站 FIFO 队列（sink 为主事件源、启动快照导入一次）、
B→A 结果回传（单槽 pending）、AIRelayLite 状态文件。

入站契约（阶段 3F-2）：
- Bridge 是唯一入站队列 owner；client 收帧构造不可变 RemoteTask 后经
  enqueue_remote_task 同步接管（网络线程 → 锁+deque，不碰 Qt）。
- 至多一项在途（_in_flight 保留完整正文）：queued → in_flight →
  accepted/ignored 删除；busy 回队首（不标 consumed、不丢正文、队首不可被越过）。
- consumed 只在终态标记：worker accepted 回执或 on_remote_task 返回 "ignored"。
- 文件快照只在启动时导入一次（__init__ → import_snapshot），生产实时不再每 tick
  从文件注入（避免陈旧文件重放）；poll() 保留为兼容/显式快照读取。
- 既有 COMPLETE 控制事件（is_control_event）旁路在途闸门，不被 busy 普通任务长期挡住。
- 开始监听清空等待队列（2026-09-27 用户裁决，总纲 §25）：set_listening(True) 时
  开始前（含启动快照导入）已积压的任务全部作废、不再投递；仅处理监听开启后
  新到达的 A 端任务。停止监听（False）不清队列。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from cliplink_status import is_stale, now_millis, read_cliplink_status

log = logging.getLogger("cliplink_bridge")

AI_RELAY_BEGIN_MARKER = "----- AI_RELAY_BEGIN -----"
AI_RELAY_END_MARKER = "----- AI_RELAY_END -----"
AI_RELAY_V1_MARKER = "AI_RELAY/1"


def extract_envelope(text: str) -> str | None:
    """从任意外层文本中提取 A→B 协议包内容。

    找第一个 AI_RELAY_BEGIN 标记及其配对的 AI_RELAY_END 标记，
    返回两者之间的内容（不含外层标记）；BEGIN 前 / END 后的杂文本忽略。
    嵌套协议包保留完整内容；任何一层缺少 END 均不视作完整任务。
    """
    if text.lstrip().startswith(AI_RELAY_V1_MARKER):
        text = AI_RELAY_BEGIN_MARKER + "\n" + text.lstrip()
    begin = text.find(AI_RELAY_BEGIN_MARKER)
    if begin == -1:
        return None
    depth = 1
    position = begin + len(AI_RELAY_BEGIN_MARKER)
    while depth:
        next_begin = text.find(AI_RELAY_BEGIN_MARKER, position)
        next_end = text.find(AI_RELAY_END_MARKER, position)
        if next_end == -1:
            return None
        if next_begin != -1 and next_begin < next_end:
            depth += 1
            position = next_begin + len(AI_RELAY_BEGIN_MARKER)
        else:
            depth -= 1
            if depth == 0:
                return text[begin + len(AI_RELAY_BEGIN_MARKER):next_end]
            position = next_end + len(AI_RELAY_END_MARKER)

def envelope_route(payload: str) -> tuple[str, str, str]:
    headers = {}
    for line in payload.splitlines():
        if not line.strip() and headers:
            break
        key, separator, value = line.partition(":")
        if key.strip().upper() == "CONTENT":
            break
        if separator:
            headers[key.strip().upper()] = value.strip().upper()
    return (headers.get("SOURCE", ""), headers.get("TARGET", ""), headers.get("TYPE", ""))

def is_nested_return(payload: str) -> bool:
    content = payload.partition("CONTENT:")[2].strip()
    if content.startswith(AI_RELAY_V1_MARKER):
        return is_return_envelope(content)
    if not content.startswith(AI_RELAY_BEGIN_MARKER) or not content.endswith(AI_RELAY_END_MARKER):
        return False
    nested = extract_envelope(content)
    return nested is not None and is_return_envelope(nested)

def is_return_envelope(payload: str) -> bool:
    return envelope_route(payload) == ("EXECUTOR", "CHATGPT", "RESPONSE")

def task_fingerprint(text: str) -> str | None:
    payload = extract_envelope(text)
    if payload is None:
        return None
    headers = {}
    lines = payload.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for index, line in enumerate(lines):
        key, separator, value = line.partition(":")
        if (not line.strip() or line.strip() == AI_RELAY_V1_MARKER) and not headers:
            continue
        if not separator:
            return None
        name = key.strip().upper()
        if name == "CONTENT":
            body = "\n".join(lines[index + 1:]).strip()
            break
        if name in headers:
            return None
        headers[name] = value.strip()
    else:
        return None
    if (headers.get("SOURCE", "").upper() != "CHATGPT"
            or headers.get("TARGET", "").upper() != "EXECUTOR"
            or headers.get("TYPE", "").upper() != "TASK"
            or not headers.get("TASK_ID") or not body):
        return None
    try:
        round_number = int(headers.get("ROUND", "0"))
    except ValueError:
        return None
    if round_number < 0:
        return None
    key = json.dumps([headers["TASK_ID"], round_number, body], ensure_ascii=False)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def is_control_event(task: RemoteTask) -> bool:
    """既有 COMPLETE 控制事件判定：裸文本严格相等，或完整包内严格 COMPLETE。

    只覆盖既有 3B 门控识别的两类合法停止监听事件，不新扩信任/协议；
    普通任务（含半包/空白包/普通文本）不旁路在途闸门，按 FIFO。
    """
    if task.text == "AI_RELAY_COMPLETE":
        return True
    payload = extract_envelope(task.text)
    return payload is not None and payload.strip() == "AI_RELAY_COMPLETE"


@dataclass(frozen=True, slots=True)
class RemoteTask:
    event_id: str
    text: str
    content_hash: str
    updated_at: int


def default_remote_event_path() -> Path:
    base = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    return Path(base) / "ClipLink" / "remote_clipboard.json"


def default_status_file_path() -> Path:
    base = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    return Path(base) / "AIRelayLite" / "status.json"


def _restore_consumed_event_id(status_file: Path) -> str | None:
    """从状态文件恢复 last_consumed_event_id：文件缺失 / 损坏 / 非 str → None（按未消费处理）。

    B 端重启后：已消费事件绝不重复执行；未消费事件重启时导入队列，但（自动）
    开始监听时随等待队列作废、不执行（总纲 §25）。
    """
    try:
        obj = json.loads(status_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    value = obj.get("last_consumed_event_id")
    return value if isinstance(value, str) and value else None

def _restore_task_fingerprints(status_file: Path) -> list[str]:
    try:
        obj = json.loads(status_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    values = obj.get("accepted_task_fingerprints") if isinstance(obj, dict) else None
    if not isinstance(values, list):
        return []
    return [value for value in values[-128:] if isinstance(value, str) and len(value) == 64]


class ClipLinkBridge:
    _STATUS_HEARTBEAT_MS = 2000

    def __init__(
        self,
        remote_event_path=None,
        cliplink_status_path=None,
        status_file_path=None,
        clipboard_writer=None,
    ) -> None:
        self._remote_path = Path(remote_event_path) if remote_event_path else default_remote_event_path()
        self._cliplink_status_path = cliplink_status_path
        self._status_file = Path(status_file_path) if status_file_path else default_status_file_path()
        self._clipboard_writer = clipboard_writer
        self._listening = False
        self._last_consumed_event_id: str | None = _restore_consumed_event_id(self._status_file)
        self._accepted_task_fingerprints = _restore_task_fingerprints(self._status_file)
        self._pending_task_fingerprints: set[str] = set()
        # 入站 FIFO（阶段 3F-2）：_queue_lock 仅保护队列/在途状态；
        # 锁内只做 deque 操作，绝不持锁调回调（回调在锁外执行）。
        self._queue_lock = threading.Lock()
        self._queue: deque[RemoteTask] = deque()
        self._in_flight: RemoteTask | None = None  # 至多一项在途（保留完整正文）
        self._attempt = 0  # 单调递增投递轮次计数（旧回执防误配令牌；不清零）
        self._snapshot_imported = False  # 启动快照只导入一次
        self._pending_result: str | None = None
        self._pending_event_id: str | None = None
        self._relay_status = "idle"
        self._model_status = "unknown"
        self._session_id: str | None = None
        self._openchamber_latency_ms: int | None = None
        self._model_first_response_ms: int | None = None
        self._last_status_write_ms = 0
        self.on_remote_task = None
        # GUI 回调：本机剪贴板写入成功后调用一次（参数=该条结果的关联 event_id，可能 None）；
        # 仅证明 B 端本机写入成功，不证明 ClipLink 采样/发送或 A 端收到。
        self.on_local_write = None
        # 启动即导入一次未消费旧快照（须在 client.start() 前建好 Bridge，见 main.py 启动顺序）
        self.import_snapshot()

    # ── A→B inbound ──────────────────────────────────────────────

    def _read_remote_event(self) -> RemoteTask | None:
        try:
            raw = self._remote_path.read_text(encoding="utf-8-sig")
        except OSError:
            return None
        try:
            obj = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        event_id = obj.get("event_id")
        text = obj.get("text")
        content_hash = obj.get("content_hash")
        updated_at = obj.get("updated_at")
        if not isinstance(event_id, str) or not event_id:
            return None
        if not isinstance(text, str):
            return None
        if not isinstance(content_hash, str):
            return None
        if not isinstance(updated_at, (int, float)):
            return None
        return RemoteTask(event_id, text, content_hash, int(updated_at))

    def set_listening(self, enabled: bool) -> int:
        """切换监听开关；开始时清空开始前积压的等待队列（总纲 §25 新语义）。

        点击“开始监听”之前收到的任务（含启动快照导入）全部作废、不再执行；
        仅处理监听开启后新到达的 A 端任务。停止监听（enabled=False）不清队列。
        返回作废的等待条数（停止时恒为 0）；不影响在途任务。
        """
        self._listening = enabled
        dropped = 0
        if enabled:
            with self._queue_lock:
                dropped = len(self._queue)
                for task in self._queue:
                    # 作废任务不会再投递，其 pending 指纹同步移除：A 端重发相同
                    # 内容时可作为新任务再执行（不加入 accepted，绝不视为已执行过）
                    fingerprint = task_fingerprint(task.text)
                    if fingerprint is not None:
                        self._pending_task_fingerprints.discard(fingerprint)
                self._queue.clear()
        self._refresh_relay_status()
        self.write_status_file()
        return dropped

    def poll(self) -> RemoteTask | None:
        """兼容/显式快照读取：读当前快照文件，返回未消费事件（纯检测，无副作用）。

        阶段 3F-2 起生产实时入站以 sink 队列为主事件源，tick 不再调用本方法；
        保留仅供兼容与显式快照读取，不作为运行时第二事件源。
        consumed 只由终态（worker accepted 回执 / on_remote_task 返回 "ignored"）标记。
        """
        if not self._listening:
            return None
        task = self._read_remote_event()
        if task is None or task.event_id == self._last_consumed_event_id:
            return None
        return task

    def _remember_consumed(self, event_id: str) -> None:
        """事件到达终态（accepted 回执 / ignored）后，记录并持久化 last_consumed_event_id。

        busy 不是终态：回队首不在此记录。持久化保证 B 重启后已消费事件（同
        event_id）绝不重复执行；未消费事件重启时导入队列，（自动）开始监听时
        作废不执行（总纲 §25）。
        """
        self._last_consumed_event_id = event_id
        self.write_status_file()

    # ── A→B 入站 FIFO（阶段 3F-2）────────────────────────────────

    def enqueue_remote_task(self, task: RemoteTask) -> None:
        """入站 sink 回调（ClipLinkClient 网络线程调用）：线程安全入队，不碰 Qt。

        锁内只做 deque.append；事件正文（完整 RemoteTask）从此由 Bridge 唯一持有。
        sink 自身抛错由调用方（client）记录可见错误，这里不吞、不重试。
        """
        with self._queue_lock:
            fingerprint = task_fingerprint(task.text)
            if (fingerprint is not None
                    and (fingerprint in self._accepted_task_fingerprints
                         or fingerprint in self._pending_task_fingerprints)):
                return
            if fingerprint is not None:
                self._pending_task_fingerprints.add(fingerprint)
            self._queue.append(task)

    def import_snapshot(self) -> None:
        """启动时导入一次未消费旧快照（至多 1 条候选）。

        只在 __init__ 执行一次：文件至多含最新 1 帧；event_id 已消费 → 跳过。
        须在 client.start() 之前完成（main.py 启动顺序），保证旧快照先于新帧入队。
        导入的候选在监听开始前入队；启动自动开始监听（initialize_startup）会将其
        随等待队列作废（总纲 §25），不投递。
        陈旧/歧义恢复不宣称可靠：单槽快照最多救回最新 1 帧，连续 sink 失败不保证。
        """
        if self._snapshot_imported:
            return
        self._snapshot_imported = True
        task = self._read_remote_event()
        if task is None or task.event_id == self._last_consumed_event_id:
            return
        with self._queue_lock:
            fingerprint = task_fingerprint(task.text)
            if fingerprint is not None:
                if fingerprint in self._accepted_task_fingerprints:
                    return
                self._pending_task_fingerprints.add(fingerprint)
            self._queue.append(task)

    @property
    def inflight_attempt(self) -> int | None:
        """当前在途项的投递轮次（on_remote_task 提交时随 submit 传给 worker）。"""
        with self._queue_lock:
            return self._attempt if self._in_flight is not None else None

    def queue_size(self) -> int:
        """等待中事件数（不含在途项），供 UI"等待 N"展示。"""
        with self._queue_lock:
            return len(self._queue)

    def resolve_inflight(self, event_id, accepted: bool, attempt: int | None = None) -> bool:
        """worker 回执匹配：accepted → 终态释放+记 consumed；busy → 回队首。

        只匹配 _in_flight 单槽：无在途 / event_id 不符（迟到/错 id）→ 忽略（防御
        守卫，不重复释放）；attempt 非 None 且与当前在途轮次不符（上一 busy 轮的
        陈旧旧回执）→ 忽略，防跨 busy 重试误配。attempt=None（无令牌直接调用）
        按 event_id 匹配，保持兼容。返回是否实际匹配处理。
        """
        with self._queue_lock:
            task = self._in_flight
            if task is None or event_id is None or task.event_id != event_id:
                return False
            if attempt is not None and attempt != self._attempt:
                return False
            self._in_flight = None  # _attempt 保持单调计数（旧轮次令牌不再有效）
            if not accepted:
                self._queue.appendleft(task)  # busy 回队首：不丢正文、不越序
            else:
                fingerprint = task_fingerprint(task.text)
                if fingerprint is not None:
                    self._pending_task_fingerprints.discard(fingerprint)
                    self._accepted_task_fingerprints.append(fingerprint)
                    self._accepted_task_fingerprints = self._accepted_task_fingerprints[-128:]
        if accepted:
            self._remember_consumed(event_id)
        return True

    def _dispatch_head(self) -> None:
        """队首投递：在途闸门（至多一项在途）+ 完整 3B 门控在 on_remote_task 内。

        回调返回值契约："submitted"=已过提交点（保持 in_flight 等 worker 回执）；
        "ignored"=终态非任务（释放 in_flight 并记 consumed）。回调异常（提交点前）
        → 回队首、下一 tick 重试，异常不吞（重新抛出）。
        """
        if self.on_remote_task is None:
            return
        with self._queue_lock:
            if self._in_flight is not None or not self._queue:
                return
            task = self._queue.popleft()
            self._in_flight = task
            self._attempt += 1
        try:
            verdict = self.on_remote_task(task)
        except Exception:
            with self._queue_lock:
                self._in_flight = None
                self._queue.appendleft(task)
            log.error("入站接管回调异常（event %s）：回队首待重试", task.event_id, exc_info=True)
            raise
        if verdict == "submitted":
            return
        with self._queue_lock:
            self._in_flight = None
            fingerprint = task_fingerprint(task.text)
            if fingerprint is not None:
                self._pending_task_fingerprints.discard(fingerprint)
        self._remember_consumed(task.event_id)

    def _dispatch_control_bypass(self) -> None:
        """既有 COMPLETE 控制事件旁路在途闸门：有在途普通任务时也立即处理（停监听）。

        只旁路已知控制事件（is_control_event），普通任务仍按 FIFO 受在途闸门约束；
        不触碰在途普通任务、不中断其结果回传、不给未接受任务自动 ack；
        控制事件不占用 in_flight 槽（同步终态），记 consumed 不污染快照恢复语义。
        """
        if self.on_remote_task is None:
            return
        with self._queue_lock:
            task = next((t for t in self._queue if is_control_event(t)), None)
            if task is not None:
                self._queue.remove(task)
        if task is None:
            return
        try:
            verdict = self.on_remote_task(task)
        except Exception:
            with self._queue_lock:
                self._queue.appendleft(task)
            log.error("入站控制事件回调异常（event %s）：回队首待重试", task.event_id, exc_info=True)
            raise
        if verdict == "submitted":
            return  # 防御：控制事件按 3B 门控必走 ignored 分支
        self._remember_consumed(task.event_id)

    # ── B→A outbound ─────────────────────────────────────────────

    def _a_available(self) -> bool:
        st = read_cliplink_status(self._cliplink_status_path)
        return st is not None and st.status == "connected" and not is_stale(st, now_millis())

    def result_slot_occupied(self) -> bool:
        """只读：结果回传单槽当前是否被占用（等待回传）。

        仅供回传状态诊断；人工包装入口不依赖此状态，不修改任何状态。
        """
        return self._pending_result is not None

    def deliver_result(self, text: str, event_id: str | None = None) -> str:
        """回传单槽占用保护：返回本次提交的可观察事实（小返回值，不静默覆盖）。

        flushed   — 本次本机剪贴板写入成功（槽已清空，已发一次本地写入完成通知）；
        pending   — 尚未写出（A 未在线 / 写失败），本条记录接管单槽等待后续 tick 重试；
        rejected  — 单槽已被另一条记录占用（不同 event_id 或冲突文本），旧记录保留；
        duplicate — 单槽为同一 event_id 的相同文本：不重写、不重复确认。
        “接管到 pending”与“本机写入成功”是两个不同事实：只有 flushed/后续 flush
        成功才发 on_local_write 通知；离线/写失败/拒绝/重复都不发（没有写入成功
        的结果不得生成通知）。
        """
        if self._pending_result is not None:
            if (
                event_id is not None
                and event_id == self._pending_event_id
                and text == self._pending_result
            ):
                return "duplicate"
            return "rejected"
        self._pending_result = text
        self._pending_event_id = event_id
        if self._a_available():
            try:
                self._clipboard_writer(text)
                self._pending_result = None
                self._pending_event_id = None
                self._notify_local_write(event_id)
                outcome = "flushed"
            except Exception:
                outcome = "pending"
        else:
            outcome = "pending"
        self._refresh_relay_status()
        self.write_status_file()
        return outcome

    def flush_pending_result(self) -> None:
        if self._pending_result is None or not self._a_available():
            return
        try:
            self._clipboard_writer(self._pending_result)
            event_id = self._pending_event_id
            self._pending_result = None
            self._pending_event_id = None
            self._notify_local_write(event_id)
        except Exception:
            # 写失败：保留完全相同的文本/TIME 及关联 event_id，下次既有 tick 重试
            pass
        self._refresh_relay_status()
        self.write_status_file()

    def _notify_local_write(self, event_id: str | None) -> None:
        if self.on_local_write is not None:
            self.on_local_write(event_id)

    # ── AIRelayLite 状态文件 ─────────────────────────────────────

    def _refresh_relay_status(self) -> None:
        if self._pending_result is not None:
            self._relay_status = "wait_return"
        elif self._listening:
            self._relay_status = "listening"
        else:
            self._relay_status = "idle"

    def set_model_status(self, status: str, latency_ms: int | None = None) -> None:
        self._model_status = status
        self._openchamber_latency_ms = latency_ms
        self.write_status_file()

    def set_model_first_response(self, latency_ms: int | None) -> None:
        """记录真实模型首响应测量值（或 None 清除），并立即写状态文件。"""
        self._model_first_response_ms = latency_ms
        self.write_status_file()

    def set_session_id(self, session_id: str | None) -> None:
        self._session_id = session_id
        self.write_status_file()

    def write_status_file(self) -> None:
        data = {
            "version": 1,
            "relay_status": self._relay_status,
            "model_status": self._model_status,
            "openchamber_latency_ms": self._openchamber_latency_ms,
            "model_first_response_ms": self._model_first_response_ms,
            "session_id": self._session_id,
            "last_consumed_event_id": self._last_consumed_event_id,
            "accepted_task_fingerprints": self._accepted_task_fingerprints,
            "updated_at": now_millis(),
        }
        try:
            self._status_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._status_file.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(self._status_file))
            self._last_status_write_ms = now_millis()
        except OSError:
            pass

    # ── QTimer tick ──────────────────────────────────────────────

    def tick(self) -> None:
        """GUI 线程每轮：控制事件旁路 → 队首投递（受监听与在途闸门）→ 回传 flush → 状态心跳。

        生产实时入站来自 sink 队列（网络线程入队），不再从快照文件注入；
        监听关闭期间队列保留（不投递）；重新开始时清空等待队列，
        仅处理之后新到达的任务（总纲 §25）。
        """
        if self._listening:
            self._dispatch_control_bypass()
            if self._listening:  # 控制事件可能刚把监听关掉
                self._dispatch_head()
        self.flush_pending_result()
        if now_millis() - self._last_status_write_ms >= self._STATUS_HEARTBEAT_MS:
            self.write_status_file()
