# Journal Active Work Budget V2 设计

日期：2026-08-20

状态：**待复审**

分支：`fix/20260820-journal-active-work-budget`

固定基线：`4a354f9d58e2eb8b0800059b4532cc6e78235c80`

## 1. 背景与问题

Durable Execution Journal 当前为每个 Segment 配置 150 ms fail-open 预算，并为最终 terminal / suspended disposition 保留一次独立 50 ms 收敛尝试。确认恢复时，`run.resumed` 是新 Segment 的 ingress transition；该 Segment 后续仍需要一次 terminal 或 suspended disposition。Journal 失败不会阻塞 Provider、工具、确认、Ledger 或业务写入，这一总边界保持正确。

当前实现的问题在于：150 ms 使用 Segment 创建时的墙上时间计算。

```text
segment_started_at
→ segment_deadline = segment_started_at + 150 ms
→ Provider / Tool / 用户确认 / 业务 Repository 等待
→ 后续 Journal 调用继续检查同一个 deadline
```

因此，Provider 即使只等待几百毫秒，Journal 数据库和 HMAC 处理本身完全健康，后续 `model.completed`、工具事件和终态前事件也会因为墙上 deadline 已过而进入不可逆 degraded。Phase 2 的真实 Provider 验收已经观察到六次业务成功但 Journal Trace 不完整的运行。

这不是 Journal fail-open 失效，而是预算衡量对象错误：预算应约束 Journal 给主链路增加的工作和等待，不应约束 Agent Segment 的总生命周期。

本项目只替换预算模型，不改变 Journal Schema、事件语义、Run/Segment 身份、Trace 分类、API、SSE、Tool Pipeline、Write Operation Ledger 或 Agent 行为。

## 2. 目标与非目标

### 2.1 目标

1. 将每个 Segment 的 150 ms 从墙上 deadline 改为累计 Journal active-work budget。
2. 每个公开 Journal 操作具有独立的 50 ms hard deadline，不能一次耗尽整个 Segment 预算；该 deadline 强制约束可中断工作，不虚假承诺抢占任意阻塞 native 调用。
3. Journal 锁等待、连接获取、SQLite 执行、canonical JSON、HMAC、Manifest 投影和 Repository 工作都计入 active work。
4. Provider、工具、业务 Repository、用户确认和两次 Recorder 调用之间的等待不计入 active work。
5. SQLite `busy_timeout`、查询中断和所有 CPU 密集循环使用当前 Operation 的动态剩余预算。
6. 任何成功、普通异常、预算异常或 `BaseException` 路径都在 `finally` 中扣减实际耗时。
7. 预算耗尽后保持不可逆 degraded latch，跳过后续非终态记录。
8. terminal、suspended 与 abandon 保留一次独立 50 ms disposition convergence；`resume()` 作为新 Segment ingress 单独计费且不消耗最终 disposition 权。
9. Journal 启用、禁用、锁定、失败和降级时，业务响应与副作用继续等价。

### 2.2 非目标

本期不实现：

- 新表、迁移、新事件类型或 Event Schema 版本；
- Prompt、回答、参数、结果或异常原文持久化；
- API、SSE、前端、Provider、Tool Schema 或 Tool Outcome 变化；
- Agent Loop、Runtime Orchestration 或 Context Projector 重构；
- Journal 后台队列、异步批量写入或跨进程 Recorder；
- 扩大或新建 Journal 与业务事务的原子提交范围；既有 `tool.started` caller-owned Session 窄例外必须保留同 commit、同 rollback；
- 业务 exactly-once、SSE replay 或 Event Sourcing；
- 改变 `healthy / degraded`、Trace anomaly 或 fail-open 语义；
- 调整 Journal 独立 SQLite Pool 的大小或启用 WAL。

## 3. 固定预算契约

### 3.1 常量

```text
JOURNAL_SEGMENT_ACTIVE_BUDGET_SECONDS = 0.150
JOURNAL_OPERATION_HARD_CAP_SECONDS    = 0.050
JOURNAL_OPERATION_CLEANUP_RESERVE     = 0.005
JOURNAL_DISPOSITION_BUDGET_SECONDS    = 0.050
JOURNAL_SQLITE_PROGRESS_STEPS         = 100
JOURNAL_DEFAULT_BUSY_TIMEOUT_MS        = 50
```

这些是代码级版本化常量，不读取用户配置，不新增 API 或设置项。测试可以显式注入更小值和可控 monotonic clock。

### 3.2 Active work 的定义

以下时间计入 150 ms 累计预算：

- Recorder / Factory 公开 Journal 调用入口后的有界串行等待；
- Journal 专用 Pool checkout；
- Event、Context Snapshot、Manifest 和 disposition command 的构建；
- canonical JSON 遍历、编码与大小检查；
- SHA-256、HMAC、fingerprint 和 digest 计算；
- SQLite `busy_timeout` 锁等待；
- Journal SQL 查询、CAS、seq 分配、事务和 commit / rollback；
- 幂等 replay 查询；
- degraded 状态的 best-effort 持久同步；
- 异常清理和 Journal 连接状态恢复。

以下时间不计入 150 ms 累计预算：

- Provider 网络、fallback 和流式等待；
- Tool executor 与业务 Repository；
- Pending Action 的用户确认等待；
- Write Operation Ledger、delivery continuation 和 heartbeat；
- HTTP / SSE 传输；
- 两次 Recorder 调用之间的任意 wall-clock 时间；
- terminal / suspended / abandon 使用的独立 disposition convergence。

调用方不得通过“暂停 ActiveWorkBudget”包裹一段任意代码。唯一计费边界是 Recorder / Factory 自己控制的公开 Journal 操作，防止遗漏或把业务工作错误计入。

### 3.3 单次 Operation

一个 Journal Operation 是一次公开 Recorder / Factory 调用所触发的全部 Journal 工作。例如：

```text
start_run(builder + create Run/Segment)
resume_waiting_run(lookup + builder + start Segment)
append_event(prepare + insert/replay)
prepare_event_draft(CPU-only canonical EventDraft)
append_prepared_event_bound(caller-owned SAVEPOINT insert)
capture_context(prepare + snapshot/event transaction)
capture_surface_context(HMAC + V2 Manifest + transaction)
attach_input_message(CAS/replay)
fingerprint_model_id(HMAC)
fingerprint_pending_identity(HMAC)
mark_degraded(best-effort persistence)
```

一次 Operation 从进入公开方法开始计时，包括等待同一 Recorder 的前一 Journal Operation 结束。取得串行执行权后，按最新累计余额形成两个 deadline：

```text
operation_hard_deadline
= operation_entry_monotonic
  + min(50 ms, segment_active_budget_remaining_at_execution)

operation_work_deadline
= operation_hard_deadline - 5 ms cleanup reserve
```

如果在获得串行执行权前已耗尽 hard cap，或最新累计余额不足 5 ms cleanup reserve，Operation 不进入 Repository，设置 degraded latch，并安全返回。canonicalization、HMAC、Pool checkout、SQLite lock 和 SQL VM 都使用较早的 work deadline；hard deadline 只用于清理完成后的最终校验和计费。

一次公开方法内部不得创建嵌套 Active Operation。`_prepare_event()`、`_sync_degraded()`、幂等 replay 与 Repository 调用全部复用同一个 Operation lease 和 deadline，避免重复计费或通过嵌套调用扩展预算。

## 4. ActiveWorkBudget 与 OperationLease

### 4.1 瞬态对象

新增内部瞬态预算对象：

```python
class ActiveWorkBudget:
    total_seconds: float
    used_seconds: float
    clock: Callable[[], float]

    def safe_monotonic_read(self) -> MonotonicSample: ...
    def begin_operation(
        self, entry: MonotonicSample, hard_cap_seconds: float
    ) -> OperationLease: ...
    def finish_operation(self, entry: MonotonicSample) -> bool: ...
    def remaining(self) -> float: ...


class OperationLease:
    entry_started_at: float
    work_deadline: float
    hard_deadline: float
    hard_cap_seconds: float

    def checkpoint(self) -> None: ...
    def remaining_seconds(self) -> float: ...


class MonotonicSample:
    value: float
    valid: bool


class SafeClockAdapter:
    def sample(self) -> MonotonicSample: ...
    def require_value(self) -> float: ...
```

它们：

- 只存在于当前进程；
- 不进入 Graph State、ChatMessage、Pending、Ledger、Journal payload 或日志；
- 不保存异常对象或业务内容；
- 使用 `repr=False` 隐藏内部 clock / lock；
- 禁止通用序列化。

所有时间读取只能通过 `ActiveWorkBudget.safe_monotonic_read()`，禁止 Recorder、OperationLease、Repository deadline adapter 或 finally 直接调用原始注入 clock callable。该方法是 total / no-throw：

- 捕获 clock 抛出的普通 `Exception` 和 `BaseException`；
- 拒绝 bool、NaN、Infinity 和相对上一有效 sample 倒退的值；第一份有限数值无论正负都合法，后续只要求不小于上一有效 sample；
- 成功时更新内部 `last_valid_monotonic`；
- 失败时返回 `valid=False`、把预算保守饱和到 total，并请求封闭诊断 `journal_clock_invalid`；
- 不保存或传播 clock 异常对象，不让 clock 的 `BaseException` 覆盖 Journal 工作路径原本需要传播的 `BaseException`。

普通非终态 Operation、Factory 与 resume ingress 的入口 sample 无效时，不获取 Operation lock、不执行 prepare / Repository，安全降级并返回。finalizer 是唯一例外，按 7.3 直接消费 disposition 权并进入 `failed`。最终 sample 无效时，`finish_operation(entry)` 不做浮点运算，直接把累计预算饱和到 total；之后仍然执行 latch、解锁和原始异常优先级逻辑。

`finish_operation()` 和纯内存 latch transition 同样必须是 total / no-throw：不执行 I/O，不调用用户 callback。任何 clock、浮点值或内部状态异常都不能跳过解锁或泄漏到业务路径。此处捕获 clock `BaseException` 是内部计时器的特殊安全边界，不改变 Repository、canonicalization、cleanup 等其他 `BaseException` 清理后原样传播的规则。

Factory 为新 Segment 创建一个 `ActiveWorkBudget`。`start_run()` 或 `resume_waiting_run()` 自身消耗的 active time 先从该对象扣除；成功后同一个对象传给 `SafeRunRecorder`，不能在 Run / Segment 建立后重置为 150 ms。

### 4.2 串行与竞态

同一个 `SafeRunRecorder` 可能同时被 Agent worker、timeout finalizer 或 SSE 取消路径触碰。V2 使用每 Recorder 一个有界 Operation lock 串行实际 Journal 工作，并使用一个不执行 I/O 的短状态锁管理 ingress、degraded 和最终 disposition claim。

规则：

1. Operation entry time 在尝试取得 lock 前记录。
2. lock acquire 最多等待本次 50 ms hard cap。
3. 等待 lock 的时间计入本次 Operation active work。
4. 取得 lock 后重新读取累计剩余预算并收紧 deadline，不能使用等待前的旧快照。
5. `recording_status`、`_degraded_persisted`、`_resume_state`、`_disposition_state` 和 diagnostics 的变化在短状态锁下完成；状态锁内禁止 canonicalization、Repository 或 SQLite。
6. `finally` 先完成 Repository / connection 清理，再扣减从 entry 开始的实际 elapsed、设置必要的 degraded latch，最后释放 Operation lock。下一调用不得看到尚未计费的旧余额。
7. 如果扣费使累计预算耗尽，内存 degraded latch 必须在方法返回或异常传播前设置。
8. lock timeout、预算耗尽或晚到的非终态调用不得制造部分 Event 序列。
9. 最终 disposition 在等待 Operation lock 前，必须先通过状态锁把 `not_attempted` 原子切换为 `claimed`；认领成功立即消耗唯一权利，即使随后等待超时也不允许第二个 finalizer 重试。
10. 每个普通非终态调用在等待 Operation lock 前可以做一次快速状态检查，但取得 Operation lock 后必须再次在状态锁内验证 `recording_status == healthy` 且 `_disposition_state == not_attempted`。这是权威检查；失败时 prepare、HMAC、Repository 和 replay 均为 0 次，只在 `finally` 计入已经发生的 lock wait。
11. post-lock 状态复核与随后开始 prepare 之间保持 Operation lock，因此 finalizer 不能在中间插入持久化工作；finalizer仍可先认领 disposition，但只能等待当前已经通过权威复核的持锁操作完成。

必须覆盖三线程顺序：A 持有 Operation lock，B 通过初始检查后等待，C 认领 final disposition，A 释放后 B 即使先于 C 取得 Operation lock，也必须在 post-lock 复核处 no-op，随后 C 才执行 convergence。

计费状态使用单独的短临界区保护，不能在持有计费锁时执行 SQLite 或 canonicalization。

### 4.3 finally 扣费与异常优先级

所有 active Operation 使用嵌套 `try/finally` 的等价结构。下面的 `primary_base` / `cleanup_base` 只表示当前调用栈内的控制流，不得保存异常到 Recorder、diagnostics 或持久层：

```python
entry = budget.safe_monotonic_read()
if not entry.valid:
    latch_clock_invalid_without_raising()
    return SAFE_NOOP
acquired = False
primary_base = None
cleanup_base = None
result = MISSING
try:
    try:
        acquired = acquire_with_hard_cap(...)
        lease = budget.begin_operation(entry, ...)
        lease.checkpoint()
        result = journal_work(lease)
    except Exception as exc:
        result = classify_and_fail_open_without_exception_text(exc)
    except BaseException as exc:
        primary_base = exc
finally:
    try:
        try:
            if acquired:
                cleanup_connection_if_needed()
        except Exception:
            latch_cleanup_degraded_without_text()
        except BaseException as exc:
            cleanup_base = exc
    finally:
        try:
            exhausted = budget.finish_operation(entry)
            if exhausted:
                latch_degraded_without_raising()
        finally:
            if acquired:
                operation_lock.release()

if primary_base is not None:
    raise primary_base.with_traceback(primary_base.__traceback__)
if cleanup_base is not None:
    raise cleanup_base.with_traceback(cleanup_base.__traceback__)
return result
```

失败、普通异常、`CancelledError`、`KeyboardInterrupt`、`SystemExit` 和其他 `BaseException` 都必须执行同一扣费并释放锁。普通 Journal 或 cleanup `Exception` 继续映射为安全诊断并 fail-open。存在原始 `BaseException` 时必须优先传播原始对象；只有没有原始 `BaseException` 时才传播 cleanup 自己抛出的 `BaseException`。任何 cleanup 失败都不能跳过扣费、latch 或 lock release。

monotonic clock 非法或倒退时不以 0 假装一次免费调用，而是把累计预算饱和到 total、记录封闭诊断 `journal_clock_invalid` 并进入 degraded；不得增加剩余预算。

## 5. CPU、canonical JSON 与 HMAC 预算

调用前后检查不足以限制 Python 侧工作。普通 Journal 工作调用 Operation lease 的 `checkpoint()`，验证：

```text
sample = budget.safe_monotonic_read()
sample.valid
sample.value < operation_work_deadline
budget.used_seconds + current_operation_elapsed < total_active_budget
```

`recording_status`、resume ingress 和 disposition 状态由外层 Recorder 在短状态锁内校验，不属于纯时间 lease。这样 Journal 失败设置 degraded latch 后，仍可在同一未耗尽 lease 内执行一次 best-effort `_sync_degraded()`；它不能借此开启新 Operation 或延长 deadline。

保留并扩展现有内部预算检查点：

- canonical JSON 进入每个 mapping、sequence 和受限字符串块前后；
- Event facts、Manifest source、Contributor、history group、tool envelope 和 provider candidate 循环；
- HMAC / SHA 输入按固定 byte chunk 更新时；
- UTF-8 编码和 byte cap 检查前后；
- V1/V2 Context Manifest 组装各阶段；
- EventDraft、PreparedSnapshot 和 disposition events 构建前后；
- 进入 Repository 前以及 Repository 返回后。

所有预算 callback 必须绑定当前 `OperationLease.checkpoint`，不能继续绑定 Segment 创建时的绝对 deadline。

Python 无法安全异步中断任意单条原生调用，因此 50 ms hard cap 是对**可中断 Journal 工作**的强制 deadline，不声称能在任意阻塞 native / UDF 调用内部抢占线程。必须保留既有输入大小上限，HMAC / SHA 使用固定小 byte chunk，并禁止在 Journal 内新增无界 join、复制、排序或一次性哈希。

保证分为两类：

```text
可机械中断：
    Operation lock wait
    Pool checkout
    SQLite busy wait
    带 checkpoint 的 Python traversal
    SQLite VM instruction loop
→ 在对应 deadline 停止并不再开始新工作

不可安全中断：
    单个阻塞 native call
    SQLite UDF 内部阻塞
    OS / driver 不返回的单次调用
→ 返回后在 finally 扣减全部实际耗时
→ 立即 degraded
→ 不再开始新的 Journal 工作
```

若未来增加不支持 checkpoint 的重型原生处理，必须先证明其输入有界并将调用切成可检查的小块；无法满足时不能进入同步 Journal 路径。

## 6. SQLite hard cap

### 6.1 动态 busy_timeout

每个 Repository 入口继续接收 monotonic absolute deadline，但该 deadline 改为当前 Operation 的动态 work deadline。

checkout 后、任何 SQL 前执行：

```text
now = safe_clock_adapter.require_value()
remaining_ms = floor((operation_work_deadline - now) * 1000)
busy_timeout_ms = min(50, max(0, remaining_ms))
PRAGMA busy_timeout = busy_timeout_ms
```

若剩余时间小于等于 0，不执行 SQL，抛出 `JournalDeadlineExceeded`。SQLite lock wait 因此最多消耗 Operation 剩余时间，不能等待默认连接超时或新的 50 ms 窗口。

Journal Pool 保持：

```text
pool_size = 1
max_overflow = 0
pool_timeout = 0
connect timeout = 50 ms
```

Pool checkout 失败计入本次 Operation，并按现有 fail-open 分类降级。

### 6.2 SQLite VM 中断

为保证可中断的慢查询不只依赖 SQL 前后检查，在 raw SQLite connection 上安装 Operation-scoped progress handler：

```text
every 100 VM steps:
    sample = safe_clock_adapter.sample()
    return non-zero when not sample.valid
    return non-zero when sample.value >= operation_work_deadline
```

`safe_clock_adapter` 是 `ActiveWorkBudget.safe_monotonic_read()` 的 Repository-facing 封闭适配器，接口语义固定为：

```text
sample() -> MonotonicSample
    total / no-throw
    clock 非法时返回 valid=false
    只供 progress handler 和其他不能传播异常的 callback 使用

require_value() -> float
    内部调用 sample()
    valid=false 时抛封闭 JournalDeadlineExceeded
    供普通 Repository deadline / busy_timeout 检查使用
```

Repository、progress handler 与连接恢复逻辑都不得直接调用原始注入 clock。Progress handler 只调用 `sample()` 并通过非零返回值中断，绝不从 callback 抛出 Python 异常。被中断且 deadline 已耗尽或 clock sample 非法时统一映射为 `journal_budget_exhausted`。Progress handler 只能在 SQLite VM 指令边界运行，不能中断正在执行的阻塞 UDF 或任意 native call；此类调用返回后依靠 `finally` 全额计费并进入 degraded。其他 SQLite operational failure 保持现有安全分类，不保存 SQL、参数或异常正文。

### 6.3 连接恢复

Repository / Session 退出前必须：

1. rollback 未完成事务；
2. 清除 progress handler；
3. 将 `busy_timeout` 恢复为 50 ms；
4. 确认连接可安全归还 Journal Pool。

普通 SQL 到达 work deadline 后不得再开始。cleanup 使用预留的 5 ms，且不得因为 work deadline 已过而跳过；它只能执行 rollback、清除 handler、恢复 PRAGMA 或 invalidate / close，不得查询或写入业务数据。若到达 hard deadline 前无法确认恢复，必须立即 invalidate / close，不能把带有旧 deadline handler 的连接交给下一借用者。cleanup 的全部实际耗时仍在 `finally` 中扣费。

增加 ABA 负向测试：前一次 Operation deadline 到期后，下一借用者不能被旧 progress handler 中断，也不能继承 0 ms `busy_timeout`。

### 6.4 Caller-owned Session 例外

`tool.started` 的 Write Ledger 路径故意通过调用方持有的业务 Session 写入，使 Journal Event 与 Ledger / 领域事务同 commit、同 rollback。该 Session 不属于 Journal Pool，不能套用 6.1–6.3 的连接所有权协议。

这条路径拆成两个明确 Operation：

```text
prepare_event_draft(event)
    CPU-only Journal Operation
    → canonicalization / HMAC / validation
    → 返回瞬态 EventDraft | None

业务 BEGIN IMMEDIATE / Ledger claim
    → append_prepared_event_bound(caller_session, draft)
       caller-owned bound Journal Operation
    → Tool executor / Ledger terminal
    → caller commit / rollback
```

`prepare_event_draft()`：

- 在进入业务事务前执行；
- 使用正常 ActiveWorkBudget、Operation lock、50 ms 可执行工作 deadline、5 ms cleanup reserve 和内部 checkpoints；
- 只做 CPU / 内存工作，不打开 Session；
- 返回的 EventDraft 不携带 lease、deadline、Session 或预算对象；
- 失败或超限时返回 `None`、设置 degraded，业务仍可继续，但不得在业务事务内重新 prepare。

`append_prepared_event_bound()`：

- 从方法入口到 nested SAVEPOINT 退出的实际时间作为独立 active Operation，在 `finally` 中全额计费；
- 进入 caller Session 前检查 active budget 与 work deadline；已耗尽时返回 `False`，不得触碰 Session；
- 只允许 `session.begin_nested()` 与 `AgentRunRepository.append_event_bound()`；
- 不安装、替换或清除 progress handler；
- 不读取或修改 `PRAGMA busy_timeout`、`query_only` 或其他连接级状态；
- 不调用外层 `rollback()`、`commit()`、`close()`、`invalidate()` 或 Pool 操作；
- 不把 Journal deadline 传给 caller-owned connection；
- nested SAVEPOINT 内的 Journal `Exception` 只回滚该 SAVEPOINT，设置 degraded 并返回 `False`，不得主动回滚或标记外层 Ledger / 领域事务失败；
- caller-owned Session 仍持有写事务时，不得调用 `_sync_degraded()`、不得另开 Journal Session 尝试持久化 degraded；只设置内存 latch，待外层事务结束后的 final disposition 使用独立预算收敛；
- 成功插入并释放 SAVEPOINT 后返回 `True`。即使原生 SQLite 调用返回时已经超过 hard deadline，仍必须返回 `True` 反映 Event 已进入 caller transaction，同时在 `finally` 全额扣费并将 Recorder 置为 degraded；不得重做或谎称 Event 未写入；
- `BaseException` 在 nested SAVEPOINT 清理、计费和 Operation lock 释放后原样传播，由 caller 按既有业务事务规则决定外层 rollback；
- caller Session 若因底层连接故障已不可用，后续权威业务 commit 自身必须失败；Journal 不得捕获后伪装业务提交成功。

这条例外不获得 SQLite progress-handler 级抢占保证。其 SQL 已处于 caller 持有的写事务中，正常情况下不再等待外部写锁；若单个 native / driver 调用超时，只能在返回后计费、degraded 并停止后续非终态 Journal 工作。

测试必须证明 caller-owned Session 的成功、Journal conflict、budget-before-entry、native overshoot、普通异常和 `BaseException` 六类路径，不改变既有 SAVEPOINT、Ledger、领域写入与外层 commit / rollback 原子性。

## 7. Recorder 状态机

### 7.1 非终态操作

```text
healthy + remaining budget
→ begin Operation
→ prepare / Repository / replay
→ finally charge elapsed
→ healthy 或 degraded
```

以下情况使 degraded latch 不可逆：

- 累计 active work 达到或超过 150 ms；
- 单次 Operation 达到 50 ms hard cap；
- SQLite lock / VM 执行耗尽 Operation deadline；
- canonical / HMAC / Manifest checkpoint 超限；
- 既有 Journal validation、conflict、key-domain 或持久化失败。

进入 degraded 后：

- 后续非终态 Journal 操作直接 no-op；
- 不重试刚才的 Event / Snapshot；
- 不改变 Provider、Tool、Pending、Ledger 或业务结果；
- best-effort `mark_degraded` 只能使用当前未耗尽的 Operation lease；
- 当前 lease 已耗尽时不创建额外 50 ms active Operation，只保留内存 latch，等待 disposition 独立收敛。

### 7.2 Resume ingress

`resume_waiting_run()` 创建确认恢复的新 Segment 后，`recorder.resume()` 记录 `run.resumed` 并把 Run 从 `waiting_confirmation` 收敛回 `running`。它是新 Segment 的 ingress，不是该 Segment 的结束 disposition。

每个 Recorder 使用独立状态：

```text
_resume_state = not_attempted | claimed | completed | failed
_disposition_waits_for_resume = false | true
```

规则：

1. `_resume_state` 与 `_disposition_state` 使用同一个状态锁和 Condition，claim 的状态锁线性化顺序决定先后，不依赖 Operation lock 公平性。
2. `resume()` claim 先发生时：确认 `_disposition_state == not_attempted`，把 `_resume_state` 从 `not_attempted` 改为 `claimed`。随后到达的 finalizer仍可认领 `_disposition_state`，但必须同时设置 `_disposition_waits_for_resume=True`。
3. finalizer claim 先发生时：`resume()` 看到 `_disposition_state != not_attempted` 后不得认领，直接 no-op；因此 final Event 后不可能出现 `run.resumed`。
4. resume 取得 Operation lock 后执行专用 post-lock 复核，唯一允许继续的条件是：

   ```text
   _resume_state == claimed
   and (
       _disposition_state == not_attempted
       or (
           _disposition_state == claimed
           and _disposition_waits_for_resume == true
       )
   )
   ```

   finalizer 已承诺等待时，resume 即使后取得 Operation lock也具有 ingress 优先权；`completed | failed` 的 disposition 永远不能被 wait flag 绕过。
5. 等待 resume 的 finalizer 不持有 Operation lock；它通过 Condition 等待 `_resume_state` 进入 `completed | failed`，等待时间计入自己的独立 50 ms。Condition 的剩余 timeout 也只能由 `safe_monotonic_read()` 的有效 sample 计算；sample 非法时立即按 deadline 耗尽处理。这样 resume 可以取得 Operation lock、完成计费和通知。
6. resume 在最外层 `finally` 把状态置为 `completed | failed` 并 `notify_all()`。finalizer 被唤醒后，只有在自己剩余 deadline 内才继续取得 Operation lock和收敛；Condition 等待超时则不调用 Repository、不产生 final Event，把 disposition 置为 `failed`、清除 wait flag并消耗唯一 final 权。尚未通过 post-lock 复核的 resume 因此会被 fence；已经持有 Operation lock 并通过复核的 resume 可以完成，但此时同样不存在一个排在它前面的 final Event。
7. `resume()` 使用该新 Segment 的普通 ActiveWorkBudget 和单次 50 ms Operation hard cap。
8. 它不消耗独立 50 ms final convergence。
9. 无论 resume Journal 是否成功，业务确认恢复继续 fail-open；该 Recorder 后续仍可且只能执行一次 `finish()`、`suspend()` 或 `abandon()`。
10. `resume → finish` 和 `resume → chained Pending → suspend` 必须各产生正确的新 Segment 事件顺序。
11. finalizer 与 resume 两种 claim 顺序，以及 `finish / suspend / abandon` 三种 target，都必须证明 `run.resumed` 从不出现在 final disposition Event 之后。

`_resume_state` 的 `claimed → completed | failed` 更新位于 resume 最外层 `finally`；即使 cleanup 或工作路径抛出 `BaseException`，也必须先更新状态、通知等待者、扣费并解锁，再原样传播。

### 7.3 独立 final disposition convergence

`finish()`、`suspend()` 和 `abandon()` 每个 Segment 共享一次、且仅一次独立 50 ms final convergence 权利。状态机固定为：

```text
_disposition_state
    not_attempted
      ├─ valid entry → claimed → completed | failed
      └─ invalid entry → failed
```

认领必须发生在等待 Operation lock 之前：

```text
finalizer entry
→ safe clock sample
   invalid:
     latch journal_clock_invalid without throwing
     state_lock: CAS not_attempted → failed
     CAS loser: no-op
     Provider / Repository / Event = 0
     return
   valid: hard deadline = sample.value + 50 ms
→ state_lock: CAS not_attempted → claimed
   CAS loser: no-op
→ if resume already claimed:
     set disposition_waits_for_resume
     predicate-loop wait for resume completed | failed
→ bounded acquire same Operation lock
→ work deadline = hard deadline - 5 ms cleanup reserve
→ prepare fixed disposition events
→ Repository converge
→ optional persist degraded status
→ nested cleanup / independent-lease accounting / release
→ state_lock: claimed → completed | failed
```

finalizer 的 fresh hard deadline 必须在进入方法时、状态 claim 之前取得。入口 sample 无效时也必须在线性化的状态锁内把 `not_attempted` 直接切为 `failed`；这会消费唯一 final convergence 权，后续 finalizer 只能 no-op，且不得调用 Provider、Repository 或创建 Event。若状态已不是 `not_attempted`，本次只是 no-op，不能覆盖既有状态。

有效入口下，等待 resume Condition 和 Operation lock 共用该同一个 50 ms，不能在 resume 完成或虚假唤醒后重置。Condition 必须使用 predicate loop / `wait_for`：只在 `_resume_state in {completed, failed}` 时退出；每次循环都用 `safe_monotonic_read()` 的有效 sample 对同一个 absolute finalizer deadline 重算剩余时间。无效 sample 或剩余时间小于等于 0 都按 Condition timeout 收敛为 `failed`，不得调用 Repository。CAS winner 一旦将状态置为 `claimed` 就永久消耗该 Segment 的唯一 final convergence 权。即使 resume 等待、前一个非终态 Operation、SQLite lock、cleanup 或 `BaseException` 耗尽 deadline，状态最终也只能进入 `failed`，其他 finalizer 只能 no-op。

`claimed → completed | failed` 必须位于 finalizer 的最外层 `finally`，在 nested cleanup、扣费和 Operation lock release 之后、任何 `BaseException` 重新传播之前执行，并清除 `_disposition_waits_for_resume` 后 `notify_all()`。只有 Repository convergence 已确定成功时进入 `completed`；Condition / lock timeout、预算耗尽、普通异常、cleanup 异常和 `BaseException` 都进入 `failed`。

认领本身只持有短状态锁，不执行 I/O。等待仍在执行的非终态 Journal Operation 计入 finalizer 的独立 50 ms。claim 之后到达的新非终态操作看到 `_disposition_state != not_attempted` 后直接 no-op；claim 前已持有 Operation lock 的工作允许完成，winner 在其后执行 convergence。

该时间不扣除也不补充 150 ms active budget。finalizer 复用 4.3 的异常与解锁结构，但其中 elapsed 只检查本次独立 disposition lease，不调用 Segment `ActiveWorkBudget.charge()`。active budget 健康时 final disposition 也使用独立预算，不从剩余 active budget 借时间。这样 terminal / suspended 语义不依赖前面记录了多少事件。

## 8. Factory 与 Repository 接口

### 8.1 RunRecorderFactory

Factory API 保持不变。内部变化：

- `start_run()` 在调用 deferred builder 前创建 ActiveWorkBudget 和第一张 Operation lease；
- builder、HMAC、Run/Segment command 和原子创建全部计入同一次 50 ms Operation；
- 成功后把已扣费的同一 ActiveWorkBudget 交给 SafeRunRecorder；
- 创建失败继续返回 NullRunRecorder；
- `resume_waiting_run()` 的 waiting lookup、builder 和 Segment 创建属于一次 Operation；
- 两次 Repository 访问共享一个 Operation deadline，不能各自获得新的 50 ms。

### 8.2 AgentRunRepository

公开 Repository 签名继续接收：

```python
deadline: float | None
safe_clock: SafeClockAdapter
```

不把 ActiveWorkBudget 或 OperationLease 传入 Repository，避免持久化层反向依赖 Runtime。Journal-owned Session 的 Repository 调用接收动态 work deadline，以及由 OperationLease 暴露的 `SafeClockAdapter`。`sample()` 自身 no-throw；`require_value()` 只把无效 sample 映射为封闭的 `JournalDeadlineExceeded`，绝不调用或暴露原始注入 clock 异常。Repository 只负责遵守 deadline、设置 SQLite guard、恢复自己拥有的连接并抛出封闭异常。

`deadline=None` 只保留给明确的内部维护和旧测试路径。机械门禁拆成两类：

1. 所有使用 Journal-owned Session 的生产 Repository 调用必须传递动态 deadline 和 safe clock adapter；缺少任一参数即失败。
2. `append_event_bound(caller_session, run_id, draft)` 是封闭且唯一的 caller-owned 例外。AST 门禁必须证明只有 `SafeRunRecorder.append_prepared_event_bound()` 能调用它，签名不新增 deadline / `SafeClockAdapter`，并禁止其调用 `_configure_deadline`、progress handler、PRAGMA、rollback、commit、close、invalidate 或 Journal Session factory。

新增任何 caller-owned Repository API 必须重新设计和复审，不能通过扩大 allowlist 静默绕过 deadline 门禁。

## 9. 兼容性与隐私

必须保持：

- `RunRecorder`、`SafeRunRecorder`、`NullRunRecorder` 对调用方的公开协议；
- Event type、facts、dedupe key、seq、Snapshot 和 Manifest Schema；
- HMAC key domain 和 fingerprint 算法；
- `healthy / degraded` 与现有 Trace 分类；
- Journal kill switch；
- 4 KiB Event、16 KiB V1 Snapshot、64 KiB V2 Snapshot 限额；
- enabled / disabled / unavailable / degraded 的业务等价；
- 普通请求、SSE、确定性动作、确认恢复、terminal replay 和 delivery recovery 行为；
- ordinary `Exception` fail-open，`BaseException` 清理后传播。

ActiveWorkBudget diagnostics 只允许现有或新增封闭 code：

```text
journal_budget_exhausted
journal_disposition_budget_exhausted
journal_clock_invalid
```

不得记录 elapsed 明细到 Event、Snapshot、日志或公开接口；内部测试可以读取瞬态计数。异常对象、SQL、payload、Provider 时间和业务内容都不得进入 diagnostics。

## 10. 测试与验收

### 10.1 预算单元测试

使用 ManualClock / StepClock 验证：

1. 两次 Journal 调用之间前进 2 秒，active budget 不减少。
2. 一次 Provider stub 实际等待 2 秒，前后 Journal 快速写入，Run 保持 `healthy` 且 Trace 完整。
3. 4 次各消耗 30 ms 的 Operation 累计 120 ms；第五次只剩 30 ms，不得获得新的 50 ms。
4. 可中断 Operation 达到 50 ms 时停止；不可中断调用返回后发现超过 50 ms 时立即 degraded，即使累计预算原本充足。
5. prepare、Repository、rollback 和 cleanup 的时间都计入。
6. Repository 抛普通异常后，elapsed 仍在 `finally` 扣减。
7. `KeyboardInterrupt`、`SystemExit` 和 cancellation 清理并扣减后原样传播。
8. monotonic 倒退不增加预算，进入 `journal_clock_invalid` degraded。
9. 并发非终态调用串行，lock wait 计入 hard cap。
10. timeout finalizer 在等待 Operation lock 前原子认领；即使等待超时，第二个 finalizer 也不得重试。
11. `resume()` 使用独立 `_resume_state`，不会消耗后续 finish / suspend 权利。
12. 三线程 barrier 固定 A 持锁、B 已通过初检并等待、C claim final；B 抢先取得锁后必须 post-lock no-op，prepare / Repository 均为 0。
13. resume-first 时 finalizer 在同一 50 ms 内等待 ingress；finalizer-first 时 resume 无法 claim。两种 Operation lock 获取顺序都不得产生 final Event 后的 `run.resumed`。
14. `safe_monotonic_read()` 在 Operation entry 和 finally 各自覆盖 NaN、Infinity、倒退、抛 `Exception` 和抛 `BaseException`；全部 no-throw、预算饱和且诊断无异常正文。
15. Journal 工作同时有原始 `BaseException`、最终 clock 又抛 `BaseException` 时，传播原始工作异常，clock 异常不能覆盖它。
16. 第一份有限 monotonic sample 可以为负数；例如 `-10.0 → -9.5 → -9.0` 合法且不产生 `journal_clock_invalid`。只有后续 sample 小于上一有效值才按倒退处理。
17. `SafeClockAdapter.sample()` 对 clock 非法或抛错始终返回 `valid=false` 且不抛异常；`require_value()` 对同一情况只抛封闭的 `JournalDeadlineExceeded`。
18. finalizer 入口 sample 非法时原子执行 `not_attempted → failed`，Repository / Event 为 0、第二个 finalizer no-op；Condition 虚假唤醒不会重置 deadline或提前越过 resume predicate。

### 10.2 CPU 与序列化

验证：

- canonical JSON 深层 traversal 多次调用同一个 lease checkpoint；
- HMAC 大输入按固定 chunk 检查预算；
- V2 最大 Manifest 在预算充足时保持现有 canonical fingerprint；
- 注入慢 canonicalizer / manifest projector，超过 hard cap 后 Repository 调用为 0；
- 在中途和最后 checkpoint 之间耗尽预算，`finally` 仍设置 degraded；
- rollback、handler 清除、PRAGMA 恢复或 invalidate 抛出普通异常时，仍然扣费、设置 degraded 并释放 Operation lock；
- 上述 cleanup 抛出 `BaseException` 时，在保留原始异常优先级后仍完成扣费和解锁；
- 任何诊断不包含 canary 正文、异常或 HMAC 原始输入。

### 10.3 SQLite

使用两个独立连接验证：

1. Connection A 持有 `BEGIN IMMEDIATE`；Recorder 的写入按 Operation 剩余预算设置 `busy_timeout` 并在上限内降级。
2. 累计只剩 12 ms 时，`busy_timeout` 不得重新设为 50 ms。
3. progress handler 使用递归 CTE 或大表扫描验证可中断 SQLite VM 在 work deadline 后停止；不得使用阻塞 UDF 证明严格中断。
4. cleanup 后下一借用者恢复 50 ms default 且不受旧 handler 影响。
5. cleanup 失败时连接被 invalidate，Pool 后续能创建健康连接。
6. lock wait 或 pool checkout 失败不改变业务 callback 的结果。
7. 阻塞 UDF / native call 返回后全额扣费、进入 degraded、完成连接清理且不再开始新 Journal 工作；不断言它在 50 ms 内被抢占。
8. caller-owned Session 不安装 handler、不修改 PRAGMA、不 rollback / close / invalidate 外层连接。
9. bound Event 成功、Journal conflict、budget-before-entry、native overshoot、普通异常和 `BaseException` 均保持 SAVEPOINT 与外层业务事务原子性。
10. AST / spy 精确证明所有 Journal-owned 调用带 deadline / safe clock，且唯一 `append_event_bound` 例外既不接受 deadline，也不触碰连接级 guard 或 cleanup。
11. clock 非法时，progress handler 调用 `sample()` 只返回非零中断且不抛异常；普通 Repository deadline 调用 `require_value()` 得到封闭的 `JournalDeadlineExceeded`。

### 10.4 disposition

验证 active budget 已耗尽后：

- `suspend()` 仍获得一次 50 ms 收敛并写入 waiting disposition；
- `finish()` 仍获得一次 50 ms 收敛并写入 terminal disposition；
- resumed Segment 使用新的 150 ms active budget，`resume()` 只消耗 ingress claim；
- `resume → finish` 与 `resume → chained pending → suspend` 都能完成 final convergence；
- 重复 `resume()` 不重复 ingress；重复或并发 finish / suspend / abandon 不产生第二次 final convergence；
- resume-first / finalizer-first 分别与 finish、suspend、abandon 做参数化并发测试；
- disposition 等待并发非终态操作时仍受自己的 50 ms 限制；
- 第一个 finalizer 等待 Operation lock 超时后，第二个 finalizer仍为 no-op；
- disposition SQLite 锁定时安全失败，业务结果不变。

### 10.5 端到端等价

普通 Chat 与 SSE 至少覆盖：

- 2 秒慢 Provider + 最终文本；
- 2 秒慢 Provider + read tool + 后续 Provider；
- 写工具 Pending / approve / reject；
- terminal replay 与 delivery fallback；
- Journal enabled、disabled、Null、locked 和 active-budget exhausted。

对比：

```text
HTTP status / response body
SSE event type / seq / order
Provider call count
Tool executor call count
ChatMessage / Pending Action
Write Operation Ledger / domain state
Journal Event sequence / Trace integrity
```

除 Journal `recording_status`、事件完整性和安全 diagnostics 外，所有业务与传输结果必须等价。健康 Journal + 2 秒 Provider 的 Trace 必须为 `healthy`，不能再因 Provider wall time出现 `model_call_incomplete`。

### 10.6 发布门禁

最终执行：

- Journal、Repository、Trace 和 Context Manifest 定向测试；
- Chat sync/SSE/HITL/Ledger 回归；
- 全量 pytest、Ruff、Mypy；
- 前端测试与构建；
- static smoke、local smoke、local verify；
- 受控 real-AI 慢 Provider 验证；
- 独立 CR，无未关闭 P0/P1/P2；
- baseline allowlist、未跟踪文件、`git diff --check` 和工作区清洁检查。

## 11. 实施边界与完成定义

推荐模块边界：

```text
src/offerpilot/agent_runtime/budget.py
    ActiveWorkBudget
    OperationLease

src/offerpilot/agent_runtime/journal.py
    Recorder / Factory 使用 active Operation wrapper

src/offerpilot/repositories/agent_runs.py
    dynamic busy_timeout / progress handler / connection cleanup
```

实施必须测试先行，并分为预算对象、Recorder 切换、SQLite deadline、并发/disposition、端到端等价和发布门禁几个独立提交。不得在本分支创建 `pilot_runtime`、修改 Agent Loop 或实施 Scoped Capability。

本项目完成的准确表述是：

> Journal 的 150 ms 限制只累计 Journal 自身同步工作；Provider、工具、业务 Repository、确认和调用间等待不再天然导致 Trace degraded。锁等待、Pool checkout、带 checkpoint 的 Python 工作和可中断 SQLite VM 受单次 deadline 限制；不可中断 native 调用返回后全额计费并停止后续 Journal 工作。最终 disposition 保留一次独立、有界的收敛尝试，Journal 失败继续 fail-open。

它不表示 Journal 必然完整，也不表示任何业务 exactly-once 或 Event Sourcing 保证。
