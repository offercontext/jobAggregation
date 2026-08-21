# Pilot Runtime Orchestration Extraction 设计

状态：已复审通过

固定 baseline：`b05d915bbb52b2740f6801b4ec46ee8f4ccda2e2`

目标分支：`refactor/20260821-pilot-runtime-orchestration`

## 1. 背景、目标与边界

### 1.1 当前问题

Durable Execution Journal、Tool Execution Pipeline、Write Operation Ledger、Context
Projector 和 Journal Active Work Budget V2 已经形成稳定的可靠性原语，但四条 Chat
入口仍分别编排这些能力：

```text
POST /api/chat
POST /api/chat/stream
POST /api/chat/confirm
POST /api/chat/confirm/stream
```

当前 `api.py` 同时决定：

- Conversation 创建和读取；
- Pending Action 路由；
- 确定性 Pilot Action 路由；
- Run / Segment 生命周期；
- Context Source 加载和 Model Surface 投影；
- Agent 初始运行和确认 continuation；
- Write Operation claim、执行、回放、delivery fencing 和 fallback；
- ChatMessage、Pending、clarification 持久化；
- timeout、cancel 和错误收敛；
- HTTP / SSE 投影；
- 标题生成资格信号。

这些路径虽然已有大量行为测试，但因果流程仍由 Route 与闭包拼接。新增一种 Transport、
改变错误处理或接入一项 Runtime 原语时，需要同步修改多条路径，容易产生隐式漂移。

### 1.2 本期目标

本期只进行内部破坏性编排提取：

```text
FastAPI Route
  → 请求语法校验与身份入口
  → PilotRuntime
  → RuntimeOutcome / RuntimeEvent
  → HTTP 或 SSE Transport Adapter
```

完成后：

- 四条 Chat 路径共享一个正式 Application Service；
- Runtime 成为 Journal、Projector、Agent、Tool、Pending、Ledger 和消息持久化的唯一编排者；
- Route 不再直接组织模型运行、确认 claim、delivery 或 Journal disposition；
- sync 与 stream 使用同一运行状态机，只保留交付方式差异；
- 现有 LangGraph Agent Loop 保持不变；
- 现有确定性 Pilot Action 与三个 Legacy deterministic 工具继续保持显式隔离；
- 后续 Agent Loop Unification 可以只替换 Runtime 内部 Agent Driver，不再修改四条 Route。

### 1.3 外部兼容契约

本期对外严格兼容：

- 四个 HTTP endpoint、请求字段、状态码和响应字段不变；
- SSE event 名称、payload、`seq`、顺序、终止和断开语义不变；
- Provider 候选链、工具 Schema、调用顺序、调用次数和 fallback 规则不变；
- 25 个模型可见工具与 3 个 Legacy deterministic 工具边界不变；
- HITL、Pending Action、CAS、Ledger、delivery lease/fencing、Undo/Compensation 不变；
- Conversation、ChatMessage、Journal、Ledger 和领域数据库 Schema 不变；
- Context Surface、Manifest、fingerprint、预算和候选选择规则不变；
- Journal 继续 fail-open，Ledger 与业务写入继续 fail-closed；
- terminal replay、delivery recovery/fallback、拒绝和确定性动作的 Provider 0 语义不变；
- 不新增隐式重试、fallback、shadow execution、双写或旧路径回退。

允许的内部破坏性变化：

- 删除 Route 内旧编排闭包和重复分支；
- 移动私有 helper，并同步迁移内部调用方和测试；
- 用封闭 DTO、Outcome 和 Event 代替 Route 内松散字典；
- 将依赖装配集中到 Runtime composition root。

### 1.4 非目标

本期明确不做：

- 不移除或重写 LangGraph；
- 不统一首次运行和 `_resume_without_checkpoint` 的 Agent Loop；
- 不实施更严格的 Scoped Capability 或 Binding Enforcement；
- 不收敛 Tool Metadata；
- 不启用 Memory、Knowledge、Summary Contributor；
- 不修改 Context Projector 的选择、预算或 Tool Surface 策略；
- 不迁移三个 Legacy deterministic 工具到模型 Tool Pipeline；
- 不增加 SSE replay、持久事件队列或后台 Wakeup lease；
- 不增加 UI、API 或数据库迁移；
- 不拆分与 Chat Runtime 无关的 API；
- 不引入多 Agent、插件系统或任意代码执行；
- 不声明跨外部系统 exactly-once。

本期保留 Phase 3 的准确保证：相同 Operation 的领域事务至多提交一次、terminal
replay 不重跑 executor、delivery generation/owner fencing、结果未知对账与显式
compensation。

## 2. 模块边界与契约

### 2.1 目标模块

```text
src/offerpilot/pilot_runtime/
├── contracts.py       # 请求、Outcome、Event、取消与 Transport 上下文
├── service.py         # 唯一 Chat Runtime 状态机
├── continuation.py    # Pending/Ledger confirmation coordination
├── persistence.py     # Chat/Pending/clarification 持久化编排
├── event_sink.py      # RuntimeEventSink 与安全 emit 规则
├── deterministic.py   # 现有确定性 Pilot Action 显式桥接
├── composition.py     # 依赖装配；不读取 FastAPI 请求
└── errors.py          # 封闭 Runtime failure 分类

src/offerpilot/chat_transport.py
└── FastAPI HTTP/SSE renderer、AgentExecutionHost、PreparedStreamGuard
```

模块可以在实施中按依赖图合并小文件，但不得把编排重新塞回 `api.py` 或
`ai/agent.py`。

依赖方向固定为：

```text
api.py / transport adapters
        ↓
pilot_runtime contracts + service
        ↓
agent_runtime / context_projector / ai agent
tool_runtime / ledger / repositories
```

下层模块不得导入 `pilot_runtime`。`pilot_runtime` 不得导入 FastAPI、`JSONResponse`、
`StreamingResponse`、`BackgroundTasks` 或 SSE 字符串编码器。

### 2.2 同步 Application Service

当前 Agent、Repository、SQLite 和 Provider 边界均以同步调用为主。为了避免本期改变
线程、timeout 和 cancellation 语义，核心 Runtime 保持同步：

```python
class PilotRuntime:
    def start_turn(
        self,
        request: StartTurnRequest,
        *,
        transport: RuntimeTransportContext,
        event_sink: RuntimeEventSink,
        signal_sink: RuntimeSignalSink[str] | None,
        execution_host: AgentExecutionHost,
        invocation_control: RuntimeInvocationControl,
        cancel_check: Callable[[], bool],
    ) -> RuntimeOutcome: ...

    def continue_confirmation(
        self,
        request: ConfirmationRequest,
        *,
        transport: RuntimeTransportContext,
        event_sink: RuntimeEventSink,
        execution_host: AgentExecutionHost,
        invocation_control: RuntimeInvocationControl,
        cancel_check: Callable[[], bool],
    ) -> RuntimeOutcome: ...

    def prepare_stream(
        self,
        request: StartTurnRequest | ConfirmationRequest,
        *,
        transport: RuntimeTransportContext,
        invocation_control: RuntimeInvocationControl,
    ) -> ImmediateHttpOutcome | PreparedStreamExecution: ...

    def execute_prepared_stream(
        self,
        prepared: PreparedStreamExecution,
        *,
        event_sink: RuntimeEventSink,
        signal_sink: RuntimeSignalSink[str] | None,
        execution_host: AgentExecutionHost,
        cancel_check: Callable[[], bool],
    ) -> RuntimeOutcome: ...
```

Runtime 本身在 Transport owner 的调用栈上执行；到达现有 Agent 调用阶段时，Runtime 将
一次性的 Agent thunk 交给 Transport-owned `AgentExecutionHost`。该 Host 唯一创建和管理
当前 timeout worker、Future、cancel Event 以及 SSE 的无界 `Queue()`。Source Loader、
precheck、消息持久化和其他 baseline 不计入 Agent timeout 的阶段不得被移入 worker。

SSE adapter 先在返回响应头前同步调用 `prepare_stream()`，只有得到
`PreparedStreamExecution` 才构造 StreamingResponse；其 generator 再调用
`execute_prepared_stream()`，Runtime 到达 Agent 阶段后才让 Host 创建 worker。不得在本期
将同步 SQLite/Provider 调用包装成表面 `async`，也不得依赖 `asyncio.wait_for()` 或
`Future.cancel()` 强制中断已经运行的底层线程。

`AgentExecutionHost` 是 Transport 注入的窄能力，不包含 Repository、Journal、Pending 或
Ledger 方法。它只能：

```text
执行一个 Runtime 提供的 Agent thunk
转发 Agent runtime events
实施 baseline Agent deadline/poll/cancel
返回 AgentTurnResult 或抛出封闭 timeout/control exception
```

Runtime 仍然是阶段顺序和业务结果的唯一编排者；Host 不解析 ToolMessage、Outcome、SSE
字符串或业务异常。

### 2.3 输入 DTO

`StartTurnRequest` 只接收 Route 已完成语法规范化的值：

```text
message
conversation_id | new conversation descriptor
mode
context_type / context_ref
validated page context
validated request attachments
validated optional pilot action
```

`ConfirmationRequest` 只接收安全控制字段：

```text
conversation_id
approved
confirmation_token
operation_id（按现有兼容规则可选或必需）
edited_args：missing / empty object / non-empty object
rejection_feedback
```

显式 `edited_args=null` 继续按现有 API 返回 422。Route 只做 JSON 形状、标量类型、
长度和字段存在性校验；Pending/Ledger 身份、token、operation、tool、revision、有效参数、
归属和业务状态验证全部由 Runtime 使用服务端事实完成。

DTO 不得携带 FastAPI 对象、ORM、Session、Repository、密钥或未验证的执行对象。

### 2.4 Transport 上下文

```text
RuntimeTransportContext
- mode: sync | stream
- transport_run_id: UUID | null
- stream_version: 现有固定值 | null
```

SSE adapter 在调用 Runtime 前创建 transport run identity；sync 使用 `null`。该身份只用于
现有 SSE 与 Journal transport 关联，不替代 Durable Run、Segment 或 Ledger Operation ID。

### 2.5 封闭 Outcome

Runtime 只返回封闭 Outcome，不返回 `JSONResponse` 或已编码 SSE：

```text
MessageOutcome
ConfirmationRequiredOutcome
RuntimeFailureOutcome
OperationPendingOutcome
OperationReplayOutcome
```

每个 Outcome 只包含生成现有 HTTP/SSE 响应所需的安全字段。HTTP status、错误 code、
retryable、degraded 和现有中文文案的映射是只读兼容表，不允许 Transport 自行推断异常。

Runtime 内部可以使用更细的失败原因，但不得把异常对象、原始异常文本、密钥、参数或工具
结果放入 Outcome、日志、Journal 或 SSE。

SSE 准备阶段另外使用两个封闭瞬态类型：

```text
ImmediateHttpOutcome
- status_code
- safe response payload

PreparedStreamExecution
- invocation_id
- preparation_kind: model | deterministic_initial | deterministic_confirmation | confirmation | replay
- execution_mode: direct | agent_host
- opaque prepared state（repr=False）
- lifecycle_state: prepared | executing | aborted | completed
- completion_reason: null | normal | cancelled | transport_aborted
```

`completion_reason` 仅在 `lifecycle_state=completed` 时为非 `null`。它是瞬态安全枚举，不进入
ChatMessage、Pending、Ledger、Journal、Trace、日志或 SSE；`aborted` 不设置 completion
reason，因为它专指执行开始前终止。

`ImmediateHttpOutcome` 只表示 baseline 本来会在 `StreamingResponse` 创建前直接返回的
HTTP 结果。`PreparedStreamExecution` 不进入 ChatMessage、Pending、Ledger、Journal、Graph
State 或 checkpoint，不可通用序列化，也不得携带活跃 ORM/Session。它可以持有 Runtime
拥有的不可变 DTO、RunRecorder、一次性控制 token 和已经冻结的 Source；敏感字段必须
`repr=False`。

`PreparedStreamExecution` 只能由创建它的 `PilotRuntime` 执行一次。Transport 不直接操作
其状态，而是使用 `PreparedStreamGuard` 调用 Runtime 的 begin/abort/finish transition。
abort 不撤销 prepare 阶段已经提交的事实，也不重做任何操作；它只阻止新增 Provider、Tool
和领域副作用，并按 preparation kind 收敛仍由本次调用持有的 Journal/delivery 资源。

### 2.6 Runtime Event Sink

```python
class RuntimeEventSink(Protocol):
    def emit(self, event: RuntimeEvent) -> None: ...
```

`RuntimeEvent` 是封闭联合，覆盖当前 SSE 需要的事件：

```text
meta
user_message_saved
status
assistant_delta
tool_call
tool_result
confirmation_required
assistant_message
error
completed
```

`user_message_saved` 使用独立的 `UserMessageSavedEvent`，payload 封闭为当前兼容字段：

```text
role = "user"
```

它只在 baseline 当前会发送该事件的初始 model/deterministic stream 出现，不得添加到确认
stream、terminal replay 或其他原本没有该事件的路径。

规则：

- Runtime 生成逻辑事件，不编码 `text/event-stream`；
- SSE adapter 唯一分配并编码现有 `seq`；
- Sync sink 可以忽略中间事件，但不得改变 Runtime 执行；
- Event Sink 交付失败统一转换为 `RuntimeTransportAborted` 控制异常；
- `RuntimeTransportAborted` 与 `RuntimeCancelled` 必须先于通用 `Exception` 被处理，清理后
  重新传播给 Transport；
- 控制异常不得生成产品错误、assistant message 或 `RuntimeFailureOutcome`，也不得重跑
  Provider、executor 或 Ledger；
- Journal 不是 Runtime Event Sink，继续由现有 RunRecorder 接点独立记录；
- 不允许从 SSE 字符串反向解析 Runtime 状态；
- sync 与 stream 的 Runtime Outcome 必须来自同一状态机，而不是从事件重新拼装业务结果。

`RuntimeCancelled` 与 `RuntimeTransportAborted` 是 `Exception` 的封闭 final 子类，只用于
当前调用栈控制，不可序列化、不可持久化、`repr` 不含原因正文。Transport 设置
`RuntimeInvocationControl` 的取消原因后，Runtime 的 `cancel_check` wrapper 必须据此抛出
对应类型：用户断开/显式取消为 `RuntimeCancelled`，Event consumer/response renderer 已
不可继续为 `RuntimeTransportAborted`。二者不得依赖解析异常文本来分类。

### 2.7 标题资格信号

标题生成继续遵守 Context Projector 阶段冻结的边界：

- 首个完整有效 Agent 模型响应后才具备资格；
- Source Loader/Projection/preflight 在首次模型调用前失败时，请求所属全部 Provider 调用为 0；
- `RuntimeSignalSink` 容量 1、非阻塞、fail-open；
- Runner/Runtime 不持有 FastAPI `BackgroundTasks`；
- sync/SSE Transport owner 在唯一 finalizer 中 drain/close 一次并决定是否注册标题任务；
- 注册失败不改变 Agent Outcome，也不得重试；
- terminal replay、delivery fallback、拒绝和确定性动作不生成标题资格。

## 3. 统一 Runtime 状态机

### 3.1 Start Turn

不存在一条适用于所有入口的严格线性顺序。Runtime 使用同一封闭状态机，但按
`transport.mode + preparation_kind` 选择已冻结的阶段表；表内顺序是兼容契约：

```text
sync model
  validate normalized request
  → create/load Conversation
  → trusted route selection
  → live Pending guard / model resolution
  → persist current user message
  → start Run / Segment
  → load frozen context sources
  → assemble runtime messages / capture initial context
  → AgentExecutionHost
  → normalize and persist result
  → finish/suspend Run
  → RuntimeOutcome

stream model（响应头前 prepare）
  validate normalized request
  → create/load Conversation
  → trusted route selection
  → live Pending guard / model resolution
  → persist current user message
  → load frozen context sources
  → assemble runtime messages
  → create transport identity and start Run / Segment
  → capture initial context
  → PreparedStreamExecution
  → meta → user_message_saved → status
  → AgentExecutionHost
  → normalize and persist result
  → finish/suspend Run
  → RuntimeOutcome / completed SSE

deterministic initial direct
  validate normalized request
  → create/load Conversation
  → trusted deterministic match
  → start new Journal Run or resume pending replay（按 baseline）
  → execute existing DeterministicPilotAdapter before response headers
  → preserve its Chat/Pending/CAS/write ordering
  → finish/suspend existing Run
  → ImmediateHttpOutcome or execution_mode=direct stream

deterministic confirmation
  safe control fields
  → load trusted server-side Legacy Pending / closed-name routing
  → token/CAS and existing deterministic confirmation transaction before response headers
  → preserve committed Chat/Pending/Ledger/domain facts
  → ImmediateHttpOutcome or execution_mode=direct stream

confirmation proposed/live
  safe control fields / Ledger-first lookup
  → trusted Pending/token/request identity
  → resume Durable Run / create confirmation Segment（按 baseline）
  → approve/modify 或 reject 的专用状态机
  → delivery / chained Pending / RuntimeOutcome

terminal replay
  safe control fields / Ledger-first terminal lookup
  → identity/fingerprint/integrity validation
  → Provider = Projector = executor = 0
  → baseline immediate HTTP 或 direct SSE delivery projection
```

`sync model` 与 `stream model` 的 Source/Run 顺序不得互相归一：sync 保持先创建
Run/Segment 再加载 Source；stream 保持先加载 Source 再创建 Run/Segment。每个
preparation kind 都用独立 transition table 和 golden 验证 Journal 事件序列。Route 只选择
Transport mode，不得执行或重排表中阶段。

Route selection 只产生两种可信结果：

```text
model
deterministic
```

它继续使用现有服务端规则和已验证的 `pilot_action`；未知客户端工具名不得进入 Legacy
Adapter。模型路径只能使用 Typed Catalog，未暴露工具仍由当前 Surface Binding 拒绝。

### 3.2 Start Turn 的事实边界

- 新 Conversation 的创建、当前 user ChatMessage 写入顺序与 baseline 一致；
- 已有 Pending 时继续在 Provider 前返回 `pending_confirmation_required`；
- Source Loader 仍使用当前 Session-bound Read UoW、2 秒 deadline 和数据库协调器；
- 一个 Segment 只冻结一次 Context Source；Agent Loop 每个 model call 继续使用当前
  Projector 规则；
- Source/Projection fail-closed 时 Provider、Tool executor、Pending/Ledger/领域写入和
  assistant/tool message 为 0；当前 user ChatMessage 与失败状态按 baseline 保留；
- Agent timeout 继续写入当前固定 timeout assistant message；
- 普通异常只映射当前安全 Provider error，不泄漏原始异常；
- Journal 任意失败不改变这些结果。

### 3.3 Agent Driver 边界

本期引入窄协议，适配当前 `LangGraphAgentRunner`：

```python
class AgentDriver(Protocol):
    def run_turn(...) -> AgentTurnResult: ...
    def resume_after_confirm(...) -> AgentTurnResult: ...
```

现有 `run_turn()`、`resume_after_confirm()`、`_resume_without_checkpoint`、两节点 Graph、
多 ToolCall 选择和最大迭代规则全部保持原样。Runtime 不读取 Graph State，也不复制 Agent
Loop。

这保证下一期 Agent Loop Unification 只需替换 `AgentDriver` 实现。

### 3.4 Pending 分支

Agent 返回 Pending 时：

```text
完整 added messages
→ missing target question 检查
→ 有缺失：持久化 clarification 结果并完成 Run
→ 无缺失：原子持久化 ToolCall messages + Pending
→ Journal suspend
→ ConfirmationRequiredOutcome
```

必须保留：

- assistant ToolCall、ToolMessage、确认结果的原子组；
- archived Conversation 的 409；
- 同一 Conversation 只能有一个 live Pending；
- chained Pending 原子替换旧 Pending；
- Pending、Operation、run/segment、tool_call_id 的既有关联；
- Provider/工具调用次数不因 Sink 或持久化投影失败而增加。

### 3.5 Confirmation 入口顺序

固定为 Ledger-first：

```text
解析安全控制字段
→ 按 operation_id 查询 Ledger（若请求契约提供）
→ terminal：校验身份、request fingerprint 和完整性后回放
→ proposed/live：读取服务端 Pending
→ token/Pending/Ledger identity 验证
→ 恢复原 Durable Run，创建 confirmation Segment
→ approve/modify 或 reject
```

terminal replay：

- Provider 0；
- Projector 0；
- executor 0；
- 不依赖已清除 Pending；
- 只执行既有 delivery 对账和响应投影；
- 不创建新的模型事件或 Surface。

### 3.6 Approve / Modify

```text
重建 effective Pending
→ prepare_call
→ BEGIN IMMEDIATE
→ 锁内 mutable recheck
→ Pending claim / Ledger authorization
→ tool.started（caller-owned Session 窄例外）
→ executor 最多一次
→ terminal Ledger commit
→ 获得 delivery ownership + heartbeat
→ continuation source load + unchanged Agent resume
→ 原子 delivery commit
```

保持现有约束：

- confirmation claim 在 executor 前；
- claim/match 失败 executor 为 0；
- 单次执行调用内 executor 最多一次；
- commit unknown 使用 fresh Session 对账；
- 基础设施/未映射内部异常不伪造确定性 terminal failure；
- required undo、terminal payload digest 和聚合上限不变；
- owning request 只启动一次 continuation，但 continuation 内 Provider/read-tool loop 保持
  baseline；
- continuation 可以生成最终文本或新的 chained Pending；
- confirmation continuation 在领域事务提交后重新加载一次冻结 Source；
- delivery heartbeat 覆盖 Loader、Projector、Provider 和 read-tool loop。

### 3.7 Reject

拒绝路径固定为：

```text
token/Pending/Ledger identity
→ rejection CAS
→ terminal rejected commit
→ deterministic compatibility result
→ atomic delivery
```

拒绝不得运行：

- Schema/Args decode；
- capability；
- binding resolver；
- 目标 Repository；
- preflight；
- Context Loader/Projector；
- Provider；
- executor。

`rejection_feedback` 继续进入既有 request fingerprint 和兼容 ToolMessage，但不得进入
Journal/日志。拒绝的 HTTP/SSE 用户反馈语义以 baseline golden 为准。

### 3.8 Deterministic 路径

确定性 Pilot Action 与三个 Legacy deterministic 工具通过
`DeterministicPilotAdapter` 进入同一 Runtime 外壳：

```text
trusted deterministic route
→ existing specialized Pending / confirmation / CAS / write / recovery
→ common RuntimeOutcome
```

它们：

- 不进入模型 Typed Catalog 或 Provider Surface；
- 不调用 Context Projector 或 Provider；
- 不改变专用确认、幂等、CAS、写入和恢复；
- 继续记录既有 Journal route/run/segment；
- 不因编排提取被隐式迁移为普通 ToolSpec。

本期允许桥接现有确定性实现，但 Route 不得继续直接调用该实现。桥接是唯一明确兼容边界，
不得成为模型路径失败时的 fallback。

## 4. Transport、超时与取消

### 4.1 Sync Adapter

sync Route 只负责：

```text
normalize payload
→ build DTO
→ create SyncResultSink + title signal
→ invoke PilotRuntime on request owner
→ Runtime 在 Agent 阶段调用 SyncAgentExecutionHost
→ drain/close title signal exactly once
→ render RuntimeOutcome to JSONResponse
```

Agent 阶段的 outer executor、Future、deadline、cancel event 和 title signal finalizer 归
sync Transport owner。Source load 和 Agent 返回后的 persistence 继续位于 Agent timeout
之外。Runtime 通过 `RuntimeInvocationControl` 拥有 timeout/cancel 对业务状态的收敛规则。
Host 到达 deadline 后只调用一次 control transition 并把封闭 timeout 结果交回 Runtime；
不得自行写 ChatMessage、Pending、Ledger 或 Journal。Transport renderer 失败不得触发第二次
Runtime 调用。

### 4.2 SSE Adapter

SSE Route 只负责：

```text
normalize payload
→ build DTO + SseRun transport identity
→ PilotRuntime.prepare_stream()（响应头发送前，同步）
→ ImmediateHttpOutcome：直接渲染 HTTP
  | PreparedStreamExecution：构造 StreamingResponse
→ generator 调用 PilotRuntime.execute_prepared_stream()
→ execution_mode=direct：按 baseline 直接投影预计算 SSE
  | execution_mode=agent_host：Runtime 在 Agent 阶段调用 SseAgentExecutionHost
    → Host 创建一个 Agent worker 并消费 baseline unbounded RuntimeEvent Queue
→ assign seq and encode existing SSE
→ drain/close title signal exactly once
→ propagate disconnect cancellation
```

#### 4.2.1 响应头前准备阶段

`prepare_stream()` 必须在构造/返回 `StreamingResponse` 前完成 baseline 当前位于该边界前的
全部判断和副作用，包括：

- Conversation 创建/读取和 live Pending guard；
- 当前模型配置是否可用；
- 初始 stream 的 user message 写入与 Context Source 加载；
- deterministic route 的匹配、执行和直接 JSON 错误；
- confirmation 的 conversation、token、operation、Ledger、edited args 和 Pending 身份检查；
- terminal replay/delivery 对账中 baseline 会直接返回的结果；
- 创建或恢复 Journal Run/Segment 的 baseline 对应部分。

准备结果固定为：

```text
baseline 在响应头前返回 4xx/5xx JSON
→ ImmediateHttpOutcome
→ worker = 0
→ event channel = 0

baseline 会进入 SSE
→ PreparedStreamExecution
→ Transport 才返回 StreamingResponse
→ 只有 baseline 原本使用 Agent worker 且执行到 Agent 阶段才启动 worker
```

如果 deterministic 或 replay 已经在准备阶段得到最终业务结果，但 baseline 仍以 SSE 交付，
则 `PreparedStreamExecution` 保存安全的预计算 Outcome，并使用 `execution_mode=direct`
投影既有 SSE，不创建 Agent worker，也不再次执行 deterministic action、Ledger replay、
Provider 或 Tool。

准备阶段必须保留 baseline 的准确顺序。例如当前初始 stream 在 Source Loader 成功后才创建
对应 Run/Segment 时，不得因抽象统一而提前创建；sync 路径也不得被迫改成 stream 的顺序。
所有顺序差异由一个 Runtime 状态机中的显式 preparation kind 表达，不允许 Route 重新编排。

`abort_before_start()` 的作用不是回滚整个请求，而是终止尚未开始的 SSE 交付。按
`preparation_kind` 固定：

```text
model
  保留 prepare 阶段已写入的 user ChatMessage
  若 Run/Segment 已创建，则按 baseline 停止/abandon；Journal 失败仍 fail-open
  assistant/tool message、Pending、Ledger 和领域写入不新增

deterministic initial
  保留已经创建的 Conversation/ChatMessage/Pending 或确定性结果
  已 finish/suspend 的 Run 绝对 no-op；仍 open 的 Run 才执行一次安全收敛
  不撤销、不重做 deterministic action

deterministic confirmation
  保留已经提交的 Ledger/领域终态、Pending CAS 与消息事实
  不执行 Undo，不重新确认，不再次 delivery
  仅释放/收敛仍归本次 invocation 持有的 lease 与 Journal disposition

terminal replay
  Provider = Projector = executor = 0
  保持已存在 terminal fact，不重新回放或写 fallback
  只终止本次尚未开始的 delivery attempt，并服从 generation/owner fencing
```

所有类型中，abort 自身不得产生额外 Provider、Tool executor 或领域写入；它可以执行既有
Journal fail-open disposition、释放 lease，或按 Phase 3 协议放弃未完成的 delivery ownership。
已完成/已释放资源必须绝对 no-op。

#### 4.2.2 PreparedStreamGuard

普通 generator 从未开始迭代时不会执行 generator body 的 `finally`，因此 Transport 必须使用
`PreparedStreamGuard`，不能只依赖生成器清理：

```text
PreparedStreamGuard（Transport-owned）
- prepared execution handle（repr=False）
- shared Runtime CAS reference
- response_started flag
- finalizer_registered flag
```

生命周期固定为：

```text
prepare_stream() returns PreparedStreamExecution
→ construct guard
→ construct GuardedStreamingResponse in try
   → construction raises: guard.abort_if_prepared()
   → construction succeeds: register response finalizer
→ ASGI response __call__ starts
   → body iterator first entry: guard.begin_execution()
      prepared → executing winner 才能调用 execute_prepared_stream()
→ body iterator / ASGI response finally
   → executing: signal cancel/finish，并且只按 baseline 规则等待/清理
   → prepared: guard.abort_if_prepared()
→ response background/finalizer
   → 再执行一次 guard.abort_if_prepared() 作为幂等兜底
```

`GuardedStreamingResponse.__call__` 的 `finally` 是“Response 已构造但 body iterator 从未开始”
场景的权威 hook；BackgroundTask/finalizer 是幂等兜底，不能依赖 `__del__`、垃圾回收时机或
generator 自身 `finally`。guard 的 begin、abort、complete 都委托同一个 Runtime CAS：

```text
prepared → aborted
prepared → executing
executing → completed(reason=normal)
executing → completed(reason=cancelled)
executing → completed(reason=transport_aborted)
```

不允许其他 lifecycle state 或转换：

- `aborted` 只表示 Agent worker/direct SSE 执行开始前终止；
- 执行开始后的用户取消、disconnect 映射为 `completed(reason=cancelled)`；
- Sink、consumer 或 response delivery 中止映射为
  `completed(reason=transport_aborted)`；
- 正常交付映射为 `completed(reason=normal)`；
- 只有 `executing → completed(reason)` 的唯一 CAS winner 执行执行后 cleanup/disposition；
- 只有 `prepared → aborted` 的唯一 CAS winner 执行 before-start cleanup；
- lifecycle 与 completion reason 的 CAS 必须在同一状态锁内原子更新，不能先完成再补原因。

CAS loser 必须绝对 no-op。`begin_execution()` 与 `abort_if_prepared()` 并发时只有一个 winner；
Response 构造异常、零次 body 迭代、立即 disconnect、正常完成、body finalizer、Background
finalizer 和重复 abort 都不得产生第二次 Runtime 执行、cleanup 或 disposition。

#### 4.2.3 Agent Worker 与 Queue

固定规则：

- 一次 Agent 阶段只启动一个 Host-owned Agent worker，与 baseline 的多轮 Agent 调用边界一致；
- Runtime Event channel 必须继续使用 baseline 的无界 `Queue()`；本期不引入 capacity、
  backpressure、丢弃或合并事件；
- Agent worker 使用非阻塞 `Queue.put()`/等价无界写入，不因慢客户端等待消费者；
- poll interval、worker shutdown、timeout 起点和 `cancel_futures` 规则保持 baseline；
- `seq` 只由单一 SSE owner 单调分配；
- worker 不直接 yield FastAPI response bytes；
- Runtime 不持有 Request/Response/BackgroundTasks；
- 客户端断开触发 `cancel_check`，不伪装成普通产品失败；
- cancel 后不得继续产生可交付 Provider/Tool 结果；
- 已提交 Ledger terminal 仍由 Phase 3 delivery lease/fencing 收敛；
- late Bundle 继续被 owner token/generation fencing 丢弃；
- SSE emitter/renderer 异常不得重跑 Runtime。

有界队列或 SSE backpressure 属于后续独立行为变更，必须单独设计慢消费者、内存上限、
timeout 和断线语义，不得在本期顺带实施。

### 4.3 Event 顺序

外部 SSE golden 继续以 baseline 为真值。典型模型路径保持：

```text
meta
→ user_message_saved（仅 baseline 初始 stream）
→ status
→ assistant_delta / tool_call / tool_result ...
→ confirmation_required | assistant_message | error
→ completed（仅当前 baseline 允许的终态）
```

确认路径继续遵守 origin `tool_result` 延迟释放、Operation ID 注入和 delivery commit 的当前
顺序。不得为了内部事件模型整洁提前发送尚未持久化或尚未获得 delivery ownership 的结果。

### 4.4 Timeout

本期不改变任何 timeout 常量、起点或所有者。机械固定：

- 普通 sync/stream Agent timeout 与 baseline 一致；
- confirmation timeout 区分未 claim、执行中、terminal 已提交和 delivery pending；
- timeout 后若可安全持久化 fallback，使用现有唯一 delivery transaction；
- operation 已在后台执行时返回既有 `confirmation_in_progress`；
- fresh read 也无法确认结果时返回既有 `operation_result_unknown`；
- timeout/fallback 不产生第二次 Provider、executor 或 continuation。

outer Python thread 不能被 `Future.cancel()` 强制停止。每次调用必须有一个瞬态
`RuntimeInvocationControl`，状态至少为：

```text
active → completed
active → timed_out
active → cancelled
```

AgentExecutionHost 拥有 wall-clock deadline，并且只能请求一次
`active → timed_out/cancelled`。Runtime 拥有 transition 的业务收敛实现。Agent worker 在
Provider/Tool 前后检查状态；Runtime owner 在持久化前和 delivery 前再次检查：

- timeout/cancel winner 已产生后，晚到的非写 Runtime Outcome 直接丢弃，不再写
  assistant/tool message、Pending 或 Journal terminal；
- 已进入 executor/terminal 的写操作继续以 Ledger 和 delivery owner/generation fencing 为
  权威，晚到 Bundle 不能提交 delivery；
- 已经不可中断的 Provider/native 调用允许返回，但返回值只用于清理，不得重新变成响应；
- Transport 不等待迟到结果，也不得启动第二个 Runtime；
- Runtime `finally` 停止 Runtime-owned heartbeat/lease，Host/Transport `finally` 只回收自己的
  Future/executor/channel/signal。

### 4.5 Cancellation 与 BaseException

- 取消和 Transport 中止只有异常传播一种权威表达，不定义 `CancelledOutcome`；
- `RuntimeCancelled` 表示 cancel event/request disconnect；
- `RuntimeTransportAborted` 表示 Event Sink 关闭、consumer 退出、响应投影不可继续或
  Transport adapter 故障；
- `cancel_check()` 和 `event_sink.emit()` 由窄 wrapper 调用。wrapper 把普通 Sink/Transport
  异常转换为 `RuntimeTransportAborted`，但不吞掉 `BaseException`；
- Runtime 必须按 `RuntimeCancelled`、`RuntimeTransportAborted`、已知产品异常、普通
  `Exception`、`BaseException` 的顺序处理；前两者清理后原样传播，绝不进入
  `_ai_provider_error`、`_safe_stream_error` 或产品失败映射；
- Sink 失败后不得尝试向同一 Sink 再发送 `error`/`completed`；
- `ChatRunCancelled` 仅保留在 Agent Driver adapter 内，并在 Runtime 边界转换为
  `RuntimeCancelled`；
- `asyncio.CancelledError`、`KeyboardInterrupt`、`SystemExit` 和其他 `BaseException` 清理后
  原样传播；
- 只有排除上述控制流之后的普通 `Exception` 才能映射封闭安全失败；
- cancellation 必须停止 Runtime-owned delivery heartbeat/lease；outer worker、event
  channel、cancel event 与 title signal 由 Transport finalizer 回收；
- cancellation 不主动生成普通 projection/provider failure；
- Journal cleanup 继续 fail-open，不能覆盖原始 BaseException。

## 5. 事务、所有权与副作用

### 5.1 Runtime 负责的副作用

只有 Runtime 可以在四条 Chat 路径中触发：

- user/assistant/tool ChatMessage 写入；
- Pending Action set/claim/replace/clear；
- clarification set/clear；
- Run/Segment start/resume/suspend/finish/abandon；
- Context Source load 与 Agent invocation；
- Write Coordinator primary/rejection/replay/delivery；
- delivery heartbeat start/stop；
- 确定性 Pilot Action 执行。

Route 不得持有这些副作用的 callback 闭包。

### 5.2 Repository Session 边界

本期保留所有既有 Session 所有权：

- Context Loader 继续使用单一 Session-bound read UoW；
- Write Coordinator 继续拥有 primary transaction；
- `tool.started` 继续使用 caller-owned Session 窄例外；
- Chat continuation/delivery 继续使用同一 transaction 原子写入消息、Pending 替换和
  delivery CAS；
- Journal-owned Session 继续遵守 Active Work Budget V2；
- Runtime 不创建跨领域的通用大事务；
- 不将 HTTP/SSE 交付纳入数据库事务。

### 5.3 一次性所有权

所有权固定拆分为：

```text
Transport owner
- outer executor / Future / worker thread handle
- baseline unbounded Runtime Event Queue
- cancel Event 与 wall-clock deadline
- SSE seq / response encoding / consumer lifecycle
- PreparedStreamGuard / GuardedStreamingResponse / response finalizer
- RuntimeSignalSink 的 drain / close / title registration finalizer

Runtime owner
- PreparedStreamExecution 的一次性状态
- RuntimeInvocationControl 的业务收敛 transition
- Run / Segment disposition
- Context Source UoW 与冻结 DTO
- Pending / Ledger coordination
- delivery heartbeat / ownership token / generation
- confirmation attempt state
- Agent Driver 调用期间创建的非线程业务资源
```

Runtime 不拥有承载自身的 outer worker，也不得在自身 `finally` 中 shutdown 该 executor 或
关闭 Event Queue。Transport 不得直接 finish/abandon Journal、停止 delivery heartbeat、写
fallback 或修改 Pending/Ledger；它只能触发 Runtime 提供的一次性 timeout/cancel/abort
transition。

同一资源不得同时由 Transport 与 Runtime finalizer。所有 cleanup 必须满足：

- 普通异常不覆盖更早的业务/取消异常；
- cleanup 失败不触发 Provider/executor 重跑；
- Journal cleanup 失败只增加安全诊断；
- delivery fencing 的数据库结果继续权威于内存状态。

Transport timeout 或 disconnect 后，即使 `Future.cancel()` 返回 false，也立即关闭本次交付
资格。Agent worker 的迟到返回先检查 `RuntimeInvocationControl`，不能写响应或消息；写
Operation 的迟到结果还必须通过既有 delivery generation/owner token CAS。Transport 丢弃
任何迟到 Outcome，且只执行一次 title signal finalizer。

### 5.4 依赖冻结

每次 Runtime invocation 在入口冻结本次依赖视图：

- 当前 Provider execution chain 仍由 Context Projector/Agent Gateway 按现有 Segment
  规则冻结；
- 当前 Tool Catalog 使用唯一 `MODEL_TOOL_CATALOG`；
- 当前 config 中会影响运行的值在 invocation 开始读取一次，除非 baseline 明确按 model
  call 读取；
- Repository、Coordinator、Clock 和 ID factory 由 composition 注入；
- Runtime 执行中不得重新调用 `create_app()` 或读取 Route 闭包状态。

密钥和 credential handle 仍只存在于瞬态 Provider chain，`repr=False`，不得进入 Outcome、
Event、Journal、Trace 或日志。

## 6. 迁移策略、机械门禁与验收

### 6.1 一次性内部切换

实施采用 characterization-first，但生产切换必须一次完成：

1. 从 baseline 捕获四条入口的合成行为 golden；
2. 建立 contracts、Outcome、Event 和纯 renderer；
3. 提取确定性桥接、persistence 和 continuation coordinator；
4. 建立 PilotRuntime，共用现有 Agent Driver；
5. 同一批将四条 Route 切换到 Runtime；
6. 删除 Route 内旧编排、闭包和重复 fallback；
7. 运行完整兼容、并发、隐私和发布门禁。

禁止：

- feature flag；
- 新旧 Runtime 双轨；
- shadow execution/write；
- 失败时回退旧 Route；
- sync 先迁移、stream 长期保留旧路径；
- 从兼容字符串或 SSE payload 反向恢复 typed state。

### 6.2 Baseline golden

Golden 必须从固定 `b05d915` 独立捕获并作为只读合成资产提交；测试不得自动更新、覆盖或
接受新结果。至少覆盖：

- 四个 endpoint 的 HTTP/SSE 完整响应；
- 初始 model 与 deterministic SSE 均严格为
  `meta → user_message_saved → status → ...`；confirmation/replay 不得新增该事件；
- 新建和已有 Conversation；
- workspace/application scope；
- read + read、write + read、read + write、write + write；
- 只读失败后继续后续只读的 baseline 语义；
- 写入 confirmation required；
- approve、modify、reject；
- approve → read tool → final；
- approve → chained write Pending；
- terminal replay、delivery pending、owner takeover、late Bundle；
- Provider failure/fallback、timeout、disconnect/cancel；
- source_load_failed、budget fail-closed、unknown/unexposed tool；
- deterministic initial action、approve、modify、reject、replay；
- Journal enabled/disabled/degraded。

SSE 响应头边界还必须逐项锁定：

- Source Loader 失败继续是响应头前 HTTP 503，而不是 HTTP 200 + SSE `error`；
- deterministic 校验/执行产生的 baseline 4xx/5xx 继续直接返回 JSON；
- confirmation token、edited args、Ledger/Pending 身份和 terminal replay 校验产生的
  baseline 409/422/503 继续直接返回 JSON；
- 只有 baseline 本来进入 SSE 的结果才创建 `StreamingResponse`；只有
  `execution_mode=agent_host` 才创建 worker 与 Queue，deterministic direct stream 继续为 0；
- precomputed deterministic/replay SSE 不重复业务执行；
- sync model 锁定 `user persist → Run start → Source load`，stream model 锁定
  `user persist → Source load → Run start`，并逐项比较 Journal 序列；

每个场景比较：

```text
HTTP status/body
SSE event/type/data/seq/order/termination
Provider candidate and call count
Tool executor call count
Conversation/ChatMessage order
Pending/clarification state
Ledger Operation/Delivery/Undo state
Journal event sequence and Trace anomalies
Context Surface fingerprint
domain side effects
```

Golden 只使用合成数据，不保存 SQLite、真实用户内容、密钥、endpoint、时间或随机 ID。

### 6.3 AST 与源码门禁

切换后机械证明：

- 四条 Route 不引用 `run_turn`、`resume_after_confirm`、Context Loader、RunRecorder、
  Write Coordinator、Pending claim、delivery heartbeat 或 Chat persistence helper；
- Route 只调用请求 normalizer、PilotRuntime 和 Transport renderer；
- `pilot_runtime` 不导入 FastAPI/Starlette response/background task；
- Agent Runner 不导入 `pilot_runtime`；
- SSE 编码和 `seq` 分配只存在于 Transport allowlist；
- `UserMessageSavedEvent` 是 RuntimeEvent 联合成员，payload 只能是 `role="user"`；
- outer executor/Future、无界 Queue、cancel Event 和 title finalizer 只存在于 Transport
  ownership allowlist；
- `AgentExecutionHost` 不依赖 Repository/Journal/Pending/Ledger，且只能执行 Runtime 提供的
  Agent thunk；
- Runtime 不 shutdown outer executor、不 close Event Queue，也不注册 FastAPI background
  task；
- `prepare_stream()` 是响应头前业务判断的唯一入口，Route 不复制 Source/Pending/Ledger
  precheck；
- 所有 `PreparedStreamExecution` 必须由 `PreparedStreamGuard` 包装；不得直接构造裸
  StreamingResponse 或只依赖 generator `finally`；
- Runtime Event 不包含任意 dict payload 或异常对象；
- 模型 dispatcher 不引用 Legacy deterministic adapter；
- 旧 Route 编排 helper 和 callback 闭包已删除，无 feature flag/fallback/shadow；
- `startswith("错误：")` 等兼容解析 allowlist 不扩大；
- Provider SDK/Agent Gateway 与 Phase 4 的网络边界 manifest 不变；
- 所有新增 DTO 均通过 checkpoint/serialization 负向门禁，不进入 Graph State、ChatMessage、
  Pending、Ledger 或 Journal，除非现有契约明确允许对应字段。

### 6.4 单元与并发测试

除 golden 外必须覆盖：

- Runtime 状态转换表的每个合法和非法边；
- Prepared lifecycle 只接受 `prepared→aborted`、`prepared→executing` 和
  `executing→completed(reason)`；`prepared→completed`、`aborted→*`、`completed→*` 及
  第二次 completion reason 更新均失败且零副作用；
- normal/cancelled/transport_aborted 三个 completion reason 分别验证，cleanup/disposition
  只由唯一 `executing→completed` winner 执行一次；
- `ImmediateHttpOutcome` 与 `PreparedStreamExecution` 的全部 preparation kind；
- prepared handle single-use、worker 启动前 abort、execute/abort 竞态和重复调用绝对 no-op；
- Response 构造失败、Response 已构造但 body iterator 零次启动、首次迭代立即断开、正常完成
  和 Background/finalizer 重复调用均通过同一 CAS；
- model abort 保留 user message 且不写领域；deterministic initial abort 保留 Chat/Pending；
  deterministic confirmation abort 保留 Ledger/领域 terminal；terminal replay abort 不重放；
- Outcome → HTTP 与 Outcome/Event → SSE 的纯函数映射；
- Event Queue 明确为 baseline 无界 `Queue()`；慢 consumer 不阻塞 worker，也不改变 timeout
  起点或事件顺序；
- Sink 抛异常、consumer 退出和 response renderer 失败分别产生
  `RuntimeTransportAborted`，不产生产品错误；
- cancel event 产生 `RuntimeCancelled`，并与 transport abort、Provider failure 做机械分类；
- sync/SSE title signal 所有退出路径恰好 finalizer 一次；
- 双连接 confirmation claim winner；
- terminal commit 后 delivery owner 活跃、崩溃 takeover、late Bundle；
- source read lock 与 heartbeat 协调；
- timeout 发生在 claim 前、executor 中、terminal 后和 continuation 中；
- Source Loader 和 Agent 返回后的 persistence 延迟不计入 Agent timeout；Agent deadline
  起点、poll 和结束点与 baseline 一致；
- timeout 后不可取消 Provider 的迟到成功/失败均被 invocation control 丢弃；非写消息和
  Pending 不落库，写 delivery 由 generation/owner fencing 拒绝；
- cancellation 与 `BaseException` cleanup；
- Journal 故障不改变 Outcome、副作用或调用次数；
- renderer/transport/projector 后处理失败不重跑 Provider/executor；
- deterministic 与 model 路由不能互相 fallback；
- 四条 Route 对同一业务场景的最终数据库状态等价。

### 6.5 完整发布门禁

完成前运行：

```text
focused pilot_runtime tests
four-route Chat API matrix
Agent/Tool Pipeline/Ledger/Journal/Context Projector suites
backend manifest union / duplicate node ID / skip / aggregate gates
full pytest
Ruff
Mypy
frontend tests and build
static smoke
local verify
controlled real-AI verify
```

至少完成一次本地浏览器闭环：

- workspace sync/stream Chat；
- application scope Chat；
- read-tool multi-call；
- write confirmation approve/modify/reject；
- chained Pending；
- terminal replay/页面状态回读。

独立 CR 必须无未关闭 P0/P1/P2。固定 baseline allowlist、未跟踪文件、
`git diff --check`、工作区干净均必须通过。外部 release-orchestrator gate 若缺少变量，必须
如实报告，不得伪造通过。

### 6.6 完成定义

只有同时满足以下条件，才能宣称本期完成：

- 四条 Chat Route 已完全切换到一个 PilotRuntime；
- Route 内不存在可靠性原语的手工编排；
- sync/stream/confirm/confirm-stream 共享同一状态机；
- 旧路径已删除且机械门禁通过；
- LangGraph、Tool、Ledger、Projector、Journal 和公开协议保持既定兼容；
- Provider/Tool 调用次数、业务副作用和持久化状态通过 golden；
- 无新增数据库/API/UI/Context/权限能力；
- 发布报告明确这是内部破坏性提取，不是 Agent Loop 重写；
- 不宣称 Scoped Capability、Metadata Convergence 或全局 exactly-once 已完成。

本设计已于 2026-08-21 完成书面复审，无剩余 P0/P1/P2。实施必须先遵循测试先行计划，
不得跳过 characterization golden、机械门禁或独立 CR。
