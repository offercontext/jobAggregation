# Core Task Surface Convergence 设计

> 状态：待复审
> 固定 baseline：`93fb0063118761f2c76e71e4209000feee0f755b`
> 设计分支：`refactor/20260829-core-task-surface-convergence`
> 本文只定义设计；复审通过前不编写实施计划，不修改生产代码。

## 1. 背景与问题定义

`main@93fb006` 已完成桌面信息架构、Haru/Pilot 单一会话控制器、Agent Runtime、Tool Execution、Write Operation Ledger、Context Projector、Scoped Authority 和 Tool Metadata Convergence。当前剩余问题不再是缺少底层能力，而是同一个求职任务仍由多个界面入口、多个局部状态和多套用户文案共同表达。

这会让用户在以下问题上反复做选择：

- 在投递详情中，应点顶部主操作、准备卡、更多操作、Pilot 卡片，还是独立 Drawer；
- 在面试页中，应从事件卡、通用准备表单、题库、推荐训练还是投递详情开始；
- 面试确认内容究竟属于“经历素材”还是“参考资料”；
- `is_master`、V1/V2、评估编号、快照、hash、Worker、队列等内部概念与用户任务是什么关系。

本项目把这些入口收敛为一组封闭、确定、可测试的桌面 Task Surface。它不增加 Agent 能力，不改变业务真值，也不实现新的结果回流。

### 1.1 基线事实

- `ApplicationDetail` 同时存在准备卡、更多操作和局部 Drawer；岗位判断、材料、面试、复盘和结果操作存在平级入口。
- Opportunity Fit 的 V1 历史与 V2 当前流程仍以两组用户可见标题、编号和状态呈现；V1 写入已经停用，历史数据仍需只读兼容。
- `MaterialKitDrawer` 已具备生成、编辑、确认、保存、结果未知和提交记录能力，但外围仍可形成第二个主操作。
- `InterviewV01View` 已能接收 `ApplicationEvent` 列表，却仍主要按 `scheduled_at` 分成“即将进行/已完成”；过去但未更新状态的事件会被误当作完成。
- `/api/interviews` 的 `InterviewIndexItem` 来自 `ApplicationEvent`，但没有返回该事件的 `status`，且 `preparation_available` 当前不是权威状态判断。
- 面试卡片已经传递 Application/Event，JD 可从 Application scope 读取；Resume 没有 Application 级绑定，仍会被重复询问。
- `KnowledgeSourcesView` 同时投影外部参考资料和已确认面试捕获；后端数据没有合并，但用户信息架构仍混在一起。
- Resume 已有 `is_master` 和 `parent_resume_id`，用户界面仍主要以平铺列表和“主简历”表达，派生关系不清晰。
- 复盘、故事、弱点和下一次准备目前是独立能力；不存在受确认、版本和来源约束的自动反馈链。

## 2. 目标、兼容口径与非目标

### 2.1 目标

本项目完成三个相互协作但可独立验收的 Surface：

1. **Application Task Surface**：同一个投递任务只有一个正式操作面；入口只负责导航和上下文交接。
2. **Interview Event Surface**：事件状态而非时间成为面试卡片真值；准备、复盘和自由练习各有唯一主入口。
3. **Materials & Resume Surface**：基础简历、岗位版本、经历素材和外部参考资料形成稳定、互斥的用户分类。

完成后应满足：

- 用户从任意入口进入同一任务时，看到同一个 owner、同一份草稿和同一个 Pending；
- 打开、关闭、切换和重复点击不会调用 Provider、启动 SSE 或产生业务写入；
- 每个状态只有一个突出主操作；
- 面试卡片不会把“时间已过但状态未更新”伪装成已完成；
- 用户界面不暴露内部协议和存储术语；
- 现有 HITL、CAS、Ledger、Journal、Context Projector、Capability/Binding 和领域归属校验继续作为安全真值。

### 2.2 兼容口径

本期采用：

> **业务与传输严格兼容，桌面信息架构允许受控破坏性收敛。**

允许删除旧的平级入口、重复 owner、重复标题和内部文案。以下契约必须保持：

- HTTP 路径、请求 payload、SSE 事件和 Pending payload 不变；
- 唯一受控 response contract 变化位于 `/api/interviews`：增加只读 `event_status/duration_minutes/scheduled_at_state`，收紧既有 `preparation_available`，并修正为每个 Event 只返回最新一条 Note 投影；
- Event 去重会有意改变历史上重复 row 的响应基数、cursor 和分页边界，属于明确批准的只读契约修复，不宣称纯 additive；不依赖重复 row 的旧客户端继续兼容；
- 不新增数据库表、字段或 migration；
- Provider 工具契约、调用上限、fallback 和 Tool execution 次数不变；
- approve/modify/reject、terminal replay、delivery recovery、operation fencing 和结果未知语义不变；
- 不增加隐式 retry、shadow execution、双写或旧路径 fallback；
- 不改变已有 Repository/API 的实体归属校验；
- Context-scoped 可见面和 UI 隐藏都不是授权边界。

### 2.3 非目标

本期明确不实现：

- Review-to-Readiness 自动反馈链；
- 复盘自动生成 Story、Memory、Knowledge 或简历修改；
- 面试结果自动更新 ApplicationEvent 状态；
- Application 与 Resume 的新持久绑定；
- 新的摘要、检索、Provider 调用或 Agent Tool；
- 新的后台队列、提醒、SSE replay 或 UI 调试台；
- ToolSpec、Tool Metadata、Ledger、Journal、Context Projector 或 Agent Runtime 重构；
- 顶层导航再次重构；
- 移动端。

## 3. 总体架构

```text
Header / Task Card / Haru / Pilot / Deep Link
                         │
                         ▼
                CoreTaskRegistryV1
              解析封闭 Task Identity
                         │
                         ▼
               CoreTaskSurfaceController
          单 owner / 去重 / 焦点 / Pending 保护
                         │
       ┌─────────────────┼──────────────────┐
       ▼                 ▼                  ▼
Application Task   Interview Event   Materials & Resume
    Surface            Surface             Surface
       │                 │                  │
       └─────────────────┴──────────────────┘
                         │
                         ▼
            现有 Service / Assistant Controller
            HITL / Ledger / Domain Repository
```

`CoreTaskRegistryV1` 是桌面端导航和展示合同，不是第二个 Tool Catalog、权限系统或业务数据库。它不能执行工具、签发 capability、改变 binding、持久化 Task、推断 Pending，也不能把隐藏某个按钮当作授权。

### 3.1 模块依赖

推荐依赖方向：

```text
features/coreTaskSurface/contracts.ts
  └─ 纯类型、封闭枚举、稳定 reason code

features/coreTaskSurface/registry.ts
  └─ 唯一 Task → owner 映射，不导入领域 Service

features/coreTaskSurface/controller.tsx
  └─ 打开、关闭、去重、焦点与 transient handoff

features/applicationTasks/*
features/interviewEvents/*
features/materialSurfaces/*
  └─ 各自导入 contracts，不反向修改 registry
```

`AppShell` 作为 composition root 注入现有 query 结果和 owner callback。入口组件不得直接创建新的业务 owner。

## 4. 封闭 Task Identity 与入口协议

### 4.1 Task 集合

V1 只包含本期已有、可达的正式工作面：

```ts
type CoreTaskId =
  | 'application.opportunity_fit'
  | 'application.material_kit'
  | 'application.interview_prepare'
  | 'application.interview_review'
  | 'application.general_review'
  | 'application.offer_review'
  | 'application.record_outcome'
  | 'interview.free_practice'
  | 'materials.resume'
  | 'materials.story'
  | 'materials.reference';
```

没有稳定 owner 的动作不得临时塞入 registry。普通记录管理，例如编辑备注、调整时间、查看详情或删除记录，仍作为所属页面的低频管理动作，不伪装成 Core Task。

简历库、经历素材列表、参考资料列表等集合页面属于 `navigation_only`，继续由 canonical `ViewMode` 路由承接，不要求伪造一个 record ID。只有打开具体 Resume/Story/Source 时才构造对应 CoreTaskRef。基线入口 manifest 必须把每个入口显式分类为 `core_task | navigation_only | record_management`；未知分类失败，但 navigation 不强行映射 TaskId。

### 4.2 TaskRef

```ts
interface CoreTaskRef {
  taskId: CoreTaskId;
  applicationId?: number;
  eventId?: number;
  offerId?: number;
  resumeId?: number;
  storyId?: number;
  sourceId?: number;
}
```

每种 `taskId` 有封闭的 required/forbidden identity schema：

| Task | 必须且仅允许的 identity 字段 |
|---|---|
| `application.opportunity_fit` | `applicationId` |
| `application.material_kit` | `applicationId` |
| `application.interview_prepare` | `applicationId,eventId` |
| `application.interview_review` | `applicationId,eventId` |
| `application.general_review` | `applicationId` |
| `application.offer_review` | `applicationId` |
| `application.record_outcome` | `applicationId` |
| `interview.free_practice` | 无 |
| `materials.resume` | `resumeId` |
| `materials.story` | `storyId` |
| `materials.reference` | `sourceId` |

所有 ID 必须是安全正整数。非法组合在入口处返回固定 `invalid_task_identity`，不进行 Repository、Provider、Chat、SSE 或业务写入。Registry 不通过按钮文案、数组位置、标题、时间戳或模型自然语言生成身份。

### 4.3 TaskLaunchRequest

```ts
interface TaskLaunchRequest {
  ref: CoreTaskRef;
  source:
    | 'application_header'
    | 'application_task_card'
    | 'interview_event_card'
    | 'materials_library'
    | 'haru'
    | 'pilot'
    | 'deep_link'
    | 'command_palette';
  focus?: 'overview' | 'current' | 'history' | 'source';
  hints?: {
    suggestedResumeId?: number;
    suggestedOfferId?: number;
    suggestedEventId?: number;
  };
}
```

它是 request-local 的瞬态对象：

- 不写入 ChatMessage、Pending、Ledger、Journal 或领域数据库；
- 不携带 JD/简历/复盘正文、异常对象、confirmation token、operation ID、hash 或密钥；
- `hints` 不是 Task identity，不得创建第二个 owner；owner 只在重新验证可见性与归属后采用，否则静默忽略；
- `source` 只用于安全的 UI 诊断和焦点恢复，不改变权限或业务行为；
- Haru/Pilot 只提交 TaskRef，不复制 Task Surface 的数据和状态。

### 4.4 单 owner 与重复打开

`CoreTaskSurfaceController` 使用 canonical key：

```text
taskId + ordered required identity
```

`focus`、入口 `source`、建议 Resume/Offer 都不进入 key。同一 Material Kit 即使从不同入口携带不同 Resume 建议，也只能打开一个 owner；冲突建议进入既有明确选择状态，不能产生两个 Surface。

状态固定为：

```text
closed → opening → open → closing → closed
```

规则：

- 同 key 重复 launch 只聚焦既有 owner；不得重新初始化草稿、再次注册 SSE 或再次提交 mutation；
- 不同 key 的 launch 先执行现有未保存草稿/Pending 保护，再替换 owner；
- 组件重渲染和 React Strict Mode 双调用不得形成第二个 owner；
- close 只关闭展示，不回滚已提交事实、不拒绝 Pending、不停止仍由 Assistant Controller 持有的请求；
- stale close/focus callback 必须携带 controller generation，旧 generation 绝对 no-op；
- 未知 task、owner 未注册或 identity 不匹配显示固定不可用状态，禁止回退到任意旧 Drawer。

### 4.5 Pending 优先级

Pending Action 仍是确认真值：

- 打开或切换 Task Surface 不得清除、替换或本地伪造 Pending；
- 当前 Task 对应 Pending 时，owner 进入既有 confirmation view；
- Pending 属于另一上下文时，显示“有一项操作等待确认”及返回入口，不把 token 或 operation identity 暴露给用户；
- approve/modify/reject 继续调用既有 Assistant/HTTP owner；
- reject 不运行 schema decode、entity preflight 或目标读取；
- terminal replay 与 delivery recovery 打开 Surface 时 Provider 为 0。

## 5. Application Task Surface

### 5.1 页面结构

投递详情保留“概览 / 准备 / 进展”三段，但“准备”不再同时挂载所有复杂组件：

```text
准备概览
  → 确定性任务状态卡
  → 用户选择一个任务
  → 单个 Active Task Surface
```

同一时刻只挂载一个 Active Task owner。返回准备概览保留 owner 明确允许的瞬态草稿；跨 Application 切换必须执行既有草稿保护。

“更多操作”只保留低频记录管理，例如编辑基础信息、备注、时间、归档/删除等。岗位判断、投递材料、面试准备、面试复盘、Offer 判断和结果记录不得再作为第二套业务入口出现在菜单中。

### 5.2 ApplicationTaskResolver

```ts
resolveApplicationTasks(
  snapshot: FrozenApplicationTaskSnapshot,
  now: number,
): ApplicationTaskResolution
```

Resolver 是纯函数：

- `now` 显式注入，不读取墙上时钟；
- 只消费已加载、不可变的 primitive/JSON view model；
- 不访问 Repository、Service、Provider 或 Assistant Controller；
- 输出唯一 primary、稳定排序的 tasks、availability 和封闭 reason code；
- 同一输入、规则版本和 `now` 得到完全相同结果。

```ts
type TaskAvailability =
  | 'ready'
  | 'loading'
  | 'blocked'
  | 'waiting_confirmation'
  | 'result_unknown'
  | 'unavailable';
```

Application stage 只提供优先级信号，不替代事件、Offer、JD、Resume、Pending 和来源真值。数据仍在加载时禁止突出可执行主操作；来源读取失败和身份不一致进入 `unavailable`，不能被当作“没有数据”。

### 5.3 任务优先级

固定优先级从高到低：

1. 当前 Application scope 的 Pending / result unknown；
2. 24 小时内可准备的真实面试事件；
3. 已完成但未记录复盘的面试事件；
4. 已存在但未完成的 Material Kit；
5. 已存在且待判断的 Offer；
6. 当前阶段的 Opportunity Fit / Material Kit / Outcome；
7. 其他只读或低频任务。

同优先级使用：

```text
event/offer 的业务时间升序
→ 稳定数字 identity 升序
→ CoreTaskId 字典序
```

时间缺失不能赢得高优先级。Resolver 不自动创建事件、Offer、材料或复盘。

现有 `application_event_id=null` 的 Application 级复盘继续由 `application.general_review` owner 承接，不被强行绑定到某场面试。只有具有可信 Event identity 的复盘才进入 `application.interview_review`。两类 owner 不自动迁移、合并或改写历史 Note。

### 5.4 Opportunity Fit 唯一 owner

正式交互 owner 固定为 `OpportunityFitReviewDrawer`，并由 `CoreTaskSurfaceController` 持有唯一实例。它承接 V2 triage、deep review、draft、history、confirmation、recovery 和所有 mutation。

现有 `PilotOpportunityFitV2Card` 在切换后只投影 Assistant 已返回的只读结果和“打开岗位判断”入口；它不得再直接执行 triage、confirm、deep review、retry 或 history mutation。其现有交互状态与 callback 在一次性切换中迁移到 Drawer owner 后删除。投递准备卡、Header、Haru、旧深链同样只能打开该 owner，不再各自启动评估。

一次性状态迁移固定为：

| 基线状态/能力 | 切换后 owner | Pilot 投影 |
|---|---|---|
| `pilotV2Draft` 与用户尚未提交的选择 | Drawer controller，以 `applicationId` 隔离 | 仅显示“有未完成判断”，点击聚焦 owner |
| V2 current session/proposal | Drawer query/view model | 显示只读摘要，不持有可变副本 |
| V2/V1 history query | Drawer owner 复用既有 React Query cache | 接收有界只读 history summary |
| Pending confirmation | 既有 Assistant/Pending controller，Drawer 订阅 | 显示“等待确认”并打开同一 confirmation owner |
| result unknown / source conflict | Drawer recovery view model | 显示安全状态和唯一 opener，不执行 retry |
| triage/deep review/confirm/reject/retry | 仅 Drawer owner callback | 不接收这些 callback |

Drawer 关闭只隐藏 Surface，未提交 draft 按现有 Application 草稿所有权保留；切换 Application 时由 controller generation 隔离并执行既有未保存保护。再次从 Pilot 打开同一 Application 必须恢复同一 draft/recovery 状态。Pilot 可以显示当前 proposal 的只读摘要，但不得自行修改或推进它。

AppShell 切换后只保留：canonical opener、只读 result projection 和 owner 需要的 composition state。原 Pilot mutation callbacks、第二份 draft/history state 和“未命中时走 Pilot 旧逻辑”必须删除。

用户可见结构固定为：

```text
当前判断
  开始 / 继续 / 等待确认 / 结果待确认 / 查看结果

历史记录
  统一只读时间线
```

V1/V2 通过一个只读 adapter 转换为：

```ts
interface OpportunityFitHistoryItem {
  internalKey: string;       // source kind + record identity，仅 React key
  createdAt: string;
  summary: string;
  sourceState: 'current' | 'source_changed' | 'unavailable';
  details: SafeOpportunityFitDetails;
}
```

规则：

- `internalKey`、source kind、record ID、schema version 不渲染；
- 排序为业务时间降序，再按内部 source ordinal 和数字 ID 降序；
- V1 历史只读，不恢复写入口；V2 是唯一当前 proposal/confirmation owner；
- 不删除、迁移、回填或重写历史数据；
- 某一路历史读取失败显示“部分历史暂时不可用”，不伪装为空；
- source conflict、Provider unknown 和 result unknown 保持现有恢复语义，不自动重新执行；
- 默认结果面只显示用户结论和来源状态，不显示“V1/V2、评估 #、阶段、快照与哈希”。

### 5.5 Material Kit 唯一 owner

所有投递材料入口固定映射到 `application.material_kit`，由一个 Material Kit owner 持有 Application、JD Version、Resume 选择、草稿、confirmation 和提交记录。

状态与唯一主操作：

| 状态 | 主操作 | 附加保证 |
|---|---|---|
| 来源加载中 | 无 | 不调用 Provider |
| 缺少 JD | 补充岗位资料 | 仅导航 |
| Resume 未选择 | 选择本次简历 | 不自动选择多份简历中的基础简历 |
| 尚未生成 | 生成投递准备 | 沿用现有 generation identity |
| 草稿已修改 | 保存修改 | 不同时突出“重新生成” |
| 等待确认 | 查看并确认 | Pending 为真值 |
| 结果未知/冲突 | 确认处理结果 | 只走既有 recovery，不创建新 mutation |
| 已准备、未记录投递 | 记录已投递 | 沿用现有 submission/evidence 流程 |
| 已记录投递 | 查看本次投递记录 | 只读 |

打开、预填、返回、关闭和重复 handoff 的 Provider、Chat、SSE 和写请求均为 0。生成、修改、保存和记录投递继续保持现有 HITL、CAS、Ledger、幂等和 source revision 语义。

AI Resume proposal 只能作为“调整本次简历”内部次操作，不能成为第二个 Material Kit 主入口。

### 5.6 Offer 与 Outcome

- `application.offer_review` 是 Application 级 owner，可表达零份、一份或多份 Offer；`suggestedOfferId` 只是经归属验证后的焦点提示，不进入 Task identity；
- 特定 Offer 的卡片、详情、Pilot 和 Application entry 都打开该 Application owner，并在 owner 内聚焦同一 Offer；不可信或历史未绑定 Offer 只读显示兼容警告；
- `application.record_outcome` 继续使用既有确定性/确认流程；打开页面不等于写入结果；
- 结果未知、stale、conflict 和 terminal replay 不被统一成普通失败；
- 本期不新增 Offer 比较、谈薪或 outcome 业务逻辑。

## 6. Interview Event Surface

### 6.1 自包含只读契约

为避免两个独立 HTTP 请求之间的状态漂移，`/api/interviews` 的 `InterviewIndexItem` 增加：

```json
{
  "event_status": "todo",
  "duration_minutes": 60,
  "scheduled_at_state": "present"
}
```

三个字段必须来自生成该 index row 的同一 `ApplicationEvent`。它们是 additive read fields：

- 不新增数据库列或 migration；
- 不改变 ApplicationEvent 写入协议；
- `event_status` 不从时间、note、标题或 Application stage 推断；
- 为兼容旧客户端，现有 `scheduled_at` 字段继续保持原序列化；新 UI 必须通过 `scheduled_at_state=present|absent` 区分真实排期与当前 `datetime.min` 兼容占位，不能把占位当作真实日期；
- `duration_minutes` 是数据库中的原始整数；V1 只接受 `1..10080`（最长七天）的整数，其他值在投影中为 unavailable，不静默修正，也不借本项目改写历史数据；
- JSON/TypeScript/Python schema 和 API golden 同步更新；
- 缺失或未知值在新 canonical UI 中为 `unavailable`；旧客户端继续忽略该字段。

`preparation_available` 保留字段兼容，但不再恒为 `true`：只有 status 属于封闭 active set、存在真实排期且 duration 合法时为 true；JD、Resume、当前时间和其他来源条件由 canonical owner 继续收紧。最终执行仍由现有 API/Repository/Tool Pipeline 重校验。

AppShell 已加载的 `/api/application-events` 可用于目标 Surface 的最新 preflight；若其 `application_id/event_id/status` 与 index 不一致，点击后显示状态已变化并刷新，Provider、executor 和写入为 0。

Python serializer 与 TypeScript normalizer 共用以下封闭输入真值表，并用同一 golden fixture 校验：

| 输入 | 规范状态 | reason code |
|---|---|---|
| DB `scheduled_at is None`，`scheduled_at_state=absent` | absent；忽略旧 `scheduled_at` 哨兵 | `schedule_absent` |
| state=present + 合法 RFC3339 | present | - |
| state=present + `""`、`0001-01-01...` 哨兵或非法 RFC3339 | unavailable | `schedule_invalid` |
| state 缺失/未知/非字符串 | unavailable | `contract_field_missing` / `contract_field_invalid` |
| duration 为 JSON integer `1..10080` | valid | - |
| duration 缺失/null/bool/小数/0/负数/>10080 | unavailable | `duration_invalid` |
| status 缺失/空/未识别 | unavailable | `status_unknown` |
| index 与 ApplicationEvent identity/status 不一致 | unavailable | `source_mismatch` |

`in_progress + absent/invalid schedule` 固定为 `unavailable`，不是 `needs_status_update`。只有合法排期、合法 duration 的 active event 才能进入时间相关分桶。字段缺失与值非法使用不同 reason code，不能都伪装成普通空状态。

### 6.2 Event 行唯一性

Interview index 必须先把每个 Event 投影成唯一 row，再执行 offset/limit 分页。现有直接 `outerjoin(InterviewNote)` 不再作为 canonical 查询。

唯一 Note 选择规则固定为：

```text
application_event_id 相等
→ created_at 降序
→ note.id 降序
→ 取第一条
```

实现使用子查询/window 或等价的 Session-bound 查询，使 `list()` 和 `get()` 共用同一规则。不得先展开多 Note、分页后再在 Python 去重；否则会丢 Event、产生重复卡片并破坏 cursor。Application 级未绑定 Note 不进入 Event index，由 `application.general_review` 承接。

### 6.3 EventLifecycleV1

```ts
type EventLifecycle =
  | 'scheduled'
  | 'in_progress'
  | 'completed'
  | 'cancelled'
  | 'unknown';
```

封闭映射：

```text
todo / pending / scheduled → scheduled
in_progress                → in_progress
done / completed            → completed
cancelled                    → cancelled
deleted / soft_deleted       → cancelled（兼容只读，不提供操作）
其他 / 空值                  → unknown
```

基线正式写值仍为 `todo/done/cancelled`。`pending/scheduled/in_progress/completed/deleted/soft_deleted` 只作为历史/兼容只读映射；本项目不得让任何新写路径产生这些 alias。

`EventLifecycleV1` 是 ApplicationDetail、InterviewV01View、InterviewReadinessCenter、AppShell handoff 和 task resolver 的唯一事件分类器。旧的局部 `ENDED_EVENT_STATUSES`、独立时间分桶和各组件 alias set 必须删除；一个事件不能在不同消费者中得到不同 lifecycle。

`preparation_available` 的精确条件是：

```text
event_status ∈ {todo,pending,scheduled,in_progress}
AND scheduled_at_state = present
AND duration_minutes ∈ 1..10080
```

它只是 read hint；past active、JD/Resume 缺失和执行时 stale 仍由前端 owner 与现有业务 preflight 收紧，绝不放宽权限。

### 6.4 分桶规则

`projectInterviewEventCard(index, now)` 是纯函数。`now` 显式注入，状态优先，时间只用于排序和识别未关闭事件：

| lifecycle | 时间 | bucket |
|---|---|---|
| completed | 任意 | history/completed |
| cancelled | 任意 | history/cancelled |
| scheduled | 有效未来 | upcoming |
| scheduled | 现在或过去 | needs_status_update |
| in_progress | 当前不晚于 `scheduled_at + duration` | upcoming/in_progress |
| in_progress | 已超过预计结束 | needs_status_update |
| scheduled / in_progress | `scheduled_at_state=absent` 或时间无法解析 | unavailable |
| unknown | 任意 | unavailable |

因此：

- 未来但 `done` 的事件进入已完成；
- 过去但仍 `todo` 的事件显示“状态待更新”，绝不伪装成完成；
- 页面不会因时钟变化自动写状态；
- `duration_minutes` 非法时不能为 `in_progress` 推断结束，进入状态待更新/不可用的封闭分支；
- 相同时间按 `event_id` 稳定排序，不依赖接口顺序。

### 6.5 卡片主操作

| 卡片状态 | 唯一主操作 | 次操作 |
|---|---|---|
| upcoming | 准备面试 | 查看投递 |
| in progress | 进入面试准备 | 查看投递 |
| completed + 无复盘 | 记录复盘 | 查看投递 |
| completed + 有复盘 | 查看复盘 | 查看投递 |
| cancelled/deleted | 无 | 查看投递 |
| needs status update | 更新事件状态 | 查看投递 |
| unavailable | 无 | 重试/查看投递 |

“整理为故事”“保存为沉淀”“复盘建议”不再与复盘主操作平级；它们位于复盘结果或经历素材内部。本期不自动执行这些后续动作。

`needs_status_update` 只导航到既有事件编辑面，不自动写 `done`，也不调用 Provider。

### 6.6 准备上下文与 ResumeSelectionLease

卡片进入准备时创建：

```ts
interface InterviewEventHandoff {
  applicationId: number;
  eventId: number;
  resumeId?: number;
  source: 'interview_event_card' | 'application_task' | 'pilot' | 'deep_link';
}
```

目标 Surface 自动锁定可信 Application/Event，并按 Application scope 读取当前 JD，不再让用户重复选择投递和事件。它不把 JD 原文、event notes 或 Resume 内容塞进路由参数。

Resume 选择固定为：

1. 当前 Surface lease 中，用户对同一 `applicationId + eventId` 明确选过且仍可见的 Resume；
2. 已有 Material Kit/Submission snapshot 明确保存的 Resume identity，且来源仍可验证；
3. 可见、未删除 Resume 恰好一份时预填；
4. 其他情况保持未选择，并只询问一次。

禁止：

- 多份 Resume 时因 `is_master` 自动替用户决定；
- 跨 Application 或跨 Event 复用选择；
- 把瞬态选择写入 Application；
- 选择失效后继续调用 Provider；
- 打开卡片即开始模型调用。

`ResumeSelectionLease` 只属于当前 UI controller generation；close/cancel/application switch 后撤销，不持久化。

现有 Pilot 某些入口只有 `applicationId`，没有可信 `eventId`。这类入口不得构造不完整的 `application.interview_prepare/review`，也不得自动选择“最近一场”或按时间猜测：

```text
Pilot application-only intent
  → navigation_only: application interview event chooser
  → 展示该 Application 的 EventLifecycleV1 卡片
  → 用户明确选择 Event
  → 创建 applicationId + eventId 的 CoreTaskRef
```

零事件时只提供现有“添加/管理事件”导航；一场事件时仍展示单卡并等待用户点击；多场事件按既定稳定规则展示。选择器不调用 Provider、不写业务状态。AppShell 中允许 `eventId?` 的旧 callback 在 canonical path 切换后拆成 `open chooser` 和 `open exact task` 两个封闭接口。

### 6.7 三个面试入口收敛

```text
即将进行
  → 只显示真实 Event Card
  → 不再先显示通用 Application/Event/Resume 表单

已完成
  → 每卡一个复盘主操作
  → 故事和沉淀进入结果内部

自由练习
  → 一个自由练习工作区
  → 题库 / 推荐训练 / 自定义题目是内部来源选择
```

AppShell 顶部“开始练习”、面试页快速练习和题库入口必须映射到同一个 `interview.free_practice` owner。Pilot 和投递详情保留上下文入口，但不保留第二套 handler 或局部业务 owner。

## 7. Materials & Resume Surface

### 7.1 互斥用户分类

| 领域事实 | 用户位置 | 用户名称 |
|---|---|---|
| `Resume.is_master=true,parent_resume_id=null` | 简历 | 基础简历 |
| 有效 `Resume.parent_resume_id` | 简历 | 岗位版本 |
| 独立非 master Resume | 简历 | 其他简历 |
| 已确认 `InterviewStory` | 经历素材 | 经历故事 |
| `KnowledgeNote.origin_kind=confirmed_interview_capture` | 经历素材 | 已确认面试片段 |
| 外部 `KnowledgeSource` markdown/text/bundle | 参考资料 | 参考资料 |
| 原始 InterviewNote/Proposal/Pending | 面试 | 面试复盘 |
| MaterialKit | 投递详情 | 投递材料 |

分类以 typed source/origin 字段为真值，禁止通过标题、正文、路径或按钮文案猜测。未知、缺失或不支持的类型进入 `unclassified`，不得静默放入参考资料或经历素材，也不得触发迁移或写入。

分类优先级固定为：

```text
KnowledgeNote.origin_kind = confirmed_interview_capture
+ 完整 KnowledgeCapturedSourceMetadata
  → experience_material/confirmed_capture

KnowledgeSource.source_kind = captured_interview_note
或存在 capture metadata，但确认 Note/关系缺失或损坏
  → captured_unavailable（留在面试来源，不进入任一素材列表）

source_kind ∈ {markdown,text,bundle}
+ 不存在 capture metadata/origin
  → external_reference

其他自由字符串、关系冲突或未知版本
  → unclassified
```

确认面试来源的判定优先于 external kind；实现不得只看到 `markdown/text/bundle` 就跳过 metadata/origin 完整性检查。orphan capture 和损坏关系显示安全 unavailable，不回退参考资料。

### 7.2 经历素材与参考资料

`reviews` canonical route 只投影已确认 Story 和已确认面试片段。`knowledge` canonical route 只投影外部 Knowledge Source。

实现上可以共享 loader/cache，但必须有两个封闭 projector：

```text
projectExperienceMaterials(...)
projectExternalReferences(...)
```

规则：

- 同一记录最多进入一个用户分类；
- 原始复盘、AI 预览、生成中 proposal 和 Pending 不进入素材库；
- 内部面试 capture 即使命中 Knowledge 搜索，也必须从参考资料投影中过滤；
- 外部资料不能因与面试文本相关而进入经历素材；
- 来源变化显示“来源已更新，已确认内容仍保留”；
- 来源缺失/读取失败与空列表严格区分；
- 页面切换不复制 fetch owner、不增加 Chat/SSE/Provider 请求。

### 7.3 ResumeLineageV1

```text
is_master=true 且 parent=null
  → base

parent 指向当前可见、非删除、非自身、无循环的 Resume
  → job_variant

parent=null 且 is_master=false
  → independent

父缺失 / 自引用 / 循环 / master 与 parent 冲突
  → relationship_unknown
```

`resolveResumeLineage()` 是纯函数，不修复数据库：

- 基础简历显示“基础简历”；
- 有效派生记录显示“岗位版本”和“基于 <可见名称>”；
- 独立记录显示“其他简历”；
- 异常关系显示“关系待确认”，不虚构父记录；
- 只有父记录可见且关系有效时展示 lineage；
- `is_master`、`parent_resume_id` 保持内部 API 字段，不直接渲染。

动作保持既有业务语义：编辑、复制、对比、允许的删除和设为基础简历。关系异常记录不提供会制造新关系的操作。本期不自动创建岗位版本、不自动绑定 Application，也不改变 Material Kit 的明确 Resume 选择要求。

### 7.4 跨入口一致性

简历库、ApplicationDetail、Interview、Story Drawer、Material Kit、Haru、Pilot、快速打开和旧深链必须复用同一分类器、label mapper 和 Task opener。入口不得各自判断 `is_master`、`origin_kind` 后生成不同用户术语。

## 8. 文案、隐私与可访问性

### 8.1 用户语言

默认产品界面、Tooltip、Toast、空状态和可访问名称不得出现以下内部表达：

```text
V1 / V2
评估 #<id>
旧版评估
快照与哈希
原 key
VAD 帧
证据门控
任务编号
Worker / worker
队列阶段
心跳
source_id / snapshot_id
fingerprint / sha256
CAS / Ledger / Pending Action
```

不得使用简单 substring 扫描 `ID`、`key` 或“阶段”，以免误伤 JD、键盘说明和正常求职阶段。门禁使用上述完整 lexeme/正则和精确 allowlist。

用户可见替代词：

| 内部状态 | 用户文案 |
|---|---|
| source current | 当前信息 |
| frozen source | 已使用当时版本 |
| source changed | 来源已更新，已有结果仍保留 |
| result unknown | 处理结果待确认 |
| worker pending/running | 处理中 |
| source unavailable | 部分来源暂时不可用 |
| master resume | 基础简历 |
| derived resume | 岗位版本 |

confirmation token、operation/owner/lease identity、HMAC、raw exception 和原始内部错误响应在任何生产 UI（包括所谓高级信息）中都不得显示。真正需要排障的数据只存在于既有安全日志/Journal，不新增前端调试面。

### 8.2 可访问性与桌面矩阵

- 每个 Surface 有唯一 heading 和唯一主操作；
- 状态不能只靠颜色，必须有文字或图标；
- Drawer/Panel 打开后焦点进入 heading，关闭后回到发起入口；
- 重复 opener 聚焦既有 Surface，不重置焦点栈；
- 任务卡、Tabs、历史列表和对话确认可全键盘操作；
- 遵守 `prefers-reduced-motion`；
- 768、1024、1280、1440 宽度无页面级横向溢出；
- 本期不为移动端增加分支、断点或行为。

## 9. 请求、副作用与并发不变量

### 9.1 打开预算

以下动作：打开、重复打开、聚焦、关闭、返回概览、切换 Tab、展开历史，必须满足：

```text
Provider calls = 0
Tool executor calls = 0
Chat mutations = 0
SSE subscriptions = 0
Pending/Ledger/domain writes = 0
```

允许 canonical owner 使用现有 query cache 发起必要只读请求。相同 owner 重复打开不得新增同 key 读请求；多个 Surface 不得各自创建同一集合 fetch owner。

### 9.2 执行预算

真正点击生成、保存、确认、记录复盘或结果时：

- Provider/Tool 调用数不超过 baseline 对应流程；
- 写工具仍单次执行；
- read/read、read/write、write/read、write/write 选择规则不变；
- no fallback after first streamed delta；
- reject、terminal replay、delivery recovery 仍 Provider-free；
- UI projector/renderer 失败不得重跑 executor；
- 网络/transport unknown 不得自动创建新 operation。

### 9.3 stale 与跨请求变化

TaskRef 和 handoff 只是导航身份。进入 owner 后必须重新读取/验证现有业务事实：

- Application/Event/Offer/Resume 归属变化；
- source revision 变化；
- Pending 已替换；
- operation 已 terminal；
- Resume 已删除；
- event status 已完成或取消。

校验失败进入固定 stale/unavailable 状态，executor 为 0。前端缓存不能取代 Repository/API 权威校验。

## 10. 路由、旧入口与生产切换

### 10.1 路由兼容

现有 `ViewMode`、URL/query/hash、Command Palette 和 Haru/Pilot page context 保留。旧深链只做：

```text
legacy location
  → parse/validate
  → CoreTaskRef
  → canonical owner
```

非法或无法归属的旧链接显示安全不可用，不回退旧 handler。路由中只保存安全 ID 和 view，不保存正文、草稿、token 或内部 fingerprint。

### 10.2 一次性切换

本项目允许内部破坏性删除：

- 删除 ApplicationDetail 中重复的岗位判断/材料/面试业务 owner；
- 删除 Interview 首页嵌入式通用准备表单和重复自由练习入口；
- 删除 V1/V2 平级标题与历史入口；
- 删除 Knowledge 页中的面试捕获投影；
- 删除组件内分散的 Resume/Knowledge 用户文案判断；
- 删除旧 handler、feature flag、shadow surface 和隐式 fallback。

不保留双轨 façade。旧深链兼容通过 registry 显式映射，不保留旧执行路径。

### 10.3 变更 allowlist

实施计划应以固定 baseline 生成精确 allowlist，原则上仅允许：

- `web/src/features/coreTaskSurface/**`；
- Application、Interview、Material、Resume 相关现有组件与测试；
- `web/src/layout/AppShell.tsx` 及其精确测试；
- Interview index 的 Python schema/repository/API serialization 和精确测试；
- 本项目 spec、plan、review、report 与只读 fixtures。

禁止顺手修改 Agent Runtime、Tool Metadata、Journal、Ledger、Context Projector、数据库模型/migration、README 和移动端代码。

## 11. 机械门禁与测试矩阵

### 11.1 只读 baseline 资产

从 `93fb006` 独立捕获并提交：

1. `core_task_entrypoints_93fb006.json`：文件、qualified symbol、入口类别、目标业务任务；
2. `core_task_request_counts_93fb006.json`：基线真正执行流程的 Provider/HTTP/SSE/mutation 上限；
3. `core_task_visible_copy_93fb006.json`：需删除或改写的用户可见内部 lexeme；
4. Interview index API golden。

测试不得自动生成、覆盖或接受新 manifest。新增、缺失、未分类入口必须失败；显式设计变更只能人工审阅 fixture diff。

### 11.2 Registry/AST 门禁

机械证明：

- `CoreTaskId` 集合与 registry key 精确相等、无重复、无缺 owner；
- 每个 audited entrypoint 精确分类为 `core_task | navigation_only | record_management`；其中 core task 精确映射一个 TaskId，另外两类只能进入各自的显式 allowlist；
- Application/Interview/Materials 入口只能调用 canonical opener；
- Provider、Chat mutation、Material generation、Opportunity Fit write、Interview write 只能存在于 owner allowlist；
- `OpportunityFitReviewDrawer` 是唯一 Opportunity Fit mutation owner；`PilotOpportunityFitV2Card` 只能渲染结果并调用 opener；
- canonical host 不引用 legacy local Drawer/handler；
- ApplicationDetail、InterviewV01View、InterviewReadinessCenter 与 AppShell 不得保留独立事件状态集合或时间终态推断，只能调用 EventLifecycleV1；
- 不存在 feature flag、shadow render、双执行或“未命中则旧路径”；
- `reviews` 不渲染 external KnowledgeSource，`knowledge` 不渲染 confirmed interview capture；
- 用户文案 mapper 是唯一 Resume/source label 入口。

### 11.3 Application 测试

- 所有 Application stage、JD absent/error、Offer ownership、Pending、unknown、deleted；
- primary 唯一、TaskRef 无重复、输入乱序结果稳定；
- Header、准备卡、Pilot、Haru、旧深链映射同一 owner；
- application-level review 与 event-level review 均可达且不能互相改绑；零/一/多 Offer 均进入 Application 级 Offer owner；
- Opportunity Fit V1/V2 统一历史的稳定顺序、部分失败、source changed、result unknown；
- Pilot 卡片的旧 triage/confirm/deep-review/retry mutation callback 已删除；所有 mutation 只发生在 Drawer owner；
- Pilot → Drawer 的 draft/history/Pending/unknown/source-conflict 状态迁移、关闭恢复、Application 切换 generation 与只读 proposal 投影；
- Material Kit 每个状态恰好一个主操作；
- 打开/重复打开/关闭不生成 Provider/SSE/write；
- 既有 approve/modify/reject、CAS、Ledger、recovery 调用次数和副作用保持。

### 11.4 Interview 测试

- `event_status/duration_minutes/scheduled_at_state` 来自同一 ApplicationEvent row；API 字段缺失/非法的安全处理；
- 多 Note Event 只生成一条 index row，选择最新 Note 后再分页；Application 级未绑定 Note 仍可达；
- future done、future cancelled、past todo、past in_progress、unknown、无时间、非法 duration；
- scheduled_at 的 absent/empty/sentinel/invalid RFC3339，duration 的 missing/null/bool/fraction/0/negative/oversize，且 Python/TypeScript reason code 一致；
- 相同时间以 event ID 稳定排序；
- index/event application mismatch fail-closed；
- 单 Resume 预填、多 Resume 不静默选择、trusted snapshot 复用、删除后撤销；
- Pilot 无 eventId 的零/一/多 Event chooser 均不隐式绑定；用户选择后才创建 exact TaskRef；
- upcoming 无通用选择表单、completed 每卡一个复盘主操作、practice 一个正式入口；
- status 在列表与点击间变化时 Provider/executor/write 为 0；
- sync/SSE/HITL/terminal replay baseline 等价。

### 11.5 Materials/Resume 测试

- confirmed capture 与 external source 互斥；capture metadata/origin 优先于 external kind，orphan capture 不回退参考资料；未知 origin 不静默分类；
- Story、raw note、proposal、Pending 的资格矩阵；
- base/job variant/independent/missing parent/self-cycle/multi-cycle/conflict/deleted parent；
- lineage projector 为纯函数，渲染不自动复制、绑定、修复或删除；
- 相同记录从 Resume Library、Application、Interview、Material Kit、Haru/Pilot 得到相同标签；
- forbidden lexeme 覆盖 visible text、aria-label、Tooltip、Toast 和 error boundary；
- 请求路径/payload golden 不变，不新增 Provider/Agent 调用。

### 11.6 并发与浏览器

- double click、Strict Mode double render、重复 deep link、Haru/Pilot 同时 handoff；
- stale close/focus generation、Application 快速切换、Pending 中切页；
- active SSE 时开关 Task Surface 不创建第二消费者；
- 768/1024/1280/1440 四档桌面浏览器；
- light/dark、键盘、reduced motion；
- 无控制台错误、重复 API、重复 SSE 或页面级横向溢出。

### 11.7 发布级门禁

最终至少执行：

```text
focused frontend + backend contract tests
full frontend tests
TypeScript
production build
supported full backend pytest matrix
Ruff
Mypy
static smoke
local verify
controlled real-AI verify
browser combination matrix
AST/manifests/allowlist/diff-check gates
independent CR: no open P0/P1/P2
```

外部 orchestrator gate、Docker 或环境依赖若不可用，必须如实列出，不能伪造输入或宣称通过。

## 12. 实施批次与所有权

复审通过后再编写测试先行实施计划。建议批次：

1. 捕获 baseline manifests，建立 RED 门禁；
2. CoreTask contracts、registry、controller 与并发测试；
3. Interview index 唯一 Event row、additive read contract 与纯 projector；
4. ApplicationTaskResolver 和 Application 单 owner；
5. Opportunity Fit 历史 adapter 与 Material Kit handoff；
6. Interview Event Card、上下文 handoff、自由练习收口；
7. Materials/Resume 分类器和页面投影；
8. 文案、旧路径删除和 AST 证明；
9. 组合验收、独立 CR 和发布报告。

`AppShell.tsx`、`ApplicationDetail.tsx`、`InterviewV01View.tsx` 等高冲突 composition 文件由单一集成人负责。纯 resolver、API read contract、fixture 和独立展示组件可并行，但每批合入前必须更新到同一 baseline 并跑交叉门禁。

## 13. 独立后续项目：Review-to-Readiness Feedback Loop

本项目完成后，另行设计：

```text
已确认复盘
  → 明确资格与来源 revision
  → 用户选择保存为 Story / 弱点信号
  → HITL + Ledger 写入
  → 下一次面试准备按可信引用读取
```

该项目必须单独处理 source eligibility、版本、Capability、Binding、HITL、Operation、幂等、撤销和 Context Projector Contributor。本期最多保留只读、瞬态 `ConfirmedReviewReference` 用于查看来源或导航；不得自动消费、持久化或触发写入。

## 14. 完成定义

只有同时满足以下条件，才能宣称本项目完成：

- 三个 Surface 的 canonical owner、入口 manifest 和删除门禁全部通过；
- 每个状态只有一个主操作；
- 面试状态由事件事实驱动，past active 不再伪装成 completed；
- V1/V2 不再构成两套用户任务，历史数据仍可只读查看；
- 经历素材、外部参考资料和 Resume lineage 用户分类互斥且稳定；
- 打开/关闭/切换的 Provider、Chat、SSE 和业务写入均为 0；
- 既有 HTTP/SSE/HITL/Ledger/Provider/Tool 副作用未退化；
- 无旧 handler、双轨、fallback 或用户可见工程术语；
- 完整发布矩阵和独立 CR 通过；
- 验收报告明确 additive Interview read fields、最新 Note 的唯一投影、内部破坏性 UI 切换、无数据库迁移，以及 Review-to-Readiness 仍属后续项目。
