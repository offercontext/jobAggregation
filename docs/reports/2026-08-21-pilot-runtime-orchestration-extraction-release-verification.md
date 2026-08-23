# Pilot Runtime 编排提取发布验证

日期：2026-08-23
分支：`refactor/20260821-pilot-runtime-orchestration`
固定实施 baseline：`735f8866ae655c9bb37ac31a14f77a834a8e1e37`
源码 baseline：`b05d915bbb52b2740f6801b4ec46ee8f4ccda2e2`
最终实现提交：`3c63bab`

## 结论

Task 0–12 的代码实现与既定验证矩阵已经完成。四条 Chat 路由已一次性切换到统一 `PilotRuntime`；`api.py` 不再保留旧可靠性编排、feature flag、shadow、fallback 或 façade。`chat_transport.py` 负责 HTTP/SSE、执行宿主、超时取消、Guard 与标题信号；`pilot_runtime` 负责因果状态机以及 Pending、Ledger、Journal、Context 编排。

外部 HTTP/SSE、Provider、工具、HITL 和业务副作用契约没有计划内变化。内部模块边界是破坏性重构：依赖 `api.py` 旧私有编排符号或旧内部调用顺序的非公开代码不再兼容。

## 范围门禁

固定 allowlist 共 26 条，外部冻结文件 SHA-256 为 `6b31d63527605199597b7a62727f1257fdb60c707578a55ac80269f8452e8e76`。

- 报告写入前，baseline 到 `3c63bab` 有 25 个变更路径，全部位于 allowlist。
- 加入本报告后共有 26 个变更路径，仍全部位于原始 allowlist；allowlist 未扩展。
- 未修改已批准的设计和实施计划。
- 生产代码仅改动 `api.py`，并新增 `chat_transport.py` 与 `pilot_runtime` 包；其余变更均为批准的测试、golden 和本报告。

## 后端与静态验证

| 命令 | 结果 |
| --- | --- |
| `uv run pytest tests/pilot_runtime tests/test_pilot_runtime_extraction_gate.py tests/test_chat_api.py tests/test_ai_agent.py tests/test_context_projector.py tests/test_agent_run_budget.py tests/test_agent_run_journal.py tests/test_agent_runs_repository.py tests/test_journal_active_work_budget_gate.py -q` | 最终通过：`1256 passed`，`2169 warnings`，706.60 秒 |
| `uv run pytest` | 最终除外部门禁外通过：`3273 passed, 4 skipped, 1 failed`，`4099 warnings`，2184.65 秒 |
| `uv run ruff check .` | 通过 |
| `uv run mypy src` | 通过：129 个 source files 无问题 |
| `git diff --check` | 通过 |

全量 pytest 唯一失败是 `tests/test_application_jd_browser_harness.py::test_application_jd_implementation_scope_is_machine_checked`。当前 release-orchestrator 环境同时缺少 `OFFERPILOT_APPLICATION_JD_BASELINE_FILE` 和 `OFFERPILOT_APPLICATION_JD_ALLOWLIST_FILE`；测试首先报告前者缺失。本分支没有伪造这两个独立门禁输入，也不声称该外部 Application JD 门禁通过。4 个 skip 是仓库批准的平台/外部环境 skip。

现有 FastAPI/httpx 弃用告警仍存在；它们未由本次重构引入，也未被屏蔽。

最终 focused matrix 的前一次执行出现 1 次既有 SQLite 并发测试 `test_concurrent_seq_allocation_has_no_gaps_or_duplicates` 的 `database is locked`；该测试未在本次 allowlist 内，原样单独复跑通过，随后完整 focused matrix 复跑 `1256 passed`。没有为此扩大 allowlist 或改动 Journal repository。

## 前端、构建与本地验收

| 命令 | 结果 |
| --- | --- |
| `npm ci` | 成功安装 lockfile 固定的 478 个包 |
| `npm test -- --run` | 通过：166 个文件、1222 个测试，493.11 秒 |
| `npm run build` | 通过：3941 modules，18.92 秒 |
| `uv run oc smoke --static-dir web/dist` | 通过：8 项静态/HTTP/Chat/HITL 检查 |
| `uv run oc verify --profile local --static-dir web/dist` | 通过：16 项本地验证 |
| `uv run oc verify --profile real-ai --static-dir web/dist` | 通过 |

前端首次执行因本地尚未安装 Vitest 依赖而无法启动；运行 `npm ci` 恢复 lockfile 声明的环境后，完整测试与构建均通过。`npm ci` 报告现有依赖审计项：3 个 moderate、7 个 high、3 个 critical；未运行会破坏 allowlist/依赖契约的 `npm audit fix`。构建保留现有约 1503.26 kB 大 chunk 告警。Vitest 保留既有 React `act`/jsdom 告警。

Docker daemon 当前不可用（本机 `dockerDesktopLinuxEngine` pipe 不存在）；批准计划未包含 Docker gate，因此没有声称 Docker smoke 通过。

## 可靠性与 real-AI 证据

受控 local 与 real-AI 验证覆盖 workspace 同步/流式 Chat、application-scoped Chat、read + read multi-call、写入确认、Pending 清理与最终业务读回；完整 real-AI 还覆盖面试准备、材料建议、Opportunity Fit triage/deep、面试复盘、Knowledge capture 与模拟面试。没有修改或输出 Provider 配置、secret、prompt、answer、JD、resume 或用户数据。

可靠性观测结论如下：

- Provider/工具调用：正常 read、多步 read、write proposal、确认后 continuation 均按预期完成；拒绝、terminal replay、前置 Source 失败和 CAS 输家路径由 focused tests 证明 Provider/executor 为零。
- Pending/Ledger：approve、modify、reject、chained Pending、重复确认和 terminal replay 均收敛；local/real-AI 与浏览器流程结束时无待确认卡残留，最终业务状态只写入一次。
- Journal：enabled、disabled 与 degraded/fail-open 路径均通过；验证中没有未解释的 Trace anomaly。Journal 仍是诊断层，不被描述为业务 exactly-once。
- Context Surface：同步/流式、首次请求/确认恢复保持同一冻结 Surface fingerprint；门禁断言 fingerprint 相等，报告不保存上下文原文或 payload。
- HTTP/SSE：同步返回、流式事件顺序、terminal replay、delivery recovery、heartbeat/fencing、超时与取消均通过；30 次独立真实 SSE 回归全部 `200/completed`，无残留线程。
- 资源清理：knowledge、Context、Journal engine 与主数据库按独立 cleanup 所有权释放；异常时保留第一个异常，Windows 临时数据库可删除。

这些结论同时由 AST、golden、privacy canary、并发、零迭代 Guard、unbounded Queue、动态 Prepared handle 与旧路径删除门禁固定。测试未把 Provider 或 native call 描述为可强制中断；取消后的 late result 只能被 fencing 丢弃。

## 内置浏览器验收

内置 Codex 浏览器连接隔离的本地构建与合成数据，完成了：

1. workspace 流式 Chat，并显示最终回复；
2. application-scoped Chat，显示页面/投递 Context，并完成两次 read 工具调用；
3. HITL modify 后 approve，最终读回修改后的 `Offer` 状态；
4. 二次写入的两步 reject，状态保持 `Offer`，且显示确定性取消结果；
5. 未编辑的普通 approve，最终读回 `interview`，Pending 卡消失并显示保存成功；
6. 流式请求建立后关闭标签页，再从新标签页加载同一服务；健康页正常、全新页面无 console error，证明用户侧 SSE disconnect 后服务可继续使用。

仅保存合成截图到仓库外目录：

`D:\Users\yuqi.chen\AppData\Local\Temp\offerpilot-pilot-runtime-gate\verification`

保留的截图为 `01-workspace-stream-chat.png`、`02-application-multicall-chat.png` 和 `03-confirm-modify-approve-readback.png`。隔离服务、worker、数据库与端口均已关闭清理。

浏览器安全策略拒绝了为“malformed stream request”创建 `data:` 页面，且明确禁止用 CDP/其他浏览器表面规避。因此 immediate preheader HTTP error 没有生成独立的浏览器截图；该边界由真实 ASGI/HTTP transport 测试与 extraction gate 覆盖，验证响应在 SSE header 之前保持普通 HTTP 错误。此项记录为验收环境限制，不将其表述为浏览器可视化通过。

## 独立代码审查

最终独立审查覆盖 `735f8866..3c63bab` 的完整 diff，并明确检查 HTTP-before-SSE-header、Source-vs-Run 顺序、Guard/CAS、Queue/timeout owner、异常分类、provider-zero/replay-zero、delivery fencing、Journal fail-open、Surface fingerprint、旧路径删除与无隐式 fallback。

初审发现两个 P1，均以测试先行修复：

1. SSE 模型路径错误地用外层 `SseAgentExecutionHost` 包住整个 Runtime，再嵌套 `SyncAgentExecutionHost`，使 Agent 返回后的 persistence/Journal 也受外层 deadline 约束。修复后，transport-owned `_SseRuntimePump` 只负责无界事件 Queue 与 Runtime worker，不设置 deadline；唯一的 `SyncAgentExecutionHost` 只计时 Agent thunk。
2. pump disconnect 最初只取消独立 outer control，prepared Runtime control 仍可能保持 ACTIVE 并在稍后 Agent deadline 分支写入 timeout 消息。修复后 pump close 先把 prepared control 原子转为 `TRANSPORT_ABORTED`，再取消 outer worker，迟到 Agent 结果不能进入普通 timeout 持久化。

两项回归先分别稳定 RED，再转为 GREEN；67 个 transport/host 定向测试通过，最终 focused matrix 通过。复审结论为 `APPROVED`，没有剩余 P0/P1/P2。

Task 10/11 的两次独立复审均已给出 APPROVED；其中最终回归分别覆盖 718/720 个 focused tests、动态 AST bypass probes、30 次真实 SSE、隐私 canary 和 Windows engine cleanup。

## 破坏性变化与剩余风险

破坏性变化：

- 旧 `api.py` 私有可靠性编排与内部符号被删除；内部调用方必须使用 `PilotRuntime`/transport 新边界。
- 四条路由一次性切换，没有 feature flag、shadow、fallback 或 façade。
- 对外 HTTP/SSE、Provider、工具、HITL 与业务副作用契约无计划内破坏性变化。

剩余风险：

- Python/native/Provider 已进入的不可取消调用可能继续运行；deadline、Guard、fencing 与 late-result 丢弃限制其后续副作用，但不承诺物理中断。
- Application JD 聚合 scope gate 缺少 release-orchestrator 的 baseline/allowlist；本地全量 pytest 因此不是全绿。
- 系统只在受控业务边界内提供幂等、CAS 与 Ledger 收敛，不作跨 Provider、数据库和外部系统的全局 exactly-once 声明。
- 浏览器安全策略阻止了单独构造 malformed preheader 请求；该边界有自动化 HTTP/ASGI 证据，但缺少浏览器截图。
- 现有前端依赖审计项、大 bundle 与测试告警不在本次固定 allowlist 内。

本报告不授权 push 或 merge。
