# Tool Metadata Convergence 发布验证报告

## 验证身份与结论

- 分支：`refactor/20260825-tool-metadata-convergence`
- 固定 baseline：`0c10e05e256eb757d5f89a8b009dcea193f2fc78`
- implementation-start：`bf879fd8f10575e3996bacf8ff9c6ccc8ab7cc64`
- 报告前最终实现提交：`63473955b273bd76f41a5c362af0455eb5dad7e4`
- 验证日期：2026-08-29（Asia/Shanghai）
- 结论：`ToolMetadataBundleV1` 已成为 Typed Tool、Legacy deterministic Tool 和 Compensation Operation 元数据、能力、Binding、确认、Undo、领域、依赖与写入分类的唯一生产事实源。发布边界精确保持为 `25 Typed / 3 Legacy / 4 Compensation`，独立 CR 无剩余 P0/P1/P2。

## 实现收敛

- 新增不可变、启动期编译并验证的 Manifest、Metadata、Runtime Binding、Presentation、Authority View、Operation/Pending route、Primary Undo、Compensation、Legacy initial route 和 Legacy confirmation proof 契约。
- Task 7–8 的 Legacy issuer/proof 在各自 Task 内保持未发布；Task 9 原子发布完整 Bundle 与 Composition，Task 11 完成 Pending/Ledger/Legacy confirmation 的生产切换。
- Provider discovery 和 Legacy discovery 分别通过 canonical manifest seal 校验；Bundle/View/Segment/route handle 均携带当前生产组合的来源与身份约束，跨 Bundle、跨 Segment、跨 registry 或失效 lease 会 fail closed。
- Typed 写入、Legacy 确认恢复和 Compensation 仅通过精确 Operation/Pending/Proof route 执行；Legacy `PreparedLegacyInputV1` 由唯一 owning Port 从同一 live prepared call 投影 canonical args、encoded args 与 confirmation 文本。
- Confirmation presentation、success summary 和 required Undo 由具体 `ToolSpec` binding 提供，不再通过工具名、结果 key 或旧分类集合选择。
- 删除旧全局名称集合、双 Catalog/Registry、隐式补全、fallback、shadow、compatibility façade 和运行时 name-based classification；机械 AST/source gate 覆盖定义、alias、反射、subclass 和死依赖绕过。
- Golden 资产固定 baseline 来源、25 个 Typed 工具顺序、3 个 Legacy adapter、4 个 Compensation operation、4 个 required Undo binding、Provider payload、resolver implementation binding、选择与操作矩阵。

## 外部兼容与破坏性变化

- Provider、HTTP/SSE、HITL、Ledger、Journal、数据库业务写入和公开 API 的外部行为保持兼容。
- 未修改 Schema、Migration、UI 或公开 API；未引入本地数据迁移或 reset。
- 破坏性变化限于内部 Python 构造与扩展点：旧 `ToolSpec` shape、旧名称集合、旧 Catalog/Registry、raw Pending/Legacy adapter overload 及兼容 façade 已删除。内部调用者必须使用最终 Bundle/View/route/proof 契约；非法或漂移配置在启动或边界验证时 fail closed。

## 后端与静态验证

### Task 13 聚焦矩阵

| 命令 | 结果 |
| --- | --- |
| `uv run pytest tests/tool_metadata tests/tool_pipeline -q` | 960 passed，1 warning，669.16s |
| `uv run pytest tests/tool_authority -q` | 642 passed，53 warnings，995.51s |
| `uv run pytest tests/test_context_projector.py tests/test_context_projector_source_gates.py -q` | 95 passed，5 warnings，34.70s |
| `uv run pytest tests/agent_loop tests/pilot_runtime -q` | 583 passed，61 warnings，1672.54s |
| Write Operation / Ledger / Chat API 兼容矩阵 | 467 passed，2237 warnings，6774.79s |

### 完整 Windows pytest gate

- `uv run pytest --collect-only -q` 在精确排除一个外部 Application-JD scope node 后收集 5001 个节点；manifest SHA-256 为 `bd8c48127f7fee7219e7ce662e799d840e67ef8cb945c0143fb69e751c54535a`。
- Windows group manifest/aggregate 与 collection union 精确一致：4997 passed、4 个批准的 Windows symlink skips、1 个明确 deselect，聚合输出为 `All pytest groups passed; coverage matches 5001 tests.`。
- 分组结果：agent 480 passed；domain 131 passed；knowledge 655 passed + 4 skipped；proposals 434 passed；misc 3297 passed + 1 deselected。
- 最终支持的完整后端运行：`4997 passed, 4 skipped, 1 deselected, 4283 warnings in 13727.40s (3:48:47)`。
- 首次 aggregate 发现 `test_legacy_confirmation_proof.py` 的三个 `uuid4()` 参数导致跨 collection node ID 漂移；提交 `6347395` 改为三个固定、合法 UUID。两次 collection 18/18 完全一致，相关参数化测试 18 passed。

外部 Application-JD release-orchestrator 输入确实不可用：`OFFERPILOT_APPLICATION_JD_BASELINE_FILE`、`OFFERPILOT_APPLICATION_JD_ALLOWLIST_FILE` 均未设置，locator 也不存在，因此按计划只排除：

```text
PYTEST_ADDOPTS=--deselect=tests/test_application_jd_browser_harness.py::test_application_jd_implementation_scope_is_machine_checked
```

未伪造或借用旧 `%TEMP%` 文件，且不声明该外部门禁通过。

### 静态验证

| 命令 | 结果 |
| --- | --- |
| `uv run ruff check .` | `All checks passed!` |
| `uv run mypy src` | 147 source files，无问题 |
| `git diff --check` | 通过；仅工作区 LF/CRLF 提示 |
| 独立 CR 聚焦回归 | provenance、删除门禁、SQL CHECK、Operation/Pending route 共 125 passed，223.45s |

## 前端与本地验证

- 首次精确 `npm test -- --run`：183 files passed、1 file failed；1325 tests passed、1 test failed。唯一失败为历史 Haru Surface 项目的冻结 scope gate，它按自己的旧 allowlist 正确拒绝本分支 138 个 Tool Metadata 路径。
- 支持的前端全量验证将该单一外部门禁文件从主 run 排除，再对同文件其余五项测试使用负向 test-name filter：183 files / 1320 tests 通过，加 5 tests 通过；总计 1325 tests 通过，仅排除以下历史 scope node：

```text
src/features/assistantSurface/assistantSurfaceGate.test.ts >
Haru Desktop Surface Completion gate >
rejects every committed, staged, unstaged, and untracked path outside the allowlist
```

- `npm run build` 通过：3954 modules transformed，3m03s；保留既有 1.55 MB 主 chunk 警告。
- `uv run oc smoke --static-dir web/dist` 通过 health、SPA、write confirmation、Pending clear 和 confirmation card smoke。
- 计划中的 `uv run oc verify --local` 与当前 CLI 不兼容并返回 `No such option: --local`；当前受支持的等价命令 `uv run oc verify --profile local --static-dir web/dist` 全部通过，包括 HTTP health/settings/SPA、resume 与 application-event CRUD、proposal terminal matrix、provider-free outcome、Chat Pending/confirm 和 cleanup。
- `oc smoke` 会写入配置的数据目录。此次创建的 application `#259` 与 conversations `#623..625` 已通过公开 API 精确清理；Conversation 与 Message 已删除，Application 按产品删除语义保留 soft-delete tombstone，不可从 UI/API 读取。

## Controlled real-AI 与浏览器兼容验证

### Real-AI HTTP gate

使用隔离临时数据目录和 redacted audit 运行：

```text
uv run python scripts/full_real_ai_verify.py --static-dir web/dist \
  --report-dir %TEMP%/offerpilot-tool-metadata-task13-real-ai-9f2545260697 \
  --model deepseek-v4-flash --timeout-seconds 900
```

- 结果：passed，exit 0，516974ms；17 次 Provider request、89 条 operation audit，request metadata 完整。
- Provider profile：`openai_compatible` / `deepseek-v4-flash`；未记录凭据或模型原文。
- interview preparation、material proposal/revision、opportunity fit、interview review、knowledge capture、mock interview 与 Chat write-confirm smoke 均完成。
- `repair_attempted=false`、`retry_count=0`、无 failure category；最终 `http_cleanup` 完成，正式配置 fingerprint 未改变。

### Tool Metadata 兼容矩阵

确定性 sync/SSE 与恢复矩阵覆盖 workspace/application context、read+read、write+read、read+write、write+write、approve、modify、reject、chained Pending、terminal replay/delivery recovery、四个 Legacy initial sources 和三个 Legacy confirmation adapters：46 passed，145 warnings，898.97s。该矩阵同时断言 Provider attempt、executor count、Pending/Ledger state、业务 readback、proof order、replay 与 recovery。

### 内置浏览器

- 使用 `OFFERPILOT_DATA=%TEMP%/offerpilot-tool-metadata-task13-browser-isolated` 启动真实构建并通过内置浏览器走查 dashboard、Haru 轻量入口与 Pilot 工作区。
- 共捕获三张会话内截图；页面结构、未配置 Provider 的禁用态、上下文 badge 与展开路径符合预期。
- 控制台 error 为 0。隔离页 reload 捕获 8 个 bootstrap API GET，每个 endpoint 精确一次；无 Chat/SSE 请求，也无重复 API/SSE 调用。
- 浏览器验收服务已停止。浏览器未注入真实 Provider 凭据，因此 real-AI 为上面的隔离 HTTP gate，不能扩写为真实浏览器端 Provider/SSE 全矩阵。

## 独立 Code Review 与不可变范围

- 最终独立 CR 以 `0c10e05e..6347395` 为范围，核对 exact 25/3/4、Provider seal、Bundle/View provenance、ToolSpec consumer、Operation/Pending handles、Legacy owner lease/proof、PreparedLegacyInput、caller-owned Session、SQL CHECK、HTTP/SSE/HITL/Ledger/Journal、replay、privacy 与删除门禁。
- 结论：无剩余 P0/P1/P2；未发现 dual registry、Typed/Legacy fallback、shadow/double execution、compatibility façade 或 runtime name-based classification 绕过。
- locator、worktree、branch、baseline、implementation-start 和全部 13 个 task gate 精确匹配。
- Tasks 1–12 union 为 141 个路径；Task 13、全体 task union 与 allowlist 均为 144 个路径，差集为 0；allowlist/task-13 SHA-256 均为 `e985bb473b05c9fa7f6dfa4028b220746aa221f7b259fc5bbb589422e7e5fa11`。
- 固定 baseline 到报告前实现提交共 138 个 changed paths；allowlist 与 task-13 外路径为 0。

## 剩余风险与非目标

- 外部 Application-JD scope gate 因 release-orchestrator 输入缺失而明确排除；历史 Haru Surface scope gate因其冻结 allowlist 不包含本项目路径而明确排除。两者均不是本实现的产品测试失败，也均未被宣称通过。
- Windows 的 4 个 symlink 用例为批准的环境 skip；完整 suite 仍输出既有 FastAPI/Starlette deprecation、React `act(...)`、jsdom 和前端 chunk-size 警告。
- 浏览器只验证真实本地构建、API 请求拓扑与未配置 Provider 状态；真实 Provider 的业务流程由隔离 HTTP gate 覆盖，而非浏览器端 live Provider loop。
- 本期保证限定在当前请求/Operation、Ledger、Pending、delivery fencing 与 replay 契约；不声明跨请求、跨进程或外部系统 exactly-once。
- 不修改 Schema、Migration、UI 或公开 API；不扩大到新工具、新业务能力或 Provider prose snapshot。

## 完成声明

`ToolMetadataBundleV1` 的唯一事实源、启动验证、生产发布、执行授权、Legacy proof、Pending/Ledger 切换和旧路径机械删除均已完成。外部兼容边界保持不变，已知排除和非目标如上，不作全局 exactly-once 声明。
