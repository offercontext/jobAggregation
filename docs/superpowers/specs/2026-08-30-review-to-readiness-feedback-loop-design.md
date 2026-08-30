# Review-to-Readiness Feedback Loop 设计

> 状态：已复审通过
> 固定 baseline：c5a020cbedd8ff64f6188f51c10d8f4daa7c7dff
> 设计分支：feat/20260830-review-readiness-feedback-loop
> Worktree：D:\Users\yuqi.chen\offerpilot\.worktrees\feat-20260830-review-readiness-feedback-loop
> 本文只定义设计；书面复审通过前不编写实施计划，不修改生产代码。

## 1. 背景与问题定义

main@c5a020c 已完成 Harness 四期、Pilot Runtime、统一 Agent Loop、Scoped Authority、Tool Metadata Convergence 和 Core Task Surface Convergence。桌面端已经把以下用户任务收敛到 canonical owner：

~~~text
application.interview_prepare
application.interview_review
interview.free_practice
materials.story
~~~

当前缺口不再是入口，而是复盘结果没有形成可信、可撤销、可用于下一次准备的闭环：

~~~text
用户保存 InterviewNote
  → Provider 生成有逐字引用的 InterviewReviewProposal
  → 用户可以查看 practice focus
  → 旧 Adaptive Practice 把未确认 Proposal 直接当推荐
  → 下一场 Interview Preparation 看不到用户明确确认过的重点
~~~

Core Task Surface 已把后续项目固定为：

~~~text
已确认复盘
  → 明确资格与来源 revision
  → 用户选择保存为 Story / 准备重点
  → HITL + Ledger 写入
  → 下一次面试准备按可信引用读取
~~~

因此，本期不能只把 Proposal 文本拼进 Prompt，也不能只为 UI 增加一个“开始练习”按钮。必须同时关闭以下事实与执行缺口：

1. InterviewNote 是用户保存的业务事实，但当前没有单调内容 revision。
2. InterviewReviewProposal 是 AI 候选，不是用户确认的能力事实；历史 Proposal 也没有生成时 Note revision。
3. Story Proposal 的确认当前自建 Session 并直接 commit，未进入 Write Operation Ledger，confirmation token 由前端生成。
4. 旧 Adaptive Practice recommendation 会扫描未确认 Proposal，且 application_event_id 表示来源面试，不是下一场准备目标。
5. Interview Preparation V1 不读取复盘信号；其 Provider 调用是独立 Non-Agent AI 边界，不经过 Agent Context Projector。
6. Context Projector 的 confirmed_memory 当前固定 disabled；不能通过普通 Application Chat 隐式扫描全部复盘。

### 1.1 已有能力与本期复用点

| 已有能力 | 本期复用 | 不能推导 |
| --- | --- | --- |
| InterviewNote | 用户保存的面试 Business Record | 不等于 AI 观察；旧记录无 revision |
| InterviewReviewProposal | 严格 JSON、逐字 Evidence、冻结 source/proposal hash | 不等于用户已确认；旧 Proposal 不具备生成 revision |
| Interview Story | source selection、Provider lease、Proposal、版本、Evidence、CAS | 当前 confirm 未进入 Ledger；Story 不等于准备重点 |
| Adaptive Practice | 固定 drill、start 幂等、completion CAS、冻结历史 | 开始、完成、自评均不证明“已掌握” |
| Interview Preparation | Event/JD/Resume/Knowledge 显式选择、冻结快照、Provider lease | V1 不读取 Memory；不能静默改变 V1 |
| Core Task Surface | exact Application/Event、单 owner、draft/Pending 替换保护 | UI 隐藏不是授权；不得新增第二个 Task owner |
| Tool Pipeline / Authority | Capability、Binding、HITL、一次 executor、typed result | Product Action 不能伪装成第 26 个 Provider Tool |
| Write Operation Ledger | operation-scoped commit、terminal replay、commit-unknown、Compensation | 不承诺跨外部系统 exactly-once |
| Context Projector | one-snapshot Source Loader、预算、Manifest、fail-closed | 普通 Chat 不能自动读取全部 Signal |

## 2. 目标、兼容口径与非目标

### 2.1 目标

本期建立两条显式、互相独立的 Product Action：

~~~text
路径 A：准备重点

current Review Proposal focus
  → 用户在 application.interview_review owner 中选择
  → save_review_readiness_signal Product Action proposal
  → Product Action HITL approve / modify / reject
  → Ledger + Confirmed Readiness Signal 同事务
  → 可选的 exact target Event Adaptive Practice
  → exact target Event 的 Preparation V2 显式选择
~~~

~~~text
路径 B：经历素材

Story Proposal ready
  → confirm_interview_story Product Action proposal
  → Product Action HITL approve / modify / reject
  → Ledger + Story Version 同事务
  → required Undo 通过独立 Compensation Operation
~~~

完成后必须满足：

- 任何长期准备重点都能追溯到用户保存的 exact Note revision、V2 Proposal、focus 和逐字 Evidence；
- Product Action 由服务器密封，模型不可见，不加入 Provider Surface 或 Legacy Catalog；
- Provider 可见工具仍精确为 25 个，Legacy deterministic 仍精确为 3 个；
- Story Proposal 确认和准备重点保存都经过显式 HITL、Ledger、CAS、terminal replay 与 required Undo；
- Product Action 不制造 Conversation、Chat Pending、ChatMessage、ToolMessage、AgentRun 或 Journal tool event；
- 下一次准备只使用用户为 exact target Event 显式选择的 current Signal Version；
- Adaptive Practice V2 必须同时绑定 Signal Version 和 exact target Event；
- 练习完成只表示 practiced，不表示 mastered、ready 或能力已证明；
- 基础 InterviewReadinessResult.ready 仍只由 Application/Event/JD/Resume 输入决定；
- Story 与准备重点是两个独立动作，不共享 Operation、token、idempotency key 或事务。

### 2.2 兼容口径

本期采用：

> 业务安全、Provider Surface 与 Chat/Agent 协议严格兼容；领域表、内部 Service 和 Product Action 执行路径允许受控破坏性切换。

必须保持：

- Chat HTTP/SSE、Agent Pending、approve/modify/reject、terminal replay 和 delivery fencing 不变化；
- Provider Tool golden 仍为 25 个，名称、顺序、描述和完整 JSON Schema envelope 均不变化；
- 3 个 Legacy deterministic route 的入口、恢复、CAS、写入和 Provider 不可见性不变化；
- 现有 4 个 Agent Compensation 的名称与语义不变化；
- 普通 Chat、Haru 闲聊和 Application Chat 不自动查询或加载准备重点；
- 旧 InterviewPreparationProposal V1、旧 AdaptivePracticePlan 和历史 Story/WriteOperation 数据继续可读；
- Journal 仍是 fail-open 诊断，不成为 Product Action、Signal、Story、Practice 或 Preparation 的业务真值；
- 不新增 retry、shadow write、双写、旧 executor fallback 或第二套业务 Registry。

允许的明确变化：

- 新增 0029_review_to_readiness_feedback migration；
- InterviewNote 增加 content_revision/updated_at；
- InterviewReviewProposal 增加记录 contract version/source_note_revision；
- 新增 Product Action proposal 私有路由材料和 Confirmed Readiness Signal 表族；
- write_operations 增加 product_action primary shape 与 2 个 Product Action Compensation；
- Story Proposal confirm 的生产路径切换到 ProductActionCoordinator；旧自建 Session confirm executor从生产调用图删除；
- 新 Adaptive Practice 只能来自 confirmed Signal Version，并必须显式绑定 target Event；
- Preparation 显式携带 feedback selection 时使用 Input V2；字段缺失继续生成 V1 字节等价快照。

现有 Story confirm HTTP 路径和请求字段保留为兼容入口，但它只能调用新的 ProductActionCoordinator，禁止直接调用旧 confirm_attempt()。新 Attempt 的同名 confirmation_token 字段改为回显并校验服务器签发的 Product Action token；只有 pre-0029 historical-ready bridge 才把旧客户端 token 当作请求幂等输入而非执行能力。该兼容入口不是旧执行 fallback。

### 2.3 非目标

本期明确不实现：

- 通用 Memory 产品、全局偏好画像、skill ontology、跨 Application 能力图谱或自动“弱点检测”；
- 把 Provider practice_focus、自评或练习完成解释为弱点、能力或招聘结果事实；
- 综合 readiness 分数、通过率、排名或招聘预测；
- 自动扫描全部历史、自动选“最重要”信号、自动选下一场 Event/Resume；
- 自动创建 Story、准备重点、练习、Knowledge Note、Question、Reminder 或 Resume 修改；
- 把 summary、clarification、next_question 或无 Evidence 内容保存为 Signal；
- 新模型调用、Review Proposal 生成策略重写或通用 retrieval；
- 新顶层导航、新 CoreTaskId、移动端、SSE replay、后台队列或多 Agent；
- 把手工 Story create/version/archive/restore 全部迁入 Ledger；本期只迁移 Story Proposal 的确认写入；
- 扩大或改变已有 Chat Pending/Agent confirmation 协议；
- 宣称跨请求、跨 Provider、跨数据库或跨外部系统 exactly-once；
- 改变历史 InterviewReviewProposal 在 Note 删除后仍保留冻结 snapshot 的既有审计/隐私语义；该问题需独立产品决策和 migration。

## 3. 事实层级与领域语言

### 3.1 事实层级

~~~text
InterviewNote
  用户保存的本次面试 Business Record

InterviewReviewProposal V2
  基于 exact Note revision 的 AI 候选
  必须通过逐字 Evidence 校验
  仍不是用户确认事实

ConfirmedReadinessSignalVersion
  用户通过 Product Action HITL 确认的“准备/练习重点”

AdaptivePracticePlan V2
  用户针对 exact target Event 做过的一次练习

InterviewPreparationProposal V2
  针对 exact Event/JD/Resume 和显式 Signal selection 的冻结建议

InterviewStoryVersion
  用户通过独立 Story Product Action 确认的经历素材版本

WriteOperation
  写入状态与回放真值

Journal
  fail-open 诊断；无 AgentRun 的 Product Action 不产生 Journal event
~~~

任何投影不得逆向提升事实等级：

- Proposal 存在不能推导 Signal 已确认；
- Signal 已确认不能推导用户已练习；
- plan completed 或 self_assessment=confident 不能推导 Signal 已解决；
- Preparation 引用了 Signal 不能改变 Signal；
- Story 与 Signal 不能互相替代；
- Journal 或前端成功 Toast 不能代替 Ledger terminal 和领域行。

### 3.2 “准备重点”而不是“弱点”

V1 的持久 subtype 固定为：

~~~text
interview_preparation_focus_v1
~~~

用户可见语言固定为“复盘后的准备重点”或“下次练习重点”。不得使用“弱点事实”“能力不足”“已掌握”等表述。

statement_text 必须是 Proposal practice_focus 中用户确认的原文。user_note 只表示用户补充，不被 Evidence 证明。练习自评只属于 Plan，不能回写 Signal。

## 4. 总体架构

### 4.1 组件关系

~~~text
application.interview_review owner
  ├─ current InterviewNote / Review Proposal V2
  ├─ ReadinessCandidateProjector（只读）
  ├─ save_review_readiness_signal action issuer
  └─ materials.story canonical opener

materials.story owner
  ├─ Story Proposal generation（既有 Provider boundary）
  └─ confirm_interview_story action issuer

ProductActionCatalogV1（Provider invisible）
  ├─ save_review_readiness_signal
  └─ confirm_interview_story

ProductActionCoordinator
  ├─ ProductActionProposalRepository
  ├─ WriteOperationRepository
  ├─ Session-bound ReadinessSignal executor
  └─ Session-bound Story confirmation executor

ProductActionCompensationCatalogV1
  ├─ undo:save_review_readiness_signal
  └─ undo:confirm_interview_story

Confirmed Signal Version
  ├─ interview.free_practice owner
  └─ application.interview_prepare owner
       └─ PreparationReadinessSelectionLoader

Future-only sealed type asset
  └─ ConfirmedReadinessContributorPort（未注册；生产不可达）
~~~

### 4.2 Product Action 不伪装成 Agent Tool

Product Action 是界面内的确定性产品确认，不是模型 ToolCall。固定：

~~~text
conversation_id = NULL
agent_run_id = NULL
tool_call_id = server-issued action_call_id
adapter_kind = product_action
~~~

它不创建或修改：

- Conversation；
- Agent Pending Action；
- ChatMessage / ToolMessage；
- AgentRun / Segment；
- Journal tool.proposed / tool.started / tool.completed / tool.failed；
- Provider Surface；
- Agent Dispatcher 或 Legacy routing。

Ledger 是 Product Action 的持久审计和回放真值。没有 AgentRun 时不得为了“看起来统一”伪造 Journal 因果链。

### 4.3 两类 HITL 互不共享 owner

- Signal 的 confirmation owner 是 application.interview_review；
- Story 的 confirmation owner 是 materials.story；
- Haru/Pilot 只打开 canonical owner，不建立第二个确认 UI；
- 两个动作不能合并为“一次确认同时保存”；
- 一个动作失败、拒绝或撤销不改变另一个动作。

## 5. 来源版本与资格

### 5.1 InterviewNote revision

interview_notes 增加：

~~~text
content_revision INTEGER NOT NULL DEFAULT 1
updated_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
~~~

规则：

- 新 Note 从 revision 1 开始；
- 任何会改变 company/position/round/date/questions/self_reflection/difficulty_points/mood/application_id/application_event_id 的生产写路径，使用同一 SQL helper 原子执行 content_revision = content_revision + 1，并显式更新 updated_at；
- Proposal 生成、只读、Story/Knowledge capture 不增加 revision；
- API additive 返回 content_revision/updated_at；旧客户端无需提交 revision；
- Signal action 和 Proposal V2 生成同时绑定 revision 与 canonical content fingerprint；
- NotesRepository、REST、Agent add/update/delete、bulk/scoped update 均进入同一机械门禁，禁止遗漏 revision。

本期不把 Note 的旧 last-write-wins API 全面升级为 CAS；revision 只为新下游事实提供来源身份。

### 5.2 InterviewReviewProposal V1/V2

interview_review_proposals 增加：

~~~text
proposal_schema_version INTEGER NOT NULL DEFAULT 1
source_note_revision    INTEGER NULL
~~~

迁移规则：

- 历史行保持 version=1/source_note_revision=NULL；
- 不能把迁移回填的 Note revision=1 反向当成历史 Proposal 的生成 revision；
- 迁移后新 Proposal 固定写 version=2 和生成时 Note revision；
- Provider-visible input snapshot 与 Proposal JSON shape 保持原样；内部 revision 列不塞入 Prompt；
- Proposal 生成前冻结 revision，Provider 返回后在 ready 回写事务内同时复核 revision、source_fingerprint 和 source resource；
- Signal 资格只接受 V2；历史 V1 继续展示，但必须重新生成后才能保存为准备重点。

candidate_fingerprint 的 canonical envelope 固定覆盖：

~~~text
proposal_schema_version
source_note_revision
source_fingerprint
proposal_hash
focus_id
focus_text
ordered evidence path + excerpt hash
~~~

### 5.3 Signal 候选资格

一个候选必须同时满足：

1. Application 可见且未软删除；
2. Note 可见并绑定 exact Application/Event；
3. Event 属于同 Application，event_type=interview；
4. EventLifecycleV1 为 completed，只按 status，不按日期推断；
5. Proposal 为 V2，note_id/event_id 与来源一致；
6. Proposal source_fingerprint、proposal_hash、source_note_revision 与当前重算一致；
7. focus 来自 practice_focuses，具有唯一 ID、非空文本和 1–5 条 Evidence；
8. Evidence source=interview_note，path 只允许 /questions、/self_reflection、/difficulty_points、/mood；
9. excerpt 仍逐字存在于当前字段；
10. 文本、数组和 canonical envelope 全部满足硬上限。

服务端与前端共用只读 EventLifecycle fixture：

~~~text
todo / pending / scheduled          → scheduled
in_progress                         → in_progress
done / completed                    → completed
cancelled / deleted / soft_deleted  → cancelled
其他                                → unknown
~~~

以下不能成为 Signal：

- summary、clarifications、next_questions；
- safe-empty Proposal 或无 Evidence item；
- Application 级、未绑定 Event 的 Note；
- V1/source changed/source missing Proposal；
- scheduled、in_progress、cancelled、unknown Event；
- 客户端上传的 statement、excerpt、Proposal JSON；
- has_review_proposal、时间、数组位置或自然语言猜出的 focus。

### 5.4 候选只读投影

~~~python
project_readiness_candidates(
    note_id: int,
    proposal_id: int,
    session: Session,
) -> CandidateProjectionV1
~~~

封闭状态：

~~~text
ready
already_confirmed
legacy_requires_regeneration
source_changed
source_missing
not_eligible
unavailable
~~~

ready 最多 8 项，按 Proposal 原始顺序。投影 Provider/Tool/Product Action/Ledger/业务写入均为 0，不返回完整 Note、完整 Proposal 或 Operation identity。

### 5.5 结构上限

- focus ID：1–128 UTF-8 bytes，禁止控制字符；
- focus text：1–1,000 Unicode code points，最多 4 KiB UTF-8；
- Evidence：1–5 条；
- 单条 excerpt：1–2,000 code points，最多 8 KiB；
- 单个 Signal 的 excerpt 总计最多 16 KiB；
- user_note：0–500 code points，最多 2 KiB；
- candidate canonical envelope：最多 32 KiB；
- Product Action route payload：最多 16 KiB；
- 所有 JSON 拒绝重复键、NaN/Infinity、额外字段和非 object 顶层，不做 Unicode 规范化。

## 6. 数据模型与 0029 Migration

### 6.1 product_action_proposals

新增私有路由材料：

~~~text
operation_id                    UUID PRIMARY KEY FK write_operations ON DELETE RESTRICT
action_call_id                  UUID NOT NULL UNIQUE
action_name                     confirm_interview_story | save_review_readiness_signal
request_origin                  current | historical_story_bridge
schema_version                  EXACT_INT NOT NULL DEFAULT 1  # SQLite typeless/BLOB-affinity storage
source_kind                     story_proposal | review_focus
source_id                       EXACT_INT NOT NULL             # SQLite typeless/BLOB-affinity storage
source_revision                 EXACT_INT NOT NULL             # SQLite typeless/BLOB-affinity storage
route_payload_json              TEXT NULL, max 16 KiB
route_payload_fingerprint       hmac-sha256:<64 hex>
route_binding_fingerprint       hmac-sha256:<64 hex>
request_idempotency_fingerprint hmac-sha256:<64 hex> UNIQUE
semantic_claim_fingerprint      hmac-sha256:<64 hex> NULL
historical_request_token_fingerprint hmac-sha256:<64 hex> NULL
created_at                      DATETIME NOT NULL
terminalized_at                 DATETIME NULL
~~~

route_payload_json 只保存可信引用、revision、hash、focus ID 和有界 user_note。禁止保存：

- 完整 InterviewNote；
- 完整 Review Proposal；
- Story 正文或完整 Evidence excerpt；
- confirmation token；
- Provider response；
- ORM/Session/issuer/proof 对象。

Story 的可编辑 content/evidence 只存在于客户端 decision request 和锁内 executor；Product Action proposal 只绑定 Story Attempt generation_revision、product_action_generation、proposal hash 与 target CAS。若请求在 terminal 前结果未知，客户端必须使用原 decision payload 对账；不同 payload 与 terminal request fingerprint 冲突。

terminal/reject/确定性 failed 同一事务将 route_payload_json 置 NULL、设置 terminalized_at，保留 HMAC 关联标识。terminal replay 不需要原始 route payload。

route_payload_json 使用封闭、无额外字段的 union。Signal 分支只能包含：

~~~text
application_id
event_id
note_id
proposal_id
proposal_schema_version = 2
focus_id
expected_note_revision
expected_source_fingerprint
expected_proposal_hash
expected_candidate_fingerprint
user_note
domain_idempotency_key
~~~

Story 分支只能包含：

~~~text
attempt_id
generation_revision
proposal_hash
source_fingerprint
target_story_id
expected_current_version_id
expected_story_revision
product_action_generation
~~~

两类 route payload 均不得保存 readiness statement、Evidence excerpt、Story content 或 Story evidence_links；这些内容只来自锁内重新加载的可信来源，或当前 decision 的受限可编辑字段。

数据库机械约束固定为：

- operation_id/action_call_id 为规范 UUID；schema_version 必须是 integer 1，source_id/source_revision 必须是正整数。SQLite 列使用无 affinity 的 exact-integer storage 并配合 `typeof(...)='integer'`，机械拒绝可被普通 `INTEGER` affinity 无损吞掉的 real/text 输入；所有 Repository/HTTP route 在绑定 SQL 前还必须以 `type(value) is int` 拒绝 bool。SQLite 驱动会把 Python/JSON bool 与整数 1 绑定为完全相同的 wire value，raw SQL 层无法再区分，因此“直接 raw SQL 传 bool”是明确的 SQLite 边界，而不是 DB 已保证的属性；Product Action 的 duplicate-key-aware raw decoder 负责在归一化前封闭该边界；
- active route_payload_json 必须 `json_valid=1`、顶层 object，并以 `length(CAST(route_payload_json AS BLOB)) <= 16384` 约束 UTF-8 bytes；三个恒定必填 fingerprint、conditional semantic/historical fingerprint 均使用封闭的 `hmac-sha256:<64 lowercase hex>` 格式；
- action/source 映射只能是 `confirm_interview_story ↔ story_proposal` 或 `save_review_readiness_signal ↔ review_focus`；request_origin=historical_story_bridge 只允许 confirm_interview_story，其他必须 current；
- historical_request_token_fingerprint 的 present iff request_origin=historical_story_bridge；current 行必须 NULL；semantic_claim_fingerprint 对 save_review_readiness_signal 必填、Story 必须 NULL；
- active 真值表为 `route_payload_json IS NOT NULL AND terminalized_at IS NULL`；terminal 真值表为 `route_payload_json IS NULL AND terminalized_at IS NOT NULL`；其余组合禁止；
- INSERT trigger 必须验证父 WriteOperation 是同 operation_id/action_call_id/action_name 的 product_action primary proposed row；SQLite immediate trigger 只能机械保证 route→parent，不能在先插入 parent 时延迟验证随后才插入的 route；
- route row 的 operation/action/source/schema/fingerprint/created_at 身份创建后不可变；禁止 DELETE；
- partial unique `semantic_claim_fingerprint WHERE action_name='save_review_readiness_signal' AND terminalized_at IS NULL` 保证同一 Proposal/focus 同时最多一个 active Product Action；terminal row 保留 claim HMAC 但自动释放 active unique；
- Product Action parent Operation 从 proposed 进入 rejected/committed/failed 时，`AFTER UPDATE OF status` trigger 必须先验证存在 exact active route，再在同一 SQL statement 内清空 route_payload_json 并写 terminalized_at；route 缺失、已 terminal 或身份不匹配使整个 parent UPDATE abort；
- route 的 active→terminal UPDATE 只有在父 Operation 已处于同身份 terminal 时才允许，因此直接在 proposed parent 下清空 route 必须被 trigger 拒绝；生产 Repository 不暴露 route terminalize 方法，唯一 owner 是 parent terminal transition；
- parent terminal 与 route clear 随同一 statement/事务原子提交或回滚，既不能提交 terminal parent+active route，也不能提交 proposed parent+terminal route；
- Coordinator 的封闭 Session-bound publication UoW 是 proposed parent+route bundle 的唯一生产创建 owner：同一 `BEGIN IMMEDIATE` 依次插入 parent 与 route，然后在 COMMIT 前反向重读 exact pair；任一步或反向校验失败即 rollback。Repository 不暴露裸 parent-only Product Action insert，AST gate 禁止其他生产模块直写。直接 raw SQL 仍可提交 parent-only row，这是 SQLite 没有 deferred commit trigger 的明确边界；任何 fresh read/reconciliation/decision 遇到 parent-only、route-only 或身份不匹配都 fail-closed 为 integrity error，绝不修补或执行。数据库 trigger 是第二道机械防线，不能替代锁内校验。

historical_request_token_fingerprint 只允许 pre-0029 historical-ready Story bridge 写入；新 Story 和 Signal 必须为 NULL。它绑定旧客户端 token 的 HMAC，不是 confirmation capability，也不能被返回给客户端或用来授权 executor。

### 6.2 interview_readiness_signals

~~~text
id                         INTEGER PRIMARY KEY
application_id             INTEGER NOT NULL FK applications ON DELETE CASCADE
source_event_id            INTEGER NULL FK application_events ON DELETE SET NULL
source_note_id             INTEGER NULL FK interview_notes ON DELETE SET NULL
source_proposal_id         INTEGER NULL FK interview_review_proposals ON DELETE SET NULL
focus_id                   VARCHAR NOT NULL
current_version_id         INTEGER NULL
revision                   INTEGER NOT NULL DEFAULT 1
created_at                 DATETIME NOT NULL
updated_at                 DATETIME NOT NULL
UNIQUE(source_proposal_id, focus_id) WHERE source_proposal_id IS NOT NULL
~~~

规则：

- application_id 创建后不可变，防止来源删除把 scope 扩大；
- source IDs 创建后不得改成其他实体，也不得从 NULL 恢复为非 NULL；SQLite trigger 只允许 identity 不变或单向 `non-null → NULL`。SQLite 无法可靠区分 FK `ON DELETE SET NULL` 触发的内部 UPDATE 与直接 SQL UPDATE，因此数据库不虚假声明能区分来源；
- 生产 Repository 不暴露 source-ID UPDATE，AST/ownership gate 只允许父 Note/Event/Proposal 删除触发该转换；source-integrity audit 将任何 NULL 一律视为 missing，不据此扩大 scope。直接绕过 Repository 的原始 SQL 可制造相同 missing 状态，这是明确的 SQLite 信任边界，不会恢复可消费资格；
- current_version_id 只能指向本 Signal 的 Version，由 trigger 验证；
- 事务外不得返回 current_version_id=NULL 的新聚合；
- Note/Event/Proposal 删除后 Signal 保留历史但 source_status=missing，不能进入新的 Practice、Preparation 或 Context；
- Application 软删除立即隐藏；Application hard delete才级联清理整个聚合。

该删除语义与现有 Proposal 的 SET NULL/冻结历史一致，也保证 committed Ledger 和 required Undo 不因普通 Note/Event 删除突然失去审计目标。显式 privacy erase 需另行设计，本期不扩大既有“删除 Note 即清除所有派生 AI 数据”的承诺。

### 6.3 interview_readiness_signal_versions

~~~text
id                         INTEGER PRIMARY KEY
signal_id                  INTEGER NOT NULL FK signals ON DELETE CASCADE
version_number             INTEGER NOT NULL
parent_version_id          INTEGER NULL
disposition                active | retracted
schema_version             readiness-signal-v1
statement_text             TEXT NOT NULL
user_note                  TEXT NOT NULL DEFAULT ''
source_note_revision       INTEGER NOT NULL
source_note_fingerprint    sha256:<64 hex>
source_proposal_hash       sha256:<64 hex>
candidate_fingerprint      sha256:<64 hex>
domain_idempotency_key     UUID NOT NULL UNIQUE
write_operation_id         UUID NOT NULL UNIQUE FK write_operations ON DELETE RESTRICT
created_at                 DATETIME NOT NULL
UNIQUE(signal_id, version_number)
UNIQUE(id, signal_id)
FOREIGN KEY(parent_version_id, signal_id)
  REFERENCES interview_readiness_signal_versions(id, signal_id)
  ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED
~~~

V1：

~~~text
首次保存  → version 1 / active / parent NULL
Undo      → version 2 / retracted / parent version 1
~~~

Version 禁止 UPDATE。composite self-FK `(parent_version_id,signal_id)→(id,signal_id)` 机械保证 parent/child 属于同一 Signal，跨聚合 parent 即使 ID 存在也拒绝。它使用 deferred NO ACTION，是为了让 Application hard delete→Signal CASCADE 在同一事务删除完整 active→retracted Version 链后于 COMMIT 通过，同时任何留下 child 的单独 parent Version delete 在 COMMIT 失败。`BEFORE DELETE ON interview_readiness_signal_versions` trigger 在对应 Signal 行仍存在时拒绝删除；生产 Repository 不暴露 Version delete，AST gate 只允许 Signal/Application owner 触发 aggregate cascade。migration 必须实测父 Application 删除时 Signal 行在 Version cascade trigger 中已不可见；若目标 SQLite 版本不满足该顺序，实施前必须改用经过书面复审的 aggregate-delete owner协议，不能退回 RESTRICT 或无保护 CASCADE。statement 不可编辑；来源变化后必须重新生成 Proposal，再保存新的 Signal。user_note 是用户补充，不自动进入 Knowledge。

### 6.4 interview_readiness_signal_evidence

~~~text
id                         INTEGER PRIMARY KEY
signal_version_id          INTEGER NOT NULL FK versions ON DELETE CASCADE
ordinal                    INTEGER NOT NULL
source_path                VARCHAR NOT NULL
excerpt                    TEXT NOT NULL
excerpt_sha256             sha256:<64 hex>
source_field_sha256        sha256:<64 hex>
UNIQUE(signal_version_id, ordinal)
~~~

Evidence 仅保存用户确认时看到的受限逐字片段，不保存完整 Note。source_field_sha256 用于发现 excerpt 仍存在但字段整体已变化。每个 active Version 必须有 1–5 行 Evidence，ordinal 固定为从 0 开始连续的 JSON/SQLite integer；插入缺 0、跳号、重复、负数或超过 4 均由 Session-bound executor 拒绝并 rollback。Version+Evidence aggregate 完整后才更新 Signal current pointer。

Adaptive Practice 使用独立 `practice_source_fingerprint_v1`，它不是任一单列 fingerprint。算法固定为 UTF-8 canonical JSON SHA-256（键排序、`(',', ':')`、禁止 bool/非有限数/重复键、不做 Unicode 规范化）：

~~~text
sha256({
  contract:'practice_source_fingerprint_v1',
  signal_id, signal_revision,
  version_id, version_number, disposition,
  statement_sha256, user_note_sha256,
  source_note_revision, source_note_fingerprint,
  source_proposal_hash, candidate_fingerprint,
  evidence:[{ordinal,source_path,excerpt_sha256,source_field_sha256}, ...]
})
~~~

evidence 严格按 ordinal 升序，所有 1–5 行都进入 fingerprint；原始 statement/user_note/excerpt 不进入 envelope，只进入各自 SHA-256。该 fingerprint 由同一只读 aggregate loader 计算并通过 Readiness advisory/detail/practice-focus API 返回，客户端只能原样回传，不能自行拼装。

### 6.5 Adaptive Practice V2 字段

adaptive_practice_plans 增加：

~~~text
origin_contract                 legacy_review_focus_v1 | confirmed_readiness_signal_v1
readiness_signal_version_id     INTEGER NULL FK signal_versions ON DELETE SET NULL
target_application_event_id     INTEGER NULL FK application_events ON DELETE SET NULL
target_fingerprint              VARCHAR NULL
~~~

现有 application_event_id 保留为 legacy/source Event 身份，不得误当 target Event。

迁移：

- 0029 必须重建 adaptive_practice_plans，删除现有普通 `UNIQUE(interview_review_proposal_id, focus_id)`；只增加列而保留旧 UNIQUE 不合格；
- 旧行回填 legacy_review_focus_v1，两个新 FK 与 target_fingerprint 均为 NULL；旧 application_event_id/interview_note_id/interview_review_proposal_id/focus_id/source snapshot 字段保持原值和 NOT NULL；
- 新 V2 行必须是 confirmed_readiness_signal_v1，并以两个新 FK 均非空、现有 source_fingerprint=`practice_source_fingerprint_v1`、target_fingerprint=`practice_target_fingerprint_v1` 的 live 形状创建；两个 fingerprint 都是 `sha256:<64 lowercase hex>` 且创建后不可变。为保持历史 renderer，旧 source identity/snapshot 列从确认时的 Signal Version复制为冻结非空值，但只用于显示/审计，不能替代两项 V2 fingerprint；
- origin truth table 由 CHECK 固定：legacy 两个新 FK 与 target_fingerprint 必须 NULL；V2 target_fingerprint 永远非 NULL，且持久状态允许 `live=(source,target 均非 NULL)`、`source_missing=(source NULL,target 非 NULL)`、`target_missing=(source 非 NULL,target NULL)`、`both_missing=(两者 NULL)`；未知 origin 拒绝。FK 后续 SET NULL 只能降低 locator availability，冻结的 source/target fingerprints 不清空、不改写；
- INSERT trigger/Repository 只允许新 V2 以 live 形状和两项 exact fingerprint 创建；后续 source_fingerprint/target_fingerprint/origin 永不可变。non-null→NULL 是 FK `ON DELETE SET NULL` 的单调历史降级，NULL→non-null 或换成其他 ID 均拒绝。SQLite 不能区分 FK 内部 UPDATE 与直接 raw SQL，生产 AST/owner 禁止直接更新；任一 NULL 只会降低 locator 可用性，不能改变冻结 fingerprint；
- legacy partial unique 为 `(interview_review_proposal_id, focus_id) WHERE origin_contract='legacy_review_focus_v1'`；
- V2 partial unique 固定为 (readiness_signal_version_id, target_application_event_id)；
- 同一 Signal 可以针对不同未来轮次分别练习；
- 已开始 V2 Plan 保留冻结来源，即使 source 后续 missing/retracted 仍可查看和完成；
- V1 行绝不回填、推断或升级为 confirmed Signal。

V2 FK 后续因删除变 NULL 时，origin_contract、source_fingerprint、target_fingerprint 和旧冻结 source snapshot 保持不变，Plan 进入历史 `source_missing`/`target_missing`/`both_missing` 分支；已经 in_progress/completed 的 Plan 仍可查看/完成，禁止把旧 snapshot 重新解释为可创建新 Plan 的 current 来源。migration golden 必须覆盖旧 UNIQUE 移除、legacy target_fingerprint NULL、V2 target_fingerprint required/格式/immutable、同 Signal 不同 target Event、同 pair 冲突、source-only/target-only/both delete、直接 partial INSERT 拒绝及 in_progress/completed 删除后的真值表。

### 6.6 Story Attempt 增量

interview_story_proposal_attempts 增加：

~~~text
product_action_operation_id UUID NULL UNIQUE FK write_operations ON DELETE RESTRICT
product_action_generation   INTEGER NOT NULL DEFAULT 0
~~~

新 Proposal 从 generating/provider_unknown 转为 ready 时，在同一短事务创建：

- server-issued action_call_id；
- confirm_interview_story WriteOperation proposed；
- product_action_proposals row；
- attempt.product_action_operation_id/product_action_generation=1。

Operation ID、action_call_id、原始 server confirmation token、token fingerprint、route/request HMAC 和 undo seed 所需材料必须在进入事务前准备；事务内不得做 keyring/file/network I/O。WriteOperation proposed INSERT 使用预先计算的 confirmation_token_fingerprint，commit 后只返回已经生成的 token，不能在 commit 后重新派生另一份身份。

历史 confirmed attempt 不回填 Ledger，只保留 read/replay compatibility，executor=0。历史 ready attempt 在第一次显式 confirm POST 时以 exact attempt CAS 创建 Product Action proposal，再进入新 Coordinator；GET 不偷偷写数据库。

Story Action 被 reject 后 Attempt 仍为 ready，当前 Operation 保留 terminal history。用户再次选择保存时，必须先调用 source-bound proposal 入口 `POST /api/interview-story-proposals/{attempt_id}/product-actions`，exact body 只能是 `{expected_generation_revision,expected_product_action_generation}`。该入口 Provider=0，不接受 confirmation token、Story content 或客户端 action name；它在 `BEGIN IMMEDIATE` 内重读 exact Attempt/current Operation，只在 current Operation 已 rejected 且 Attempt 仍 ready 时，用 CAS 新建下一代 Product Action、递增 product_action_generation、更新 current pointer并返回全新的 Operation/action_call/token。相同 expected generation 的并发或响应丢失重试收敛到同一个新 generation；pending/unknown Operation 必须恢复当前身份，不能并行创建下一代。`product_action_story_write_conflict` 表示 target Story CAS 已漂移，旧 route 已失效，禁止创建下一代；UI 只能通过既有 Story Proposal 生成入口创建新的 Attempt/generation_revision/proposal hash并重新确认。旧 terminal token 永远不能授权或换取新 generation。committed 后 Attempt 已 confirmed，禁止再签发。

#### 6.6.1 Story ready publication 的 commit-unknown

Story Provider result 在进入 ready 事务前已完成验证并冻结；同一请求固定 attempt ID、generation_revision、product_action_generation、proposal hash、Operation/action_call ID、server token 及所有 fingerprint。ready Attempt、WriteOperation proposed、ProductActionProposal 与 `(seq=1,state=proposed)` transition 必须作为一个事务整体出现，禁止只补写其中一部分。

COMMIT 抛错后使用 fresh Session 一次性读取三者并分类：

~~~text
exact_ready_bundle
pre_ready_and_bundle_absent
partial_or_mismatched
unreadable
~~~

- exact_ready_bundle 使用封闭交叉矩阵并要求每个 parent status 的 §6.7.1 exact transition prefix：`proposed + active route ↔ ready`，或 `invalidated(source_changed)` 且 Operation 从未 approved/claimed；`rejected + terminal route ↔ ready` 或同一 invalidated；`committed + terminal route ↔ confirmed` 且 confirmed_story_id/version_id 与 terminal result/undo 完全一致；`failed(product_action_story_write_conflict) + terminal route ↔ ready`。其他 failed code、status、transition 或不一致字段一律 integrity error；
- pre_ready_and_bundle_absent：Attempt 仍为 generating/provider_unknown 且 Operation/route 均 absent。只有当前请求仍持有那份已验证 Frozen Story Provider result 时，才可用完全相同身份重放整个 ready+Operation+route 事务一次，Provider=0；
- 进程重启或 Frozen result 已丢失：保持既有 provider_unknown 恢复语义，不得只创建 Product Action，不得隐式再次调用 Provider；
- partial_or_mismatched：任一单独存在、transition prefix 缺失/额外、身份不一致或 attempt 状态与 bundle 矛盾均 fail-closed 为 integrity error，不修补、不执行；
- unreadable：返回 operation_result_unknown，不生成新 product_action_generation/token/ID。

响应丢失后的 GET 必须按矩阵恢复：exact active proposed 返回同一 Operation 和同一 server token；若并发 decision 已使它 rejected/committed/declared-failed，则只返回安全 terminal status/result 且 token=absent；partial/mismatched fail-closed，unreadable 返回 unknown。所有分支 Provider=0。两连接 commit-unknown、进程重启、三类 partial corruption、publication 响应丢失后并发 approve/reject/declared-failed、token absence 和迟到 Provider result 均进入机械测试。

#### 6.6.2 Story next-generation proposal 的 commit-unknown

§6.6.1 只适用于 `generating/provider_unknown → ready + generation 1` 的首次 publication。`ready + terminal N → proposed N+1` 使用独立状态机。Coordinator 在进入事务前根据 exact expected N 冻结 deterministic N+1 Operation/action_call、全新 server token、fingerprint 和 route；COMMIT 抛错后 fresh Session 一次性读取 Attempt pointer、N terminal parent/route/transition prefix、N+1 parent/route/transition prefix；若 current pointer 已大于 N+1，还必须直接读取 current pointer 指向的 parent+route/transition prefix bundle（无需遍历中间历史）并分类：

~~~text
old_terminal_pointer_n_and_n_plus_1_absent
exact_n_plus_1_proposed
exact_n_plus_1_terminal
exact_historical_n_plus_1_terminal_with_later_pointer
partial_or_mismatched
unreadable
~~~

- `old_terminal_pointer_n_and_n_plus_1_absent`：Attempt 仍 ready、current pointer/product_action_generation exact N，N 必须是 rejected terminal，N+1 parent/route 均 absent。只有原请求仍持有预先冻结的 exact N+1 identities/token 时，才可重放 proposal publication 事务一次；Provider=0、executor=0。N=failed（包括 product_action_story_write_conflict）不属于 absent/replay 条件；
- `exact_n_plus_1_proposed`：Attempt 仍 ready、pointer/generation exact N+1，N+1 parent proposed + active route + exact `[(1,proposed)]` 完全匹配；按保存的 key profile重建并返回同一 N+1 token，不能再 INSERT；
- `exact_n_plus_1_terminal`：pointer 仍为 N+1，且 parent/terminal route/Attempt ready-or-confirmed与§6.7.1 terminal prefix符合交叉矩阵；只读返回 N+1 terminal status/result，token=absent、Provider/executor=0；
- `exact_historical_n_plus_1_terminal_with_later_pointer`：N+1 已 exact terminal且transition prefix正确，current pointer 指向的更高 generation bundle也必须 exact：若 current parent proposed，则 active route+`[(1,proposed)]`与Attempt ready必须匹配；若 current parent terminal，则 terminal route/transition prefix/Attempt ready-or-confirmed必须通过同一交叉矩阵。只有这样才能证明另一个显式 proposal 已合法推进；原 expected N 请求只读 replay N+1 terminal，不能返回当前更高 generation token，也不能覆盖 pointer；
- `partial_or_mismatched`：pointer 已变化但 N+1 identity 不匹配、parent/route/transition 任一单边或prefix错误、N 非允许 terminal、N+1 route/parent/Attempt 矛盾、current pointer 的直接 bundle absent/单边/identity mismatch，或任何 fingerprint 不一致，统一 integrity error；不修补、不签发新 token；
- `unreadable`：返回 operation_result_unknown，不创建 N+1/N+2。

next-generation proposal endpoint 使用独立 `story_product_action_proposal_response_v1`，不得复用 `/confirm` 的 Story write codec。exact body 是以下封闭 union，禁止额外字段：

~~~text
direct N+1 proposal commit:
  HTTP 201
  {schema_version:1, contract:'story_product_action_proposal_response_v1',
   operation_id, action_call_id, product_action_generation:N+1,
   status:'proposed', proposal_created:true, confirmation_token}

fresh exact N+1 proposed:
  HTTP 200
  {schema_version:1, contract:'story_product_action_proposal_response_v1',
   operation_id, action_call_id, product_action_generation:N+1,
   status:'proposed', proposal_created:false, confirmation_token}

exact terminal or historical N+1 terminal:
  HTTP 200
  {schema_version:1, contract:'story_product_action_proposal_response_v1',
   operation_id, action_call_id, product_action_generation:N+1,
   status:'rejected'|'failed'|'committed', proposal_created:false,
   terminal_result:<safe action-specific terminal projection>}
~~~

`proposal_created` 只表示 Product Action proposal row 是否由当前正常返回的 COMMIT 新建，绝不表示 Story/Version 已创建；terminal union 禁止 confirmation_token，terminal_result 精确复用 §8.6 已验证的 Product Action safe result projection（rejected decision、declared failed code 或 committed Story IDs/outcome），不含正文/Evidence。partial/unreadable 使用既有安全 error codec，不伪装成该 success union。canonical JSON golden 必须覆盖 direct 201、fresh proposed 200、rejected/failed/committed terminal、later-pointer historical terminal与 token presence/absence。两连接 commit throw、pointer/parent/route 三类 partial、proposal提交后并发 approve/reject/definite-failed、随后 N+2、restart key recovery 和 late request 都必须覆盖。

### 6.7 WriteOperation Ledger shape

0029 重建 write_operations，并精确增加：

~~~text
adapter_kind += product_action

primary product actions:
  confirm_interview_story
  save_review_readiness_signal

product action compensations:
  undo:confirm_interview_story
  undo:save_review_readiness_signal

result_contract:
  product_action_json_v1
~~~

Agent Tool Metadata 保持：

~~~text
25 Typed
3 Legacy deterministic
4 Agent Compensation
~~~

另建：

~~~text
ProductActionCatalogV1: 2
ProductActionCompensationCatalogV1: 2
Runtime Operation seal: 25 / 3 / 4 + 2 / 2
~~~

Product Action primary 约束：

- conversation_id IS NULL；
- agent_run_id IS NULL；
- tool_call_id=action_call_id 且全局 partial unique；
- authorization_scope_fingerprint 非空；
- proposed 时 delivery_status=pending、generation=0，沿用现有 proposed shape；
- 该 pending 仅表示 Operation 尚未 terminal，不是 Chat delivery ownership；所有 delivery lease/recovery/takeover 查询必须显式排除 adapter_kind=product_action；
- rejected/committed/failed terminal 时：

~~~text
delivery_status = not_applicable
delivery_generation = 0
delivery_outcome = none
delivery_message_count = 0
delivery owner / lease / manifest / next operation = NULL
delivered_at = rejected_at | committed_at | failed_at
~~~

- committed/failed result_contract=product_action_json_v1；
- rejected 继续使用 rejection_json_v1；
- 两个成功 primary 的 UndoPolicy 均为 REQUIRED。

历史 row 的 canonical terminal bytes、digest、HMAC、delivery 字段不得重算或改写。

#### 6.7.1 WriteOperation transition 前缀

Product Action primary 与 compensation 完整复用 Phase 3 的 immutable `write_operation_transitions`，不能只更新 parent row。对每个 Operation，parent.status 与 transition 集合必须精确满足：

~~~text
proposed  → [(1, proposed)]
rejected  → [(1, proposed), (2, rejected)]          # primary only
committed → [(1, proposed), (2, approved), (3, claimed), (4, committed)]
failed    → [(1, proposed), (2, approved), (3, claimed), (4, failed)]
~~~

禁止缺 seq、额外 seq、重复 state、跳号或同 status 的其他前缀。Product Action proposal publication（Signal、Story首次ready、Story N→N+1）必须在创建 parent/route 的同一事务追加 seq1；Story 首次 publication 因而是 Attempt+parent+route+seq1 四对象 bundle。reject 的 parent terminal+route clear+seq2 同事务；approve/modify 的 seq2 approved、seq3 claimed、领域写、parent terminal+route clear与seq4同一事务。compensation proposal 与 seq1 同事务，execution 的 seq2/3、领域 undo、parent terminal与seq4同一事务。任何 rollback 都不能留下 transition-only 或 parent-only 的生产结果。

现有 transition INSERT/immutable/delete triggers 原样保留，只验证被插入行的局部合法性；它们不能反向保证 parent 一定拥有完整 prefix。因此所有 ProductAction Repository load、decision、owner recovery、Undo proposal/execution 和 fresh reconciliation 都必须按 parent status 查询完整 ordered transition list并验证上述 exact prefix。缺失/额外/错误 transition 一律 integrity error，Provider/executor=0，不修补。terminal replay 除校验 terminal digest/request identity外也必须校验 prefix；unreadable 返回 operation_result_unknown。

0029 不回填、不重排、不更新时间戳、不改写任何历史 transition bytes。Agent/Legacy/既有 compensation transition golden 必须逐行不变；新 2/2 只增加合法新行。

### 6.8 Migration 机械保证

版本固定：

~~~text
0029_review_to_readiness_feedback
~~~

实施前检查编号未被占用。覆盖：

- 空库、0028 库、真实 25/3/4 terminal row 库；
- Note revision 回填 1，不修改正文；
- Proposal 历史行回填 V1/NULL；
- Story 历史 confirmed/ready/generating；
- Adaptive legacy 行读取/完成；
- write_operations column-for-column rebuild；
- indexes/triggers/partial unique/foreign_key_check/integrity_check；
- 含 active→retracted composite self-FK 链的 Application hard delete 整聚合 cascade 成功；跨 Signal parent INSERT拒绝；Signal 存在时单独删除 parent/leaf Version 均被拒绝；COMMIT 后 foreign_key_check=0；
- migration 中断回滚与重复启动；
- 历史 terminal canonical bytes/digest 与每条 write_operation_transition 的 id/operation/seq/state/created_at 全量一致；
- 合法 product action/product compensation 及其 exact transition prefix 可插入；
- 未知 adapter/action/compensation 仍被 DB CHECK 拒绝。

## 7. Product Action Catalog、Issuer 与 API

### 7.1 封闭 Catalog

~~~python
ProductActionSpecV1
- action_name
- exact route source
- capability declaration
- binding policy
- route payload contract
- decision payload decoder
- safe presentation
- result codec
- write contract
- compensation binding
~~~

实例：

~~~text
save_review_readiness_signal
  source = review_readiness_focus_owner
  capability = application.interview_readiness_feedback.write
  undo = required

confirm_interview_story
  source = interview_story_owner
  capability = stories.write
  undo = required
~~~

ProductActionCatalog 不导入 ToolCatalog 或 LegacyDeterministicCatalog。Agent Dispatcher 查询不到 Product Action 名称，Provider 返回同名调用仍为 unknown_tool、executor=0。

### 7.2 Source-bound issuer

Composition 创建两个可复用 issuer capability：

~~~text
ReviewReadinessActionIssuer
InterviewStoryActionIssuer
~~~

每次 proposal 创建：

1. 建立 request-local lease；
2. 由固定 route 签发稳定 operation_id/action_call_id；
3. 在 BEGIN 前读取固定 Ledger key profile，生成原始 server confirmation token、fingerprint 和全部 HMAC；
4. 产生不可由调用方构造的 route proof；
5. 事务内写带该 token fingerprint 的 WriteOperation proposed + ProductActionProposal；
6. commit 后只返回步骤 3 已生成的 token，不重新读取 key 或重新派生；
7. lease 在完成、异常、取消、owner 切换时 revoke。

客户端不能提交 action_name 来决定路由。generic decision API 必须先按 operation_id 读取服务器 ProductActionProposal，再由 Catalog exact resolve。

#### 7.2.1 Proposal identity HMAC

所有 HMAC 使用 WriteOperation.fingerprint_key_id 指向的 Ledger key、UTF-8 canonical JSON（键排序、`(',', ':')`、禁止非有限数/重复键、不做 Unicode 规范化）以及 tagged null；整数只能是 JSON integer，数组顺序由各 envelope 固定。五个核心身份没有隐式同义字段：

派生顺序固定为：validate exact route payload → route_payload_fingerprint → semantic_claim_fingerprint → deterministic operation/action_call ID → authorization_scope_fingerprint → route_binding_fingerprint → request_idempotency_fingerprint → proposal_fingerprint → server token → confirmation_token_fingerprint。

~~~text
route_payload_fingerprint = HMAC(
  b"product-action-route-payload-v1\0",
  {schema_version, catalog_fingerprint, action_name, request_origin, source_kind,
   source_id, source_revision, exact_route_payload}
)

semantic_claim_fingerprint = HMAC(
  b"product-action-semantic-claim-v1\0",
  {action_name:"save_review_readiness_signal", application_id,
   proposal_id, focus_id}
)

route_binding_fingerprint = HMAC(
  b"product-action-route-binding-v1\0",
  {catalog_fingerprint, action_name, request_origin, source_kind, source_id,
   source_revision, canonical_owner, application_scope,
   route_payload_fingerprint, semantic_claim_fingerprint,
   historical_request_token_fingerprint}
)

proposal_fingerprint = HMAC(
  b"product-action-proposal-v1\0",
  {operation_id, action_call_id, catalog_fingerprint, action_name, request_origin,
   route_payload_fingerprint, route_binding_fingerprint,
   authorization_scope_fingerprint, semantic_claim_fingerprint,
   historical_request_token_fingerprint}
)

request_idempotency_fingerprint = HMAC(
  b"product-action-proposal-request-v1\0",
  {schema_version, action_name, request_origin, canonical_request_identity, operation_id, action_call_id,
   route_payload_fingerprint, route_binding_fingerprint, semantic_claim_fingerprint,
   historical_request_token_fingerprint}
)
~~~

这里的 `proposal_fingerprint` 就是父 WriteOperation.proposal_fingerprint；其余四项写入 ProductActionProposal 同名列。可空项统一编码为 `{"state":"absent","value":null}` 或 `{"state":"present","value":<typed value>}`，禁止裸 null/字段省略。application_scope 是 `{kind:'application', id:<JSON integer>}`；Story 没有 Application 时使用 `{kind:'story', id:<JSON integer或tagged null>}`，不得用字符串化数字。historical fingerprint 对非 bridge、semantic claim 对 Story 都必须是 tagged absent。

canonical_request_identity 是 proposal request fingerprint 专用封闭 union：Signal=`{kind:'signal',idempotency_key:<canonical UUID>}`；新 Story=`{kind:'story',attempt_id,generation_revision,product_action_generation,proposal_hash}`；historical Story=`{kind:'historical_story',attempt_id,generation_revision,product_action_generation,proposal_hash,historical_request_token_fingerprint}`。historical token 变化必须改变 request fingerprint，但不能改变 deterministic Operation ID，否则会绕过 same-operation conflict。跨进程 golden 固定两个 action、historical bridge、tagged null、中文 user_note、generation_revision 与 product_action_generation 变化。

confirmation token 使用固定 codec `product-action-confirmation-token-v1`：

~~~text
token = lowercase_hex(HMAC-SHA256(
  ledger_key,
  b"product-action-confirmation-token-v1\0" + canonical_json(envelope)
))
~~~

canonical_json 使用 UTF-8、键排序、`(',', ':')` 分隔、禁止非有限数、拒绝重复键且不做 Unicode 规范化。envelope 封闭绑定：

~~~text
catalog fingerprint
action name
operation_id
action_call_id
proposal fingerprint
route binding fingerprint
historical request token fingerprint（仅 historical-ready Story bridge；其余为 tagged null）
~~~

WriteOperation confirmation_token_fingerprint 继续使用现有 Ledger token-fingerprint 域对这 64 位 token 做 HMAC；数据库不保存 token。跨 action、attempt、runtime bundle、generation_revision、product_action_generation 和 ABA token 均拒绝。

token 派生 profile/version 和 fingerprint_key_id 一起进入 proposed Operation 身份。GET/restart 只能用 Operation 已保存的 fingerprint_key_id 和同一版本算法重建同一个 token；V1 不允许 active key rotation。未来若引入 rotation，必须保留所有仍被 proposed Product Action 引用的旧 key profile。引用 key 缺失时 fail-closed 为不可恢复的完整性错误，不签发新 token、不改变 Operation，也不执行 action。

authorization_scope_fingerprint 使用独立域 product-action-scope-v1：

- Signal 绑定 Catalog fingerprint、action、Application/Event/Note/Proposal/focus、Note revision、source/proposal/candidate fingerprint 和 capability set；
- Story 绑定 Catalog fingerprint、action、Attempt/generation_revision/product_action_generation/proposal hash、source-set fingerprint、target Story/current version/revision 和 capability set；
- 缺失字段以显式 tagged null 编码，整数为 JSON integer，数组按既定顺序；
- HMAC 原始输入不进入日志、Ledger result 或前端。

UUID 生成契约固定为：

~~~text
PRODUCT_ACTION_OPERATION_NAMESPACE =
  2dba24b4-8d35-5c48-8c40-99b9fa5e84f2

PRODUCT_ACTION_CALL_NAMESPACE =
  60f6c6ce-1d72-5e28-9f54-9d3a2b4d41e2

operation_id =
  uuid5(PRODUCT_ACTION_OPERATION_NAMESPACE,
        action_name + ":" + request_origin + ":" + canonical_operation_identity)

action_call_id =
  uuid5(PRODUCT_ACTION_CALL_NAMESPACE, lowercase_canonical_operation_id)
~~~

canonical_operation_identity 与 proposal request identity 分离。Signal 绑定客户端生成并在 unknown/replay 中保留的 UUID idempotency key；新/historical Story 都使用封闭 envelope：

~~~text
{
  "attempt_id": JSON integer,
  "generation_revision": JSON integer,
  "product_action_generation": JSON integer,
  "proposal_hash": "sha256:<64 lowercase hex>"
}
~~~

historical bridge 第一次创建时 product_action_generation 原子从 0 变 1。旧 client token 不进入 canonical_operation_identity；同 Attempt 的不同 token 因 operation_id 相同而由 request_idempotency_fingerprint 明确 conflict。generation_revision 只表示 Story Provider Attempt generation，product_action_generation 只表示同一 ready Proposal 被用户 reject 后再次请求确认的代次；禁止用含糊的 `generation` 别名。仅 reject 后 CAS 创建下一代时，product_action_generation 必须递增，因此 operation_id、action_call_id、token 和 route proof 全部变化；failed 不推进本 Attempt 的 Product Action generation，旧 product_action_generation 仍只读 replay。准确 UTF-8 编码、分隔符和大小写由 golden 固定，不同进程必须得到相同 ID。

锁内验证 ProductActionProposal、Ledger、HMAC 和来源身份后，Repository 签发不可构造、不可复制、request-local 的 ProductActionRouteProof。Catalog 只接受该 proof，不接受普通 DTO；Catalog 不持有 Ledger key 或 Repository。事务结束、取消、异常和 decision 完成后 proof revoke。

pre-0029 historical-ready Story bridge 另由 Repository 在 BEGIN IMMEDIATE 内验证 exact ready Attempt、generation_revision、proposal hash、旧 token 的 baseline 格式和该 ready 行尚无 terminal confirmation hash 后，签发一次性的 HistoricalStoryRouteProof。普通 DTO、字段相同的伪造对象和 ProductActionRouteProof 都不能代替它。ready 行第一次请求没有可比较的旧 hash；Repository 用 Ledger key 与域 `historical-story-request-token-v1` 对 `{attempt_id,generation_revision,proposal_hash,legacy_confirmation_token}` 做 HMAC，原 token 不落盘。结果写入 historical_request_token_fingerprint 并进入 proposal/request identity；同 token+同 payload 收敛到同 Operation，不同 token 或 payload 返回 conflict。已经 confirmed 的历史行继续使用既有 confirmation_token_hash/payload_hash 只读 replay，绝不创建 Product Action。该 fingerprint 只证明兼容请求身份，永远不能授权 executor；真正执行仍必须在新 Product Action server token 和锁内 ExecutionAuthorization 下完成。proof 在 claim、异常、取消或事务结束后 revoke。

caller-owned publication 在 COMMIT unknown 后若 fresh reconciliation 为 exact all-absent，不得复用已消费 proof，也不得按当前 active key 重签身份。Repository 在第一次写入返回一个 Repository-bound、不可复制/序列化的一次性 opaque replay grant；只有该 grant 才能在新的 BEGIN IMMEDIATE、exact all-absent 且旧 proof 已退役时，为其中冻结的 Prepared identity（同 persisted key、token、operation/call ID 与全部 HMAC）签发一次 fresh proof；第二次 unknown 不再重放。historical bridge 的 replay grant 还必须在同一事务重验 exact baseline Attempt 并原子推进 product_action_generation/pointer。historical bridge 若已存在 attempt-bound route，则必须先用 Operation 持久化 key 重验 legacy token fingerprint 与完整 route payload：同 token+同 payload 跨 active-key rotation 收敛，不同 token 或 payload返回 conflict。

### 7.3 API

新增：

~~~text
POST /api/interview-notes/{note_id}/readiness-focus-actions
  创建 save_review_readiness_signal proposal

GET /api/product-actions/{operation_id}
  读取 safe state / terminal result；永不返回 proposed execution token

GET /api/interview-notes/{note_id}/readiness-focus-actions/{operation_id}
  Review owner 恢复 exact proposed action/token

GET /api/applications/{application_id}/product-actions/{operation_id}/rejection-control
  来源缺失时仅恢复 reject control

POST /api/product-actions/{operation_id}/decisions
  approve | modify | reject

POST /api/chat/undo-last-write
  既有 Agent/Conversation Undo 原样保留；不新增 operation-id-only generic HTTP route

POST /api/applications/{application_id}/readiness-signals/{signal_id}/undo
  owner-scoped Signal Product Action Undo；body={parent_operation_id}

POST /api/interview-stories/{story_id}/product-action-undo
  owner-scoped Story Product Action Undo；body={parent_operation_id}
~~~

Signal proposal request 是无额外字段的 exact object：

~~~json
{"proposal_id":1,"focus_id":"focus-1","expected_note_revision":2,"expected_candidate_fingerprint":"sha256:...","idempotency_key":"uuid","user_note":""}
~~~

客户端不能提交 statement、Evidence、Application/Event/Note ID、action_name 或 capability；服务器从 note path、Proposal 与 focus 重建全部可信 route payload。

两个 Product Action Undo body 都是无额外字段的 exact object，只允许 canonical parent UUID。客户端不能提交 compensation_kind、undo_json、action_name、Signal/Story revision 或 capability；owner-scoped route 和 parent terminal result 共同重建可信 undo route。仅持有或推导 operation_id 不能通过既有 Chat Undo 入口触发 Product Action compensation。Story route 必须使用现有 `/api/interview-stories/...` 资源前缀，不建立 `/api/stories/...` alias。

decision contract：

~~~json
{"confirmation_token":"...","decision":"approve"}
{"confirmation_token":"...","decision":"modify","edited_payload":{}}
{"confirmation_token":"...","decision":"reject"}
~~~

规则：

- generic GET 只返回 safe status、action kind、terminal result/unknown reason，不能只凭 operation_id 取得 execution token；
- proposed token 只在首次 proposal response 或 exact canonical owner recovery 返回。Review recovery path 固定 note_id/source_kind，Story recovery 复用 `GET /api/interview-story-proposals/{attempt_id}`；Repository 必须在锁内验证 source-bound owner 与 active Operation/route，再签发下述 request-local sealed proof union，HTTP adapter 只接受 union 成员，不能接受普通 DTO；
- `SignalOwnerRecoveryProof` 精确绑定 issuer/container、canonical review owner、application_id、note/proposal/event 的 present locator 与可信 revision、semantic_claim_fingerprint、operation_id、action_call_id、route_payload_fingerprint、route_binding_fingerprint、request_origin=current，并固定 `allowed_decisions=('approve','modify','reject')`。Signal 没有 product_action_generation，proof 中禁止该字段、禁止伪造 0/null generation；
- `StoryOwnerRecoveryProof` 精确绑定 issuer/container、canonical story owner、attempt_id、generation_revision、product_action_generation、proposal_hash、operation_id、action_call_id、route_payload_fingerprint、route_binding_fingerprint、request_origin=current|historical_story_bridge，并固定 `allowed_decisions=('approve','modify','reject')`；
- `RejectionOnlyRecoveryProof` 是 action-discriminated、route-only 的 union，不声明当前来源是否存在：Signal 分支绑定 canonical application owner、application_id、route 中预期的 Note/Proposal/Event locator、`live_source_state='not_observed'`、semantic_claim_fingerprint、operation/action_call 与两项 route fingerprint，且禁止 generation 字段；Story 分支绑定 canonical attempt owner、route 中预期的 attempt locator、`live_source_state='not_observed'`、可信 route 中的 generation_revision/product_action_generation/proposal_hash、operation/action_call 与两项 route fingerprint。两分支都固定 `allowed_decisions=('reject',)`，不能通过 nullable 通用字段互相模拟；
- 三类 proof 都绑定 issuer/container/registry incarnation，单次 render 后 revoke；ordinary/dataclass duck type、跨 union、跨 owner/source/action/Operation、replaced generation、重复消费、ABA proof 和 generic GET 都不能换取 token。full owner proof 才能陈述 verified-current source；rejection-only proof 只陈述 HMAC 已验证的 expected locator 与 not_observed，既不制造 present/absent、revision 或 generation，也不把 not_observed 当作 missing 事实；
- invalidated/source_changed Story 以及来源 missing 的 exact owner recovery 都只能签发 Story 分支 `RejectionOnlyRecoveryProof`；render 后 response 的 `confirmation_token` 字段承载不同 bytes 的 rejection-scoped opaque credential，不得返回原 server confirmation token；safe state 标记 rejection_only，approve/modify 返回 stale 且 executor=0；
- rejection-scoped opaque credential 固定使用 HMAC domain `product-action-rejection-decision-v1` 与 Operation 持久化的 key profile，绑定 action、operation_id、action_call_id、原 confirmation token fingerprint、route payload fingerprint、route binding fingerprint、semantic claim 与 `allowed_decisions=('reject',)`。approve/modify 必须在 capability/source/preflight 查询前拒绝；reject 在可信边界内映射回原持久 token identity。该 credential 不能进入 Ledger request/input/terminal fingerprint，decision body 与普通确认请求保持一致；
- Signal 来源 changed 或 Note/Proposal/Event 任一已删除时，full note-bound recovery 不可用；application-bound rejection-control 只读取 Operation+ProductActionProposal，验证 route HMAC 内的 exact application_id/operation/semantic claim 后签发 Signal 分支 `RejectionOnlyRecoveryProof`，source existence/currentness Repository、capability、binding、preflight=0；
- Story 来源 changed/missing 时，canonical Attempt route 使用 Story 分支 `RejectionOnlyRecoveryProof`。rejection-only proof 不要求也不探测来源仍存在，数据库读取失败仅指 Operation/route 本身不可读并返回 unknown；terminal、跨 owner 或普通 generic GET 绝不返回 token；
- Coordinator 向 Story adapter 提供 attempt-bound full-owner 与 rejection-only 两个公共可信恢复接口；full-owner 在 writer lock 内验证 exact active Operation/route 与 ready Attempt 后消费 `StoryOwnerRecoveryProof`，来源不再 current 时只能降级为 route-only rejection credential。adapter 不复制 proof/HMAC/route 验证器；
- approve 不携带 edited_payload；
- modify 必须携带 action-specific exact object；
- Signal modify 只允许 user_note；
- Story modify 只允许 content/evidence_links；target Story/current version/revision CAS 已由 route payload 固定，客户端重复提交的兼容 CAS 字段必须精确相等，不能修改；
- reject 不允许 edited_payload；
- reject 只解析安全 control envelope，并验证 Operation/ProductActionProposal/token/decision identity；action args/effective payload schema decode、source load、capability/binding、preflight、executor 均为 0；
- terminal replay 先读 Ledger，Provider/source Repository/executor=0。

Story 保留：

~~~text
POST /api/interview-story-proposals/{attempt_id}/confirm
~~~

但它只是新 Coordinator 的固定 action adapter：

- 禁止直接调用旧 confirm_attempt()；
- 新 Attempt 必须回显并校验服务器 token；旧 confirmation_token 只在 pre-0029 historical-ready bridge 中作为兼容请求 idempotency，Repository 必须验证并签发 HistoricalStoryRouteProof，不作为服务器 capability；
- 历史 confirmed attempt 只读重放；

另增加且只增加一个 Story Product Action proposal 入口：

~~~text
POST /api/interview-story-proposals/{attempt_id}/product-actions
body = {expected_generation_revision, expected_product_action_generation}
~~~

它只由 canonical Story owner 的“再次保存”使用，职责是把 ready + rejected current Operation 原子推进到下一代 proposed 并签发新 token；不调用 Provider，不接收旧 token，不执行 Story 写入，也不等价于 decision endpoint。相同 expected generation 的并发、响应丢失和 restart recovery 必须返回同一新 generation/token；failed、stale expected generation、pending/unknown、invalidated 或 confirmed 均 executor=0。failed 尤其不能沿用已漂移的 target CAS，必须重新生成 Story Proposal Attempt。`/confirm` 只决策请求中 token 所属的既有 generation，绝不能隐式创建下一代。
- 历史 ready attempt 先 exact CAS 创建 Product Action proposal，再由内部 server proof 执行；
- 不接受 action_name，不 fallback Agent/Legacy。

该兼容 endpoint 的成功响应保持 baseline：同一请求直接完成 primary COMMIT 且 commit 调用正常返回时为 HTTP 201/created=true；任何预先存在的 terminal、fresh-session commit-unknown reconciliation 或后续 replay 都为 HTTP 200/created=false；body 精确为 `{"story_id":<int>,"version_id":<int>,"created":<bool>}`。不持久化也不猜测“原请求是否 winner”；COMMIT 抛错后即使 fresh read 证明已提交，也固定走 reconciliation 200。错误映射保持：不存在/不可用为 `404 interview_story_not_found`；409 分别为 `story_source_conflict`、`story_idempotency_conflict`、`story_cas_conflict`、`story_conflict`；请求 shape/类型为 `422 interview_story_invalid_request`。唯一受控兼容例外是 confirmation_token：新 Attempt 要求服务器 token，pre-0029 historical-ready 才接受旧 token 作为已验证请求身份。其他字段、status/body/error code 不借本期改名或扩展。

新 Story UI 使用 Proposal response 返回的 product_action 字段：

~~~json
{
  "operation_id": "...",
  "action_call_id": "...",
  "confirmation_token": "...",
  "action_name": "confirm_interview_story"
}
~~~

前端不得继续自行生成 Story authorization token。

## 8. HITL、执行、幂等与恢复

### 8.1 Proposal 创建

Signal proposal：

~~~text
safe request parse
→ exact route issuer
→ capability short-circuit
→ application binding
→ read-only candidate preflight
→ deterministic operation ID / request fingerprint
→ BEGIN IMMEDIATE
→ idempotency + source identity recheck
→ insert WriteOperation proposed + ProductActionProposal + seq1 proposed transition
→ commit
→ return safe presentation + server token
~~~

Story proposal 在 Story Provider result 进入 ready 的事务中原子创建。Provider request 期间不创建 domain Story/Version。

同 request idempotency key + 同 route payload 返回同一 Operation/token；同 key + 不同 payload 返回 conflict。两连接 absent→insert 必须锁内重读。

Signal proposal 还必须在同一个 BEGIN IMMEDIATE 内按固定顺序检查 request idempotency、domain semantic unique 和 active semantic claim：同一原始 idempotency key + exact request 由它自己的 request fingerprint 恢复同一 Operation/token；同 key + 不同 request 仍为 idempotency conflict。已有 committed Signal 时直接返回 already_confirmed 的只读定位，WriteOperation/ProductActionProposal=0。已有 active semantic claim 时，任何不同 idempotency key——即使可信 route 与 user_note 字节相同——都只返回稳定 `409 review_readiness_action_in_progress`，Operation/token/owner identity 均不泄露，也不创建 alias、第二个 Operation 或 accepted idempotency mapping；只有 winner 的原始 key 能走 canonical owner recovery。该 409 是未接纳的瞬态冲突：winner 仍 active 时，同一 loser key 的首次响应丢失与任意重试都继续 409；winner committed 后转为 already_confirmed；winner rejected/definite failed 释放 claim 后，loser 使用同一 key可以首次被接纳并创建新 Operation，因为此前从未为该 key 持久化成功映射。partial unique 处理两连接竞态，winner 之外永远不留下 proposed row。

terminal operation_request_fingerprint 使用 Ledger HMAC key 和域
product-action-request-v1，canonical envelope 固定覆盖：

~~~text
operation_id
action_call_id
action_name
decision
effective_payload canonical digest（reject 为 null）
confirmation token fingerprint
proposal fingerprint
route binding fingerprint
~~~

terminal replay 必须重算并比较；同 Operation 的不同 decision 或 effective payload 返回 conflict。

Product Action primary 在 `committed` / `failed` terminal 写入的
`input_fingerprint` 使用 Ledger HMAC key 和独立域
`product-action-input-v1`。canonical envelope 精确为：

~~~text
operation_request_fingerprint
authorization_scope_fingerprint
effective_payload_sha256
~~~

其中 `effective_payload_sha256` 是 effective payload canonical JSON bytes 的
`sha256:<lowercase hex>`；不得用 Agent Tool 的 `write-operation-input-v1`，也不得省略
scope 绑定。`rejected` 仍按 terminal shape 保持 `input_fingerprint=NULL`。

### 8.2 Approve / modify

统一顺序：

~~~text
解析安全控制字段
→ Ledger-first lookup
→ terminal: fingerprint/integrity 验证后 replay
→ proposed: load ProductActionProposal
→ verify server token + route provenance
→ decode approve/modify effective payload
→ transaction-external read-only preflight
→ BEGIN IMMEDIATE
→ reload Operation + ProductActionProposal
→ mutable source/CAS/capability/binding recheck
→ claim + create request-local ExecutionAuthorization
→ claim winner 在本次 execution 内调用 Session-bound executor 恰好一次
→ result/transport/required undo 全部生成并通过 cap
→ domain + Ledger terminal + route material clear
→ one COMMIT
~~~

claim 失败、token/proof mismatch、stale、permission/binding failure时 executor=0。executor 抛普通映射领域异常时恰好调用一次；未映射 internal/SQLite/serialization 异常回滚，Operation 保持 proposed。

### 8.3 Reject

~~~text
Operation/ProductActionProposal/token/decision identity
→ rejection CAS
→ rejection terminal + route material clear
→ direct response
~~~

除安全 control envelope 外，action args/effective payload decode、capability、binding、source Repository、preflight、Provider、executor 均为 0。来源已删除或内容已变化也不能阻止用户拒绝。

### 8.4 Story executor

现有 Story Repository 拆为：

~~~python
confirm_attempt_bound(session, trusted_request, authorization) -> StoryWriteResult
~~~

它只 flush，不 commit/rollback/close。生产旧自建 Session confirm path 删除。

锁内复核：

- attempt 为 exact ready generation_revision/product_action_generation；
- Product Action operation identity 一致；
- proposal_hash/source_fingerprint 未变化；
- selected sources 仍 current；
- target Story current_version/story_revision CAS；
- effective content/evidence 通过既有 canonical/evidence validator。

成功同事务写 Story/Version/Evidence/UserAssertion、attempt confirmed、Ledger terminal。Story Provider 调用不在该事务内，approval Provider=0。

source/target CAS 漂移在 claim 前识别；为保持现有 Story GET 语义，可在该短事务只把 Attempt 标记为 invalidated(source_changed)，Operation 保持 proposed/未 claimed、route 保留，executor=0。该状态只能 reject 或只读 stale recovery，不能签发下一代或重新执行；reject 不读取 source。它不是 Story/Version 写入，也不伪造 failed terminal。

### 8.5 Signal executor

锁内复核：

- Application/Event/Note/Proposal 可见和 exact；
- Note revision/fingerprint；
- Proposal V2/hash/source revision；
- focus/evidence/candidate fingerprint；
- semantic unique 与 domain idempotency；
- Operation authorization scope。

成功同事务写 Signal/Version/Evidence 和 Ledger terminal。不存在 created=false committed。正常生产调用图的 semantic duplicate 已在 proposal active claim/domain preflight 收敛；executor 前若发现没有对应 active claim 的 domain duplicate，视为完整性错误并 fail-closed，不能伪装成可重试的 stable conflict。

### 8.6 Product Action terminal codec

`product_action_json_v1` 复用 Phase 3 的完整 terminal envelope；terminal_payload_sha256 必须覆盖 canonical：

~~~text
status
result_contract
result_json
visible_result
transport_json
undo_json
failure_category
failure_code
~~~

Signal committed result_json 只能是：

~~~json
{"schema_version":1,"action_name":"save_review_readiness_signal","outcome":"created","signal_id":1,"signal_version_id":1,"signal_revision":1,"source_status":"current"}
~~~

visible_result 固定为“已保存为下次准备重点。”；transport_json 只包含 schema_version、operation_id、action_name、status 和上述安全 result；undo_json 精确为：

~~~json
{"kind":"retract_review_readiness_signal_v1","signal_id":1,"created_version_id":1,"expected_current_version_id":1,"expected_signal_revision":1,"parent_operation_id":"uuid"}
~~~

Story committed result_json 只能是：

~~~json
{"schema_version":1,"action_name":"confirm_interview_story","outcome":"created|version_appended","story_id":1,"story_version_id":1,"story_revision":1}
~~~

visible_result 固定为“已保存到经历素材。”；transport_json 只保存安全 ID/outcome/revision，并同时冻结可纯函数投影的 `legacy_direct_commit={status_code:201,body:{story_id,version_id,created:true}}` 与 `legacy_reconciliation_or_replay={status_code:200,body:{story_id,version_id,created:false}}`，不保存 Story 正文或 Evidence。HTTP adapter 只依据“本次 commit 调用是否正常返回且本请求完成 transition”选择第一支；任何 fresh read 都选择第二支，不能重算或改写 terminal payload。undo_json 使用 §9.3 两个 exact union。

Coordinator 的 DecisionResult 必须携带已经通过 action-local exact codec、terminal digest 与 persisted-key 校验的递归只读 transport，并按 direct commit 或 reconciliation/replay 暴露其中已冻结的 legacy projection；Story adapter 禁止 fresh-load 后重算 transport、status 或 created。

failed result_json 固定为 `{schema_version, action_name, outcome:'failed', code}`；transport 只复制 operation/action/status/code，undo_json 必须 NULL；visible_result 只能由封闭 code→文本 renderer 产生。

rejected 继续使用 rejection_json_v1 的 Product Action 子形状 `{schema_version:1, action_name, decision:'rejected'}`。纯函数 renderer 对两个 action 分别固定 visible_result 为“已取消保存准备重点。”和“已取消保存经历素材。”；transport_json 精确为 `{schema_version:1,operation_id,action_name,status:'rejected',result:{decision:'rejected'}}`，HTTP status=200，undo_json=NULL。它不接受或保存 feedback、effective payload、来源正文或异常文本；result/visible/transport/aggregate 仍受 4/1/4/12 KiB cap，terminal digest golden 同时覆盖两个 action 与 replay。

Action-local budgets 同时受 Ledger 全局 cap 限制：Signal result/visible/transport/undo 为 4/1/4/4 KiB、aggregate 16 KiB；Story 为 4/1/4/32 KiB、aggregate 48 KiB；rejected/failed 为 4/1/4/0 KiB、aggregate 12 KiB。全部按 UTF-8 bytes 计算，任一 projector/renderer/required undo/cap 失败回滚事务，不能提交领域写入。

异常 disposition 固定为：

- executor 前 validation/capability/binding/source/CAS 失败：Operation 保持 proposed、route 保留、executor=0；
- reject：rejected terminal，parent terminal trigger 清 route；
- executor 已调用后，只有 ProductActionSpec 显式声明、可证明确定且 SAVEPOINT 无副作用的领域异常，才 rollback executor SAVEPOINT 后写 failed terminal；executor 恰好 1 次；
- Signal V1 不声明 executor-after-call terminal failure；Story V1 仅允许显式 `product_action_story_write_conflict → conflict`，且必须证明 SAVEPOINT 已回滚且无 domain mutation；未来新增必须提升封闭映射 golden；
- SQLite、serialization、result/transport/undo projector、未映射 Exception 或 internal_error：回滚整个事务，Operation 保持 proposed、route 保留；同一次 execute 不得再次调用 executor。

任何 IntegrityError 都不能按异常类自动映射 conflict。每个 Spec 的 declared exception table、result/transport/undo codec、字段集合和 renderer 都由只读 golden 固定。

### 8.7 Commit unknown

COMMIT 抛错后用 fresh Session 对账，但 proposal publication、decision execution 和 compensation 是三个不同状态机，不能共用 absent 语义。

Signal proposal publication：

~~~text
absent
proposed
terminal
unreadable
~~~

- absent：parent/route/transition 均 absent时，才可同 deterministic ID 重建完整 proposal+seq1，executor=0；任一 partial 为 integrity error；
- proposed：验证 route/request identity与exact `[(1,proposed)]` 后返回同 Operation/token；
- terminal：验证完整 terminal digest、request identity与§6.7.1 exact terminal prefix后 replay；
- unreadable：operation_result_unknown，不生成新 key。

Story 首次 ready publication 必须使用 §6.6.1 Attempt+Operation+route+transition 四对象矩阵；仅 reject 后的 next-generation proposal 使用 §6.6.2 的专属 N→N+1 矩阵。failed 必须重新生成 Story Proposal Attempt，不能进入 N→N+1。两类 publication 的 absent 条件不得互换，任何分支都禁止单独补写 Product Action/transition或第二次 Provider。

Decision execution：Operation/route/seq1 在请求前已持久存在，因此 fresh read任一 absent/partial/prefix mismatch都是 integrity error，不能重建或补 transition；`proposed + [(1,proposed)]` 表示领域+terminal事务未提交，可由携带原 decision payload的同请求按同 Operation再执行；`terminal` 验证 digest/request fingerprint与exact terminal prefix后executor=0 replay；`unreadable` 返回 operation_result_unknown。不同 decision/effective payload conflict，不生成新 key、不fallback旧 executor。

Compensation 使用 §9.1 独立 proposal/execution 四态对账。

### 8.8 Cancellation 与 BaseException

- 只捕获普通 Exception 做封闭映射；
- CancelledError、KeyboardInterrupt、SystemExit 和其他 BaseException cleanup 后原样传播；
- 取消不自动 reject、不创建领域行、不写安全 fallback；
- 已提交 terminal 不回滚，随后由 replay 读取；
- 任何异常不得触发第二次 executor。

## 9. Required Undo

### 9.1 独立 ProductActionCompensationCoordinator

现有 Agent Compensation 路径假定 typed parent、Conversation 和 last-write。Product Action 不能直接套用。

新增独立 Coordinator，或把共享纯核心安全泛化，但必须保证：

- parent.adapter_kind=product_action；
- conversation_id/agent_run_id 均 NULL；
- parent committed、terminal digest 和 compensation kind exact；
- compensation delivery not_applicable；
- 不读取/修改 Conversation last-write；
- replay executor=0。

#### 9.1.1 Owner-scoped Undo authorization

Product Action 的 operation_id、terminal result 和 undo_json 都不是执行 capability。baseline 只有 `POST /api/chat/undo-last-write`：它继续绑定 conversation_id、可选 parent_operation_id 与 Conversation last-write，HTTP contract/错误码全字节不变；本期禁止新增 operation-id-only generic Undo route。Product Action parent 的 conversation_id=NULL，无法通过既有 Chat owner 校验，compensation executor=0。即使内部复用纯 Coordinator port，也不能把该 port直接暴露为 HTTP。

Composition 创建两个可复用、source-bound issuer capability：

~~~text
ReadinessSignalUndoIssuer
InterviewStoryUndoIssuer
~~~

owner-scoped HTTP route 的固定顺序：capability short-circuit → canonical Application/Story binding → 按 path 与 body 加载 exact domain aggregate + committed parent → 验证 parent action/result/undo 指向该 aggregate → 签发 request-local sealed proof → 进入 Coordinator。缺 capability 时不得查询实体；跨 owner/scope 与不存在继续使用同一安全 not-found。

proof 是不可由普通调用方构造、不可复制的 action-discriminated union：

~~~text
ReadinessSignalProductActionUndoProof
  issuer/container/registry incarnation
  canonical application owner
  application_id + signal_id
  expected current_version_id + signal_revision
  parent_operation_id + parent action + terminal digest
  compensation_kind = undo:save_review_readiness_signal

InterviewStoryProductActionUndoProof
  issuer/container/registry incarnation
  canonical story owner
  story_id + source attempt_id
  expected current_version_id + story_revision
  parent_operation_id + parent action + terminal digest
  compensation_kind = undo:confirm_interview_story
~~~

Signal proof 不依赖 Note/Event/Proposal 存在性：来源已 missing 时，Repository 仍必须从 Signal.application_id 验证 canonical owner，所以 required Undo 保持可用但不能跨 Application。Story proof 从 committed parent result/undo 与 Story aggregate验证 story_id/attempt lineage。proof 不进入 JSON/日志/Ledger/Journal，request 结束、异常、取消、proposal 失败或消费后 revoke；ordinary DTO、跨 action/scope/owner/container、replay proof、ABA proof 全部拒绝。

Coordinator 在 `BEGIN IMMEDIATE` 内先验证 proof provenance/未消费，再重读 parent terminal digest和 exact Signal/Story binding、current pointer/revision、capability snapshot；只有 exact 才原子 consume proof并创建/读取 deterministic compensation。claim 后由既有一次性 ExecutionAuthorization 执行。proof 不跨请求持久化；响应丢失重试由 owner-scoped route重新权威加载并签发新 request-local proof，再按 deterministic compensation Operation replay，executor=0。

固定身份：

~~~text
PRODUCT_ACTION_COMPENSATION_NAMESPACE =
  1c914194-602c-54fa-b770-853a5ac87a2b

operation_id = uuid5(
  PRODUCT_ACTION_COMPENSATION_NAMESPACE,
  lowercase_canonical_parent_uuid + ":" + compensation_kind
)
~~~

只允许：

~~~text
save_review_readiness_signal → undo:save_review_readiness_signal
confirm_interview_story      → undo:confirm_interview_story
~~~

冒号、UTF-8、canonical UUID 和两个跨进程 UUID golden 固定。其他 parent/action/compensation 组合在 ProductActionCompensationCatalog、Coordinator 和数据库 trigger 三层拒绝；Agent Compensation Registry 不得解析这两个名称。

operation_request_fingerprint 使用域 `product-action-compensation-request-v1`，封闭 envelope 为：

~~~json
{"request_kind":"product_action_compensation_v1","operation_id":"uuid","parent_operation_id":"uuid","parent_action_name":"...","parent_terminal_payload_sha256":"sha256:...","compensation_kind":"undo:..."}
~~~

input_fingerprint 使用域 `product-action-compensation-input-v1`，封闭 envelope 为 `{operation_request_fingerprint,parent_terminal_payload_sha256,validated_undo_json}`。Ledger shape 保持 operation_role=compensation、adapter_kind=compensation、conversation_id/agent_run_id/tool_call_id/proposal_fingerprint/confirmation_token_fingerprint/authorization_scope_fingerprint 全部 NULL、result_contract=compensation_json_v1、terminal delivery=not_applicable。

Proposal 阶段：

~~~text
BEGIN IMMEDIATE
→ 锁内重读 deterministic operation_id
→ absent: 验证 parent committed/product_action/exact action、terminal digest、required undo exact schema
          → insert compensation proposed + seq1 proposed transition
→ existing: 验证 request fingerprint、parent identity、digest 与 exact transition prefix
→ COMMIT
~~~

Execution 阶段：

~~~text
BEGIN IMMEDIATE
→ reload compensation + parent
→ revalidate parent terminal digest / mapping / undo + compensation exact proposed prefix
→ compute input_fingerprint
→ approved / claimed transitions
→ SAVEPOINT executor，最多调用一次
→ domain mutation + compensation terminal
→ one COMMIT
~~~

Signal Undo 不再读取 Note/Event/Proposal，只按 parent undo_json 验证 Signal current pointer/revision。基础设施、serialization、projector、未映射 Exception 回滚整个事务并保持 proposed；显式 mapped domain stale 可写封闭 failed terminal。不得调用 Provider、写 Conversation last-write、创建 ProductActionProposal 或 fallback Agent Compensation。

compensation_json_v1 继续使用 Phase 3 完整 terminal digest：Signal 成功结果只含 `{kind,signal_id,retracted_version_id,signal_revision}`；Story 成功结果只含 `{kind,story_id,current_version_id,story_revision,status}`；visible/transport 由固定 renderer 生成且无正文；undo_json 必须 NULL。result/visible/transport/aggregate cap 为 4/1/4/12 KiB，并继续受 Ledger 全局上限约束。

proposal/execution COMMIT unknown 均 fresh-read `absent | proposed | terminal | unreadable`并验证transition prefix：proposal+absent 仅在 compensation parent与transition均absent时可用同 deterministic ID 重建proposal+seq1且executor=0；proposed必须是exact `[(1,proposed)]`才可进入/重试execution；terminal校验完整digest与seq1/2/3/4 prefix后executor=0 replay；execution commit-unknown后parent或seq1 absent/partial是完整性异常（proposal前一事务已提交），不能新建或补transition；unreadable返回operation_result_unknown；parent/digest/request/transition mismatch全部fail-closed。两连接同时absent必须锁内重读，只产生一个compensation和一个executor winner。

### 9.2 Signal Undo

~~~text
undo:save_review_readiness_signal
~~~

验证 current version 仍是 parent 创建的 active version，Signal revision 未变化；随后追加 retracted Version，更新 current pointer/revision。它不物理删除历史，不修改 Note/Proposal/Practice/Preparation。

retracted Version 的完整写入契约：

~~~text
READINESS_SIGNAL_RETRACTION_VERSION_NAMESPACE =
  6fc5aa59-d7c6-53e0-9f3e-98aac460b593

version_number       = parent active version_number + 1
parent_version_id    = parent active version ID
disposition          = retracted
schema_version       = readiness-signal-v1
statement_text       = byte-for-byte copy parent
user_note            = byte-for-byte copy parent
source_note_revision = copy parent
source_note_fingerprint / source_proposal_hash / candidate_fingerprint = copy parent
domain_idempotency_key = uuid5(namespace,
  lowercase_compensation_operation_id + ":signal-retraction")
write_operation_id   = compensation operation ID
~~~

所有 Evidence rows 按 ordinal/source_path/excerpt/excerpt_sha256/source_field_sha256 byte-for-byte 复制到新 Version；不重新读取或验证 Note/Proposal。复制、Version INSERT、Signal current pointer/revision 增加和 compensation terminal 必须同事务。parent/Evidence 任一损坏或超 cap 均 fail-closed，不生成部分 retraction。

来源 Note/Event/Proposal 已删除并变为 missing 时，Undo 仍可基于 Signal current pointer/revision 安全追加 retracted Version；不得因为来源不可读而让 required Undo 永久失效。

### 9.3 Story Undo

~~~text
undo:confirm_interview_story
~~~

封闭 undo payload union：

新 Story：

~~~text
archive_created_story_v1
story_id
created_version_id
expected_current_version_id
expected_story_revision
expected_status=active
~~~

CAS 成立时 archive Story 并增加 revision；不删除 Story/Version/Evidence。

追加 Version：

~~~text
restore_story_pointer_v1
story_id
created_version_id
previous_current_version_id
previous_title
expected_post_revision
~~~

要求 current pointer 仍指向 parent 创建 Version、revision 未变化；随后恢复 previous pointer/title 并增加 revision。新 Version 保留为不可变历史。任何后续 Story 修改都返回 stale，不能覆盖新状态。

previous_title 是 required Undo 唯一允许进入 undo_json 的受限用户字段，沿用 Story title 的字段 cap；它禁止进入 visible_result、transport、日志、Journal、Manifest、error 和测试报告。其他 Story 正文/Evidence 不得进入 undo payload。

## 10. Signal 状态与只读投影

### 10.1 Source 状态

~~~text
current
  source IDs 存在，Note/Event/Proposal 可见且 revision/hash/focus/evidence 一致

changed
  source 仍可见，但 revision/hash/focus/evidence 任一变化

missing
  source FK 为 NULL、资源物理缺失或关系断裂

unavailable
  Application 软删除、越权、DB/完整性无法确认

retracted
  current Version disposition=retracted
~~~

changed/missing/unavailable/retracted 保留历史解释，但不能创建新的 Practice、Preparation selection 或 Context input。

### 10.2 Readiness advisory

基础 InterviewReadinessResult.ready 不变化。新增正交投影：

~~~ts
interface ReadinessFeedbackAdvisoryV1 {
  signalId: number;
  versionId: number;
  practiceSourceFingerprint: `sha256:${string}`;
  practiceTargetFingerprint: `sha256:${string}`;
  state: 'available' | 'practiced' | 'stale_source' | 'retracted' | 'unavailable';
  practiceState: 'not_started' | 'in_progress' | 'completed' | 'legacy_only';
  selected: boolean;
  title: string;
  sourceLabel: string;
}
~~~

规则：

- 只从 same Application、current active Signal 产生可选项；
- source Event 保持 completed；
- target Event 必须由用户 exact 选择、属于 same Application、event_type=interview、lifecycle=scheduled/in_progress；
- source Event 与 target Event 必须不同；
- 不按 scheduled_at 猜下一场；
- practiced 只来自 exact (Signal Version, target Event) completed Plan；
- self_assessment 不进入 advisory state；
- 无 Signal 是正常空态，不使基础 readiness 失败。

### 10.3 只读 API

~~~text
GET /api/interview-notes/{note_id}/readiness-feedback-candidates?proposal_id=...
GET /api/applications/{application_id}/events/{event_id}/readiness-feedback
GET /api/applications/{application_id}/readiness-signals/{signal_id}
GET /api/interview-practice/focus/{signal_version_id}?target_event_id=...
~~~

不存在与跨 scope 统一安全 404。读取异常返回 unavailable/error，不能伪装成空数组。

## 11. Adaptive Practice V2

### 11.1 Source 与目标

新推荐唯一来源为 current active Signal Version。指定 Version/target Event 不合法时 fail-closed，不能回退其他 Proposal、Signal 或列表第一项。

target 使用独立 `practice_target_fingerprint_v1`，同样是 canonical JSON SHA-256：

~~~text
sha256({
  contract:'practice_target_fingerprint_v1',
  event_id, application_id, event_type, subtype,
  tags:<validated ordered string array>, round,
  scheduled_at:<UTC RFC3339 or tagged absent>,
  duration_minutes, lifecycle_status
})
~~~

location、notes、remind_at 和 created_at 不进入练习授权。整数/时间/tagged-absent、Unicode 与 JSON canonical 规则和 practice source fingerprint 相同。带 exact target 的 readiness-feedback/practice-focus只读响应必须同时返回 source fingerprint 与 target fingerprint；普通 ApplicationEvent 列表不承担该授权材料。

精确状态：

~~~text
ready
in_progress
completed
source_changed
source_missing
target_changed
target_missing
retracted
not_eligible
unavailable
~~~

V2 list/get 以持久化 source_fingerprint/target_fingerprint 与可用的当前 aggregate 比较来投影 source_changed/target_changed；FK 为 NULL 时分别投影 missing。该投影只描述“现在是否还能据此新建 Plan”，不改写 frozen Plan，也不阻止既有 in_progress Plan completion。读取失败为 unavailable，不能误报 changed/missing。

### 11.2 Start V2

~~~json
{
  "readiness_signal_version_id": 91,
  "target_application_event_id": 103,
  "expected_source_fingerprint": "sha256:...",
  "expected_target_fingerprint": "sha256:...",
  "idempotency_key": "uuid"
}
~~~

Start 的锁内顺序固定为 idempotency-first：

1. safe decode 后计算仅来自 request body 的 `start_input_fingerprint={idempotency_key,readiness_signal_version_id,expected_source_fingerprint,target_application_event_id,expected_target_fingerprint}`；
2. `BEGIN IMMEDIATE` 后先按 start_idempotency_key 查询；existing + exact stored start_input_fingerprint 直接 replay冻结 Plan，source/target Repository=0，即使 locator后来 changed/missing/retracted/terminal也不改变首次响应身份；existing + different fingerprint 返回 idempotency conflict；
3. 只有 key absent 才按与只读 API 相同的 canonical loader重算 `practice_source_fingerprint_v1` 与 `practice_target_fingerprint_v1`，验证 Signal current/active、source current、same Application、target exact lifecycle、pair unique；
4. source expected 不一致即 source_changed；target expected 不一致即 target_changed；Plan=0；exact 时把 source fingerprint 写入现有 source_fingerprint、target fingerprint写入新 target_fingerprint，并与 start_input_fingerprint、immutable snapshot 同事务提交。

因此响应丢失重试始终回放首次冻结 Plan；“来源变化后不能创建新 Plan”只约束新 idempotency key，不会让已经创建的 frozen Plan消失或变成 conflict。

V2 继续使用现有 Plan 的单组冻结 source_path/source_excerpt/source_hash 列，选择规则固定为 Evidence ordinal=0；statement/user_note 进入受限 drill 文案，primary Evidence 逐字复制到旧 snapshot 列。其他 Evidence 不复制进 Plan，但全部参与 practice source fingerprint，所以任一行、顺序或 hash 变化都会阻止新建/replay identity漂移。不得依赖数据库无 ORDER BY 的返回顺序，也不得随机选择“最相关” Evidence。Provider=0。

旧 V1：

- 既有 start key replay 原 Plan；
- 已有 in-progress/completed 可读、可完成；
- 禁止创建新 legacy Plan，返回 410 adaptive_practice_v1_retired；
- 不回填 Signal 或 target Event；
- 前端同批切换，无隐式 fallback。

V2 list/get 按 origin_contract 分支，不能继续依赖 legacy source inner join 导致冻结 Plan 因 source 删除而消失。

### 11.3 Completion

Completion CAS 保持。完成后：

- Plan=completed；
- Signal 不自动 retract/resolve/master；
- advisory 可显示“已完成一次训练”；
- response/reflection/self_assessment 不写 Signal/Memory/Knowledge/Story/Question；
- source 后续变化不覆盖冻结 Plan 历史。

### 11.4 单 owner

review feedback、question bank、quick practice 都是 interview.free_practice owner 内部 mode。handoff：

~~~ts
{
  ref: { taskId: 'interview.free_practice' },
  source: 'application_task_card',
  focus: 'source',
  hints: {
    practiceMode: 'review_feedback',
    readinessSignalVersionId: 91,
    targetEventId: 103
  }
}
~~~

hints 仅用于 owner 内重新解析，不进入 canonical key，不携带正文/hash。invalid hint 显示 unavailable，不回退其他推荐。

## 12. Interview Preparation Input V2

### 12.1 精确选择

application.interview_prepare 基础 readiness 完成后显示“来自复盘的准备重点”。默认不选。用户可选 0–8 个 current active Signal Version，全部必须 same Application，target 为当前 exact Event。

选择存在于 preparation draft/attempt，不是 Signal 属性。没有 Signal、没有选择、加载失败均不得自动补全。

### 12.2 V1/V2

- request 不含 readiness_feedback_version_ids：继续生成 interview-preparation-input-v1，canonical JSON/fingerprint 与 baseline 字节等价；
- 显式包含字段，包括空数组：生成 interview-preparation-input-v2；
- 历史 V1 validator/renderer 永久只读兼容；
- V2 不覆盖旧 Proposal；重新生成使用新 idempotency key。

HTTP safe decoder 必须在默认值填充前从原始 JSON object 冻结 presence：

~~~text
readiness_feedback_version_ids_present = object has own key
~~~

Typed request 同时保存该 bool 与有序 `tuple[int,...]`。字段缺失时 present=false，直接调用未修改 V1 builder，V1 snapshot/fingerprint 不增加任何新 envelope；字段存在且 `[]` 时 present=true 并生成 V2；显式 null、非 array、bool/int 混淆、重复 ID 或超过 8 均 422。实现可读取 Pydantic model_fields_set，但禁止在 `model_dump`/默认值归一化后推断 presence。

V2 request/input fingerprint 明确包含 `{readiness_feedback_selection:{present:true,ordered_version_ids:[...]}}`；V1 完全没有该字段。同一 idempotency key 的 absent 与 explicit empty 必须 conflict。前端 V1 请求必须真正省略字段；unknown/replay 从 Attempt 冻结的 contract version/presence 恢复，不从当前 UI 默认值重算。

V2 增加：

~~~json
{
  "readiness_feedback": [
    {
      "statement": "用户确认的准备重点",
      "user_note": "",
      "source_event": {"round": 1, "subtype": "technical"},
      "practice_state": "completed",
      "evidence": [
        {"path": "/difficulty_points", "excerpt": "逐字片段", "excerpt_sha256": "..."}
      ]
    }
  ]
}
~~~

Provider payload 不包含 Signal/Version/Event 内部 ID。内容作为 untrusted user context，不进入 system policy。

### 12.3 Selection Loader

Interview Preparation 是独立 Non-Agent Provider boundary，不经过 Agent Context Projector。新增 Session-bound：

~~~text
PreparationReadinessSelectionLoader
~~~

在与 Event/JD/Resume 相同的冻结读取事务中：

1. exact 读取客户端选择的 Version IDs；
2. 验证 current pointer、active、source current，并对每个选择强制 source Event lifecycle=completed；
3. 权威加载 target Event，强制它属于同一 Application、`event_type=interview`、EventLifecycleV1 为 scheduled/in_progress，且每个 selected Signal 的 source_event_id 均不等于 target_event_id；不得使用 scheduled_at、客户端标签或唯一候选推断；
4. 复制 immutable DTO；
5. rollback 后 canonicalize/fingerprint/chunk；
6. 写入 attempt.input_snapshot_json；
7. Provider fallback 复用同一冻结输入。

任一 selected item 不可用、target 为 completed/cancelled/unknown/非法类型/跨 Application，或任一 source==target，均整体 fail-closed、Provider=0。direct API、UI owner 和恢复路径共享同一 Loader，不能只在 advisory 层校验。不能把客户端 page context 中的正文当成 Signal。

### 12.4 上限与 Evidence

- Signal 最多 8；
- 每个 Evidence 最多 5；
- statement 总计 16 KiB；
- excerpt 总计 32 KiB；
- user_note 总计 8 KiB；
- readiness_feedback envelope 64 KiB；
- 再受 Provider token/byte cap；超限 Provider=0，不截断 mandatory chain。

64 KiB 是完整 canonical `readiness_feedback` envelope 的 UTF-8 byte cap，不是各字段独立 cap 的相加近似。计算使用与最终 Provider input 相同的键排序、分隔符、字段名、数组/对象包装、JSON escaping、中文/emoji 编码。各 raw 字段上限不能同时达到：8×5 数量 high-water fixture 使用引号、反斜杠、中文和 emoji，并按最终 wrapper 精确填充到最大 `<=65536`；65,537-byte fixture必须 fail-closed。各单字段最大值由单独较小组合验证。Builder 每加入一个完整 Signal 都用最终 canonical wrapper 计算增量，不截断字段；若封闭 fixture 无法满足 cap，实施前必须下调版本化子预算并重新书面复审，禁止运行时随机裁剪。

V2 output Evidence source 增加 confirmed_readiness_feedback，只能引用本次冻结 snapshot 的 provider path。不得引用未选择 Signal 或把 user_note 当外部证据。

### 12.5 漂移与 lease

现有 Preparation Provider lease/CAS 保持。最终回写验证：

- selected Version 仍为 current active；
- source 仍 current且 source Event lifecycle=completed；
- Application/Resume exact；target Event 仍属于同一 Application、`event_type=interview`、lifecycle=scheduled/in_progress；每个 source_event_id 仍与 target_event_id 不同；
- Evidence hash 不变；
- preparation attempt generation/lease exact。

Provider 期间 target 变为 completed/cancelled/unknown、source/target 关系变化或其他任一条件变化时，未 ready attempt 进入既有 source_conflict/invalidated，迟到 Provider result 丢弃。已 ready V2 保持冻结历史。

## 13. Context Projector confirmed_memory 边界

### 13.1 保持 Phase 4 生产不变量

本期 Preparation V2 由独立的 `PreparationReadinessSelectionLoader` 服务，不经过 Agent Context Projector。Phase 4 已冻结的三个生产 Contributor——`confirmed_memory`、`knowledge_context`、`older_conversation_summary`——继续无条件返回 `disabled`，0 Repository/Provider/network；本期不得修改 Contributor registry、`SurfaceManifestV2` 状态矩阵或任何 Agent 调用图来令 `confirmed_memory=ready`。

`ConfirmedReadinessContributorPort` 只可作为未注册的未来类型资产存在：validator/golden 使用纯合成 DTO，生产 composition root、Projector selector 和 Runner 的 AST/call-graph 必须不可达；不得把普通 `ContributorResult(status='ready')` 当作资格证明。未来若要接入 Agent `application.interview_prepare` Segment，必须另开设计，引入不可伪造且 source-bound 的 eligibility proof、更新 Manifest/budget policy version，并重新复审隐私与 fallback，不能在本期暗启。

### 13.2 Preparation 独立 Loader

Preparation request/attempt snapshot 持久化 ordered selected Signal Version IDs 与 selection fingerprint。`PreparationReadinessSelectionLoader` 在同一个 Session-bound read UoW 中按 exact IDs 加载；空选择表示本次 Preparation 没有 readiness feedback，不查询 Application 全部 Signal。任一显式选择 stale/missing/retracted 均 fail-closed、Preparation Provider=0。

Loader 输出只包含有界 statement、明确标注的 user_note、安全来源标签和 opaque source ref。禁止完整 Note/Proposal、内部 Operation/token。它只进入该 Preparation Attempt 的冻结 Provider input 与 Evidence；不产生 Context Projector Runtime audit 或 Journal Surface Manifest，也不声称是 confirmed memory。

### 13.3 未来桥接资产门禁

独立 sealed future-port fixture 可以验证 ordered version IDs、application/target Event/Resume identity、selection fingerprint 和 issuer-shaped字段的 canonical schema，但不得创建可被生产 Runner 接受的 proof 对象。机械门禁固定：生产 `confirmed_memory` 恒为 disabled；`PreparationReadinessSelectionLoader` 只能被 Interview Preparation owner 调用；普通 Chat/Application Chat/Haru 的 Signal query=0；任何把 future port 注册进 Projector 或返回 ready 的改动都使本期 gate 失败。

## 14. 桌面交互与 owner

### 14.1 Review owner

在 V2 Proposal practice_focuses 下增加“复盘后的下一步”：

~~~text
选择一个有来源的准备重点
→ 主操作：保存为下次准备重点
→ 次级操作：整理为经历素材
~~~

同一时刻只有一个强调主操作。V1 Proposal、generating、result_unknown、source_changed、无安全 focus、读取失败均不显示保存执行按钮。

Signal Product Action confirmation 在 review owner 内展示；不打开 Chat Pending，不创建第二个浮层 owner。proposed/replay 状态由 operation_id 驱动。

### 14.2 Story owner

Story Proposal 生成保持现有 Provider flow。ready 后：

- response 返回 server Product Action identity/token；
- 用户可在 Story editor 修改内容；
- 保存通过 Product Action decision；
- reject 不改变 Story Proposal，可重新编辑并创建/使用新 proposal；
- unknown 使用原 Operation/token/effective payload 对账；
- 前端删除自行生成 authorization token 的路径。

### 14.3 Preparation owner

- 默认不选 Signal；
- 最多 8 个；
- stale/retracted/unknown 不可选；
- 选择不立即调用 Provider 或写业务；
- 点击原“生成准备建议”时冻结 Selection；
- Provider unknown 保留原 preparation key。

### 14.4 Practice owner

review_feedback/question_bank/quick_practice 共享 interview.free_practice owner、draft guard 和焦点恢复。draft 按 owner generation + Signal Version + target Event/plan 隔离。

### 14.5 Haru/Pilot

Haru/Pilot 只展示安全摘要和打开 exact Core Task。它们不扫描 Signal、不持有 Product Action token/draft、不直接调用 Repository、不建立确认 UI、不猜 Event/Resume。

### 14.6 无障碍与桌面范围

- loading aria-busy；
- error role=alert；
- 状态变化有界 aria-live；
- 状态不只靠颜色；
- 主操作至少 44×44px；
- 768/1024/1280/1440；
- heading focus、关闭恢复焦点；
- reduced motion；
- 移动端不在范围。

## 15. 隐私、删除与诊断

### 15.1 数据最小化

Signal 表只保存受限 statement、user_note、逐字 excerpt、hash/revision/关系。ProductActionProposal 只保存引用型 route payload。

不复制：

- 完整 Note；
- 完整 Proposal snapshot；
- Story 正文到 ProductActionProposal；
- Resume/JD/Prompt/模型回答/聊天/附件；
- raw confirmation token；
- issuer/proof/Session。

### 15.2 日志

日志、Journal、Manifest、CR fixture 禁止 statement、user_note、excerpt、Story content、完整 args/result、异常原文和 token。只允许：

~~~text
schema/version
safe status/reason code
count/UTF-8 bytes/duration
HMAC source identity
public contract fingerprint
~~~

Golden 使用合成 canary。扫描日志、Snapshot、Event、WriteOperation transport、sessionStorage 和 release report。

### 15.3 删除

- Application hard delete 级联 Signal/Version/Evidence；soft delete 只隐藏；
- Note/Event/Proposal delete 对 Signal source FK SET NULL，Signal 变 missing，不扩大 application scope；
- missing Signal 不可进入新 Practice/Preparation/Context；
- Signal retract 追加 Version，不删除历史；
- Product Action terminal 清除 route_payload_json；
- Adaptive/Preparation/Story 保持各自冻结历史；
- 显式 privacy erase 与历史 Proposal snapshot 清理不在本期，必须单独设计，不能伪称普通 Note delete 已彻底清除派生内容。

## 16. 失败与外部错误

稳定 code：

~~~text
404 review_readiness_not_found
409 review_readiness_source_changed
409 review_readiness_already_exists
409 review_readiness_action_in_progress
409 product_action_idempotency_conflict
409 product_action_request_conflict
409 product_action_revision_conflict
409 product_action_stale
409 product_action_story_write_conflict
422 review_readiness_invalid_candidate
422 product_action_invalid_request
422 product_action_input_too_large
503 review_readiness_unavailable
503 operation_result_unknown
~~~

跨 scope 与不存在使用同一安全 404。错误不含实体存在性、原始异常、文本或 token。

读取失败不返回普通空数组。UI 保留 owner/draft，重试只重做只读查询。

## 17. 模块与依赖边界

推荐新增：

~~~text
src/offerpilot/product_actions/
  contracts.py
  catalog.py
  issuer.py
  repository.py
  coordinator.py
  compensation.py

src/offerpilot/review_readiness/
  contracts.py
  candidates.py
  repository.py
  projection.py
  preparation_selection.py
  contributor.py

web/src/features/reviewReadiness/
  contracts.ts
  service.ts
  ReviewReadinessNextStep.tsx
  ReadinessFeedbackAdvisory.tsx
  ProductActionConfirmation.tsx
~~~

依赖：

~~~text
domain repositories
  ← ProductAction executors
  ← read projectors / preparation selection

ProductActionCatalog
  → sealed executors and compensation bindings
  ↛ ToolCatalog / LegacyCatalog / Provider

Core Task UI
  → service + canonical owner
  ↛ repository / catalog / provider
~~~

机械删除/所有权门禁：

- Provider builder 仍精确 25；
- Legacy 仍精确 3；
- Agent Compensation 仍精确 4；
- Product Action 2、Product compensation 2 精确；
- Product Action name 不出现在 Provider Schema/selector/dispatcher/Legacy；
- Story confirm API 不再调用旧 self-committing confirm_attempt；
- ProductAction executor 只能调用 Session-bound repository；
- Product Action 不写 Chat/Pending/Message/AgentRun/Journal；
- HTTP route manifest 精确包含两个 owner-scoped Product Action Undo endpoint；禁止 `/api/write-operations/{id}/undo` 与 `/api/stories/...` alias，既有 `/api/chat/undo-last-write` 不变；
- Review UI 不直接 POST domain CRUD；
- list_recommendations 不扫描未确认 Proposal；
- exact focus/target missing 不回退；
- Note 所有更新路径经过 revision helper；
- Preparation V1 不导入 V2 builder；
- normal Chat 不查询 Signal；
- 不存在 feature flag、shadow、双写或旧 executor fallback。

## 18. 测试矩阵

### 18.1 Source/Proposal/Candidate

- Note revision：create/update/bulk/Agent/API/delete、updated_at；
- Proposal V1/NULL 不合格；V2 exact revision；
- Provider 前后 Note 修改，V2 ready CAS；
- completed/todo/in_progress/cancelled/unknown Event；
- future done/past todo 只按 status；
- source current/changed/missing；
- safe-empty、无 ref、未知 path、重复 focus、超限；
- candidate projection 所有副作用 0。

### 18.2 Product Action seal/issuer

- 25/3/4 + 2/2 exact manifest；
- Provider Tool canonical golden 全字节不变；
- Product Action 同名 Provider ToolCall unknown_tool/executor 0；
- source-bound issuer、连续 token identity、跨 request/container/source/ABA；
- server token 在 BEGIN 前生成、proposed INSERT 使用同 fingerprint、commit 后原样返回；
- GET/restart 按保存的 key profile 重建同 token，missing key fail-closed，禁止偷换 active key；
- route payload/semantic claim/binding/proposal/request idempotency 五类 HMAC 的跨进程 canonical golden、tagged null、中文、generation 差异；
- client action_name 无法路由；
- generic GET 永不返回 proposed token；SignalOwner/StoryOwner/RejectionOnly 三类 sealed proof exact，ordinary DTO、跨 union/owner/source/action/generation、重复消费和 ABA 拒绝；
- Signal proof 没有 generation 字段；source changed/missing 后 action-specific route-only rejection recovery 固定 `live_source_state=not_observed`，source existence/currentness Repository 0、approve/modify executor 0，proof 不虚构 present/absent；
- ProductActionProposal route cap、UUID/integer/HMAC/action-source/active-terminal checks；
- child-without-parent DB trigger 拒绝；parent-without-route 是已声明 SQLite deferred-check 边界，但封闭 publication UoW/AST 禁止生产提交，fresh reconciliation/decision fail-closed；route identity immutable、parent-terminal trigger 同 statement clear、禁止 delete；
- baseline `/api/chat/undo-last-write` manifest/response保持，product_action因conversation/last-write owner不匹配而executor 0；不存在operation-id-only generic Undo route；Signal/Story owner-scoped routes签发 sealed UndoProof，跨 action/scope/owner/container、ordinary DTO、重复/ABA proof拒绝；
- no Conversation/Pending/Message/AgentRun/Journal。

### 18.3 HITL/Ledger

- propose same key/same input 并发 20 次一个 Operation；
- primary/compensation transition exact prefix：proposed seq1；approve/modify seq2 approved+seq3 claimed+seq4 committed|failed；reject seq2 rejected；同事务rollback、immutable、缺失/额外/错误prefix fail-closed；
- same key/different input conflict；
- same focus/different keys 并发只有一个 active semantic claim：loser 不论 route/user_note 是否相同都稳定 409，不能取得 winner Operation/token，loser Operation/alias=0；
- loser 首次 409 响应丢失后，在 winner active/rejected/definite-failed/committed 各状态用同 key 重试：active 仍 409；committed 为 already_confirmed；rejected/definite-failed 因旧 409 从未被接纳而可首次创建新 Operation；
- approve/modify/reject exact truth table；
- reject source repo/preflight/executor 0；
- claim 前 stale executor 0；
- executor exception 恰好一次；
- absent/proposed/terminal/unreadable commit-unknown；
- proposal/decision/compensation fresh reconciliation均联合验证parent+route/domain+ordered transitions；transition-only、parent无seq1、terminal缺seq2/3/4、额外seq均不修补且executor 0；
- proposal publication 与 decision commit-unknown 的 absent 语义严格分离；
- terminal replay Provider/source repo/executor 0；
- terminal digest/result/transport cap；
- Signal/Story/reject/failed terminal codec、完整 digest、action-local cap 和 renderer golden；
- 两个 Product Action reject 的 fixed visible/transport/HTTP 200、无 feedback/正文；
- pre-executor failure 保持 proposed；mapped domain failure terminal；infra/projector/internal rollback proposed；
- product action delivery not_applicable exact shape；
- Chat delivery/recovery/heartbeat 查询永不取得 product_action row；
- cancellation/BaseException。

### 18.4 Signal

- semantic duplicate；
- Note/Event/Proposal race；
- parent DELETE 的 FK SET NULL 成功并变 missing、不扩大 scope；
- active→retracted Version composite self-FK deferred chain：跨 Signal parent拒绝；Application hard delete aggregate cascade成功；Signal存活时单独删除 parent或leaf Version均拒绝；foreign_key_check=0；
- source FK 改为其他 ID、NULL→非 NULL 均被 trigger 拒绝；原始 SQL non-null→NULL 的已声明 SQLite 边界只会得到不可消费的 missing，生产 Repository/AST 不存在该写入口；
- Signal/Version/Evidence/Ledger same commit/rollback；
- active→retracted Undo；
- retracted Version 全 NOT NULL 字段、deterministic domain key、Evidence byte-copy、compensation operation binding；
- Undo stale/replay/unknown；
- source Note/Event/Proposal missing 后 Signal owner-scoped Undo 仍按 Signal.application_id 成功；跨 Application仅持有operation_id不可执行；
- statement 不可编辑，user_note 边界；
- practiced/self_assessment 不改变 disposition/ready。

### 18.5 Story

- new ready Attempt 原子带 Product Action proposal；
- response loss GET：active proposed 返回同 action/token；并发 approved/rejected/declared-failed 后只返回 safe terminal 且 token absent；不调用 Provider；
- Story direct COMMIT=201/created true；commit-unknown fresh reconciliation及 replay=200/created false；
- historical confirmed replay；
- historical ready confirm bridge：同 legacy token+payload 收敛，不同 token/payload conflict，伪造 proof 拒绝；
- Story ready publication COMMIT unknown：Attempt+parent+route+transition exact bundle、pre-ready+all absent、restart无Frozen result、四对象partial corruption、unreadable；
- Story next-generation proposal COMMIT unknown：old terminal+prefix+pointer N/N+1 all absent、exact N+1 proposed+seq1、exact N+1 terminal prefix、later pointer 的 current direct bundle/prefix exact 才 historical replay、current pointer/parent/route/transition partial fail-closed、unreadable；
- story_product_action_proposal_response_v1 canonical golden：direct 201/proposal_created true、fresh proposed 200/false、三类 terminal 200/token absent、later-pointer replay；与 Story write `{story_id,version_id,created}` codec 不混用；
- publication response-loss 后 concurrent decision：ready+proposed/rejected/failed 与 confirmed+committed 矩阵；
- source_changed invalidated+proposed/rejection_only recovery、approve/modify executor 0、reject 成功；
- 仅 reject 后 source-bound proposal POST 才能触发 next product_action_generation；并发/响应丢失 CAS 收敛、旧 generation replay、UUID/token 全部变化，旧 token 不能授权新 generation；failed/stale target 必须新建 Story Proposal Attempt且该 POST executor=0；
- new/append Story Session-bound same transaction；
- source/target CAS、edited content/evidence validator；
- reject no Story preflight；
- client-generated authorization token production path removed；
- new Story undo archive；
- append undo restore pointer/title；
- later edit makes undo stale；
- Story owner-scoped Undo proof 绑定 story/attempt lineage；跨 Story、普通 DTO、owner 切换、响应丢失重签 proof后 terminal replay executor 0；
- manual create/version/archive/restore 不被误称 Ledger 化。

### 18.6 Adaptive Practice

- exact Signal Version + target Event ready/in-progress/completed；
- practice_source_fingerprint_v1 / practice_target_fingerprint_v1 跨进程 canonical golden；1/5 Evidence、乱序查询仍按ordinal、缺0/跳号拒绝、任一 Evidence hash变化 source_changed；
- V2 Plan 单列冻结 snapshot 永远复制 ordinal=0，其他 Evidence 只参与 fingerprint；expected source/target fingerprint、same key不同 fingerprint conflict与响应丢失 replay；
- target_fingerprint migration/CHECK/immutable：legacy NULL、V2 required；source/target FK SET NULL 后两 fingerprint保持；
- start idempotency-first：same key/same stored input 在 source/target changed/missing/retracted/completed 后仍 replay frozen Plan且live query=0；same key/different body conflict；新 key才执行live校验；
- source/target changed/missing/retracted；
- same Signal different target Event；
- same pair concurrency；
- V1 existing replay/complete；
- 0029 确实移除旧普通 proposal/focus UNIQUE，legacy/V2 partial unique 与 origin truth table；
- V1 new create 410；
- V1 不回填 confirmed；
- V2 source deleted 后冻结 Plan 仍可查看/完成；
- V2 source-only/target-only/both delete 的 CHECK/FK 真值表与新建 partial shape 拒绝；
- no fallback；
- draft isolation。

### 18.7 Preparation V2

- 字段 absent 的 V1 canonical golden 字节等价；
- explicit empty V2；
- raw presence bit、null/类型/重复拒绝、absent vs empty 同 key conflict、unknown/replay 保留 presence；
- 0/1/8/9、duplicate/order/cross-app/wrong-target；
- direct API 与 UI 对 target 使用同一权威规则：same Application、interview、scheduled/in_progress；completed/cancelled/unknown/非法 type、source==target 全部 Provider 0；
- Provider 执行期间 target lifecycle 翻转、source lifecycle 不再 completed 或 source/target 关系变化时 invalidated、late result discard；
- stale/retracted/missing/soft-deleted；
- one-snapshot Selection Loader；
- provider payload/output evidence；
- 64 KiB 完整 canonical envelope max-legal/超 1 byte fixture；
- fallback frozen input；
- lease/source changed/late result；
- original key/different selection conflict；
- V1/V2 history coexist。

### 18.8 Context Projector

- Phase 4 三个 Contributor，尤其 confirmed_memory，在所有生产 Agent path 恒为 disabled、Signal query 0；
- future port validator/golden 为纯合成不可构造资产，production composition/Projector/Runner AST 与 call graph 不可达，普通 ready DTO 不能启用；
- PreparationReadinessSelectionLoader 只在 Preparation owner 一次快照加载，普通 Chat/Application Chat/Haru 不可达；
- selected invalid 时 Preparation Provider 0；
- 不修改 SurfaceManifestV2/Journal Manifest，不产生 Context proof/body/checkpoint/Chat/Pending/Ledger/log。

### 18.9 UI/Core Task

- Review owner 一个 primary、Story secondary；
- Signal/Story confirmation 各自 canonical owner；
- 不创建 Chat Pending/第二确认 owner；
- duplicate click focus 同 Product Action；
- draft/result_unknown/replacement guard；
- Review→Practice/Preparation exact target；
- Haru/Pilot 只 opener；
- Event chooser 不猜最近/唯一；
- loading/error/empty/stale/unknown；
- keyboard/focus/theme/reduced motion/四档桌面；
- 无重复 API/SSE/Provider。

### 18.10 Migration/隐私

- 0028→0029、空库、重复启动、中断；
- historical terminal bytes/digest；
- FK/index/trigger/check exact；
- Product compensation 固定 UUID/request/input fingerprint/parent mapping、双连接单 winner；
- Compensation proposal/execution commit-unknown 四态、terminal replay、infra rollback、Agent fallback 0；
- max legal ProductAction route 16KiB、16KiB+1；
- Signal/Preparation caps、中文/emoji、重复键、非有限数；
- Note/Event delete→source missing；
- Application soft/hard delete；
- canary 不出现在日志/Journal/Manifest/error/session storage/report。

## 19. 发布门禁与完成定义

只有全部满足才可宣称完成：

- 设计与测试先行实施计划分别复审通过；
- 0029、Proposal V2、Product Action 2/2、Signal、Adaptive V2、Preparation V2 和 contributor 边界完成；
- Provider 25、Legacy 3、Agent Compensation 4 全部不变；
- 每个长期 Signal 可追溯到 exact Note revision/V2 Proposal/focus/Evidence；
- 未确认 Proposal、legacy Plan、自评不能成为准备事实；
- Story Proposal confirm 与 Signal save 均经过 Product Action HITL/Ledger/required Undo；
- Product Action 不污染 Chat/Pending/AgentRun/Journal；
- 下一次准备只使用 exact target Event 显式选择；
- 基础 readiness、Chat HTTP/SSE、Agent HITL、Provider/Tool 副作用未退化；
- V1/V2 replay/unknown/undo/concurrency/privacy gates；
- backend manifest union/duplicate node/skip/aggregate；
- Ruff、Mypy、frontend full、build、static smoke、local verify；
- controlled real-AI 只验证既有 Review/Story/Preparation Provider 调用次数；Product Action Provider=0；
- browser：保存 Note、Proposal V2、Signal approve/modify/reject/undo、Story approve/modify/reject/undo、Practice、Preparation、source changed、unknown recovery；
- independent CR 无未关闭 P0/P1/P2；
- baseline/allowlist/untracked/diff-check/worktree clean；
- 验收报告明确 25/3/4+2/2、Story confirm 内部破坏性切换、V1兼容、无自动 Memory/Knowledge 写入，以及不声明全局 exactly-once。

外部 release orchestrator、Docker 或环境 gate 不可用时如实排除，不伪造输入。

## 20. 设计后的顺序

书面复审通过后才编写测试先行实施计划。建议责任顺序：

~~~text
baseline golden + 0029 RED
→ Note revision + Proposal V2
→ Product Action catalog/proposal/Ledger shape
→ Story confirmation cutover + Story Undo
→ Signal domain + Signal Undo
→ Candidate/advisory projection
→ Adaptive Practice V2
→ Preparation Input V2
→ future contributor type asset + confirmed_memory disabled gates
→ Core Task UI
→ old-path deletion + full release verification
~~~

本文已完成书面复审并获用户批准进入实施；任何偏离已批准边界的重大变更必须先停止并重新复审。
