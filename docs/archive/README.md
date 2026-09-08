# 历史文档索引

这里保留已结束、被替代或仅用于追溯的施工资料；其中的计划步骤、未勾选项、测试数量和“未合并”等状态只属于记录时的基线，不是当前执行要求。

当前入口：[仓库协议](../../AGENTS.md)、[文档规范](../architecture/documentation-rules.md)、[产品待办](../product-backlog.md)、[用户指南维护记录](../product-manual/维护记录.md)。产品事实源继续按 AGENTS 路由，不由本索引替代。

## 已归档

| 主题 | 历史资料 | 当前参考 |
|---|---|---|
| Python 切换 | [切换验收](python-cutover-verification.md)、[架构审查](python-architecture-review-20260706.md) | [兼容契约](../python-rewrite-contract.md)、[发布清单](../p0-release-checklist.md) |
| Agent 指令整理 | [2026-09-07 审计](agent-instruction-audit.md) | [AGENTS.md](../../AGENTS.md) |
| README 产品概览 | [2026-08-01 设计](readme/2026-08-01-readme-product-overview-design.md)、[计划](readme/2026-08-01-readme-product-overview.md) | [当前 README](../../README.md) |
| README 旧截图刷新 | [2026-08-13 设计](readme/2026-08-13-readme-screenshot-refresh-design.md)、[计划](readme/2026-08-13-readme-screenshot-refresh.md)、[验收](readme/2026-08-13-readme-screenshot-refresh-browser-acceptance.md) | [指南与配图维护](../product-manual/维护记录.md) |
| 早期 AI 对话调研 | [2026-07-10 调研](ai-chat-experience-research-20260710.md) | 当前 Agent 行为查代码、测试与相关实施契约 |

README 旧图片仍保留在 `docs/assets/readme/`，用于历史证据追溯；本次不批量删除图片。

## 保持原路径的报告入口

不为统一目录而移动全部资料。以下位置统一由此导航，但仍保留原文件路径及验收基线：

- [迭代与验收报告](../reports/)：按具体功能查阅，不把一次报告视为当前整库发布结论。
- [面试准备发布验证](../release/2026-07-30-evidence-gated-interview-preparation-release-validation.md)：原 `docs/release/` 资料。
- [施工阶段验证报告](../superpowers/reports/)与[独立审查记录](../superpowers/reviews/)。
- [Offer 谈薪历史验证](../reports/2026-08-01-offer-negotiation-release-verification.md)：已移除重复 artifacts 路径的空壳报告，截图保留原位。
- [用户指南历史覆盖与问题记录](../product-manual/维护记录.md#historical-coverage)：已并入维护记录，不再分头维护。

其余 [spec](../superpowers/specs/) 与 [plan](../superpowers/plans/) 按相关任务检索；其中仍有当前契约及测试直接依赖，不按日期一概归档或删除。例如 Haru surface、desktop task flow 的设计、计划和报告路径仍由前端 gate 引用，本次保持不变。
