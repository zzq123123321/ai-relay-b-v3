# LITE_MIGRATION.md — Lite 迁移清单（L01-01 交付）

- 生成：2026-09-23，B 端（MSI 笔记本）
- 依据：`AI_Relay_B_Lite_修正方案_v1.0.docx`（已完整阅读）
- 新项目根：`D:\AIwork\ai_relay_b\最新开发01`（当前**非 Git 仓库**，尚无业务源码）
- 旧代码参考（只读）：`D:\AIwork\ai_relay_b_v3`，`master` @ `d0ab381`（= origin/main 基线），工作树有未提交 T23 遗留（`ui/settings_page.py` 已改、`app/openchamber_binding_commit.py` 等未跟踪），本轮只读不碰

## 一、Lite 最终功能边界（只围绕以下能力）

1. A端连接（ClipLink 通道，显示 peer/RTT，只读状态文件）
2. 大模型连接（OpenChamber 服务检测 + 服务延迟 + 模型首响应）
3. 当前激活会话（只显示/获取，不创建、不轮换）
4. 自动包装（收到 A端任务即包装，只认 `{content}` 占位符）
5. 手动发送（原样发送，不套包装）
6. 项目总指挥模板（弹窗编辑，默认模板，只存 default/last 两个值）
7. 模型中断自动续接（RECOVER_CHECK → 最多一次 resume_prompt，与 A端完全解耦）
8. 手动/自动压缩（OpenChamber 原生 compact；自动 = 完成 N 个任务后压缩）
9. 三类延迟显示（A端网络延迟 / 大模型服务延迟 / 模型首响应）
10. ClipLink 联动（本机 status.json 松耦合，不复制 TCP/心跳实现）
11. AI_RELAY_COMPLETE（命中即触发自动联动停止）

## 二、KEEP_AND_SIMPLIFY（保留并精简，从 v3 迁移思想/代码）

| 项 | v3 来源 | Lite 处理 |
|---|---|---|
| OpenChamber 只读 transport（health / list_sessions / session_status / permission_state、base_url 校验、有界读、非重定向写） | `adapters/openchamber.py` 的 `OpenChamberReadTransport/ReadClient` 等 | 收敛进新 `openchamber_client.py`，一个类 |
| OpenChamber 单次发送 transport（pre-send 快照、send_once、status 判定） | `OpenChamberPromptAsyncTransport` | 精简迁移：只保留"发送一次 + 读状态"，删对账/重试语义 |
| OpenChamber API 合同知识 | `contracts/openchamber_contract.md`、`contracts/openchamber_write_contract.md` | 文档直接沿用，供 L01 client 开发参照 |
| HTTP 基础（timeout、异常类型、错误提示） | `adapters/openchamber.py`、`core/errors.py` | 保留最低限度错误处理 |
| 剪贴板收发的基本思想（A端经剪贴板给任务、结果写回剪贴板） | `adapters/clipboard.py` | Lite 中 ClipLink 负责同步，AI Relay 只读状态文件 + 读写本机剪贴板，`cliplink_bridge.py` 内保留最简剪贴板访问 |
| 协议解析的可用部分（识别 A端任务文本/回传格式） | `core/protocol_v1.py` | 只保留 RAW 任务识别与 `{content}` 包装拼接，删 exchange 身份/round 头等重型字段 |
| 可注入时钟（轮询/冷却计时可测） | `infra/clock.py` | 直接沿用 |
| pytest 测试组织方式（fake transport 假接口先测） | `tests/test_t20_*`、`adapters/fake_executor.py` 的思想 | L01 起沿用"假接口先测，不改 UI" |
| 主题/基础控件 token 思想（若 L05 UI 需要七态控件样式） | `ui/theme_tokens.py`、`ui/theme.qss` | 单窗口 UI 只需子集，按需取用 |

## 三、REWRITE（重写）

| 项 | v3 现状 | Lite 目标 |
|---|---|---|
| UI | 多页 Dashboard/Settings/Presenter（`ui/dashboard.py` 20K、`ui/settings_page.py` 37K、`ui/status_presenter.py` 29K、`ui/task_detail.py`、`ui/task_records.py`、`ui/navigation.py`） | 单窗口 `ui/main_window.py`：两张状态卡（A端连接/大模型连接）+ 延迟 + 当前会话 + 按钮区 + 手动发送框 + 日志 + 开始监听 |
| Controller | `app/controller.py` + `app/commands.py` 多命令通道 | 一个 `controller.py`：只含 A端 Relay 链 + 手动发送 + 结果回传 |
| 状态机 | `core/domain.py`（22K 重型状态/身份） | 只保留 7 状态：IDLE / READY_TO_SEND / RUNNING / MODEL_OFFLINE / RECOVER_CHECK / RESUME_SENT / WAIT_RETURN |
| 配置 | `core/settings_service.py` + `storage/settings_store.py`（草稿/快照/revision/CAS） | 一个 `config.py` + config.json 覆盖保存 |
| 模型监控 | 分散在 `core/dispatch.py`（27K）、`core/scheduler.py`、`app/openchamber_read_runtime.py` 等 | 独立 `model_watchdog.py`：进展检测 + 恢复观察 + 一次 resume_prompt |
| 入口 | `main.py`（拉多 worker/托管资源） | `main.py` 单进程：UI + 两条链路 |
| ClipLink 桥接 | v3 无（A端链路靠剪贴板命令通道） | 新增 `cliplink_bridge.py`：读 `%LOCALAPPDATA%\ClipLink\status.json`，写 `%LOCALAPPDATA%\AIRelayLite\status.json` |
| 会话压缩 | v3 无 compact 实现（仅文档字符串提及） | `openchamber_client.py` 新增 compact 调用（L01 交付） |

## 四、DELETE（删除，不迁入 Lite）

| 项 | v3 来源 |
|---|---|
| SQLite 全库（tasks/operations/results/leases/schema、`storage/database.py` 等 8 文件 + `001_initial.sql`） | 全部删除，Lite 无数据库 |
| Operation/Attempt/Lease/Binding（`storage/operation_store.py`、`lease_store.py`、`project_binding_store.py`、`app/openchamber_binding_commit.py`） | 删除 |
| UNKNOWN 对账 / CREATE_SESSION（`adapters/openchamber_create_session.py`、`openchamber_reconciliation.py`、`openchamber_create_reconciliation.py`、`app/openchamber_create_session_coordinator.py`、`openchamber_create_unknown_reconciler.py`、`openchamber_unknown_reconciler.py`） | 删除，Lite 不自动创建会话 |
| Scheduler / FIFO 调度 / 多 worker（`core/scheduler.py`、`core/dispatch.py`、`core/ingress.py` 重型部分） | 删除 |
| ResultCommit / Outbox / 权威提交（`core/result_commit.py`、`core/delivery.py`） | 删除，改为剪贴板直写回传 + WAIT_RETURN |
| Settings 快照/revision/CAS 体系（`core/settings_service.py`、`storage/settings_store.py`） | 删除，改 config.json |
| FIXED_SESSION / PROJECT_ROTATING / ProjectBindingStore | 删除，只认当前激活会话 |
| Reasonix / TargetExecutor / 多执行端抽象（`core/domain.py` 中 executor 身份、T43-46 路线） | 删除，只支持 OpenChamber |
| 任务记录/详情页、日志中心、诊断导出、托盘、llm-monitor 桥接（`ui/task_records.py`、`ui/task_detail.py` 等 T16/T48-T52 能力） | 删除，主界面只留简单日志 |
| v3 根目录散落的 T22/T23 过程文件（`.t22_*`、`.t23_*`、`.recovery/`） | 不迁入，留在旧仓库 |

## 五、新旧目录审计结论（供 A 端决策下一轮策略）

1. `最新开发01` 目前**不是 Git 仓库**，也没有业务源码，只有：Lite 方案文档（docx+pdf）、V3.0 开发资料包（60 张任务卡 + 规格 + 模板 + 验收）、T01 侦察遗留（docs/、evidence/T01/）、一个无关文件 `axis-video.lan.ts`（15KB，疑似误放，建议 A 端裁决去留）。
2. 注意：本目录嵌套在旧 V2 仓库 `D:\AIwork\ai_relay_b`（`feature/dual-executor` @ `8461c23`）工作区内（T01 时发现的"未跟踪项"就是本目录）。若在此 `git init` 独立建库需先确认外层仓库处理策略（T01 报告已提示，DEC-L01 备注）。
3. v3 项目源码集中在 `app/ core/ adapters/ storage/ ui/ infra/` + 41 个测试文件，其中 OpenChamber 读写合同（contracts/ 两篇 md）与 transport 代码是 Lite 最有价值的可迁移资产；其余重型状态机/存储/对账全部落入 DELETE。
4. v3 的 T23 未提交工作（`ui/settings_page.py` 修改、`app/openchamber_binding_commit.py` 未跟踪）按 A 端指示保留不动，Lite 不继承它。

## 六、建议的新 Lite 结构（与方案第 9 节一致，落在本目录）

```text
ai_relay_b_lite/
├─ main.py
├─ config.py
├─ openchamber_client.py
├─ cliplink_bridge.py
├─ controller.py
├─ model_watchdog.py
├─ ui/
│  └─ main_window.py
└─ tests/
   ├─ test_openchamber.py
   ├─ test_controller.py
   ├─ test_watchdog.py
   └─ test_cliplink_bridge.py
```

L01 阶段验收：openchamber_client 对假/本机接口测通"服务检测 + 当前激活会话 + 发送 + 读结果 + compact"，不改 UI。

## 七、实现进度（B 端接力交付记录）

- **L04-02**（2026-09-23）：功能边界 #1/#10（A端连接，只读 status.json）已实现——
  - 新增 `cliplink_status.py`：只读 `%LOCALAPPDATA%\ClipLink\status.json`（BOM/损坏/缺失一律收敛，不抛），并把 `updated_at` 用于假连接判定——`status==connected` 静默超过 3×ClipLink 心跳(30s) 判为"已断开(过旧)"；状态/对端/RTT 映射成 UI 文案。
  - `main.py` 用 GUI 线程 `QTimer`（2s，非后台线程框架）轮询，经 `MainWindow.set_a_connection` 刷新"A端连接"卡；`paused` 不按假连接降级、也不等同 `connected`。
  - 模块名 `cliplink_status.py`（本轮只读展示），区别于"建议结构"里的 `cliplink_bridge.py`（后续剪贴板自动中继的读+写桥，尚未建）。
  - 测试：`tests/test_cliplink_status.py`（纯）+ `tests/test_ui_wiring.py` 增 4 例接线；全量 **109 passed**。