"""只读 ClipLink 暴露的本机连接状态文件，供 AI Relay 的“A端连接”卡展示。

ClipLink（独立 Tauri 应用）把已有连接状态/对端/RTT 写到：

    Windows  %LOCALAPPDATA%/ClipLink/status.json

固定 schema（写方见 ClipLink external_status.rs）：

    {"version":1,
     "status":"connected|paused|offline|connecting|reconnecting|error",
     "peer_name":str|null, "peer_ip":str|null, "latency_ms":int|null,
     "generation":int, "updated_at":<epoch ms>}

本模块只读、绝不写/删该文件；任何异常（文件缺失 / JSON 损坏 / 缺关键字段）
一律收敛为 None，绝不把异常抛给 UI。

假连接判定：ClipLink 的 connected 态每 ~10s（生产 heartbeat_secs）刷新一次
updated_at；进程崩溃后文件冻结。若 status==connected 但 updated_at 距今超过
STALE_MS（= 3 次心跳周期），判为“已断开(过旧)”，避免把崩溃前遗留的
connected 当活连接。paused / offline / connecting / reconnecting / error 均不按
此降级：paused 本就不可自动中继、其余非活连接态本就没有周期性心跳刷新。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

#: connected 态判为“假连接”的静默阈值：= 3 × ClipLink 生产心跳周期(10s)。
#: ponytail: 固定 30s；若 ClipLink 改心跳周期，按“3 × 周期”同步调整即可。
STALE_MS = 30_000

#: ClipLink 状态串 → UI 文案（配色语义见 ui.main_window._status_color）
_STATUS_TEXT = {
    "connected": "已连接",
    "paused": "暂停",
    "offline": "未连接",
    "connecting": "连接中",
    "reconnecting": "恢复中",
    "error": "连接失败",
}


@dataclass(frozen=True, slots=True)
class ClipLinkStatus:
    """一次 status.json 快照（只保留 UI 需要的字段）。"""

    status: str
    peer_name: str | None
    peer_ip: str | None
    latency_ms: int | None
    generation: int
    updated_at: int


def now_millis() -> int:
    """当前 epoch 毫秒（与 ClipLink updated_at 同一时间基准）。"""
    return int(time.time() * 1000)


def default_status_path() -> Path:
    """%LOCALAPPDATA%/ClipLink/status.json（本机固定，非 Tauri app_data 目录）。"""
    base = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    return Path(base) / "ClipLink" / "status.json"


def is_stale(status: ClipLinkStatus, now_ms: int, max_age_ms: int = STALE_MS) -> bool:
    """updated_at 距今超过阈值 → 过旧（假连接）。未来时钟偏移(负龄)视为未过旧。"""
    return (now_ms - status.updated_at) > max_age_ms


def _peer_text(name: str | None, ip: str | None) -> str:
    if name and ip:
        return f"{name} ({ip})"
    return name or ip or "--"


def read_cliplink_status(path=None) -> ClipLinkStatus | None:
    """读取并解析 status.json；缺失/损坏/缺 status 或 updated_at → None（绝不抛）。"""
    target = Path(path) if path is not None else default_status_path()
    try:
        raw = target.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    status = obj.get("status")
    if not isinstance(status, str) or not status:
        return None
    updated_at = obj.get("updated_at")
    if not isinstance(updated_at, (int, float)):
        return None

    def opt_str(key: str) -> str | None:
        v = obj.get(key)
        return v if isinstance(v, str) and v else None

    latency = obj.get("latency_ms")
    latency = int(latency) if isinstance(latency, (int, float)) else None
    generation = obj.get("generation")
    generation = int(generation) if isinstance(generation, (int, float)) else 0
    return ClipLinkStatus(
        status=status,
        peer_name=opt_str("peer_name"),
        peer_ip=opt_str("peer_ip"),
        latency_ms=latency,
        generation=generation,
        updated_at=int(updated_at),
    )


def describe(status: ClipLinkStatus, now_ms: int) -> tuple[str, str, int | None]:
    """把一次快照映射成 (状态文案, 对端文案, 延迟ms)。

    connected 且过旧 → “已断开(过旧)”（崩溃遗留假连接）、延迟清空；
    其余状态原样展示（latency 直接取文件值，非活连接态 ClipLink 已置 null）。
    """
    peer = _peer_text(status.peer_name, status.peer_ip)
    if status.status == "connected" and is_stale(status, now_ms):
        return "已断开(过旧)", peer, None
    return _STATUS_TEXT.get(status.status, "未连接"), peer, status.latency_ms


def snapshot(path=None, now_ms: int | None = None) -> tuple[str, str, int | None]:
    """一次“读 + 判定”，供 UI 定时轮询直接喂给 set_a_connection。

    文件读不到/损坏 → (“未连接", “--", None)；now_ms 缺省用真实时钟。
    """
    st = read_cliplink_status(path)
    if st is None:
        return "未连接", "--", None
    return describe(st, now_ms if now_ms is not None else now_millis())


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else None
    status, peer, latency = snapshot(target)
    lat = f"{latency} ms" if latency is not None else "-- ms"
    print(f"{status}  对端：{peer}  网络延迟：{lat}")
    raise SystemExit(0)