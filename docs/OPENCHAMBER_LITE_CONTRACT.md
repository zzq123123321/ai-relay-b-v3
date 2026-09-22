# OpenChamber Lite API 合同（L01-02R1）

> 任务：L01-02R1-20260923。本轮**只做静态确认**（源码/合同/已归档探测证据），不做任何真实 HTTP 探测。
> 静态证据来源：本机 OpenChamber Electron 安装包源码（app.asar 解包）。
>
> | 项 | 值 |
> |---|---|
> | 安装包 | `C:\Users\LocalUser\AppData\Local\Programs\@openchamberelectron` |
> | 版本 | `@openchamber/electron` 1.24.2（T19 探测时为 1.23.0，本机已自动升级） |
> | 服务端代码 | `resources/app.asar` → `node_modules/@openchamber/web/server/`（Express） |
> | 内置 OpenCode SDK | `@opencode-ai/sdk` 1.18.31（v1 `dist/` 为主，v2 `dist/v2/` 并存） |
> | OpenCode 进程 | `resources/opencode-cli/opencode.exe`（OpenChamber 托管，版本本轮未探测） |
> | 归档真实证据 | `D:\AIwork\ai_relay_b_v3\contracts\openchamber_contract.md`（T19 只读）、`openchamber_write_contract.md`（T21 写） |

## 0. 架构事实（静态，决定所有端点的解读）

OpenChamber 服务端对 `/api/*` 的通用处理是**代理到 OpenCode 服务器**：

- `lib/opencode/proxy.js:478`：`upstreamPath = requestUrl.startsWith('/api') ? requestUrl.slice(4) : requestUrl`（剥掉 `/api` 前缀转发）。
- `lib/opencode/proxy.js:696-697`：`app.use('/api', ...)` 先执行 `ensureOpenCodeApiPrefix()`，运行时自动探测上游 OpenCode 是否带 `/api` 前缀（`lib/opencode/network-runtime.js:97-103`），再转发。
- `lib/routing/routes.js:14-18`：`/api/session/:sessionId/{prompt_async,prompt,command}` 仅在 routing 特性可用时插入 body 改写（把 `openchamber/auto` 解析为真实模型），随后照常走 OpenCode 代理。

结论：`/api/session...`、`/api/session/status`、`/api/session/{id}/message`、`/api/session/{id}/prompt_async` 等**不是 OpenChamber 自实现端点，而是 OpenCode API 的代理入口**；`/health`、`/api/permission-auto-accept`、`/api/sessions/*`（snapshot/attention/view）、`/api/session-activity` 是 OpenChamber 自实现端点。

认证边界（沿用 T19 合同 + 适配器 `adapters/openchamber.py` 一致实现）：

- 本地 Bearer token 仅对精确白名单主机 `{localhost, 127.0.0.1, ::1}` 自动携带（`desktopLocalClientToken` 来自 `~/.config/openchamber/settings.json`，或 `OPENCHAMBER_CLIENT_TOKEN`）。
- `/health` 无认证即可成功（T19 实测 200）。

## 1. 服务健康检测

```text
method:  GET
path:    /health
query:   无
body:    无
auth:    无（实测 200 未认证）
response: JSON dict，固定键 status="ok"、timestamp、openchamberVersion、runtime、compatibility，
          另含 getHealthSnapshot() 动态键（T19 实测 32 键，含 openCodePort、openCodeRunning、
          openCodeApiPrefix、openCodeApiPrefixDetected、isOpenCodeReady 等）
evidence/source:
  静态  lib/opencode/core-routes.js:209-220（1.24.2 安装包）
  实测  T19 探测（2026-09-13，OpenChamber 1.23.0，HTTP 200，32 键，
        contracts/openchamber_contract.md §3）
certainty: HIGH（实测 + 静态双重确认；键集随版本动态，只依赖 status/openchamberVersion/openCodeRunning/isOpenCodeReady）
```

辅助：`GET /api/opencode/health`、`GET /api/opencode/version`（`lib/opencode/routes.js:380/402`）可查托管 OpenCode 子进程状态，需要本地 token。

## 2. 当前激活会话（UI 正在查看的 session）

```text
结论: ACTIVE_SESSION_API_NOT_FOUND
```

已逐一排查 1.24.2 安装包服务端全部路由（约 350 条 `app.get/post/put/delete/patch` 注册），**不存在**任何公开 HTTP 接口能返回“当前 UI 正在激活/查看的 session”。最接近的候选及其不可用原因（静态源码证据）：

| 候选 | 源码位置 | 为什么不满足 |
|---|---|---|
| `GET /api/sessions/snapshot` → `attentionSessions[id].isViewed` | `lib/notifications/routes.js:290`；`lib/opencode/session-runtime.js:309-323` | `isViewed` 是**聚合布尔**（`viewedByClients.size > 0`），丢失“哪个客户端”和“当前”语义；`view` 由客户端（UI）经 `POST /api/sessions/:id/view` 写入（`session-runtime.js:262`），是 latch 状态（unview 前一直为 true），多个 session 可同时 isViewed=true；服务端重启后内存状态全丢（`session-runtime.js:439-450`） |
| `GET /api/session-activity` → 每 session `phase`（busy/idle） | `lib/notifications/routes.js:285`；`session-runtime.js:338` | 表达的是**执行活跃度**，不是 UI 焦点 |
| `GET /api/sessions/status`（OpenChamber 自实现） | `lib/notifications/routes.js:302` | 同上，status + pending 阻塞请求，无 UI 焦点 |
| OpenCode `GET /api/session?directory=...` 列表的 `updatedAt` | 代理到 OpenCode `/session` | 属于被禁止的“最后更新时间最大”猜测 |

推论（明确标注）：Lite UI 若要拿到“当前激活 session”，只能依赖 OpenChamber 内部客户端状态（前端 bundle 中的 `data-active-session-tab` 等纯 UI 属性），该状态未通过任何服务端 API 暴露。

## 3. 指定 session 消息读取

```text
method:  GET
path:    /api/session/{session_id}/message
query:   directory=<URL 编码的工作目录>
body:    无
auth:    loopback 本地 Bearer
response: JSON list，每项 {info:{id, role, parentID, ...}, parts:[{type:"text", text, ...}]}
evidence/source:
  实测  T21 真实写合同：SUPPORTED_REAL，四条件归属判定基于此读回
        （contracts/openchamber_write_contract.md §7，GET 读回 + parentID 归属 VALID）
  静态  @opencode-ai/sdk 1.18.31 v1 `session.messages` → GET /session/{id}/message（代理剥前缀后同形）；
        OpenChamber proxy.js:478 前缀剥离 + 目录 query realpath 规范化（proxy.js:108-128）
certainty: HIGH（真实读回实证）
注意: 内置 v2 SDK 另有分页变体（limit/order/cursor 查询参数，url 同为 /api/session/{sessionID}/message），
     本机运行中的 OpenCode 是否支持 = UNVERIFIED（本轮不探测）。
```

## 4. prompt_async 精确发送

```text
method:  POST
path:    /api/session/{session_id}/prompt_async
query:   directory=<URL 编码的工作目录>
body:    仅 5 个已实证顶层键：
         { "messageID": "msg_<客户端生成>",
           "model": {"providerID": "...", "modelID": "..."},
           "agent": "...", "variant": "default",
           "parts": [{"type": "text", "text": "<prompt>"}] }
         （delivery 字段不在已实证键集内，不发送）
auth:    loopback 本地 Bearer
response: HTTP 204 = request accepted；204 != completed（无归属完成结果）；
         4xx = rejected；超时/连接失败/5xx = UNKNOWN（可能已投递，禁止盲目重发）
evidence/source:
  实测  T21：HTTP 204 accepted，SUPPORTED_REAL（contracts/openchamber_write_contract.md §5/§6）
  静态  v1 SDK `session.promptAsync`（POST /session/{id}/prompt_async）；
        OpenChamber `lib/routing/routes.js:14-18` AUTO_SESSION_PATHS 改写注册；
        服务端内部同款调用（lib/openchamber-sessions/routes.js:201）
certainty: HIGH（真实副作用 + 读回实证）
```

## 5. compact 当前 session

```text
候选 A（v1，本安装包 web 构建主用 SDK 1.18.31 的 v1 面）:
  method:  POST
  path:    /api/session/{session_id}/summarize
  query:   （上游为 /session/{id}/summarize，OpenChamber 剥 /api 后代理；directory 按惯例可携带）
  body:    v2 SDK 文档同形：{"providerID": "...", "modelID": "...", "auto": bool}
  语义:    "Summarize the session" / "Generate a concise summary of the session
           using AI compaction to preserve key information"
  evidence/source: @opencode-ai/sdk 1.18.31 v1 `session.summarize`（dist/gen/sdk.gen.js:340，
           url: "/session/{id}/summarize"）；v2 文档同路径（dist/v2/gen/sdk.gen.js:2420）

候选 B（v2 面，SDK dist/v2 内）:
  method:  POST
  path:    /api/session/{session_id}/compact
  query:   无
  body:    无（仅 path 参数 sessionID）
  语义:    "Compact a session conversation."
  evidence/source: @opencode-ai/sdk 1.18.31 v2 `compact()`（dist/v2/gen/sdk.gen.js:3458-3465，
           url: "/api/session/{sessionID}/compact"）

auth:    loopback 本地 Bearer
response: 本轮未实证（未发请求）
evidence/source:
  静态  OpenChamber 服务端**没有**自实现的 /compact 或 /summarize 路由（全量路由清单核对），
        两个候选都经由 /api/* 代理落到 OpenCode 服务器（proxy.js:478 前缀剥离）；
        OpenChamber 自身仅消费 OpenCode 的 `session.compacted` 事件
        （lib/context-obligatory/runtime.js:156）
certainty: MEDIUM（两条路径均有内置 SDK 源码实证，均未在本机运行中的 OpenCode 上真实调用）
```

关于 G4 挂起项（裸 `/compact` 走 send 端点 vs 程序化 compact 路径）：静态证据表明程序化路径在 v1 SDK 中就是 `POST /session/{id}/summarize`，v2 SDK 中另有 `POST /api/session/{sessionID}/compact`；两条是**不同 OpenCode 版本面的同语义端点**，不是同一端点的两种写法。本机 `opencode.exe` 实际接受哪一条，取决于其版本——**留待后续真实 probe 裁决，本轮不下结论**。禁止使用 `POST /api/session/{id}/compact` 之外的自创路径。

## 6. 本轮范围外（与 T19/T21 一致，保持 DEFERRED）

session create、stop/interrupt、permission approve/reject、delete/archive、delivery 语义、
active session 的可靠接口（结论 ACTIVE_SESSION_API_NOT_FOUND）。

## 7. 卫生

- 本文件不含任何真实 token / Authorization 值 / session id / 用户数据；示例仅用形态占位。
- `desktopLocalClientToken`、`OPENCHAMBER_CLIENT_TOKEN`、`Bearer` 仅作为字段概念出现。