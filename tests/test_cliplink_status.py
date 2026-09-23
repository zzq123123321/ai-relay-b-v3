"""cliplink_status 只读解析 + 假连接判定测试（全 tmp_path，不碰真实 %LOCALAPPDATA%）。

覆盖：
  - 读有效 status.json → ClipLinkStatus 各字段
  - 文件缺失 / JSON 损坏 / 非对象 / 缺 status / 缺 updated_at → None（稳定不抛）
  - null peer / null latency 稳定收敛
  - updated_at 过旧阈值（= 3 × ClipLink 心跳 30s）：未过旧 / 已过旧 / 未来时钟
  - describe：connected 新→保留 latency；connected 过旧→“已断开(过旧)”+清延迟；
    paused 不按假连接降级；各状态串→中文文案映射（含未知→未连接）
  - snapshot：文件缺失→(未连接,--,None)；connected→含“name (ip)”对端
  - default_status_path 在 LOCALAPPDATA 下
  - now_millis 为正 int
"""

import json

import pytest

from cliplink_status import (
    STALE_MS,
    ClipLinkStatus,
    default_status_path,
    describe,
    is_stale,
    now_millis,
    read_cliplink_status,
    snapshot,
)


def _write(tmp_path, **fields):
    base = {
        "version": 1,
        "status": "connected",
        "peer_name": None,
        "peer_ip": None,
        "latency_ms": None,
        "generation": 0,
        "updated_at": 1000,
    }
    base.update(fields)
    p = tmp_path / "status.json"
    p.write_text(json.dumps(base), encoding="utf-8")
    return p


def _st(**fields):
    base = dict(status="connected", peer_name=None, peer_ip=None, latency_ms=None, generation=0, updated_at=1000)
    base.update(fields)
    return ClipLinkStatus(**base)


# --- 读取：有效值 -------------------------------------------------------


def test_read_valid_fields(tmp_path):
    p = _write(
        tmp_path,
        status="connected",
        peer_name="A端",
        peer_ip="1.2.3.4",
        latency_ms=42,
        generation=7,
        updated_at=999,
    )
    st = read_cliplink_status(p)
    assert isinstance(st, ClipLinkStatus)
    assert (st.status, st.peer_name, st.peer_ip, st.latency_ms, st.generation, st.updated_at) == (
        "connected",
        "A端",
        "1.2.3.4",
        42,
        7,
        999,
    )


def test_missing_file_returns_none(tmp_path):
    assert read_cliplink_status(tmp_path / "nope.json") is None


def test_corrupt_json_returns_none(tmp_path):
    (tmp_path / "status.json").write_text("{not json at all", encoding="utf-8")
    assert read_cliplink_status(tmp_path / "status.json") is None


def test_not_an_object_returns_none(tmp_path):
    (tmp_path / "status.json").write_text("[1,2,3]", encoding="utf-8")
    assert read_cliplink_status(tmp_path / "status.json") is None


def test_missing_status_returns_none(tmp_path):
    p = tmp_path / "status.json"
    p.write_text(json.dumps({"updated_at": 5}), encoding="utf-8")
    assert read_cliplink_status(p) is None


def test_missing_updated_at_returns_none(tmp_path):
    p = tmp_path / "status.json"
    p.write_text(json.dumps({"status": "connected"}), encoding="utf-8")
    assert read_cliplink_status(p) is None


def test_null_peers_and_latency_stable(tmp_path):
    st = read_cliplink_status(_write(tmp_path, status="offline"))
    assert st is not None
    assert st.peer_name is None
    assert st.peer_ip is None
    assert st.latency_ms is None
    assert st.generation == 0


# --- 假连接判定（updated_at 过旧）--------------------------------------


def test_fresh_connected_not_stale():
    assert not is_stale(_st(updated_at=1000), 1000 + STALE_MS - 1)


def test_connected_stale_past_threshold():
    assert is_stale(_st(updated_at=1000), 1000 + STALE_MS + 1)


def test_future_clock_not_stale():
    # updated_at 在未来（时钟偏移）→ 负龄，不判过旧
    assert not is_stale(_st(updated_at=5000), 1000)


def test_describe_connected_fresh_keeps_latency():
    assert describe(_st(status="connected", latency_ms=42, updated_at=1000), 2000) == ("已连接", "--", 42)


def test_describe_connected_stale_downgrades_and_clears_latency():
    got = describe(_st(status="connected", latency_ms=42, peer_name="A端", updated_at=1000), 1000 + STALE_MS + 1)
    assert got == ("已断开(过旧)", "A端", None)


def test_describe_paused_not_fake_downgraded():
    # paused 不触发假连接降级：即使过旧也按“暂停”原样显示
    assert describe(_st(status="paused", updated_at=1000), 1000 + STALE_MS + 1) == ("暂停", "--", None)


@pytest.mark.parametrize(
    "status,expected",
    [
        ("connected", "已连接"),
        ("paused", "暂停"),
        ("offline", "未连接"),
        ("connecting", "连接中"),
        ("reconnecting", "恢复中"),
        ("error", "连接失败"),
        ("weird", "未连接"),  # 未知状态 → 兜底未连接
    ],
)
def test_describe_status_text_mapping(status, expected):
    # now=1500、updated_at=1000 → 未过旧，走正常文案映射
    assert describe(_st(status=status, updated_at=1000), 1500)[0] == expected


# --- snapshot（读+判定一体）--------------------------------------------


def test_snapshot_missing_file():
    assert snapshot("/nonexistent/dir/x.json", now_ms=1) == ("未连接", "--", None)


def test_snapshot_connected_combines_peer_name_and_ip(tmp_path):
    p = _write(tmp_path, status="connected", peer_name="A端", peer_ip="1.2.3.4", latency_ms=9, updated_at=1000)
    assert snapshot(p, now_ms=2000) == ("已连接", "A端 (1.2.3.4)", 9)


def test_snapshot_peer_name_only(tmp_path):
    p = _write(tmp_path, status="paused", peer_name="A端", updated_at=1000)
    assert snapshot(p, now_ms=2000) == ("暂停", "A端", None)


# --- 路径与时钟 ---------------------------------------------------------


def test_default_status_path_under_localappdata(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
    assert str(default_status_path()) == r"C:\Users\test\AppData\Local\ClipLink\status.json"


def test_now_millis_positive_int():
    v = now_millis()
    assert isinstance(v, int)
    assert v > 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))