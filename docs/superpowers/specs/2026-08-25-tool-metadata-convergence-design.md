# Tool Metadata Convergence 设计

**状态：已复审通过**

**固定 baseline：`0c10e05e256eb757d5f89a8b009dcea193f2fc78`**

**分支：`refactor/20260825-tool-metadata-convergence`**

**Worktree：`D:\Users\yuqi.chen\offerpilot\.worktrees\refactor-20260825-tool-metadata-convergence`**

> 本文只定义本阶段架构，不修改生产代码。设计已复审通过，测试先行实施计划另文维护。

## 1. 背景与问题

OfferPilot 已依次完成：

```text
Durable Execution Journal
→ Tool Execution Pipeline
→ Write Operation Ledger
→ Context Projector
→ Pilot Runtime Orchestration Extraction
→ Agent Loop Unification
→ Scoped Capability & Binding Enforcement
```

当前 25 个模型可见 Typed Tool 已具备 Provider 契约、Capability、Binding、HITL、Ledger、Undo 和 context-scoped Tool Surface，但同一工具的静态事实仍散落在多个模块：

- `tool_specs/catalog.py` 手工维护 `MODEL_TOOL_NAMES`、editable fields，并按工具名补 confirmation/write metadata；
- `tool_runtime/contracts.py` 维护 `TRANSACTIONAL_TYPED_WRITE_NAMES` 和 `REQUIRED_UNDO_TOOL_NAMES`；
- `tool_runtime/catalog.py` 的 `_with_write_contract()` 再次按名称隐式补 `WriteContract`；
- `context_projector/selector.py` 独立维护 domain、dependency、page、attachment 和 lexical 规则；
- `tool_authority` 将 ToolSpec 再投影成 Authority Manifest，并维护独立完整性快照；
- `write_operations.py` 再维护 Typed、Legacy、Compensation 和 required-undo 名称集合；
- `repositories/chat.py` 通过这些集合判断 Pending/Operation 类型。

因此，新增或调整一个工具可能需要同步修改多处集合。某一处漏改时，可能出现：

```text
Provider 看得到工具，但 Selector 永远选不到
Selector 选中了工具，但 dependency closure 不完整
Pipeline 将它视为写工具，Ledger 却不认
UndoPolicy 已声明 REQUIRED，但没有 Compensation
Capability/Binding 已变化，Authority Golden 未变化
Legacy 名称被误放进模型 Surface
```

这些不是新业务能力问题，而是静态契约存在多重真值。本项目负责一次性收敛这些事实。

## 2. 目标、兼容口径与非目标

### 2.1 目标

建立唯一、不可变、可验证的工具元数据束：

```text
ToolMetadataBundleV1
├── TypedToolMetadataCatalog       精确 25 个模型可见 Typed Tool
├── ToolDiscoveryPolicyV1          page / attachment / lexical → domain
├── LegacyDeterministicBoundaryV1  精确 3 个服务端确定性工具
├── CompensationMetadataView       精确 4 个 required-undo binding
└── ToolOperationMetadataView      Typed / Legacy / Compensation / Undo 投影
```

完成后：

- 每个 Typed Tool 只在自己的 `ToolSpec` 中声明一次静态元数据；
- Selector、Authority、Pipeline、Ledger 和 Undo 读取同一个冻结 Bundle 的派生视图；
- Provider payload 继续只来自 `ProviderToolContract`；
- 旧的工具名集合、domain/dependency map、隐式补全和双重 manifest 全部删除；
- 初始化时发现任何静态不一致都 fail-fast；
- 请求运行期间不重新组装、不读取第二份配置，也不做旧路径 fallback。

### 2.2 兼容口径

本期定义为：

> **内部破坏性重构，外部契约严格兼容。**

必须保持：

- 25 个 Provider Tool 的完整 envelope、名称、描述、顺序和参数 Schema；
- 当前 Tool Selector 对 page、attachment、trusted domain 和 lexical signal 的选择结果；
- dependency closure 及 capability 过滤后的闭包校验；
- Capability Profile、capability-first 短路和 Binding 真值表；
- `matched / mismatched / unbound / unavailable` 审计和拒绝语义；
- 现有 Repository/API 的实体归属校验；
- HITL、Pending、approve/modify/reject、claim、CAS 和恢复语义；
- Ledger、Undo、Compensation、terminal replay 和 delivery fencing；
- HTTP、SSE、Provider fallback、Journal、Trace 和业务副作用；
- 25 Typed + 3 Legacy 的精确隔离边界。

允许删除和同步改写所有内部调用方、测试和私有接口。不保留 feature flag、shadow、双轨、隐式 fallback 或长期 façade。

### 2.3 非目标

本期不实现：

- 3 个 Legacy deterministic Tool 的 Typed Pipeline 迁移；
- 新工具、工具重命名或 Provider Schema 变化；
- 新的 domain、lexical term、page kind、attachment kind 或 dependency；
- 更严格或更宽松的 Capability/Binding 规则；
- ID 自动注入、persistent Resume binding 或跨记录限制；
- 新的用户可见错误文本、HTTP/SSE 字段或 Journal Event；
- Schema、数据库 migration、UI 或设置项；
- Compact Confirmation、SSE replay、Runtime Query UI、后台 Queue；
- Summary、Memory、Knowledge retrieval、多 Agent 或插件系统；
- 跨请求、跨进程或跨外部系统 exactly-once。

### 2.4 方案选择

评估过三种方案：

1. `ToolSpec` 显式携带 Metadata，由唯一 Composition Root 编译冻结 Bundle；
2. 另建按工具名索引的 Metadata Registry；
3. 使用 JSON/YAML 或代码生成声明工具元数据。

本期固定采用方案 1。方案 2 仍会留下 ToolSpec/Registry 双重真值；方案 3 会引入配置解析、生成产物和 Python callable 绑定复杂度，对固定 25 个工具不合适。

## 3. 总体架构与唯一真值

### 3.1 构建与消费关系

```text
六个领域 tool_specs
  applications / events / notes / offers / resumes / jd
                    │
                    ▼
          Typed Catalog Composition Root
                    │
                    ▼
          TypedToolMetadataCatalog
                    │
       Legacy Catalog / Compensation Handlers
                    │
                    ▼
      Pilot Runtime Application Composition Root
                    │
                    ▼
          ToolMetadataBundleV1（冻结一次）
      ┌─────────────┼──────────────┬──────────────┐
      ▼             ▼              ▼              ▼
Provider View   Discovery View  Authority View  Operation View
      │             │              │              │
Provider        Projector      Pipeline/HITL   Ledger/Undo
Gateway         Selector       Binding         Repositories
```

所有 View 都是同一个 Bundle 的只读派生对象。消费者不得再维护工具名集合或重新解释工具名。

Bundle factory 还会创建一个不可构造、不可序列化、`repr=False` 的进程内
`BundleInstanceToken`。每个 View 都绑定该 exact token；fingerprint 只证明内容，
token identity 才证明运行期 provenance。Authority、Projector、Pipeline 和 Operation
Port 不接受仅字段相同的调用方伪造 View。

### 3.2 真值层次

唯一真值按职责分为：

```text
ProviderToolContract
  → Provider 可见的完整 payload

ToolSurfaceMetadataV1
  → 工具的静态选择、授权、确认和 Operation 分类

ToolDiscoveryPolicyV1
  → 输入结构信号如何映射为 domain

Runtime handlers
  → decoder / resolver / preflight / executor / renderer / projector
```

`ProviderToolContract` 与 `ToolSurfaceMetadataV1` 互不生成对方：

- Metadata 不生成或替换 Provider Schema；
- Provider Schema 不通过名称或字段反推 capability、binding、write 或 undo；
- 两者在 Catalog 编译时交叉验证。

### 3.3 Typed、Legacy 与 Compensation 类型隔离

必须保持三种不同类型：

```text
TypedToolMetadataCatalog       # 25，允许进入 Provider Surface
LegacyDeterministicBoundary    # 3，只能由可信服务端流程路由
CompensationHandlerRegistry    # 4，只能由 Undo Coordinator 路由
```

禁止建立“28 个工具总 Catalog”后再依赖 `model_visibility=false` 过滤。Legacy 和 Compensation 从类型上就不能进入 Provider、Selector 或模型 Dispatcher。

## 4. 核心契约

### 4.1 `ToolSpec`

内部破坏性切换后的结构固定为：

```text
ToolSpec[Args, Result]
├── contract: ProviderToolContract
├── metadata: ToolSurfaceMetadataV1
├── resolver_bindings: tuple[ResolverImplementationBinding, ...]
├── undo_builder_binding: UndoBuilderBinding | null
├── decoder
├── executor
├── preflight
├── mutable_validator
├── declared_failure_categories
├── exception_map
├── success_renderer
├── result_metadata_projector
├── presentation: ToolPresentationBindingV1
└── schema_failure_renderer
```

旧的顶层 `kind`、`required_capabilities`、`binding_contract`、`confirmation_policy`、`editable_fields` 和 `write_contract` 字段全部迁入 `metadata`。所有生产调用方同步改用新接口，不保留转发属性。

`name` 继续从 `ProviderToolContract.name` 派生，Metadata 不重复保存第二份工具名。

### 4.2 `ToolSurfaceMetadataV1`

```text
ToolSurfaceMetadataV1
├── metadata_version = "tool-surface-metadata-v1"
├── domains: tuple[ToolDomain, ...]
├── dependencies: tuple[ToolName, ...]
├── provider_visibility = model_eligible
├── required_capabilities: tuple[ToolCapability, ...]
├── binding: ToolBindingMetadataV1
├── confirmation_policy
├── editable_fields: tuple[EditableFieldMetadataV1, ...]
└── operation: ReadOperationMetadataV1 | WriteOperationMetadataV1
```

V1 封闭枚举：

```text
ToolDomain:
  applications | events | jd | notes | offers | resumes

ToolCapability:
  applications.read | applications.write
  application_events.read | application_events.write
  notes.read | notes.write
  offers.read | offers.write
  resumes.read | resumes.write
  jd_analyses.read

ProviderVisibility:
  model_eligible

LegacyBoundaryVisibility:
  forbidden

OperationKind:
  read | transactional_write

UndoPayloadKind:
  delete_application
  update_application_status
  delete_application_event
  delete_note

CompensationKind:
  undo:create_application
  undo:update_application_status
  undo:create_application_event
  undo:add_note
```

现有 Authority Manifest 的公开/评审投影仍使用 `kind = "read" | "write"`：

```text
metadata read                → authority read
metadata transactional_write → authority write
```

该投影必须与 `authority_manifest_v1.json` 完全相同，不能把新内部枚举值写进旧 V1
Manifest、Pending、Ledger 或 Journal。

`ProviderVisibility` 只用于 Typed Metadata，因此 V1 唯一合法值是
`model_eligible`。Legacy Boundary 使用独立的单值封闭枚举
`LegacyBoundaryVisibility=forbidden`；两者不得混成一个可以让 Typed Tool 取
`forbidden` 的运行时开关。

`ToolCapability`、`ToolDomain`、`ProviderVisibility`、`LegacyBoundaryVisibility` 和
`OperationKind` 必须定义在
无 Repository、Authority Composition 或 Runtime Context 依赖的 leaf 模块
`ai/tool_runtime/policy_types.py`。当前 `ToolCapability` 从 `context.py` 移到该 leaf，
所有调用方一次性更新，不保留旧模块 re-export。`context.py` 和 Authority 只导入 leaf
类型，避免 `metadata.py → context.py → Repository/Authority` 循环依赖。

生产 V1 要求：

- 每个 model-eligible 工具至少属于一个 domain；
- domain、capability 和 dependency 数组无重复，使用固定 canonical 顺序；
- dependency 精确到具体 Typed 工具名；
- dependency 不得指向自己、Legacy、Compensation 或未知工具；
- dependency graph 必须无环；
- 每个工具继续精确声明一个现有 V1 Capability；
- `read` 必须 `confirmation_policy=none` 且没有 WriteContract；
- `transactional_write` 必须 `confirmation_policy=required` 且显式声明 WriteContract；
- editable field 顺序保持当前确认 UI 顺序；CI/release 另与独立 Golden 比较完整 descriptor；
- editable field 的 `field` 必须存在于 Provider 参数 Schema 的顶层 properties；
- Metadata 不得包含 ORM、Session、Repository、请求参数、实体 ID 或用户内容。

`EditableFieldMetadataV1` 使用唯一规范形状，不保留当前稀疏
`dict` 的多种等价表示：

```text
field: non-empty Provider top-level property name
value_type: string | long_text | enum | datetime | number | boolean
options: tuple[JSON scalar, ...] | null
clearable: bool
clear_value: JSON scalar | null
```

封闭规则为：`enum` 必须有非空、无重复且保持声明顺序的 `options`，
非 `enum` 必须为 `options=null`；`clearable=false` 必须为
`clear_value=null`。`to_compat_descriptor()` 是唯一兼容投影，必须精确还原当前
HTTP/SSE 确认 UI 的稀疏 `field/type/options/clearable/clear_value` JSON，而
Provider Schema 不由这个 descriptor 生成。

### 4.3 Binding metadata 与 resolver

```text
ToolBindingMetadataV1
├── contract: BindingContract
└── resolver_descriptors: tuple[BindingResolverDescriptorV1, ...]
```

现有同时携带 descriptor/callable 的 `BindingResolverSpec` 在本期拆分为：

```text
BindingResolverDescriptorV1（位于 Metadata，纯静态）:
  resolver_id
  entity_kind
  arg_path
  presence
  identity_type

ToolSpec runtime resolver projection（runtime-only）:
  由 resolver_bindings 投影的 tuple[Callable]

ResolverImplementationBinding（构造期显式配对）:
  descriptor（必须是 Metadata tuple 中的 exact object）
  implementation_id（稳定、tool-local reviewed ID）
  resolve callable（named callable，repr=False / compare=False）
```

领域 Spec 通过唯一 `build_tool_spec()` factory 接收 `ResolverImplementationBinding`；
factory 从每个 binding 的 exact descriptor object 构造最终
`metadata.binding.resolver_descriptors`，调用方不能再传另一份 descriptor tuple。
Catalog 封存：

```text
(provider tool name, resolver ordinal, descriptor identity,
 implementation_id, callable identity)
```

禁止建立仅以 `resolver_id` 为 key 的全局 callable registry，因为不同 Tool 可以复用
同一 resolver ID。所有 resolver callable 必须是具名函数，不接受 lambda、partial 或
运行时反射生成函数。CI 中独立的 `resolver_implementation_bindings_0c10e05.json` 固定
`tool name + ordinal + descriptor + implementation_id + qualified function name`，并由现有
Binding 行为矩阵证明 resolver 语义；生产运行时不读取该 fixture。Capability 缺失时仍
在任何 resolver callable 前短路。

初始化继续校验：

- `none/non_application_only` 没有 resolver 和 entity kind；
- bound contract 必须有合法 entity kind；
- resolver entity kind 与 contract 一致；
- `scoped_collection/optional_target` 的 resolver 数量和 optional 规则保持当前 V1；
- resolver ID、arg path、presence 和 identity type 属于封闭 V1 类型；CI/release 另与
  Authority Golden 完全比较。

### 4.4 Operation metadata

```text
ReadOperationMetadataV1
  kind = read

WriteOperationMetadataV1
  kind = transactional_write
  write_contract:
    adapter_kind = typed
    result_contract = typed_json_v1
    result_bytes = 512 KiB
    visible_bytes = 256 KiB
    transport_bytes = 128 KiB
    undo_bytes = 64 KiB
  undo_policy: none | required
  undo_payload_kind: UndoPayloadKind | null
  compensation_kind: CompensationKind | null
  undo_contract_version: str | null
  undo_builder_id: str | null
  undo_seed_phase: none | before_execute | null
```

固定一致性：

```text
undo_policy = none
  → undo_payload_kind 必须为 null
  → compensation_kind 必须为 null
  → undo_contract_version / undo_builder_id / undo_seed_phase 必须为 null

undo_policy = required
  → undo_payload_kind 必须存在
  → compensation_kind 必须存在
  → undo_contract_version / undo_builder_id / undo_seed_phase 必须存在
  → ToolSpec 必须有且只有一个 exact UndoBuilderBinding
  → CompensationHandlerRegistry 必须有且只有一个同名 handler
```

不再通过工具名隐式补 WriteContract。缺失或不一致直接导致静态初始化失败。

### 4.5 Primary Undo builder

Required Undo 的生成也必须从按工具名分支收敛为 exact binding：

```text
UndoBuilderBinding（runtime-only）
├── descriptor（必须与 WriteOperationMetadataV1 的 exact identity 一致）
├── implementation_id
├── capture_seed(session-bound context, prepared typed args) -> FrozenJSON | null
└── build_undo(seed, exact ToolExecutionRecord) -> FrozenJSON
```

四个 builder 分别声明在对应领域 Spec 中：

- `update_application_status` 的 `undo_seed_phase=before_execute`，在同一
  caller-owned Session 和领域写入之前读取 status/closed_reason；
- `create_application`、`create_application_event`、`add_note` 的
  `undo_seed_phase=none`，从 exact typed result 构建已冻结的删除 payload 和
  `expected_after` fingerprint；
- Undo label、payload shape、datetime canonicalization、fingerprint field 顺序、64 KiB
  上限和 projector 失败回滚语义全部保持 Phase 3 baseline；
- `capture_seed`/`build_undo` 只调用一次；未声明 required Undo 的 Tool
  不得拥有 binding。

当前 `api.py::_undo_seed_for_pending()`、`_build_write_undo()`、
`_CREATED_RECORD_FINGERPRINT_FIELDS` 必须删除；实现迁入上述 tool-local
binding，不允许保留按 `pending.tool_name` 的 fallback。Catalog seal 覆盖 descriptor、
implementation ID 与两个 callable identity，Golden 固定四个 builder 的精确输入/输出
shape 与 digest。

### 4.6 Presentation binding

确认文案和 pending/terminal 投影不得留下新的工具名路由表：

```text
ToolPresentationBindingV1（runtime-only）
├── implementation_id
├── confirmation_description(typed args)
├── pending_details_projector(typed args, read-only session-bound context)
└── success_summary_projector(exact typed result)

LegacyPresentationBindingV1（runtime-only）
├── implementation_id
├── confirmation_description(encoded args)
├── pending_details_projector(encoded args, LegacyReadContext)
└── success_summary_projector(encoded result, LegacyReadContext | pure)

LegacyReadContext（transient, repr=False）
├── exact caller-owned Session
└── 由该 Session 绑定的只读 Repository/Service adapter
```

没有专用投影的 Tool 使用在 Spec 构造时显式绑定的具名 `empty`
projector，不在运行时按名称 fallback。Legacy Adapter 对称声明自己的
`LegacyPresentationBindingV1`。Legacy presentation 不得打开隐式 Session 或捕获
Composition 期 Repository；初始确认/执行使用当前事务的
`LegacyReadContext`，若尚未进入写事务，则由 Pilot Runtime 显式拥有一个有界只读
Session 并在投影后关闭。首次执行投影使用 exact
Spec/Adapter Handle；terminal replay 只使用已持久的 visible/transport payload，不重查
Metadata。

当前 `api.py::_pending_action_details()`、`pilot_runtime/service.py` 中的按
tool-name 成功摘要分支，以及 `write_operations.py` 中首次执行阶段的同类
human projection 都迁入 exact binding。只读 terminal replay 兼容 renderer 可以消费
已持久 payload，但不得参与 visibility、authority、Ledger、Undo 或 executor
路由。

### 4.7 Runtime handlers 的边界

以下 callable 不进入 portable Manifest 或 Bundle fingerprint：

```text
decoder / executor / resolver callable
preflight / mutable validator
exception detail mapper
renderer / result projector / presentation / Undo builder
```

但 Catalog 会封存它们的对象 identity 与结构快照；运行期被替换时 fail-closed。其行为继续由现有 Tool outcome、HTTP/SSE 和副作用 Golden 验证。

每个领域 Spec 必须显式绑定自己的 resolver implementation binding、
editable fields、presentation、Undo builder（如适用）和 WriteContract。删除
composition root 中按 `tool_name` 分派的 `_with_runtime_metadata()` 与通用
`_confirmation_description(name, ...)`。

## 5. Discovery Policy 与 Selector

### 5.1 `ToolDiscoveryPolicyV1`

页面、附件和词法规则属于 Catalog 级策略：

```text
ToolDiscoveryPolicyV1
├── selector_version = "tool-surface-selector-v1"
├── discovery_policy_version = "tool-discovery-policy-v1"
├── page_domains
├── attachment_domains
├── lexical_rules
├── no_signal_behavior = full_typed_catalog
└── invalid_input_behavior = fail_closed
```

V1 从 baseline 原样迁移：

```text
page:
  workspace    → []
  applications → [applications]
  application  → [applications, events, notes, offers, resumes, jd]
  calendar     → [events, applications]
  notes        → [notes, applications]
  offers       → [offers, applications]
  resumes      → [resumes, jd]

attachment:
  resume          → [resumes]
  job_description → [jd, applications]
  image            → []
  document         → []
```

Lexical rules 完整保留当前六组词项及顺序，不新增或删除同义词：

```text
applications → 投递 | 申请 | application | company | 岗位 | 职位 | 改成 offer
events       → 日程 | 面试时间 | 笔试 | deadline | event | 提醒
notes        → 复盘 | 笔记 | note | 记录
offers       → offer | 薪资 | 谈薪 | 待遇 | 比较
resumes      → 简历 | resume | 经历 | 求职意向
jd           → jd | 职位描述 | job description | 匹配分析
```

匹配算法仍为：

```text
normalized_request = current_request.casefold()[:16_384]
任一固定字符串作为子串出现 → 命中对应 domain
```

本期不改成 tokenizer、正则、语义匹配或模型分类。

### 5.2 选择顺序

```text
验证 selector/signals version
→ 验证 page/attachment/trusted domain 为封闭枚举
→ 合并 trusted/page/attachment/lexical domains
→ 无任何 domain：完整 25 Typed Tool
→ 有 domain：从每个 Tool 的 metadata.domains 选择
→ 通过 metadata.dependencies 求传递闭包
→ 按 Typed Catalog 原始 ordinal 恢复顺序
→ Authority Surface 交集
→ 再次验证 dependency closure
→ 冻结 Provider envelopes 与 surface fingerprint
```

必须保持：

- `workspace/image/document` 是合法的零领域信号，不是非法输入；
- 零领域信号回退完整 25，不返回空 Surface；
- 未知 page、attachment、domain 或版本 fail-closed，Provider 0；
- Selector/Bundle 异常 fail-closed，不伪装成全量回退；
- Capability 过滤若破坏 dependency closure，整次投影失败，不自动重新加入无权工具；
- 同一 `model_call_id` 的 Provider fallback 复用同一 Surface；
- 未暴露工具仍由 `ModelCallSurfaceBinding` 在 Catalog lookup 前 fail-closed。

### 5.3 唯一 dependency view

删除独立 `_DEPENDENCIES`、`DEPENDENCY_POLICY_V1` 和 `require_dependency_policy_v1()` 单例真值。Bundle 编译时生成唯一的 `DependencyPolicyViewV1`，Selector、Authority Surface 和 Projector 共享其 exact object identity。

该 View 的 canonical projection、version 和 fingerprint 必须与现有 `dependency_policy_v1.json` 完全一致。不能同时保留旧 policy 与新 metadata graph。

## 6. 25 个 Typed Tool 冻结矩阵

以下矩阵是本期迁移真值摘要；完整 resolver、editable fields、Provider contract 和 byte budgets 由独立 Golden 固定。

| # | Tool | Domain | Dependencies | Kind | Capability | Binding | Undo |
|---:|---|---|---|---|---|---|---|
| 1 | `list_applications` | `applications` | — | read | `applications.read` | `scoped_collection(application)` | — |
| 2 | `get_application` | `applications` | `list_applications` | read | `applications.read` | `enforce_if_bound(application)` | — |
| 3 | `create_application` | `applications` | — | write | `applications.write` | `non_application_only` | required |
| 4 | `update_application_status` | `applications` | `get_application` | write | `applications.write` | `enforce_if_bound(application)` | required |
| 5 | `list_application_events` | `events` | — | read | `application_events.read` | `scoped_collection(application)` | — |
| 6 | `get_application_event` | `events` | `list_application_events` | read | `application_events.read` | `enforce_if_bound(application)` | — |
| 7 | `create_application_event` | `events` | — | write | `application_events.write` | `enforce_if_bound(application)` | required |
| 8 | `update_application_event` | `events` | `get_application_event` | write | `application_events.write` | `enforce_if_bound(application)` | none |
| 9 | `delete_application_event` | `events` | `get_application_event` | write | `application_events.write` | `enforce_if_bound(application)` | none |
| 10 | `list_notes` | `notes` | — | read | `notes.read` | `scoped_collection(application)` | — |
| 11 | `add_note` | `notes` | — | write | `notes.write` | `optional_target(application)` | required |
| 12 | `update_note` | `notes` | `list_notes` | write | `notes.write` | `enforce_if_bound(application)` | none |
| 13 | `delete_note` | `notes` | `list_notes` | write | `notes.write` | `enforce_if_bound(application)` | none |
| 14 | `list_offers` | `offers` | — | read | `offers.read` | `scoped_collection(application)` | — |
| 15 | `get_offer` | `offers` | `list_offers` | read | `offers.read` | `enforce_if_bound(application)` | — |
| 16 | `compare_offers` | `offers` | `get_offer`, `list_offers` | read | `offers.read` | `non_application_only` | — |
| 17 | `update_offer` | `offers` | `get_offer` | write | `offers.write` | `enforce_if_bound(application)` | none |
| 18 | `save_offer_assessment` | `offers` | `get_offer` | write | `offers.write` | `enforce_if_bound(application)` | none |
| 19 | `list_resumes` | `resumes` | — | read | `resumes.read` | `none` | — |
| 20 | `get_resume` | `resumes` | `list_resumes` | read | `resumes.read` | `enforce_if_bound(resume)` | — |
| 21 | `resume_update_career_intent` | `resumes` | `get_resume` | write | `resumes.write` | `enforce_if_bound(resume)` | none |
| 22 | `resume_rewrite_highlight` | `resumes` | `get_resume` | write | `resumes.write` | `enforce_if_bound(resume)` | none |
| 23 | `list_resume_matches` | `resumes` | `list_resumes` | read | `resumes.read` | `enforce_if_bound(resume)` | — |
| 24 | `list_jd_analyses` | `jd` | — | read | `jd_analyses.read` | `scoped_collection(application)` | — |
| 25 | `get_jd_analysis` | `jd` | `list_jd_analyses` | read | `jd_analyses.read` | `enforce_if_bound(application)` | — |

四个 required-undo 映射固定为：

```text
primary Tool                undo payload kind             compensation operation kind
create_application          delete_application            undo:create_application
update_application_status   update_application_status     undo:update_application_status
create_application_event    delete_application_event      undo:create_application_event
add_note                    delete_note                   undo:add_note
```

内部 builder descriptor 也同时固定（均使用
`undo_contract_version=write-undo-payload-v1`）：

| Primary Tool | `undo_builder_id` | `undo_seed_phase` |
|---|---|---|
| `create_application` | `create_application_delete_v1` | `none` |
| `update_application_status` | `update_application_status_restore_v1` | `before_execute` |
| `create_application_event` | `create_application_event_delete_v1` | `none` |
| `add_note` | `add_note_delete_v1` | `none` |

矩阵变化必须作为后续独立契约变更，不能在本期实现中“顺手修正”。

## 7. Canonical Manifest、Golden 与 fingerprint

### 7.1 `ToolMetadataManifestV1`

Bundle 生成内部可审计的静态 Manifest（不是 HTTP/Journal 公开契约）：

```text
ToolMetadataManifestV1
├── schema_version = 1
├── catalog_profile = agent_typed_v1
├── typed_tools[25]
│   ├── ordinal
│   ├── provider_name
│   ├── provider_contract_fingerprint
│   ├── domains
│   ├── dependencies
│   ├── provider_visibility
│   ├── required_capabilities
│   ├── binding + resolver descriptors
│   ├── confirmation_policy
│   ├── editable_fields
│   └── operation/write/undo/compensation metadata
├── discovery_policy
├── legacy_boundary（ordered exact 3）
└── compensation_operation_order（ordered exact 4）
```

Provider 完整 envelope 不由 Metadata Manifest 重建。Manifest 只引用其 fingerprint，完整内容继续由 Provider Golden 独立验证。

Manifest Schema 使用 exact-key validator，完整形状固定为：

```text
top-level exact keys:
  schema_version
  metadata_version
  catalog_profile
  typed_tools
  discovery_policy
  legacy_boundary
  compensation_operation_order

typed_tools[i] exact keys:
  ordinal
  provider_name
  provider_contract_fingerprint
  domains
  dependencies
  provider_visibility
  required_capabilities
  binding
  confirmation_policy
  editable_fields
  operation

binding exact keys:
  contract
  resolver_descriptors

binding.contract exact keys:
  kind
  entity_kind

resolver descriptor exact keys:
  resolver_id
  entity_kind
  arg_path
  presence
  identity_type

read operation exact keys:
  kind

write operation exact keys:
  kind
  adapter_kind
  result_contract
  result_bytes
  visible_bytes
  transport_bytes
  undo_bytes
  undo_policy
  undo_payload_kind
  compensation_kind
  undo_contract_version
  undo_builder_id
  undo_seed_phase

discovery_policy exact keys:
  selector_version
  discovery_policy_version
  page_domains
  attachment_domains
  lexical_rules
  no_signal_behavior
  invalid_input_behavior

page_domains item:
  {page_kind, domains}

attachment_domains item:
  {attachment_kind, domains}

lexical_rules item:
  {domain, terms}

legacy_boundary exact keys:
  boundary_version
  provider_visibility
  adapter_kind
  ordered_names
  chained_policies
  initial_route_bindings

initial_route_bindings item:
  {route_source, adapter_ordinal}
```

嵌套 validator 还必须机械封闭以下规则，不得只依赖 Golden 全对象比较：

- `editable_fields[i]` exact keys 为
  `field/value_type/options/clearable/clear_value`，类型、nullable 和 enum 约束按
  §4.2；
- `typed_tools[i].ordinal` 必须为连续 `1..25`，与现有 Authority Manifest
  ordinal 字节等价；Provider name 唯一，contract
  fingerprint 格式为 `sha256:<64 lowercase hex>`，visibility 只能是
  `model_eligible`；
- `domains` 只允许六个 `ToolDomain`，非空、无重复且按 canonical 顺序；
- `dependencies` 只允许生产 25 Typed Provider name，无重复、无自引用、
  按 canonical 顺序且全图无环；
- `required_capabilities` 长度精确为 1，值属于现有 11 个
  `ToolCapability`，顺序使用 Capability Profile ordinal；
- Binding contract/resolver 的 kind、entity kind、presence、identity type、数量与
  nullable 完全按 §4.3，resolver descriptor 顺序不得自动重排；
- `confirmation_policy` 只能是 `none|required`，并与 operation discriminator
  按 §4.2 对应；
- `page_domains` 必须精确包含且只包含 §5.1 的 7 个 page kind，
  `attachment_domains` 必须精确包含且只包含 4 个 attachment kind，均按
  baseline 声明顺序；每项 `domains` 长度为 `0..6`、无重复、值只能是
  `ToolDomain` 且按 canonical domain 顺序；
- `lexical_rules` 必须精确六项且按 §5.1 domain 声明顺序；每项
  `terms` 为 1..16 个无重复非空字符串，每个按 UTF-8 计最多 64
  bytes，禁止 surrogate、NUL 和 C0/C1 control，保持 baseline 原始顺序且不做
  Unicode normalization；
- `no_signal_behavior` 只能是 `full_typed_catalog`，
  `invalid_input_behavior` 只能是 `fail_closed`；
- `operation.kind` 是 tagged-union discriminator；`read` 只允许 `{kind}`，出现任一
  write-only key 必须拒绝；`transactional_write` 必须包含上述全部 exact
  keys，Undo `none/required` 的 nullability 按 §4.4；
- `legacy_boundary.ordered_names` 在生产 Manifest 中长度精确为 3、无重复且
  必须等于 §7.2 的固定顺序；`provider_visibility` 只能是
  `forbidden`，`adapter_kind` 只能是 `legacy_deterministic`；
  `chained_policies` 按同一 ordinal 精确为
  `[same_adapter_only, forbidden, forbidden]`；`initial_route_bindings` 按
  `LegacyRouteSourceV1` direct-source ordinal 精确为
  `[{jd_clarification,1}, {jd_deterministic_action,1},
  {submission_snapshot_action,2}, {outcome_recording_action,3}]`，ordinal 只能指向上述
  三个 Adapter，不能包含 `confirmation_resume`。
- `compensation_operation_order` 长度精确为 4、无重复且必须等于
  §7.2 的固定 Compensation 顺序；每项必须是封闭
  `CompensationKind` 字符串。

所有 enum 在 JSON 中为字符串，ordinal/byte budgets/schema version 为 JSON integer，
数组为空时写 `[]`。Write operation 即使无需 Undo，也必须显式写：

```text
undo_policy = "none"
undo_payload_kind = null
compensation_kind = null
undo_contract_version = null
undo_builder_id = null
undo_seed_phase = null
```

禁止省略 nullable 字段或接受额外字段。`provider_contract_fingerprint` 固定为：

```text
sha256(canonical_json(完整 ProviderToolContract.payload))
→ sha256:<64 lowercase hex>
```

它覆盖 `type/function/strict/name/description/parameters` 等完整 envelope 字段，不包含
endpoint、credential、Provider candidate、runtime Surface 或适配器配置。现有 Provider
Golden 继续单独校验完整 envelope fingerprint 与 parameters/schema fingerprint。

`ToolSurfaceMetadataV1.metadata_version` 在 Manifest 顶层只写一次；每个
`typed_tools[i]` 必须来自同一 V1 compiler，不重复一个可以彼此冲突的
per-tool version 字段。

### 7.2 Canonical 算法

```text
UTF-8
JSON object keys sorted
separators = (",", ":")
ensure_ascii = false
allow_nan = false
不做 Unicode normalization
Enum 使用 .value
fingerprint = sha256:<64 lowercase hex>
```

数组顺序固定：

- tools：Provider Catalog ordinal；
- domains：按现有 `sorted(str)` 的 Unicode code point 顺序，即 `applications, events, jd, notes, offers, resumes`；
- dependencies：按现有 `DependencyPolicyV1` 的 Unicode code point 字符串顺序；最终 Provider Surface 仍按 Catalog ordinal；
- capabilities：Capability Profile ordinal；
- resolvers：ToolSpec 声明顺序；
- editable fields：确认 UI 顺序；
- page/attachment/lexical rules：当前 baseline 声明顺序；每条映射中的 domain 按上述 domain 顺序；
- Legacy：`save_application_jd_version`、`create_application_submission_snapshot`、`record_application_outcome`；
- Compensation：保持 Phase 3 既有顺序：`undo:update_application_status`、
  `undo:create_application`、`undo:create_application_event`、`undo:add_note`。

非 canonical 顺序不自动排序掩盖。通用测试 Catalog 也以自己显式传入的 `ordered_specs` 作为 ordinal 真值，并按同一规则验证；它与生产模式的差异只有固定数量和独立 Golden 校验，不能使用更宽松的 canonical 算法。

### 7.3 五类独立、仅供 CI/复审的 Golden

禁止新 Bundle 自证正确。必须同时保留并校验：

1. `provider_manifest_30c944f.json`：完整 Provider envelope、顺序、Schema；
2. `authority_manifest_v1.json`：当前 Capability/Binding/Resolver 投影；
3. `dependency_policy_v1.json` 及既有 baseline：当前闭包图和 fingerprint；
4. 新增从 `0c10e05` 独立捕获的 Metadata 资产族：
   - `tool_metadata_manifest_v1.json`：domain、discovery、operation 和 Legacy 边界；
   - `tool_selection_matrix_0c10e05.json`：合成 signal 的 ordered Surface/closure；
   - `tool_operation_matrix_0c10e05.json`：Typed/Legacy/Undo/Compensation 分类；
5. `resolver_implementation_bindings_0c10e05.json`：tool-local descriptor/callable 配对身份。

测试资产：

- 只保存纯合成 canonical JSON；
- 记录固定 baseline commit、raw SHA-256 和 canonical SHA-256；
- 不保存用户内容、实体 ID、Prompt、异常、密钥、SQLite 或时间；
- 测试只读，不存在 writer、`--update`、accept-new 或自动覆盖路径；
- Authority Manifest 保持现有 `schema_version=1` 和字段集合，不塞入新字段。

Resolver binding 资产与其他四类一样固定 baseline commit、raw/canonical
digest 和 exact-key Schema；每项精确包含
`tool/ordinal/descriptor/implementation_id/qualified_callable`。它不存 callable
repr、对象地址或用户内容，不能自动生成/接受新值，生产运行时也不读取。

五类 Golden 是 CI、CR 和 release gate 的 review-only 资产：

- 生产代码不得导入 `tests`、读取 `tests/fixtures` 或把 Golden 当运行时配置；
- Golden 不反向构造 ToolSpec、Bundle、Authority View 或 Dependency View；
- 生产启动只校验 source code 声明的内部结构、闭包、seal 和既有安全 policy constants；
- 当前 Capability/Binding 已冻结的 reviewed fingerprint constants 继续按 Phase 6
  语义使用；本期只新增 §7.5 两个从固定 baseline 独立捕获的协议边界 seal，不增加
  一份由当前 Bundle 自算再自验的完整 Metadata expected digest；
- Golden 缺失只会令测试/发布门禁失败，不会令打包后的应用因为测试文件不存在而失败。

### 7.4 fingerprint 使用边界

`bundle_fingerprint` 只用于：

- 静态启动完整性；
- 同一个 Application Composition 内证明消费者来自同一个 Bundle；
- 测试、CR 和发布报告；
- 可选的静态安全日志。

它不进入 ChatMessage、Pending、Ledger、Journal Event/Snapshot、Prompt、checkpoint、HTTP 或 SSE。本期不修改持久 Schema。

Callable 不进入 portable fingerprint；其进程内 identity 由 Catalog seal 保护，行为由独立 Golden 验证。

Portable fingerprint 的唯一定义为：

```text
bundle_fingerprint
  = sha256(canonical_json(ToolMetadataManifestV1 exact object))
```

输入包含顶层 metadata/schema version、Discovery Policy、25 个完整 Typed
metadata、每个 Provider contract fingerprint、Legacy Boundary 和 Compensation
order。不包含 Bundle/Segment/Authority token、callable/implementation object identity、
凭据、动态 Provider 配置、时间或用户内容。`bundle_fingerprint` 不写回
Manifest 本身，避免循环；它与 Provider/Legacy protocol seal 分别承担
“完整内部静态投影身份”和“已批准外部边界”两种职责。

### 7.5 生产协议边界 seal

为保证即使绕过 CI，生产 Composition 也不能把“数量正确但名称已替换”的 Catalog
当成批准边界，本期增加两个不含可查询名称集合的固定 protocol seal：

```text
APPROVED_PROVIDER_TOOL_BOUNDARY_V1
  = sha256:db60a499a2c76fa46e769214cbdce1488bb86b2f67941d6ae961f25ed997e98b

输入：canonical({
  "schema": "provider-tool-boundary-v1",
  "ordered_tools": [25 个完整 ProviderToolContract.payload]
})

APPROVED_LEGACY_DETERMINISTIC_BOUNDARY_V1
  = sha256:7d1d6b7e6cf3953b1a17e7a81c9d5655b5366de25b263ea6bd89a646c2f2a580

输入：canonical({
  "schema": "legacy-deterministic-boundary-v1",
  "ordered_names": [
    "save_application_jd_version",
    "create_application_submission_snapshot",
    "record_application_outcome"
  ],
  "provider_visibility": "forbidden",
  "adapter_kind": "legacy_deterministic"
})
```

两个常量放在无 Repository 依赖的 `tool_runtime/protocol_seals.py`。它们是已批准外部
Provider/Legacy 协议边界的固定 review seal，只能比较整体 canonical input，不能按名称
查询、选择或路由，因此不是第二份运行时分类 Registry。它们不从当前 Bundle 自动生成
期望值；变更必须提升 seal version、更新独立 Golden 并走单独契约复审。
内部 Legacy `chained_policies` 与 `initial_route_bindings` 由 Metadata Manifest/bundle
fingerprint 保护，不追加到这个已固定的 Legacy 外部名称边界 seal 中。

`protocol_seals.py` 是生产代码中唯一允许声明 boundary seal 和其固定协议
input 的模块。AST gate 精确 allowlist 该模块的两个 digest/整体投影，同时禁止：

- 任何模块从 seal input 导出单工具查询、visibility、routing 或 classification；
- 生产代码从 `tests/fixtures` 读取或重算 expected seal；
- 在其他模块声明第三个 Provider/Legacy boundary digest。

独立测试从 baseline Golden 重算两个实际 seal，并证明修改名称、顺序、
Provider payload、Legacy adapter kind/visibility 或 expected digest 任一项都使
Application Composition fail-closed。

## 8. 编译、不可变性与启动校验

### 8.1 两级构造

```text
Generic ToolCatalog compiler
  → 支持单元测试构造 1..N 个闭合小 Catalog
  → 每个 ToolSpec 仍必须显式提供完整 Metadata
  → 不根据生产工具名自动补全

Production build_typed_tool_metadata_catalog()
  → 在 tool_specs/catalog.py 组装精确 25 Typed
  → 只做 Typed 结构、闭包、seal 和既有安全 policy constant 校验
  → 完整 Provider envelope 必须匹配 APPROVED_PROVIDER_TOOL_BOUNDARY_V1

Application compose_tool_metadata_bundle(...)
  → 在 pilot_runtime/composition.py 接收上述 Typed Catalog
  → 接收实际 LegacyDeterministicCatalog（精确 3 Adapter）
  → Legacy projection 必须匹配 APPROVED_LEGACY_DETERMINISTIC_BOUNDARY_V1
  → 接收 Typed Metadata 声明的 4 Compensation metadata
  → 与实际 CompensationHandlerRegistry 做 exact binding
  → 创建唯一 BundleInstanceToken
  → 返回该 Application Composition 唯一的完整 Bundle
```

生产代码只有 Composition Root 可以调用生产 factory；AST gate 禁止其他模块新建生产 Bundle。测试小 Catalog 有自己的 token，不能混入生产 Runtime/Authority；它不要求 Provider 配置、API key 或网络。

### 8.2 递归冻结

`frozen=True` 不足以保护嵌套 `dict/list`。构造时必须：

- 递归复制并冻结 Provider payload、parameters 和 editable field JSON；
- list 转为 immutable sequence，mapping 转为只读结构；
- 查询接口只返回不可变视图；
- Provider Adapter 通过唯一 `materialize_provider_payloads()` 获得新复制的普通 JSON；
- JSON Schema validator 从受控复制品预编译；
- 不允许 `dict(contract.payload)` 这种浅复制成为网络真值。

Catalog 继续保留完整 integrity seal，覆盖：

- Provider envelope；
- Metadata primitive/tuple/frozen JSON；
- Binding resolver descriptor 与 callable identity；
- decoder/executor/renderer/projector identity；
- presentation/Undo builder descriptor 与 callable identity；
- WriteContract 和 Compensation handler identity。

任何 mutation probe 或对象替换都必须在 Provider、Repository、executor 前 fail-closed。

Legacy 边界使用对称的最小冻结契约：

```text
LegacyDeterministicCatalog
  → ordered adapters immutable
  → editable field metadata recursively frozen
  → describe/validate/presentation/execute callable identity sealed
  → APPROVED_LEGACY_DETERMINISTIC_BOUNDARY_V1 checked
```

Adapter/Catalog 复制、pickle、嵌套 editable mapping mutation、callable 或顺序替换都必须
在 Adapter、Repository 和 executor 前 fail-closed。Legacy 的 runtime seal 只保护
exact Adapter identity，不会使它成为 Typed Catalog 或 Provider 可见对象。

### 8.3 静态初始化失败

以下任一运行时结构问题阻止应用 Composition 完成：

- Typed 不精确为 25、Legacy 不精确为 3；
- 名称重复、集合重叠、ordinal 不连续；
- unknown domain/capability/binding/resolver/policy version；
- dependency 缺失、自引用、指向 Legacy/Compensation 或形成环；
- read/write/confirmation/WriteContract 组合非法；
- required undo 缺少或重复 Compensation metadata；
- required undo 的 descriptor/seed/build binding 缺少、多余或错绑；
- editable field 与 Provider Schema 或同一 ToolSpec 内部声明不符；
- presentation binding 缺少或 callable identity 与 Spec seal 不符；
- Frozen JSON 含非有限数、非法 key 或不可序列化值；
- `ResolverImplementationBinding` 与 Metadata descriptor/implementation identity 不闭合。

Provider/Authority/Dependency/Metadata Golden 漂移由 CI/release gate 阻止，不由生产
进程读取 fixture 检测。Application Composition 还会将 Bundle 声明的四个
`compensation_kind` 与实际 `CompensationHandlerRegistry` 做 exact binding；缺少、
多余或重复 handler 时 Composition 失败，但 `tool_specs/catalog.py` 不反向导入
`write_operations.py`。

这里没有全局 resolver callable registry。Legacy Adapter Catalog 与 Compensation
Handler Registry 是两个独立的实现边界，也不是 resolver registry。

这些是部署缺陷，不能在请求时伪装成用户 `validation_error` 或回退旧 Catalog。

“没有配置 AI Provider/API key”仍是合法应用状态。静态 Metadata 校验不得读取凭据、动态 Provider 配置、数据库或网络；只有发起 AI Segment 时按现有语义返回 AI 未配置。

### 8.4 Segment identity 与共享 Bundle

当前 `_PolicyCatalogResolver._fresh_segment_catalog()` 会 deepcopy 25 个 Spec 并为每个
Segment 重建 Catalog/Authority Manifest。本期必须删除这种 Metadata 重编译，但保留
Segment 级 Authority identity 隔离：

```text
Application Composition 级 ToolMetadataBundleV1
  → 每个 Application/PilotRuntime container 唯一、递归冻结
  → 共享 Spec/Metadata/Manifest

SegmentToolCatalogLease
  → 每个 Segment 新建
  → 持有 exact BundleInstanceToken
  → 持有独立 SegmentCatalogToken
  → resolve(name) 返回 SegmentToolSpecHandle
  → 不 deepcopy Spec、不重算 Metadata、不重建 Manifest

SegmentToolSpecHandle
  → 不可序列化、repr=False
  → 绑定 shared ToolSpec identity + Bundle token + SegmentCatalogToken
  → 供该 Segment 的 Authority/Pipeline/PreparedCall 使用
```

同一个 Application Composition 内，Approval invocation、continuation Segment 和下一 turn 仍获得不同的
`SegmentCatalogToken`，所以跨 Segment 复用 Prepared/Authority/Surface 继续失败；它们
同时引用同一个 Bundle token，证明静态事实没有漂移。Provider payload 每次通过受控
materialization 得到新普通 JSON，不需要复制整个 Catalog。

Authority Registry 以 `(SegmentCatalogToken, shared ToolSpec identity, handle identity)`
注册，而不是只以共享 `id(spec)` 注册。Pipeline 只有在验证 Handle provenance 后才能
访问 underlying Spec；PreparedCall 保存 Handle/opaque token，不把 Handle 写入 Pending、
Ledger、Journal 或 checkpoint。

测试必须同时证明：

- 两个 Segment lease identity 不同；
- underlying Bundle、Spec、Metadata 和 Dependency View identity 相同；
- 两个 Segment resolve 同名 Tool 得到不同 Handle；
- 一个 Segment 的 Authority/Prepared object 不能在另一个 lease 使用；
- request 期间没有 Catalog compiler、deepcopy Spec 或 Manifest rebuild。

测试或同一进程内创建的另一个独立 Application container 可以拥有不同 Bundle token；
不同 container 的 View、Lease、Handle 和 Authority 不能互换。

## 9. Authority、Pipeline 与 HITL

### 9.1 Authority Surface

固定顺序不变：

```text
Discovery 选择候选工具
→ 从同一 Bundle 读取 required_capabilities / binding kind
→ 与 SegmentExecutionAuthority 求交集
→ 验证 dependency closure
→ 构造 Frozen Model Surface
```

`provider_visibility=model_eligible` 只代表工具有资格进入完整 Typed Catalog，不代表当前请求已获授权。

### 9.2 Pipeline

执行顺序继续为：

```text
Surface Binding 验证
→ Typed Catalog lookup
→ Capability 短路
→ Schema / decode
→ Binding resolver / audit
→ read-only preflight
→ confirmation 或 PreparedToolCall
→ 锁内 mutable recheck / claim / executor
```

Metadata 收敛不得改变：

- capability failure 时 Binding、Repository、preflight、executor 均为 0；
- Binding resolver 不通过工具名推断；
- Repository/API 的最终归属校验继续执行；
- 未暴露工具在 Dispatcher 进入 Catalog 前 fail-closed；
- executor 前失败为 0 次，executor Exception 为恰好 1 次；
- renderer、Journal 或 transport 失败不得重跑 executor；
- `BaseException` 继续原样传播。

### 9.3 确认恢复

Approve/modify 仍从可信 Pending/Ledger 重建 Prepared call；reject 不重新 prepare 或查询实体。Metadata Bundle 是静态 Application Composition 依赖，不进入 Pending，也不改变 authorization scope fingerprint。

保持：

- approve/modify 的 token、revision、effective args digest、claim/CAS；
- reject 的 identity/rejection CAS、executor 0；
- terminal replay/delivery recovery Provider 0；
- chained Pending、delivery lease 和 late-result fencing；
- 当前 `authority_manifest_v1` 与 Binding policy fingerprint。

本期不新增 migration，也不因 Bundle fingerprint 变化使旧 Pending stale。

## 10. Ledger、Undo 与 Repository 边界

### 10.1 `ToolOperationMetadataPort`

Write Coordinator 只接收从 Bundle 派生的窄 Port：

```text
ToolOperationMetadataPort
├── bind_typed_write(
│       prepared_spec_handle,
│       pending_or_operation_identity,
│       live_claim_token,
│   )
│     → TypedWriteHandle
├── bind_legacy(legacy_adapter_route_handle, locked_pending_identity)
│     → LegacyWriteHandle
└── bind_compensation(committed_parent_identity, compensation_handler_handle)
      → CompensationHandle
```

Port 不接受客户端或 Provider 提供的裸工具名作为分类依据。Handle 均为
`repr=False`、不可序列化、绑定 exact Bundle/Spec/Adapter/parent identity 的瞬态对象：

- `TypedWriteHandle` 只能在 Typed Catalog resolve、Surface/Authority/Prepared identity
  验证后签发，并绑定 exact Bundle token、SegmentCatalogToken、Authority token、
  Prepared token、ToolSpec identity 和 Pending/Operation identity；
- `LegacyWriteHandle` 只能从服务端初始 exact route handle，或锁内 issuer 签发且被 exact
  Legacy Catalog 一次性消费的 proof-derived route handle 继续签发；
- `CompensationHandle` 只能从 committed primary parent 和 exact handler 签发；
- read Tool 不能进入 proposal；unknown、Legacy-as-Typed、Compensation-as-primary 或
  identity 不匹配均在 Repository/executor 前 fail-closed。

Handle 的生命周期不扩展现有 Claim：Typed Handle 只在对应 Pending/Execution Claim
`in_flight` 期间有效，claim exit/revoke 后立即失效。确认恢复必须重新 prepare 并为新
Segment/Authority 签发新 Handle。不同 Segment、Authority、Runner、字段相同的复制品或
已撤销 Handle 都不能进入事务。

Handle 本身只能保存：

```text
opaque registry tokens / exact instance tokens
tool_call_id / operation_id 等已有有界 primitive identity
revision / adapter_kind / operation_role 等封闭 primitive
canonical digest / relation fingerprint
```

它不得保存 ORM、Session、Repository、Pending model、WriteOperation model、原始 args、
Undo payload 或用户内容。Port 的入参也必须是已由当前事务读取并冻结的
primitive identity 或 opaque route/handler handle，不接收 ORM model。
Repository 在使用 Handle 时重新比对当前锁内 row/claim 与这些 identity。

首次 proposal/claim 路径把 Handle 中已经验证的 `tool_name/adapter_kind/operation role`
写入既有列，但不持久化 Handle 或 Bundle fingerprint。

`write_operations.py`、`repositories/chat.py` 和 Pilot Runtime 不再导入或定义：

```text
TRANSACTIONAL_TYPED_WRITE_NAMES
REQUIRED_UNDO_TOOL_NAMES
TYPED_WRITE_OPERATION_NAMES
LEGACY_WRITE_OPERATION_NAMES
COMPENSATION_OPERATION_NAMES
REQUIRED_UNDO_OPERATION_NAMES
WRITE_OPERATION_NAMES
```

Repository 不自行解释工具名。Coordinator 在调用 Session-bound Repository 前提供
已验证 Handle；Repository 在事务内验证 Handle provenance 和其中的封闭 classification，
不得回到名称集合。旧 Terminal Replay 不需要也不得重新签发 Handle，见 §10.6。

### 10.2 Pending persistence route

`ChatRepository` 不再用 `_pending_adapter_kind()` 或 Typed/Legacy 名称集合路由。
所有可能持久 Pending 的调用点显式传入下列 sealed union：

```text
PendingPersistenceRouteHandle
  = TypedPendingRouteHandle
  | LegacyPendingRouteHandle
  | ClarificationPendingRouteHandle

TypedPendingRouteHandle
  ← SegmentToolSpecHandle + exact PendingAuthorityClaim + Pending primitive digest

LegacyPendingRouteHandle
  ← server-selected LegacyAdapterRouteHandle（初始动作）
    | proof-resolved LegacyAdapterRouteHandle（确认恢复）
    + Pending primitive digest

ClarificationPendingRouteHandle
  ← 既有 clarification flow，必须 operation_id=""
```

Typed Pipeline 签发 exact Typed Handle；服务端确定性初始流程直接签发 exact Legacy
Handle，确认恢复则必须先经过 §11.2 的锁内 proof issuer，再由 Legacy Catalog 消费
proof 签发 exact Legacy Handle。
`set/persist/replace/persist_confirmation_continuation` 等所有初始、替换和 chained
Pending 路径都消费该 union，Repository 只验证：

- Handle 由注入的 Bundle/Adapter registry 签发且尚未撤销；
- tool call/operation/conversation generation/claim/args digest 与锁内 Pending 完全一致；
- Typed Handle 携带 exact authority claim，Legacy Handle 不能携带 Typed claim；
- operation-bearing Pending 不得使用 clarification handle。

缺少 Handle、字段相同的伪造/复制 Handle、客户端 tool name 或 server-loaded
provenance 不成立时，Pending/Operation/Repository 写入和 executor 均为 0。
`persist_typed_pending()`/`persist_legacy_pending()` 可作为 Repository 内部的两个
session-bound 实现，但不得对外开放绕过 Handle 的入口，也不得再次用
`pending.tool_name` 分类。

### 10.3 原子性与状态机

保持 Phase 3：

- proposal、claim、domain write、terminal payload 与 required undo 的事务边界；
- 同一个 Operation 的 terminal commit at-most-once；
- response-loss replay 不重跑 executor；
- delivery generation/owner fencing；
- commit-unknown fresh-session reconciliation；
- Compensation 作为独立 Operation；
- caller-owned Session 中 `tool.started` 的窄原子例外。

Metadata 查询是纯内存、无 I/O、无锁等待，不改变业务事务 deadline。它不能成为 Journal 或领域写入失败的新来源。

### 10.4 非终态恢复与 chained Pending

终态不重查 Metadata，但 `proposed`/正在 claim/commit-unknown 必须有明确的
rehydration 边界。请求携带安全控制字段 `operation_id` 时先按它读 Ledger；字段缺失时，
只能从当前 Conversation 的 live `pending_operation_id` 指针锁内引导，再回到同一
Ledger-first 分支。没有 live Pending 时不得从 tool name/历史消息猜测，也不能恢复已清理
Pending 的 terminal replay：

```text
terminal
  → 按 §10.6 直接 replay，Metadata/Provider/executor = 0

non-terminal primary
  → BEGIN IMMEDIATE
  → 重读 owning Conversation + Pending + Operation
  → 校验持久 adapter_kind/operation_role/identity/fingerprint
  → typed: 用当前 Segment Catalog resolve persisted tool_name，重跑 prepare/Authority/
           mutable recheck，再签发新 Typed Handle
  → legacy: 仅当锁内 trusted Pending/Operation 完整验证后，
            签发一次性 LegacyRouteProof；Catalog 消费 proof 后 resolve exact Adapter
            并签发新 Legacy Handle
  → 按现有 claim/CAS 继续
```

持久的 `adapter_kind` 只是已发布 Ledger 身份字段，用于选择上述封闭
rehydration branch；分支内仍必须通过 exact Handle，不能仅凭 tool name 执行。
approve/modify 会重建 Prepared call，reject 仍只做 token/Pending/Ledger identity 与
rejection CAS，不运行 Metadata prepare/preflight。

commit-unknown 的 fresh-session reconciliation 保持 Phase 3：只有 fresh read 看到 terminal
才 replay；absent/unreadable/non-terminal 按现有 `operation_busy`/
`operation_not_committed`/`operation_result_unknown` 语义返回，当次 reconciliation 不签发
Handle、不重跑 prepare 且 executor=0。只有之后由 owning request 进入上述
normal non-terminal 路径才可继续。

Chained topology 使用封闭 `ChainedPendingTopologyPolicyV1`，不保留
`_chained_adapter_kind()` 工具名 switch：

| trusted parent | exact child Handle | 结果 |
|---|---|---|
| Typed primary | Typed | 允许 |
| Typed primary | Legacy/Compensation | 拒绝 |
| Legacy `save_application_jd_version` | 同 exact Adapter | 允许 |
| 其他 Legacy 或不同 Legacy Adapter | 任何 child | 拒绝 |
| Compensation | 任何 child | 拒绝 |

Legacy 的例外由该 Adapter 自身的
`chained_policy=same_adapter_only` 声明，其余 Adapter 显式为 `forbidden`；不从
名称前缀或集合推断。Parent 必须是锁内读取的 persisted identity，child 必须是
当前 Segment 的 exact Pending Route Handle。替换 Pending、child proposal、parent delivery
outcome 仍在同一事务；response-loss replay 只验证已持久 parent/child relation，
不重建 child 也不调用 Provider/executor。

### 10.5 Compensation handler binding

新增 `pilot_runtime/compensation.py`，把当前 `api.py::_execute_chat_undo()` 的四个
session-bound 分支迁移为：

```text
CompensationHandlerSpec
  undo_payload_kind
  compensation_operation_kind
  adapter_kind = compensation
  result_contract = compensation_json_v1
  undo_contract_version（沿用 Phase 3 封闭 payload shape）
  validate_undo_payload
  execute(session, immutable_undo) -> visible result

CompensationHandlerRegistry
  ordered exact 4 handler specs
  resolve(handle) -> handler
```

`pilot_runtime/composition.py` 将 Registry 与 Bundle 的四个 required-undo metadata 做
exact binding，再注入 Write Coordinator。当前
`compensation_kind_for_undo()` 的名称 switch 删除，由 Metadata 中的
`undo_payload_kind ↔ compensation_kind` 映射取代。Registry key 是实现绑定，不是第二份
分类策略：

- 缺少、多余或重复 handler 在启动时失败；
- handler 的 undo kind、compensation kind、payload validator、adapter kind 和 result
  contract 必须逐项匹配 §6 真值表；数量正确但错绑也必须失败；
- handler 不进入 Typed/Legacy Catalog；
- 客户端或 Provider 不能提交 compensation 名称；
- Compensation Coordinator 只能通过可信 parent Operation 路由。

以下 Phase 3 身份保持字节/语义不变：

- `COMPENSATION_OPERATION_NAMESPACE = 4079900d-84a6-5cff-aa63-65089c4ccccd`；
- `uuid5(namespace, parent_operation_id + ":" + compensation_kind)`；
- 四个 compensation kind 字符串；
- parent committed-primary 校验、claim/CAS、commit-unknown、terminal digest 和 replay；
- immutable Undo payload 的 shape 与 byte budgets。

`/api/chat/undo-last-write` 继续只表达“撤销可信的 last committed write”。Transport 不
接受或转发裸 `compensation_kind` 到 executor。服务端顺序固定为：

```text
读取并验证可信 parent Operation + immutable Undo payload
→ 从 parent Tool metadata 得到 expected undo/compensation kind
→ Registry 验证 payload kind
→ 签发 CompensationHandle
→ Coordinator claim/CAS/execute
```

AST gate 禁止 API、客户端字段或普通字符串直接调用 Compensation executor。

### 10.6 旧 Operation 与 terminal replay

已持久化 terminal Operation 的权威回放输入仍是：

```text
operation_role
adapter_kind
tool_name
request / terminal fingerprints
terminal payload digest
parent compensation relation（适用时）
```

Terminal replay、delivery recovery 和 fallback：

- 不重新 resolve ToolSpec、Provider Surface、Binding resolver 或完整 Metadata Bundle；
- primary/Legacy replay 按既有 DB CHECK、identity、fingerprint 和 terminal digest 校验；
- Compensation replay 继续验证持久 parent/child relation；
- Provider 和 executor 均为 0；
- 即使升级后当前 Bundle 无法提供一致性诊断，也不得重跑 executor、改写 terminal
  Outcome 或阻止既有 delivery fencing 收敛；静态部署不一致由启动/发布门禁处理。

`conversation_id IS NULL` 的 Compensation/兼容行继续按 Phase 3 既有规则处理。本期不
回填 Metadata version/fingerprint，也不增加 Operation 列。

### 10.7 已发布数据库 CHECK 的历史例外

`models.py` 中以下约束继续原样保留：

```text
ck_write_operations_manifest
ck_write_operations_undo_policy
```

它们硬编码的 12 Typed、3 Legacy、4 Compensation 和 4 required-undo 名称是 Phase 3
已经发布的数据库 Schema 兼容资产，不参与 Python 运行时分类，也不由 Bundle 动态生成。
本期不修改、删除或新增 migration。

CI 必须分别证明：

- source gate 固定两个 constraint name 和源字符串结构；
- 在临时 SQLite 分别执行 baseline/current 允许与拒绝矩阵，验证 semantic 等价；
- Bundle Operation projection 与 CHECK 允许集合完全一致；
- Python Coordinator/Repository 未读取、解析或复制 CHECK SQL 来做运行时路由。

允许/拒绝矩阵覆盖 12 Typed、3 Legacy、4 Compensation、4 required-undo 及非法
role/adapter/tool/undo 交叉组合。测试可以解析/构造 fixture，但生产代码不得引入 SQL
parser。由于空白或 SQLAlchemy 方言渲染可能变化，不把整个生成 DDL 的 byte equality
当作唯一语义证明。

因此“唯一真值”精确定义为：**唯一 Python 运行时 Operation 分类真值**。已发布的持久
Schema CHECK 与只读 review Golden 是独立约束，不是运行时 Registry。

## 11. Legacy deterministic 边界

### 11.1 静态 Adapter 与 Session-bound execution

固定三个名称和顺序：

```text
save_application_jd_version
create_application_submission_snapshot
record_application_outcome
```

`LegacyDeterministicBoundaryV1` 从唯一 `LegacyDeterministicCatalog.ordered_adapters`
编译，不再另写一份生产名称集合。它的 canonical projection 只包含：

```text
boundary_version
ordered exact names
provider_visibility = forbidden
operation adapter kind = legacy_deterministic
chained policies = same_adapter_only / forbidden / forbidden
initial route bindings = 四个 direct source → exact Adapter ordinal
```

Legacy Adapter 自身继续拥有唯一的 name、editable fields、describe、validate、
presentation 和 execute callable；Boundary 的名称和顺序均从 Adapter Catalog 派生
并由独立 Golden 验证。本期不把它们伪装成 Typed `ToolSpec`。

但 Adapter 必须从“闭包捕获 Repository/Service”改为静态 Spec：

```text
LegacyDeterministicAdapterSpec
├── name / editable fields / chained policy / initial_route_sources
├── describe / validate / presentation
└── execute(encoded_args, LegacyExecutionContext) -> str

LegacyExecutionContext（transient, repr=False）
├── exact caller-owned Session
└── 由该 Session 绑定的现有 Repository/Service adapter
```

`LegacyReadContext` 与 `LegacyExecutionContext` 共享同一 Session ownership 规则；
前者只暴露展示所需的只读 Port，后者只能在 Coordinator 已取得业务事务后
构造。两者都不能在 Adapter 中缓存或跨 Session 复用。

`build_legacy_deterministic_catalog()` 不再接收或捕获
`ApplicationJDService`/`ApplicationOutcomesRepository`，Application Composition 只构造一次
静态 Catalog。Write Coordinator 已获得 `BEGIN IMMEDIATE` 的业务 Session 后，
才构造 `LegacyExecutionContext` 并恰好调用一次 Adapter executor。Adapter 不得
打开隐式 Session、重建 Catalog 或使用 Composition 期间捕获的 Repository。

因此既保留“Ledger/Journal `tool.started`/Legacy 领域写入同 Session、同 commit、
同 rollback”，又不需每次执行重编译 Metadata。测试必须覆盖同一事务成功/
rollback、两个 Session 隔离、executor Exception 后零部分写入，以及 Adapter
企图打开自有 Session 时 fail-closed；Legacy presentation 还要比较跨 Session、
rollback 与 baseline 展示结果等价。

### 11.2 可信路由

本期不新增持久 `route_kind` 字段。可信入口继续使用现有服务端流程：

| 服务端来源 | exact Adapter |
|---|---|
| JD clarification / JD deterministic action | `save_application_jd_version` |
| submission snapshot deterministic action | `create_application_submission_snapshot` |
| outcome recording deterministic action | `record_application_outcome` |

`LegacyRouteSourceV1` 的完整封闭枚举和映射固定为：

```text
jd_clarification            → save_application_jd_version（初始 direct route）
jd_deterministic_action     → save_application_jd_version（初始 direct route）
submission_snapshot_action  → create_application_submission_snapshot（初始 direct route）
outcome_recording_action    → record_application_outcome（初始 direct route）
confirmation_resume         → 仅 proof issuer；Adapter 来自锁内 persisted protocol name
```

初始 Pending/route handle 保留实际 direct source；该 source 本期不新增持久列。确认恢复不
猜测或重建初始 source，统一签发 `route_source=confirmation_resume` 的新瞬态 proof/handle。
因此 issuer、Registry entry、route digest 和测试中的恢复 source 始终相同；其他枚举值
传入 proof issuer 必须拒绝。

初始 direct route 只能通过 Composition 注入的 source-bound issuer capability 与窄 Port：

```text
LegacyInitialRouteIssuer.open_request_lease(
    exact RuntimeRequestOwnerLease,
) -> exact LegacyInitialRequestLease

LegacyInitialRouteIssuer.issue(
    exact LegacyInitialRequestLease,
) -> exact ServerDeterministicInvocationToken

LegacyInitialRoutePort.resolve_initial(
    exact ServerDeterministicInvocationToken,
) -> LegacyAdapterRouteHandle
```

Composition 从 Adapter Spec 的 `initial_route_sources` 和 Bundle 的
`initial_route_bindings` 编译四个 exact source → Adapter binding，并原子创建四个不同的
`LegacyInitialRouteIssuer`：`jd_clarification`、`jd_deterministic_action`、
`submission_snapshot_action`、`outcome_recording_action`。前两个虽然指向同一个 Adapter，
issuer identity 仍必须不同。Issuer 是 application/Runtime-container scoped、可重复使用、
无 public constructor、不可复制/序列化的 capability，不是 invocation token；它永久绑定
一个 exact source、Adapter identity/ordinal、Runtime container、Catalog、Registry 与
`LegacyInitialRoutePort` identity。Issuer API 不接受 `source`、Adapter/tool name、Pending、
dict 或客户端字段，调用方不能要求它为另一 source 签发 token。

每个服务端确定性请求开始时，Pilot Runtime 为当前 request owner 创建 exact、不可构造的
`RuntimeRequestOwnerLease`，再由对应 source-bound issuer 的私有 lease factory 通过
`open_request_lease(owner_lease)` 创建新的 `LegacyInitialRequestLease`。该 factory 是 issuer
capability 的不可分割部分，不作为通用对象单独注入。生成的 Lease 同时绑定 exact issuer
instance/token、source、Adapter、当前 Runtime container、Initial Route Registry、Port
identity 与 request owner；`open_request_lease()` 必须验证 owner lease 属于同一 Runtime
container 且仍 live。它只在当前请求调用栈存在，不从 HTTP/SSE/Pending/Operation 字段构造。
对应服务端流程只获得自己那一个 source-bound issuer，并在其 live lease 内调用一次
`issue(lease)`；issuer 必须重新验证 lease 中的 exact issuer token/source 与自身一致。每个
`(lease, issuer)` 最多签发一个 token；同一 issuer 的后续请求使用新 lease，必须得到不同
token identity，且前一次 token 的 resolved/revoked 状态不得污染 issuer 或后续请求。

Initial Route Registry 的 token entry 绑定 exact token object、issuer instance、source、
Adapter、Runtime container、Catalog、Port/Registry identity、request owner lease 与
source-bound request lease。Port 只接受
exact token，并从 Registry entry 内部取得 source/Adapter；`resolve_initial()` 不再接收
调用方提供的 source。它通过一次 `issued → resolved` CAS winner 签发 route handle，拒绝
普通字符串、dict、Pending 字段、客户端/Provider tool name、伪造/复制 token、错误
issuer/lease/container/Port/Registry 和 `confirmation_resume`，不进入 Provider Surface，
也不与 proof consumer 共用可编辑名称 API。Initial route handle 继续绑定 exact
source/Adapter/Catalog/lease identity，Pending/Operation 仍由后续 Route
Handle/Repository 边界验证。

Initial Route Registry 必须线程安全，并以强引用保持 token、issuer、lease 和派生 handle，
直到 request lease 关闭；不得以 `id()`、值相等或调用方提供的 nonce 作为授权根。
`open_request_lease`、`issue`、`resolve_initial` 与 lease close 在同一 Registry 状态锁内
线性化：close winner 会
fence 尚未 resolve 的 token；resolve winner 已生成的 handle 也会由随后的 close 撤销。
因此 cancellation/退出与 resolve 并发时只有上述两个封闭结果，不存在 close 后仍可取得
live handle 的中间态或 ABA 复用。

`RuntimeRequestOwnerLease` 和 `LegacyInitialRequestLease` 均只有
`open → closing → closed`，前者关闭会级联关闭其全部 source-bound child lease；
`ServerDeterministicInvocationToken` 为 request-local、source-specific、不可复制/序列化的
一次性 token，只允许 `issued → resolved → revoked` 或 `issued → revoked`。每个请求必须
以 `finally` 关闭 lease，并统一 revoke 该 lease 下所有 token 和派生 handle；正常结束、
Pending persistence 成功后的请求退出、Pending persistence 失败、Exception、Cancellation
及其他 `BaseException` 均适用。关闭/过期 lease 不得再签发或解析 token。重复、跨请求、
跨 source/container/Port 使用均绝对失败，Pending/Operation/Adapter/executor 为 0。
Lease close/revoke 是幂等、no-throw 的内存状态转换，不得覆盖原始 Exception/
Cancellation/`BaseException`。Issuer capability 本身不随请求关闭，下一请求仍可签发
全新的 token。

`confirmation_resume` 永远不能获得上述 issuer 或 initial request lease；它只走后文锁内
验证后签发 `LegacyRouteProof` 的恢复边界。

确认恢复的 Catalog API 不接受装有普通 server-loaded 字段的 DTO、仅有 `tool_name`
的 Protocol、dict 或任意调用方构造的 exact-type 对象。锁内事实验证与静态 Catalog
resolve 拆成两个权限边界：

```text
approve / modify 的只读 prepare 阶段（claim 前）
  → API 将已验证确认请求构造为 exact LegacyApprovedConfirmationInput
  → LegacyRouteProofIssuer.prepare_server_loaded(
        read_session, lookup_identity, confirmation_input
    )
  → verifier 重读并验证 server-loaded Pending/Operation 的 proposed 身份
  → issuer 内部取得 LegacyPreparationBinding（无 execute capability）
  → issuer 通过该 binding 执行 prepare_legacy_arguments() + Adapter validate()
  → PreparedLegacyCall（effective args + exact preparation identity）

现有 confirmation claim transaction
  → BEGIN IMMEDIATE
  → 重读 Conversation + Pending + WriteOperation
  → mutable recheck
  → Pending claim/CAS 成功，得到 exact LegacyClaimLease
  → LegacyRouteProofIssuer.issue_after_claim(
        session, issuance_lease, claim_lease, prepared_legacy_call
    )
  → verifier 再次验证锁内身份、claim、effective args 与已有 Ledger HMAC
  → LegacyRouteProof（一次性、transaction-scoped）
  → LegacyDeterministicCatalog.resolve_server_loaded(proof)
  → exact LegacyAdapterRouteHandle
  → tool.started / executor 恰好一次
```

这样保持已经批准的顺序：prepare/validate 在 claim 前，锁内 mutable recheck 与 claim
仍是执行授权前置条件，proof 在 claim 成功后才可能签发。`confirm()` 不得继续先用
`pending.tool_name` 获得可执行 Adapter；claim 前只能得到不含 execute callable 的
`LegacyPreparationBinding`。Catalog 是 executable route 的唯一解析边界，执行期不得
按 name 重建 Catalog/Adapter。

只读 prepare 使用一个 Session-bound read transaction 一次性读取 Conversation/Pending/
Operation，不调用内部另开 Session 的 Repository helper，也不写 claim 或业务状态；冻结
必要 primitive 后结束 read transaction，再做纯 CPU prepare/validate。其结果只是候选，
write transaction 中的权威重读、mutable recheck、claim 和 proof 必须全部重做，不能信任
旧 Repository/ORM 状态。

`LegacyRouteProofIssuer` 是 Repository/Coordinator 边界内的组合对象；它不是 Catalog
静态元数据的一部分。只读 prepare API 只接受 read Session、conversation lookup
identity 和 exact `LegacyApprovedConfirmationInput`；执行 proof API
只接受当前 caller-owned write Session、该业务
事务的不透明 `LegacyRouteIssuanceLease`、exact `LegacyClaimLease` 和 exact
`PreparedLegacyCall`。两者都不接受 tool name、persisted raw args、调用方计算的 digest
或调用方构造的 evidence；唯一允许的编辑值入口是下述封闭 confirmation input。Issuer
通过注入的 `LegacyPendingIdentityVerifierPort` 在对应 Session 中重读
权威行；Port 返回的
`LockedLegacyRouteEvidence` 同时绑定 exact verifier instance token 与 issuance lease
token（只读 prepare 使用独立 read-snapshot token），并由 issuer 在同一调用栈中立即消费。
Evidence 为 `repr=False`，可短暂包含重算
指纹所需的 canonical args 和持久 primitive；它从不返回给 Coordinator/Catalog，不能
逃逸事务、进入通用序列化或作为另一个持久 DTO。字段相同但没有 exact verifier/lease
registry provenance 的伪造 Evidence 必须在 proof 注册前拒绝。

`LegacyApprovedConfirmationInput` 不是授权凭证，而是 API validation 后的封闭瞬态输入：

```text
LegacyApprovedConfirmationInput（exact type, frozen, repr=False）
├── decision = approved
├── operation_id = canonical UUID | missing
├── edited_args_state = missing | present
├── edited_args = MISSING sentinel | deep-copied canonical-safe mapping（present）
├── confirmation_token = bounded opaque string（repr=False）
├── rejection_feedback_present = false
└── rejection_feedback = ""
```

外部 `edited_args` 继续只接受字段缺失、空 object 或非空 object；显式 `null` 维持现有
422，不能构造 input。Approve 的 `missing` 使用 persisted args；modify 的 `present` 只能
通过 exact Preparation Binding 的 editable-field policy 合并。Input 不含客户端 tool
name 或 persisted raw args，token/final mapping 不进入日志、repr 或持久化；issuer 内部
重算 confirmation-token/request fingerprint，不信任调用方 digest。Reject 不构造该对象。

`lookup_identity` 只含 conversation ID；可选 operation ID 只来自 confirmation input。
若提供，必须 canonicalize 并与锁内 Conversation/Pending/Operation 全部一致；若缺失，
non-terminal prepare 只能从
当前 `Conversation.pending_operation_id` 锁内引导并把结果绑定进 PreparedCall。Pending
已清理的 terminal replay 必须依赖已有 operation ID 走 Ledger-first 路径；缺失时沿用
现有 stale/invalid 结果，不能从 tool name 或历史消息猜测。

`LegacyPreparationBinding` 与 `PreparedLegacyCall` 同样是 Registry exact-identity
对象、`repr=False`、不可复制/序列化。Preparation Binding 只允许访问该 exact Adapter
的 recursively frozen editable fields、describe/validate/presentation port；不得暴露
Adapter 对象、execute callable、Repository 或 Session。`PreparedLegacyCall` 绑定
preparation binding identity、persisted Pending identity、原始 args digest、effective
args digest、request fingerprint 和 effective args（后者仅瞬态、`repr=False`）。它不
构成执行授权；只有锁内 proof 消费后签发的 route handle 才能进入 executor。

read-snapshot token 在只读事务结束并形成 PreparedCall 后立即关闭，read Session、
Locked Evidence 和 live Preparation Binding capability 随即销毁。Registry 原子地把
Binding entry 转成 immutable preparation-identity tombstone，只保留 opaque binding/
exact Adapter identity，不保留 callable、Session、Evidence 或 persisted raw args；
PreparedCall 绑定该 tombstone 与本次 confirmation attempt token。其生命周期唯一为：

```text
preparation_open → prepared → consumed
                 ↘ revoked
prepared         → revoked
```

`issue_after_claim()` 在全部锁内校验成功后，以一次原子 Registry transition 完成
`prepared → consumed` 并注册 execution proof；只有 winner 能得到 proof。prepare/
validation failure、claim CAS failure、proof failure、request 结束、Exception、Cancellation
或其他 `BaseException` 都在 `finally` revoke 未消费 attempt/PreparedCall。后续请求必须
重新读取、重新 prepare；同一 Pending 的旧 PreparedCall、同字段 clone 或不同 claim/
Session/attempt 复用均在 proof/Adapter/executor 前失败。

Consumed preparation-identity tombstone 作为无可复用能力的 provenance 保留到 issuance
lease 关闭；PreparedCall 的 effective args 私有引用另由 `LegacyPreparationRegistry` 的
exact consumed-call entry 保留到 executor 返回，以支持 route handle 校验和恰好一次
调用。`LegacyRouteProofRegistry` 只接收 PreparedCall opaque identity/digest，不持有该
对象或 effective args。随后无论 commit/rollback/异常都清除引用。read transaction 结束
时必须销毁 live Binding capability，但不能提前删除 identity tombstone；transaction 退出
后不得保留 effective args。

Issuer 在签发前必须在锁内逐字段验证：

- Conversation ID/scope revision 及 owning Pending 的真实归属；
- `adapter_kind=legacy_deterministic`、`operation_role=primary`，且 proof route source
  必须精确为 `confirmation_resume`；
- operation/tool call/protocol name 与 Pending/Operation 一致；
- Pending args、`proposal_fingerprint`、`confirmation_token_fingerprint` 与 Ledger
  `fingerprint_key_id` 重算一致，其余 fingerprint/null 状态精确符合下表；
- PreparedCall 仍为当前 confirmation attempt 的 `prepared` 状态，exact Adapter identity、
  persisted Pending digest、effective args/input fingerprint 和 request fingerprint 均重算一致；
- claim ID/timestamp 与当前 confirmation claim/CAS 一致；
- 当前 issuance lease 仍绑定同一 Session/transaction/claim，且未结束。

Legacy proposed/claim 的 fingerprint 真值表必须精确保持 Phase 3：

| 阶段 | proposal | confirmation token | authorization scope | input | operation request | Conversation claim |
|---|---|---|---|---|---|---|
| read-only prepare snapshot | 非空并重算一致 | 非空并重算一致 | 必须 `NULL` | 必须 `NULL` | 必须 `NULL` | `"" / NULL` |
| claim 后、proof 签发前 | 同上 | 同上 | 必须 `NULL` | 行内仍为 `NULL` | 行内仍为 `NULL` | `operation_id / non-null claimed_at` |
| rejected/replay | 按既有 terminal 完整性规则 | 按既有规则 | 必须 `NULL` | 必须 `NULL` | 非空 | 按 Phase 3 rejection/delivery 状态 |
| committed/failed/replay | 按既有 terminal 完整性规则 | 按既有规则 | 必须 `NULL` | 非空 | 非空 | 按 Phase 3 delivery/replay 状态 |

proof issuer 在 claim 后计算并绑定 `write-operation-legacy-input-v1` effective input
fingerprint 与 `write-operation-request-v1` request fingerprint，但不要求它们在 proposed
row 中提前存在。只有既有 terminal commit 路径把二者写入 Operation；proof/prepare
失败会 rollback claim，Operation 仍保持 proposed 真值。Legacy 不制造 Typed
`authorization_scope_fingerprint`。Terminal/replay 不签发 proof，只走既有完整性校验。
`LegacyClaimLease` 由更新 `Conversation.pending_confirmation_claim_id` 与
`Conversation.pending_confirmation_claimed_at` 的现有 CAS winner 创建；不在
WriteOperation 增加 claim 字段，也不能把 claim 前的空值当成执行授权。

`LegacyPendingIdentityVerifierPort` 由 Write Coordinator 注入 issuer，可使用已加载的
Ledger HMAC verifier/key domain 重算已有指纹；Catalog、Adapter 和 proof registry
不持有 Ledger key、原始 preimage 或 Repository。事务内不允许因此新增
keyring/文件 I/O；不可用的 verifier 会在 Adapter、Repository 写入和 executor 前
fail-closed。

`LegacyRouteProof` 的实际授权来源是注册表中的 exact object identity 与
issuer provenance，不是 Python 类名或可复制字段：

```text
LegacyRouteProof
  exact type / slots / frozen / eq=False / repr=False
  无 public constructor
  copy / deepcopy / pickle / generic serialization 显式拒绝
  只携带不透明 proof identity，不携带 key/args/ORM/Session/Repository

LegacyRouteProofRegistry entry
  exact proof object identity
  exact issuer instance token
  exact Bundle + Legacy Catalog instance token
  exact Session/transaction/claim lease token
  adapter_kind=legacy_deterministic / operation_role=primary / route_source
  conversation scope revision / operation / tool call / protocol name / claim identity
  pending_identity_digest
  exact preparation-binding identity token + PreparedLegacyCall opaque identity token
  effective input fingerprint / operation request fingerprint
  exact Adapter object identity
  state = issued | resolved | revoked
```

Application Composition 从同一个 sealed Legacy Catalog 创建独立、线程安全的
`LegacyPreparationRegistry` 与 `LegacyRouteProofRegistry`，以及只能签发的 issuer token
和只能消费 proof 的 exact Catalog token。Preparation Registry 是 issuer/Coordinator
私有的 request-local args/call lifecycle 边界；Proof Registry 只保存安全 identity/digest/
exact Adapter binding。Proof Registry 以强引用保持已签发 proof 穿过 resolved 状态，直到 transaction
lease revoked，
不以 `id()`、值相等或可控 nonce 作为授权根。即使调用方绕过
private constructor 制造 exact-type/同字段对象，也不存在对应 registry entry，
必须拒绝。Registry 不暴露通用 `register()`；只有持有 exact issuer token
的 `LegacyRouteProofIssuer` 能创建 entry。

为保持 Catalog 递归冻结，Composition 使用内部原子 factory 从 ordered Adapter tuple
同时创建 Catalog instance token、两个 confirmation Registry、issuer-only registration
port、consumer port 和最终 Catalog；不存在“先构造 Catalog、再可变挂载 Registry”的
阶段，也不向其他模块暴露半初始化对象。Initial Route Registry/Port 与四个 source-bound
issuer capability 同样在该原子 factory 调用内绑定最终 Catalog identity，但不与
confirmation proof Registry 共用注册或消费 API。

Registry 在 Composition 时只从实际 sealed Catalog 接收 ordered exact Adapter
object/name identity，不建立第二份可编辑名称表。Issuer-only registration port 仅在
`route_source=confirmation_resume` 时，用已验证的 persisted protocol name 命中这三个
exact Adapter 之一；unknown、其他 source 或客户端 name 均不能创建 entry。表中的四个
initial direct route source 由对应 source-bound issuer 与 Initial Route Registry 绑定 exact
Adapter；服务端流程本身不接收或选择 Adapter name，并且它们不进入 proof registry。

`resolve_server_loaded(proof)` 只能以 exact Catalog token 一次性消费 proof。
Registry 必须验证 proof 对象身份、issuer/Catalog/Bundle/transaction/claim provenance、
live lease 和 `state=issued`，再返回 registry 已绑定的 exact Adapter identity；
Catalog 不重算 digest，不从 proof 的普通字段或客户端 tool name 重新选择
Adapter。成功消费原子地使 proof 进入 `resolved`，然后签发同一事务
的 `LegacyAdapterRouteHandle`。同一 proof 重用、不同 issuer/Catalog/Bundle/
transaction/claim 消费、已过期或已撤销 proof 全部在 Adapter/Repository/
executor 前 fail-closed。

允许的 registry lifecycle 只有 `issued → resolved → revoked` 或
`issued → revoked`。Resolved entry 作为不可复用 tombstone 保留到 transaction lease
结束，避免对象 identity/nonce ABA；lease 撤销后清除强引用。并发消费只有一个原子
winner 能获得 Adapter binding，loser 为绝对失败且不能得到 Handle。

事务在 commit、rollback、Exception、Cancellation 或其他 `BaseException` 退出时，
Coordinator 必须在 `finally` 关闭 issuance lease，并撤销其下所有 proof 和已派生
route handle。Proof 或 Handle 不得跨 Session/事务/claim 复用；执行前发现关闭/
撤销失败时 executor 为 0，执行后的清理异常不得触发第二次 Adapter/executor 调用，
并按现有事务安全失败路径 rollback/收敛。

`conversation_id` 使用 positive int64，`conversation_scope_revision` 精确映射
已有 `Conversation.scope_revision`，不引入 `conversation_generation`、`updated_at`
或另一个持久代数。

`pending_claim_identity` 的 canonical 形状固定为：

```text
{
  "claim_id": Conversation.pending_confirmation_claim_id,
  "claimed_at": canonical_claimed_at | null
}

claim_id = ""  → claimed_at 必须为 null
claim_id != "" → claimed_at 必须存在
```

`canonical_claimed_at` 把已有 SQLite/SQLAlchemy datetime 按当前仓库约定处理：
naive 值视为 UTC，aware 值转 UTC，输出固定六位微秒的
`YYYY-MM-DDTHH:MM:SS.ffffffZ`。时间只是已有 claim 身份的完整性输入，
不新增独立 CAS 规则；权威 claim/CAS 仍用 Phase 3 现有查询。
Issuer 计算的 `pending_identity_digest` 使用现有 Ledger HMAC key 和新的
域分离：调用 `ledger_fingerprint(key, "legacy-route-pending-identity-v1", preimage)`，
其实际 HMAC 前缀字节为 `b"legacy-route-pending-identity-v1\0"`，避免调用方把 NUL
重复放进 domain 参数。输入是上述所有 canonical
primitive 及按 Legacy 现有 codec 规范化的 args。它只放在瞬态 registry
entry 中用于 route handle 绑定，不是调用方传入 proof 的字段；
digest/claim identity 均不记录、不持久。Issuer 必须先逐字段比对锁内
Conversation/Pending/Operation，再重算和比较已有 Ledger 指纹，不能只比较
最终 route digest。

route digest 的 canonical preimage 唯一固定为：

```text
{
  "schema": "legacy-route-pending-identity-v1",
  "adapter_kind": "legacy_deterministic",
  "operation_role": "primary",
  "route_source": "confirmation_resume",
  "conversation_id": <positive JSON integer>,
  "conversation_scope_revision": <non-negative JSON integer>,
  "pending_claim_identity": {
    "claim_id": <existing bounded string>,
    "claimed_at": <canonical_claimed_at | null>
  },
  "operation_id": <Phase 3 canonical UUID string>,
  "tool_call_id": <existing canonical bounded string>,
  "tool_name": <persisted exact Legacy protocol name>,
  "fingerprint_key_id": <Phase 3 canonical UUID string>,
  "normalized_args": <Legacy existing codec canonical JSON object>
}
```

采用现有 canonical JSON 的 UTF-8、键排序、紧凑分隔符、有限数和 Unicode 不规范化
规则；不省略 null、不接受额外字段。该 preimage 只在 issuer 内存中存在，Catalog 和
Registry 均只接收计算后的 digest 与验证过的安全 primitive。

Legacy args 兼容规则不得由 proof 层另造 decoder：

- read-only prepare 先按当前 Legacy `json.loads`/Adapter validate 行为解析 persisted
  args；合法 JSON object 才能形成 `PreparedLegacyCall`；
- 无编辑时保留当前 raw encoded args 供 describe/validate/executor，identity/HMAC 使用其
  解码后的 canonical object；有编辑时继续由 `prepare_legacy_arguments()` 合并允许字段，
  生成当前 compact effective encoding，再 validate；
- current codec 对重复 object key 的 last-value-wins 行为保持兼容；route digest 对解码后
  object 计算，不假装保留原始数字/键词法；非有限数仍由现有 canonical/HMAC 边界拒绝；
- scalar、非法 JSON 或 Adapter validation failure 使用现有可见 validation/stale 语义，
  不签发 `PreparedLegacyCall`/proof，claim/Repository write/executor 为 0；
- `pending_identity_digest` 只绑定 persisted 原始 Pending 的语义对象；修改后的 effective
  args 另以现有 `write-operation-legacy-input-v1` fingerprint 绑定。二者不得互换，且
  route handle 执行前必须同时匹配 exact PreparedLegacyCall identity 与 effective input
  fingerprint。

初始动作由对应 source-bound issuer 在 request lease 内签发一次性 token，再由
`LegacyInitialRoutePort` 从 Registry 中解析 exact Adapter 并获得
`LegacyAdapterRouteHandle`；服务端流程不直接选择 Adapter，该路径也不需要数据库恢复
proof。确认恢复只读取数据库中的 server-loaded Pending，经过 issuer 锁内验证后，再由
`LegacyDeterministicCatalog.resolve_server_loaded(proof)` 消费一次性证明并签发新
Handle。Handle 绑定 exact Catalog/Adapter identity、route source、transaction/claim
lease 和 Pending primitive identity/digest，不保存 Pending model 或 arguments。客户端
提交的 `tool_name` 不参与选择；unknown、不匹配或非 server-loaded Pending 按现有
stale/invalid 路径失败，Adapter、Repository 和 executor 均为 0。

该 proof 路径只服务于 approve/modify 的 non-terminal Legacy resume。Reject 继续只做
token/Pending/Ledger identity 与 rejection CAS，不解析/规范化 args、不创建 issuance
lease/proof、不查询 Adapter；terminal replay/delivery recovery 同样不签发 proof，
Catalog/Adapter/Provider/executor 均为 0。

必须保持：

- Provider builder、Selector、dependency closure 和 Dispatcher 永远看不到 Legacy；
- Typed Catalog miss 绝不尝试 Legacy；
- Legacy 初始动作只能由服务端确定性流程创建；
- confirmation resume 先读取服务端 Pending/Ledger `adapter_kind`，由 issuer 在锁内
  签发 proof，再由 Catalog 一次性消费 proof 签发 exact Adapter Handle；
- 客户端 `tool_name` 不能单独进入 Legacy；
- 专用确认、幂等、CAS、Ledger、写入、恢复和用户可见结果不变。

## 12. 错误、诊断与隐私

### 12.1 运行时失败

正常生产进程中 Bundle 在启动后不可变化。若完整性 seal 在请求期间发现异常：

- Provider Surface 构造阶段：按现有 Projection internal failure fail-closed，Provider 0；
- Provider response 后、executor 前：按现有安全内部失败路径收敛，executor 0；
- 已进入 Ledger terminal/replay：完全按 §10.6 的持久 identity/digest 回放，不重新读取 Metadata，也不触发 executor 或 Provider；
- 不触发 Legacy、旧 Registry 或全量 Surface fallback。

不得新增用户可见错误协议。`Exception` 继续按现有边界映射；Cancellation、`SystemExit`、`KeyboardInterrupt` 等 `BaseException` 原样传播。

### 12.2 诊断

允许的安全诊断仅包括：

```text
metadata schema/version
bundle fingerprint
tool/domain/capability/operation 的有界计数
封闭错误 code
```

禁止记录：

- Tool arguments、results、异常原文；
- Prompt、回答、附件名或正文；
- 实体 ID、Pending token、Operation ID、HMAC 或凭据；
- callable repr、对象地址或动态 Provider 配置。

本期不增加 Journal 事件或 Manifest 字段。即使静态 Metadata 诊断失败，也不能改变已冻结 Surface、Tool Outcome 或业务事务。

## 13. 模块与依赖方向

建议模块结构：

```text
ai/tool_runtime/contracts.py
  ProviderToolContract、BindingContract、WriteContract、runtime DTO

ai/tool_runtime/policy_types.py
  ToolCapability、ToolDomain、ProviderVisibility、LegacyBoundaryVisibility、
  OperationKind 等无依赖 leaf enum

ai/tool_runtime/protocol_seals.py
  两个固定 Provider/Legacy boundary fingerprint；只做整体比较，不提供名称查询

ai/tool_runtime/metadata.py
  ToolSurfaceMetadataV1、Discovery Policy、Frozen JSON、通用 compiler/View/Port
  只依赖 tool_runtime contracts，不导入 tool_specs、Repository 或 Pilot Runtime

ai/tool_specs/<domain>.py
  显式声明每个 ToolSpec + Metadata + runtime handler

ai/tool_specs/catalog.py
  唯一生产 Composition Root，组装六个领域的 25 个 Spec

ai/tool_runtime/legacy.py
  Legacy boundary、静态 Adapter、Session-bound execution context、
  LegacyInitialRouteIssuer capability、RuntimeRequestOwnerLease、
  LegacyInitialRequestLease/Registry、
  LegacyInitialRoutePort 和唯一 Legacy Catalog；direct route issuer 只凭 exact live request
  lease 签发 token，Port 只消费该 Registry 注册的 exact token，
  resume route 只消费已注册 LegacyRouteProof，不持有 Ledger key、Repository 或原始 Pending

ai/tool_runtime/legacy_proof.py
  LegacyApprovedConfirmationInput、LegacyPreparationBinding、PreparedLegacyCall identity、
  LegacyRouteProof、opaque issuer/catalog/read-snapshot/preparation/transaction token 和
  分离的 LegacyPreparationRegistry/LegacyRouteProofRegistry；无 Ledger key、HMAC、
  Repository、ORM 或 Pilot Runtime 依赖

context_projector/selector.py
  纯选择算法，只消费 injected Discovery/Dependency View

ai/tool_authority/*
  纯 Policy/Authority，消费 Bundle 的 Authority View

ai/write_operations.py
  消费 injected ToolOperationMetadataPort

pilot_runtime/compensation.py
  四个 session-bound Compensation Handler 与 Registry

pilot_runtime/primary_undo.py
  四个 required-undo builder binding 的共享 contract/checkpoint helper；工具特有
  seed/build callable 仍在对应领域 Spec 显式绑定

pilot_runtime/legacy_route.py
  LegacyRouteProofIssuer、LockedLegacyRouteEvidence 和 LegacyPendingIdentityVerifierPort；
  只读 prepare 签发无 execute capability 的 binding；只在 Write Coordinator 已持有
  锁内 Session 且 claim CAS 成功后签发 execution proof

pilot_runtime/composition.py
  组装 Typed Catalog + Legacy Adapter Catalog + Compensation Registry，创建完整 Bundle，
  原子创建四个 source-bound Legacy initial-route issuer capability（各自封装匹配的私有
  request-lease factory）、Runtime request-owner lease factory、initial route Port/Registry、
  proof issuer/consumer token 与 Preparation/Proof
  Registry，并将精确 capability/View/Port 注入对应确定性流程、Projector、Agent Loop、
  Pipeline 和 Ledger
```

依赖规则：

- `tool_runtime` 不导入 `tool_specs`；
- `legacy_proof.py` 不导入 Ledger、keyring、Repository、ORM 或 Pilot Runtime；
- Preparation Registry 是唯一可持有 request-local effective args 的 registry；Proof
  Registry/Catalog/route handle 只接收 identity/digest，不能保留 PreparedCall 或 args；
- `legacy.py` 只能消费 proof Registry 的窄验证结果，不能导入 Ledger key/HMAC verifier、
  Repository 或锁内 evidence；
- `pilot_runtime/legacy_route.py` 是唯一允许把锁内 evidence 与 Ledger verifier 组合后调用
  proof issuer registration port 的生产模块；
- Selector、Authority、Ledger 和 Repository 不导入生产 Composition Root singleton；
- `pilot_runtime/composition.py` 是生产对象装配点；
- 四个 Initial Route Issuer 只能由 Composition 创建；每个精确服务端确定性流程只能收到
  与自身 source 绑定的一个 issuer；通用 Runtime request-owner lease factory 只能注入
  Pilot Runtime orchestration，source-bound child lease factory 只能封装在对应 issuer 内，
  二者都不能暴露给 API DTO、通用 Dispatcher 或 confirmation resume；
- 非 Composition Root 模块不得重新构造 Bundle；
- Projector 不再默认导入 `MODEL_TOOL_CATALOG`，由 Runtime 显式注入；
- Write Coordinator 不通过反向导入领域 Spec 获取 Operation metadata；
- `protocol_seals.py` 只能被 Composition/compiler 整体校验调用，不向 Selector、
  Authority、Repository 或 Dispatcher 提供名称查询。

## 14. 删除门禁

切换完成后必须从生产代码删除：

- `MODEL_TOOL_NAMES` 手写 tuple；
- `LEGACY_DETERMINISTIC_NAMES` frozenset（改由 ordered Adapter Catalog/Boundary 派生）；
- Selector 的 `_DOMAIN_TOOLS`、`_DEPENDENCIES`、`_LEXICAL_RULES`、`_PAGE_DOMAINS`、`_ATTACHMENT_DOMAINS`；
- 独立生产 `DEPENDENCY_POLICY_V1` singleton；
- Typed write、Legacy write、required undo、compensation 的名称集合；
- `ToolCatalog._with_write_contract()`；
- composition root 的 `_with_runtime_metadata()` 和按工具名 confirmation switch；
- `editable_fields_for_tool(name)` 名称查询 façade；Typed 确认从 exact resolved Spec
  metadata 投影，Legacy 确认从 exact Adapter 投影；
- `ChatRepository._pending_adapter_kind()` 及任何按 Pending tool name 选 Typed/Legacy 的分支；
- `api.py::_undo_seed_for_pending()`、`_build_write_undo()`、
  `_CREATED_RECORD_FINGERPRINT_FIELDS`，以及任何等价的 required-undo 名称 switch；
- `write_operations.py::_chained_adapter_kind()`，改为消费 exact parent identity 与
  child Pending Route Handle 的 topology policy；
- `api.py::_pending_action_details()`、`pilot_runtime/service.py` 的按工具名摘要分支和
  `write_operations.py` 的首次执行 human projection 分支，改为 exact presentation binding；
- 捕获 Repository/Service 的 Legacy executor lambda 和执行期 Catalog rebuild；
- `pilot_runtime/deterministic.py::_legacy_catalog()`、`_legacy_adapter(pending)`、当前
  `_executor()` 内 Session-bound Catalog rebuild，以及 dependencies 中可注入的
  `legacy_catalog_factory`；改为 read-only preparation binding + claim 后 proof-derived
  route handle；
- `LegacyDeterministicCatalog.resolve_server_loaded(pending)` 和接收
  `ServerLoadedPending` Protocol 的旧 API；Catalog 只保留 proof consumer API；
- `build_legacy_deterministic_catalog(jd_service, outcomes)` 的 Repository/Service 参数与
  closure；新 builder 只构造静态 Adapter spec，执行依赖只来自 caller-owned
  `LegacyExecutionContext`；
- Authority/Selector/Ledger 中复制的工具静态矩阵；
- 任何 catalog drift alternate surface、Typed→Legacy fallback 或 shadow path。

以下为精确允许保留的独立契约资产，不属于 Python 运行时分类集合：

- `models.py` 的 `ck_write_operations_manifest/undo_policy`：已发布数据库 CHECK；
- `agent_runtime/events.py::_TOOL_NAMES`：Phase 1 持久 Journal Event Schema 白名单；
- 各领域 `ProviderToolContract.name`：Provider 契约本身；
- Legacy Adapter 的三个 name：服务端确定性协议；
- Compensation Handler 的四个 operation kind：实现绑定；
- Renderer/presentation 中的封闭业务值 label map（例如 event type → 中文）；
- tests/fixtures 中的只读 Golden；
- `tool_runtime/protocol_seals.py` 中两个完整 boundary input/digest。

Journal `_TOOL_NAMES` 不从 Bundle 动态生成，本期保持第一期事件 Schema 原样；CI 单独
验证它与 25 Typed + 3 Legacy 的已发布事件白名单一致。Presentation label 不参与
visibility、authority、Ledger 或 Undo 分类。
工具名 → projector/renderer/route 的 map 不属于这个例外，必须迁入 exact
Spec/Adapter binding；terminal replay 兼容 renderer 只能消费持久 terminal payload。

当前 `pilot_runtime/deterministic.py::_LEGACY_EDITABLE_FIELDS` 与 Legacy Adapter
`editable_fields` 是重复真值，前者必须删除；确定性确认 UI 从 exact Adapter 的只读
editable fields 投影。其他 Legacy 专用行为仍留在 Adapter。

AST/source gate 必须证明：

- 除上述精确 allowlist 外，生产 Python 不存在用于分类/路由的模块级工具名集合；
- 不使用 `startswith`、名称前缀、字符串拼接或 `getattr` 推断 domain/write/undo/visibility；
- Provider builder 只消费完整 `ProviderToolContract` materialized payload；
- 模型 Dispatcher 只能查询 Typed Catalog；
- Legacy 和 Compensation 不能进入 Provider Surface；
- `tool_runtime` 无反向 import；
- Golden loader 只有读取/canonicalize helper；
- `protocol_seals.py` 是唯一 boundary seal allowlist，且没有任何单工具查询 API；
- 所有 operation-bearing Pending Repository 入口都要求 exact route handle；
- Legacy executor 只消费 caller-owned Session-bound context，不捕获 Repository；
- Legacy Catalog 不存在接收普通 server-loaded fields/dict/Protocol 的 resolve API，只接受
  exact `LegacyRouteProof` 并通过 Registry 的 exact object identity 一次性消费；
- Composition 必须恰好创建四个 reusable、exact source-bound
  `LegacyInitialRouteIssuer` capability；精确 allowlist 的四个服务端确定性入口各自只能接收
  对应 issuer，并只能从 Pilot Runtime 新建的 live `RuntimeRequestOwnerLease` 派生与该 issuer
  identity/source 匹配的 `LegacyInitialRequestLease`，再签发一次新 token；生产代码不得
  直接构造 token/lease、调用通用 `issue(source, ...)`、把 source 传给 issuer、拆出/替换
  issuer 私有 child-lease factory，或让 `confirmation_resume` 获得 issuer/任一 lease factory；
- 初始 Legacy route 只能由 `LegacyInitialRoutePort.resolve_initial(exact token)` 消费 Registry
  注册的 token；Port 内部从 issuer-bound entry 取得 source/Adapter，不接收 source，且
  Port/Catalog 不存在按客户端/Pending/tool name 查询 initial Adapter 的 API；
- Legacy claim 前只允许 Registry-bound `LegacyPreparationBinding` 的
  editable/describe/validate/presentation port，任何 prepare object 都不能访问 execute
  callable；execution proof issuance 只能出现在锁内 mutable recheck 与 claim CAS 成功后；
- 只有 `pilot_runtime/legacy_route.py` 可调用 proof issuance registration port 或
  `LegacyPendingIdentityVerifierPort`；`legacy.py/legacy_proof.py` 禁止导入 Ledger key、
  keyring、HMAC helper、Repository、ORM 或锁内 evidence；
- `LegacyApprovedConfirmationInput`、`LegacyPreparationBinding`、`PreparedLegacyCall`、
  `LegacyInitialRouteIssuer`、`RuntimeRequestOwnerLease`、
  `LegacyInitialRequestLease` 及 issuer 私有 child-lease factory、
  `ServerDeterministicInvocationToken`、Initial Route Registry entry、
  `LockedLegacyRouteEvidence`、
  read-snapshot/preparation/issuance/claim lease、
  `LegacyRouteProof`、Preparation/Proof Registry entry、route handle 均不得进入 Pending、Ledger、Journal、
  ChatMessage、checkpoint、HTTP/SSE、日志、repr、pickle 或通用序列化；
- 不存在 feature flag、双轨、旧 handler registry 或 fallback。

必要的工具名字符串仍可存在于：

- 各领域 `ProviderToolContract.name`；
- dependency/compensation metadata；
- Legacy Adapter 注册；
- `protocol_seals.py` 的已批准整体边界输入；
- 已发布 SQL CHECK、Journal Schema 白名单和 presentation label；
- 独立 Golden 和测试输入。

Gate 禁止的是第二份运行时分类真值，不是禁止所有合法名称字面量。`getattr` 禁令只
针对通过动态字段推断 domain/write/undo/visibility，不影响读取既有 Operation 字段。
每个 Gate 必须声明文件范围、exact allowlist/denylist，并有负向 fixture 覆盖直接引用、
import alias、局部别名、集合字面量和基础反射写法。

## 15. 切换、迁移与回滚

### 15.1 测试先行切换顺序

实施计划应按以下顺序拆分：

```text
1. 从 0c10e05 独立捕获 Metadata/Discovery/Operation 资产与第五类
   Resolver implementation binding Golden，记录各自 raw/canonical digest
2. 先写 Metadata contract、canonical、deep-freeze 和负向测试
3. 建立通用 compiler 与生产严格 Composition Root
4. 六个领域 ToolSpec 显式声明 Metadata
5. 切换 Provider/Selector/Dependency View
6. 切换 Authority/Pipeline/HITL
7. 切换 Primary Undo Builder、Ledger/Repository Operation/Pending Route Port
8. 收口 Session-bound Legacy Boundary、锁内 Legacy proof issuer/Registry、chained topology
   与 Compensation Registry
9. 删除全部旧常量、名称分派、隐式补全和 fallback
10. 运行组合门禁、独立 CR 和发布验收
```

可以分批提交测试和实现，但生产运行时只允许一次性切换。不得在合并版本中保留新旧双轨。

### 15.2 数据迁移

本期：

- 不新增或修改数据库表、列、index、CHECK 或 migration；
- 不重写现有 Pending、Ledger、Journal、Conversation 或 ChatMessage；
- 不改变 persisted policy/version/fingerprint；
- 不需要数据备份或 reset。

### 15.3 回滚

回滚方式是整体 revert 本项目提交。由于没有 Schema 或数据变化，回滚不需要数据库降级。

不能在生产代码中保留 runtime rollback switch。若新 Bundle 初始化失败，应阻止有缺陷的构建启动，而不是回到旧静态集合。

## 16. 测试与机械门禁

### 16.1 Metadata 合同

覆盖：

- 精确 25 Typed、3 Legacy、4 Compensation handler；
- 名称、ordinal、domain、dependency、capability 和 operation 唯一性；
- unknown enum、重复值、空 metadata、跨 Catalog dependency、自引用和环；
- read/write/confirmation/undo/compensation 非法组合；
- editable fields 与 Provider Schema；
- `ResolverImplementationBinding` 与 Metadata descriptor/implementation identity 闭合；
- resolver 数量正确但 callable 互换、implementation ID/qualified name 漂移；
- Undo builder 数量正确但 seed/build callable、builder ID、contract version 或 payload
  kind 互换；
- Frozen JSON 深层 mutation、Provider payload mutation 和 callable replacement；
- canonical 顺序、Unicode、非有限数字和 fingerprint 稳定性；
- 多线程并发读取同一 Bundle 获得相同 identity、内容和 fingerprint；
- Segment lease identity 不同但共享 exact Bundle，且不存在 per-Segment recompile/deepcopy；
- Provider/Legacy protocol seal 的 name 替换、顺序替换、数量不变替换均启动失败；
- exact-key Manifest validator 对缺字段、多字段、错误 null、错误 digest、
  editable descriptor、enum/ordinal/order、operation union、Discovery kind/domain/term/behavior
  与 Legacy/Compensation length/order 全部拒绝；
- Bundle fingerprint 在不同进程/对象 identity 下保持一致，但任一 Manifest 字段
  改变都改变 fingerprint；
- 五类 Golden 缺失、自动更新、digest 漂移或生产代码读取 fixture 都使门禁失败。

### 16.2 Provider 与 Selector

Provider 继续比较实际 Adapter 收到的完整 25 envelopes：

- `type/function/strict/name/description/parameters` 全字段；
- 顺序和 Schema fingerprint；
- 无真实网络；
- 3 Legacy 和 4 Compensation 全部缺席。

Selector Golden 至少覆盖：

- 所有 page kind；
- 所有 attachment kind；
- 六个 trusted domain；
- 每条中文/英文 lexical rule；
- 多领域组合和依赖闭包；
- workspace/image/document 零信号全量回退；
- unknown kind/version 与 Selector Exception fail-closed；
- capability 交集破坏闭包；
- Provider fallback Surface/fingerprint 不变；
- 未暴露工具拒绝。

每个场景比较 ordered names、完整 envelopes、domain、closure、fallback 标志和 surface fingerprint。

### 16.3 Capability、Binding 与 HITL

25 工具逐项比较：

- required capability；
- Binding contract 和 resolver descriptor；
- kind、confirmation policy 和 editable fields；
- workspace/global/application/mode Surface；
- capability-first、Binding aggregate 和 scoped Repository；
- approve/modify/reject/stale/CAS/replay；
- chained Pending 和 confirmation continuation。

断言 Provider、resolver、Repository、preflight 和 executor 次数与 baseline 一致。

### 16.4 Ledger 与 Undo

固定并比较：

- 12 个 Typed transactional write；
- 3 个 Legacy deterministic write；
- 4 个 required undo 和 Compensation 映射；
- WriteContract result/visible/transport/undo byte budgets；
- proposed/claimed/committed/failed/rejected；
- response loss、terminal replay、delivery fencing、commit unknown；
- required undo 生成和 Compensation Operation；
- 四个 Primary Undo 的完整 seed/result/payload/datetime/fingerprint/byte-budget Golden；
- caller-owned `tool.started` 原子边界；
- Typed Handle 跨 Segment/Authority/Runner、claim 撤销、字段相同复制品均失败；
- Legacy/Compensation Handle 不含 ORM/Session/Pending/Operation/原始 Undo，跨 parent 或 Adapter identity 失败；
- `models.py` 两个 WriteOperation CHECK 与 baseline 不变且与 Bundle projection 一致；
- 升级前 Typed/Legacy/Compensation terminal row、`conversation_id IS NULL` 兼容行回放 Provider/executor 0；
- operation-bearing Pending 缺失/伪造/跨 Segment route Handle 时 ChatRepository/Operation/executor 0；
- initial/replace/chained/continuation 的 Typed、Legacy 和 clarification route union 矩阵；
- non-terminal Typed/Legacy rehydration、reject 无 prepare、commit-unknown 的
  absent/non-terminal/terminal/unreadable 矩阵；
- chained Typed→Typed、Typed→Legacy、Legacy same-JD→same-JD、Legacy cross-adapter、
  Compensation 组合及 response-loss recovery Golden；
- Legacy 同 Session commit/rollback、两 Session 隔离、隐式 Session 负向门禁、Catalog/
  Adapter mutation/copy/pickle/callable replacement。
- Legacy presentation 的当前 Session/read-only context、跨 Session 拒绝、rollback 和
  confirmation/details/success 兼容输出等价；
- Legacy proof issuer 对 typed/compensation role、unknown source、伪造/替换 args、错误
  proposal/confirmation-token/input/authorization-scope/operation-request fingerprint 或 key ID、
  claim/scope/operation/route mismatch 均不签发 proof，
  Adapter/Repository/executor 为 0；
- `LegacyRouteSourceV1` 五个值及映射逐项 Golden；恢复统一使用
  `confirmation_resume`，四个 direct source 进入 issuer 均拒绝；
- 四个 source-bound `LegacyInitialRouteIssuer` 与 Port/Adapter mapping 逐项 Golden；
  `jd_clarification` 与 `jd_deterministic_action` 虽映射同一 Adapter，issuer identity 仍不同；
  issuer API 不接受 source/name/dict/Pending，`confirmation_resume` 无 issuer；
- 同一 source/issuer 连续两个请求都成功，request lease 与 token identity 均不同；消费或撤销
  前一 token 不影响 issuer 复用及后一请求。两个并发请求使用同一 issuer 时 token 也必须
  不同，且各自在自身 lease 内独立收敛；
- A issuer + B source lease、A issuer + B issuer 私有 lease factory、同一 container/
  Registry/Port 下跨 source 组合全部拒绝；尤其覆盖映射到同一 Adapter 的
  `jd_clarification` 与 `jd_deterministic_action` 交叉 lease/issuer，失败时 token 为 0；
- `LegacyInitialRoutePort` 对错误 issuer/lease/token/container/Port/Registry、普通
  string/dict/Pending/tool name、伪造/复制/重复/跨请求 token 和关闭 lease 的负向矩阵；
  失败时 Pending/Operation/Adapter/executor 为 0；同一 token 并发仅一个 consume winner；
- 正常结束、Pending persistence 成功后的请求退出、Pending persistence 失败、Exception、
  Cancellation、其他 `BaseException` 均关闭 request lease 并 revoke 其 token/handle；不同
  lease 的 token 不被连带撤销，Provider Surface 永远不出现 Legacy；
- issue/resolve/close 三方 barrier 覆盖 close-first 与 resolve-first；close 后不能得到 live
  handle，resolved handle 会在 close 时撤销，重复 finalizer 绝对 no-op，旧 token/handle
  identity 不得通过新 lease 形成 ABA；
- caller 伪造/复制 `LockedLegacyRouteEvidence`、错误 verifier instance token、错误 Session
  或 issuance lease 时 proof 注册为 0；Issuer API 不接受 tool name、persisted raw args、
  caller digest/evidence，只接受 exact validated confirmation input；
- `LegacyApprovedConfirmationInput` 覆盖 operation ID present/missing、edited args
  missing/empty/non-empty object、显式 null 422、token mismatch 和 rejection fields 拒绝；
  operation ID 缺失只从 live Conversation Pending 引导，Pending 已清理的 replay 不猜测；
- approve/modify 精确顺序为 read-only preparation binding → prepare/validate →
  `BEGIN IMMEDIATE` → mutable recheck → claim CAS → execution proof → Catalog consume →
  executor；claim 前对象不得取得 execute capability，claim/proof/Catalog 任一步失败时
  executor 为 0；reject/terminal replay 的 preparation/proof/Catalog 均为 0；
- proposed Legacy fingerprint 真值表逐字段覆盖：proposal/token 非空，authorization/input/
  operation-request 为 `NULL`，claim 前为空、claim 后为 operation ID + timestamp；proof
  绑定计算出的 effective input/request fingerprint，但失败 rollback 后行仍为 proposed；
- rejected terminal 的 input 保持 `NULL`、request 非空；committed/failed terminal 的
  input/request 均非空；三类 Legacy authorization scope 均为 `NULL`，replay 不签发 proof；
- Legacy args Golden 覆盖无编辑 raw encoding、有编辑 compact encoding、语义 canonical、
  重复键 baseline 行为、scalar/非法 JSON/非有限数/Adapter validation failure，以及原始
  pending digest 与 effective input fingerprint 不得互换；
- Catalog 对 raw dict、tool-name-only Protocol、直接构造的 exact-type proof、同字段 clone、
  copy/deepcopy/pickle、错误 issuer/Catalog/Bundle/transaction/claim、expired/revoked/reused
  proof 全部拒绝，且 proof 只能由一次 `issued → resolved` CAS winner 消费；
- commit/rollback/Exception/Cancellation/其他 `BaseException` 均撤销 transaction lease 下
  proof 与派生 Handle；late/reused Handle 不能进入 Repository 或 executor；
- preparation attempt 精确覆盖 `open → prepared → consumed|revoked`、并发单 winner、claim/
  proof 失败与所有退出路径 revoke；live Binding capability 在 prepare 后消失，identity
  tombstone 保留到 attempt 结束；tombstone 被替换/提前删除或下一请求复用旧 PreparedCall
  时 proof 为 0；
- confirmation input、PreparationBinding、PreparedCall、Locked Evidence、所有 read/
  preparation/issuance/claim lease、proof/registry/Handle 的 repr、日志、copy/deepcopy/pickle/
  generic serialization 均不含或泄漏 effective args、token、HMAC key/digest、Pending/
  Operation identity 或用户内容，且不会进入任何持久/transport State；
- spy 证明只有 Preparation Registry 短暂持有 effective args，request/transaction 退出后
  清零；Proof Registry/Catalog/route handle 从未接收 PreparedCall 对象或 raw/effective args；
- Legacy route identity 覆盖 unclaimed、claim 中、claim 清除、Pending 替换、
  scope revision 变化和 stale resume；空 claim/nullable timestamp 及 UTC 微秒 canonical
  Golden 必须字节稳定。

必须证明 Ledger/Repository 已消费 Operation Port，而不是仍引用旧集合。

### 16.5 Agent、HTTP/SSE、Journal

复用现有 Golden 覆盖：

- new turn；
- read+read、write+read、read+write、write+write；
- read failure 后的后续 read 行为；
- sync、SSE 和 Provider fallback；
- Pending、approve、modify、reject、chained Pending；
- deterministic action；
- terminal replay、delivery recovery、timeout 和 disconnect；
- Provider/Tool 调用次数和业务写入；
- `tool.proposed → tool.started → completed|failed` Journal 时序；
- Trace healthy/degraded 与 integrity anomalies。

Metadata Bundle 不得出现在 checkpoint、ChatMessage、Pending、Ledger payload、Journal、Prompt、HTTP/SSE 或通用序列化中。

另外单独验证 Phase 1 Journal `_TOOL_NAMES` 白名单保持 25+3，不由 Bundle 动态改写；
Legacy editable fields 只来自 Adapter，不再存在 deterministic bridge duplicate。
同时用 AST 证明 `_pending_action_details/_undo_seed_for_pending/_build_write_undo/
_chained_adapter_kind` 及 service/write-operations 的同类 tool-name dispatch 已删除；
允许的 terminal renderer 只读持久 payload，不能进入分类/授权/执行路径。

### 16.6 发布级矩阵

完成前至少执行：

```text
Tool Pipeline focused suite
Tool Authority focused suite
Context Projector focused suite
Agent Loop focused suite
Pilot Runtime focused suite
Write Operation / Chat API regression
全量 pytest
Ruff
Mypy
前端 test/build
static smoke
local verify
controlled real-AI verify
浏览器 workspace/application sync + SSE + HITL 闭环
```

同时要求：

- 后端分组 manifest、并集、重复 node ID、skip 和 aggregate 精确覆盖；
- 独立 CR 无开放 P0/P1/P2；
- baseline allowlist、未跟踪文件、`git diff --check` 和 worktree clean；
- Docker、Application-JD 外置门禁或其他环境限制未执行时如实记录，不伪造通过。

## 17. 完成声明与后续项目

只有以下条件同时满足才可宣称本项目完成：

```text
Typed Catalog = exact 25
Legacy Boundary = exact 3
Compensation Registry = exact 4
Provider Golden = unchanged
Discovery Golden = unchanged
Capability/Binding Matrix = unchanged
HITL/Ledger/Undo = unchanged
HTTP/SSE/Journal = unchanged
Old Python runtime classification maps/sets/fallback = deleted（历史 CHECK/Journal 白名单除外）
Typed Provider/Selector/Authority/Pipeline/Ledger/Undo = same frozen Bundle views
Legacy Adapters / Compensation Handlers = exact-bound, type-isolated
Primary Undo Builders / Pending Route Handles / chained topology = exact-bound, no name switch
Legacy execution = caller-owned Session, no captured Repository or per-call Catalog rebuild
```

本期只能声明：

> 25 个模型可见 Typed Tool 的 Provider、Discovery、Dependency、Capability、Binding、Confirmation、Ledger 和 Undo 静态事实已收敛到唯一冻结 Metadata Bundle；3 个 Legacy deterministic Tool 与 4 个 Compensation Operation 继续类型隔离；外部协议和业务副作用保持既定兼容边界。

不得声明：

- Legacy 已迁移为 Typed Tool；
- Entity Binding、RBAC 或权限模型得到新增强化；
- 已支持动态插件或运行时注册工具；
- 已改变 Context Projector 的语义选择策略；
- 已提供全局 exactly-once。

本项目验收并合并后，再独立设计后续项目。候选包括 Compact Confirmation / `ActionPresentationPolicy`、Persistent Resume Scope、Runtime Query/SSE Replay 和 Wakeup Queue；不得提前混入本期。
