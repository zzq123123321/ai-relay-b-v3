"""T14 UI 壳层不可变快照（view-model）。

数据流向：

    DB / Controller / Client
            ↓
        Snapshot
            ↓
           UI

只表达主窗壳当前需要展示和保持的状态，不做任何数据查询；
本模块禁止 import Qt 与任何业务模块（core/storage/adapters/app.controller 等）。

T14 只定义壳层所需最小集（规格 14.1 / 15.4 前瞻）：
active task 身份与活动状态、停止入口可用性、接收开关、连接新鲜度。
续接计数、阶段条、近期事件等由 T15 工作台引入，不在此过早扩张。

任务状态字符串与核心状态枚举名保持一致（大写枚举名），但为保持
view-model 纯净不引入 core 依赖，只在本模块集中声明当前使用的取值：
QUEUED / ACTIVE / BLOCKED / COMPLETED / FAILED / STOPPED_BY_USER。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

TASK_STATES = ("QUEUED", "ACTIVE", "BLOCKED", "COMPLETED", "FAILED", "STOPPED_BY_USER")


@dataclass(frozen=True, slots=True)
class ActiveTaskSnapshot:
    """当前活动任务身份与活动状态（不可变）。

    页面切换、导航选择、Fake 停止入口一律不得修改本对象；
    修改业务状态必须换发新的 Snapshot（见 ApplicationSnapshot.replace_snapshot）。
    """

    task_id: str | None = None
    title: str | None = None
    project: str | None = None
    state: str | None = None
    session: str | None = None


@dataclass(frozen=True, slots=True)
class ApplicationSnapshot:
    """主窗壳当前活动状态（不可变 view-model）。

    页面索引 / 导航选中态属于 UI 状态，不放在业务快照里；
    页面切换只改 UI 状态（MainWindow），不改本对象。
    """

    active_task: ActiveTaskSnapshot | None = None
    stop_available: bool = False
    receiving_enabled: bool = False
    connection_healthy: bool = False
    connection_source: str | None = None

    def replace_snapshot(self, **changes: object) -> "ApplicationSnapshot":
        """换发新快照（frozen 语义）；调用方负责构造完整一致性。"""
        return replace(self, **changes)


def fake_snapshot(
    *,
    task_id: str = "task-123",
    title: str = "测试任务：验证主窗壳页面切换保持活动任务",
    project: str = "ai-relay-b-v3",
    state: str = "ACTIVE",
    session: str | None = "rel_fake_session_0001",
    stop_available: bool = True,
    receiving_enabled: bool = True,
    connection_healthy: bool = True,
    connection_source: str = "Fake：未连接 OpenChamber/Reasonix",
) -> ApplicationSnapshot:
    """独立构造最小 Fake 快照（T14 壳层测试用；真实来源由 T47 接管）。

    snapshot 始终不可变：页面切换 / 停止入口点击不得改变对象或值。
    """
    active = ActiveTaskSnapshot(
        task_id=task_id,
        title=title,
        project=project,
        state=state,
        session=session,
    )
    return ApplicationSnapshot(
        active_task=active,
        stop_available=stop_available,
        receiving_enabled=receiving_enabled,
        connection_healthy=connection_healthy,
        connection_source=connection_source,
    )


def empty_snapshot(
    *,
    receiving_enabled: bool = True,
    connection_healthy: bool = False,
    connection_source: str | None = None,
) -> ApplicationSnapshot:
    """无活动任务的空快照（UI-A01 空状态）。停止入口不可用。"""
    return ApplicationSnapshot(
        active_task=None,
        stop_available=False,
        receiving_enabled=receiving_enabled,
        connection_healthy=connection_healthy,
        connection_source=connection_source,
    )