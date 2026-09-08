# Agent 指令审计记录（2026-09-07）

> 已归档：一次性指令整理的决策与验证记录，不是当前施工任务。

本文件记录本次审计，不是新增执行协议；当前规则由 [AGENTS.md](../../AGENTS.md) 维护。

## 依据与范围

已检索并打开 [GPT-6 Astra 官方模型指南](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6-astra)。本次采用其中关于减少无谓暂停、明确用户与技能优先级、限定委派条件、按改动范围验证的建议。下列具体整理方案是仓库维护判断，不是官方要求的文件结构。

覆盖当前项目的 AGENTS.md、CLAUDE.md、文档规范及其 Claude 入口、文档提醒 hook、RULE 注册表和飞书操作参考。扫描包含隐藏与忽略文件，排除 .git、依赖、隔离 worktree 和测试临时目录；未发现项目自有 SKILL.md 或技能 references 目录。

项目使用用户级 Superpowers 等技能。读取了相关 SKILL.md（using-superpowers、writing-plans、verification-before-completion、requesting-code-review、skill-creator、openai-docs、web-access）及 openai-docs 的 model-migration reference；没有全量审计或修改全局技能、插件缓存和其他 worktree。

## 修改决策

| 发现 | 处理 |
|---|---|
| 重复输出禁令、泛化的推理口号、GPT-5.5 远程评测命令 | 从项目常驻指令移除；保留简洁输出与必要验证，不运行远程评测 |
| AGENTS 与 CLAUDE 重复领域红线、工作流和门禁 | 统一到 AGENTS；CLAUDE 保留定位入口与测试约定 |
| CLAUDE 固定路由数量、代码行数、旧 fallback 字段和迁移实现快照 | 改为当前代码、配置与测试入口；保留 checkpoint、provider_blocks 和 HITL 约束 |
| 技能强制加载和设计/计划流程可能造成额外暂停 | AGENTS 明确按任务选择、文档任务轻量处理、已有授权不重复确认 |
| 验证要求可能扩展到无关全量测试 | 区分文档、行为、UI、发布；保留非平凡改动的子代理 CR 和完整发布 gate |
| 文档行数既写软约束又写禁止、旧日期触发自动删除 spec/plan | 统一软上限；历史文档按明确维护任务归档，不按日期删除 |
| 文档规范与 hook 重复模板和流程 | hook 只保留简短提醒和规范链接；注册状态按 settings.json 修正 |
| 飞书细节在所有任务中加载 | 移至 [操作参考](../architecture/lark-document-operations.md)，保留备份、块操作陷阱和回读要求 |

## SKILL 与 references 的剩余边界

- `using-superpowers` 的极低相关性加载门槛、`writing-plans` 的固定审批/执行选择、`verification-before-completion` 的重复全量检查措辞，可能与本任务的精简目标冲突。项目入口明确了适用范围与用户授权优先级；全局原文件未更改。
- `web-access` 的浏览器前置检查和提示不适合泛化为所有联网任务的门槛；项目仅把它路由到网页访问，Git 使用 Git CLI。官方文档本次由内置 web 工具读取，未启动 CDP 或操作用户浏览器。
- `model-migration` reference 中的 API 参数和迁移步骤不适用于本次指令维护；未改模型默认值、provider 路由或 API 配置。
- 不为不存在的项目技能新建空 SKILL.md；飞书参考直接从 AGENTS 按任务路由。

## 验证与限制

- AGENTS 从 202 行减至 95 行，CLAUDE 从 175 行减至 31 行；飞书参考单独保留 46 行。
- 独立子代理审查暂存的七个文件及相邻引用，未发现需要返工的问题；暂存 diff 空白检查通过。
- 飞书块操作约束与原文对比通过，仅适配临时目录和字符串检查措辞。
- 六份执行文档的 20 个本地 Markdown 链接与长度检查通过；Claude 文档规则入口的原相对链接有效，保持原样。
- AGENTS §6 与修改前逐字比较一致；产品写工具仍要求 HITL，自动批准仍须显式配置。
- 文档提醒 hook 的 Bash 语法与四个隔离场景通过：非 Bash、非 commit、无 Markdown 暂存改动不提示，有 Markdown 的 commit 提示；各场景均 exit 0。
- hook 场景使用 jq/git 替身，环境缺少 jq，未验收真实 Claude 会话触发。没有运行应用测试、前端构建、Docker 或真实 provider gate，因为本次不改应用行为。
- 飞书操作经验仅迁移整理，未访问或修改远端飞书文档，未重新验证 CLI 的远端行为。
- 无数据或应用 API 的破坏性变化；现有用户代码、测试与临时产物不纳入本次提交。
