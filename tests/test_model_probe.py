"""model_probe 配置与后台探测测试（全 fake probe + tmp LOCALAPPDATA，不依赖本机 OpenChamber）。

覆盖：默认地址、合法规范化、非法 scheme/缺 host/空值拒绝、原子保存重载、
损坏配置回退、无效保存不覆盖旧值、探测成功延迟、超时/连接失败、
worker 非阻塞且可安全停止。
"""

import json
import os
import threading
import time
import urllib.error

import pytest

from model_probe import (
    DEFAULT_MODEL_BASE_URL,
    OPENCHAMBER_BASE_URL,
    ModelProbeResult,
    ModelProbeWorker,
    load_model_base_url,
    model_settings_path,
    normalize_model_base_url,
    probe_model_endpoint,
    save_model_base_url,
)
from openchamber_client import ModelTarget, ProbeResult

def test_selected_model_probe_uses_current_address_only_for_local(monkeypatch):
    targets = [ModelTarget(True, "http://192.168.100.190:8080/v1"),
               ModelTarget(False, "https://api.example.com/v1"), None]
    called = []
    def fake_probe(url):
        called.append(url)
        return ProbeResult(True, 4, None)
    monkeypatch.setattr("model_probe.probe_model_endpoint", fake_probe)
    worker = ModelProbeWorker("http://127.0.0.1:65000", target_fn=lambda: targets.pop(0))
    local = worker.run_once()
    assert local.connected and local.base_url == called[0] == "http://192.168.100.190:8080/v1"
    remote = worker.run_once()
    assert remote.skipped and remote.base_url == "https://api.example.com/v1"
    unknown = worker.run_once()
    assert not unknown.connected and not unknown.skipped
    assert len(called) == 1


@pytest.fixture
def appdata(tmp_path, monkeypatch):
    """重定向 LOCALAPPDATA 到临时目录，不碰真实 %LOCALAPPDATA%。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return tmp_path


# ---------------- 默认地址与规范化 ----------------

def test_load_default_when_file_missing(appdata):
    assert load_model_base_url() == DEFAULT_MODEL_BASE_URL


def test_settings_path_under_appdata(appdata):
    assert model_settings_path() == appdata / "AIRelayLite" / "settings.json"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("http://127.0.0.1:57123", "http://127.0.0.1:57123"),
        (" http://127.0.0.1:57123/ ", "http://127.0.0.1:57123"),
        ("https://example.com", "https://example.com"),
        ("HTTPS://Example.com:8443", "https://Example.com:8443"),
        ("http://10.0.0.5:8080/a/b/", "http://10.0.0.5:8080/a/b"),
        ("http://localhost:57123//", "http://localhost:57123"),
    ],
)
def test_normalize_valid(raw, expected):
    assert normalize_model_base_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["ftp://127.0.0.1", "ws://127.0.0.1", "http://", "https://", "not-a-url", "", "   ", None, 42, []],
)
def test_normalize_rejects(raw):
    assert normalize_model_base_url(raw) is None


# ---------------- 原子保存与重载 ----------------

def test_atomic_save_and_reload(appdata):
    saved = save_model_base_url("http://10.1.1.1:9999/")
    assert saved == "http://10.1.1.1:9999"
    target = appdata / "AIRelayLite" / "settings.json"
    assert target.is_file()
    assert not target.with_suffix(".json.tmp").exists()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == {"model_base_url": "http://10.1.1.1:9999"}
    assert load_model_base_url() == "http://10.1.1.1:9999"


def test_save_preserves_other_keys(appdata):
    target = appdata / "AIRelayLite" / "settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"future_key": 1}), encoding="utf-8")
    save_model_base_url("http://10.1.1.1:9999")
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["future_key"] == 1
    assert data["model_base_url"] == "http://10.1.1.1:9999"

def test_legacy_model_address_is_read_without_changing_openchamber(appdata):
    target = model_settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"openchamber_base_url": "http://192.168.100.190:8080/v1"}), encoding="utf-8")
    assert load_model_base_url() == "http://192.168.100.190:8080/v1"
    assert OPENCHAMBER_BASE_URL == "http://127.0.0.1:57123"
    save_model_base_url("http://10.1.1.1:8080/v1")
    assert load_model_base_url() == "http://10.1.1.1:8080/v1"

def test_legacy_openchamber_default_is_not_treated_as_model(appdata):
    target = model_settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"openchamber_base_url": OPENCHAMBER_BASE_URL}), encoding="utf-8")
    assert load_model_base_url() == DEFAULT_MODEL_BASE_URL


# ---------------- 损坏配置回退默认 ----------------

@pytest.mark.parametrize(
    "content",
    ["not json {", "[1, 2]", '"plain string"', "{}", '{"openchamber_base_url": "ftp://x"}', '{"openchamber_base_url": null}'],
)
def test_corrupt_config_falls_back_default(appdata, content):
    target = appdata / "AIRelayLite" / "settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    assert load_model_base_url() == DEFAULT_MODEL_BASE_URL


# ---------------- 无效保存不覆盖旧值 ----------------

def test_invalid_save_does_not_overwrite(appdata):
    assert save_model_base_url("http://10.1.1.1:9999") == "http://10.1.1.1:9999"
    target = model_settings_path()
    before = target.read_text(encoding="utf-8")
    assert save_model_base_url("ftp://10.1.1.1") is None
    assert save_model_base_url("") is None
    assert save_model_base_url(None) is None
    assert target.read_text(encoding="utf-8") == before
    assert load_model_base_url() == "http://10.1.1.1:9999"


# ---------------- 探测结果 ----------------

def test_run_once_success_returns_latency():
    worker = ModelProbeWorker("http://127.0.0.1:57123/", probe_fn=lambda: ProbeResult(True, 12, None))
    result = worker.run_once()
    assert isinstance(result, ModelProbeResult)
    assert result.connected is True
    assert result.latency_ms == 12
    assert result.error is None
    assert result.base_url == "http://127.0.0.1:57123"


def test_run_once_timeout_returns_disconnected_with_error():
    worker = ModelProbeWorker("http://127.0.0.1:57123", probe_fn=lambda: ProbeResult(False, 7, "timeout"))
    result = worker.run_once()
    assert result.connected is False
    assert result.latency_ms == 7
    assert result.error == "timeout"
    assert result.base_url == "http://127.0.0.1:57123"


def test_run_once_connection_refused():
    worker = ModelProbeWorker("http://127.0.0.1:57123", probe_fn=lambda: ProbeResult(False, 1, "connection: [Errno 111]"))
    result = worker.run_once()
    assert result.connected is False
    assert result.error.startswith("connection:")


def test_invalid_base_url_falls_back_to_default(monkeypatch):
    seen = {}

    def fake_probe(base_url):
        seen["base_url"] = base_url
        return ProbeResult(True, 3, None)

    monkeypatch.setattr("model_probe.probe_model_endpoint", fake_probe)
    worker = ModelProbeWorker("ftp://broken")
    assert worker.base_url == DEFAULT_MODEL_BASE_URL
    result = worker.run_once()
    assert seen["base_url"] == DEFAULT_MODEL_BASE_URL
    assert result.connected is True
    assert result.base_url == DEFAULT_MODEL_BASE_URL


def test_default_probe_uses_model_endpoint(monkeypatch):
    seen = {}

    def fake_probe(base_url):
        seen["base_url"] = base_url
        return ProbeResult(True, 3, None)

    monkeypatch.setattr("model_probe.probe_model_endpoint", fake_probe)
    worker = ModelProbeWorker("http://127.0.0.1:8080/v1/")
    result = worker.run_once()
    assert seen["base_url"] == "http://127.0.0.1:8080/v1"
    assert result.connected is True

def test_model_probe_checks_models_not_openchamber_health(monkeypatch):
    seen = []

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, limit):
            return b'{"object":"list","data":[{"id":"local-model"}]}'

    def fake_urlopen(url, timeout):
        seen.append((url, timeout))
        return Response()

    monkeypatch.setattr("model_probe.urllib.request.urlopen", fake_urlopen)
    result = probe_model_endpoint("http://192.168.100.190:8080/v1")
    assert result.connected is True
    assert seen == [("http://192.168.100.190:8080/v1/models", 5.0)]

def test_model_probe_rejects_http_error_and_bad_models(monkeypatch):
    def http_error(url, timeout):
        raise urllib.error.HTTPError(url, 503, "unavailable", {}, None)

    monkeypatch.setattr("model_probe.urllib.request.urlopen", http_error)
    assert probe_model_endpoint("http://127.0.0.1:8080/v1").error == "http: 503"

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, limit):
            return b'{"unexpected":true}'

    monkeypatch.setattr("model_probe.urllib.request.urlopen", lambda url, timeout: Response())
    assert probe_model_endpoint("http://127.0.0.1:8080/v1").connected is False


# ---------------- worker 非阻塞且可停止 ----------------

def test_worker_nonblocking_and_stoppable():
    started = threading.Event()

    def probe():
        started.set()
        time.sleep(0.05)
        return ProbeResult(True, 5, None)

    worker = ModelProbeWorker("http://127.0.0.1:57123", probe_fn=probe, interval_seconds=0.02)
    began = time.perf_counter()
    worker.start()
    assert time.perf_counter() - began < 1.0  # start() 立即返回
    assert worker._thread is not None and worker._thread.is_alive()

    assert started.wait(2.0), "后台线程未在 2s 内完成首次探测"
    # 主线程非阻塞读取：探测进行中 last_result() 也立即返回
    reads_began = time.perf_counter()
    for _ in range(100):
        worker.last_result()
    assert time.perf_counter() - reads_began < 0.5

    result = worker.wait_for_result(timeout=2.0)
    assert result is not None and result.connected is True

    old = worker._thread
    worker.stop()
    old.join(timeout=2.0)
    assert not old.is_alive()  # 探测快，stop 等待窗口内线程真实退出
    assert worker._thread is old  # 句柄保留，不清空
    worker.stop()  # 重复 stop 安全


def test_wait_for_result_timeout_returns_none():
    worker = ModelProbeWorker(
        "http://127.0.0.1:57123",
        probe_fn=lambda: ProbeResult(True, 1, None),
        interval_seconds=0.02,
    )
    # 未 start：无结果产出，带超时等待返回 None
    assert worker.wait_for_result(timeout=0.05) is None
    assert worker.last_result() is None


def test_worker_stops_even_when_probe_sloppy():
    """probe 仍 sleep 时 stop() 也能在超时内收敛（探测 < join 等待，线程真实退出）。"""
    def probe():
        time.sleep(0.2)
        return ProbeResult(False, 1, "timeout")

    worker = ModelProbeWorker("http://127.0.0.1:57123", probe_fn=probe, interval_seconds=0.02)
    worker.start()
    old = worker._thread
    began = time.perf_counter()
    worker.stop()
    assert time.perf_counter() - began < 2.0
    assert not old.is_alive()  # join 窗口足够，线程真实退出而非谎报
    assert worker._thread is old  # 句柄保留


def test_stop_with_blocked_probe_keeps_handle_and_prevents_second_thread():
    """确定性生命周期：probe 阻塞超过 stop 等待 → stop 返回但句柄保留、旧线程仍 alive；
    再次 start 不创建第二线程；释放 probe 后旧线程观察停止请求真实退出；之后 start 才创建新线程。"""
    gate = threading.Event()
    entered = threading.Event()

    def probe():
        entered.set()
        gate.wait()  # 卡住，超过 stop 的 join 等待（interval 0.02 + 1.0s）
        return ProbeResult(True, 1, None)

    worker = ModelProbeWorker("http://127.0.0.1:57123", probe_fn=probe, interval_seconds=0.02)
    worker.start()
    assert entered.wait(2.0), "后台线程未在 2s 内进入阻塞 probe"
    old = worker._thread
    assert old.is_alive()

    worker.stop()  # join 超时返回；probe 仍阻塞，旧线程实际仍存活
    assert worker._thread is old  # 不谎报已停止：句柄保留
    assert old.is_alive()

    worker.start()  # 旧线程仍存活期间：不创建第二线程，不清除其停止事件
    assert worker._thread is old
    assert old.is_alive()

    gate.set()  # 释放 probe
    old.join(timeout=2.0)
    assert not old.is_alive()  # 旧线程观察停止请求并真实退出
    assert worker._thread is old

    worker.start()  # 旧线程已退出：现在才能创建新线程
    new = worker._thread
    assert new is not old
    assert new.is_alive()

    worker.stop()  # 新线程探测不阻塞，stop 收敛
    new.join(timeout=2.0)
    assert not new.is_alive()
    assert worker._thread is new


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
