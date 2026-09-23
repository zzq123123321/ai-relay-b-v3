"""OpenChamber 服务连接层（Lite）。

只有一个职责：探测 OpenChamber 服务是否可达，并用单调时钟测出
"请求开始 → 响应完成"的服务延迟（UI"大模型连接/服务延迟"的数据源）。
本模块不做 prompt 发送、会话读取或 compact（后续轮次扩展）。

延迟口径：服务延迟 != 模型首响应延迟。模型首响应由真实任务
（prompt 发出 → 第一段模型响应）另行计算。
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


def is_loopback_base_url(base_url: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(base_url).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOOPBACK_HOSTS


def default_settings_path() -> Path:
    return Path.home() / ".config" / "openchamber" / "settings.json"


def resolve_local_token(
    base_url: str,
    *,
    settings_path: str | os.PathLike | None = None,
) -> str | None:
    """loopback 才解析本地 Bearer token（desktopLocalClientToken）。

    非 loopback 直接返回 None 且不读任何设置文件，杜绝远程目标
    携带本地桌面凭据。token 只用于组 Authorization 头，不落日志。
    """
    if not is_loopback_base_url(base_url):
        return None
    path = Path(settings_path) if settings_path is not None else default_settings_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("desktopLocalClientToken") if isinstance(data, dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """probe() 的稳定返回：永不抛异常，失败信息收敛到 error 字段。"""

    connected: bool
    latency_ms: int | None
    error: str | None


class OpenChamberClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 5.0,
        settings_path: str | os.PathLike | None = None,
    ) -> None:
        base = base_url.rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise ValueError(f"base_url must be http(s): {base_url!r}")
        self.base_url = base
        self.timeout = timeout
        env_token = os.environ.get("OPENCHAMBER_CLIENT_TOKEN", "").strip()
        self._token = (token or env_token) or resolve_local_token(base, settings_path=settings_path)

    def probe(self) -> ProbeResult:
        url = self.base_url + "/health"
        request = urllib.request.Request(url)
        if self._token:
            request.add_header("Authorization", f"Bearer {self._token}")
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read(65536)
        except urllib.error.HTTPError as exc:
            latency = _elapsed_ms(started)
            return ProbeResult(False, latency, f"http: {exc.code}")
        except (TimeoutError, socket.timeout):
            latency = _elapsed_ms(started)
            return ProbeResult(False, latency, "timeout")
        except urllib.error.URLError as exc:
            latency = _elapsed_ms(started)
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                return ProbeResult(False, latency, "timeout")
            return ProbeResult(False, latency, f"connection: {reason}")
        except OSError as exc:
            latency = _elapsed_ms(started)
            return ProbeResult(False, latency, f"connection: {exc}")
        latency = _elapsed_ms(started)
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return ProbeResult(False, latency, "malformed: body is not JSON")
        if not isinstance(data, dict):
            return ProbeResult(False, latency, "malformed: health is not an object")
        return ProbeResult(True, latency, None)


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:57123"
    result = OpenChamberClient(target).probe()
    if result.connected:
        print(f"connected latency_ms={result.latency_ms}")
    else:
        print(f"disconnected error={result.error}")
    raise SystemExit(0 if result.connected else 1)