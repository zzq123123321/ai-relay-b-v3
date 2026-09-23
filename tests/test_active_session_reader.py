"""active_session_reader + openchamber_client.validate_session 测试（全内存夹具，不打真实服务、不动 OpenChamber）。

覆盖：
  - 读取有效 persisted session（SST/WAL 夹具 → ActiveSession）
  - 没有存储键 → None
  - 损坏 JSON → None
  - session_id 缺失 → None
  - directory 缺失/null → ActiveSession(directory=None)（稳定不抛）
  - .log 内存表覆盖 .ldb（新→旧）；.log 删除遮蔽 .ldb 值
  - local runtime 优先 / 仅 host 时取 max(updatedAt)
  - validate_session success / not-found(404) / 连接失败
"""

import io
import json
import socket
import urllib.error

import pytest

from active_session_reader import (
    ActiveSession,
    parse_last_session_value,
    read_active_session,
    read_last_session_value,
)
from openchamber_client import OpenChamberClient


# --- 夹具 ---------------------------------------------------------------

_KEY = b"\x01oc.lastSession.v1"


def _value_bytes(session_id: str | None, directory=None, updated=10, runtime="local", raw: str | None = None) -> bytes:
    if raw is not None:
        return _KEY + raw.encode("utf-8")
    entry: dict = {"updatedAt": updated}
    if session_id is not None:
        entry["sessionId"] = session_id
    entry["directory"] = directory
    return _KEY + json.dumps({"version": 1, "runtimes": {runtime: entry}}).encode("utf-8")


def _write_dir(tmp_path, files: dict[str, bytes]):
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    return tmp_path


# --- 读取：有效值 -------------------------------------------------------


def test_read_valid_persisted_session(tmp_path):
    _write_dir(tmp_path, {"000003.ldb": b"prefix" + _value_bytes("ses_abc", "D:/work/repo", updated=7)})
    result = read_active_session(tmp_path)
    assert isinstance(result, ActiveSession)
    assert result.session_id == "ses_abc"
    assert result.directory == "D:/work/repo"
    assert result.source == "persisted-last-active"


def test_read_wal_overrides_sst(tmp_path):
    # .ldb 里是旧值 A，.log 里是更新值 B → 取 B（内存表最新）
    _write_dir(
        tmp_path,
        {
            "000010.ldb": b"x" + _value_bytes("ses_old", "D:/old", updated=1),
            "000020.log": b"x" + _value_bytes("ses_new", "D:/new", updated=2),
        },
    )
    result = read_active_session(tmp_path)
    assert result is not None
    assert result.session_id == "ses_new"
    assert result.directory == "D:/new"


def test_wal_tombstone_shadows_sst_value(tmp_path):
    # .log 里 key 存在但无值（删除）→ 遮蔽 .ldb 里的旧值 → None
    _write_dir(
        tmp_path,
        {
            "000010.ldb": b"x" + _value_bytes("ses_old", "D:/old"),
            "000020.log": b"x" + _KEY + b"\x01unrelated",
        },
    )
    assert read_active_session(tmp_path) is None


# --- 读取：无键 / 损坏 / 缺字段 -----------------------------------------


def test_no_storage_key_returns_none(tmp_path):
    _write_dir(tmp_path, {"000003.ldb": b"some other keys \x01ui-store\x01{...}"})
    assert read_active_session(tmp_path) is None
    assert read_last_session_value(tmp_path) is None


def test_missing_dir_returns_none(tmp_path):
    assert read_active_session(tmp_path / "does_not_exist") is None


def test_corrupt_json_returns_none(tmp_path):
    _write_dir(tmp_path, {"000003.ldb": b"p" + _value_bytes(None, raw="{not json at all")})
    assert read_active_session(tmp_path) is None


def test_missing_session_id_returns_none(tmp_path):
    # runtimes 有条目但缺 sessionId
    _write_dir(tmp_path, {"000003.ldb": b"p" + _value_bytes(None, directory="/x", updated=3)})
    assert read_active_session(tmp_path) is None


def test_empty_runtimes_returns_none(tmp_path):
    _write_dir(tmp_path, {"000003.ldb": b"p" + _KEY + b'{"version":1,"runtimes":{}}'})
    assert read_active_session(tmp_path) is None


def test_wrong_version_returns_none():
    assert parse_last_session_value('{"version":2,"runtimes":{"local":{"sessionId":"s"}}}') is None


def test_directory_null_is_stable(tmp_path):
    # directory 为 null → ActiveSession(directory=None)，不抛
    _write_dir(tmp_path, {"000003.ldb": b"p" + _value_bytes("ses_null", directory=None, updated=9)})
    result = read_active_session(tmp_path)
    assert result is not None
    assert result.session_id == "ses_null"
    assert result.directory is None
    assert result.source == "persisted-last-active"


def test_directory_empty_string_becomes_none():
    raw = '{"version":1,"runtimes":{"local":{"sessionId":"s","directory":"","updatedAt":1}}}'
    result = parse_last_session_value(raw)
    assert result is not None
    assert result.directory is None


# --- runtime 选择 -------------------------------------------------------


def test_local_runtime_preferred_over_newer_host():
    raw = (
        '{"version":1,"runtimes":{'
        '"local":{"sessionId":"ses_local","directory":"/l","updatedAt":5},'
        '"host:remote":{"sessionId":"ses_host","directory":"/h","updatedAt":99}'
        "}}"
    )
    result = parse_last_session_value(raw)
    assert result is not None
    assert result.session_id == "ses_local"


def test_only_host_uses_max_updated_at():
    raw = (
        '{"version":1,"runtimes":{'
        '"host:a":{"sessionId":"ses_a","directory":"/a","updatedAt":5},'
        '"host:b":{"sessionId":"ses_b","directory":"/b","updatedAt":99}'
        "}}"
    )
    result = parse_last_session_value(raw)
    assert result is not None
    assert result.session_id == "ses_b"


# --- validate_session ---------------------------------------------------


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"[]") -> None:
        self.status = status
        self.body = body

    def read(self, size: int = -1) -> bytes:
        return self.body if size is None or size < 0 else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def _client(monkeypatch, tmp_path, **kwargs) -> OpenChamberClient:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"desktopLocalClientToken": "tok-123"}), encoding="utf-8")
    kwargs.setdefault("settings_path", settings)
    kwargs.setdefault("token", None)
    return OpenChamberClient("http://127.0.0.1:57123", **kwargs)


def test_validate_session_success(monkeypatch, tmp_path):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        return FakeResponse(200, b"[{}]")

    monkeypatch.setattr("openchamber_client.urllib.request.urlopen", fake_urlopen)
    client = _client(monkeypatch, tmp_path)
    assert client.validate_session("ses_abc", "D:/work/repo") is True
    assert seen["url"] == "http://127.0.0.1:57123/api/session/ses_abc/message?directory=D%3A/work/repo"
    assert seen["auth"] == "Bearer tok-123"


def test_validate_session_no_directory_param(monkeypatch, tmp_path):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        return FakeResponse(200, b"[{}]")

    monkeypatch.setattr("openchamber_client.urllib.request.urlopen", fake_urlopen)
    assert _client(monkeypatch, tmp_path).validate_session("ses_x") is True
    assert seen["url"] == "http://127.0.0.1:57123/api/session/ses_x/message"


def test_validate_session_not_found(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 404, "not found", {}, io.BytesIO(b""))

    monkeypatch.setattr("openchamber_client.urllib.request.urlopen", fake_urlopen)
    assert _client(monkeypatch, tmp_path).validate_session("missing") is False


def test_validate_session_connection_failure(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(socket.timeout("timed out"))

    monkeypatch.setattr("openchamber_client.urllib.request.urlopen", fake_urlopen)
    assert _client(monkeypatch, tmp_path).validate_session("ses_x") is False


def test_validate_session_empty_id(monkeypatch, tmp_path):
    assert _client(monkeypatch, tmp_path).validate_session("") is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))