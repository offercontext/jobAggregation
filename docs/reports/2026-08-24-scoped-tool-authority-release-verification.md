# Scoped Capability & Binding Enforcement 发布验收报告

日期：2026-08-25
分支：`feat/20260824-scoped-tool-authority`
固定实施 baseline：`1574d0e891391c817c325f598b4f22f8a7838330`
Agent Loop Unification baseline：`2427fa642c06babbd6680cf2d4363314169b071a`
设计初稿提交：`2d5538af3bed21a7d9d9b1d415adbf06b3393e69`
批准设计复审提交：`419e70e37deb8d16cd52dd7ff221e142c1394982`
实施计划提交：`aa8c422a1615c6af8ed7586b2e34fa9257c9f224`
最终被验证源码提交：`a3e1ecdc5c754233b1a02c25b62acb613f53cef5`
最终生产代码提交：`fd275736b6494cea22198961bcb179c2b639f809`

## 范围与结论

本项目把模型可见工具面与执行授权分离，并为模型可见的 25 个 Typed Tool 固化 Capability/Binding 矩阵。New Turn 由 `SegmentExecutionAuthority` 约束；approve/modify continuation 由一次性的 `ApprovalExecutionAuthority` 约束。Application scope 的集合查询、单实体读取和写入最终都由 scoped SQL、可信 Conversation Scope revision、Binding 与一次性 claim 共同 fail-closed。

内部切换是破坏性的：Typed Pending 不再接受无 claim 的通用持久化入口，Typed Primary WriteOperation 不再接受空授权指纹，执行仓库不再接受未密封的 scope binding。没有 feature flag、shadow path、旧 Typed fallback 或根据 Pending/请求反推 Authority 的兼容路径。

公开 Provider Tool Schema、HTTP/SSE、HITL、Ledger、Undo 和业务事务形状保持兼容。Reject 仍不解析参数、不读取目标实体；terminal replay、delivery recovery、补偿事务和三个 Legacy deterministic 工具保留既有边界。

## Characterization → RED → GREEN 证据

- 在生产代码仍等于固定 characterization baseline 时固化 Provider envelope/schema、HTTP/SSE、HITL、Ledger、scope、serialization/privacy 与 25/3 边界 golden；golden 没有生成器、覆盖命令或 update 开关。
- 每个实施任务先落失败测试，再切生产路径。RED 覆盖密封对象伪造、cross-factory/cross-segment identity、Surface/Gateway provenance、Pending/Prepared mutation、作用域 SQL 越权、Conversation revision CAS、Typed Pending 缺 claim、approve/reject 事务竞态和 serialization/privacy 泄漏。
- 独立阶段 CR 发现的 P0/P1/P2 均先以最小负测复现，再修复并跑 focused green，包括：
  - Provider Surface/Binding/Gateway 与 model-call/build provenance；
  - Pending、Prepared、claim/proof 的源对象突变与 identity-only cleanup；
  - ToolSpec resolver callable、BindingContract 与 Provider contract 深冻结；
  - 伪造 repository binding、AuthorityFactory 子类绕过、冷启动循环导入和 exact positive-int64；
  - Typed Pending exclusive persistence、授权 HMAC、Ledger lock 内重校验与一次性 execution claim；
  - reject 的无参数 preheader、approval source/load 顺序和 Approved continuation 的 Segment 重建；
  - AST deletion/privacy/serialization gate 的别名、闭包、分派和动态类型逃逸。

Task 19 首轮 focused matrix 暴露两个早于 Typed Pending exclusive port 的旧测试 fixture；生产门禁正确拒绝 `claim=None`。fixture 改为直接种植升级前历史 Pending 后，两项 RED 节点及相关文件转绿，未放宽生产代码。

Task 20 独立复审继续以 RED → GREEN 关闭三项 P1 和一项 P2：Catalog 与 AuthorityFactory 现在都固定并复验 ToolSpec 的完整执行语义；mixed Typed/Legacy chained delivery 在 terminal delivery commit 前即拒绝；验收 fixture 的执行作用域在每个测试后关闭且 active registry 清零。修复提交为 `11bda3c`，fixture 提交为 `911fa24`。

最终 Windows backend gate 又暴露 PowerShell 默认大小写/culture 比较会把合法的参数化 node ID 误判为重复，并会让 skip allowlist 接受 case-mismatch。`bf6044b`/`fd27573` 以 Ordinal HashSet、SetEquals 和 Ordinal Dictionary 收口 duplicate、union 与 skip identity；8 个 gate tests 在 PowerShell 5.1 下通过，独立复审清零。

## 实现内容

- 新增密封且不可序列化的 Segment/Approval Authority、scope constraint、Prepared/Pending/Execution identity、一次性 claim/proof 和严格生命周期 registry。
- 固化 25 个 Typed Tool 的 Capability/Binding/Resolver/Provider contract 清单，启动时验证并深冻结运行时快照。
- Provider Frozen Surface 只暴露当前 authority 允许的工具；执行前仍由 Pipeline、Binding 与 scoped repository 独立授权。
- Application scope 的 Applications、Events、Notes、Offers 与 JD 查询/写入改为同一 Session 内的 scoped SQL；拒绝 bool/float/string/越界 ID，越权在 SQL/执行前 fail-closed。
- 新增 Conversation `scope_revision`、WriteOperation `authorization_scope_fingerprint` 与 `0028_scoped_tool_authority` additive migration、触发器和原子 scope mutation API。
- Typed Pending 只能经 `persist_typed_pending(..., PendingAuthorityClaim)` 落库；Pending、Primary Operation、消息和 delivery child 在同一事务内原子提交。三个 Legacy deterministic 工具使用隔离入口且授权指纹保持 NULL。
- New Turn、approve/modify、sync/stream 在 composition root 组装真实 Authority；批准续跑在 Ledger terminal/delivery fence 建立后激活全新的 Segment bundle，再进入统一 Agent Loop。
- Reject 使用 Ledger-first omitted-token proof，不解析 Pending args、不访问目标实体；Conversation 已删除时收敛为 `operation_unavailable`。
- 新增 source/deletion/privacy/serialization AST 门禁，禁止 generic Typed fallback、未 scoped repository、Authority/claim/source 泄漏及旧入口复活。

## 精确工具边界

25 个模型可见 Typed Tool：

1. `list_applications`
2. `get_application`
3. `create_application`
4. `update_application_status`
5. `list_application_events`
6. `get_application_event`
7. `create_application_event`
8. `update_application_event`
9. `delete_application_event`
10. `list_notes`
11. `add_note`
12. `update_note`
13. `delete_note`
14. `list_offers`
15. `get_offer`
16. `compare_offers`
17. `update_offer`
18. `save_offer_assessment`
19. `list_resumes`
20. `get_resume`
21. `resume_update_career_intent`
22. `resume_rewrite_highlight`
23. `list_resume_matches`
24. `list_jd_analyses`
25. `get_jd_analysis`

三个隔离的 Legacy deterministic Tool：

- `save_application_jd_version`
- `create_application_submission_snapshot`
- `record_application_outcome`

Legacy 不进入 Typed Pending Port，`authorization_scope_fingerprint` 保持 NULL；补偿事务不创建 Pending。

## 内部破坏性变化

- Chat Runtime 不再自动获得全部 Capability；新增 Tool 不会被旧入口隐式继承。
- Binding 不再只是审计信息；缺失、伪造、跨 scope、跨 Segment 或突变的 Binding/Authority 在 Provider、SQL 或 executor 前拒绝。
- Typed Prepared/Pending/Execution 值必须由当前 `AuthorityFactory` 的受控 Port 产生并保持源对象快照一致。
- 旧 `ToolExecutionContext(capabilities=..., current_bindings=...)` 构造契约已删除；New Turn 与 Approval 必须使用 authority-bound context、受控 Provider surface/invocation identity 和对应 Prepare identity。
- Typed Pending 缺少合法 `PendingAuthorityClaim` 时禁止写入；通用 `set/persist/replace` 路径只接受 exact 三个 Legacy adapter。
- Typed Primary Operation 的授权作用域指纹固定为 `hmac-sha256:<64 lowercase hex>`；新提议不可为 NULL，身份与指纹列插入后不可变。
- Conversation scope 只能通过原子 create/PATCH Port 建立或变更；scope 原始字段变化必须使 revision 精确加一，普通标题/置顶/归档更新不改变 revision。
- repository binding 只能由 exact `AuthorityFactory` 注册；伪造 dataclass、导入 seal、子类覆写或手工挂载都会在 SQL 前失败。
- chained delivery 只接受 Typed → Typed，或 `save_application_jd_version` Legacy → 同名 Legacy；mixed adapter 和另外两个 Legacy child 在 delivery commit 前拒绝。

## 外部兼容结果

- Provider-visible 25 个 Tool name、description、parameters 与 envelope golden 保持不变。
- Baseline golden 的 12 个节点固定 Provider envelope/schema、HTTP/SSE、HITL 与 Ledger 外部形状；最终完整后端门禁包含这些节点。
- New Turn、approve/modify/reject 的 sync/stream 形状和 HITL 卡片保持兼容；`auto_approve` 仍不能绕过 Typed 写确认。
- 同 scope read 可见，Application scope 的集合查询只返回当前 application；跨 application point read 返回 `permission denied`，SQL executor 不泄漏其他实体。
- standalone `add_note` 可以没有 `application_id`，但 Conversation 的 Application scope 与 revision 仍必须有效。
- Resume 在尚无持久 Conversation binding 时保持显式 `unbound`，没有伪造 workspace/application target。
- Reject、terminal replay、delivery recovery 与三个 Legacy deterministic path 保持 Provider/Driver 边界和既有结果语义。
- `scope_revision` 与 `authorization_scope_fingerprint` 只用于内部授权和数据库完整性，不进入公开 DTO、SSE、Prompt 或 Journal payload；privacy/serialization/source gates 保持通过。

## 0028 migration 与回滚边界

`0028_scoped_tool_authority` 为 additive、幂等 migration：

- `Conversation.mode` 的历史 NULL/空值先归一为 `general`，未知/无效历史值保留；既有行 `scope_revision=0`。
- 新 Conversation 只能以 revision 0 插入；scope raw field 任一变化必须 old revision + 1，不变则 revision 不变。
- `authorization_scope_fingerprint` 为 nullable，以兼容历史 terminal、Legacy 与 compensation；新 Typed primary/proposed 必须是严格 HMAC，且插入后不可变。
- 迁移前已有的 Typed proposed/NULL 可 reject，但不能 approve/commit/fail；Legacy 和 terminal replay 的非身份更新继续允许。
- 新二进制可运行于 0028；旧二进制不能在迁移后的数据库创建新的 Typed proposal，因为数据库触发器拒绝空授权指纹。

回滚不是二进制热切换：必须先停止并 drain 应用，再恢复 0028 前的数据库快照，之后才能启动旧二进制。仅回退代码而继续使用 0028 数据库不受支持。

## 验证结果

| 门禁 | 结果 |
| --- | --- |
| Focused authority matrix | 被验证源码 `911fa24`：首次 `2 failed, 1215 passed, 2 skipped`，均为旧历史 fixture；迁移 fixture 后 `1217 passed, 2 skipped, 105 warnings`，8:49 |
| Ledger/Chat/migration compatibility suite | 首轮旧 fixture 26 failed / 496 passed；被验证源码 `911fa24`：522 passed、2193 warnings、22:18 |
| 完整后端 supported suite | 被验证源码 `102fdd8`：4011 collected，4006 passed、4 approved skipped、1 external-gate deselected、4227 warnings，58:04，exit 0；`a3e1ecd` 仅稳定同一测试矩阵的 collection node ID |
| `uv run ruff check .` | 被验证源码 `a3e1ecd`：通过；`All checks passed!` |
| `uv run mypy src` | 被验证源码 `a3e1ecd`：通过；140 个 source files 无问题 |
| Windows backend manifest/group aggregate | `a3e1ecd` manifest/Aggregate：复用四个未受后续 test-only 改动影响的 `102fdd8` GREEN group，fresh 重跑 misc；4010/4010 Ordinal exact union，4006 passed、4 approved skipped、1 exact external-gate exclusion；exit 0 |
| Windows frontend manifest/group aggregate | `f076843` 工作树加后来提交于 `911fa24` 的唯一标题修正：181 files / 1286 tests；10 组及 Aggregate 全部 exit 0；node ID 唯一、union 精确、0 skip/todo |
| `npm test -- --run` | 被验证源码 `911fa24`：181 files、1286 tests 全部通过；677.75s |
| `npm run build` | 被验证源码 `911fa24`：3950 modules；`built in 45.69s`；通过 |
| `uv run oc smoke --static-dir web/dist` | 被验证源码 `911fa24` 的 build：8 steps；通过 |
| Docker smoke | 未运行；`docker version` 无法连接 `dockerDesktopLinuxEngine`，不声明 Docker smoke 通过 |
| `uv run oc verify --profile local --static-dir web/dist` | 复审前源码 `f076843`：16 steps；通过 |
| `uv run oc verify --profile real-ai --static-dir web/dist` | 复审前源码 `f076843`：19 steps；通过；真实 AI preparation/material/opportunity fit/interview review/knowledge capture/mock interview/chat write 全覆盖 |
| 隔离内置浏览器验收 | 复审前源码 `f076843`：通过，见下节 |
| 0028 migration/backup-reopen proof | 被验证源码 `911fa24`：`test_migration_0028.py + test_scope_mutation.py` 61 passed、53 warnings、1:07；操作性回滚边界见上节 |
| `git diff --check` / `git show --check` | 实现/测试源码 `a3e1ecd`：`1574d0e..a3e1ecd` 与 `a3e1ecd` 均通过 |

前端测试保留仓库既有 React `act(...)`、非布尔 DOM attribute 和 jsdom `getComputedStyle(..., pseudoElt)` warning；没有失败、skip、pending 或 todo。构建只有既有大 chunk warning（主 JS 约 1.54 MB；WASM 约 23.57 MB）。

裸跑 `uv run pytest` 在生产源码 `fd27573` 上得到 `4003 passed, 6 skipped, 2 failed`。其中 `test_application_jd_implementation_scope_is_machine_checked` 是历史 Application-JD 项目专属的外置 release-orchestrator scope gate：当前分支没有一组可合法复用的冻结 baseline/allowlist，按既有发布报告约定不得从当前 diff 合成，因此最终 supported suite 只精确 deselect 该一个节点，不声称它通过。另一失败是独占 Chromium 在 readiness 前退出的环境瞬态；同一节点随后单独重跑通过，与紧邻 Chromium 前序节点串行重跑也通过。`102fdd8` 把两个旧 raw `ToolExecutionContext` confirmation skip 迁移到真实 Approval Authority、Prepared 与 SQLite claim/race 路径；两个节点、整个 confirmation 文件和十轮确定性 race 均通过，独立 CR 清零。`a3e1ecd` 进一步把 Legacy frozenset 参数化改为 Ordinal 稳定 collection；不同 `PYTHONHASHSEED` 的全仓 4011 个 node 完全一致，目标矩阵 8 cases 通过。

Docker Desktop daemon 在验收环境中未运行，因此没有 Docker 容器启动/文件打包路径的现场证据。静态发布 smoke、local verify、real-AI verify 和 built-SPA 浏览器验收均已完成；Docker 环境差异保留为发布前基础设施风险。

Windows frontend gate 首轮发现 `applicationWorkspaceModel.test.ts` 两条 interview 参数化 case 的标题只插值 status，产生一个重复 node ID；断言本身全部通过。证据生成于 `f076843` 工作树，加上后来提交于 `911fa24` 的唯一前端测试标题修正；该修正只给标题补充 `completed interview` 参数。最终 manifest SHA256 为 `49260ee70126bcb235a329a7d695274a6d6c20e0b0086049eedad39aff271bf0`，source hash 为 `eae65b6f5017833185a73e41ca9bfac82dff57d45e2ce1ddc85d916c6dedf8d1`。`911fa24..a3e1ecd` 没有 `web/` 或 Vitest 分组脚本变化。

Windows backend gate 在 `a3e1ecd` 上用两个独立 `PYTHONHASHSEED` 收集 4011 个原始节点，Ordinal sequence/set 完全相同、duplicate 为 0，规范化 manifest SHA256 均为 `c10149e8d17cf94b335a0e9aa486ba5af8adf9110b1ac6f1000445222e88f27b`。仅精确排除上述 Application-JD 外置 gate 后，supported manifest 为 4010 个节点，SHA256 `e78796f19027e4d7d7c9289475602df68f00f4e03d88edb19e7bc8da2137d572`。`a3e1ecd` 相对 `102fdd8` 只改变 misc 组中同一 Legacy 参数矩阵的 node-ID 生成顺序；因此 agent 462、domain 131、knowledge 655 + 4 approved skip、proposals 434 复用 `102fdd8` 的最终 GREEN JUnit/collect artifacts，misc 2324 在 `a3e1ecd` 全新重跑，Aggregate 再对全部 artifact hash 与 `a3e1ecd` supported manifest 做 exact-union 校验。最终 Aggregate SHA256 `bcb7905ef5d954b9265bb6a1a110b5ad065e330e6a84cfa957de346b3d864388`，duplicate/missing/extra 均为 0。四个 skip 均是批准的知识库符号链接权限测试，无其他 skip。Application-JD 缺少外置 baseline 的失败证据同样来自 `102fdd8`；`a3e1ecd` 未修改该节点或其依赖。

agent 组首次与完整 suite 并发时有一条 active-budget timing case 瞬态降级；同一节点定向重跑 5.99 秒通过，资源竞争结束后 agent 整组 462 passed。一次因会话中断而失去父代理的 misc run 在超过正常时长后被明确终止，未计入证据；其隔离测试临时目录在确认无 reparse point、live reference、server、Chromium/CDP 或 listener 后清理。最终结果来自另一全新 evidence directory。

### 真实 Provider

`real-ai` 验收直接读取既有本地 Provider 配置，不打印、修改或复制 secret。19 个阶段均通过，包括 health/settings/SPA、Application/Resume/Event、Interview Preparation、Material Proposal、Opportunity Fit、Interview Review、Knowledge Capture、bounded Mock Interview、Chat Typed write confirmation 和最终清理。

### 内置浏览器与受控 API

使用隔离临时数据库、built SPA 和 deterministic model 完成：

1. built SPA 正常加载，首页、投递看板、投递详情、Haru 与完整 Pilot 工作区可交互；浏览器 console error 为 0。
2. workspace 集合查询返回两条合成 application；Application scope 集合只返回当前 `Scope Browser Primary`。
3. 同一 Application Conversation 请求另一条 `Scope Browser Other`，可见结果为 `permission denied`。
4. targetless `add_note` 在有效 Application scope 下产生 HITL 卡片；拒绝后显示“已取消这次操作”，没有写入。
5. approve 把合成投递从 `applied` 改为 `interview`；modify 把 Provider 提议的 `offer` 编辑为 `closed` 并写入 `closed_reason=browser modified`；两者均显示“保存成功”。
6. terminal replay 对同一 operation/token 第二次确认返回相同 operation，`replayed=true`。
7. SSE start 事件为 `meta → user_message_saved → status → tool_call → status → confirmation_required → completed`；approve continuation 为 `meta → status → tool_call → tool_result → assistant_message → completed`，最终 response 与 sync 同为 `message`。
8. workspace Resume 查询返回正常空列表，保持已知 explicit `unbound` 边界；global collection 可见两条合成 application。

隔离服务已停止；只包含合成数据的临时目录已移入 Windows 回收站，没有写入正常 OfferPilot 数据库。浏览器验收未传输真实用户数据或 secret。

## 独立 Code Review

Task 20 已对生产实现 `1574d0e..fd27573` 以及最终测试可信度提交 `102fdd8`/`a3e1ecd` 完成独立只读复审，覆盖 Capability/Binding 与 Provider provenance、所有密封身份和生命周期、scoped SQL、Pending/HMAC、approve/modify/reject、replay/recovery、HTTP/SSE、Source、scope mutation、0028 trigger、privacy/serialization/AST 门禁、Windows grouped gate 及测试可信度。当前没有未关闭的 P0/P1/P2。

复审发现并关闭：

- P1：ToolCatalog 未冻结全部执行语义；
- P1：Prepared 注册后 AuthorityFactory 未重验 ToolSpec 的 validator、renderer、exception map、WriteContract 等执行语义；
- P1：mixed Typed/Legacy chained delivery 可先提交、再在 replay 阶段失败；
- P1：Windows grouped gate 对 node ID 和 skip allowlist 使用非 Ordinal 比较；
- P2：acceptance fixture 的 AuthorityFactory 未关闭；
- P2：Windows grouped gate 缺少 case-only union mismatch 与 Unicode normalization node-ID 覆盖。

定向复审证据包括 Catalog/replay 53 passed、privacy/source 56 passed、serialization/deletion 27 passed、ToolSpec hardening 16 passed、write-operation acceptance 26 passed；`ruff` 与 `git diff --check` 通过。

## 剩余风险与明确非目标

- Resume 尚无持久 Conversation binding 时仍是显式 `unbound`；本项目没有猜测或新增持久绑定。
- standalone `add_note` 没有 target ID；Application scope 本身、revision、Capability/Binding 与 HMAC 仍必须有效。
- 本项目不声明跨请求、跨进程、跨 Provider 或跨外部系统 exactly-once。
- 本期不迁移三个 Legacy deterministic Tool，不实现 Tool Metadata Convergence、Memory/Knowledge/Summary contributor、SSE replay、多 Agent、插件系统或 wakeup queue。
- 前端构建保留既有大 chunk warning；不影响本期授权语义，后续可独立做 bundle 拆分。
- ToolCatalog 与 AuthorityFactory 当前分别维护同一套 ToolSpec execution snapshot/matcher；字段已对齐，但新增 ToolSpec metadata 时存在双实现漂移的 P3 维护风险。下一独立项目 `Tool Metadata Convergence` 应收敛为共享 leaf helper。
- local verify、real-AI verify 与 built-SPA 浏览器验收执行于 `f076843`，没有在最终生产提交 `fd27573` 上重跑；`f076843..fd27573` 对 `composition.py`、`catalog.py`、`write_operations.py` 的后续 fail-closed 加固由 focused、完整 supported suite 和 Windows backend matrix 覆盖，但最终生产源码缺少同提交级的 Provider/browser 集成重跑，保留为验证时点风险。

## 最终提交与状态

最终被验证的实现/测试源码为 `a3e1ecdc5c754233b1a02c25b62acb613f53cef5`。全部实现、测试与验收材料仅保留在本地分支 `feat/20260824-scoped-tool-authority`；分支无 upstream，未 push、未 merge。
