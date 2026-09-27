"""本地模型地址配置 + 后台连接探测（Lite）。

配置：
  - 模型地址独立于 OpenChamber 业务 API 地址。
  - 存放 %LOCALAPPDATA%/AIRelayLite/settings.json，原子写入（tmp + fsync + os.replace）。
  - 文件缺失/损坏/值非法 → 安全回退默认地址；无效保存不覆盖上一次有效配置。
探测：
  - 探测 OpenAI 兼容模型接口的 /models（永不抛异常，失败收敛到 error）。
  - ModelProbeWorker 单守护线程周期探测，GUI 主线程只读 last_result()/wait_for_result()。
  - 结果只含 已连接/未连接 + 服务延迟 + 错误原因 + 规范化地址；
    无首响应时间、无会话 ID、无执行中/恢复中等复杂状态。
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from openchamber_client import ProbeResult

DEFAULT_MODEL_BASE_URL = "http://127.0.0.1:8080/v1"
OPENCHAMBER_BASE_URL = "http://127.0.0.1:57123"
PROBE_INTERVAL_SECONDS = 5.0


def normalize_model_base_url(raw) -> str | None:
    """规范化模型 API 基地址：仅 http(s)、必须有 host、去尾部 /。

    非字符串/空/纯空白/缺 host/其它 scheme → None。路径保留（只去尾部斜杠）。
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def model_settings_path() -> Path:
    """%LOCALAPPDATA%/AIRelayLite/settings.json（本机固定，与 ClipLink 同风格）。"""
    base = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    return Path(base) / "AIRelayLite" / "settings.json"


def load_model_base_url(path: str | os.PathLike | None = None) -> str:
    """读取已保存的基地址；文件缺失/损坏/值非法 → DEFAULT_MODEL_BASE_URL。"""
    target = Path(path) if path is not None else model_settings_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_MODEL_BASE_URL
    if not isinstance(data, dict):
        return DEFAULT_MODEL_BASE_URL
    value = data.get("model_base_url")
    if value is None and "model_base_url" not in data:
        legacy = data.get("openchamber_base_url")
        value = legacy if legacy != OPENCHAMBER_BASE_URL else None
    normalized = normalize_model_base_url(value)
    return normalized if normalized is not None else DEFAULT_MODEL_BASE_URL


def save_model_base_url(raw, path: str | os.PathLike | None = None) -> str | None:
    """规范化后原子保存；raw 非法 → 返回 None 且不写文件（不覆盖上一次有效配置）。

    保留文件里已有的其它键，避免未来扩展配置项时被覆盖丢失。
    写盘失败（OSError）直接抛出，配置保持原子不变。
    """
    value = normalize_model_base_url(raw)
    if value is None:
        return None
    target = Path(path) if path is not None else model_settings_path()
    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            existing = {}
    except (OSError, ValueError):
        existing = {}
    existing["model_base_url"] = value
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp), str(target))
    return value

def probe_model_endpoint(base_url: str) -> ProbeResult:
    """只读校验 OpenAI 兼容模型列表；/health 属于 OpenChamber，不用于模型检测。"""
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(base_url + "/models", timeout=5.0) as response:
            body = response.read(65536)
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            error = "malformed: models response"
        else:
            error = None
    except urllib.error.HTTPError as exc:
        error = f"http: {exc.code}"
    except (TimeoutError, socket.timeout):
        error = "timeout"
    except urllib.error.URLError as exc:
        error = f"connection: {exc.reason}"
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        error = f"connection: {exc}"
    latency_ms = max(0, int((time.perf_counter() - started) * 1000))
    return ProbeResult(error is None, latency_ms, error)


@dataclass(frozen=True, slots=True)
class ModelProbeResult:
    """单次探测结果：已连接/未连接 + 服务延迟 + 错误原因 + 规范化地址。"""

    connected: bool
    latency_ms: int | None
    error: str | None
    base_url: str
    skipped: bool = False


class ModelProbeWorker:
    """后台周期探测 worker：单守护线程，绝不阻塞 GUI 主线程。

    probe_fn 可注入（默认 probe_model_endpoint(base_url)）。
    GUI 主线程：start() 立即返回；stop() 在有界时间（interval+1s）内返回。
    若 probe 阻塞超过该等待，旧线程可能仍存活：stop() 保留线程句柄（不谎报已停止），
    期间再次 start() 不会创建第二线程；probe 解除阻塞后旧线程观察停止请求自动退出，
    之后才能正常再次 start。每次 start() 使用本代独立停止事件，旧线程不会被新事件“复活”。
    读结果用 last_result()（非阻塞）或 wait_for_result(timeout)（带超时等待首个结果）。
    """

    def __init__(
        self,
        base_url: str,
        probe_fn=None,
        interval_seconds: float = PROBE_INTERVAL_SECONDS,
        target_fn=None,
    ) -> None:
        self._base_url = normalize_model_base_url(base_url) or DEFAULT_MODEL_BASE_URL
        if probe_fn is None:
            probe_fn = lambda: probe_model_endpoint(self._base_url)
        self._probe_fn = probe_fn
        self._target_fn = target_fn
        self._interval = float(interval_seconds)
        self._stop = threading.Event()
        self._cond = threading.Condition()
        self._last: ModelProbeResult | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        """规范化后的探测地址（输入非法时回退默认）。"""
        return self._base_url

    def run_once(self) -> ModelProbeResult:
        """同步单次探测（主线程/单测可直接调用，无需 start()）。"""
        if self._target_fn is not None:
            target = self._target_fn()
            if target is None:
                return ModelProbeResult(False, None, "unavailable: 当前会话模型地址未确认", self._base_url)
            if not target.local:
                return ModelProbeResult(False, None, None, target.base_url or "", skipped=True)
            if not target.base_url:
                return ModelProbeResult(False, None, "unavailable: 本地模型地址不可用", self._base_url)
            probe = probe_model_endpoint(target.base_url)
            return ModelProbeResult(probe.connected, probe.latency_ms, probe.error, target.base_url)
        probe = self._probe_fn()
        return ModelProbeResult(
            probe.connected, probe.latency_ms, probe.error, self._base_url
        )

    # ---------------- 线程生命周期（与 AutoMonitor 同惯例，停止语义更严格） ----------------
    def start(self) -> None:
        # 任意一代线程仍存活（含 stop 后 probe 仍卡住的旧线程）→ 不创建第二线程，
        # 也不重置停止事件让旧线程恢复循环。
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = threading.Event()  # 每代独立停止事件
        self._thread = threading.Thread(
            target=self._run, name="model-probe", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 1.0)
        # join 超时后线程可能仍存活（probe 阻塞中）：保留句柄，
        # start() 据此如实判断并禁止第二线程；旧线程解除阻塞后自行退出。

    def _run(self) -> None:
        gen_stop = self._stop  # 捕获本代停止事件，不受后续 start() 新事件影响
        while not gen_stop.is_set():
            self._publish(self.run_once())
            gen_stop.wait(self._interval)

    def _publish(self, result: ModelProbeResult) -> None:
        with self._cond:
            self._last = result
            self._cond.notify_all()

    # ---------------- 结果读取（线程安全） ----------------
    def last_result(self) -> ModelProbeResult | None:
        """非阻塞取最近一次结果；worker 未产出过结果 → None。"""
        with self._cond:
            return self._last

    def wait_for_result(self, timeout: float | None = None) -> ModelProbeResult | None:
        """等待首个结果（或超时/一直为 None）；已有结果时立即返回。"""
        with self._cond:
            if self._last is None:
                self._cond.wait(timeout)
            return self._last
