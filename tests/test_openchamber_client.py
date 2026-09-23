"""openchamber_client.probe() 单元测试（假接口，不打真实服务）。

覆盖：probe success / latency>=0 / connection failure / timeout /
HTTP error / malformed response / 任何情况下不抛异常。
"""

import io
import json
import socket
import urllib.error

import pytest

from openchamber_client import (
    OpenChamberClient,
    ProbeResult,
    is_loopback_base_url,
    resolve_local_token,
)


class FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict | None = None) -> None:
        self.status = status
        self.body = body
        self.headers = headers or {"Content-Type": "application/json"}

    def read(self, size: int = -1) -> bytes:
        return self.body if size is None or size < 0 else self.body[:size]

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> None:
        return None


def make_client(monkeypatch, tmp_path, **kwargs) -> OpenChamberClient:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"desktopLocalClientToken": "tok-123"}), encoding="utf-8")
    kwargs.setdefault("settings_path", settings)
    kwargs.setdefault("token", None)
    return OpenChamberClient("http://127.0.0.1:57123", **kwargs)


def test_probe_success(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=None):
        assert request.full_url == "http://127.0.0.1:57123/health"
        assert request.get_header("Authorization") == "Bearer tok-123"
        return FakeResponse(200, json.dumps({"status": "ok", "openchamberVersion": "1.24.2"}).encode())

    monkeypatch.setattr("openchamber_client.urllib.request.urlopen", fake_urlopen)
    client = make_client(monkeypatch, tmp_path)
    result = client.probe()
    assert isinstance(result, ProbeResult)
    assert result.connected is True
    assert result.error is None
    assert result.latency_ms is not None
    assert result.latency_ms >= 0


def test_probe_connection_refused(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr("openchamber_client.urllib.request.urlopen", fake_urlopen)
    result = make_client(monkeypatch, tmp_path).probe()
    assert result.connected is False
    assert result.error.startswith("connection:")
    assert result.latency_ms >= 0


def test_probe_timeout_not_wrapped(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=None):
        raise socket.timeout("timed out")

    monkeypatch.setattr("openchamber_client.urllib.request.urlopen", fake_urlopen)
    result = make_client(monkeypatch, tmp_path).probe()
    assert result.connected is False
    assert result.error == "timeout"
    assert result.latency_ms >= 0


def test_probe_timeout_wrapped_in_urlerror(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(socket.timeout("timed out"))

    monkeypatch.setattr("openchamber_client.urllib.request.urlopen", fake_urlopen)
    result = make_client(monkeypatch, tmp_path).probe()
    assert result.connected is False
    assert result.error == "timeout"


def test_probe_http_error(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            "http://127.0.0.1:57123/health", 404, "not found", {}, io.BytesIO(b"")
        )

    monkeypatch.setattr("openchamber_client.urllib.request.urlopen", fake_urlopen)
    result = make_client(monkeypatch, tmp_path).probe()
    assert result.connected is False
    assert result.error == "http: 404"


def test_probe_malformed_json(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "openchamber_client.urllib.request.urlopen",
        lambda request, timeout=None: FakeResponse(200, b"not json at all"),
    )
    result = make_client(monkeypatch, tmp_path).probe()
    assert result.connected is False
    assert result.error.startswith("malformed:")


def test_probe_malformed_non_object(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "openchamber_client.urllib.request.urlopen",
        lambda request, timeout=None: FakeResponse(200, b"[1, 2, 3]"),
    )
    result = make_client(monkeypatch, tmp_path).probe()
    assert result.connected is False
    assert result.error.startswith("malformed:")


def test_probe_no_token_still_succeeds(monkeypatch, tmp_path):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["auth"] = request.get_header("Authorization")
        return FakeResponse(200, b'{"status": "ok"}')

    monkeypatch.setattr("openchamber_client.urllib.request.urlopen", fake_urlopen)
    # 无 settings 文件、无显式 token → 不带 Authorization 头
    client = OpenChamberClient("http://127.0.0.1:57123", settings_path=tmp_path / "missing.json")
    result = client.probe()
    assert result.connected is True
    assert seen["auth"] is None


def test_non_loopback_does_not_read_settings(monkeypatch, tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"desktopLocalClientToken": "tok-123"}), encoding="utf-8")
    assert resolve_local_token("http://10.0.0.5:57123", settings_path=settings) is None
    client = OpenChamberClient("http://10.0.0.5:57123", settings_path=settings)
    assert client._token is None


def test_base_url_validation():
    with pytest.raises(ValueError):
        OpenChamberClient("ftp://127.0.0.1")
    assert is_loopback_base_url("http://localhost:57123/")
    assert not is_loopback_base_url("http://10.0.0.5:57123")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))