# OfferPilot 产品待办

状态核对：2026-09-08，主线 `408d851`。本文收敛 2026-08-07 调研形成的待办，不重新定义产品 PRD，也不将历史验收当作本次回归结果。

原调研来源：Career-ops、ai-job-search、interview-coach-skill、ResumeSkills。

## 原调研事项的实现状态

原优先顺序是“投递结果回流 → 事实与故事沉淀 → 下一轮准备”。以下能力已在当前主线存在，不再列为尚未启动或等待合并；具体覆盖范围与限制以对应实现及历史报告为准。

| 原事项 | 当前实现范围与入口 | 历史证据 |
|---|---|---|
| JD 版本前置依赖 | [JD 版本仓储](../src/offerpilot/repositories/application_jd_versions.py)已存在，不再等待分支合并 | [JD 版本验证](reports/2026-08-05-application-jd-versions-release-verification.md) |
| 投递事实档案与结果反馈 | [投递档案及结果仓储](../src/offerpilot/repositories/application_outcomes.py)保存冻结快照与追加式反馈历史，API 已接入 | [结果闭环验证](reports/2026-08-12-application-outcome-feedback-release-verification.md) |
| 结构化面试故事库 | [故事仓储](../src/offerpilot/repositories/interview_stories.py)支持故事、不可变版本、来源与人工确认；不据此宣称原调研中的所有扩展字段均已交付 | [故事库验证](reports/2026-08-10-interview-story-library-release-verification.md) |
| 基于复盘的自适应练习 | [专项练习仓储](../src/offerpilot/repositories/adaptive_interview_practice.py)支持确认重点驱动的练习与完成历史，不引入综合能力评分 | [练习验证](reports/2026-08-12-adaptive-interview-practice-release-verification.md) |
| 简历诊断与事实补充 | [事实补充工作台](../web/src/components/ResumeFactSupplementWorkspace.tsx)支持手工确认事实并创建派生版本；不是 AI 自动补齐事实 | [工作台验收](reports/2026-08-12-resume-fact-workspace-browser-acceptance.md) |
| 跟进信、感谢信草稿 | [沟通草稿面板](../web/src/components/ApplicationCommunicationDraftPanel.tsx)提供本地可编辑、可复制草稿，不调用 AI、不持久保存草稿、不自动发送 | [沟通草稿验收](reports/2026-08-13-application-communication-drafts-browser-acceptance.md) |

报告内“本轮未合并／未推送”是其编写时状态；测试数量与验收结果只适用于报告所列基线。剩余体验问题与未完成文档走查见[用户指南维护记录](product-manual/维护记录.md)，不混入新功能待办。

原调研还提出故事的 STAR 结构、能力标签、适用问题、使用次数与最近使用时间，以及简历工作区按诊断、事实补充、岗位定制、版本与投递收敛。本次只纠正“尚未启动”的状态，不逐项认定这些目标全部完成或取消；扩展范围需按相关设计与实现另行核对。

## 仍暂缓

- **批量岗位导入**：最后考虑，只处理用户主动导入的数据；不自动扫描招聘网站、不操作外部平台、不引入岗位排名或低分劝退。
- **Pipeline Intelligence / Mission Control 等工作台扩张**：不因已有基础闭环就自动启动；仍需独立评估价值与范围，本次不指定下一项开发任务。

## 持续边界

- 冻结实际投递的 JD、Resume 版本及确认材料，历史记录不可覆盖。
- 所有结果反馈、故事和练习建议区分用户确认事实、系统观察、尚未验证的建议。
- 缺少真实数字时追问并确认，不估算或伪造指标；来源变化应可见。
- 用户主动确认后启动练习；不生成综合面试能力分、录用概率或不可解释的成功率。
- 不新增自动外联、不降低证据校验，产品写工具保留人工确认。
