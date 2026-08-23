# Agent Loop Unification 设计

日期：2026-08-23

状态：**待复审**

固定 baseline：`aaecf5dfa6ce913ecaf00b25a0e88bcf46096eeb`

目标分支：`refactor/20260823-agent-loop-unification`

## 1. 背景、目标与边界

### 1.1 当前问题

Durable Execution Journal、Tool Execution Pipeline、Write Operation Ledger、Context
Projector、Journal Active Work Budget V2 和 Pilot Runtime 已经形成稳定边界。四条 Chat
入口现在共享一个 `PilotRuntime`，HTTP/SSE Transport 不再自行编排可靠性原语。

当前剩余的主要重复位于 Agent Driver 内部：

```text
首次运行
  → LangGraph StateGraph
  → call_model ↔ handle_tool
  → interrupt() 产生 Pending

批准/修改后的确认续跑
  → _resume_without_checkpoint()
  → 手工 prepare / claim / execute / ToolMessage
  → continuation_message_loader()
  → 再递归进入 run_turn()
```

这两条路径虽然由 Pilot Runtime 统一承载，但仍分别维护：

- 消息累积与 `AgentTurnResult` 合并；
- Tool prepare、ToolMessage 和失败收集；
- 最大模型轮次；
- cancellation / delivery fence 检查点；
- Runtime tool event 接入；
- 确认结果进入后续模型循环的时序。

当前 Graph 只有 `call_model` 与 `handle_tool` 两个节点，使用 `InMemorySaver`，不再承担持久
恢复真值。确认恢复的业务真值已经是 Pending Action、Write Operation Ledger、Conversation
消息和 delivery fencing。继续保留 LangGraph 不再提供与其复杂度相匹配的能力。

### 1.2 本期目标

本期采用内部破坏性切换，将首次运行和批准/修改后的确认续跑统一成一个显式 Agent Loop：

```text
PilotRuntime
  → AgentDriver.execute(AgentLoopInvocation)
  → AgentLoopRunner.run()
       ├── NewTurnSeed
       └── ApprovedWriteSeed
  → AgentTurnResult
```

完成后必须满足：

- 生产代码只有一个 Agent Driver 调用方法和一个 Agent Loop 入口；
- 首次模型运行与确认成功后的后续模型运行进入同一 `while` 循环；
- 写入确认不再依赖 Graph interrupt、Graph State 或 checkpointer；
- `LangGraphAgentRunner`、`_resume_without_checkpoint` 和反射式双调用协议被删除；
- 若仓库中无其他使用者，删除 `langgraph` 与 `langgraph-checkpoint-sqlite` 直接依赖；
- Provider、Tool、Pending、Ledger、Journal、Projector、HTTP/SSE 与业务副作用保持兼容；
- 后续 Scoped Capability、Binding Enforcement 和 Tool Metadata Convergence 可在单一 Loop
  上实施，不再维护初始/恢复两套分支。

### 1.3 兼容口径

本期定义为：

> 内部允许破坏性重构；外部协议、可靠性边界和可观察因果序列严格兼容。

必须保持：

- 四个 Chat endpoint、请求字段、HTTP 状态和响应字段；
- SSE event 名称、payload、`seq`、顺序、终止和断开语义；
- Provider 候选链、完整 Tool Surface、请求 envelope、fallback、调用顺序和调用次数；
- 25 个模型可见工具与 3 个 Legacy deterministic 工具边界；
- 当前多 ToolCall 精确选择规则；
- HITL、Pending Action、confirmation token、CAS、Ledger、Undo/Compensation；
- delivery owner/generation/lease/heartbeat/fencing 与 commit-unknown 对账；
- Context Source 加载次数、Model Surface、Manifest、fingerprint 和预算规则；
- Journal 事件 Schema、事件顺序、Trace 异常规则和 fail-open；
- Conversation/ChatMessage/Pending/Ledger/领域数据库 Schema；
- terminal replay、delivery recovery/fallback、拒绝和确定性动作的 Provider 0；
- 单个 Operation 的领域事务至多提交一次、terminal replay 不重跑 executor；
- 当前最大模型轮次、取消、超时和迟到结果丢弃语义。

允许改变的是私有 Python 类型、文件布局、内部入口、测试注入接口和依赖集合。对于相同的
合成 Provider 输出，本期要求最终字符串、消息、事件和副作用与 baseline 等价；不以真实模型
回答文本的字节一致作为验收标准。

唯一明确的 failure-only 兼容例外是 Agent Event Sink：baseline 的 Agent 私有 emitter 会吞掉
普通 Sink 异常并继续执行，新实现改为由 composition adapter 将其转换为
`RuntimeTransportAborted` 并立即停止本次 Loop。这一变化只发生在 Transport/Event Sink 已经
故障的请求中，不改变成功路径的 HTTP/SSE payload；它用于落实 Pilot Runtime 已批准的
Transport ownership 契约。Sink 故障绝不能触发 Provider fallback、第二次 Provider 调用或
额外 Tool 执行。

### 1.4 非目标

本期明确不做：

- 不实施 Scoped Capability 或强制 Binding；
- 不改变 Context Projector 的 Tool Selector、预算、历史选择或 Contributor；
- 不收敛 Tool domain/dependency/transaction metadata；
- 不迁移 3 个 Legacy deterministic 工具到模型 Loop；
- 不改变写工具确认策略，也不启用 `auto_approve` 绕过确认；
- 不更换用户可见 Tool Result 或错误字符串协议；
- 不新增 Provider retry、Tool retry、fallback、shadow execution 或双写；
- 不新增数据库、API、SSE、UI、Memory、Knowledge、Summary 或 Wakeup queue；
- 不把 rejection、terminal replay 或 deterministic action 强行送入模型循环；
- 不引入多 Agent、插件系统、持久 SSE replay 或任意代码执行；
- 不声明跨请求、跨进程、跨 Provider 或跨外部系统 exactly-once。

本期不扩大 Pilot Runtime 的职责，也不重新设计 Phase 1–4 已冻结的契约。

## 2. 核心决策

### 2.1 使用显式循环并移除 LangGraph

目标控制流固定为：

```text
bootstrap seed
→ while model_steps < max_iterations
    → project_model_surface
    → Provider logical model call
    → 无 ToolCall：final
    → 选择 baseline ToolCall batch
    → dispatch batch
       → read/pre-executor Tool failure：生成 ToolMessage，continue
       → write confirmation：返回 Pending，suspend
```

选择显式循环的原因：

- 当前没有持久 Graph checkpoint；
- 业务恢复由 Pending/Ledger 负责；
- Graph 没有复杂分支或并行节点；
- Graph interrupt 只是把写调用转换成 Pending；
- 显式循环可直接表达模型轮次、消息和一次性工具处理顺序。

不保留 LangGraph adapter、feature flag、shadow loop 或失败时回退。切换完成后旧 Graph 路径
必须删除，不能长期作为 façade 留在新 Loop 外层。

### 2.2 Agent Loop 只接收两类 Seed

模型 Loop 的封闭入口为：

```python
@dataclass(frozen=True, slots=True)
class NewTurnSeed:
    messages: tuple[Message, ...]


@dataclass(frozen=True, slots=True, repr=False)
class ApprovedWriteSeed:
    continuation: ApprovedWriteContinuation


AgentLoopSeed = NewTurnSeed | ApprovedWriteSeed
```

`NewTurnSeed` 表示 Source Loader 与 Context Assembler 已经提供的首次运行消息。

`ApprovedWriteSeed` 只表示服务端已经通过 Ledger-first preheader 校验，并由 Runtime 建立的有效
批准/修改 Session。它不接受客户端布尔值、裸 `tool_name`、裸参数或裸 callback 字典。

拒绝和 terminal replay 不属于 Agent Loop Seed：

```text
reject
  → token/Pending/Ledger identity
  → rejection CAS
  → deterministic delivery
  → AgentDriver 0 / Projector 0 / Provider 0

terminal replay / delivery recovery
  → Ledger-first integrity + fencing
  → persisted result delivery
  → AgentDriver 0 / Projector 0 / Provider 0 / executor 0
```

这不是保留第二套 Agent Loop，而是维持 Pilot Runtime 已批准的 provider-free terminal route。

### 2.3 Agent Driver 收敛为一个严格方法

目标协议固定为：

```python
class AgentDriver(Protocol):
    def execute(self, invocation: AgentLoopInvocation) -> AgentTurnResult: ...
```

删除：

- `AgentDriver.run_turn()`；
- `AgentDriver.resume_after_confirm()`；
- `_callable(driver, ("run_turn", "run"))`；
- `_invoke()` 对 `max_iter/max_iterations`、`catalog/tool_catalog` 等别名的反射适配；
- `run_turn_fn` 与 `resume_after_confirm_fn` 双依赖；
- API 中 `_runtime_resume_after_confirm` 包装函数。

所有生产调用方和测试 fake 同步迁移到 `execute(invocation)`。缺少该方法是 composition/startup
错误，不在运行时尝试其他名字。

### 2.4 Loop Invocation 与确认 Port 均为瞬态对象

```python
@dataclass(frozen=True, slots=True, repr=False)
class AgentLoopInvocation:
    seed: AgentLoopSeed
    model: ChatModel
    catalog: ToolCatalog
    tool_context: ToolExecutionContext
    auto_approve: bool
    max_iterations: int
    run_recorder: RunRecorder
    event_sink: AgentEventSink | None
    runtime_signal_sink: RuntimeSignalSink[str] | None
    cancel_check: CancelCheck | None
```

Agent 侧事件同时收敛为封闭、transport-neutral 的瞬态联合类型：

```text
AgentAssistantDelta
AgentToolCall
AgentToolResult
```

`AgentEventSink.emit(AgentLoopEvent)` 不接受任意 dict。composition adapter 负责把它们一对一
投影为既有 `RuntimeEvent`；Agent Loop 不导入 `pilot_runtime.contracts`。Sink 的普通异常由
adapter 转换为既有 `RuntimeTransportAborted`，cancellation 与其他 `BaseException` 原样传播，
Loop 不得吞掉后继续执行 Provider/Tool。

确认 Port 固定为：

```python
class ApprovedWriteContinuation(Protocol):
    @property
    def pending(self) -> PendingAction: ...

    def claim(
        self,
        pending: PendingAction,
        prepared: PreparedToolCall[Any, Any],
    ) -> ExecutionAuthorization | ToolFailure: ...

    def record_result(
        self,
        pending: PendingAction,
        tool_message: Message,
        record: ToolExecutionRecord[Any, Any],
    ) -> None: ...

    def load_continuation_messages(self) -> tuple[Message, ...]: ...

    def delivery_fence(self) -> bool: ...
```

`ApprovedWriteContinuation` 是现有 `ConfirmationSession` 的窄 adapter；它不复制 Ledger 状态机。
真实写入仍由 `ToolExecutionContext.operation_executor` 指向现有
`ConfirmationSession.execute_operation`，不由 Loop 直接调用 Repository。

上述对象可能携带 callback、credential-bearing model、owner token 或 Session 状态，因此：

- 全部 `repr=False`；
- 不进入 LangGraph/checkpoint（本期删除该机制）；
- 不进入 ChatMessage、Pending、Ledger、Journal、Trace 或日志；
- 禁止通用 JSON/pickle/dataclass 序列化；
- 不跨 request、process 或 delivery generation 缓存。

### 2.5 Seed 与 Context 必须匹配

构造时执行封闭校验：

- `NewTurnSeed` 必须有 detached message tuple，不携带 confirmation Port；Loop 不原地修改
  tuple 中的输入消息；
- `ApprovedWriteSeed` 必须有非空 operation/tool-call/tool-name 身份；
- Approved Seed 的 Pending 必须来自 Port，不接受第二份可能漂移的 Pending；
- Approved Seed 的 `tool_context.operation_executor` 必须是 Runtime 绑定的 Ledger executor；
- New Seed 使用 normal Tool Context 与 `_ProposalJournalGate`；
- Approved Seed 的 origin prepare 使用 `record_proposal=False`，不重复写 origin
  `tool.proposed`；confirmation Segment 内后续模型提出的新 chained write 仍按 baseline
  使用当前 recorder 记录新的 proposal；本期不改变这条 Journal 时序；
- `max_iterations=0` 继续按 baseline 解析为 `DEFAULT_MAX_ITERATIONS`；负数或非法类型继续由
  现有 Runtime 配置校验处理，不新增对外错误协议。
- 删除只为 Graph checkpointer 和进程内 confirmation lock 服务的 Agent `thread_id`；Durable
  Run/Segment、Conversation 和 model_call 身份继续使用各自现有 typed identity，不拿
  `thread_id` 代替。

任何组合错误均在 Provider/executor 前失败，不尝试旧入口或 fallback。

## 3. 统一 Loop 状态机

### 3.1 瞬态状态

Loop 使用局部、显式状态，不使用任意字符串 Graph State：

```text
working_messages    本 Segment 当前模型可见输入候选
added_messages      本次 AgentTurnResult 新增消息
records             本次 Segment 的 ToolExecutionRecord
failures            本次 Segment 的 ToolFailure
model_steps         本 Segment 已完成的 logical model call 数
last_surface_binding 当前 logical model call 的瞬态 Surface binding
```

状态只存在于一次 `execute()` 调用栈。没有 checkpoint、resume command、recursion limit 或
`__interrupt__` 投影。

### 3.2 New Turn bootstrap

```text
NewTurnSeed.messages
→ working_messages
→ added_messages = []
→ records/failures = []
→ model_steps = 0
→ 进入统一 while loop
```

Source Loader、用户消息持久化、Run/Segment 创建与 Journal context capture 仍由 Pilot Runtime
按当前 sync/stream 时序完成。Agent Loop 不重新读取 Conversation 或业务 Repository。

### 3.3 Approved Write bootstrap

批准与修改使用相同流程；修改后的 canonical effective args 已由 ConfirmationCoordinator 写入
Port 的 `pending`：

```text
检查 cancel / delivery eligibility
→ 从 trusted Port 读取 effective Pending
→ resolve ToolSpec（必须存在且 kind=write）
→ canonical JSON object 校验
→ prepare_call(record_proposal=False)
→ 必须得到 ConfirmationRequired
→ emit approved origin tool_call
→ execute_prepared(
     prepared,
     confirmation_claimer=Port.claim,
     context.operation_executor=Ledger executor
   )
→ executor 前失败：不生成 ToolMessage，不进入模型循环
→ executor 已开始并得到 terminal record：渲染权威结果
→ emit origin tool_result（SSE 仍由 _ConfirmationEventSink 延迟释放）
→ 构造一个 origin ToolMessage
→ Port.record_result()
→ Port.load_continuation_messages() 恰好一次
→ working_messages = loaded_messages + origin ToolMessage
→ added_messages = [origin ToolMessage]
→ records/failures 保留 origin 执行结果
→ model_steps = 0
→ 进入同一 while loop
```

固定约束：

- `prepare_call` 是执行前重建，不信任跨请求 `PreparedToolCall`；
- claim/authorization 必须发生在 executor 前，失败时 executor 为 0；
- mutable recheck、`BEGIN IMMEDIATE`、Pending/Ledger claim、领域事务、terminal digest、required
  undo 与 commit-unknown 对账继续由现有 Tool Pipeline + Write Coordinator 承担；
- Loop 不读取或缓存 Repository 状态；
- `record_result` 失败、Source Loader 失败、Projector 失败、renderer/transport 失败不得重跑
  executor；
- terminal 持久结果存在时使用 `persisted_visible_result` 与 `persisted_transport`，不得根据当前
  内存结果重新生成；
- 若 live preflight 后在 `execute_operation` 处发现 terminal race，传播既有
  `ConfirmationReplayError` 给 Runtime 做 Ledger replay；不得构造 origin ToolMessage、加载
  Source 或进入 Provider loop；
- terminal 写入失败仍可进入后续模型循环，并且 origin failure 必须保留在最终 records/failures，
  以维持当前 `write_status=failed`；
- Source 只在 terminal commit 与 delivery ownership 建立后加载一次；失败也由现有
  `load_once` 缓存，不进行第二次 mutable read；
- continuation 内 Provider/read-tool loop 可以生成最终文本或 chained Pending；
- chained Pending 与旧 Pending 的原子替换仍由 Runtime delivery transaction 完成。

### 3.4 统一 model step

每轮固定为：

```text
cancel/fence check
→ if model_steps >= max_iterations: 抛既有最大轮次错误
→ Projector 为新的 model_call_id 构建 Frozen Surface
→ Provider Gateway logical call
→ 验证 BoundProviderResponse 与 ModelCallSurfaceBinding
→ model_steps += 1
→ 按 baseline 选择 ToolCall batch
→ 添加一个 assistant Message
→ 无 ToolCall：返回 final
→ 有 ToolCall：逐个 emit tool_call，再进入 dispatch
```

模型失败不增加一次成功 step，也不重试 Loop。Provider Gateway 内候选 fallback 仍属于同一个
logical `model_call_id` 与同一个 Frozen Surface；fallback attempt 不重新投影，也不额外增加
`model_steps`。

确认 continuation 是新的 Durable Segment，因此 `model_steps` 从 0 开始；origin write bootstrap
不占模型轮次。每次 chained Pending 再确认又创建新 Segment并重新从 0 开始。这与当前
`_resume_without_checkpoint() → run_turn()` 一致。

### 3.5 final 与 pending outcome

仅有两种正常 Loop 出口：

```text
final
  → AgentTurnResult(added, reply, pending=None, records, failures)

suspend
  → AgentTurnResult(added, reply="", pending=PendingAction, records, failures)
```

Pending 的 `operation_id` 在把当前 `ConfirmationRequired` 转换成 `PendingAction` 时生成一次；
同一次 Loop 不得重新生成。Runtime 仍负责检查 missing target question、持久化 assistant
ToolCall、Pending/Operation proposal 和 Journal suspend。

异常、取消和 Transport 中止不转换成第三种 Agent outcome；它们按既有 typed exception
边界传播给 Pilot Runtime。

## 4. ToolCall 选择与执行语义

### 4.1 多 ToolCall 精确基线

选择规则保持当前实现，不在本期“修正”：

```text
全部已通过 Surface Binding 的调用均为 read
  → 按 Provider 原顺序保留全部

只要任一调用解析为 write
  → 只保留原始 tool_calls[0]
```

因此机械矩阵为：

| Provider 返回 | 本期处理 |
|---|---|
| read + read | 依次执行两个 |
| write + read | 只处理第一个 write |
| read + write | 只处理第一个 read，第二个 write 被丢弃 |
| write + write | 只处理第一个 write |

该矩阵只对已经通过 `ModelCallSurfaceBinding.validate_response()` 的 ToolCall 生效。只要一个
Provider response 含有未暴露名称，整个 response 会在进入本选择函数前 fail-closed，不能用
“unknown 是 read-like”规则保留其余调用。

选定 batch 后，assistant Message 中只保存选定 ToolCall，与 baseline 一致。sync 与 stream
必须使用同一个纯选择函数。

### 4.2 未暴露工具与 Catalog 完整性

`ModelCallSurfaceBinding.validate_response()` 必须在增加 `model_steps`、追加 assistant Message、
发出 ToolCall event 或进入 Dispatcher 前验证完整 response：

- 任一 ToolCall 名称不在本次 `exposed_tool_names` 时，整个 logical response 以现有
  `ProjectionError("unknown_tool")` fail-closed；
- 即使完整 Typed Catalog 能解析该名称，也不得进入 Dispatcher；
- assistant Message、ToolCall/ToolResult event、兼容 ToolMessage 均为 0；
- capability、binding、preflight、executor 均为 0；
- 不保留同一 response 中其他合法 ToolCall，不重新运行 Tool Selector，也不回退完整 Catalog；
- Journal 继续按 baseline 将该 logical model call 记录为 `model.failed`，不伪造
  `model.completed` 或 tool events。

Tool Pipeline 的 `validation_error / unknown_tool` 分类继续存在，供直接 Catalog/Dispatcher
契约和防御性测试使用；它不能被 Agent Loop 用来把 Phase 4 的 Surface provenance violation
降级成可继续的 ToolMessage。生产 Agent Provider 必须全部经过 Frozen Surface/Gateway，不保留
绕过 Binding 的 non-surface-aware 模型路径。

### 4.3 只读工具

每个 read ToolCall 固定为：

```text
cancel/fence
→ prepare_call
→ Rejected：兼容失败 ToolMessage，继续 batch
→ ReadyToExecute：executor 恰好一次，兼容结果 ToolMessage，继续 batch
→ 意外 ConfirmationRequired：既有“只读工具不能请求确认”结果
```

前一个只读工具失败不阻止后续只读工具。每次结果都按原始顺序加入
`working_messages/added_messages`，batch 完成后回到统一 model step。

### 4.4 首次写工具 proposal

New Turn 中选中的 write ToolCall 固定为：

```text
prepare_call(record_proposal=True)
→ Rejected：兼容失败 ToolMessage，继续模型循环
→ ConfirmationRequired：创建 PendingAction 并立即退出 Loop
→ 其他状态：保持既有“确认操作状态不一致”兼容结果，不执行写入
```

必须满足：

- 写工具即使 `auto_approve=True` 也不绕过确认；
- 等待确认只产生 `tool.proposed → approval.requested`，不产生 `tool.started`；
- Pending durable 前 `_ProposalJournalGate` 继续暂存 proposal；
- Runtime 成功持久化 Pending 后才提交暂存 Journal proposal；
- missing target clarification、Pending CAS 和 Conversation archived 仍由 Runtime 处理；
- 创建 Pending 时 executor 为 0。

### 4.5 已批准写工具

Approved bootstrap 是唯一可执行模型可见 write Tool 的路径。单次
`execute_prepared()` 内 executor 最多调用一次；它不声明跨请求 exactly-once。跨请求重放与
并发由 Operation ID、Ledger terminal state 和 delivery fencing 收敛。

任何执行前失败：

```text
executor = 0
tool.started = 0
不进入后续 Provider loop
```

executor 自身返回或抛普通 `Exception`：

```text
executor 调用恰好 1 次
→ Tool Pipeline / Ledger 形成既有 terminal 或 result-unknown 语义
→ 绝不 fallback 到旧 handler
```

取消、`KeyboardInterrupt`、`SystemExit` 和其他 `BaseException` 不映射成 ToolFailure，清理后
原样传播。

## 5. Context、Provider、Journal 与事件

### 5.1 Context Projector 不变

每个 logical model call 继续独立投影一个 Frozen Surface：

- 同一 Segment 的业务 Source snapshot 不重新查询；
- read ToolMessage 可使下一轮重新选择历史/工具，但候选 Provider 链与预算版本保持冻结；
- Approved continuation 在 origin Ledger terminal 后加载一次新 Source snapshot；
- fallback Provider 复用同一 Surface；
- Surface binding 只在当前调用栈存在；
- Projector 失败 fail-closed，Provider 0；
- terminal replay/reject/deterministic route 完全绕过 Projector。

本期不改变 `current_request`、mandatory TurnGroup、附件、scope、tool selector 或 byte/token
预算算法。

### 5.2 Provider 边界不变

Agent Loop 只能调用现有 Agent Provider Gateway/Frozen Provider Chain，不能直接访问多 Provider
client、LiteLLM、SDK 或 HTTP transport。Surface 冻结时尚未发生真实 Provider attempt，因此
不得预先发明一个 expected candidate ordinal 或 attempt ID。`BoundProviderResponse` 的验证
固定为：

```text
response.model_call_id == binding.model_call_id
response.runtime_surface_fingerprint == binding.runtime_surface_fingerprint
0 <= response.candidate_ordinal < binding.provider_candidate_count
response.provider_attempt_id 为非空、由本次 AgentProviderGatewaySession 创建的 attempt identity
response 中每个 ToolCall.name ∈ binding.exposed_tool_names
```

`candidate_ordinal` 与 `provider_attempt_id` 在 Gateway 实际开始每个候选 attempt 时生成；它们不与
投影前不存在的“预期值”比较。Gateway Session 必须是生产代码中
`BoundProviderResponse` 的唯一构造者：Runner 和 Provider transport 只能接收 Gateway 创建的
attempt handle，不能自行填写 ordinal/attempt ID。`provider_attempt_id` 只存在于当前调用栈，
不得进入 Surface、Journal、Trace、日志或持久化消息。AST 门禁必须证明生产构造点封闭；测试
注入也必须通过单候选 Gateway Session，而不是直接伪造 Bound response。

上述任一 provenance 或 Tool Surface 校验失败时，整个 logical response fail-closed，executor
为 0，不读取 response 中的工具名称做自我授权。

流式候选一旦已经产生首个对外可观察 delta，后续 Provider failure 不得 fallback 到下一候选；
只有现有 Gateway 允许的 pre-output failure 才能按冻结顺序 fallback。Sink abort、request
cancellation 和 `BaseException` 属于控制流，不得被 Provider client 分类为可 fallback 的普通
Provider failure。

### 5.3 Journal 时序

不新增 Event 类型或 payload 字段。每个 logical model call 继续为：

```text
context.captured
→ model.requested
→ model.completed | model.failed
```

约束：

- `model.requested` 在第一次真实网络调用前；
- Projection/Adapter preflight 失败时不写 `model.requested`；
- Provider fallback attempts 不产生多组 logical model events；
- Snapshot/Journal 写入失败 fail-open，不重新投影、不阻止 Provider；
- read tool 与 write proposal/execute 继续使用 Phase 1 已冻结的
  `tool.proposed → tool.started → tool.completed|tool.failed` 合法子序列；
- pre-executor failure 不伪造 `tool.started/completed/failed`；
- Pending suspension 不产生 `tool.started`；
- Journal degraded 不改变 AgentTurnResult、Provider/Tool 次数或业务状态。

### 5.4 Runtime Event 与 SSE

Agent Loop 只产生封闭的 `AgentLoopEvent`，composition adapter 投影为现有 typed
`RuntimeEvent`；Loop 不编码 SSE，不分配 `seq`。典型顺序保持：

```text
assistant_delta / tool_call / tool_result ...
→ confirmation_required | assistant_message | error
```

确认 origin `AgentToolResult` 投影后仍先进入 `_ConfirmationEventSink` 的延迟队列，只有权威
delivery transaction 成功后才向外释放。Loop 不因内部统一提前交付 terminal 结果。

Event Sink 的兼容例外按执行阶段固定：

```text
首个 delta emit 失败
  → RuntimeTransportAborted
  → Provider fallback 0，Tool 0，后续 Runtime event 0

已经成功 emit 一个或多个 delta 后失败
  → RuntimeTransportAborted
  → 不向下一候选 fallback，不补 error/completed

AgentToolCall emit 失败
  → Dispatcher 尚未开始
  → prepare/capability/binding/executor 0

AgentToolResult emit 失败
  → 已经发生的 executor/terminal 不回滚、不重跑
  → 后续 ToolCall 与 Provider loop 0
  → write terminal 只允许既有 Ledger replay/delivery recovery 收敛
```

composition adapter 的异常阶梯固定为：

```text
RuntimeCancelled / RuntimeTransportAborted / RuntimeAgentTimedOut
  → 原样传播
普通 Exception
  → RuntimeTransportAborted
KeyboardInterrupt / SystemExit / 其他 BaseException
  → 原样传播
```

Agent Loop 在任一 Sink failure 后不得尝试向同一 Sink 发送 `error` 或 `completed`。

标题资格仍由第一个完整有效模型响应触发一次；RuntimeSignalSink 的容量 1、非阻塞、
fail-open 协议不变。

## 6. Pilot Runtime 集成

### 6.1 Start Turn

```text
PilotRuntime existing preparation
→ 构造 NewTurnSeed(messages)
→ AgentDriver.execute(invocation)
→ normalize AgentTurnResult
→ existing persistence / Pending / disposition
```

composition adapter 对 New Seed：

- 安装 `_ProposalJournalGate`；
- 将当前 RunRecorder 注入 normal ToolExecutionContext；
- 调用 `AgentLoopRunner.run()` 一次。

### 6.2 Approve / Modify

```text
Ledger-first preheader
→ live Pending + effective args
→ model / catalog resolve
→ ConfirmationSession + journal Segment + bound tool context
→ 构造 ApprovedWriteSeed(ApprovedWriteContinuationAdapter(session))
→ AgentDriver.execute(invocation)
→ existing _finish_ledger_confirmation / delivery transaction
```

sync 与 stream 只在 ExecutionHost/EventSink 上不同；Seed、Loop 和 Tool 执行完全共用。当前
`messages=[]`、`resume_values` 字典和反射 `_invoke(resume, ...)` 全部删除。

### 6.3 Reject 与 Replay

拒绝和 replay 继续在调用 `AgentDriver.execute` 前终止。必须增加 spy/AST 门禁证明：

- Conversation/model/source/catalog 不因 rejection 被加载；
- Agent Driver、Projector、Provider、Tool prepare、executor 均为 0；
- replay 不依赖 live Pending；
- rejection feedback 继续只进入既有 request fingerprint/兼容结果，不能进入 Journal/日志；
- sync/SSE 使用既有 typed RuntimeOutcome/Event renderer。

### 6.4 Deterministic 路径

三个模型不可见 deterministic 工具继续只由服务端可信流程进入
`DeterministicPilotAdapter`。它们：

- 不构造 AgentLoopSeed；
- 不进入 Typed model Catalog 或 Provider Surface；
- 不改变专用确认、CAS、幂等、写入和恢复；
- 不是 Agent Loop 异常时的 fallback。

### 6.5 Typed exception 映射

Agent Loop 不导入 `pilot_runtime`。composition adapter 负责把 typed `ChatRunCancelled` 映射为
`RuntimeCancelled`。删除 `type(exc).__name__ == "ChatRunCancelled"` 的字符串识别。

边界顺序固定为：

```text
RuntimeCancelled / RuntimeTransportAborted / RuntimeAgentTimedOut
→ 原样传播

PendingActionValidationError / StalePendingActionError / known product error
→ 既有 HTTP/SSE 安全映射

普通 Exception
→ 既有 provider/internal failure 映射

BaseException
→ cleanup 后原样传播
```

不因错误类型收敛新增用户可见 error code。

## 7. 超时、取消、所有权与单次执行

### 7.1 cancellation checkpoints

保持或加强为下列固定点，但不得减少 baseline 已有检查：

- Seed bootstrap 前；
- Approved prepare 前后；
- 每次 model step 前、Provider 返回后；
- 每个 ToolCall 前；
- `prepare_call` 后；
- read executor 前后；
- confirmation claim 前与 write executor 返回后；
- `record_result` 前后；
- continuation Source load 前后；
- 返回 AgentTurnResult 前。

delivery fence 在 Provider、read executor 和交付敏感边界继续检查。fence 丢失映射为现有取消/
stale 语义，不尝试新 owner 或第二次调用。

### 7.2 timeout 与迟到结果

Pilot Runtime/AgentExecutionHost 继续拥有 wall-clock deadline。显式 Loop 不创建 thread、Future、
Queue、timer 或 heartbeat，也不改变 deadline 起点。

- 不可中断 Provider/native 调用允许迟到返回；
- `RuntimeInvocationControl` 关闭交付资格；
- 非写迟到消息/Pending 被丢弃；
- 已 claim 的写操作由 Ledger 和 delivery generation/owner token 收敛；
- timeout/fallback 不启动第二个 Loop、Provider 或 executor；
- late Bundle 不能替换 takeover winner 的 delivery。

### 7.3 单次调用与重入

每个 `AgentDriver.execute(invocation)` 只能由 ExecutionHost 启动一次。Loop 内没有递归
`run_turn()`，确认 bootstrap 直接落入同一 while。

Approved Port 的业务 at-most-once 不依赖进程内锁：

- production 必须有 Ledger Session 和 operation executor；
- 删除全局 `_CONFIRMATION_LOCKS`、`_FALLBACK_CONFIRMATION_CLAIMS` 和 LRU fallback claim；
- 单元测试通过显式 fake Approved Port 模拟 claim winner/loser；
- 重复请求由 Ledger terminal replay 处理，不再次进入 Loop。

这消除了仅对单进程有效的伪恢复路径，同时不扩大 Phase 3 的保证。

## 8. 模块与依赖方向

目标模块建议收敛为：

```text
src/offerpilot/ai/agent_contracts.py
  ChatModel / PendingAction / AgentTurnResult
  Agent errors / AgentLoopEvent / narrow protocols

src/offerpilot/ai/agent_loop.py
  AgentLoopSeed / AgentLoopInvocation
  ApprovedWriteContinuation
  AgentLoopRunner
  model step / dispatch helpers

src/offerpilot/ai/confirmation.py
  prepare_pending_action（确认编辑的纯参数转换）

src/offerpilot/pilot_runtime/composition.py
  one AgentDriver adapter
  ConfirmationSession → ApprovedWriteContinuation adapter
```

允许实施阶段在不改变依赖方向的前提下合并过小文件，但必须满足：

```text
pilot_runtime → agent contracts / agent loop
agent loop → Context Projector / Tool Runtime / Journal interfaces
agent loop ↛ pilot_runtime / FastAPI / Repository / Transport
```

旧 `offerpilot.ai.agent` 不作为长期 façade 保留。其生产调用方、非 Agent `ChatModel` 引用和
测试同步迁移到新契约模块；完成后删除旧执行模块或确保其中不存在旧入口/Graph 实现。

若全仓扫描确认无其他 LangGraph 使用者，则从 `pyproject.toml` 和 `uv.lock` 删除：

```text
langgraph
langgraph-checkpoint-sqlite
及仅由其引入且不再需要的 transitive packages
```

不以手工编辑 lockfile 伪造删除，使用项目锁文件工具重新解析并验证安装。

## 9. 迁移策略

### 9.1 Characterization-first，生产一次切换

固定步骤：

1. 从 `aaecf5d` 捕获 Agent Loop 合成 golden；
2. 为 Seed、Invocation、Approved Port 和显式 Loop 写失败测试；
3. 用显式 Loop 跑通纯单元矩阵，尚不接 production；
4. 将 Pilot Runtime 的 start/approve sync/stream 同批切到 `AgentDriver.execute`；
5. 删除 Graph、resume、反射 adapter 和 in-memory confirmation fallback；
6. 删除 LangGraph 依赖并更新 lockfile；
7. 跑完整兼容、并发、隐私、AST 和发布门禁。

禁止：

- feature flag；
- 新旧 Loop 双轨；
- shadow Provider/Tool execution；
- sync 先切、stream 长期保留旧 resume；
- Agent Loop 失败时回退 `resume_after_confirm`；
- 从 ToolMessage 兼容字符串或 SSE payload 反向恢复 typed state；
- 自动更新/接受 golden。

### 9.2 Baseline golden

新增只读合成资产，来源必须标记固定 baseline `aaecf5d`。资产不覆盖 Phase 1–4 或 Runtime
Extraction 已有 golden。至少覆盖：

- final answer，无 ToolCall；
- read + read；
- read failure + read，后一个仍执行；
- unexposed + read：整个 response 在 Binding 处 fail-closed，其余 read 不执行；
- write + read、read + write、write + write；
- 未暴露但完整 Catalog 可解析的 tool，仍在 Binding 处 fail-closed；
- write 参数 validation failure 后继续 Provider；
- write proposal → Pending，executor 0；
- `auto_approve=True` 仍产生 Pending；
- approve / modify → committed write → final；
- approved deterministic write failure → read → final，最终 write status 仍 failed；
- approve → read tool → Provider → final；
- approve → chained write Pending；
- prepare/claim/authorization stale，executor 0；
- terminal replay、reject 和 delivery recovery，Provider/AgentDriver 0；
- Provider A failure → B fallback，同 Surface、一个 model step；
- Projection fail-closed，Provider 0；
- max iteration boundary；
- sync/stream cancellation、timeout 和 disconnect；
- Journal enabled/disabled/degraded；
- Context Surface fingerprint 与 Journal/Trace sequence。

每个场景比较：

```text
Agent added/reply/pending（随机 ID 规范化）
Provider complete envelope / Surface fingerprint / call count
Tool prepare/executor call count and order
RuntimeEvent and SSE payload/order/seq
Conversation/ChatMessage order
Pending/clarification state
Ledger operation/delivery/undo state
Journal event sequence and Trace anomalies
domain side effects
```

Golden 只保存 canonical 合成投影，不保存 SQLite、真实用户内容、密钥、endpoint、异常原文、
不稳定时间或未经规范化的 UUID。测试不得自动生成、覆盖或提供 update 开关。

## 10. 测试与机械门禁

### 10.1 Loop 单元测试

必须覆盖：

- 两种 Seed 的合法/非法构造组合；
- Approved Port Pending 是唯一身份来源；
- approved prepare、claim、execute、record、load 的严格顺序；
- claim failure、mutable failure、executor failure、record failure、load failure 的调用次数；
- terminal persisted result 优先于内存 renderer；
- origin ToolMessage 只追加一次；
- confirmation origin records/failures 不被后续循环重置；
- model step 只计成功 logical call；
- confirmation Segment 的 model step 从 0 开始；
- max iteration 在下一次 Provider 前失败；
- 所有多 ToolCall 矩阵；
- read batch 前项失败后继续；
- unexposed tool response 在 Dispatcher 前失败，assistant/ToolMessage/event/executor 均为 0；
- direct Tool Pipeline unknown-tool 契约保持 `validation_error / unknown_tool`，但 Agent Loop
  不把它用作 Surface violation 的兼容恢复；
- write Pending 不产生 ToolMessage/tool.started；
- chained Pending 返回完整 added messages；
- BaseException 不被 ToolFailure/Provider failure 吞掉；
- Event Sink 在首个 delta、后续 delta、ToolCall 与 ToolResult 四个阶段失败的精确收敛；
- Event Sink failure 不 fallback、不发送后续 event、不重跑 Provider/executor；
- renderer/projector failure 不重跑 Provider/executor。

### 10.2 Runtime 与并发测试

必须覆盖：

- start sync/stream 都只调用一次 `AgentDriver.execute(NewTurnSeed)`；
- approve/modify sync/stream 都只调用一次
  `AgentDriver.execute(ApprovedWriteSeed)`；
- reject/replay/deterministic route 调用 Driver 0 次；
- 双连接 confirmation claim 只有一个 executor winner；
- terminal commit 后 owner 活跃、crash takeover、late Bundle；
- source load 恰好发生在 terminal/ownership 后一次；
- chained Pending 原子替换且旧 Pending 不残留；
- timeout 位于 prepare、claim、executor、record、Source load、Provider 各阶段；
- disconnect/cancel 不产生迟到非写消息/Pending；
- 已进入 write transaction 的迟到结果仍服从 Ledger fencing；
- Journal fail-open 不改变数据库、Outcome 或调用次数；
- sync/SSE exact event golden；
- 首个 delta Sink failure 与可见 delta 后 Sink failure 均不切换 Provider candidate；
- ToolCall Sink failure 时 Tool prepare/executor 为 0；ToolResult Sink failure 时已发生的 executor
  恰好 1 次，后续 Provider/Tool 为 0；
- 标题信号所有退出路径仍恰好 finalizer 一次。

### 10.3 AST / Source gates

切换后机械证明：

- `src` 中无 `langgraph` import；
- 无 `StateGraph`、`InMemorySaver`、`interrupt(`、`__interrupt__`、`_GraphState`；
- 无 `LangGraphAgentRunner`、`_resume_without_checkpoint`、旧 `_compile_graph/_config`；
- 生产代码无 `resume_after_confirm`、`resume_after_confirm_fn`、`_runtime_resume_after_confirm`；
- Agent Driver Protocol 只有一个 `execute` 方法；
- Runtime 不用 `_callable/_invoke` 猜测 Agent Driver 入口或参数别名；
- API composition 只注入一个 Agent Loop dependency；
- rejection/replay path 在源码与 spy 测试中都不可达 Agent Driver；
- model dispatcher 不引用 Legacy deterministic adapter；
- Agent Loop 不导入 FastAPI、Starlette、Repository、PilotRuntime 或 SSE renderer；
- Agent Event Sink 不再接受任意 dict，legacy event mapping bridge 已删除；
- `BoundProviderResponse` 的生产构造点只存在于 `AgentProviderGatewaySession`；Runner、
  SingleCandidate transport 和 test injection seam 均不能直接构造；
- provenance validator 只要求 model-call/surface fingerprint 相等、candidate ordinal 在冻结范围、
  Gateway attempt ID 非空且属于当前 Session，不比较投影前不存在的 ordinal/attempt 值；
- Provider SDK/network boundary manifest 不扩大；
- 25/3 工具 manifest、Provider Tool golden 与 Schema fingerprint 不变；
- `startswith("错误：")` 等兼容字符串解析 allowlist 不扩大；
- 无 feature flag、shadow execution、dual write 或 old-loop fallback；
- `pyproject.toml` 与 `uv.lock` 不再声明无使用者的 LangGraph 包；
- 新增 transient DTO/Port 通过 repr、serialization、checkpoint、log privacy 负向门禁。

负向 fixture 必须证明每类门禁能拒绝刻意重新引入的 forbidden pattern，避免只对当前源码返回
空集合的自证测试。

### 10.4 既有回归套件迁移

现有测试中带 `checkpoint`、`mapped resume`、`missing checkpoint fallback` 名称但实际验证
Pending/Ledger 或 direct resume 的场景，应重命名为真实业务语义并迁移到新 Approved Port。

不得保留仅为了让旧测试继续通过的 fake `LangGraphAgentRunner` 或 public
`resume_after_confirm` façade。测试应直接覆盖 Seed、Loop 和 Runtime outcome。

## 11. 发布门禁与完成定义

### 11.1 完整门禁

完成前至少运行：

```text
agent loop focused tests
pilot runtime start/confirmation/stream tests
four-route Chat API matrix
Tool Pipeline / Ledger / Journal / Context Projector suites
backend manifest union / duplicate node ID / skip / aggregate gates
full pytest
Ruff
Mypy
frontend tests and build
static smoke
local verify
controlled real-AI verify
```

浏览器至少覆盖：

- workspace 与 application scope sync/stream Chat；
- read-only multi-call；
- write approve、modify、reject；
- approve → read → final；
- approve → chained Pending；
- terminal replay/页面状态回读；
- SSE disconnect 后恢复。

浏览器不以随机模型文本为断言，而检查 Provider/Tool 次数上限、Pending/Ledger/领域状态、SSE
终止、页面状态和无重复请求。

独立 CR 必须无未关闭 P0/P1/P2。固定 baseline allowlist、未跟踪文件、`git diff --check`、
worktree 干净均必须通过。外部 release-orchestrator 变量缺失必须如实报告，不能伪造成功。

### 11.2 完成定义

只有同时满足以下条件，才能宣称 Agent Loop Unification 完成：

- 首次运行与批准/修改确认共用一个显式 Loop；
- Agent Driver 只有一个严格 typed `execute` 入口；
- 旧 Graph、resume 分支、反射 adapter 和进程内 fallback claim 已删除；
- 仓库不再依赖无使用者的 LangGraph 包；
- rejection、terminal replay、deterministic action 仍严格 Provider/Driver 0；
- Provider/Tool 调用次数、多 ToolCall、消息、Pending、Ledger、Journal、Context 与 SSE golden
  通过；
- timeout、cancel、owner takeover、late Bundle 与 chained Pending 并发门禁通过；
- 无新增 Schema/API/UI/权限/Context 能力；
- 发布报告明确这是内部破坏性 Loop 切换，不声明全局 exactly-once；
- 独立 CR 无未关闭 P0/P1/P2。

本设计批准前只允许修改设计文档和设计阶段机械资产，不得修改生产代码或编写实施计划。设计
书面复审通过后，再使用测试先行方式单独编写详细实施计划。
