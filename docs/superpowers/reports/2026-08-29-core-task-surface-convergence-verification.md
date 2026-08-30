# Core Task Surface Convergence 验收报告

状态：实现代码已完成独立规格与质量复审，`SPEC_APPROVED / QUALITY_APPROVED`，无开放 P0 / P1 / P2。分支未 push、未 merge。

## 基线与范围

- 固定 baseline：`93fb0063118761f2c76e71e4209000feee0f755b`
- Code-review HEAD：`76f17dd`（`fix: AI 统一投递主操作与来源阻断`）
- Branch：`refactor/20260829-core-task-surface-convergence`
- Worktree：`D:\Users\yuqi.chen\offerpilot\.worktrees\refactor-20260829-core-task-surface-convergence`
- 实现范围限定为 Application Task Surface、Interview Event Surface、Materials & Resume Surface，以及对应的受控 Interview read contract。
- 未新增 Agent Tool、Provider 路径、SSE transport、后台队列、Application/Resume 持久绑定或 Review-to-Readiness 自动反馈。

## 改了什么

### Core Task 与 Application

- 建立封闭的 11 项 `CoreTaskId` registry、严格 TaskRef parser、单实例 `CoreTaskSurfaceController` 与 generation-safe host。重复 Header、任务卡、Haru、Pilot、深链和命令入口只会聚焦同一个 owner。
- `ApplicationTaskResolver` 以不可变 source snapshot、显式 `now`、固定优先级和稳定 tie-break 产生唯一 primary。Pending、result unknown、来源冲突、非法身份和删除/过期数据均 fail closed。
- 投递详情 Header 改为消费 resolver primary；任务卡、事件卡和 chooser 同时遵守 `task.executable`。已完成与待准备面试并存时不再出现复盘/准备两个突出主操作。
- `application.interview_review` 保留 Application + Event identity；历史 `application_event_id=null` 继续由 `application.general_review` 承接。Offer 保持 Application 级 owner，`offerId` 只作 focus hint。

### Opportunity Fit 与 Material Kit

- `OpportunityFitReviewDrawer` 成为 draft、triage、deep review、confirmation、history、result unknown、source conflict 与 recovery 的唯一 mutation owner。
- `PilotOpportunityFitV2Card` 只接收有界、只读投影与 canonical opener；V1/V2 历史经稳定 adapter 合并，V1 只读，部分读取失败不伪装为空。
- Material Kit 使用按 Application 隔离的 owner store 和纯状态投影；缺 JD、缺 Resume、未生成、dirty、等待确认、结果未知/冲突、未记录投递和已记录状态均只有一个主操作。
- 打开、预填、关闭、重复 handoff 和返回不会调用 Provider、Chat、SSE 或业务写入。

### Interview Event

- `EventLifecycleV1` 与 immutable Event Card 成为 ApplicationDetail、Interview、Readiness、AppShell、Next Step 和 resolver 的共同分类依据；终态、过去待更新、非法排期/时长、取消和未知状态均按统一真值表处理。
- Application-only Pilot intent 即使只有一个 Event 也进入显式 chooser；只有用户点击后才创建 exact TaskRef。快速练习会再次校验当前可信 Resume source 与可见 selection。
- 重复/foreign/malformed Event identity 不再依赖输入顺序；准备与复盘入口均复用唯一可信事件投影。

### Materials 与 Resume

- 新增经历素材视图，将已确认面试片段与 Story 从外部参考资料中拆出；Knowledge 只投影 markdown/text/bundle 等外部资料，capture 关系损坏会 unavailable 而不会错误降级为参考资料。
- Source、capture metadata、Story detail 和 hostile Proxy/envelope 均在边界归一化；详情文本经过安全投影与长度限制，不显示内部 source/evidence/job/worker/stage identity。
- Resume lineage 统一为基础简历、岗位版本、其他简历和关系待确认；覆盖父记录缺失、自引用、循环、master/parent 冲突与不可见父记录，且不自动修复数据库。

## 内部破坏性删除

- 删除旧 `PilotOpportunityFitCard` 及其测试；移除 AppShell 内第二份 Opportunity Fit draft/history/mutation/recovery owner 和 Pilot V2 mutation callback。
- 删除旧局部 Event 终态集合、时间终态推断、nearest-event 自动选择、重复精确事件 `find/some` 路径和通用面试 fallback。
- 删除重复的 Material Kit / Interview owner handoff、旧 handler 和 shadow/fallback 入口；任务入口必须由固定 manifest 分类为 `core_task`、`navigation_only` 或 `record_management`。
- 删除 reviews/knowledge 交叉投影及 V1/V2、hash、session/key、Worker、队列、内部 source identity 等用户可见文案。
- 这些是前端内部一次性 cutover；未删除或迁移 V1 历史业务数据。

## `/api/interviews` 受控只读契约

- 响应加法新增 `event_status`、`duration_minutes`、`scheduled_at_state`。
- Interview index 先按 `application_event_id` 选择 `created_at DESC, id DESC` 的唯一最新 Note，再排序和分页，因此每个 Event 只返回一行；`list()` 与 `get()` 共享同一 statement。
- `preparation_available` 由可信 status、排期和 `duration_minutes ∈ [1, 10080]` 共同决定。
- 该变化未新增数据库字段，也没有 migration。现有 Application-level Note 不会被塞入 Event index。

## 副作用等价证据

- 固定只读 baseline 资产记录 entrypoint、visible copy、Interview API golden 和 16 条 Provider/HTTP/SSE/mutation count；生产代码不能读取或更新这些资产。
- AST/源码门禁验证每项 CoreTask 恰好一个 owner、全部入口已分类、所有 Event consumer 绑定 `EventLifecycleV1`、Pilot Fit 无 mutation callback、无 legacy/shadow fallback、materials 分类互斥。
- baseline count 规定 surface open / repeat focus / close 的 Provider、HTTP read/mutation、SSE、Tool executor 和 domain write 均为 0；controller、composition 和真实浏览器请求记录均满足。
- Fit、Material、Interview preparation/review 与 free-practice 的 service golden 保持既有 URL、payload、HITL、idempotency、CAS 和调用上限；本项目未改 Ledger、Journal、terminal replay、Context Projector 或 Tool runtime。
- `oc verify --profile local` 验证 Pending Action 暂停、HITL confirm、写入和 pending clear；`oc verify --profile real-ai` 使用现有受控配置完成真实 Fit triage/deep review、Material proposal、Interview prepare/review/capture、Mock、Chat Pending 和确认清理。

## 自动化验证

| 命令 | 结果 |
|---|---|
| Backend focused：Core assets、Interview index/capture、Knowledge、Resume | 78 passed，241 warnings，510.46s |
| Frontend focused（计划中的 26 files） | 26 files / 470 tests 全通过，54.79s |
| 独立复审 focused 增量矩阵 | 10 suites / 152 tests 全通过 |
| `cd web && npm test -- --run` | 206 files / 1807 tests 全通过，617.80s |
| `cd web && npx tsc -b --pretty false` | 通过 |
| `cd web && npm run build` | 通过，3966 modules，40.89s |
| `uv run ruff check .` | 通过 |
| `uv run mypy src` | 147 source files 通过 |
| `uv run oc smoke --static-dir web/dist` | health、SPA、Application/Event card、Pending/HITL smoke 全通过 |
| `uv run oc verify --profile local --static-dir web/dist` | 全部 local HTTP/HITL/cleanup steps 通过 |
| `uv run oc verify --profile real-ai --static-dir web/dist` | 全部真实 Provider/HITL/cleanup steps 通过 |
| `uv run pytest` | 5010 passed / 4 skipped / 1 external-scope failure，4299 warnings，13684.08s（3:48:04）；失败为 release orchestrator 未提供 `OFFERPILOT_APPLICATION_JD_BASELINE_FILE` |
| Application-JD harness（排除外部 scope node） | 22 passed / 1 deselected |
| `git diff --check` | 通过 |

非阻塞 warning：

- Vitest 仍输出仓库既有 React `act()`、测试 stub DOM 属性及 jsdom warning；无测试失败。
- FastAPI / Starlette 测试仍输出既有 deprecation warning。
- Vite 主 chunk 为 1586.51 kB，超过 1500 kB warning 阈值；本项目未新增 transport，也未把 bundle 优化扩进本次范围。

## 浏览器矩阵

使用内置 Codex Browser、production build 和隔离数据完成不依赖 Provider 的真实桌面走查：

- 768 / 1024 / 1280 / 1440 宽度均满足 `document.scrollWidth === innerWidth`，导航与主内容可见，无横向溢出。
- Dark → Light → Dark 切换正常；键盘 Arrow/Enter 可操作投递详情 tabs；CDP `prefers-reduced-motion: reduce` 下 Haru transition 为 `0s`、animation 为 `none`，结束后已恢复默认 viewport 与 motion。
- 经历素材页面只显示确认片段/Story，参考资料页面只显示外部来源；面试 upcoming 空态与自由练习入口正确。
- 1024 宽度打开 CoreTask 时只存在一个 owner；重复 opener 仍为同一 canonical key，且没有 API/Provider/Tool/Chat/SSE/domain-write。任务活动期间隐藏会遮挡关闭按钮的 contextual Haru，关闭后恢复同一个 Haru/Conversation owner。
- 页面切换、重复 opener 与关闭过程的浏览器 request recorder 未发现重复 API/SSE；经历素材/外部参考资料隔离已在真实页面验证。
- Opportunity Fit approve/modify/reject/unknown 与 Material Kit generate/confirm/conflict recovery 没有在无 Provider credential 的隔离浏览器数据中执行。替代证据来自 26-file / 470-test focused 矩阵中的 `OpportunityFitReviewDrawer.test.tsx`、`PilotOpportunityFitV2Card.test.tsx`、`opportunityFitReviews.test.ts`、`MaterialKitDrawer.evidenceBundles.test.tsx`、`materialKitOwnerStore.test.ts` 和 `materialKits.test.ts`；controlled real-AI verify 只证明受控 API/Provider/HITL 链，不是浏览器 UI 验证。该限制没有被记作浏览器成功。
- 隔离数据补充了 past `todo`、future `todo` 与 completed Event：面试页把 past active 显示为“状态待更新”而不是已完成；Pilot/Application-only 无 `eventId` 复盘入口先打开“选择要复盘的面试”对话框，没有自动选择。
- 浏览器实际投影了“基础简历”“岗位版本 · 基于 …”“其他简历”，并把 self-parent 异常标为“关系待确认”、禁用设为基础简历与复制；`resumeLineage.test.ts` 与 `ResumeCard.test.tsx` 另覆盖 missing parent、循环和 master/parent 冲突。
- 最终 console 无 error/warning。浏览器隔离数据未携带 Provider credential，因此 Fit/Material 的真实执行链由同一 production build 的 component/service 矩阵和已通过的 controlled real-AI verify 补足；没有把错误或假数据冒充浏览器成功。

## 独立 Code Review

独立 reviewer 对 `93fb006..df2c971` 首轮复核发现并关闭：

- P1：Header 用“任意 completed Event”覆盖 resolver primary，导致完成复盘与未来准备同时成为突出操作。
- P2：高优先级 source issue 只清空 primary，却保留低优先级任务的 executable 与可点击旁路。

修复提交 `76f17dd` 让 Header 消费 resolver primary，并让任务卡、chooser、事件复盘入口统一检查 effective executable；补充 completed + upcoming、Pending loading/error/absent 和事件旁路回归。

最终 reviewer 对 `df2c971..76f17dd` 增量复审，并结合首轮 `93fb006..df2c971` 范围给出 `SPEC_APPROVED / QUALITY_APPROVED`：无开放 P0 / P1 / P2，四文件修复未引入第二 Controller、第二 owner 或 mutation 旁路。复审矩阵为 10 suites / 152 tests 全通过，`npm run build` 与 `git diff --check df2c971..76f17dd` 均通过。

## 外部门禁与剩余风险

- 裸 `uv run pytest` 中的 `test_application_jd_implementation_scope_is_machine_checked` 依赖 release orchestrator 提供 `OFFERPILOT_APPLICATION_JD_BASELINE_FILE` 与 `OFFERPILOT_APPLICATION_JD_ALLOWLIST_FILE`；本次 bare run 在缺少 baseline 时按设计失败。没有以当前 HEAD 自生成 baseline/allowlist 来绕过历史范围门禁；其余结果为 5010 passed / 4 skipped，Application-JD harness 排除该外部 scope node 后为 22 passed / 1 deselected。
- 上述浏览器 Provider UI 深链没有在无凭据隔离数据中实际发起；真实 Provider API、HITL、cleanup 已由 controlled `real-ai` profile 成功验证。
- 浏览器合成数据保留在 `D:\Users\yuqi.chen\AppData\Local\Temp\offerpilot-core-task-surface-b6eb1bcc3bb44d998e253aab73f7d59a`；宿主策略阻止了递归清理，因此没有绕过策略强删。该目录不含 Provider credential 或用户生产数据，可由用户稍后手动删除。
- 既有测试 warning 与 Vite chunk warning 保留为 P3 技术债；未发现本项目新增的开放规格或质量缺陷。
- Review-to-Readiness 自动反馈链仍是独立后续项目；本期不会自动生成 Story/Memory/Knowledge、修改 Resume 或写 Event status。

## 集成状态

- 无数据库 migration。
- 分支未 push、未 merge。
- 最终交付前仅剩由 release orchestrator 提供 Application-JD 历史 scope 输入后重跑该单一外部门禁；不得用当前分支自证其历史 baseline。
