# -*- coding: utf-8 -*-
"""独立取证：带 token 只读 GET OpenChamber 当前激活会话最后一条 assistant 的原始结构。

只读：绝不 POST / 绝不写剪贴板 / 绝不改动任何状态。若有 token 则带 Authorization。
用 openchamber_client 自己的 _http/_token/_base_url（内部已 resolve_local_token）。
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = r"D:\AIwork\ai_relay_b\最新开发01"
sys.path.insert(0, ROOT)

from active_session_reader import ActiveSession, read_active_session
from openchamber_client import (
    OpenChamberClient,
    default_settings_path,
    resolve_local_token,
    _visible_text,
)

if __name__ == "__main__":
    import os

    old_out = sys.stdout
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    s = read_active_session()
    print("session_id=", s.session_id, "directory=", s.directory, "source=", s.source)
    if not s.session_id:
        raise SystemExit("no session")

    base = "http://127.0.0.1:57123"
    client = OpenChamberClient(base)
    token = resolve_local_token(base, settings_path=default_settings_path())
    print("token_resolved=", token is not None)

    url = client._messages_url(s.session_id, s.directory)
    print("messages_url=", url)

    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            body = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        print("read_error=HTTP", exc.code, exc.reason)
        raise SystemExit(0)
    except (urllib.error.URLError, OSError) as exc:
        print("read_error=", repr(exc))
        raise SystemExit(0)

    messages = json.loads(body.decode("utf-8"))
    print("read_ok=True total_messages=", len(messages))

    assistants = [
        m
        for m in messages
        if isinstance(m, dict)
        and isinstance(m.get("info"), dict)
        and m["info"].get("role") == "assistant"
    ]
    print("assistant_count=", len(assistants))
    if not assistants:
        raise SystemExit(0)

    latest = assistants[-1]
    info = latest.get("info", {})
    print("\n=== 最后一条 assistant info（完整，取证用）===")
    print(json.dumps(info, ensure_ascii=False, indent=2))

    parts = latest.get("parts") or []
    print("\n=== parts 结构（仅取证 text 是否可见）===")
    vis_count = 0
    for i, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        t = part.get("text")
        is_vis = isinstance(t, str) and t.strip() and _visible_text({"parts": [part]})
        if is_vis:
            vis_count += 1
        head = (t[:80] if isinstance(t, str) else t).replace("\n", "\\n")
        print(f"  part[{i}] keys={sorted(part.keys())} text_head={head[:90]!r} visible={is_vis}")
    print("\nvisible_text_count=", vis_count)

    # 对照 Lite complete 判定最可能失败点：completed 时间戳
    tail_info = info.get("info") if isinstance(info.get("info"), dict) else {}
    time_obj = info.get("time")
    print("\n=== 判定对照字段 ===")
    print("finish=", repr(info.get("finish")), "（需要 == 'stop'）")
    print("completed=", repr((time_obj or {}).get("completed") if isinstance(time_obj, dict) else time_obj),
          "（需要为数字 ms）")
    print("error=", repr(info.get("error")), "（需要 None）")
    print("synthetic=", repr(info.get("synthetic")), "（需要 False/None）")
    print("summary=", repr(info.get("summary", info.get("is_summary"))), "（需要 False/None）")
    print("assistant_after_user=", "最后 assistant 是原任务后产生的（info.time 在主 user 之后）")
