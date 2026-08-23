# Agent Loop Unification 发布验收报告

日期：2026-08-24
分支：`refactor/20260823-agent-loop-unification`
固定 baseline：`aaecf5dfa6ce913ecaf00b25a0e88bcf46096eeb`
批准设计提交：`9a049e8d8b573189e563fe47b74c9b904c6d1c5d`
Characterization / plan 提交：`888011e`

## 范围与结论

本项目将首次运行和 approve/modify continuation 切换到同一个显式 Agent Loop。`PilotRuntime` 只通过严格类型化的 `AgentDriver.execute(AgentLoopInvocation)` 进入 Loop；New Turn 与 Approved Write 使用不同 Seed bootstrap，随后共用同一个 `while`。Reject、terminal replay、delivery recovery 和 deterministic action 仍在 Loop 外保持 Driver/Projector/Provider 0。

内部切换是破坏性的，不保留 feature flag、shadow execution、旧路径 fallback 或双轨。HTTP/SSE、HITL、Pending、Ledger、Journal、Context Surface、Provider Tool Surface 与业务副作用的既有外部契约通过回归、受控 Provider 和隔离浏览器验收。

## Characterization → RED → GREEN 证据

- 在生产代码仍等于 `aaecf5d` 时固化只读 canonical golden；资产固定 baseline、ToolCall 选择矩阵、Provider/Driver 0 路径和既有 SSE/结果形状，没有 generator、overwrite 或 update 开关。
- 先为 `NewTurnSeed`、`ApprovedWriteSeed`、`AgentLoopInvocation`、closed events、Provider provenance、显式 Loop、Runtime start/approve cutover 和 deletion/privacy gate 写失败测试，再做一次生产切换。
- 独立 CR 发现的每一项 P0/P1/P2 均先由最小失败用例复现，再修复并跑 focused green，包括：
  - Pipeline decoder/binding/preflight/mutable/claim/executor 的 typed control exception 透传；
  - 未暴露工具在 Binding 前置 fail-closed，assistant/tool event/dispatcher/executor 为 0；
  - Provider attempt Session ownership、single-use 与 transient serialization；
  - failed streaming candidate delta 丢弃、pre-output fallback 和 sink 后禁止 fallback；
  - fallback 前 cancel/delivery-fence checkpoint；
  - Provider 返回后、对外 delta flush 前的 active checkpoint；
  - final/pending `AgentTurnResult` 返回前统一校验 cancellation 与 delivery fence；
  - raw `BaseException` 的 attempt 清理和原对象传播；
  - strict `AgentTurnResult`、Approved Port 隐私、Seed 深拷贝与唯一 Pending snapshot。

最终 affected focused suite：`196 passed`。

## 实现内容

- 新增瞬态、严格类型化的 Agent contracts、New/Approved Seeds、Invocation、closed Agent events 和 `AgentTurnResult`。
- 新增一个显式 `AgentLoopRunner`：首次运行和确认续跑共用唯一 model/tool `while`；多 ToolCall 保持 all-read 全执行、任含 write 只保留原始第一项。
- Approved bootstrap 在 executor 前 claim；terminal commit 和 delivery ownership 建立后只加载一次 continuation Source；chained Pending 继续由既有 delivery transaction 替换。
- Runtime sync/stream 的 start 与 approve/modify 路径统一调用 `AgentDriver.execute()`；删除反射式 Agent 双入口和参数 alias adapter。
- Provider Gateway 绑定 model call ID、surface fingerprint、候选 ordinal 和本 Session 生成的 attempt identity；attempt 只存在于瞬态调用栈。
- streaming Agent surface 使用 candidate-local deferred delta：失败候选内容不泄露；成功 response 完整绑定后才对外 flush；sink/control failure 不触发第二 Provider。
- Event Sink 普通异常统一映射为 `RuntimeTransportAborted`，不会重跑 Provider、Tool 或 executor。
- 删除 LangGraph、Graph State、interrupt/checkpointer、`_resume_without_checkpoint`、进程内 fallback claim 和旧 `ai.agent` 模块；移除 LangGraph 及无用传递依赖并重锁。
- 增加 AST/source/dependency/privacy/serialization deletion gates 及会被门禁拒绝的负 fixture。

## 内部破坏性变化

- `offerpilot.ai.agent` 及其 `run_turn` / `resume_after_confirm` / LangGraph API 被删除。
- `AgentDriver` 只保留 `execute(AgentLoopInvocation) -> AgentTurnResult`。
- Agent Runtime event sink 只接受 closed event union，不再接受任意 dict。
- Provider surface 调用必须经过 Frozen Surface/Gateway/Binding，并提供 request-scoped active check；伪造或跨 Session attempt 会 fail-closed。
- Invocation、Approved Port、result、event 和 Bound response 为不可 checkpoint/不可 pickle 的瞬态值。

未修改数据库 schema、公开 API、UI 或 Provider-visible Tool manifest。

## 外部兼容结果

- New Turn、approve 与 modify 的 sync/stream 路径保持既有 final/pending/SSE 形状。
- read/read、read/write、write/read、write/write 选择矩阵保持 baseline 精确语义。
- 写工具始终 HITL；`auto_approve` 不绕过确认。
- Reject、terminal replay、delivery recovery 和 deterministic action 保持 Driver/Provider 0。
- Provider fallback 仍复用同一 Frozen Surface 和 logical model call；取消、timeout/fence、sink abort 或已对外可见 delta 后不再切换候选。
- Ledger、confirmation claim、delivery fencing、Journal fail-open 和领域副作用回归保持兼容。

## 验证结果

| 门禁 | 结果 |
| --- | --- |
| Agent Loop / Context Projector / Pipeline affected suite | `196 passed` |
| 最终完整后端（排除外置 Application-JD scope gate） | `3314 passed, 4 skipped, 1 deselected, 4099 warnings`；42:29 |
| Application-JD scope gate | 外置前置条件未提供；单独运行如预期失败于缺少 `OFFERPILOT_APPLICATION_JD_BASELINE_FILE`，未伪造 baseline/allowlist |
| `uv run ruff check .` | 通过 |
| `uv run mypy src` | 通过，132 个源文件 |
| `uv lock --check` | 通过，82 个包 |
| `uv sync --locked` | 通过；移除 LangGraph 及其 19 个无用包 |
| deletion/cutover gates | `25 passed`；生产 source/dependency 扫描无旧 Agent/Graph/fallback claim 命中 |
| `npm test -- --run` | 181 个文件、1286 项测试全部通过 |
| `npm run build` | 通过，3950 modules transformed；保留既有 >1500 kB chunk warning |
| `uv run oc smoke --static-dir web/dist` | 通过 |
| `uv run oc verify --profile local --static-dir web/dist` | 通过 |
| `uv run oc verify --profile real-ai --static-dir web/dist` | 通过 |
| 隔离内置浏览器验收 | 通过 |
| `git diff --check` | 通过 |

前端测试保留仓库既有 React `act(...)`、DOM attribute 和 jsdom warning；没有失败、skip、pending 或 todo。构建仅保留既有大 chunk warning。

### 真实 Provider

最终版本直接读取既有本地 Provider 配置，不打印或修改 secret。隔离验收覆盖 health/settings/SPA、Application/Resume/Event CRUD、Interview Preparation、Material Proposal、Opportunity Fit triage/deep review、Interview Review、Knowledge Capture、bounded Mock Interview、Chat write confirmation、Pending 清理和最终数据清理。

### 内置浏览器

使用隔离临时数据库和本地 deterministic model 在内置浏览器完成：

1. 打开合成投递详情，验证 Haru/Pilot 正确携带 `Agent Loop Browser · Verification Engineer` application context。
2. 发送一次合成状态更新请求，观察 HITL confirmation card；写入前领域状态仍为 `applied`。
3. 确认后进入 Approved continuation，观察 `保存成功` 与最终 `http smoke complete`。
4. API/Repository 回读确认状态为 `offer`、Pending 已清空。
5. 服务日志证明恰好一次 `POST /api/chat/stream` 与一次 `POST /api/chat/confirm/stream`；浏览器控制台 error 为 0。

隔离服务已停止；仅含浏览器合成验收数据的临时目录已移入 Windows 回收站，浏览器验收本身没有写入用户正常 OfferPilot 数据库。

`oc smoke` 的既有 CLI 行为会使用正常数据目录。本次运行产生的 3 条 `Smoke Co` 申请与 9 个 smoke 会话已按精确 ID、时间和消息标记核验后通过公开 API 清理；申请均已软删除，会话、消息和 Journal 已移除，外键检查为 0。9 条 Write Operation Ledger 审计记录依照既有不可变约束解除 Conversation 关联后保留。

## 独立 Code Review

独立 reviewer 对 baseline→最终工作树 diff 进行了多轮 P0/P1/P2 审查，覆盖 exposed-tool fail-closed、Gateway provenance/fallback、单 Loop/单入口、Approved claim/source 时序、sink abort、ToolCall 选择、Provider-free 路径、Graph/dependency 删除与 transient privacy。

已关闭 findings 包括 strict result/port privacy、typed cancellation、Seed identity、surface wrapping、Pipeline control mapping、deferred delta fallback、fallback active checkpoint、返回前 active checkpoint 和 raw `BaseException` attempt cleanup。最终结论：无开放 P0/P1/P2。

## 剩余风险与明确非目标

- Application-JD aggregate scope gate 依赖 release orchestrator 提供的外部 baseline 与 allowlist；本分支没有生成替代文件，因此该门禁不是“通过”状态。
- 现有 Write Operation Ledger 提供既有边界内的幂等/收敛能力；本项目不声明跨请求、跨进程、跨 Provider 或跨外部系统 exactly-once。
- Journal 继续 fail-open；诊断记录降级不改变 Provider、Tool、HITL 或业务结果。
- 浏览器验收使用 deterministic local model 验证 UI/HITL/continuation；真实 Provider 行为由独立 `real-ai` verify 覆盖，随机文本不作为断言。
- Scoped Capability、Tool Metadata Convergence、Memory/Knowledge/Summary contributor、SSE replay、多 Agent/插件/wakeup queue 均不在本期范围。

## 最终提交与状态

实现与本报告将由本报告所在的最终本地提交承载；精确 SHA 在提交后验收回执中记录。分支保持 `refactor/20260823-agent-loop-unification`，未 push、未 merge。
