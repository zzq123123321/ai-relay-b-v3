"""读取 OpenChamber 持久化的“最近一次激活会话”。

OpenChamber（Electron/Desktop）把 UI 的 localStorage 落在 Chromium LevelDB：

    Windows  %APPDATA%/OpenChamber/Local Storage/leveldb
    macOS    ~/Library/Application Support/OpenChamber/Local Storage/leveldb
    Linux    ~/.config/OpenChamber/Local Storage/leveldb

每次 UI `setCurrentSession()` 都会写 localStorage 键 `oc.lastSession.v1`，
值为（源码 packages/ui/src/sync/last-session-cache.ts）：

    {"version":1,"runtimes":{"<runtimeKey>":{"sessionId","directory","updatedAt"}}}

桌面 loopback 的 runtimeKey 固定为 "local"。本模块只读、绝不修改
OpenChamber；任何异常一律返回 None。

可靠性边界：
- 值是 Chromium localStorage → 磁盘的“最后一次已落盘快照”，相对 UI 实时
  状态可能有落盘延迟；读到的是 persisted-last-active，不是 exact/live。
  调用方应再用 openchamber_client.validate_session 对服务端核实会话仍存在。
- 本读取依赖 LevelDB 数据块未压缩（本机 OpenChamber 实测 comp=none，
  值紧跟 key 之后为明文字节）。若未来 Chromium 改为压缩 SST，需补解压。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

#: localStorage 键（值结构见模块 docstring）
LAST_SESSION_KEY = "oc.lastSession.v1"
#: 磁盘里该 key 的本地名前面带 0x01 分隔符，用作定位锚点
_KEY_ANCHOR = b"\x01" + LAST_SESSION_KEY.encode("utf-8")
_KEY_PLAIN = LAST_SESSION_KEY.encode("utf-8")
#: 值紧随 key 之后；窗口上限（值很小，8KiB 足够且防止扫到别的数据块）
_VALUE_WINDOW = 8192
#: “key 存在但其后无 JSON”（删除/空值）的内部标记
_DELETED = object()


@dataclass(frozen=True, slots=True)
class ActiveSession:
    """一次持久化的“最近激活会话”记录。"""

    session_id: str
    directory: str | None
    source: str


def default_leveldb_dirs() -> list[Path]:
    """按平台给出 OpenChamber 的 Local Storage/leveldb 候选目录。

    顶层为主 UI（origin openchamber-ui://app）；Partitions/* 为可能的
    内嵌浏览器分区，一并纳入候选，取第一个“键可解析”的目录。
    """
    home = Path.home()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming"))) / "OpenChamber"
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support" / "OpenChamber"
    else:
        base = home / ".config" / "OpenChamber"
    dirs = [base / "Local Storage" / "leveldb"]
    parts = base / "Partitions"
    if parts.is_dir():
        for child in sorted(parts.iterdir()):
            candidate = child / "Local Storage" / "leveldb"
            if candidate.is_dir():
                dirs.append(candidate)
    return dirs


def _extract_json_after(data: bytes, value_start: int) -> bytes | None:
    """从 value_start 起，在窗口内找到首个 JSON 对象并截到匹配的 '}'。"""
    end = min(len(data), value_start + _VALUE_WINDOW)
    i = data.find(b"{", value_start, end)
    if i == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for j in range(i, end):
        c = data[j]
        if in_str:
            if esc:
                esc = False
            elif c == 0x5C:
                esc = True
            elif c == 0x22:
                in_str = False
        elif c == 0x22:
            in_str = True
        elif c == 0x7B:  # {
            depth += 1
        elif c == 0x7D:  # }
            depth -= 1
            if depth == 0:
                return data[i : j + 1]
    return None


def _key_value_start(data: bytes, anchor: int) -> int:
    """给定 key 起点 anchor，返回其值（紧跟 key 之后）的起始下标。"""
    if data[anchor : anchor + 1] == b"\x01":
        return anchor + len(_KEY_ANCHOR)
    return anchor + len(_KEY_PLAIN)


def _value_from_file(data: bytes) -> object:
    """该文件里 LAST_SESSION_KEY 的最新值。

    返回 bytes（值 JSON）/ _DELETED（key 存在但其后无 JSON）/ None（无该 key）。
    同一文件内取最后一次出现的 key（更靠后 = 更新）。
    """
    anchor = data.find(_KEY_ANCHOR)
    if anchor == -1:
        anchor = data.find(_KEY_PLAIN)
        if anchor == -1:
            return None
    search_from = _key_value_start(data, anchor)
    while True:
        nxt = data.find(_KEY_ANCHOR, search_from)
        if nxt == -1:
            nxt = data.find(_KEY_PLAIN, search_from)
        if nxt == -1:
            break
        anchor = nxt
        search_from = _key_value_start(data, nxt)
    value = _extract_json_after(data, _key_value_start(data, anchor))
    return value if value is not None else _DELETED


def read_leveldb_value(leveldb_dir: str | os.PathLike) -> bytes | None:
    """只读扫描一个 LevelDB 目录，返回 LAST_SESSION_KEY 的最新值（或 None）。

    文件按“新→旧”遍历：*.log（内存表，最新）优先于 *.ldb（SST），
    同名内按文件名降序。第一个“含该 key”的文件即权威结果。
    """
    base = Path(leveldb_dir)
    if not base.is_dir():
        return None
    logs = [p for p in base.glob("*.log") if p.is_file()]
    ldbs = [p for p in base.glob("*.ldb") if p.is_file()]
    for path in sorted(logs, reverse=True) + sorted(ldbs, reverse=True):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        result = _value_from_file(data)
        if result is None:
            continue
        if result is _DELETED:
            return None
        return result
    return None


def read_last_session_value(
    leveldb_dir: str | os.PathLike | None = None,
) -> bytes | None:
    """给定目录（或自动探测）读取 LAST_SESSION_KEY 的值；找不到返回 None。"""
    if leveldb_dir is not None:
        return read_leveldb_value(leveldb_dir)
    for candidate in default_leveldb_dirs():
        if not candidate.is_dir():
            continue
        value = read_leveldb_value(candidate)
        if value is not None:
            return value
    return None


def parse_last_session_value(raw: bytes | str | None) -> ActiveSession | None:
    """把 oc.lastSession.v1 的 JSON 值解析成 ActiveSession。

    选择策略：优先 desktop loopback 的 "local" runtime；否则取
    updatedAt 最大的那条（源码本就按 updatedAt 保留最 recent）。
    任何结构异常都返回 None。
    """
    if raw is None:
        return None
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not raw.strip():
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("version") != 1:
        return None
    runtimes = obj.get("runtimes")
    if not isinstance(runtimes, dict) or not runtimes:
        return None

    entries = []
    for runtime_key, entry in runtimes.items():
        if not isinstance(entry, dict):
            continue
        session_id = entry.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            continue
        directory = entry.get("directory")
        directory = directory if isinstance(directory, str) and directory else None
        updated = entry.get("updatedAt")
        if not isinstance(updated, (int, float)):
            updated = 0
        entries.append((updated, runtime_key, session_id, directory))
    if not entries:
        return None

    local = next((e for e in entries if e[1] == "local"), None)
    # ponytail: 多 runtime（relay/远程 host）时按 updatedAt 取最 recent；
    # 桌面单机只有 "local"，此分支不触发。若要精确匹配“当前”runtime，
    # 需另读内存态 runtimeKey，届时改为按 runtimeKey 精确选择。
    chosen = local if local is not None else max(entries, key=lambda e: e[0])
    _, _, session_id, directory = chosen
    return ActiveSession(session_id=session_id, directory=directory, source="persisted-last-active")


def read_active_session(
    leveldb_dir: str | os.PathLike | None = None,
) -> ActiveSession | None:
    """读取 OpenChamber 持久化的最近激活会话；无法取得时返回 None。"""
    raw = read_last_session_value(leveldb_dir)
    return parse_last_session_value(raw)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else None
    session = read_active_session(target)
    if session is None:
        print("unavailable")
    else:
        print(f"{session.source} session_id={session.session_id} directory={session.directory}")
    raise SystemExit(0)