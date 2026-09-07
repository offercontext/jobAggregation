# 体验审计优先问题修复验证

## 范围与状态

- 分支：`fix/20260907-audit-priority-flows`。
- 基线：`782d524583a21eba02d9078463c318d4aae56f48`。
- 独立 worktree 实施，不触碰根工作区未提交修改，不 merge/push。
- 本次是聚焦修复，不是全产品验收；准备 AI 的来源限制仍存在，不能宣称全部端到端闭环。

## 已修复

| 问题 | 根因和修复 | 验证 |
|---|---|---|
| 日历比详情少8小时 | SQLite naive UTC-like 时间错误按宿主时区转换；改用既有 RFC3339 helper | 旧路径会触发失败的 naive datetime 回归；实测同一面试均为14:00 |
| 未来面试无法准备、同时阻塞复盘 | 把24小时推荐窗口误当执行门并制造全局 issue；远期事件可执行、priority8 | 同 Application 的未来/已完成事件同时可用，复盘优先；浏览器两入口均可打开 |
| 保存故事后版本不可读 | API 缺少 confirmed_at；前端拒绝合法空 text_location | 两轮 RED/GREEN；原已保存 AI 故事可读，无需重建 |
| 知识出题必然报不支持 | 暴露尚未实现的来源 | 明确禁用并默认复盘；mounted 证明提交 notes，不声称实现知识 retrieval |
| 资料解析/搜索详情停留旧缓存 | evidence/content 未随提取完成刷新；定位在异步依据挂载前被消费 | 提取状态变更后两类读取各增加一次，正文/依据更新；搜索重新进入同源并定位 |
| Offer 比较先出现设置表单 | 维度编辑抢占结果层级、传入全部 Offer | 比较表前置，设置默认折叠；3选2仅读取选中ID并保持顺序 |
| Haru 只有结构摘要 | compact projection 丢弃正文 | 同一响应包含摘要/正文/下一步；真实 AI 返回三条建议可读，不改变 Controller |

## 浏览器截图

截图文件位于本机验证目录，不加入 Git。每张截图说明如下：

1. [日历时间](D:/Users/yuqi.chen/.offerpilot/verification/audit-priority-fixes-20260907/01-calendar-time.jpg)：同一星河智能面试显示9月8日14:00，暗色日历使用语义颜色。
2. [准备入口](D:/Users/yuqi.chen/.offerpilot/verification/audit-priority-fixes-20260907/02-preparation-open.jpg)：从具体事件选择样例简历后，能进入面试准备任务，而非“不可准备”。
3. [真实剩余来源阻塞](D:/Users/yuqi.chen/.offerpilot/verification/audit-priority-fixes-20260907/03-preparation-source-block.jpg)：点击生成后实际POST404，不能把本图当作生成成功。
4. [复盘入口](D:/Users/yuqi.chen/.offerpilot/verification/audit-priority-fixes-20260907/04-review-open.jpg)：已完成事件能进入正确绑定的新建复盘表单；本轮未提交新复盘。
5. [资料搜索](D:/Users/yuqi.chen/.offerpilot/verification/audit-priority-fixes-20260907/05-knowledge-search.jpg)：检索“过期版本”，打开并定位现有合成资料的依据。
6. [Offer 比较](D:/Users/yuqi.chen/.offerpilot/verification/audit-priority-fixes-20260907/06-offer-comparison.jpg)：先展示两份 Offer 的固定事实，设置维度在下方折叠。
7. [Haru 正文](D:/Users/yuqi.chen/.offerpilot/verification/audit-priority-fixes-20260907/07-haru-full-answer.jpg)：真实 AI 的项目表达建议正文在小窗可滚动阅读，不再只剩一句摘要。
8. [出题来源](D:/Users/yuqi.chen/.offerpilot/verification/audit-priority-fixes-20260907/08-question-supported-source.jpg)：参考资料明确暂未开放，默认面试复盘。
9. [故事正文](D:/Users/yuqi.chen/.offerpilot/verification/audit-priority-fixes-20260907/09-story-readable.jpg)：原来无法打开的版本1，现可显示标题、适用问题和冻结来源。

## 验证口径

- 首批前端6文件81 tests；关联8文件129 tests；新增运行时出题测试1 test。最后修改的相关组再次运行，不能把重复执行次数算成新增覆盖。
- Ruff：改动的2个Python源码、2个测试文件通过。
- Mypy：2个改动Python源码通过。
- TypeScript + Vite生产构建通过，保留约1653.74 kB主chunk警告。
- 独立CR最终P0/P1/P2均为0；三项测试真实性建议已经补齐。
- 未跑全量后端、全量前端、Docker或发布编排外部门禁；本次不冒充发布级全量验收。
- 后端 `tests/test_calendar_api.py tests/test_interview_stories_api.py`：16 passed，65条既有弃用警告，242.49秒。
- 最后一次 Knowledge mounted 回归：11 passed；最后一次出题相关3文件回归18 passed。独立CR针对5个重点文件复跑93 passed。均是上述去重范围的重复验证。
- `git diff --check` 通过，仅LF/CRLF提示；暂存范围只包含本批源码、测试、计划及本报告。

## 未解决边界与风险

1. 审计 Application 276 来自 `source='ai'`；准备生成API明确要求 `HUMAN_APPLICATION_SOURCES={cli,manual,web}`。实际POST404发生在Provider之前。此次不放宽白名单、不把来源改为manual。是否允许经HITL创建的AI投递消费准备/评估能力，需要另行确定统一来源资格，而不是删除一处校验。
2. 知识出题仍未实现。本次只撤去误导性可执行入口，复盘出题保留。
3. 资料提取完成的自动刷新竞态由mounted测试覆盖；浏览器验证的是现有已解析资料回读和搜索，没有重跑完整文档解析生命周期。
4. Haru保持现有纯文本渲染，Markdown标记仍可能可见；本次修复丢正文，不做视觉重构。
5. 日历修复已观察的时区重复转换；未声称本次重设跨午夜月历分桶规则。

## 数据与部署

无migration、历史数据改写或删除。新增一段“体验审计回归”合成中文AI对话，用于验证Haru正文；未确认领域写操作。尝试面试准备被来源门禁拒绝，未生成准备建议。其余使用既有审计资料只读回归。

本地8080运行本修复worktree的后端和生产构建，方便用户检查；这不代表已合入main。
