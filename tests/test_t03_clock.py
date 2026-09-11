"""T03 时钟测试：FakeClock / SystemClock、advance、monotonic 不回退、deadline 与 sleep/wake 重建。"""

from datetime import datetime, timedelta, timezone

import pytest

from core.errors import DomainError, ErrorCode
from infra.clock import (
    Clock,
    DeadlineState,
    FakeClock,
    SystemClock,
    recompute_after_wake,
    schedule_state,
)

TZ = timezone.utc
T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=TZ)


def test_system_clock_provides_wall_and_monotonic():
    clock = SystemClock()
    now = clock.now()
    mono = clock.monotonic()
    assert isinstance(now, datetime)
    assert now.tzinfo is not None  # 避免 naive/aware 混淆
    assert isinstance(mono, float)
    assert clock.monotonic() >= mono  # 单调不回退


def test_fake_clock_initial_values():
    clock = FakeClock()
    assert clock.now() == datetime(2000, 1, 1, tzinfo=timezone.utc)
    assert clock.monotonic() == 0.0
    custom = FakeClock(wall=T0, mono=10.0)
    assert custom.now() == T0
    assert custom.monotonic() == 10.0


def test_fake_clock_advance_moves_both():
    clock = FakeClock(wall=T0, mono=5.0)
    clock.advance(10)
    assert clock.now() == T0 + timedelta(seconds=10)
    assert clock.monotonic() == 15.0
    # 继续前进，不依赖任何真实 sleep。
    clock.advance(0.5)
    assert clock.monotonic() == 15.5


def test_fake_clock_monotonic_never_goes_back():
    clock = FakeClock(wall=T0, mono=0.0)
    clock.advance(30)
    with pytest.raises(DomainError) as err:
        clock.advance(-1)
    assert err.value.code is ErrorCode.CLOCK_ERROR
    with pytest.raises(DomainError) as err:
        clock.advance_to(T0 + timedelta(seconds=-5))
    assert err.value.code is ErrorCode.CLOCK_ERROR
    # 失败操作不改动状态。
    assert clock.monotonic() == 30.0


def test_fake_clock_advance_to_and_set_wall():
    clock = FakeClock(wall=T0, mono=1.0)
    clock.advance_to(T0 + timedelta(seconds=60))
    assert clock.monotonic() == 61.0
    clock.set_wall(T0)
    assert clock.now() == T0
    # set_wall 只改墙上时间，不动单调时间。
    assert clock.monotonic() == 61.0


def test_deadline_pending_then_due_after_advance():
    clock = FakeClock(wall=T0, mono=0.0)
    deadline = 10.0  # monotonic 刻度
    assert schedule_state(clock.monotonic(), deadline) is DeadlineState.PENDING
    clock.advance(10)  # 瞬间前进 10 秒
    assert clock.monotonic() == 10.0
    assert schedule_state(clock.monotonic(), deadline) is DeadlineState.DUE


def test_wake_recomputation_uses_current_clock():
    """睡眠/挂起/时间跳变后必须用当前 Clock 重算，不信任旧线程 sleep 剩余时间。"""
    clock = FakeClock(wall=T0, mono=3.0)
    deadline = 10.0

    # 唤醒时用 FakeClock 当前时刻重算：3.0 < 10.0 → 仍 WAIT。
    assert recompute_after_wake(clock, deadline) is DeadlineState.PENDING

    # 模拟唤醒后时间已流失：FakeClock 前进到 11.0 → 到点。
    clock.advance(8)
    assert clock.monotonic() == 11.0
    assert recompute_after_wake(clock, deadline) is DeadlineState.DUE


def test_wake_contract_works_with_system_clock():
    clock = SystemClock()
    now_mono = clock.monotonic()
    deadline = now_mono + 3600.0
    assert recompute_after_wake(clock, deadline) is DeadlineState.PENDING
    assert recompute_after_wake(clock, now_mono) is DeadlineState.DUE


def test_clock_abstract_contract():
    # Clock 必须有 now() 与 monotonic()：通过子类化检查接口存在。
    assert hasattr(Clock, "now")
    assert hasattr(Clock, "monotonic")