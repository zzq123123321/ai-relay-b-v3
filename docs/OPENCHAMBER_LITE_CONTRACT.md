# OPENCHAMBER_LITE_CONTRACT.md — Lite 所需 OpenChamber 最小 API 合同

- 生成：2026-09-23，L01-02（B 端只读勘察）
- 证据来源（三者区分，不混写）：
  1. **本轮真实只读实测**：OpenChamber v1.24.2，`http://127.0.0.1:57123`，OpenCode 上游 `:64076`，loopback Bearer（`~/.config/openchamber/settings.json` → `desktopLocalClientToken`）
  2. **v3 合同**：`D:\AIwork\ai_relay_b_v3/contracts/openchamber_contract.md`（T19 只读）、`openchamber_write_contract.md`（T21 真实写，仅引用）
  3. **源码证据**：`D:\AIwork\openchamber-source`（当前工作树，Node/TS；含 `@opencode-ai/sdk@1.18.29` 锁定版本核验）

## 合同总表

| # | 能力 | method | path | query/body | auth | response shape | Lite 用途 | 已只读实测 |
|---|---|---|---|---|---|---|---|---|
| A | 服务健康检测 | GET | `/health` | 无 | 无（未认证即成功） | JSON dict，32 键：`status, timestamp, openchamberVersion, openCodeRunning, openCodePort, isOpenCodeReady, ...`（完整键表见 v3 合同 §3） | 大模型连接卡片"服务延迟"数据源（`probe()`） | **是**（200，46 ms，v1.24.2，openCodeRunning=true） |
| B | 当前激活会话 | **无服务端接口**（事实，见下节） | — | — | — | — | "获取当前激活会话"按钮 | 见下节 |
| C | 当前 session 消息读取 | GET | `/api/session/{session_id}/message` | `directory=<urlencoded 绝对路径>`（必填，值取自会话自身 `directory` 字段） | loopback Bearer | JSON list：`[{info:{id, sessionID, role, time, agent, model, parentID...}, parts:[{type:"text", text}]}]`（本轮实测 200；schema 与 v3 T21 一致） | 读结果 / watchdog 进展检测 | **是**（200） |
| D | 向指定 session 发 prompt | POST | `/api/session/{session_id}/prompt_async` | `directory=<urlencoded>` | loopback Bearer | 204 = accepted（**≠completed**）；body（v3 T21 真实发送形状）：`{messageID, model:{providerID, modelID}, agent, variant, parts:[{type:"text", text}]}` | A端任务 / 手动发送 / resume_prompt | 否（本轮禁止真实 POST；引用 v3 T21 SUPPORTED_REAL） |
| E | compact 当前 session | POST | `/api/session/{session_id}/summarize` | `directory=<urlencoded>`（**可选**；不带时上游按客户端 `x-opencode-directory` 头或 opencode 进程 cwd 解析） | loopback Bearer | 200 + JSON `true`；body：`{providerID, modelID}`（UI 实际发送形状） | "立即压缩当前会话" / 自动压缩 | 否（本轮禁止 POST；源码合同，见下） |

## B. "当前激活会话"事实结论（重点）

**事实：OpenChamber 没有"UI 当前激活会话"的服务端接口。** 依据源码（`D:\AIwork\openchamber-source`）：

1. UI 激活会话是**纯前端状态**：
   - Zustand store `useSessionUIStore.currentSessionId / currentSessionDirectory`（`packages/ui/src/sync/session-ui-store.ts:349-350`，内存）
   - URL query 参数 `?session=<id>`（`packages/ui/src/lib/router/types.ts:42`；双向同步 `useRouter.ts:112-171`）
   - localStorage `oc.lastSession.v1`（`packages/ui/src/sync/last-session-cache.ts:8`；注释明确"startup-continuity context ONLY"，仅冷启动恢复用，非权威状态）
2. 切换会话**不会**向 server 发任何 HTTP/WS 上报。前端唯一的 session 相关上报是发消息时 `POST /api/sessions/{id}/message-sent`（`session-ui-store.ts:302-305`）；`POST /api/sessions/:id/view|unview`（写"已读"）的调用方是**原生 App**，Web UI 从不调用（`lib/notifications/APNS.md:56-59`）。
3. server 端最接近的只读快照：
   - `GET /api/sessions/attention` → 每会话 `{needsAttention, lastUserMessageAt, status, isViewed}`（本轮实测 200）。但 `isViewed` 只反映原生 App 的 `/view` 写入，**不反映 Web UI 焦点**。
   - `GET /api/session` **不带 directory**（Windows 合并逻辑，`lib/opencode/proxy.js:779-856`）→ 跨全部 projectDirs 的全局会话列表（本轮实测：202 个会话 / 23 个目录，每会话含 `id, directory, title, model`）。这是**列表**，不是"激活会话"。
4. 桌面壳（Electron）：主窗口当前会话不回报主进程；仅 mini-chat 窗口按 sessionId 建窗（`packages/electron/main.mjs:2834-2938`）。
5. 按 A 端要求：不自行退化为"最新 session"、不按时间猜。

**待 A 端下一轮裁决的实现候选**（本轮未选）：
- 候选 1：UI 显示当前 URL（OpenChamber Web 页地址栏含 `?session=<id>`）→ AI Relay 让用户从地址栏复制/或经浏览器扩展读取；简单但非全自动。
- 候选 2：读 Electron/Web UI 的 localStorage 落盘文件（`oc.lastSession.v1`）→ 得到"最后一次激活"的 session+directory；对桌面版可行，实时性受限于切会话时机。
- 候选 3：给 OpenChamber 提一个"上报 UI 激活会话"的上游改动（写 `%LOCALAPPDATA%` 状态文件）→ 最干净，但依赖上游/自建 fork。

## B2. L02-01 实现落定（候选 2，2026-09-23 实测）

A 端裁决走**候选 2**：读 OpenChamber 自己落盘的 `oc.lastSession.v1`。已实现 `active_session_reader.py`（只读）+ `openchamber_client.validate_session()`（`GET /api/session/{id}/message?directory=...` 只读核实会话存在/directory 可用）。

真机事实（Windows，Electron/Desktop，`OpenChamber.exe`，OpenChamber v1.24.2）：

1. **存储位置**：`%APPDATA%\OpenChamber\Local Storage\leveldb`（Chromium LevelDB，origin `openchamber-ui://app`）。另有内嵌浏览器分区 `%APPDATA%\OpenChamber\Partitions\openchamber-browser\Local Storage\leveldb`（本例无 UI 键）。
2. **值结构**（源码 `last-session-cache.ts`）：`{"version":1,"runtimes":{"<runtimeKey>":{"sessionId","directory","updatedAt"}}}`；每次 `setCurrentSession()` 都会写。桌面 loopback 的 `runtimeKey` 恒为 `"local"`（`runtime-switch.ts`）。
3. **可靠性边界**：
   - 值走 Chromium localStorage→磁盘，链路为 `persistLastActiveSession` → deferred storage（`setTimeout(flush,0)` 近即时）→ `window.localStorage` → 浏览器进程 LevelDB WAL。读到的是**最后一次已落盘快照**，相对 UI 实时状态有落盘延迟 → 属 `persisted-last-active`，非 `exact/live`。调用方必须再 `validate_session` 核实。
   - 本机 LevelDB 数据块 **comp=none**（metaindex 无 `compression.type`），值紧跟 key 之后为明文字节，reader 用"key 后取 JSON"只读扫描（`*.log` 优先 `*.ldb`，新→旧，删除遮蔽）。若未来 Chromium 改压缩 SST，需补解压。
   - **本次采样该 key 在 WAL/SST 均无活动值**（等 60s 未出现，仅 MANIFEST 有历史引用）→ `read_active_session()` 返回 `None`（`unavailable`）。这是正确结果，非读取失败；值一旦被 UI 写入并落盘即可读到。
4. **view/unview/attention 不改变方案**：切会话时 UI 只调内存 `markSessionViewed`（`notification-store.ts`，无 HTTP）；`POST /api/sessions/:id/view|unview`、`GET /api/sessions/attention` 在 Web/Desktop UI 切会话路径**均不触发** → 不改用 `isViewed`（源码实证，非猜测）。

**L02-01 只读实测**：`GET /health` 200（40 ms）；`GET /api/session/{dummy}/message` 404→`validate_session=False`（端点可达）；真实 POST/compact/create = 0。

## C2. L02-02 执行配置复用 + wire shape 实证（2026-09-23 只读）

**裁决**：UI 不再显示/配置 Agent、Model、工作目录；`session_id+directory` 来自当前会话解析，`agent+providerID+modelID+variant` 直接从当前会话历史**复用**，不猜默认模型。

**执行配置来源**（`openchamber_client.resolve_execution_config`）：
1. **首选**：当前 session 历史中**最近一条配置完整的 assistant 消息**（`agent`+`providerID`+`modelID` 均非空；`variant` 可空；按 `time.created` 取最新，不完整则向前跳过）。
2. **回退**：会话对象自身的 `agent` + `model{id, providerID, variant}`。
3. 都无 → `unavailable`（上层显示"无法取得当前会话的模型配置"）。

**真机 wire shape 实证**（v1.24.2，只读 GET）：
- 消息 `info` 键集：`agent, cost, finish, id, mode, modelID, parentID, path, providerID, role, sessionID, time, tokens, variant` → **消息用平铺 `providerID`/`modelID` + 可选 `variant`**（无合并 `model` 字段）。
- 会话对象键集：`agent, cost, directory, id, model, path, projectID, slug, summary, time, title, tokens, version`，其中 `model = {id, providerID, variant}` → **会话用 `model.id`（非 `modelID`）**。
- 真机解析实测（跨两种 provider 均正确）：`4090/qwen3.8-27b/variant=平均`；`opencode/big-pickle/variant=None`。

**prompt_async body**（`send_text`，text 原样、不包装）：`{messageID, model:{providerID, modelID}, agent, variant, parts:[{type:"text", text}]}`；HTTP 204=accepted（≠完成）。
**summarize body**（`compact_session`）：`{providerID, modelID}`；成功 = HTTP 200 且 body `true`。

**L02-02 只读实测**：真实 GET 12 次（health + session 列表 + 会话消息 + 单会话对象，全部只读）；`real_prompt_post=0 / real_compact=0 / real_create=0`。

## E. compact 源码证据

- UI 的"压缩会话" = composer 斜杠命令 `/compact`（`packages/ui/src/components/chat/CommandAutocomplete.tsx:156`）→ `opencodeClient.summarizeSession(...)`（`packages/ui/src/lib/opencode/client.ts:1123-1132`）→ SDK `session.summarize` → **`POST /session/{sessionID}/summarize`**（SDK `@opencode-ai/sdk@1.18.29` 映射，query `directory`/`workspace` 可选，body `{providerID, modelID, auto?}`）。
- OpenChamber server 无自有 compact 路由，`/api/*` 全部 proxy 到 OpenCode 上游（`pathRewrite: {'^/api': ''}`，`lib/opencode/proxy.js:877-887`），并先做 directory query 规范化（`proxy.js:938-948`）。
- SDK 另有一个新方法 `session.compact` → `POST /api/session/{sessionID}/compact`（无 query/body，新版 opencode httpapi），但 OpenChamber UI/server **均未使用**。Lite 第一版跟随 UI 走 `/summarize`。
- 成功判定：HTTP 200 且 body `true`；压缩完成后 OpenCode 发 `session.compacted` SSE 事件（`{type:"session.compacted", properties:{sessionID}}`）。
- v3 时代"裸 `/compact` 走 send 端点 vs 程序化 compact 路径"的 G4 冲突已被源码解答：UI 走的就是程序化 `summarize`；Lite 采用同路径即可，无需再复核。

## 认证边界（沿用 v3 合同 §4）

- 自动本地 Bearer 只允许 loopback（localhost / 127.0.0.1 / ::1）；token 来源：环境变量注入 > `~/.config/openchamber/settings.json` 的 `desktopLocalClientToken`。
- 非 loopback 不自动发送本地 token；token 不落日志、异常、响应。
- `/health` 无需认证。

## 本轮实测记录（只读，2026-09-23）

| 请求 | 结果 |
|---|---|
| `GET /health` | 200，46 ms；`openchamberVersion=1.24.2`，`openCodeRunning=true`，`openCodePort=64076` |
| `GET /api/session`（无 directory） | 200，91 ms；202 会话 / 23 目录（Windows 合并列表） |
| `GET /api/sessions/attention` | 200，13 ms |
| `GET /api/sessions/snapshot` / `/api/session-activity` / `/api/sessions/status` | 200，1-2 ms |
| `GET /api/session/{id}/message?directory=...` | 200；`[{info:{id,sessionID,role,parentID...}, parts:[...]}]` |
| 真实 POST / compact / create session | **0**（本轮禁止） |