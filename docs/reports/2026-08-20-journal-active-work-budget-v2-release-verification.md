# Journal Active Work Budget V2 发布验证

## 版本与范围

- 固定源 baseline：`4a354f9d58e2eb8b0800059b4532cc6e78235c80`
- 实施 baseline：`51f35be659717df2f2cec52ba77ad76251feeca4`
- 最终生产与测试实现提交：`78a3f0b8e12a067860b233913646f836da06b9d1`
- 分支：`fix/20260820-journal-active-work-budget`
- immutable allowlist SHA-256：`2093651dab79d4a4cc7afa853c1683e7e9df29bb7eca763f255dd5db55b5b49c`
- scope gate：通过；相对实施 baseline 的 12 个变更路径全部位于批准 allowlist 内。

本次没有数据库 Schema、迁移、API、SSE、Provider、Tool Pipeline、Ledger、业务领域或前端行为变更。

## 实现保证

- Segment 的 150 ms 限制现在是累计 active-work budget。Provider、工具、业务 Repository、确认等待以及普通调用间隙不计入预算。
- 每次 Journal Operation 都获得独立 lease，最大 active-work 为 50 ms，其中保留 5 ms cleanup；所有已经开始的 active path 都在 `finally` 中按真实耗时计费。
- SafeClockAdapter 对异常、回退、NaN、正负无穷、bool 和非数值样本 fail closed，SQLite progress callback 不向外抛 Python 异常。
- canonical JSON、HMAC、ordered digest 和 Manifest V1/V2 使用固定 4096-byte 分块并设置预算检查点，golden digest 保持不变。
- 普通 SQLite Journal 操作按剩余 lease 动态设置 `busy_timeout`，安装 progress handler，并在成功、失败、超时与 BaseException 路径恢复或失效连接；ABA 复用门禁已覆盖。
- resume/finalizer 采用显式并发状态机与 post-lock fencing，确保只产生一个合法终态，invalid clock 不会污染已完成 disposition。
- `append_event_bound(caller_session, ...)` 保留为 `tool.started` 的唯一 caller-owned Session 窄例外；不安装 progress handler、不修改 PRAGMA，也不接管 caller 的 commit、rollback、close 或 invalidate。
- AST 门禁验证预算/时钟所有权、caller-owned 调用点、方法与装饰器清单，以及旧 wall-deadline 路径删除。

### 已知实现边界

SQLite progress handler 只能在 SQLite VM 指令边界中断。单个阻塞 UDF、driver/native call 或不可中断的系统调用不能在 50 ms 时被抢占；调用返回后仍会在 `finally` 中全额计费、标记 degraded，并停止后续非终态 Journal 工作。caller-owned 窄例外同样不获得 progress-handler 级抢占。

## 验证结果

### 聚焦后端门禁

- Journal/Repository/Trace/Context/Tool Pipeline/Chat 完整聚焦集：`642 passed`，`2129 warnings`，用时 `613.87s`。
- `uv run ruff check` 聚焦路径：通过。
- `uv run mypy` 聚焦路径：通过，8 个 source files 无问题。
- `git diff --check`：通过。

### 全量本地门禁

- `uv run pytest -q`：`2898 passed, 4 skipped, 1 failed, 4059 warnings`，用时 `1964.53s`。
- 唯一失败为既有、与本改动无关的发布编排前置条件：`test_application_jd_implementation_scope_is_machine_checked` 缺少外部提供的 `OFFERPILOT_APPLICATION_JD_BASELINE_FILE`（同时需要配套独立 allowlist）。本任务没有伪造该独立门禁输入。
- `uv run ruff check .`：通过。
- `uv run mypy src`：通过，119 个 source files 无问题。
- 前端 `npm test -- --run`：166 个 test files、`1222 passed`。
- 前端 `npm run build`：通过；Vite 构建成功，保留既有大 chunk 警告。
- `uv run oc smoke --static-dir web/dist`：通过，health、SPA、application、Chat pending/confirm 与卡片 smoke 全部成功。
- `uv run oc verify --profile local --static-dir web/dist`：通过。

安装锁定前端依赖时 `npm audit` 报告 13 个既有依赖漏洞（3 moderate、7 high、3 critical）；未执行可能产生破坏性升级的 `npm audit fix`，且 `package-lock.json` 未变化。

### Runtime 与真实 Provider

- `uv run oc verify --profile real-ai --static-dir web/dist`：通过；覆盖真实 Provider 的面试准备、材料提案、Opportunity Fit、面试复盘、知识确认、模拟面试，以及 Chat 写操作 HITL。
- 另行运行隔离的真实 Provider sync Chat：首次 Provider 往返 `7.907s`，大于 2 秒；返回 `confirmation_required`，批准后业务状态回读为 `offer`，pending action 已清除。
- 同一真实运行最终 `Journal status=completed`、`recording_status=healthy`、Trace `integrity_status=healthy`、`completion_status=terminal`，且没有 `model_call_incomplete`。

## 独立 Code Review

最终独立 CR 结论为 `APPROVED`，无开放 P0/P1/P2。审查发现并通过测试先行修复了：

- cleanup 普通异常错误压制 cleanup BaseException；
- SafeClock finite 样本验证缺口；
- HMAC/SHA 非固定 byte chunk；
- caller-owned conflict/lock 故障注入假阳性；
- Chat/HITL/Context integration 的异步观测与真实时钟抖动。

非阻塞 P3：AST gate 已覆盖当前生产路径，但没有穷举 `operator.attrgetter("repository")` 一类动态反射绕过。当前代码没有该模式，且生产模块清单、调用点和旧路径删除门禁均已通过。

## 破坏性变化与剩余风险

- 破坏性变化：无。Journal 是 fail-open 观测子系统；对外 API/SSE/业务结果保持等价。
- 剩余风险：不可中断 native call 只能返回后计费；外部 Application JD scope gate 缺少独立发布编排输入；前端依赖审计存在既有告警；AST 动态反射穷举属于后续加固项。
- 未 push、未合并。
