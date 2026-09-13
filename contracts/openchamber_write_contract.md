# OpenChamber Write Contract（T21 真实写合同）

> 本文件固化 T21 系列真实等幂写冒烟证据，供后续生产 write transport（T22+）消费。
> 只记录已观察事实；推论明确标注；未验证一律 UNVERIFIED。
> 本文件为**写合同补充快照**，不回写、不修改历史只读合同
> `contracts/openchamber_contract.md`（T19 时代 message endpoint = UNVERIFIED 是当时的正确事实；
> 本轮 T21 real smoke 之后才补充了真实验证，属时序上后续的事实，不代表 T19 当时已知）。

## 1. Provenance（证据来源）

本合同证据来自四个历史来源，彼此区分，不混写：

| # | 来源 | 内容 | 证据类 |
|---|---|---|---|
| S1 | commit `7142615e0792f2627bd28c49e955876856db0bdc`（T21-01） | 只读合同侦察：`POST` 类写端点仅做路由接线/文档锚点探测，未发真实写 | STATIC_SOURCE |
| S2 | commit `ee898485f19eade40cf7166bc174d44d441f4b94`（T21-01F） | probe 安全硬化（fail closed / GET only / 脱敏），保证探测无副作用 | AUTOMATED_SAFETY_TEST |
| S3 | commit `909c8d7d64901055050dcdfb690ccfc0445a8175`（T21-02 / T21-02R） | 真实 disposable write smoke 探测器代码本体（含 UNKNOWN 恢复语义） | REAL_SIDE_EFFECT |
| S4 | private runtime evidence（本地文件，不纳入版本库）：`.recovery/t21_write_smoke_state.json`、`.recovery/t21_write_smoke_state.before_reconcile.json`、`.recovery/t21_write_smoke_evidence.json`、`.recovery/t21_write_contract_evidence.json` | 真实运行状态、HTTP 结果、message schema、attribution 判定、recovery history | REAL_READBACK |

- Private evidence 绝不纳入版本库；本合同只摘取脱敏事实。
- 本合同不含真实 session id / message id / token / Authorization / 用户数据 / probe directory 全路径中的随机 run id。

## 2. 正式能力等级

能力等级四分类：`SUPPORTED_REAL` / `OBSERVED_READONLY` / `DOCUMENTED_ONLY` / `UNVERIFIED`。

| 能力 | 等级 | 依据 |
|---|---|---|
| session create | **SUPPORTED_REAL** | 真实 POST `/api/session` 成功并创建 exactly one disposable session |
| prompt_async send | **SUPPORTED_REAL** | 真实 POST `/api/session/{session_id}/prompt_async` 成功（HTTP 204） |
| message list/read（用于 attribution） | **SUPPORTED_REAL** | 真实 GET message 读回并完成四条件归属判定 |
| classic `POST /api/session/{session_id}/message` | **DOCUMENTED_ONLY** | 静态源码/SDK 锚点存在；无真实写验证 |
| v2 `POST /api/session/{session_id}/prompt` | **DOCUMENTED_ONLY** | 静态源码锚点存在；无真实写验证 |
| compact / interrupt / approve / reject / auto accept write / delete / archive | **UNVERIFIED** | 本轮真实 smoke 未调用；不变更等级 |

规则：**静态源码存在 route 不得升级为 SUPPORTED_REAL**；只有真实副作用（side effect）或真实 readback 才升级。
其余能力（delivery steer/queue 语义、permission approve/reject、auto accept write、delete/archive、compact 最终控制路径）见 §12 deferred。

## 3. Session create 实证

实际真实调用形状（来自 S3 smoke 代码 + S4 private evidence，非旧文档改写）：

| 项 | 实际值 |
|---|---|
| Method | POST |
| Path | `/api/session` |
| Directory binding | query 参数 `?directory=<probe_directory>`（值经 URL 编码） |
| Request body keys（仅实际发送的 key） | `title` |
| Observed HTTP | 200 |
| Observed result | 服务端创建 exactly one disposable session |

- create 返回 body 为 JSON 对象；实际观察到并使用其中的 `id` 字段作为 session id。
- 其余 create 返回字段本轮未用于判定，一律 **UNVERIFIED**，不列为 mandatory。

## 4. Session ID 事实

- **observed runtime prefix = `ses_`**（OBSERVED_REAL runtime fact，S4 实测）。
- **静态/SDK 曾出现 `sess_`**（compatibility / documented clue，S1 静态来源与本地 SDK）。
- 二者都为合法前缀候选，但 **session identity 不得仅靠 prefix 判定**。
- 生产代码不得把 `sess_` 当作唯一真实前缀。
- **UNKNOWN_CREATE reconciliation 决定性条件**（四条件同立才能恢复）：
  1) unique frozen directory（冻结的 probe directory）；
  2) GET session list；
  3) 恰好 exactly one session；
  4) 该 session 含非空合法 id。
  否则（0 个 / 多个 / 缺 id / malformed）一律 declined，绝不发起写。

## 5. prompt_async 实证

实际真实调用形状（S3 + S4）：

| 项 | 实际值 |
|---|---|
| Method | POST |
| Path | `/api/session/{session_id}/prompt_async`（session_id in path，URL 编码） |
| Directory binding | query 参数 `?directory=<probe_directory>` |
| Request body keys（仅实际发送的 key） | `messageID`、`model`、`agent`、`variant`、`parts` |
| client messageID 位置 | body 顶层 `messageID`（`msg_<synthetic>` 形态，客户端生成） |
| agent / model | **实际发送**：body `agent`、`model`= `{providerID, modelID}`、`variant` |
| parts/content 实际结构 | `parts`= `[{"type": "text", "text": "<synthetic prompt>"}]` |
| delivery | **未发送**；不在当前 SDK promptAsync body key 集内，本轮不发送 |
| Observed HTTP | 204 |

只写 T21-02 实际发送形状；**不把所有 SDK optional 字段伪装成本轮已验证**。

## 6. prompt_async response 语义

- 真实观察 **HTTP 204**。
- **204 = request accepted**，**204 != completed**；204 response 本身**没有 attributable completion result**。
- 生产状态至少区分：`execute_accepted` / `execution_progressing` / `result_observed` / `session_attribution_valid` / `completed`。
- **禁止**“POST 成功 → task completed”的推理。

## 7. Message read schema 实证

- 用于 attribution 的只读端点：`GET /api/session/{session_id}/message?directory=<probe_directory>`。
- 真实观察消息形状（嵌套 info）：`{info: {id, role, parentID, ...}, parts: [...]}`。
- 本轮实际用到且观察到的字段：
  - `info.id`
  - `info.role`
  - `info.parentID`
  - `parts`（`parts[].text`，type=text）
- **client supplied probe messageID == observed user message `info.id`**（真实观察，user_match_count=1）。
- 未用于 evidence 判定的其他字段不列为 mandatory。

## 8. Attribution 规则

完成归属必须同时满足四个条件：

| 条件 | 含义 |
|---|---|
| A | user message identity 可定位（本轮 messageID 唯一命中） |
| B | assistant role 正确（role == assistant） |
| C | response content 满足 completion 判定（精确 marker 匹配） |
| D | 显式 causal relation 存在 |

- 当前实证关系 = **D: assistant.info.parentID == user.info.id**（真实观察，completion verdict VALID）。
- 对生产实现**禁止**以下启发：last assistant message / newest message / nearest timestamp / content looks similar / only one assistant message。
- 若 content（C）正确但无显式 parent/causal 关系（D 不成立）→ **ATTRIBUTION_AMBIGUOUS**，不允许判 COMPLETE。

## 9. UNKNOWN 写语义（核心）

> POST timeout / connection loss / ambiguous response **≠ rejected**，而是 **UNKNOWN = possibly delivered**，所以 **never blind resend**。

- **UNKNOWN_CREATE**：禁止第二次 create POST；只允许只读（read only）reconciliation（对唯一冻结 directory 做 GET session list，exactly one 才能恢复）。
- **UNKNOWN_SEND**：禁止第二次 prompt POST；只允许 message/status GET 尝试确认服务端是否已接收/完成。

### 关于真实 timeout 证据

- 本轮**尚未真实制造一次网络超时**；因此 **UNKNOWN recovery algorithm 是 safety contract（由自动化测试验证），不是一次真实 timeout 事故证明**。二者区别必须保持：
  - 真实 timeout 事故 -> 未有；
  - 自动化 safety test -> test_t21_write_smoke.py 已覆盖（timeout → UNKNOWN → no resend）。
- 真实 incident 佐证：T21-02 首次 create POST 返回 HTTP 200，但本地 probe 合约期待的运行前缀与实测前缀不符（本地期待 `sess_`，实测 `ses_`），该运行被归类为 UNKNOWN_CREATE 并**未重发 create**，随后以只读 reconciliation GET 恢复 exactly one session。该 incident 证明“UNKNOWN 后不盲目重发 POST”的行为真实执行过；但它不是网络超时制造。

## 10. Durable intent 原则

- T21 probe 代码证明：**before POST → 先持久化尝试意图（attempt intent/counter）**；任何 HTTP POST 之前必须先落盘 durable attempt 记录。
- 生产要求固化：**durable attempt record must exist before POST**（副作用之前必须先有持久化记录）。
- **T21 probe journal != production T22 Operation Ledger**；真正 OperationId / idempotency-UNKNOWN authority / durable operation ledger 留给 T22+ 裁决。

## 11. Seven independent runtime states

七层为独立状态，**禁止合成/合并**（例如笼统“connected/healthy/ready”）。逐项列出并保持独立：

1. `service_reachable`
2. `api_authenticated`
3. `capability_available`
4. `session_exists`
5. `session_attribution_valid`
6. `execute_accepted`
7. `execution_progressing`

不变式：**`execute_accepted=true` 不 imply `session_attribution_valid=true`**。
T21 实测 seven_layers：service_reachable=true、api_authenticated=true、capability=OBSERVED、session_exists=true、attribution=VALID、execute_accepted=true、execution_progressing=true（本次快照；状态互不推出）。

## 12. Deferred 能力

以下仍未裁决（本轮不判能力等级/不选路径）：

- classic `POST /message`（write）
- v2 `POST /prompt`
- delivery steer / queue 语义
- interrupt / stop
- permission approve / reject
- auto accept write
- delete / archive
- compact final control path

### Compact（G4）

- T21 static source saw 程序化 compact route，**但 T21 real smoke 未调用 compact**。
- **G4 remains authority for POST_RESPONSE_COMPACT**；本 T21 合同**不替 G4 提前选路径**；compact 最终控制路径 deferred 到 G4 复核。

## 13. Evidence provenance table

| Claim | Evidence class | Source | Result |
|---|---|---|---|
| create POST `/api/session` 成功创建 session | REAL_SIDE_EFFECT | S3 smoke + S4 journal | HTTP 200；directory query；body key=title；exactly one |
| 运行 session id 前缀 = `ses_` | REAL_READBACK | S4 evidence（id 脱敏/hash 化） | OBSERVED_REAL runtime fact |
| `sess_` 仅为 documented 兼容线索 | STATIC_SOURCE | S1 静态整流 + 本地 SDK | DOCUMENTED_ONLY 级别线索 |
| prompt_async POST `/api/session/{id}/prompt_async` | REAL_SIDE_EFFECT | S3 smoke | HTTP 204 accepted |
| prompt body keys（仅发送的） | REAL_SIDE_EFFECT | S3 代码（messageID/model/agent/variant/parts） | 与 SDK 子集一致；delivery 未发送 |
| HTTP 204 语义 | REAL_SIDE_EFFECT + REAL_READBACK | S3/S4 | 204 = accepted != completed |
| message schema（嵌套 info） | REAL_READBACK | S4 GET message | info.id/role/parentID + parts |
| client messageID 持久化 | REAL_READBACK | S4 evaluate_completion | client messageID == user info.id |
| parentID attribution | REAL_READBACK | S4 verdict VALID | assistant.info.parentID == user.info.id |
| accepted != completed | REAL_SIDE_EFFECT + AUTOMATED_SAFETY_TEST | S3 data model + tests | 204 不推出 completed |
| UNKNOWN no resend | AUTOMATED_SAFETY_TEST（+ 真实前缀不匹配 incident 佐证） | test_t21_write_smoke.py + S4 recovery_history | timeout -> UNKNOWN -> never blind resend |

## 14. Hygiene

- 本文件不含任何真实 session id / message id / token / Authorization 实值 / 用户数据。
- 示例 id 只允许形态：`<session_id>`、`<message_id>`、`<probe_directory>`、`ses_<synthetic>`、`msg_<synthetic>`。
- 本文件不含真实 run timestamp、probe directory 全路径中的随机 run id、disposable slug。
- `Authorization` / `Bearer` 只作为字段概念出现，不带任何真实值。