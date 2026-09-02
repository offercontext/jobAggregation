# Review-to-Readiness Feedback Loop 验收报告

状态：实现代码已完成全量门禁、真实浏览器走查和独立规格/质量复审；最终复审 `PASS`，无开放 P0 / P1 / P2。分支未 push、未 merge。

## 基线与范围

- 固定 baseline：`c5a020cbedd8ff64f6188f51c10d8f4daa7c7dff`
- 实现与最终 Code Review HEAD：`97db2c332acc3ca15ec7b67b4bb34f5de8763868`
- Branch：`feat/20260830-review-readiness-feedback-loop`
- Worktree：`D:\Users\yuqi.chen\offerpilot\.worktrees\feat-20260830-review-readiness-feedback-loop`
- 实现范围：Interview Note revision、Interview Review Proposal V2、Readiness candidate/Evidence/Signal、独立 Product Action 2/2、Story confirm cutover、required Undo、Adaptive Practice V2、Interview Preparation Input V2、四个 canonical Core Task owner，以及 `0029_review_to_readiness_feedback` migration。
- 同一分支补充修复 Haru Live2D 表情状态泄漏，并加固 Vitest 子进程诊断和 Interview Story 浏览器门禁的 Chromium 启动恢复。

## 改了什么

### 复盘来源、候选与 Readiness Signal

- Interview Note 更新现在原子递增 revision；Review Proposal V2 冻结 Note/Event/source identity，候选投影只接受 exact、current、可审计来源。
- 新增 immutable Signal Version 与 1–5 条连续 ordinal Evidence；statement 不可编辑，用户确认时可补充 user note。Undo 不删除历史，而是创建新的 retracted Version。
- source Note/Event/Proposal 删除后保留历史并投影为 `missing`；changed/missing/retracted Signal 不再进入新的 Practice 或 Preparation。
- 保存的是“用户确认的准备重点”，不是能力、弱点、招聘结果或长期记忆事实；没有写入 Summary、Memory 或 Knowledge。

### 独立 Product Action 安全边界

- 保持 Agent Runtime 分类精确为 `25 Typed / 3 Legacy deterministic / 4 Agent Compensation`，新增独立的 `2 Product Actions / 2 Product Action Compensations`，即 `25 / 3 / 4 + 2 / 2`。
- Product Action 固定为 `confirm_interview_story`、`save_review_readiness_signal`；补偿固定为 `undo:confirm_interview_story`、`undo:save_review_readiness_signal`。
- Product Action 使用独立 Catalog、Issuer、Repository、Coordinator 和 owner-scoped proof，但复用 Write Operation Ledger 的 canonical transition、HMAC、terminal payload 与事务 primitives。
- Product Action 不进入 Provider schema、Typed/Legacy catalog、Agent compensation registry、selector、dispatcher、Chat Pending、Chat/Tool message、AgentRun/Segment 或 Journal tool events。Provider 返回同名 ToolCall 时继续 fail closed，executor 为 0；terminal delivery 固定为 `not_applicable`。
- approve/modify、reject、recovery、commit-unknown 和 compensation 均验证完整 ordered prefix；缺失、额外、跳号或错误 transition 作为 integrity failure 封闭处理。

### Story、Practice 与 Preparation 闭环

- Story Proposal ready 时原子发布 Story Attempt、WriteOperation、private ProductAction route 和 `seq=1 proposed`；既有 `POST /api/interview-story-proposals/{attempt_id}/confirm` 保留，但内部已切换到 ProductActionCoordinator。
- rejected generation 可通过显式 endpoint 创建 N+1；旧 token 不能授权新 generation。历史 ready/confirmed replay 兼容保留，terminal replay 不返回 confirmation token。
- Story `operation_result_unknown` 在前端保留同一 Attempt、冻结请求和 idempotency key；canonical `product_action_story_write_conflict` 会保留用户编辑内容/陈述并要求 fresh Attempt。
- Adaptive Practice V2 绑定 exact `Readiness Signal Version + Target Application Event`，持久化 source/target fingerprints；同 key replay frozen Plan 时 live source/target query 为 0，新建才检查 current source 与 target eligibility。
- V1 新练习创建返回 410；V1 历史 Plan 仍可读取、replay 与 complete。未确认 Proposal 不再产生新 recommendation。
- Interview Preparation 新增显式 `readiness_feedback_version_ids`：字段缺失严格走 byte-equivalent V1，存在 `[]` 表示明确 V2 空选择，允许 0–8 条 exact Version；selection 与 Event/JD/Resume 共用 Session-bound snapshot，完整 canonical envelope 受 64 KiB 总预算约束。
- Practice 503/transport unknown 会保留冻结 start/completion body、回答、自评与原 key；只有当前后端定义的确定性 4xx 会清理 draft。

### Canonical owner、Context 与 Haru

- Review、Story、Practice、Preparation 都接入现有 Core Task Surface 的唯一 owner；close/reopen、unknown recovery、owner generation 和跨 Application/Event identity 均 fail closed。
- `confirmed_memory`、`knowledge_context`、`older_conversation_summary` 三个生产 contributor 继续 disabled；普通 Chat、Application Chat 与 Haru 对 Signal query 为 0。新增 future-only type asset 只参与离线 validator/golden，不注册到生产 composition。
- Haru 新增显式 `neutral` Expression。初始 idle 必定应用中性状态；idle、thinking、preparing_voice、transcribing、reviewing_voice 都会显式复位。
- speaking/success 仍可短暂使用 `f06`，1 秒后回到 neutral；状态切换和 dispose 会取消旧 timer。model3 中 `f06` 实际映射到 `F07.exp3.json`，因此复位整个 Expression 会让 ParamTere、眉毛、眼睛、嘴形等参数一起淡出/重置，而不是只改脸红参数。
- listening、waiting_confirmation、error 等既有映射保持不变；reduced-motion 和 `animationLevel='off'` 仍不会触发动态 Expression/Motion。

## 0029 与破坏性变化

- `0029_review_to_readiness_feedback` 对相关内部表执行受约束 rebuild，并新增 `product_action_proposals`、Signal/Version/Evidence 以及 Practice/Story/Note 增量字段、索引、触发器和 migration marker。空库、真实 0028 升级、外键、级联和历史 canonical bytes 均有迁移测试。
- Signal Version 的 self-FK 使用 `(parent_version_id, signal_id) -> (id, signal_id)`、`ON DELETE NO ACTION`、`DEFERRABLE INITIALLY DEFERRED`；跨 Signal parent 由数据库拒绝，Application hard delete 可级联完整 Version 链，Signal 存活时不能单独删除 Version。
- 新创建 Review/Practice/Preparation 采用 V2 契约；不保留 shadow、feature flag 或运行时 fallback。V1 只在明确列出的历史读取/replay/complete 与 Preparation 字段缺失路径继续兼容。
- Story confirm 的公开 URL 保留，但 self-committing 内部路径已删除；调用方若依赖旧内部 repository 行为需要迁移到 Product Action。
- 本地开发数据库会执行一次性内部迁移；没有提供自动 downgrade。Haru 变更不涉及用户数据迁移。

## Commit-unknown 与 Undo 矩阵

| 能力 | 结果未知时保留 | 恢复规则 | Undo |
|---|---|---|---|
| Story ready publication | Attempt、generation、frozen proposal input、原 idempotency key | 原 key/Attempt 重载；Provider 与 executor 不重复；unreadable bundle fail closed | `POST /api/interview-stories/{story_id}/product-action-undo`，仅接受 parent operation id |
| Story decision | Product Action operation、server token identity、edited payload | terminal state/rejection control 恢复；CAS failed 要求 fresh Attempt | request-local、action-specific、单次消费 UndoProof；owner/aggregate/digest exact |
| Signal decision | source-bound candidate、operation、edited payload | winner 原 key 才能恢复；不同 key 命中 active semantic claim 返回 409，且不泄露 winner/token、不创建 alias | `POST /api/applications/{application_id}/readiness-signals/{signal_id}/undo`，创建 retracted Version |
| Practice start/complete | exact Signal/Event、fingerprints、回答/复盘/自评、原 key | 同 key同输入 replay frozen Plan/terminal；503 与 transport unknown 保留 draft | 不新增 operation-id-only 通用 Undo |

两个 Product Action compensation 继续使用 `proposed -> approved -> claimed -> committed|failed` 的完整 Ledger transition。proof 在异常、取消和结束后 revoke；响应丢失后重新加载 canonical owner、重新签发 proof，并按 deterministic compensation operation replay，executor 不重跑。项目不声称全局 exactly-once。

## 自动化验证

| 命令 | 结果 |
|---|---|
| Backend focused contract suite | 824 passed，937 warnings，2181.40s |
| Frontend focused（计划中的 18 files） | 18 files / 390 tests 全通过，53.49s；生产审计 20036ms < 30000ms |
| Story/Adaptive unknown recovery targeted | 4 files / 57 tests 全通过，27.19s |
| Haru Live2D targeted | 2 files / 46 tests 全通过，10.09s |
| `uv run pytest --deselect tests/test_application_jd_browser_harness.py::test_application_jd_implementation_scope_is_machine_checked` | collected 5902 / selected 5901；5897 passed、4 skipped、1 deselected、0 failed，5203 warnings，16597.72s（4:36:37） |
| `cd web && npm test -- --run` | 217 files / 2114 tests 全通过，643.93s；生产审计 26383ms < 30000ms |
| `cd web && npm run build` | 通过，3974 modules，21.69s |
| `uv run ruff check .` | 通过 |
| `uv run mypy src` | 162 source files 通过 |
| `uv run oc smoke --static-dir web/dist` | 8 项检查通过 |
| `uv run oc verify --profile local --static-dir web/dist` | 16 项检查及 cleanup 通过 |
| `git diff --check` | 通过 |

全量 backend warning 主要为 Starlette/httpx、FastAPI `on_event` deprecation，另有 2 条 Pillow DecompressionBombWarning。Vitest 保留仓库既有 React `act()`、DOM prop 与 jsdom warning；没有测试失败。

一次 post-fix 前端全量首跑在机器刚结束长时 backend gate 后出现 `spawnSync git ENOENT`，同时 wall-clock 审计为 30251ms（超阈值 251ms）。`git` 可执行文件与两个失败节点随后单独验证通过（142 tests，审计 12943ms），在无相关遗留进程的 clean worktree 上重跑完整命令得到上述 217/217、2114/2114 结果；未通过放宽阈值、重试脚本或伪造报告绕过门禁。

## 真实浏览器验收

使用内置 Codex Browser、隔离本地数据和 production-compatible 页面完成：

- Signal approve、modify、reject、unknown/replay、undo；Story approve、modify、reject、N+1、unknown、undo；source deletion/missing、source drift，以及 exact Preparation -> Practice handoff/reconcile。
- Preparation V2 0 / 1 / 8 条选择；Practice exact Signal/Event target；Product Action 请求记录中 Provider 调用为 0，未观察到重复 HTTP/SSE/Provider/executor 调用。
- 768 / 1024 / 1280 / 1440 宽度、light/dark、键盘 focus/label 均完成走查。1024/1280 下既有 Haru overlay 可能遮到下方“查看详情”，但 primary CTA 仍可达；记为 P3。
- Haru 初始 idle 为 neutral；success 显示短暂 `f06` 后即使状态仍为 success，1.6 秒时也已恢复 neutral；success -> idle、speaking -> thinking/idle 不残留脸红。
- success -> error/listening 分别切换到既有目标 Expression；preparing_voice、transcribing、reviewing_voice 均恢复 neutral。
- `animationLevel='off'` 下 idle/success 画面一致且不调用动态反馈；通过 CDP 模拟 `prefers-reduced-motion: reduce` 后 matchMedia 为 true，idle/success 仍一致。媒体设置、临时 QA 页面、浏览器 tab、Vite server 和端口均已清理。

## 独立 Code Review

- 最终全量 reviewer 首轮覆盖 `c5a020c..cb83641`，发现 1 个 P1、2 个 P2：Story coded `operation_result_unknown` 被误判为确定失败、Story CAS terminal 使用错误 wire 字段/值、Adaptive 503 被当作确定失败并清除冻结 draft。
- 修复提交 `97db2c3` 增加对应 RED/GREEN 回归，保留 Story/Practice 的 exact identity 与用户输入，并修正 canonical terminal 匹配。
- 同一 GPT-5.6 Sol xhigh reviewer 复审 `97db2c3` 后给出最终 `PASS`：P0 = 0、P1 = 0、P2 = 0；复审定向 3 files / 50 tests 通过，worktree clean。
- Haru 中性复位和 Chromium/Vitest gate hardening 也纳入最终审查，未发现开放 P0–P2。

## 外部门禁与剩余风险

- Application-JD machine-check 节点依赖 release orchestrator 提供历史 baseline/allowlist。本次精确 deselect 该单一节点；没有用当前分支自生成输入或伪造结果。
- Docker client 29.6.1 可用，但 Docker Desktop Linux daemon named pipe 不存在，因此未运行 Docker smoke。
- 未检测到明确受支持的 real-AI credential/cost 授权，故没有运行 real-AI profile；没有用假 Provider 输入冒充真实 AI 验收。
- Vite 主 chunk 为 1645.75 kB（gzip 515.74 kB），超过 1500 kB warning 阈值；属于既有 bundle 技术债。
- Review readiness wall-clock 审计和少数 Git 子进程 gate 对 Windows 主机资源较敏感；门禁仍保持固定阈值，Vitest group/Chromium harness 已增加失败子进程、deadline、cleanup 和多 attempt 诊断。
- Adaptive 当前按“带机器码的 4xx”为确定性失败；现有后端只返回确定性的 404/409/410/422。若未来引入可重试的 408/425/429，前端需同步改为显式 code allowlist。
- 项目提供 Ledger/aggregate 作用域内的幂等、commit-unknown 与 compensation recovery，不声称跨所有外部系统的全局 exactly-once。

## 集成状态

- 分支未 push、未 merge。
- 代码审查 HEAD 为 `97db2c332acc3ca15ec7b67b4bb34f5de8763868`；本报告与已完成计划将在独立文档提交中落盘。
- 最终 repository hygiene 要求：报告提交后 `git diff --check`、tracked/untracked scope、HEAD 与 worktree clean 再次回读确认。
