# 体验审计优先问题修复计划

> 执行方式：本会话按测试先行逐批实施，最终独立子代理 CR；不合并、不推送，不改真实业务记录。

**Goal:** 修复截图审计七组问题，保留现有 HITL、来源校验、幂等和单 Controller。

**Architecture:** 在发生偏差的读接口、任务投影和展示组件修复，不绕过权限或来源校验。24小时是首页推荐优先级，不是用户主动准备的权限边界。未实现的知识出题不能伪装为可用。

**Tech Stack:** Python/FastAPI/SQLite，React/TypeScript/React Query/Vitest。

## 1. 日历时间

- [x] 在 `tests/test_calendar_api.py` 增加无时区数据库 datetime 不允许按本机时区转换的回归；月历与日程详情必须返回同一时间。
- [x] 运行 `uv run pytest tests/test_calendar_api.py -q` 验证 RED。
- [x] `src/offerpilot/api.py` 月历使用既有 `_format_rfc3339(scheduled_at)`，与 `_event_json` 一致，不迁移历史数据。
- [x] GREEN 后检查暗色日历 `web/src/components/CalendarView.module.css` 使用既有语义颜色。

## 2. 准备与复盘

- [x] `web/src/features/applicationTasks/applicationTaskResolver.test.ts` 增加超过24小时未来事件可主动准备、与已完成事件并存仍可复盘测试。
- [x] 定向运行 Vitest 观察 RED。
- [x] `applicationTaskResolver.ts` 区分有效未来事件与24小时推荐窗口；远期事件不制造 contract_invalid。保留 malformed/foreign/pending/source guard。
- [x] 回归同一 Application 两个事件、已取消、过期未更新、来源错误与重复身份。

## 3. 故事回读

- [x] `tests/test_interview_stories_api.py` 验证详情 version 与版本列表 `confirmed_at` 一致。
- [x] RED 后在 `src/offerpilot/repositories/interview_stories.py::_version_payload` 输出数据库已有 `confirmed_at`，不放宽前端验证。
- [x] 运行故事 API 与前端历史校验测试；浏览器回读审计故事。

## 4. 知识出题与资料刷新

- [x] 核实既有知识消费协议；本次不引入未经设计的新检索/跨来源AI能力。后端不支持的选项明确不可选，默认已有复盘出题，文案不承诺知识出题已可用。
- [x] `QuestionBankView.test.tsx` 及新增 mounted 测试锁定请求来源；不静默替换用户已选来源。
- [x] `KnowledgeSourcesView` 回归解析完成后依据刷新，以及从搜索重新进入同一来源刷新/定位；不无限轮询。

## 5. 比较页

- [x] `OfferCenterView.test.tsx` 增加选中2份/总共3份时先显示比较，维度编辑仅接收选中对象。
- [x] `OfferCenterView.tsx` 将比较结果前置、维度设置置于可展开次级区域；保留维度选择与保存行为。

## 6. Haru结果

- [x] 对比 `HaruChatWindow` 与 `MessageBubble` 数据使用，补包含正文+结构摘要的同一响应回归。
- [x] 展示正文而非只展示摘要；不重复请求、不开新会话、不改变保存内容或确认 owner。

## 7. 验证与交付

- [x] 每批定向 RED/GREEN，前端类型检查/构建，后端 Ruff/Mypy相关范围。
- [x] 浏览器逐一回归审计场景并保存修后截图；失败如实标记，不把关闭入口写成完成检索实现。
- [x] 独立 CR 无未关闭 P0/P1/P2 后按中文 conventional commit 提交。
- [x] 报告列出改动、破坏性变化、验证、剩余风险；未授权不 merge/push。

## 执行补充

- Python 使用既有 venv，`PYTHONPATH` 指向本 worktree，避免更改部署依赖。
- 浏览器发现第二个故事读取根因：API 合法空 `text_location` 被前端拒绝。fixture 加入真实空串后出现8个RED，精确允许空串后21个测试GREEN；未放宽身份/来源/类型校验。
- 审计 Application 276 的 `source='ai'`，准备生成 API 只允许 `cli/manual/web`。入口已修复，但实际生成仍被既有来源门禁拒绝。本次不修改白名单或历史数据，须单独确认产品范围。
- 各项勾选表示对应修复/验证动作执行，不表示上述来源阻塞或知识出题能力已解决；最终验证详情以验收报告为准。
