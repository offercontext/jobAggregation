# Scoped Capability & Binding Enforcement 设计

**状态：待复审**

**设计捕获基线：`888011ee1a6d6fdfada115a4daccd94fefd2b117`**

**实施基线：`Agent Loop Unification` 最终验收并合并到 `main` 后的提交（当前尚未产生）**

**建议实施分支：`feat/20260824-scoped-tool-authority`**

**建议实施 Worktree：`D:\Users\yuqi.chen\offerpilot\.worktrees\feat-20260824-scoped-tool-authority`**

> 本文可以在 Agent Loop 开发期间复审，但不得以开发中的 Agent Loop 工作树作为实施基线。实施前必须用 Agent Loop 最终合并提交重新捕获工具、调用路径和测试清单；若最终接口与本文假设不同，应先修订本文，不得用兼容层掩盖差异。

## 1. 背景与问题

OfferPilot 已经完成或正在完成：

```text
Durable Execution Journal
→ Tool Execution Pipeline
→ Write Operation Ledger
→ Context Projector
→ Pilot Runtime Orchestration
→ Agent Loop Unification
```

第二期已经为 25 个模型可见工具建立 Typed Catalog、`ToolSpec`、Capability 声明、Binding Audit、HITL 和类型化结果；第四期已经能按当前上下文裁剪 Provider Tool Surface。当前仍存在两个执行安全缺口：

1. Chat Runtime 通过 `frozenset(ToolCapability)` 自动授予枚举中的全部能力。未来新增 Capability 时，也可能被旧入口自动获得。
2. Binding 只产生 `matched / mismatched / unbound / unavailable` 审计结果，没有普遍成为执行拒绝条件。

因此当前的四个概念没有形成真正独立的边界：

```text
Tool Visibility
    模型是否看到工具

Tool Capability
    当前 Runtime 入口是否有权使用该类能力

Entity Binding
    当前 Segment 可以作用于哪些可信实体

Approval + Ledger
    本次具体写操作是否已获批准并取得执行 claim
```

Context Projector 的 Tool Surface 只能减少模型可见面。隐藏工具不是授权；工具可见也不代表可以作用于任意实体；用户确认也不能替代 Capability 或 Binding。

Agent Loop 统一后，首次请求、read-tool 循环、确认 continuation 和 chained Pending 会共享同一个显式循环。这是一次性收紧 25 个 Typed Tool 执行边界的正确时点。

## 2. 目标、兼容口径与非目标

### 2.1 目标

本期必须完成：

1. 用服务端版本化 Capability Profile 取代生产代码中的“自动授予全部枚举值”。
2. 为 25 个 Typed Tool 建立封闭、机器可校验的 Binding Contract。
3. 将 Application scope 的实体 Binding 从审计提升为执行前强制校验。
4. 对 Application-owned collection 执行服务端 Repository scope constraint，避免应用上下文读取其他投递的数据。
5. 在确认等待期间把提案时可信 Scope 绑定到 Write Operation，防止切换 Conversation Context 后批准旧提案。
6. 在写事务锁内重新校验可变归属，关闭 prepare 与 executor 之间的 TOCTOU。
7. 将 Context Projector 的选择结果与 Capability/Scope 允许的工具做安全交集，但继续由 Pipeline 承担最终授权。
8. 保持 25 个 Provider Tool Contract 与 3 个 Legacy deterministic 工具边界不变。

### 2.2 兼容口径

本期采用：

> **内部执行权限一次性收紧；Provider、HTTP/SSE、HITL 和领域事务协议保持兼容。**

保持不变：

- Provider 看到的工具 `type/function/strict/name/description/parameters` 等完整 envelope；
- 25 个工具的名称、顺序和 JSON Schema；
- JSON parse、Schema 校验和 typed decode 契约；
- HTTP 状态、SSE event 名称、字段和既有顺序；
- Pending Action、confirmation token、修改、拒绝和 CAS 语义；
- Write Operation Ledger、Undo、delivery fencing 和 terminal replay；
- Repository/API 已有的实体归属、revision、stale-state 和唯一约束；
- Compatibility Renderer 的既有权限失败文本；
- Journal V1 Event Schema 与 Snapshot Manifest V2；
- 3 个模型不可见 Legacy deterministic 工具的专用路由。

明确允许的行为变化：

- 过去被 Binding Audit 放过的跨 Application 调用现在会在 executor 前拒绝；
- Application conversation 中的 collection 结果会被限制到当前 Application；
- `create_application` 和跨 Application 的 `compare_offers` 在 Application scope 中不再可用；
- Context Scope 或 Authority Policy 在等待确认期间变化时，旧 approve/modify 会返回 stale；
- Provider Tool Surface 可能因 Authority 交集进一步收窄；
- 历史未知/custom `context_type`、非法 Application ref 或非法 mode 不再进入 Typed Agent/confirmation flow，而是在 Provider 前安全失败；workspace/global/mode 的历史非空 ref 继续被忽略且不获得 binding；
- 模型回答、工具选择和 Provider 调用次数可以随工具表面变化，但不能突破 Agent Loop 最大迭代、无隐式 retry、HITL 和 Ledger 约束。

这些是有意的安全收紧，发布报告不得宣称旧业务行为完全等价。

### 2.3 非目标

本期不实现：

- 完整 Tool Metadata Convergence；
- 将 Selector 的 domain、lexical rule、dependencies 或 Ledger 集合全部迁入统一元数据；
- 自动改写 Provider ToolCall 参数；
- 为写工具自动注入 `application_id`、`resume_id` 或其他 ID；
- 多用户 RBAC、角色、ACL 或可配置权限管理；
- 新的用户可见错误协议；
- 3 个 Legacy deterministic 工具的 Typed Pipeline 迁移；
- Summary、Memory 或 Knowledge Contributor；
- Runtime Query UI、Haru Control Plane、Wakeup Queue 或 SSE Replay；
- 新 Agent、多 Agent或通用插件系统；
- 跨 Provider、数据库和外部系统的全局 exactly-once。

Tool Metadata Convergence 仍是紧随本期的独立项目。本期只增加执行授权所必需的最小 `BindingContract` 和 Capability Profile，不顺手迁移 Selector/Ledger 的其他静态事实。

## 3. 总体架构

### 3.1 四层独立边界

固定执行关系：

```text
完整 25 Tool Typed Catalog
        ↓
Context Projector Tool Selection
        ↓
Capability / Scope Surface Intersection
        ↓
Frozen Provider Tool Surface
        ↓
ModelCallSurfaceBinding 验证 Provider response
        ↓
Typed Catalog lookup
        ↓
Capability Gate
        ↓
Binding Resolve + Binding Policy
        ↓
Existing Repository/API checks
        ↓
HITL + Pending + Ledger claim/CAS
        ↓
Executor
```

规则：

- Visibility 通过不能替代 Capability；
- Capability 通过不能替代 Binding；
- Binding 通过不能替代 Repository 归属和 revision；
- 用户批准不能绕过前三层；
- `auto_approve`、测试注入或 Provider fallback 不能绕过任何层。

### 3.2 Segment 级 Authority

新增瞬态不可变对象：

```python
@dataclass(frozen=True, slots=True, repr=False)
class SegmentExecutionAuthority(TransientToolRuntimeValue):
    conversation_id: int
    conversation_scope_revision: int
    segment_id: str
    trusted_scope: TrustedContextScope
    capability_profile_id: str
    capabilities: frozenset[ToolCapability]
    capability_policy_version: str
    binding_policy_version: str
    capability_profile_fingerprint: str
    binding_policy_fingerprint: str
    authority_instance_token: AuthorityInstanceToken
```

`AuthorityInstanceToken` 是当前进程、当前 Authority 生命周期内的不可序列化对象身份，不是业务 ID、认证 token 或持久指纹。

Approve/modify 在 origin terminal commit 之前不属于 Model continuation Segment，因此另有封闭瞬态类型：

```text
ApprovalExecutionAuthority
- operation_id
- conversation_id / conversation_scope_revision / trusted_scope
- pending identity/revision / tool_call_id / tool_name / effective_args_digest
- capability profile 与 binding policy identity
- approval_authority_instance_token
```

它只能由 Ledger-first confirmation Port 从可信 Operation、Pending 和 Conversation 构造，只用于重建/执行 origin write；不能创建 Provider Surface、调用 Provider、创建 chained Pending 或跨越 terminal commit。`ToolExecutionAuthority` 是上述 approval 类型与 `SegmentExecutionAuthority` 的 sealed union，Pipeline 两者都接受，但 Provider/Selector 与 `PendingAuthorityClaim` 只接受 Segment 类型。

公共门禁分两步：`require_authority_phase(authority, use, call_identity)` 在 Catalog lookup 前验证类型/生命周期/调用身份；lookup 成功后 `require_authority_spec(authority, use, spec)` 在 resolver/Repository 前验证 Tool kind 与 confirmation policy。封闭 use matrix：

| Authority | 允许 use | 额外条件 |
|---|---|---|
| Segment | `provider_surface`、`new_turn_prepare`、`read_execute`、`typed_pending_claim` | segment/token 必须与当前 Runner/Surface/Context 相同 |
| Approval | `approved_write_prepare`、`approved_write_execute` | `spec.kind=write` 且 operation/pending/tool/effective-args identity 全匹配 |

任何其他组合统一 internal fail-closed，Provider/Binding resolver/Repository/Pending/executor 为 0。特别禁止 Approval Authority 进入 read executor、Selector、Provider、ModelCallSurfaceBinding、`PendingAuthorityClaim` 或 chained Pending；Segment Authority 也不能在没有既有 confirmation authorization 时直接执行 write。这个门禁不能靠类型注解或调用者约定，必须是生产运行时判定和 AST/negative-test 共同约束。

约束：

- 只能由 Pilot Runtime composition root 创建；
- Agent Loop、Provider、客户端和 ToolCall 不能构造或扩展它；
- Segment Authority 在同一 Segment 内冻结，不因页面切换、fallback 或下一次 model call 改变；
- Approval Authority 只对当前 approve/modify 尝试有效；新尝试和 terminal 后的新 Segment 都必须重新构造；
- 不进入 ChatMessage、Pending、Ledger payload、Journal、Trace、HTTP、SSE、Prompt 或日志；
- 不携带 ORM、Session、Repository、credential 或原始附件内容；
- 所有原始 Scope ID 均为 `repr=False`；
- pickle、`to_json()`、dataclass 通用序列化和 checkpoint 写入必须失败。

`ToolExecutionContext` 内部破坏性切换为持有 sealed `authority`，删除生产调用方直接注入 `capabilities/current_bindings` 的旧构造协议。Approved Write Seed 分别接收 origin approval context 与 terminal 后加载的 continuation context；不得把前者复用给模型循环。不得保留长期 façade 或从旧字段回退。

### 3.3 Source Loader 与 Authority 顺序

New Turn 固定为：

```text
读取服务端 Conversation
→ ContextSourceLoader 在同一只读 UoW 中验证并冻结 Scope
→ 同时冻结 Conversation 的单调 scope revision
→ AuthorityResolver 从 Frozen Source DTO 构造 SegmentExecutionAuthority
→ Tool Selector
→ Authority Surface Intersection
→ Projector 组装 Frozen Surface
→ Agent Loop
```

AuthorityResolver 不再单独打开 Repository Session，从而避免与 Context Projector 看到不同的 Application 事实。

Approve/modify 在领域事务提交前只通过最小只读 `ApprovalAuthorityResolver` 加载可信 Conversation Scope/Application visibility 并构造 `ApprovalExecutionAuthority`；它不调用完整 ContextSourceLoader，不加载历史、附件或 Provider。成功或稳定 terminal 后的 Provider continuation 才创建新 Segment：领域事务提交后运行一次 ContextSourceLoader，再建立新的 `SegmentExecutionAuthority`。Terminal replay、delivery recovery、reject 和纯 deterministic 结果不运行任一 Authority resolver。

## 4. 可信 Scope 与 Capability Profile

### 4.1 可信来源

Authority 只允许使用：

- 服务端加载的 `Conversation.id/context_type/context_ref/mode/scope_revision`；
- New Turn/continuation 的 ContextSourceLoader 或 approve/modify 的最小 ApprovalAuthorityResolver 已验证的 Application 可见性；
- 服务端代码内固定的 Capability/Binding Policy 版本；
- 当前 Agent Run/Segment 的服务端身份；
- 确认路径中的服务端 Pending 与 Write Operation 身份；
- 写事务内 Session-bound Repository 的最终归属事实。

以下不能赋予或扩大权限：

- 未经过 Conversation scope mutation Port 的请求 payload `context_type/context_ref/mode`；
- page context；
- 页面 URL、路由名称或可见文字；
- 附件文件名、显示名称、自然语言或未经 Authority Policy 声明的附件 ID；
- Provider ToolCall 参数和自然语言回答；
- 客户端提交的 `tool_name`；
- 旧 `PreparedToolCall`、checkpoint、缓存或跨请求内存对象。

本期附件继续作为 Context Projector 的内容来源和 Tool Selector 信号，不成为持久 Entity Binding。这样确认恢复不需要信任已经丢失的请求附件对象。未来如需 Resume-scoped Conversation，应单独扩展持久 Context Scope，而不是让附件隐式授权。

新建 Conversation 或既有产品流程显式切换上下文时，请求字段只是一份 scope mutation proposal。API 事务外只做封闭类型、格式和 canonicalization；Application visibility 不能在事务外成为授权事实。持久化顺序固定为：

```text
normalize raw scope proposal
→ BEGIN IMMEDIATE
→ 使用同一 Session 调用 resolve_visible_authority_application（仅 application scope）
→ set_context_scope(..., expected_scope_revision) CAS
→ commit
→ reload canonical Conversation
```

visibility 失败或 CAS loser 均回滚，本次 Provider/Tool/Pending 为 0。只有 reload 后的 Conversation 才能进入 Source Loader/Authority；Agent Loop、Provider 和 Pipeline 永远不能直接把 raw request scope 当作权限。字段与已持久 Scope 相同则不递增 revision。双连接 barrier 必须证明“事务外格式验证后 Application 删除/隐藏”不会持久化不可见 scope。

是否“显式切换”必须依据解析前的字段存在性或严格 DTO fields-set 标志，不能依据 Pydantic/default 后自动出现的 `workspace/general/""`；否则旧 Conversation 的省略字段请求可能被错误重置并递增 revision。

Application visibility 只有一个 Session-bound Repository predicate/Port（建议命名 `resolve_visible_authority_application(session, application_id)`），由 Scope mutation、ContextSourceLoader、ApprovalAuthorityResolver、Pending proposal transaction 与 approved-write transaction 共同调用；各层不得复制“可见”判断或使用不同 Repository。它只返回有界 primitive identity/revision/absent state，不返回正文或 ORM。

### 4.2 Scope canonicalization

`TrustedContextScope` 只允许：

| `context_type` | canonical ref | mode | 绑定 |
|---|---:|---|---|
| `workspace` | `null` | canonical Conversation mode | 无实体绑定 |
| `global` | `null` | canonical Conversation mode | 无实体绑定 |
| `application` | 正 JSON integer，且 Application 已由 Loader 验证 | canonical Conversation mode | `application → {id}` |
| `mode` | `null` | 服务端 Conversation 中的 canonical mode string | 无实体绑定 |

Wire/persisted/internal ref 真值表固定为：

| context type | API `context_ref` 输入 | persisted TEXT | internal canonical ref |
|---|---|---|---|
| application | JSON string `"37"` 或 JSON integer `37` | `"37"` | JSON integer `37` |
| application | missing/null/`""`/`"01"`/`"+1"`/含空白/`1.0`/`true`/非正数/超出 signed 64-bit | reject 422 | 不写入 | 不构造 |
| workspace/global/mode | missing/null/`""` | `""` | JSON `null` |
| workspace/global/mode | 有界 JSON string | 新写入丢弃其内容并保存 `""`；legacy DB 值只读兼容 | JSON `null` |
| workspace/global/mode | number/bool/array/object | reject 422 | 不写入 | 不构造 |

新建 Conversation 的 `context_type` missing/null/empty 固定默认为 `workspace`；既有 Conversation 请求未显式发起 scope mutation 时 missing/null/empty 表示保持当前值，不能重置为 workspace。除此之外只接受精确小写 JSON string `workspace/global/application/mode`，不 trim/casefold；application integer 上限为 `9223372036854775807`。被忽略的非 Application ref 最多 256 UTF-8 bytes，且不得包含 surrogate/NUL/control；它永远不进入 Authority/HMAC。Conversation model 继续用 TEXT 保存 ref，只有 Authority canonical envelope 使用 integer/null，禁止各入口自行依赖 Pydantic coercion。

既有 Conversation 若只提交 `context_ref` 而没有显式合法 `context_type`，固定返回 422，不推断当前 type；mode 可以作为独立显式 mutation。一个请求同时修改 type/ref/mode 时使用一次 CAS、revision 最多递增 1。

规则：

- workspace/global/mode 的历史非空 `context_ref` 不具有授权语义，Loader 固定忽略并 canonicalize 为 JSON `null`；所有新写入由 Repository 保存为空字符串，不能把该 ref 变成 binding；
- application ref 禁止 `+1`、`01`、浮点、布尔、空白和非正值；
- `unknown` 仅允许出现在 Journal 诊断，不是可执行 Scope；
- 非法/不可见 Application 在 Provider 前产生既有 `source_load_failed`；
- mode 的唯一 default canonical value 是 JSON string `"general"`：新建 Conversation 的请求缺失/null/空字符串，以及既有 Conversation 显式提交 null/空字符串的 mode mutation，在 API 边界映射为 `"general"`；既有 Conversation 省略 mode 则保持当前值。数据库只保存 canonical 非空值；migration 先把 legacy SQL NULL/空字符串统一为 `"general"`，再初始化 scope revision。其他 mode 最多 64 个 Unicode code point 且最多 256 UTF-8 bytes；禁止 surrogate、NUL、C0/C1 control 和首尾空白；不做 trim、casefold 或 Unicode 规范化；`"GENERAL"` 等大小写变体是不同的合法未知 mode，不等同 default；
- V1 不根据 mode 值增加 Capability，未知但满足上述词法约束的 mode 使用同一 `agent_typed_v1` Profile；不满足约束的数据库值 fail-closed；
- 当前请求中与服务端 Conversation 冲突的 Scope 字段不能覆盖持久 Scope。

未知/custom `context_type` 不静默升级成 workspace，也不阻止应用启动。新建 Conversation 的请求若提交未知 type，在 Conversation/User Message 写入和响应头前按现有 request-validation 形状返回 422，Provider/Tool/Pending 为 0；已存在的 legacy unknown Conversation 发起 Typed Agent 请求时使用既有 `source_load_failed` 安全交付，用户原始 ChatMessage 与失败状态是否持久化沿用当前 sync/SSE Source Loader 失败契约。已有 terminal Operation 仍可 replay，reject 仍可执行；升级前 Typed proposed/null fingerprint 仍先走 `authorization_scope_unbound` 短路。3 个 Legacy deterministic Pending 保持各自既有可信路由，不因本期获得 Typed Authority。该兼容例外、旧 `custom-private-type` restart/golden 和用户可见行为变化必须进入发布报告。

Conversation 另有内部单调 `scope_revision`：新建行为必须为 0，迁移前既有行在 mode default 规范化后回填 0；持久化的 raw `(context_type, context_ref, canonical mode)` 任一值发生字节级变化时必须在同一事务中恰好 `+1`，三者均未变化时必须保持不变。即使 workspace/global 的 legacy ref 不参与授权，它发生变化也保守递增 revision。该值是 `0..9223372036854775807` 的 JSON/SQLite integer，不使用 `updated_at`、时间戳或内容 hash 代替；达到上限后 Scope mutation fail-closed，禁止 wrap。A→B→A 因此产生两个 revision，不能恢复旧值。所有 Scope 修改只能通过 Session-bound `set_context_scope(..., expected_scope_revision)` CAS Port；数据库 INSERT trigger 强制初始值 0，UPDATE trigger 同时阻止“raw Scope 变化但 revision 未递增”和“raw Scope 未变化却任意改 revision”。该字段不进入公开 Conversation DTO、HTTP、SSE、Prompt、Journal 或日志。

### 4.3 Capability Profile V1

禁止继续使用：

```python
frozenset(ToolCapability)
```

V1 建立显式、封闭的 `agent_typed_v1` Profile，逐项列出当前 11 个 Capability：

```text
applications.read
applications.write
application_events.read
application_events.write
notes.read
notes.write
offers.read
offers.write
resumes.read
resumes.write
jd_analyses.read
```

workspace、global、application 和 mode 的普通 Agent Loop 首批使用同一个显式 Profile；实体范围差异由 Binding/Scope Policy 承担。这样保持当前产品能力，同时保证未来新增 `ToolCapability` 不会被旧 Profile 自动获得。

Profile 规则：

- Catalog 初始化时验证所有 `required_capabilities` 都属于 V1 已知集合；
- Capability Profile 与 25 Tool Binding manifest 分别使用公开 canonical SHA-256；内容改变但 version 未改变时启动失败；
- 新增 Capability 必须显式更新 Profile 和 golden，默认拒绝；
- 未知 entrypoint、未知 policy version 或缺失 Profile 在 Provider 前 fail-closed；
- Legacy deterministic、reject、replay、delivery recovery 不取得 Typed Capability Profile；
- Capability Profile 只控制能力类别，不能由 Tool Surface 反向推导；
- Context Projector 隐藏某工具不会从 Authority 中删除能力；Pipeline 仍按 Authority 判断。

### 4.4 静态 Policy fingerprints

两个公开 SHA-256 的 canonical 输入固定为：

```text
capability_profile_fingerprint = sha256(canonical({
  schema: "capability-profile-v1",
  profile_id: "agent_typed_v1",
  capabilities: [按本节显式顺序的 11 个字符串]
}))

binding_policy_fingerprint = sha256(canonical({
  schema: "binding-policy-v1",
  aggregation_rule_version,
  collection_scope_rule_version,
  public_denial_rule_version,
  tools: [按完整 Provider Catalog 顺序的 {
    name,
    contract_kind,
    entity_kind_or_null,
    resolvers: [按声明顺序的 {entity_kind, presence}]
  }]
}))
```

canonical JSON 固定 UTF-8、对象键排序、紧凑分隔符、`ensure_ascii=false`、禁止非有限数字且不做 Unicode 规范化；结果格式为 `sha256:<64 lowercase hex>`。输入禁止 Python qualname、对象地址、函数 `repr` 或运行时顺序。应用启动时同时验证 version、运行时计算值与只读 golden；任一不一致立即启动失败。Policy 语义变化必须提升对应 rule/policy version 并更新人工复审后的 golden，测试不得自动接受新值。

## 5. 确认提案的 Scope 绑定

### 5.1 必要性

确认等待可以跨请求。不能只在批准时读取“当前 Conversation Scope”，否则可能出现：

```text
Application A 中产生 Pending
→ 用户或并发请求改变 Conversation Scope
→ 在新 Scope 中批准旧提案
```

因此提案时的可信 Scope 必须进入业务真值侧的 Write Operation，而不能依赖 fail-open Journal 或跨请求内存。

### 5.2 `authorization_scope_fingerprint`

迁移为 `write_operations` 增加私有 nullable 字段：

```text
authorization_scope_fingerprint
```

新 Typed primary Operation 创建时必须写入：

```text
HMAC(
  ledger_key,
  b"write-operation-authorization-scope-v1\0"
  + canonical({
      conversation_id,
      conversation_scope_revision,
      context_type,
      canonical_context_ref,
      mode,
      capability_profile_id,
      capability_policy_version,
      binding_policy_version,
      capability_profile_fingerprint,
      binding_policy_fingerprint
    })
)
```

固定规则：

- 使用 Ledger HMAC Key Domain，不使用 Journal Key；
- Application ID、mode 和 Context 原值只作为 HMAC 输入，不新增明文列；
- `conversation_scope_revision` 来自锁内 Conversation integer，用于拒绝 A→B→A 的 Scope ABA；
- workspace/global 的 ref 为 JSON `null`；
- HMAC 算法沿用 Ledger canonical JSON、UTF-8、键排序、分隔符和非有限数字拒绝规则；
- 当前 `fingerprint_key_id` 继续标识 Ledger Key；
- 不把 fingerprint 写进 Pending、HTTP、SSE、Journal 或 ToolMessage；
- Typed proposal、Pending 写入和 scope fingerprint 必须在同一事务中完成；
- 持久化 proposal 的事务必须重新加载 Conversation，并把其 canonical Scope 与 `scope_revision` 同当前 Segment Authority 比较；若 Source Loader 后 Scope 已改变（包括 A→B→A），则不创建 Pending/Operation，返回 stale；
- 事务内从已加载的 Conversation primitive 和固定 Policy 计算 HMAC，不访问文件、Keyring、Provider 或网络。

### 5.3 approve/modify/reject/replay

Approve/modify：

```text
Ledger-first load Operation
→ terminal 则直接 replay
→ proposed Typed primary 且 authorization_scope_fingerprint=null 则立即 authorization_scope_unbound
→ proposed 才加载可信 Pending/Conversation，并经 ApprovalAuthorityResolver 验证 Scope
→ 事务外构造 provisional ApprovalExecutionAuthority 和 canonical HMAC envelope
→ prepare_call() 只做只读预检
→ BEGIN IMMEDIATE
→ 锁内 reload Operation / Pending / Conversation
→ 锁内 Operation/Pending/confirmation request 身份与 effective-args digest 必须重新匹配
→ 锁内 Conversation canonical Scope 与 scope revision 必须与 provisional Authority 完全一致
→ 使用启动时已加载的 Ledger key 在事务内做纯 CPU HMAC
→ constant-time compare persisted authorization_scope_fingerprint
→ Binding / mutable recheck / claim / execute
```

不相等：

```text
category = stale_state
code = authorization_scope_changed
Provider = 0
executor = 0
tool.started/completed/failed = 0
Pending 不清除，用户仍可 reject
```

Reject 不读取或比较该 fingerprint，不运行 Source Loader、Capability、Binding、preflight 或 executor。Scope 已变化也不能阻止拒绝。

旧 null fingerprint 的短路发生在 ApprovalAuthorityResolver、ContextSourceLoader、Projector、Catalog、Schema/decode 和 prepare 之前，但必须先检查可信 Operation 的 Conversation identity：`conversation_id IS NULL` 固定返回既有 `operation_unavailable`；只有 Conversation identity 仍存在时，approve/modify 才返回 `stale_state + authorization_scope_unbound`，reject 才可按既有 identity/CAS 完成。这些组件与 Provider/executor 均为 0。锁内身份重绑定至少覆盖 Pending identity/revision、`operation_id`、`tool_call_id`、`tool_name` 和 effective arguments canonical digest；不匹配固定 stale/conflict 且 executor 为 0。锁外 typed decode 结果只有在其 digest 与锁内可信 Pending/confirmation request 完全一致时才能复用，否则必须丢弃，不能让旧 `PreparedToolCall` 进入 executor。

Terminal replay 只验证既有 Ledger request/integrity/delivery fingerprint，不与当前 Scope 比较；已经提交的结果不能因页面或策略变化而失去可回放性。

### 5.4 旧数据

迁移不猜测旧提案创建时的 Scope，也不从当前 Conversation 回填：

- 已 terminal Operation 保持可回放；
- Legacy deterministic 和 compensation Operation 保持原有协议，字段允许为空；
- 升级前仍为 proposed 且字段为空、`conversation_id` 仍存在的 Typed Operation：approve/modify 固定返回 `authorization_scope_unbound` stale，executor 为 0；reject 仍可完成；若 Conversation 已删除或 identity 被清空，则更高优先级的 `operation_unavailable` 适用于 approve/modify/reject；
- 新 Typed primary Operation 缺少或格式非法时 Repository 创建失败，不能退回旧提案路径。

## 6. Binding Contract

### 6.1 内部类型

现有 `BindingAudit` 的安全公开形状继续保持：

```text
status: matched | mismatched | unbound | unavailable
target_count
entity_kinds
```

为避免把“参数合法省略”和“实体无法解析”混为同一状态，内部 resolver 结果改为：

```python
@dataclass(frozen=True, repr=False)
class BindingTargetResolution(TransientToolRuntimeValue):
    entity_kind: str
    state: Literal["resolved", "omitted", "detached", "unavailable"]
    identity: int | str | None
```

- `resolved`：有可信 identity；
- `omitted`：Provider Schema 明确允许省略该目标；
- `detached`：目标记录存在，但没有该 entity kind 的父归属，例如 standalone note 或未绑定 Application 的 Offer；
- `unavailable`：参数要求目标，但 Repository 无法安全解析；
- 只有 `resolved` 可以携带 identity；
- identity 不进入 `BindingAudit`、Prepared safe diagnostics 或任何持久化。

Resolver 不能只以函数 tuple 存在。每个 resolver 同时声明：

```text
entity_kind
presence: required | optional
resolve(args, context) -> BindingTargetResolution
```

`optional` 只表示参数省略时可以忽略该 resolver；显式参数无法解析仍是 `unavailable`。例如 collection 的可选 `application_id` resolver 为 optional；`update_application_event` 的 event parent 和显式 `application_id` 都是 required，二者必须同时匹配。

所有 Binding resolver 必须满足同一封闭执行契约：

- 只读，不得写入领域数据、Pending、Ledger 或 Journal；
- 不得调用 Provider、网络、文件系统、keyring、外部进程、executor 或 renderer；
- 接收到 caller-owned Session 时只能调用 Session-bound Repository 方法，禁止内部 checkout 或创建新 Session；
- 只返回 primitive identity 与上述封闭 state，不返回 ORM、lazy collection、Repository 对象或跨调用缓存；
- 普通 `Exception` 进入 Pipeline 的安全失败映射；`BaseException` 原样传播；
- Catalog 初始化必须验证 resolver 的 presence、entity kind 与 Binding Contract 一致，运行时不得按函数签名猜测。

每个 `ToolSpec` 必须声明一个封闭 `BindingContract`：

```text
none
enforce_if_bound(entity_kind)
scoped_collection(entity_kind)
optional_target(entity_kind)
non_application_only
```

不能通过 resolver 数量、工具名、模块名或 `list_` 前缀猜测策略。

V1 一个 ToolSpec 只能声明零个或一个 Binding entity kind：除 `none/non_application_only` 必须零 resolver 外，其余 contract 的全部 resolver 必须与 contract 的唯一 entity kind 相同。Catalog 初始化遇到 mixed-kind resolver 立即失败；当前 25 Tool 不定义跨 kind 聚合。未来需要 application+resume 等混合授权时必须提升 Binding Policy version 并单独设计，不能依赖 resolver 顺序。

### 6.2 通用聚合顺序

审计聚合优先级固定为：

```text
当前 entity kind 没有 binding → unbound
否则任一可解析目标不同 → mismatched
否则任一 required 目标 unavailable/detached，或 optional 目标显式提供但 unavailable → unavailable
否则全部目标属于当前 bound set → matched
```

同一个 entity kind 的多个目标必须全部属于 Authority 的 bound set。相同数字但不同 entity kind 不能匹配；mixed entity kind 在 V1 Catalog 初始化阶段已被拒绝，运行时不可达。

### 6.3 Policy 真值表

#### `none`

- 工具没有需要当前 Scope 约束的实体目标；
- capability 和 Repository 检查仍执行；
- 有 Application binding 不会自动拒绝；
- Catalog 初始化要求没有 resolver。

#### `enforce_if_bound(kind)`

| Authority 是否绑定该 kind | resolver 结果 | 结果 |
|---|---|---|
| 否 | resolved/omitted/detached/unavailable | `unbound`，允许继续既有 Repository 语义 |
| 是 | 全部 resolved 且属于 bound set | `matched`，允许 |
| 是 | 任一 resolved 不属于 bound set | `mismatched`，拒绝 |
| 是 | required resolver 为 omitted/detached/unavailable | `unavailable`，拒绝 |

workspace/global/mode 没有 Application binding，因此仍允许显式 ID；Application scope 中必须匹配当前 Application。

optional resolver 为 `omitted` 时不参与聚合；若其显式值最终为 detached/unavailable，则仍拒绝。

#### `scoped_collection(kind)`

| Authority binding | 显式 filter | 行为 |
|---|---|---|
| 无 | 缺失 | 保持现有 workspace/global collection |
| 无 | 存在 | 保持既有显式过滤和 Repository 校验 |
| 有 | 匹配 | 使用显式过滤 |
| 有 | 不匹配 | `permission_denied`，Repository collection 为 0 |
| 有 | 缺失 | 参数保持不变；给 Repository 应用服务端 scope constraint |

“服务端 scope constraint”不是参数注入：

- 不修改 Provider ToolCall；
- 不修改 typed args；
- 不改变 arguments canonical digest；
- 不写入 Pending 或 ToolMessage；
- 只限制最终 Repository query 的可见行；
- scope constraint 失效或实现缺失时 fail-closed，不能退回全量 collection。

它使用不可序列化的内部接口：

```python
@dataclass(frozen=True, repr=False)
class ApplicationScopeConstraint(TransientToolRuntimeValue):
    entity_kind: Literal["application"]
    mode: Literal["unrestricted", "restricted"]
    allowed_identities: frozenset[int | str]
    authority_instance_token: AuthorityInstanceToken
```

以下五个生产 collection executor 必须调用各自 Session-bound scoped Repository 方法，并把 constraint 作为不可选参数：

```text
list_applications_scoped(constraint, status=...)
list_application_events_scoped(constraint, month=..., application_id=..., event_type=...)
list_notes_scoped(constraint, application_id=...)
list_offers_scoped(constraint, status=...)
list_jd_analyses_scoped(constraint, application_id=...)
```

workspace/global/mode 传 `unrestricted` constraint；Application scope 传只包含当前 Application 的 `restricted` constraint。constraint 缺失、token 不匹配、kind 错误或 Repository 没有 scoped 实现均在查询前 fail-closed。生产 executor 不得在异常时调用原有无约束 `list()`。

五个 scoped 方法必须共享当前 Tool execution 的 Repository Session；`ToolExecutionContext.bind(session)` 必须同时绑定 Application、ApplicationEvent、Note、Offer 与 JD Analysis Repository，JD 路径也不得自行打开连接。restricted 查询必须把 `application_id` 约束下推到 SQL，禁止先读取全量结果再用 Python 过滤。

Application scope 的 collection 还固定以下可见性语义：

- `application_id IS NULL` 的 detached Note、Offer、JD Analysis 不可见；
- 父 Application 已删除、不可见或无法在同一 Session 内确认时，该子记录不可见；
- Repository 无法安全证明约束完整性时整个 Tool fail-closed，不返回“已经确认安全”的部分结果；
- workspace/global/mode 的 unrestricted 路径保持现有 detached 记录可见性与过滤语义。

同一 `ApplicationScopeConstraint` 还必须进入所有 Application-owned point-query Port，至少包括：

```text
get_application_scoped
get_application_event_scoped
get_note_scoped
get_offer_scoped
get_jd_analysis_scoped
```

restricted 模式的最终 SQL 必须同时约束 target ID、`application_id IN allowed_identities`（Application 本身约束其 ID）及 parent Application 可见性。点查询不得先按 target ID 读取正文后再判断 parent；无法命中时不区分“其他 Application / detached / missing”，统一使用安全 `scope_access_denied`。workspace/global/mode 使用 unrestricted constraint，保持既有 `not_found` 语义。

Read Tool 的 resolver recheck 不是最终数据授权：`get_application`、`get_application_event`、`get_note`（当前 Provider Catalog 未暴露时不新增工具）、`get_offer` 与 `get_jd_analysis` 的实际数据读取必须由上述单条 scoped SQL 完成。因此即使实体在 recheck 后、最终查询前被 reparent/delete，也不会返回跨 Application 正文。Write Tool 在 `BEGIN IMMEDIATE` 后用同一 Session 的 scoped Port 完成最终 Binding/mutable recheck；取得写锁后才允许现有 mutation executor 运行。

#### `optional_target(kind)`

- 显式目标存在时采用 `enforce_if_bound`；
- 目标省略时按 Provider 原契约执行独立记录语义；
- 不把当前 Application 自动注入参数；
- 目标 unavailable 不能伪装成 omitted。

首批仅用于 `add_note`。Application scope 中未提供 `application_id` 仍表示创建 standalone note；这是现有 Provider Contract 明确允许的行为。

#### `non_application_only`

- workspace、global 和 mode 允许；
- application scope 在 Provider Surface 交集阶段移除；
- 即使伪造 Provider response 绕过 Surface，Pipeline 仍返回 `permission_denied`；
- 不执行 Binding resolver、preflight 或 executor。

### 6.4 25 个工具的冻结矩阵

| # | 工具 | kind | Capability | Binding Contract | Application scope 行为 |
|---:|---|---|---|---|---|
| 1 | `list_applications` | read | `applications.read` | `scoped_collection(application)` | 仅返回当前 Application；status filter 仍生效 |
| 2 | `get_application` | read | `applications.read` | `enforce_if_bound(application)` | ID 必须匹配 |
| 3 | `create_application` | write | `applications.write` | `non_application_only` | Surface 移除且 Pipeline 拒绝 |
| 4 | `update_application_status` | write | `applications.write` | `enforce_if_bound(application)` | ID 必须匹配 |
| 5 | `list_application_events` | read | `application_events.read` | `scoped_collection(application)` | 缺 filter 时服务端限制到当前 Application |
| 6 | `get_application_event` | read | `application_events.read` | `enforce_if_bound(application)` | event parent 必须匹配 |
| 7 | `create_application_event` | write | `application_events.write` | `enforce_if_bound(application)` | `application_id` 必须匹配 |
| 8 | `update_application_event` | write | `application_events.write` | `enforce_if_bound(application)`；event parent 与 application arg 均 required | event parent 与显式 application 都必须匹配 |
| 9 | `delete_application_event` | write | `application_events.write` | `enforce_if_bound(application)` | event parent 必须匹配 |
| 10 | `list_notes` | read | `notes.read` | `scoped_collection(application)` | 缺 filter 时限制当前 Application |
| 11 | `add_note` | write | `notes.write` | `optional_target(application)` | 显式 application 必须匹配；省略仍为 standalone |
| 12 | `update_note` | write | `notes.write` | `enforce_if_bound(application)` | note parent 必须匹配；standalone note 在 Application scope 拒绝 |
| 13 | `delete_note` | write | `notes.write` | `enforce_if_bound(application)` | 同上 |
| 14 | `list_offers` | read | `offers.read` | `scoped_collection(application)` | 仅返回当前 Application 的 Offer |
| 15 | `get_offer` | read | `offers.read` | `enforce_if_bound(application)` | offer parent 必须匹配 |
| 16 | `compare_offers` | read | `offers.read` | `non_application_only` | Surface 移除且 Pipeline 拒绝跨 Application compare |
| 17 | `update_offer` | write | `offers.write` | `enforce_if_bound(application)` | offer parent 必须匹配 |
| 18 | `save_offer_assessment` | write | `offers.write` | `enforce_if_bound(application)` | offer parent 必须匹配 |
| 19 | `list_resumes` | read | `resumes.read` | `none` | Resume 是 workspace resource，行为不变 |
| 20 | `get_resume` | read | `resumes.read` | `enforce_if_bound(resume)` | V1 无持久 resume binding，因此为 unbound 并保持显式 ID 语义 |
| 21 | `resume_update_career_intent` | write | `resumes.write` | `enforce_if_bound(resume)` | 同上，仍需 HITL/Ledger |
| 22 | `resume_rewrite_highlight` | write | `resumes.write` | `enforce_if_bound(resume)` | 同上 |
| 23 | `list_resume_matches` | read | `resumes.read` | `enforce_if_bound(resume)` | 同上 |
| 24 | `list_jd_analyses` | read | `jd_analyses.read` | `scoped_collection(application)` | 缺 filter 时限制当前 Application |
| 25 | `get_jd_analysis` | read | `jd_analyses.read` | `enforce_if_bound(application)` | analysis parent 必须匹配 |

矩阵必须作为只读 canonical JSON golden 提交，测试不能自动更新、覆盖或接受新值。

`get_resume`、两个 Resume write Tool 与 `list_resume_matches` 在 V1 没有可持久化的 current-resume binding，因此其 `enforce_if_bound(resume)` 会得到 `unbound`，仍沿用显式 Resume ID 与既有 Repository 权限语义。这是本期明确接受的剩余实体隔离缺口，必须进入发布报告；后续应以独立的 persistent Resume scope 项目补齐，不能在本期完成声明中声称所有模型可见实体 Tool 已完成强制绑定。

## 7. Pipeline 阶段与失败语义

### 7.1 `prepare_call()`

顺序固定为：

```text
Authority phase/call-identity prelookup gate
→ Typed Catalog lookup
→ Authority/ToolSpec postlookup gate
→ JSON parse
→ JSON Schema validate
→ lossless typed decode
→ Capability gate
→ pre-resolver Scope Policy gate
→ Binding resolve
→ Binding policy decision
→ read-only preflight
→ PreparedToolCall / confirmation_required / ToolFailure
```

未知工具保持：

```text
category = validation_error
code = unknown_tool
Capability/Binding/Repository/preflight/executor = 0
Legacy fallback = 0
```

Capability 缺失：

```text
category = permission_denied
code = missing_capability
Binding resolver/Repository/preflight/executor = 0
```

`non_application_only` 在 pre-resolver Scope Policy gate 处理：Application scope 立即返回与其他 scope denial 相同的 `permission_denied + scope_access_denied`，Binding resolver、目标 Repository、preflight 和 executor 均为 0。其他 Binding Contract 才能进入 resolver；任何实现都不得为了决定是否允许访问而先查询目标实体。

Binding 拒绝使用：

```text
category = permission_denied
code = scope_access_denied
compatibility_detail = 既有权限失败 detail
preflight/executor = 0
```

`BindingDecision` 可以在瞬态内区分 `binding_mismatched / binding_detached / binding_unavailable / scope_forbidden`，但这些 reason 不得成为 `ToolFailure.code` 或进入模型、HTTP、SSE、Journal、日志。Application scope 内，下列情况的外部投影必须逐字段完全相同：

```text
真实存在但属于其他 Application
记录存在但没有 Application parent
记录不存在或无法安全解析
```

它们统一得到 `permission_denied + scope_access_denied + 既有权限失败文本`。HTTP status、SSE payload、ToolMessage、兼容字符串和后续 Agent Loop 控制流相同；只允许 resolver 内部所需的有界 Repository lookup 次数不同，下游 preflight/executor 均为 0。

对 New Turn 中已经通过 `ModelCallSurfaceBinding`、但在 Pipeline 内因 validation/Capability/Binding/preflight 被拒绝的 ToolCall，继续生成既有兼容 ToolMessage，并严格按 Agent Loop 基线的 read/write batch 规则决定是否进入下一次模型调用。这是正常 Agent continuation，不是 Provider fallback 或隐式 retry；同一个 ToolCall 不得再次 prepare 或执行，写工具不创建 Pending。Approved Write bootstrap 不适用此规则：origin executor 前失败不生成 ToolMessage、不加载 continuation messages、不进入 Provider loop。

`not_found` 只能在 Capability 已通过、且当前 Scope 允许该目标后由既有 Repository 语义产生。Application scope 对一个属于其他 Application 的真实 ID 和一个无法解析的 ID，都不能向模型暴露归属差异。

普通 `Exception` 映射为封闭内部失败；`asyncio.CancelledError`、`KeyboardInterrupt`、`SystemExit` 和其他 `BaseException` 原样传播。任何失败不得触发旧路径或第二次 resolver/executor。

`PreparedToolCall` 只新增不可序列化的 Authority instance token 和安全 `BindingAudit`；不保存原始 target identity、ORM 或 Repository 结果。

### 7.2 Read execution

```text
Prepared Authority token 与当前 Context 匹配
→ 立即重新执行 capability assertion
→ 重新解析可变 Binding
→ Binding policy decision
→ 生成并验证 ApplicationScopeConstraint
→ tool.started
→ executor 通过最终 scoped SQL 恰好读取一次
→ tool.completed | tool.failed
```

Read Binding recheck 只负责早期拒绝；最终数据授权由 executor 内的单条 scoped SQL 完成。restricted point/collection query 无法命中时，不返回任何已读取正文，并使用统一 `ToolOutcome(permission_denied, scope_access_denied)` 与既有权限失败 compatibility text；因此 recheck 与 executor 之间发生 reparent/delete 也不会泄漏跨 Application 数据。

该 race 已发生在 `tool.started` 后，投影固定为：

```text
ToolOutcome.category/code = permission_denied / scope_access_denied
Compatibility ToolMessage = 与执行前同工具 scope denial 逐字段相同
Journal = tool.started → tool.failed(failure_category=tool_error)
executor = 1；scoped Repository final query = 1
Provider fallback/retry = 0
```

Agent Loop 把它作为普通 read Tool failure record：全只读 batch 继续后续只读 ToolCall，随后是否进入下一 model step 完全按 Agent Loop 基线与最大迭代限制；sync/SSE 使用同一 typed record 和 transport projector，不新增事件/字段。HTTP/SSE、ToolMessage、日志和 Journal 都不得暴露 missing/reparent/cross-Application 差异。本期不为 read tool 新增长事务或全局锁，workspace/global/mode unrestricted query 保持既有 Repository 语义。

### 7.3 Approved write execution

事务外：

```text
重建新的 ApprovalExecutionAuthority
→ prepare_call()
→ Schema/decode/capability/binding/preflight
```

权威事务内：

```text
BEGIN IMMEDIATE
→ reload Operation / Conversation / Pending
→ 校验锁内 Conversation Scope 与 provisional Authority 一致
→ Application scope 时用同一 Session 重新确认 parent Application 当前可见
→ 用内存 Ledger key 计算并校验 authorization_scope_fingerprint
→ Session-bound Binding resolver 与 policy recheck
→ mutable validator / revision / stale-state recheck
→ Pending claim/CAS
→ 创建并匹配 ExecutionAuthorization
→ tool.started
→ executor 恰好一次
→ result/transport/required undo 投影
→ Ledger terminal + 领域写入同 commit
```

规则：

- Approval Authority 下 `prepare_call()` 返回的 `confirmation_required` 只是一项内部 static-policy 结果；它不能再次创建 Pending、写 `approval.requested`、interrupt 或要求第二次确认。只有随后锁内 Scope/Binding/mutable recheck、claim 和 ExecutionAuthorization 全部匹配才直接进入 executor；任一不匹配返回既有 stale/conflict，executor 为 0；
- Binding recheck 必须发生在 claim 和 executor 前；
- Application scope 的 parent visibility 在锁内失败时返回 `stale_state + authorization_scope_unavailable`，整个事务回滚，Operation/Pending 保持 proposed，用户仍可 retry/modify/reject；不进入 WriteContract terminal failure 映射；
- 只有 WriteContract 显式映射的稳定领域 Binding/mutable failure（封闭的 not-found/stale/conflict/scope denial）才能按 Phase 3 在同一事务中 claim 并提交 terminal failed；executor 为 0，且不产生 `tool.started/completed/failed`；
- SQLite busy/I/O、Repository/Resolver 普通异常、未映射异常、`internal_error`、codec/canonical/projector 缺陷属于 retryable infrastructure/internal failure：回滚整个事务、Operation 保持 proposed，不 claim、不生成 origin ToolMessage、不进入 Provider loop；
- Approved Write 的任何 executor 前失败都由 Runtime 按 Agent Loop Approved Write Seed 契约交付，不加载 continuation messages；只有已获得 terminal record 的 origin 才能进入 terminal 后的新 continuation Segment；
- claim 或 authorization match 失败时 executor 为 0；
- executor 自身抛普通异常时调用恰好 1 次并按既有 Ledger 分类；
- Journal、renderer、transport、delivery 或后续 continuation 失败不得重跑 executor；
- Repository/API 已有归属、revision 和 CAS 校验不得删除。

Approved Write 各阶段的收敛表固定为：

| 阶段 | Operation / Pending | 本次交付与 Loop | 后续 |
|---|---|---|---|
| 事务外 `prepare_call()` 返回 validation/capability/binding/preflight `ToolFailure` | proposed、Pending 原样保留且未 claim | 使用 Agent Loop 最终基线的 approve/modify pre-executor failure HTTP/SSE golden；origin ToolMessage=0、Provider continuation=0 | 同一请求不重试；以后可 approve/modify/reject |
| 事务外 resolver/Repository 普通异常 | proposed、Pending 原样保留 | 既有 safe retryable confirmation failure；ToolMessage/Provider=0 | 同一请求不重试；以后可重试或 reject |
| 锁内 Scope/fingerprint/identity/parent visibility stale | 回滚，proposed、Pending 保留 | 对应封闭 stale code；ToolMessage/Provider=0 | 可 modify/reject；仅仍可恢复的状态允许 retry |
| 锁内 WriteContract 显式 `terminal_domain_failure` | claim 后 terminal failed，按 Phase 3 清理/交付 | executor=0、无 `tool.started`、无 origin ToolMessage/Provider continuation | 后续只 replay，不重试 executor |
| 锁内 infrastructure/internal failure | 整个事务回滚，proposed、Pending 保留 | safe retryable；ToolMessage/Provider=0 | 后续请求可重试 |
| claim/CAS loser | fresh Ledger read 决定 terminal replay 或 proposed conflict | executor/ToolMessage/Provider=0 | 按 Phase 3 路由 |
| executor 返回或抛显式领域异常 | 单次调用内恰好 1 次，按 Phase 3 committed/failed/commit-unknown | 只有取得权威 terminal record 后才允许创建 continuation Segment | terminal replay 或结果未知对账 |

实施前从 Agent Loop 最终合并提交捕获上述 pre-executor sync/SSE status、event、code 与 body golden；本项目不得借机更换用户可见错误协议。任何 `prepare_call()` 失败都不能在事务外把 Operation 永久改为 failed，稳定可终结结果必须在锁内重新确认并由 WriteContract 显式映射。

### 7.4 TOCTOU

禁止只信任 prepare 阶段的 `BindingAudit`：

```text
prepare matched
→ 用户等待确认
→ 其他事务修改 event/note/offer/analysis 的 Application 归属或删除记录
→ approve
```

最终结果必须由 `BEGIN IMMEDIATE` 后的 Session-bound resolver 决定。若归属已变化，executor 为 0；若记录已删除，则按不泄露旧归属的 safe failure 收敛。

## 8. Agent Loop、确认和 Provider-free 路径

### 8.1 New Turn

一个 New Turn Seed 创建一次 Authority。Agent Loop 内的多个 model call、read-tool loop 和 Provider fallback 都复用它。工具写入不能在同一 Segment 内扩展 Authority。

当模型提出需要确认的写工具时，Pending/Operation 持久化事务必须再次确认 Conversation Scope 仍与该 Authority 相同。Scope 已变化时不能展示一张基于旧 Scope 的新确认卡。

#### 8.1.1 Typed Pending Proposal Port

Typed proposal 不得继续使用能够从 `conversation_id + pending` 自行猜测 Authority 的通用 helper。生产入口固定为：

```text
persist_typed_pending(
    session,
    conversation_id,
    pending,
    PendingAuthorityClaim,
)
```

`PendingAuthorityClaim` 是 `repr=False`、不可序列化的瞬态值，只携带 Authority instance token、conversation scope revision、canonical scope envelope、capability/binding policy version 与静态 policy fingerprints；不携带请求 payload、Provider page context、实体正文、凭据或 Journal key。

同一 `BEGIN IMMEDIATE` 事务必须：

```text
reload Conversation
→ 重新 canonicalize trusted Scope 并读取 scope revision
→ Application scope 时用同一 Session 重新确认 parent Application 当前可见
→ 与 PendingAuthorityClaim 做 constant-time authority match
→ 使用事务前已加载的 Ledger key 计算 authorization_scope_fingerprint
→ 原子写入 Pending + Typed primary Operation
```

该 Port 覆盖所有 Typed Pending 创建与替换调用点：New Turn 首张 Pending、approve/modify continuation 产生的 chained Pending、原 Pending 的原子替换，以及 delivery transaction 中的新 Operation proposal。任何调用点缺少有效 claim 都必须在写入前失败，不能调用旧 `_create_operation_for_pending(session, conversation_id, pending)` 或从 Pending 参数反推 Scope。

若锁内 Scope/revision 不匹配，Pending、Operation 与对应 assistant ToolCall 持久消息整个事务回滚，不能留下悬空链：New Turn 沿用安全 stale 交付；已 terminal origin Operation 的 chained continuation 按 Phase 3 既有 delivery failure/fallback 原子收敛，origin terminal 不回滚，Provider、Tool 与 proposal 不重跑。

Application 在 Source Loader 后被删除/隐藏也按同一规则处理，即使 Tool 是省略目标的 standalone `add_note`：Application scope 本身必须仍有效，不能因为 Tool 没有 target resolver 而跳过 parent visibility。无法确认可见性或 Repository 异常时回滚，不创建部分 Pending/Operation。

Legacy deterministic 只允许走单独的 `persist_legacy_pending(...)` 封闭入口，不接收 Typed claim，fingerprint 继续允许为空；compensation Operation 不创建 Pending，也不走上述 Port。

### 8.2 Approve / Modify

批准和修改都必须：

```text
Ledger-first terminal/proposed routing
→ proposed 才读取可信 Pending/Conversation 并运行 ApprovalAuthorityResolver
→ 构造 provisional ApprovalExecutionAuthority
→ 从可信 Pending 重建 ToolCall
→ prepare_call()
→ BEGIN IMMEDIATE 后做 scope fingerprint / Binding / mutable recheck
→ execute_prepared()
```

修改后的 effective args 重新执行 Schema、decode、Capability、Binding 和 mutable checks。修改目标 ID 不能沿用旧 Binding 结果。

Origin write terminal commit 后如需 Provider continuation，必须创建新的 continuation Segment，在领域事务释放后重新运行 Source Loader，并构造新的 Authority 与 Model Surface；旧确认执行阶段的 Authority 到此失效。Delivery generation/owner 可以继续引用 origin Operation 作为交付身份，但不得被当作执行 Authority。若 continuation 提出 chained write，新 Operation 只能绑定 continuation Segment 的 Authority/scope revision/fingerprint；下一次确认再创建新的执行/continuation Segment。Source Loader、Authority 或 projection 失败不得回滚 origin terminal，也不得复用旧 Authority 或重跑 executor。

### 8.3 Reject

```text
token / Pending / Ledger identity
→ rejection CAS
→ terminal rejected commit
→ 既有 deterministic rejection delivery
```

下列调用均为 0：

```text
Source Loader
ApprovalAuthorityResolver
Context Projector
Provider
Typed Catalog
Schema/decode
Capability
Binding
target Repository
preflight
executor
```

参数失效、目标实体删除、Scope 改变或 Policy 升级都不能阻止拒绝。唯一例外是 owning Conversation 已删除或 Operation 的 `conversation_id` 已被置空：此时可信 Operation identity 已不可用于 conversation-bound CAS，沿用 Phase 3 的 `operation_unavailable`，Provider、resolver、Tool 和 executor 均为 0；它不能被重新绑定到其他 Conversation。目标实体删除与 owning Conversation 删除必须使用两组独立 reject golden。

### 8.4 Terminal replay / delivery recovery

保持 Ledger-first、Provider-free：

- 不读 Pending；
- 不重新加载 Context；
- 不构造 Authority；
- 不运行 Projector、Provider、Tool 或 Repository preflight；
- 只验证 Operation request、terminal payload、delivery generation/owner 和持久消息。

上述正常 replay 前提是 Operation 仍绑定现存 Conversation；Conversation 已删除并触发 FK `SET NULL` 时按 §11 的现有 unavailable/unknown 收敛，不重新创建 Conversation 或消息。

### 8.5 Legacy deterministic

以下 3 个工具仍是唯一 Legacy 集合：

```text
save_application_jd_version
create_application_submission_snapshot
record_application_outcome
```

它们：

- 不进入 25 Tool Provider Surface；
- 不进入模型 ToolCall Dispatcher；
- 不从客户端 `tool_name` 单独路由；
- 不自动套用 Typed `BindingContract`；
- 保持服务端 deterministic 创建、专用确认、CAS、幂等、Ledger 和恢复路径；
- Typed Pipeline 的 Authority 失败绝不 fallback 到 Legacy。

Legacy Authority 统一另开设计，本期完成声明不得把这 3 个工具算作已迁移。

## 9. Context Projector 与 Provider Surface

### 9.1 处理顺序

```text
Context Selector 产生按 Catalog 原顺序的选择
→ 按 Capability Profile 移除无权工具
→ 按 Scope Policy 移除 application scope 禁止工具
→ 调用 Context Projector 现有只读 DependencyPolicyV1 校验 closure 仍完整
→ 冻结完整 Provider envelopes 与 fingerprint
→ 计算剩余 token/byte budget
```

Authority 交集必须发生在历史预算分配前，因为最终工具 envelope 占用输入预算。

### 9.2 安全规则

- 最终 Surface 只能来自完整 25 Tool Catalog；
- Legacy 永远不能进入；
- Provider contract payload 不能被 Authority 改写；
- 交集只删除完整 envelope，不修改 Schema；
- dependency 被删除但依赖者仍保留时 fail-closed，不能回退完整 25；
- 交集为空或 Policy/Selector 异常时 Provider 0，不返回未知部分 Surface；
- 同一个 `model_call_id` 的 fallback 复用同一 Frozen Surface 和 Authority；
- 下一次 Agent Loop model call 可根据新 runtime messages 重投影，但 Authority 不变；
- 下一 Segment 才重新解析 Authority Policy。

本期不复制 dependency metadata。`context_projector.selector` 必须把现有 `_DEPENDENCIES/_dependency_closure` 收口为只读 `DependencyPolicyV1.validate_closed(selected_names, catalog_names)` Port，并冻结 `dependency_policy_version`、25-name coverage 与 canonical golden；Selector 和 Authority Surface Intersection 共享同一个实例。未知工具、未知依赖、缺失节点、环、版本不匹配或 Port 异常均在 Provider 前 fail-closed。这个窄 Port 只暴露 closure 校验，不迁移 domain/lexical/Ledger 元数据；完整 ownership 仍留给后续 Tool Metadata Convergence。

### 9.3 Dispatcher 双重门禁

Provider 返回后仍先由当前 `ModelCallSurfaceBinding` 验证工具名。完整 Catalog 能解析但本次未暴露的工具继续按 Phase 4 fail-closed：

```text
model.failed
assistant/tool message = 0
Dispatcher/Capability/Binding/executor = 0
Provider fallback = 0（已有可观察响应后）
```

通过 Surface Binding 后，Dispatcher 才查询 Typed Catalog，并由 Pipeline 再执行 Capability/Binding。不得重新运行 Selector，也不得从工具名推断 Authority。

## 10. Journal、诊断与隐私

### 10.1 Journal 时序

继续沿用：

```text
无需确认的只读/已批准写入：
tool.proposed
→ tool.started（executor 调用前）
→ tool.completed | tool.failed

等待确认：
tool.proposed
→ approval.requested（Pending/Operation 原子持久化成功后）
→ run.waiting_confirmation
→ segment.finished(suspended)

批准/修改：
approval.decided(approved | edited)（claim/CAS 成功后）
→ tool.started
→ tool.completed | tool.failed

拒绝：
approval.decided(rejected)（rejection CAS 成功后）
→ 不产生 tool.started/completed/failed
```

模型调用继续沿用 Phase 4：

```text
context.captured
→ model.requested
→ model.completed | model.failed
```

`model.requested` 只在 Authority Surface、Projector、完整预算与 Adapter preflight 全部通过后、第一次真实 Provider 网络调用前产生；这些阶段 fail-closed 时不产生。冻结候选链中的 fallback attempts 共用一个 logical `model_call_id` 和一组 model events，不记录 attempt registry。Journal/Snapshot 写入 fail-open，不改变 Frozen Surface、Provider 或 Agent 结果；Authority Surface Intersection 不新增 Event type。

固定边界：

- ToolCall 已经进入 Typed Pipeline 时，`tool.proposed` 按现有规则写入；
- `approval.requested` 只在首次 Pending/Operation 原子持久化成功后写入；Pending/replay/duplicate request 不重复写；
- `approval.decided` 只在 approve/modify execution claim 或 reject CAS 成功后写入对应固定 decision；claim/CAS loser 不伪造决定事件；
- `tool.proposed.proposal_outcome` 只表示观察到模型 ToolCall 时由 ToolSpec static confirmation policy 得到的 proposal/policy intent，不表示 Schema/decode、Capability、Binding、preflight、claim 或 executor 已通过；
- Capability、Binding、scope、preflight、stale、claim 或 authorization 在 executor 前失败：不写 `tool.started/completed/failed`；
- 只有 executor 即将真实调用时才写 `tool.started`；
- executor 返回或抛普通 `Exception` 后才写 completed/failed；
- Journal fail-open，不改变既定 Outcome，不触发重新授权、重投影或重跑 executor；
- 不新增 Journal Event type，不提升 Event Schema version。

Schema/decode、Capability、Binding 和 preflight denial 的 golden 都必须保持 proposal-only 序列；Trace/UI 不得把 `proposal_outcome` 解释为执行授权结果，也不得为消除该歧义向 V1 Event payload 增加字段。

### 10.2 安全诊断

允许的瞬态 code：

```text
capability_missing
binding_mismatched
binding_unavailable
scope_forbidden
authorization_scope_changed
authority_policy_invalid
```

诊断只能包含封闭枚举、布尔值、有界计数、policy version 和工具名。禁止记录：

- Capability 的原始集合；
- Application/Resume/Event/Offer/Note/JD Analysis ID；
- context_ref、mode 原文或附件身份；
- Tool args、typed result、Provider answer；
- confirmation token、operation owner/lease；
- Repository 对象、异常对象、异常文本或 traceback。

`authorization_scope_fingerprint` 只存 Write Operation 私有列，不进入 Journal Manifest。Trace 只能验证字段格式和 Ledger 完整性；不能从 HMAC 恢复 Scope。

## 11. Schema 与迁移

建议迁移标识：

```text
0028_scoped_tool_authority
```

该编号基于设计捕获基线；实施时若 Agent Loop 最终合并提交已经占用 0028，必须顺延到下一个连续编号并同步所有 gate，不得复用或改写已发布 migration。

变更：

```text
conversations.scope_revision
write_operations.authorization_scope_fingerprint
```

约束：

- migration 先把 legacy `mode IS NULL OR mode=''` 规范为 `general`，再为既有 Conversation 回填 revision 0；不得把其他非法/未知 mode 静默改成 general；
- `scope_revision` 是 `INTEGER NOT NULL DEFAULT 0`；新库 model 使用 `typeof='integer' AND BETWEEN 0 AND 9223372036854775807` CHECK，Conversation INSERT trigger 额外要求 `NEW.scope_revision=0`；
- Conversation mode INSERT/UPDATE trigger 要求数据库值为通过 §4.2 词法校验的 canonical 非空字符串；请求 null/empty 的 default 映射只能发生在 API/Repository 边界；
- Conversation trigger 以 stored `context_type/context_ref/mode` 的 SQLite 值逐字段比较；任一 raw 值变化必须 `OLD.scope_revision + 1`，全部未变化必须保持原值；
- `set_context_scope(..., expected_scope_revision)` 使用 CAS 并与 scope 字段更新同 commit；未知 context type 不能由请求写入新 Conversation；
- `authorization_scope_fingerprint` nullable，用于兼容 terminal、Legacy、compensation 和升级前 proposed 行；
- 新 Typed primary proposal 的 Repository API 强制非空、`hmac-sha256:<64 hex>`；
- 新库模型与旧库 additive migration 一致；
- SQLite `BEFORE INSERT` trigger 阻止新 Typed primary Operation 写入空、长度错误、前缀错误或非小写 hex fingerprint；
- SQLite `BEFORE UPDATE OF authorization_scope_fingerprint` trigger 保证该列写入后不可改变，也禁止旧 null 行运行时补写；
- Write Operation authority identity trigger 使 `operation_role/adapter_kind/tool_name/tool_call_id/fingerprint_key_id/proposal_fingerprint/confirmation_token_fingerprint/parent_operation_id` 插入后不可改变；`conversation_id` 禁止改绑到另一 Conversation，只保留既有 FK 删除时 `SET NULL` 的兼容路径，null Operation 不能 approve；
- SQLite status trigger 规定旧 `typed + primary + proposed + fingerprint null` 只能保持 proposed 或转为 rejected，不能转为 committed/failed；
- SQLite status trigger 规定任何 `typed + primary + proposed → committed/failed` 都必须已经携带合法且未改变的 fingerprint；
- 旧 terminal、Legacy 和 compensation null 行继续允许 delivery/replay 所需的非 fingerprint 更新；
- trigger 对直接 SQL 和绕过 Repository 的 INSERT/UPDATE 同样生效；
- trigger 不回填、不猜测旧 proposed Scope；
- migration 不访问 Provider、Journal key 或网络；
- Ledger key 在应用启动时加载；事务外可以准备 canonical envelope，但最终 HMAC 必须从锁内 Conversation primitive 计算并 constant-time compare；事务内不得做文件、Keyring、Provider 或网络 I/O；
- 迁移可重复运行，备份/恢复与 Windows SQLite 行为沿用 Phase 3 gate。

SQLite trigger 无法区分 FK action 产生的 `conversation_id=NULL` 与直接 SQL 主动清空，因此本期机械规则只禁止 non-null ID 改绑以及 null→non-null；允许置空被明确视为 fail-closed 的销毁操作，不能扩大权限。Conversation 删除沿用现有行为：ChatMessage/Pending 随 Conversation 消失，Operation 保留且 `conversation_id=NULL`。此时 proposed Operation 的 approve/modify/reject 固定为现有 `operation_unavailable`，Provider、Authority resolver、Tool 与 executor 为 0；terminal payload 本身仍保持不可变，但 conversation-bound replay/delivery recovery 沿用现有 `operation_unavailable/operation_delivery_unknown`，不宣称删除 Conversation 后仍可交付。直接 SQL 清空只能把 Operation 推入同一不可用状态，不能将其绑定到其他 Conversation。

本期不增加公开 Schema、API DTO、前端 type 或 UI 字段。

## 12. 模块与依赖方向

建议结构：

```text
src/offerpilot/ai/tool_authority/
  contracts.py       纯 dataclass / enum / transient contracts
  policy.py          Capability Profile 与 Binding decision 纯函数
  composition.py     从 Frozen Source DTO 构造 Authority
  fingerprint.py     Ledger scope HMAC canonical envelope

src/offerpilot/ai/tool_runtime/
  contracts.py       ToolSpec 引用最小 BindingContract
  context.py         ToolExecutionContext 持有 Authority
  pipeline.py        capability/binding gate 与 recheck

src/offerpilot/ai/tool_specs/
  six domain specs   声明 25 个 BindingContract

src/offerpilot/context_projector/
  authority_surface.py  只消费 safe AuthoritySurfaceView
  selector.py           暴露现有 DependencyPolicyV1 窄 Port

src/offerpilot/ai/write_operations.py
  scope fingerprint 持久化与事务内 recheck
```

依赖规则：

- `tool_authority/contracts.py` 不导入 repositories、Tool Specs、Pilot Runtime 或 Context Projector；
- domain Tool Specs 可以导入 authority leaf contracts；
- core policy 不反向导入六个 domain spec；
- composition root 负责组装 25 个策略并进行闭集校验；
- Context Projector 只能看到 `allowed_tool_names/profile/version`，看不到实体 ID；
- API 只调用 composition root，不自行创建全权限集合或 current binding；
- Write Operation 不依赖 Context Projector；
- 不保留 audit-only production bypass、feature flag、shadow execution 或旧 fallback。

## 13. 并发与线性化规则

### 13.1 Segment 冻结

- Authority 创建后，当前 Segment 不读取动态 Capability 配置；
- 同一 Segment 多次 model call 使用相同 Profile/Scope；
- 页面切换或请求 page context 变化不影响当前 Segment；
- 下一 Segment 读取新 Conversation Scope 和当前 policy version；
- fallback 不能重新计算 Authority 或 Surface。

### 13.2 Confirmation 竞态

双连接结果必须固定：

```text
approve 事务先锁定并验证 Scope
→ 按已验证的当前 Scope 执行
→ Context 更新随后提交
```

```text
Context 更新先提交
→ approve scope fingerprint mismatch
→ executor 0
```

两个 approve 仍只有一个 claim/executor winner；approve 与 reject 仍只有一个 Ledger terminal winner；late delivery 仍由 generation/owner fencing 丢弃。

### 13.3 Entity reparent/delete

- prepare matched 后实体归属变化，事务内 recheck 拒绝；
- prepare matched 后实体删除，按 safe unavailable/not-found 规则收敛；
- resolver 不能缓存 ORM；
- prepared object 不能跨请求复用；
- Journal degraded 不放宽 Authority；
- cancellation/BaseException 不转换成 permission failure，清理后原样传播。

## 14. Golden、测试与机械门禁

### 14.1 基线资产

必须从 Agent Loop 最终合并提交独立捕获并只读提交：

1. 完整 25 Tool Provider envelope、顺序和 Schema fingerprint；
2. 25 Tool Capability/Binding Contract manifest；
3. 3 Legacy deterministic 精确名单；
4. Agent Loop sync/stream/confirmation/replay 调用次数基线；
5. HTTP/SSE 兼容文本和事件序列。
6. Context Projector `DependencyPolicyV1` version、25-name coverage 与 closure canonical manifest。

Golden 使用纯合成数据和 canonical JSON，不保存 SQLite、用户内容、Prompt、结果、密钥、实体 ID或时间。测试不得自动生成、覆盖或接受新 manifest。

### 14.2 Capability 公共测试

覆盖：

- V1 Profile 精确 11 项；
- enum 新增值不会自动授权；
- empty/read-only/write-only/缺一项/未知 Profile；
- 客户端伪造 capability 字段；
- Profile 构造异常；
- 缺 capability 时 resolver、Repository、preflight、executor 均为 0；
- `BaseException` 原样传播。
- Approval Authority 调 read/Provider/Surface/Pending、Segment Authority 无 authorization 执行 write、伪造 use/call identity 均在 Catalog/resolver 前失败；

### 14.3 Binding 公共测试

覆盖：

- matched/mismatched/unbound/unavailable；
- resolved/omitted/detached/unavailable target；
- 相同 + 不同；
- 相同 + unavailable；
- 不同 + unavailable；
- mixed entity kind Catalog 初始化失败；
- 相同数字、不同 kind；
- resolver Exception/BaseException/非法 identity；
- scoped collection 缺 filter、匹配 filter、错误 filter；
- scope constraint 失败绝不退回全量 query；
- 五个 scoped collection Repository 方法的 constraint 为不可选参数；
- 五个 scoped collection Repository 与 resolver 使用同一个 caller Session，不得隐式 checkout；
- restricted SQL predicate、detached row、已删除/不可见 parent 与无法证明完整性时的整体 fail-closed；
- Application constraint 与 workspace/global/mode unrestricted constraint；
- Application scope 的 `non_application_only` 在 resolver 前拒绝，resolver/Repository/preflight/executor spy 均为 0；
- resolver 普通异常、`BaseException`、副作用调用和隐式 Session 创建门禁；
- optional target omitted、detached 与 unavailable 不混淆；
- update event 的两个 required application resolver 组合。
- 同一工具对“其他 Application 的真实 ID / detached record / 不存在 ID”产生逐字段相同的公开 failure golden。
- point resolver 通过后并发 reparent/delete，最终 scoped SQL 仍不返回正文；
- race-after-started 固定为 permission_denied/scope_access_denied、Journal tool_error、executor/query 各 1 次；
- workspace/global/mode 的 resolved、detached、unavailable 均先聚合为 unbound 并保持既有语义。

### 14.4 25 Tool 矩阵

每个工具至少验证：

- workspace/global 正常成功路径；
- application scope 的矩阵预期；
- capability 缺失；
- 该工具声明可能出现的 Binding 状态；
- 实体不存在；
- Provider/Tool 调用次数；
- 既有 Repository guard 仍执行。

不为不可能出现的状态制造虚假场景；所有公共状态由 Pipeline 公共测试完整覆盖。

重点 golden：

- A scope 读取/修改 B Application；
- event/note/offer/JD analysis parent 不匹配；
- update event 的两个 application target 不一致；
- Application collection 不泄漏其他 Application；
- standalone note；
- compare_offers 仅 workspace/global；
- Resume 无持久 binding 时的明确 unbound 行为；
- 相同显示名称、不同 ID；
- page context 与 Conversation scope 冲突。

### 14.5 Confirmation / Ledger

覆盖：

- approve、modify、reject；
- Pending 首次创建、duplicate/replay、approve/modify/reject 的完整 `approval.requested/approval.decided/run.waiting_confirmation/segment.finished` Journal golden；Journal degraded 时业务与确认结果保持不变；
- 修改 args 到其他 Application；
- Pending 后 Scope 改变；
- Pending 后 Scope A→B→A，单调 revision 仍使 approve/modify stale；
- Source Loader 后、Pending transaction 前删除/隐藏 Application，不创建 proposal；
- Pending 后、approve 锁前删除/隐藏 Application（含 standalone `add_note`），保持 proposed/rejectable 且 executor 0；
- 目标实体删除仍可 reject；owning Conversation 删除则 reject 为 `operation_unavailable`，两者不可合并为同一 golden；
- Pending 后 Policy version 改变；
- upgrade 前 null fingerprint；
- 旧 null fingerprint 在 Source Loader/prepare 前短路；Conversation identity 仍存在时可 reject，`conversation_id IS NULL` 时 approve/modify/reject 均为 `operation_unavailable`；
- 新 insert、fingerprint/Operation authority identity immutable、旧 null 只能 reject 和 direct SQL trigger；
- direct SQL 修改 adapter kind/tool name/role/tool-call/key identity 或把 conversation 改绑到另一 ID 被拒绝；
- Conversation 删除后的 proposed approve/modify/reject 为 operation_unavailable、terminal delivery 为既有 unknown；FK/direct null 都不能重新绑定或执行；
- Conversation Scope CAS、revision 恰好递增、无变化不递增及 direct SQL trigger；
- mode missing/null/empty/`general` canonical truth table、migration normalization、HMAC 一致性与大小写变体区分；
- context_ref wire/persisted/internal 全真值表，string/integer application ID 等价且非法 coercion 为 0；
- 新 Conversation 默认 workspace 与既有 Conversation 省略 scope 字段不重置/不递增 revision；
- 新 Conversation revision 非零 INSERT 被拒绝，迁移旧行与新建行均从 0 开始；
- 历史 workspace/global/mode 非空 ref 被安全忽略；未知/custom context、非法 Application ref/mode 的新旧请求 fail-closed、restart、reject/replay；
- terminal replay 在当前 Scope 改变后仍 Provider/Tool 0；
- chained Pending 保存新 fingerprint；
- New Turn、Pending replacement、approve/modify chained continuation 与 delivery transaction 全部只能通过 `persist_typed_pending(..., PendingAuthorityClaim)`；
- 缺 claim、伪造 claim、过期 Authority token、Scope 在事务前后变化均不能创建 Pending/Operation；
- Legacy Pending 与 compensation Operation 不误入 Typed proposal Port；
- prepare 后归属变化的双连接 barrier；
- 锁内 Pending revision、Operation/tool-call/name/effective-args digest 不匹配时 executor 0；
- 两个 approve/modify、Pending replacement 与旧参数 late request 并发；
- 事务外 prepare ToolFailure/Exception、锁内 stale/domain/infra、claim loser、executor terminal 的完整收敛表；
- claim winner、delivery takeover、late Bundle；
- executor 0 的路径不产生 `tool.started/completed/failed`。
- approve/modify 的内部 `confirmation_required` 不创建第二张 Pending、不重复 `approval.requested`、不 interrupt；锁内授权不匹配时 stale/conflict 且 executor 0。

### 14.6 Agent Loop 与 Transport

覆盖：

- read + read、write + read、read + write、write + write；
- 前一 read 权限失败后后续 read 的基线循环行为；
- sync/stream 完全一致；
- Provider fallback 复用同一 Surface/Authority；
- origin write terminal 后 continuation 重新 Source Load 并获得新 Segment Authority，旧 Approval Authority 不能进入 Provider loop；
- 未暴露工具在 Surface Binding 前 fail-closed；
- 暴露但 capability/binding 拒绝进入兼容 ToolMessage；
- Pending、最终文本和 chained Pending；
- SSE 断连、timeout、cancellation；
- read scope race-after-started 的 sync/SSE ToolMessage、transport、后续 batch/loop 完全一致；
- Provider retry/fallback、Tool executor、claim 均无额外调用。

### 14.7 Journal 与隐私

覆盖：

- pre-executor permission failure 只有既有 proposal，不伪造 terminal event；
- schema/decode/capability/binding/preflight denial 的 `proposal_outcome` 均只表示 static policy intent；
- Authority/Surface/Projector/Adapter preflight 失败无 model.requested；fallback attempts 仍只有一组 logical model events；
- executor 开始后的完整 started/terminal；
- Journal degraded 不改变结果；
- checkpoint/serialization 中无 Authority、Binding resolution、capability 或 entity ID；
- 日志、SSE、HTTP、Manifest 和 Trace 隐私 canary；
- `scope_revision` 与 authorization scope fingerprint 不进入 Conversation/API/SSE/Prompt/Journal serialization；
- scope HMAC domain separation 和 constant-time compare。

### 14.8 AST / source gates

机械禁止：

- 生产代码使用 `frozenset(ToolCapability)` 自动授权；
- API/Agent 从 request payload 读取 capability 或 current binding；
- `ToolExecutionContext` 在 composition root 之外直接构造；
- 绕过 authority phase/spec gate，或把 Approval Authority 传给 Provider/Selector/read/Pending；
- Typed executor 绕过公共 Pipeline；
- Capability 之前调用 Binding resolver/Repository；
- `non_application_only` 在 Scope Policy gate 前调用 resolver/Repository；
- Binding resolver 写入、网络/Provider、文件/keyring、executor/renderer 或隐式创建 Session；
- 通过工具名、反射、动态字典或 `getattr` 绕过 policy；
- Typed Catalog miss 转入 Legacy；
- Legacy 名称进入 Provider Surface；
- audit-only/enforce feature flag、shadow、双轨或 fallback；
- Journal/日志/transport 序列化 Authority 或 target identity；
- 前端自行决定 Tool Capability/Binding；
- `auto_approve` 绕过 Authority。
- mixed-kind BindingContract 进入生产 Catalog；
- Authority Surface 复制 dependency metadata 或绕过共享 `DependencyPolicyV1`；
- Conversation Scope 字段绕过 `set_context_scope` CAS/revision Port；
- 五个 Application collection executor 调用无约束 `list()`，或 scoped Repository 接受 optional constraint；
- scoped Repository 在 constraint 错误时 fallback 到无约束 query；
- scoped Repository 读取全量后在 Python 过滤，或五个领域使用不同 Session；
- Application-bound point Tool 的最终数据查询调用无约束 `get()`，或在读取正文后才判断 parent；
- Typed Pending/Operation proposal 绕过 `PendingAuthorityClaim` Port，或从 request/provider/page context 推导可信 Scope。

## 15. 切换、回滚与发布门禁

### 15.1 切换策略

25 个 Typed Tool 一次性切换，不按工具长期双轨：

```text
Agent Loop 最终合并
→ 捕获只读 baseline golden
→ 实现完整 Policy/25 Tool matrix
→ 所有 RED/green gate 通过
→ drain 活跃 Chat 请求
→ 停止旧 binary 的 Chat 写入
→ 执行 0028 migration
→ 只启动新 binary
→ 全部 25 Tool strict enforcement
```

禁止：

- production feature flag；
- audit-only fallback；
- “Authority 失败时使用旧全权限 Context”；
- 部分工具 strict、部分工具 permissive；
- 新旧 Provider surface 双发或 shadow executor。

### 15.2 回滚边界

0028 只增加两个私有列及其 trigger/check，不改变公开 DTO；但迁移后旧 binary 不会维护 scope revision，也无法创建带 fingerprint 的 Typed proposal，因而不支持旧写入 binary 继续运行或原地代码回滚。

发现 Policy 问题时：

- fail-closed 停止受影响 Typed Tool；
- 修复 Policy/Contract 后重新发布；
- 不自动回退到全 Capability 或 audit-only Binding；
- 已 terminal Operation 继续 replay；
- 未 claim 的 permission failure 不改变领域数据；
- owning Conversation 仍存在的旧 proposed Pending 始终可 reject；Conversation 已删除或 `conversation_id IS NULL` 时按 `operation_unavailable` 收敛。

若必须回到旧 binary，只能停止应用并同时恢复 0028 前的数据库快照；不得在已迁移数据库上删除 trigger 后继续旧宽松执行。发布测试必须证明迁移后的旧 binary Typed proposal 写入被数据库拒绝，而新 binary、Legacy 和 terminal replay 保持预期行为。

### 15.3 发布级完成条件

- 25 Typed + 3 Legacy 精确分类，无新增、遗漏或重复；
- Provider contract golden canonical 等价；
- Capability/Binding manifest 与 Catalog 精确一致；
- 25 Tool scope matrix全部通过；
- sync/stream、approve/modify/reject/replay、chained Pending 全部通过；
- Conversation scope revision 与 Operation scope fingerprint migration、旧数据、触发器和恢复通过；
- HTTP/SSE、Pending、Ledger、Undo、业务副作用满足本设计兼容口径；
- Journal/Trace/隐私/serialization/AST gate 通过；
- backend manifest/并集/node ID/skip/aggregate gates 通过；
- 全量 pytest、Ruff、Mypy；
- 前端 test/build、static smoke、local verify；
- controlled real-AI verify；
- 浏览器 workspace/global/application Chat，跨 scope 拒绝，HITL approve/modify/reject；
- 独立 CR 无 P0/P1/P2；
- baseline allowlist、未跟踪文件、`git diff --check` 和 worktree clean；
- 发布报告明确安全行为变化、25/3 边界、0028 迁移和不承诺全局 exactly-once。

## 16. 完成声明与下一项目

本期只能声明：

> 25 个模型可见 Typed Tool 已按冻结矩阵执行服务端显式 Capability、Binding 或 collection scoping；Application-owned target/collection 在 Application scope 中强制隔离，Resume target 的 V1 unbound 与 `add_note` standalone 语义被明确记录且不宣称强制绑定；确认提案与可信 Scope revision 持久绑定，写入在 Ledger 事务内重新校验。3 个 Legacy deterministic Tool 继续隔离，Provider/HTTP/SSE/HITL 契约保持既定兼容边界。

不得声明：

- 3 个 Legacy 已迁移；
- 已有多用户 RBAC；
- 所有 workspace/global 显式 ID 都被实体绑定；
- Tool metadata 已完全单一来源；
- 跨请求或跨系统 exactly-once；
- Journal 已成为业务真值或事件溯源。

本期通过、合并并稳定后，下一独立项目为：

```text
Tool Metadata Convergence
```

它再负责把 Selector domains、lexical discovery、dependencies、Provider visibility、Ledger/Undo 静态集合和本期 Binding Contract 收敛到唯一的 `ToolSurfaceMetadata`，不得提前混入本项目。
