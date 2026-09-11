"""可注入时钟与 sleep/wake 重建合同（T03）。

领域逻辑计算 deadline 一律使用 monotonic 时间；墙上时间与单调时间分开。
睡眠/挂起/系统时间跳变后，不能假设旧定时器剩余时间仍准确，
必须用当前 Clock 重新计算下一步动作（见 recompute_after_wake）。
"""

from __future__ import annotations

import time as _time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from enum import Enum

from core.errors import DomainError, ErrorCode


class Clock(ABC):
    """可注入时钟抽象：墙上时间与单调时间分开提供。"""

    @abstractmethod
    def now(self) -> datetime:
        """当前墙上时间；领域层不允许直接调用 datetime.now()。"""

    @abstractmethod
    def monotonic(self) -> float:
        """单调递增秒数，用于 deadline 计算；同一来源内不应倒退。"""


class SystemClock(Clock):
    """生产时钟，使用标准库 datetime / time.monotonic。"""

    def now(self) -> datetime:
        return datetime.now().astimezone()

    def monotonic(self) -> float:
        return _time.monotonic()


class FakeClock(Clock):
    """测试时钟：可任意前进/设墙，不依赖真实 sleep。"""

    def __init__(
        self,
        wall: datetime | None = None,
        mono: float = 0.0,
    ) -> None:
        self._wall = (
            wall if wall is not None else datetime(2000, 1, 1, tzinfo=timezone.utc)
        )
        self._mono = mono

    def now(self) -> datetime:
        return self._wall

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        """同时推进墙时间与单调时间；单调时间不可倒退，否则抛 CLOCK_ERROR。"""
        if seconds < 0:
            raise DomainError(
                ErrorCode.CLOCK_ERROR,
                f"FakeClock.advance 不能倒退 {seconds}s（单调时间不回退）",
            )
        self._wall = self._wall + timedelta(seconds=seconds)
        self._mono += seconds

    def advance_to(self, target: datetime) -> None:
        """推进到指定墙时间；目标早于当前墙时间视为时钟倒退，抛 CLOCK_ERROR。"""
        diff = (target - self._wall).total_seconds()
        self.advance(diff)

    def set_wall(self, wall: datetime) -> None:
        """直接改写墙时间（如模拟系统时间跳变）；不影响单调时间。"""
        self._wall = wall


class DeadlineState(str, Enum):
    """deadline 判定结果。"""

    PENDING = "pending"
    DUE = "due"


def schedule_state(now_mono: float, deadline_mono: float) -> DeadlineState:
    """纯函数：给定单调时间判断 deadline 是否到期。"""
    if now_mono >= deadline_mono:
        return DeadlineState.DUE
    return DeadlineState.PENDING


def recompute_after_wake(
    clock: Clock, checkpoint_deadline_mono: float
) -> DeadlineState:
    """睡眠/挂起/系统时间跳变后，用当前 Clock 重新计算下一步动作。

    不依赖旧线程 sleep 的剩余时间，唤醒后一律以当前 Clock 为准。
    """
    return schedule_state(clock.monotonic(), checkpoint_deadline_mono)