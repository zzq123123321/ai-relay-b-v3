# OpenChamber Read-Only Contract

> 本文件固化 T19-B1 真实只读探测证据为 T20 可消费的合同。
> 只记录已观察事实；推论明确标注；未验证一律 UNVERIFIED。

## 1. Provenance

| 项 | 值 |
|---|---|
| Probe implementation commit | `9ea7bb59ae097df8ba504cc2951d138c698da8f3` |
| Evidence source | `.recovery/t19_probe_evidence.json`（本地文件，不纳入版本库） |
| Evidence type | local read-only GET probe |
| Evidence generated_at | `2026-09-13T19:54:54.123368+08:00` |
| Probe target base URL | `http://127.0.0.1:57123`（loopback） |
| Queried directory | `D:\AIwork\ai_relay_b_v3` |
| session_id supplied | 否（message endpoint 未发起请求） |

## 2. Evidence Rules

- **事实** = B1 实探支持（HTTP 状态、shape、hash 等来自 evidence 文件）。
- **推论** = 明确标注（以“推论：”开头）。
- **未验证** = `UNVERIFIED`，不因旧项目实现或文档描述而升级为 SUPPORTED。
- 本合同时效范围：仅覆盖上述 generated_at 快照；目录会话是动态状态，不得外推。

## 3. Endpoint Contract

Endpoint 表列为固定格式。字段取值全部来自 evidence，未做改写。

| Endpoint | Method | Capability | Auth observed | HTTP observed | Response shape | Pagination evidence | Missing semantics | Error semantics | Sample hash |
|---|---|---|---|---|---|---|---|---|---|
| `/health` | GET | SUPPORTED | none（未认证即成功） | 200 | `dict; top_keys=32: [status, timestamp, openchamberVersion, runtime, compatibility, serverId, openCodePort, openCodeRunning, openCodeSecureConnection, openCodeAuthSource, openCodeApiPrefix, openCodeApiPrefixDetected, isOpenCodeReady, lastOpenCodeError, lastOpenCodeLaunchDiagnostics, lastOpenCodeHealthFailure, lastManagedOpenCodeProcess, lastOpenCodeRestartDiagnostics, opencodeBinaryResolved, opencodeBinarySource, opencodeLaunchBinary, opencodeLaunchArgs, opencodeLaunchWrapperType, opencodeViaWsl, opencodeWslBinary, opencodeWslPath, opencodeWslDistro, nodeBinaryResolved, bunBinaryResolved, desktopNotifyEnabled, planModeExperimentalEnabled, apiOnly]` | none observed | 11 个 `null` 语义（见 §7） | none（成功） | `109fad8e5ed67ca369f467bfb7a5771c5aeb8440844bf489c376a76659e35239` |
| `/api/session?directory=...` | GET | SUPPORTED | loopback local token（Bearer） | 200 | `list; length=0; item_type=null; item_top_keys=null` | none observed | 空列表 = 本次目录未观察到 session（非字段缺失） | none（成功） | `4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945` |
| `/api/session/status?directory=...` | GET | SUPPORTED | loopback local token（Bearer） | 200 | `dict; top_keys=[]` | none observed | 空 dict = 本次目录无可观察 status entry | none（成功） | `44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a` |
| `/api/session/{session_id}/message?directory=...` | GET | UNVERIFIED | —（未发起请求） | — | — | UNVERIFIED | UNVERIFIED（MISSING_INPUT：未提供 session-id） | UNVERIFIED / `MISSING_INPUT` | `—`（无伪造 hash） |
| `/api/permission-auto-accept` | GET | SUPPORTED | loopback local token（Bearer） | 200 | `dict; top_keys=[sessions, revision]` | none observed | none observed | none（成功） | `ade11b8609f1ef193e2e20afd72b2d55e12089c26fea19d98ca5f5b15f147d05` |

### 3.1 逐端点附加说明

- `/health`：observed `openchamberVersion=1.23.0`，`compatibility.capabilities` 六项：`api.health.v1`、`api.runtime-url.v1`、`api.raw-file.v1`、`realtime.sse.v1`、`realtime.websocket.global-events.v1`、`terminal.websocket.v1`。其回调仅表示**只读健康状态可观察**；`/health` 返回 200 不得推出任何 API 认证结论。
- `/api/session?directory=...`：本次查询目录返回 `[]`。语义仅为 **“本次查询目录未观察到 session”**，禁止写成“OpenChamber 没有 session”。空目录结果只对本次 directory 有效，不构成全局断言。
- `/api/session/status?directory=...`：本次返回 `{}`（无可观察 status entry）。不得伪造 `status` / `sessionId` 字段。
- `/api/session/{session_id}/message?directory=...`：B1 未发送该请求（未提供 session-id）。capability=`UNVERIFIED`，error/evidence=`MISSING_INPUT`，sample hash=`—`。**不得根据旧项目代码升级为 SUPPORTED。**
- `/api/permission-auto-accept`：observed `sessions` 映射（本次快照 **84 条**，值均 bool）+ `revision=86`。只表示 **只读自动接受权限状态可观察**；禁止写成“可以 approve”或“权限工作流已实现”。合同不列任何真实 session ID。

## 4. Authentication Boundary

- 自动本地 Bearer **只允许**发往：
  - `localhost`
  - `127.0.0.1`
  - `::1`
- 非 loopback（remote host）：
  - `OPENCHAMBER_CLIENT_TOKEN` **不自动发送**
  - `desktopLocalClientToken` **不自动发送**
- 本轮观察事实：
  - `/health` **无认证即可成功**（observed HTTP 200）。
  - loopback `/api/*` **携带本地认证成功**（observed HTTP 200）。
- 反向外推禁止：本证据只证明“带 loopback 本地认证的 API 请求被接受”，**不证明“缺少认证时必然失败”的完整服务端策略**。

## 5. Seven Independent Runtime States

七层为独立状态，禁止合成/合并为单层概括判断（例如笼统表述连接完全健康）。

| # | State | 当前证据支持 |
|---|---|---|
| 1 | `service_reachable` | `YES`（/health 200 observed） |
| 2 | `api_authenticated` | `YES`，仅针对本轮成功的 authenticated read-only probes |
| 3 | `capability_available` | **按 endpoint 单独判定**（见 §3）：health/session list/session status/permission=SUPPORTED；message=UNVERIFIED |
| 4 | `session_exists` | `NO`（本次 queried directory 未观察到 session；限本次快照） |
| 5 | `session_attribution_valid` | `UNVERIFIED` |
| 6 | `execute_accepted` | `UNVERIFIED` |
| 7 | `execution_progressing` | `UNVERIFIED` |

- `service_reachable` 与 `api_authenticated` 互不推出。
- 第 5–7 层本轮不探测（涉及 send/execute），一律 UNVERIFIED。

## 6. Acceptance Mapping

### O01

- 当前支持只读证据：session list / status 能观察 empty/missing。
- 固定不变量：**FIXED session 不存在**（范围限定：本次 queried directory 未观察到 session；不泛化为全局无 session）
  - → 不创建 replacement
  - → 不换 session
  - → 不发送
- T19 只观察，不执行该策略（运行时策略行使 deferred）。

### O04

- `UNVERIFIED`：message endpoint 尚未真实探测。
- 不得宣称可从完整回答判断完成。

### O10

- `permission-auto-accept` 只读状态 = `SUPPORTED`（observed）。
- `WAITING_USER` 状态机、`approve`/`reject`、budget 行为：全部 deferred。

### O11

- `ATTRIBUTION_AMBIGUOUS` 运行时判定 = `UNVERIFIED`。
- message id / role / parent id / timestamps 的实际可观察性：仍 `UNVERIFIED`（message endpoint 未实探）。
- 禁止“最后一条消息就是结果”之类的归属规则。

## 7. Pagination and Missing Semantics

分类型语义，禁止合并为单一“missing”：

| 观测 | 语义 |
|---|---|
| `[]`（session list） | 本次目录未观察到 session；不是字段缺失 |
| `{}`（session status） | 本次目录无可观察 status entry；不是字段缺失 |
| `null`（health 部分字段） | 字段值缺失/未设置；observed 11 处：`lastOpenCodeError`、`lastOpenCodeLaunchDiagnostics.wrapperType`、`lastOpenCodeHealthFailure`、`lastManagedOpenCodeProcess`、`lastOpenCodeRestartDiagnostics`、`opencodeLaunchWrapperType`、`opencodeWslBinary`、`opencodeWslPath`、`opencodeWslDistro`、`nodeBinaryResolved`、`bunBinaryResolved` |
| `404` | 本轮未观察到（全部 endpoint 均 200）；语义预留为 `NOT_FOUND_OR_UNSUPPORTED` |
| missing input（message） | 未提供 session-id，B1 未发请求，`MISSING_INPUT` |

Pagination：本轮 5 个端点**均未观察到**任何分页字段（`none observed`）；不猜测 `cursor` / `nextPage` / `hasMore`。message 行 pagination=`UNVERIFIED`。

## 8. T20 Consumer Boundary

T20 最小只读 adapter 合同建议（本文件不写代码）：

| 逻辑能力 | 方法示意 | Capability |
|---|---|---|
| health | `health()` | **SUPPORTED** |
| list sessions(directory) | `list_sessions(directory)` | **SUPPORTED** |
| session status(directory) | `session_status(directory)` | **SUPPORTED** |
| permission auto-accept state | `permission_state()` | **SUPPORTED** |
| messages(session_id, directory) | `messages(session_id, directory)` | **UNVERIFIED** |

共识约束：

- 未验证能力不得静默当作 supported。
- 消息端点实现 `messages(...)` 前必须先行实探（补传 session-id），否则保持未验证状态。
- transport 必须继承本合同策略：
  - same transport / auth policy
  - loopback-only local token
  - no token logging

## 9. Deferred Write / Control APIs

以下接口 T19 **不裁决**，全部列为本合同 OUT OF SCOPE / DEFERRED：

- send
- session create
- stop
- retry
- permission approve
- compact

### Compact

本文件**不判定** compact 的最终控制路径（裸 `/compact` 通过 send 端点发控制命令，与程序化 compact 路径并存待复核），不做任何一方最终结论（例如断言仅存在单一路径，或断言某个路径一定存在/一定不存在）。compact 的唯一处理方式：

> T19 不裁决 compact 的最终控制路径；naked `/compact` 与程序化 compact 路径留给 **G4** 独立复核。

（G4 待复核冲突项仍挂起：裸 `/compact` 走 send 端点发控制命令，与程序化 `POST /api/session/{sessionID}/compact` 两条路径并存待复核。）

## 10. Hygiene

- 本文件允许出现字段名/规则字符串：`desktopLocalClientToken`、`OPENCHAMBER_CLIENT_TOKEN`、`Authorization`、`Bearer`。
- 本文件不含任何真实凭据值（任何本地 token 实值含前缀型凭据、private key、`desktopUiPassword` 实值、真实 Authorization header）。
- 本文件不含 permission endpoint 返回的真实 session ID（仅记录 count/structure）。