"""AI Relay B V3.0：OpenChamber 只读运行时与配置快照绑定工厂（T20-03）。

本模块把 T20-01 封板的只读 transport 与 T20-02 封板的只读 client 绑成一份
“配置冻结的只读运行时”：config_revision / base_url / directory / token /
timeout 全部在构造时从传入的 SettingsSnapshot 取值，构造后绝不回读当前
SettingsService 生效配置或任何草稿。

范围约束：
- 不接 controller、不接 UI、不写 OpenChamber，不消费 snapshot 的 session_id；
- 公开面只有四个只读 endpoint 的便利委托，无任何消息/写/控制能力；
- token 解析只复用 adapters.openchamber 已封板的 resolve_local_auth_token(...)，
  本模块不以任何方式重写认证规则或放宽认证边界；
- HTTP 唯一入口仍是 OpenChamberReadTransport（urllib），不引入第二套 HTTP client。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from adapters.openchamber import (
    OpenChamberReadClient,
    OpenChamberReadTransport,
    resolve_local_auth_token,
)
from core.domain import SettingsSnapshot

# 本地 token 环境变量名（resolve_local_auth_token 的 env_token 等价注入值）
ENV_CLIENT_TOKEN = "OPENCHAMBER_CLIENT_TOKEN"


@dataclass(frozen=True, slots=True)
class OpenChamberReadRuntime:
    """配置冻结的 OpenChamber 只读运行时（不可变）。

    config_revision / base_url / directory / client 全部来自构造时传入的
    SettingsSnapshot，之后不随 SettingsService 当前配置或草稿变化。

    便利方法只委托内部 OpenChamberReadClient：不重新解析 JSON、不复制
    endpoint 逻辑，也不对外输出统一的 connected/healthy/everything_ok 综合判定。
    list_sessions() / session_status() 使用构造时冻结的 directory，
    调用方不能临时换目录。
    """

    config_revision: int
    base_url: str
    directory: str
    client: OpenChamberReadClient

    def health(self):
        return self.client.health()

    def list_sessions(self):
        return self.client.list_sessions(self.directory)

    def session_status(self):
        return self.client.session_status(self.directory)

    def permission_state(self):
        return self.client.permission_state()


def build_openchamber_read_runtime(
    snapshot: SettingsSnapshot,
    *,
    env: Mapping[str, str] | None = None,
    settings_path: str | os.PathLike | None = None,
    opener: object = None,
) -> OpenChamberReadRuntime:
    """从一份 SettingsSnapshot 构建只读运行时。

    取值唯一来源是传入的 snapshot（url / directory / revision / http 超时），
    构造后不再从当前配置或草稿读取。token 仅复用 resolve_local_auth_token(...)：
    - env 未传时读 os.environ，测试注入 mapping 不依赖真实环境；
    - 非精确白名单 host 一律解析为 None 且不读取 desktop settings；
    - opener 只透传给现有 OpenChamberReadTransport（测试注入）；
      Factory 构造本身不产生任何 HTTP。
    """
    base_url = snapshot.config.openchamber.url
    directory = snapshot.config.openchamber.directory
    config_revision = snapshot.revision

    env_source = os.environ if env is None else env
    env_token = env_source.get(ENV_CLIENT_TOKEN) if env_source is not None else None
    token = resolve_local_auth_token(
        base_url, env_token=env_token, settings_path=settings_path
    )

    # Timeout 绑定：adapters.openchamber 的只读 transport 使用单一 urllib timeout。
    # 本轮只绑定 read_timeout_seconds；不宣称已分别实现 connect/read 双阶段超时，
    # 也不对 connect_timeout_seconds 做 min/max/相加等推测性替换。
    timeout = snapshot.config.http.read_timeout_seconds
    transport = OpenChamberReadTransport(
        base_url, token=token, timeout=timeout, opener=opener
    )
    client = OpenChamberReadClient(transport)
    return OpenChamberReadRuntime(
        config_revision=config_revision,
        base_url=base_url,
        directory=directory,
        client=client,
    )