# OfferPilot Agent 工作指南

## 0. 指令与授权边界

- 遵守系统与开发者指令；用户当前任务和已有明确授权优先于本文件及技能中的流程建议。技能不能扩大任务范围或覆盖业务、安全约束。
- 在已授权范围内完成工作；常规、可逆的实现选择自行判断。仅当缺失信息实质影响结果且无法从上下文推断，或下一步超出现有授权时询问；等待期间继续独立工作。
- 发布、部署、向他人发送消息、修改远端文档及破坏性操作需在授权范围内执行。需要额外批准时，先完成可安全进行的准备，给出可审阅的结果；已有授权不重复询问。
- 若技能导致暂停、额外确认或偏离任务，指出实际读取的 `SKILL.md` 路径、相关原文和适用原因，区分硬性约束与流程建议。不要从建议推导新的审批门槛。
- 默认简洁中文，不发可选进度；用户请求或更高优先级指令要求时提供必要更新。回答先给结果，再给验证证据与限制。

## 1. 文档职责

- 本文件是跨 Agent 的仓库施工协议；产品事实见 §4，领域红线见 §6。
- 写改 Markdown 前读取 [文档规范](docs/architecture/documentation-rules.md)。按任务读取相关参考，不全量加载历史 spec、plan 或技能。
- `README.md` 面向用户。仅在用户要求，或公开安装、启动、许可证、命令行为确实变化时更新；它是公开承诺，不是内部验收表。

## 2. 开工与工具

- 改文件前运行 `git status --short --branch`。不覆盖、回滚、stash 或整理用户未提交改动；提交只包含本任务文件。
- feature work 新建隔离 worktree，默认基于最新相关上游分支，命名见 §3。文档维护不强制新建 worktree。
- 涉及产品口径时优先用 `lark-cli` 读取相关飞书 PRD / ADR / Check 表；涉及代码行为时先读对应本地文档、当前代码和附近测试。
- 对可能变化的状态检查真实仓库、文档或运行时。浏览器验收优先内置 Codex browser，仅在用户明确要求时使用 Chrome。
- Git 操作（含远程传输）直接用 Git CLI，不因 Git 加载 `web-access`。该技能仅用于实际网页搜索、访问或浏览器交互。

## 3. Git 规范

- 新分支：`<type>/<yyyymmdd>-<name>`；type 为 `feat/fix/docs/chore/refactor/test`，日期为本地日期，name 为小写短横线短语，建议 4–6 个词以内，不写 Agent 名。
- 本次任务有改动时，完成验证后做一次小步提交；纯问答或无变更不创建空提交。已有用户提交安排优先。
- 提交标题：`<type>: AI <中文描述>`。type 使用 conventional commits 的 `build/chore/ci/docs/feat/fix/perf/refactor/revert/style/test`。
- `git add` 与 `git commit` 分开执行，先检查暂存 diff；不得夹带用户改动。

## 4. 事实源

OfferPilot 的产品和架构事实源是飞书 wiki：

- 主 wiki：https://ycn8095q3nc7.feishu.cn/wiki/K6BQw1X5Piksm2kDex3cMQMenvf
- Root docx token：`Q353d2stRowjrFx8fmkc6uPmnQb`
- Wiki node token：`K6BQw1X5Piksm2kDex3cMQMenvf`

改相关行为前需要检查的本地文档：

- `docs/architecture/knowledge-system.md`（Knowledge、Memory、Pilot retrieval 或练习消费相关改动）
- `docs/python-rewrite-contract.md`
- `docs/p0-release-checklist.md`
- `docs/superpowers/specs/*`

## 5. 代码改动规则

- 领域模型变化必须同步后端 models、schemas、repositories、API routes、AI tool schemas、前端 types、services、components、tests 和 mock data。
- 不要为已经被 v0.1 最新设计废弃的名称或字段保留长期兼容。如果最新 PRD/ADR 说旧契约已经移除，就干净移除。
- 设计要求的破坏性迁移或 reset 仅限本地开发数据；执行前确认目标与现有授权，未获授权的数据删除先询问。最终汇报说明破坏性变化。
- API 命名、前端 service 命名、Agent tool schema 应暴露当前产品语言，不要继续暴露旧内部语义。
- 优先沿用现有 repository/module 边界。实现一个聚焦改动时，不做无关重构。
- 同一能力同时有 CLI/API 时，尽量保持行为一致。
- 产品运行时的写工具必须保留 HITL 确认，除非配置明确开启 `chat_auto_approve_writes=true`（默认 false）。开发任务的授权不等于允许关闭产品 HITL。
- Agent checkpoint 与 pending/confirm 恢复链路必须保持可用；保留 `provider_blocks` 中的 provider 特定内容。provider fallback 行为以当前配置、实现与测试为准。

## 6. 领域红线

- 事件表和 API 语义是 `application_events`。不要把旧 `events` 表/API 作为长期兼容层重新引入。
- 后端模型名应继续与 `ApplicationEvent` 对齐。
- 事件语义是 `event_type + subtype + tags`。
- `event_type` 至少覆盖 `written_test`、`interview`、`offer_step`、`deadline`、`custom`。
- `assessment` 不是一级 `event_type`；应表示为 `event_type=written_test` 且 `subtype=assessment`。
- Conversation、Chat API、前端 Chat 上下文和 Agent runtime context 使用 `context_type/context_ref`。不要扩展旧 `offer_id` 上下文字段。
- 投递场景使用 `context_type=application` 和 `context_ref=<application_id>`。workspace/global 对话应默认到合理的 workspace context。
- v0.1 面试范围：左栏展示面试入口，进入后展示空状态 / 占位页。保存操作可以 no-op 或保存本地占位状态，但 v0.1 不创建正式 `interview_notes` 或 `mock_sessions` 数据。
- v0.2 面试范围：面试笔记 CRUD、Agent 追问、事件绑定、弱点信号写入。
- v0.3 面试范围：模拟面试、谈薪、录音 / 转写能力。
- Knowledge 的长期产品职责、领域模型和数据流以 `docs/architecture/knowledge-system.md` 为唯一事实源；旧 Source -> Wiki 方向及其 Spec、Plan、ADR 已删除。

## 7. 验证与 Code Review

- 按改动风险选择最小充分验证：纯文档检查 diff、链接、指令冲突与约束保留；行为变更运行相关测试和静态检查；UI 行为用内置浏览器走查。
- 不为可逆、低影响改动新增只复述实现的测试。检查通过后，仅在有新变更、失败或未解决疑点时扩大或重复验证。
- release-style handoff 跑完整本地 gate：`bash scripts/release-gate.sh`（包含 pytest、ruff、mypy、前端测试与构建、HTTP smoke、`oc verify --profile local`）。按发布范围追加 `--docker`、`--install` 或 `--real-ai`；真实 provider 验收沿用已有费用与凭据授权。
- 非平凡代码改动交付前必须启动子代理 CR，涵盖 schema、API、AI tools、前端主流程、持久化、导航、设置、auth 或 Agent 行为改动。工具不可用时做手工 CR 并明确缺口，不声称已完成子代理审查。
- CR 问题修复后验证受影响部分；接受的剩余风险说明理由。不要只凭子代理的成功描述判断完成。
- 报告实际执行的验证及结果。未运行或失败的必要检查说明命令、原因和风险；需要 Docker 却不可用时明确说明，不声称 Docker smoke 通过。

## 8. 技能工作流

- 使用与任务匹配且可读取的 Superpowers 技能；只检查本次需要的技能，不把安装整套技能作为所有任务的前置条件。
- 需求尚不清楚或需设计取舍时用 `brainstorming`；多步骤代码实现用 `writing-plans`；可行的行为变更和 bugfix 用 `test-driven-development`；排查失败用 `systematic-debugging`。
- 完成声明前用 `verification-before-completion`；非平凡实现用 `requesting-code-review` 或等价子代理 CR。无独立工作收益时不为流程形式拆分子代理。
- 纯文档整理可直接审计、编辑和验证，不机械套用产品设计审批、完整代码计划或 TDD。已授权实施不因技能的执行方式选择题再次暂停。
- 技能缺失或不适用时说明，并采用最接近的手工流程；不能因此略过必要的安全或业务验证。

## 9. 飞书文档 / 画板操作

涉及飞书文档或画板时读取 [操作参考](docs/architecture/lark-document-operations.md)。编辑前读取 `lark-cli skills read lark-doc`；遵守 §0 授权边界及参考中的备份、块编辑和回读验证要求。

## 10. 最终汇报

简洁说明改了什么、破坏性变化、剩余风险、验证结果；无破坏性变化时写“无”。未运行的相关测试说明原因。若更新飞书文档，提供链接以及 revision 或回读验证结果。
