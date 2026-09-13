"""T20-03：配置快照绑定与只读运行时工厂自动测试（全程 fake，0 真实网络）。

覆盖卡列 25 项（可多于 25）+ 若干补充：factory 返回值与冻结字段、env /
desktop 认证边界、connect_timeout 不被偷偷替换、四方法委托与冻结目录、
blank 目录 MISSING_INPUT、后续 snapshot 不变异已有 runtime、公开面安全、
session_id 不消费。测试风格对齐 tests 目录（pytest 函数式 + 断言惯例）。
"""

from __future__ import annotations

import inspect
import io
import json
import os
import sys
import urllib.parse
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from adapters.openchamber import (  # noqa: E402
    KIND_MISSING_INPUT,
    OpenChamberApiError,
    OpenChamberObservation,
)
from app.openchamber_read_runtime import (  # noqa: E402
    ENV_CLIENT_TOKEN,
    OpenChamberReadRuntime,
    build_openchamber_read_runtime,
)
from core.domain import (  # noqa: E402
    BusyResumePolicy,
    ConfigBody,
    DeliveryConfig,
    HttpConfig,
    LimitsConfig,
    LogsConfig,
    NetworkConfig,
    OpenChamberConfig,
    RecoveryConfig,
    RotationConfig,
    SessionBindingMode,
    SettingsSnapshot,
    TargetExecutor,
    TransportProfile,
    UiConfig,
    UiTheme,
)


# =====================================================================
# 测试基础设施：FakeOpener / FakeResponse / _make_body / _snapshot
# =====================================================================


class FakeResponse:
    def __init__(self, status: int, body: str | bytes, content_type: str = "application/json"):
        self.status = status
        self._body = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        self.headers = {"Content-Type": content_type}
        self.content_type = content_type

    def getcode(self) -> int:
        return self.status

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = len(self._body)
        return self._body[:n]


class FakeOpener:
    def __init__(self, routes: dict[str, object], default_status: int = 200, default_body: str = "{}"):
        self.routes = {("/" + str(k).strip("/")): v for k, v in routes.items()}
        self.default_status = default_status
        self.default_body = default_body
        self.requests: list[dict] = []
        self.call_count = 0

    def __call__(self, req):
        self.call_count += 1
        parts = urllib.parse.urlsplit(req.full_url)
        path_query = (parts.path or "").rstrip("/") or "/"
        if parts.query:
            path_query += "?" + parts.query
        self.requests.append(
            {"url": req.full_url, "method": req.get_method(), "headers": {k: v for k, v in req.header_items()}}
        )
        entry = self.routes.get(path_query)
        if entry is None:
            return FakeResponse(self.default_status, self.default_body)
        if isinstance(entry, Exception):
            raise entry
        status, body = entry[0], entry[1]
        return FakeResponse(status, body)


def _make_body(
    *,
    url: str = "http://localhost:57123",
    directory: str = "/proj",
    session_id: str = "",
    connect: float = 3.0,
    read: float = 10.0,
) -> ConfigBody:
    """直接构造完整 ConfigBody（所有子配置全部显式给出）。"""
    return ConfigBody(
        schema_version=1,
        default_target=TargetExecutor.OPENCHAMBER,
        openchamber=OpenChamberConfig(
            url=url,
            directory=directory,
            session_id=session_id,
            session_policy=SessionBindingMode.FIXED_SESSION,
            agent="build",
            model="",
            capability_profile="UNVERIFIED",
            auto_open_session=True,
        ),
        recovery=RecoveryConfig(
            automatic_resume=True,
            resume_after_restart=True,
            completion_timeout_seconds=0.0,
            poll_interval_seconds=2.0,
            completion_grace_seconds=5.0,
            idle_confirmations=3,
            recovery_delay_seconds=10.0,
            awaiting_progress_seconds=30.0,
            cumulative_resume_limit=None,
            no_progress_cooldown_threshold=3,
            cooldown_seconds=60.0,
            fast_read_retry_delays_seconds=(3.0, 8.0),
            sustained_read_delays_seconds=(15.0, 30.0),
            busy_stale_log_seconds=30.0,
            busy_review_seconds=120.0,
            busy_resume_policy=BusyResumePolicy.CONFIRMED_IDLE_ONLY,
            resume_truncated_output=True,
            prompt_version="continue_zh_v1",
        ),
        http=HttpConfig(
            connect_timeout_seconds=connect,
            read_timeout_seconds=read,
            operation_budget_seconds=20.0,
            automatic_post_retries=0,
        ),
        network=NetworkConfig(
            model_probe_target="",
            interval_seconds=5.0,
            recovery_successes=2,
            zerotier_enabled=False,
            external_bridge_enabled=False,
            external_bridge_path="",
            sample_ttl_seconds=20.0,
        ),
        rotation=RotationConfig(enabled=False, success_threshold=5, inherit_auto_accept=False),
        delivery=DeliveryConfig(
            profile=TransportProfile.LEGACY_V1,
            automatic_ack=False,
            max_unconfirmed_offers=1,
        ),
        limits=LimitsConfig(
            incoming_body_bytes=2097152,
            incoming_envelope_bytes=4194304,
            queued_tasks=1000,
        ),
        logs=LogsConfig(
            event_retention_days=30,
            event_budget_bytes=209715200,
            debug_enabled=False,
            debug_file_bytes=10485760,
            debug_files=5,
            debug_retention_days=7,
            include_task_body=False,
            include_model_response=False,
        ),
        ui=UiConfig(
            theme=UiTheme.LIGHT,
            density="comfortable",
            always_on_top=False,
            close_action="hide_to_tray",
            auto_start_with_windows=False,
            quit_warning_seconds=15.0,
        ),
    )


def _snapshot(
    *,
    revision: int = 3,
    url: str = "http://localhost:57123",
    directory: str = "/proj",
    session_id: str = "",
    connect: float = 3.0,
    read: float = 10.0,
) -> SettingsSnapshot:
    body = _make_body(url=url, directory=directory, session_id=session_id, connect=connect, read=read)
    return SettingsSnapshot(revision=revision, created_at="2026-01-01T00:00:00+00:00", config=body)


# =====================================================================
# 01-05：factory 返回值 / 冻结字段 / 构造不产生 HTTP
# =====================================================================


def test_factory_returns_runtime():
    snap = _snapshot(revision=4, directory="/proj")
    rt = build_openchamber_read_runtime(snap)
    assert isinstance(rt, OpenChamberReadRuntime)


def test_config_revision_frozen():
    snap = _snapshot(revision=9)
    rt = build_openchamber_read_runtime(snap)
    assert rt.config_revision == 9
    assert rt.config_revision == snap.revision


def test_base_url_from_snapshot():
    rt = build_openchamber_read_runtime(_snapshot(url="http://localhost:9876"))
    assert rt.base_url == "http://localhost:9876"


def test_directory_from_snapshot():
    rt = build_openchamber_read_runtime(_snapshot(directory="/my/project"))
    assert rt.directory == "/my/project"


def test_construction_sends_zero_http():
    opener = FakeOpener({})
    rt = build_openchamber_read_runtime(_snapshot(), opener=opener)
    assert opener.call_count == 0
    assert rt.client.transport.n_calls == 0


# =====================================================================
# 06-10：认证边界继承
# =====================================================================


def test_env_token_passed_to_local_transport():
    opener = FakeOpener({})
    rt = build_openchamber_read_runtime(
        _snapshot(),
        env={ENV_CLIENT_TOKEN: "tok-env"},
        opener=opener,
    )
    assert rt.client.transport._token == "tok-env"
    # 委托调用验证 Bearer 头
    query = urllib.parse.urlencode({"directory": "/proj"})
    opener.routes[f"/api/session?{query}"] = (200, "[]")
    rt.list_sessions()
    assert opener.requests[0]["headers"]["Authorization"] == "Bearer tok-env"


def test_env_token_priority_over_desktop_token(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"desktopLocalClientToken": "tok-desktop"}), encoding="utf-8")
    rt = build_openchamber_read_runtime(
        _snapshot(),
        env={ENV_CLIENT_TOKEN: "tok-env"},
        settings_path=settings,
    )
    assert rt.client.transport._token == "tok-env"


def test_desktop_token_fallback(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"desktopLocalClientToken": "tok-desktop"}), encoding="utf-8")
    rt = build_openchamber_read_runtime(_snapshot(), env={}, settings_path=settings)
    assert rt.client.transport._token == "tok-desktop"


def test_remote_base_url_does_not_read_desktop_settings(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"desktopLocalClientToken": "tok-desktop"}), encoding="utf-8")

    def _fail(_p):
        raise AssertionError("远程目标不得读取 desktop settings 文件")

    monkeypatch.setattr(os.path, "isfile", _fail)
    rt = build_openchamber_read_runtime(
        _snapshot(url="http://192.0.2.10:57123"), env={}, settings_path=settings
    )
    assert rt.client.transport._token is None


def test_remote_base_url_never_sends_token():
    directory = "/proj"
    query = urllib.parse.urlencode({"directory": directory})
    opener = FakeOpener({f"/api/session?{query}": (200, "[]")})
    rt = build_openchamber_read_runtime(
        _snapshot(url="http://192.0.2.10:57123", directory=directory),
        env={ENV_CLIENT_TOKEN: "tok-secret"},
        opener=opener,
    )
    assert rt.client.transport._token is None
    rt.list_sessions()
    assert "Authorization" not in opener.requests[0]["headers"]


# =====================================================================
# 11-12：timeout 绑定
# =====================================================================


def test_read_timeout_seconds_bound_to_transport_timeout():
    rt = build_openchamber_read_runtime(_snapshot(read=5.0))
    assert rt.client.transport.timeout == 5.0


def test_connect_timeout_not_silently_substituted():
    rt = build_openchamber_read_runtime(_snapshot(connect=11.0, read=5.0))
    assert rt.client.transport.timeout == 5.0
    assert rt.client.transport.timeout != 11.0


# =====================================================================
# 13-16：四方法委托
# =====================================================================


def test_health_delegates_to_client():
    opener = FakeOpener({"/health": (200, '{"status":"ok"}')})
    rt = build_openchamber_read_runtime(_snapshot(), env={}, opener=opener)
    obs = rt.health()
    assert isinstance(obs, OpenChamberObservation)
    assert obs.endpoint == "health"
    assert obs.http_status == 200
    assert opener.requests[0]["url"] == "http://localhost:57123/health"
    # health 不附加 Authorization
    assert "Authorization" not in opener.requests[0]["headers"]


def test_list_sessions_uses_frozen_directory():
    directory = "D:\\AIwork\\ai relay b&v3"
    query = urllib.parse.urlencode({"directory": directory})
    opener = FakeOpener({f"/api/session?{query}": (200, "[]")})
    rt = build_openchamber_read_runtime(_snapshot(directory=directory), env={}, opener=opener)
    obs = rt.list_sessions()
    parts = urllib.parse.urlsplit(opener.requests[0]["url"])
    assert parts.path == "/api/session"
    assert parts.query == query
    assert obs.payload == []
    assert obs.missing_semantics is not None  # [] → MISSING 输入语义


def test_session_status_uses_frozen_directory():
    directory = "D:\\AIwork\\ai relay b&v3"
    query = urllib.parse.urlencode({"directory": directory})
    opener = FakeOpener({f"/api/session/status?{query}": (200, "{}")})
    rt = build_openchamber_read_runtime(_snapshot(directory=directory), env={}, opener=opener)
    obs = rt.session_status()
    parts = urllib.parse.urlsplit(opener.requests[0]["url"])
    assert parts.path == "/api/session/status"
    assert parts.query == query
    assert obs.payload == {}
    assert obs.missing_semantics is not None


def test_permission_state_delegates_to_client():
    opener = FakeOpener({"/api/permission-auto-accept": (200, '{"sessions":{},"revision":1}')})
    rt = build_openchamber_read_runtime(_snapshot(), env={ENV_CLIENT_TOKEN: "tok"}, opener=opener)
    obs = rt.permission_state()
    assert obs.endpoint == "permission_state"
    assert obs.payload == {"sessions": {}, "revision": 1}
    assert opener.requests[0]["headers"]["Authorization"] == "Bearer tok"


def test_list_sessions_accepts_no_directory_argument():
    rt = build_openchamber_read_runtime(_snapshot())
    sig = inspect.signature(rt.list_sessions)
    assert list(sig.parameters) == []


def test_session_status_accepts_no_directory_argument():
    rt = build_openchamber_read_runtime(_snapshot())
    sig = inspect.signature(rt.session_status)
    assert list(sig.parameters) == []


# =====================================================================
# 17-18：空目录行为
# =====================================================================


def test_blank_directory_list_missing_input_zero_http():
    opener = FakeOpener({})
    rt = build_openchamber_read_runtime(_snapshot(directory=""), opener=opener)
    with pytest.raises(OpenChamberApiError) as ctx:
        rt.list_sessions()
    assert ctx.value.kind == KIND_MISSING_INPUT
    assert ctx.value.endpoint == "list_sessions"
    assert opener.call_count == 0
    assert rt.client.transport.n_calls == 0


def test_blank_directory_status_missing_input_zero_http():
    opener = FakeOpener({})
    rt = build_openchamber_read_runtime(_snapshot(directory=""), opener=opener)
    with pytest.raises(OpenChamberApiError) as ctx:
        rt.session_status()
    assert ctx.value.kind == KIND_MISSING_INPUT
    assert ctx.value.endpoint == "session_status"
    assert opener.call_count == 0
    assert rt.client.transport.n_calls == 0


# =====================================================================
# 19-21：配置冻结证明
# =====================================================================


def test_later_snapshot_does_not_mutate_existing_revision():
    first = build_openchamber_read_runtime(_snapshot(revision=2))
    second = _snapshot(revision=88)
    assert first.config_revision == 2
    assert second.revision == 88


def test_later_snapshot_does_not_mutate_existing_url():
    first = build_openchamber_read_runtime(_snapshot(url="http://localhost:57123"))
    second = _snapshot(url="http://192.0.2.10:57123")
    assert first.base_url == "http://localhost:57123"
    assert second.config.openchamber.url == "http://192.0.2.10:57123"


def test_later_snapshot_does_not_mutate_existing_directory():
    first = build_openchamber_read_runtime(_snapshot(directory="/alpha"))
    second = _snapshot(directory="/beta")
    assert first.directory == "/alpha"
    assert second.config.openchamber.directory == "/beta"


# =====================================================================
# 22-24：公开面安全
# =====================================================================


def test_no_messages_public_api():
    rt = build_openchamber_read_runtime(_snapshot())
    for name in ("messages", "list_messages", "session_messages", "get_messages"):
        assert not hasattr(OpenChamberReadRuntime, name), f"类公开面不应存在 messages 方法 {name}"
        assert not hasattr(rt, name), f"公开面不应存在 messages 方法 {name}"


def test_no_write_or_control_public_api():
    rt = build_openchamber_read_runtime(_snapshot())
    for name in (
        "send", "send_once", "post", "create_session", "rotate_session",
        "stop", "retry", "approve", "reject", "compact", "request",
    ):
        assert not hasattr(OpenChamberReadRuntime, name), f"类公开面不应存在写/控制方法 {name}"
        assert not hasattr(rt, name), f"公开面不应存在写/控制方法 {name}"


def test_no_unified_connected_healthy_api():
    field_names = {f.name for f in fields(OpenChamberReadRuntime)}
    assert field_names == {"config_revision", "base_url", "directory", "client"}
    rt = build_openchamber_read_runtime(_snapshot())
    for name in (
        "connected", "healthy", "everything_ok", "is_ready",
        "api_authenticated", "session_exists", "execution_progressing",
    ):
        assert not hasattr(rt, name), f"运行时不应输出综合判定属性 {name}"


# =====================================================================
# 25：session_id 不消费
# =====================================================================


def test_session_id_not_consumed_for_http():
    directory = "/proj"
    query = urllib.parse.urlencode({"directory": directory})
    opener = FakeOpener({
        "/health": (200, '{"status":"ok"}'),
        f"/api/session?{query}": (200, "[]"),
        f"/api/session/status?{query}": (200, "{}"),
        "/api/permission-auto-accept": (200, '{"sessions":{},"revision":1}'),
    })
    rt = build_openchamber_read_runtime(
        _snapshot(directory=directory, session_id="rel_s246_live"),
        env={},
        opener=opener,
    )
    rt.health()
    rt.list_sessions()
    rt.session_status()
    rt.permission_state()
    assert opener.call_count == 4
    for r in opener.requests:
        assert "rel_s246_live" not in r["url"]
        assert r["method"] == "GET"
        assert "/message" not in r["url"]
    paths = {urllib.parse.urlsplit(r["url"]).path for r in opener.requests}
    assert paths == {"/health", "/api/session", "/api/session/status", "/api/permission-auto-accept"}


# =====================================================================
# 补充：env 读 os.environ / frozen dataclass 行为
# =====================================================================


def test_env_defaults_to_os_environ(monkeypatch):
    monkeypatch.setenv(ENV_CLIENT_TOKEN, "tok-os")
    rt = build_openchamber_read_runtime(_snapshot())
    assert rt.client.transport._token == "tok-os"


def test_runtime_is_frozen():
    rt = build_openchamber_read_runtime(_snapshot())
    with pytest.raises(FrozenInstanceError):
        rt.config_revision = 999
    with pytest.raises(FrozenInstanceError):
        rt.base_url = "http://never"
    with pytest.raises(FrozenInstanceError):
        rt.directory = "/x"
