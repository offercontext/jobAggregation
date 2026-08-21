# Desktop Workspace Experience Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 OfferPilot 桌面端收敛为任务优先的今日、投递、面试、资料与统一设置工作台，同时保持后端和 Assistant Surface 契约不变。

**Architecture:** 在现有 AppShell、ViewMode、React Query 服务和业务 Drawer 上重排页面；把确定性状态映射和简历无损转换抽成纯函数。固定 manifest、Golden 与源码门禁验证范围和架构不变量。

**Tech Stack:** React 18、TypeScript、Ant Design、CSS Modules、React Query、Vitest。

---

### Task 1：固定第二阶段范围与 Golden

**Files:**
- Create: `web/workspace-experience-gate.json`
- Create: `web/src/features/workspaceExperience/workspaceExperienceGolden.ts`
- Create: `web/src/features/workspaceExperience/workspaceExperienceGolden.test.ts`
- Create: `web/src/features/workspaceExperience/workspaceExperienceGate.test.ts`

- [ ] 写失败测试，断言 baseline、合成导航/页面清单、允许目录和 manifest SHA-256。
- [ ] 运行 `cd web; npm test -- --run src/features/workspaceExperience/workspaceExperienceGolden.test.ts src/features/workspaceExperience/workspaceExperienceGate.test.ts`，确认因文件或导出缺失失败。
- [ ] 添加纯合成 Golden 与外部门禁 manifest，不记录真实内容或密钥。
- [ ] 重跑目标测试并确认通过。

### Task 2：收敛今日工作台

**Files:**
- Create: `web/src/features/dashboard/todayWorkspace.ts`
- Create: `web/src/features/dashboard/todayWorkspace.test.ts`
- Modify: `web/src/features/dashboard/DashboardView.tsx`
- Modify: `web/src/features/dashboard/dashboard.module.css`

- [ ] 写失败测试，覆盖稳定主行动、其他待办最多三条、未来七天和数据分析默认折叠。
- [ ] 运行目标测试确认 RED。
- [ ] 复用现有 mission、event、KPI、funnel、momentum 数据实现新层级和诚实空状态。
- [ ] 运行 dashboard 相关测试确认 GREEN。

### Task 3：阶段化投递详情

**Files:**
- Create: `web/src/components/applicationWorkspaceModel.ts`
- Create: `web/src/components/applicationWorkspaceModel.test.ts`
- Modify: `web/src/components/ApplicationDetail.tsx`
- Modify: `web/src/components/ApplicationDetail.module.css`

- [ ] 写失败测试，覆盖六个后端状态到七种用户阶段动作的确定性映射、主按钮与更多操作不重复。
- [ ] 运行目标测试确认 RED。
- [ ] 增加固定头、分区锚点、阶段主操作和更多菜单；把 AI 入口统一为“让 Haru 帮我”。
- [ ] 运行 ApplicationDetail 相关测试确认 GREEN。

### Task 4：组合面试、练习与资料入口

**Files:**
- Modify: `web/src/layout/navigation.ts`
- Modify: `web/src/layout/navigation.test.ts`
- Modify: `web/src/components/InterviewV01View.tsx`
- Modify: `web/src/components/InterviewV01View.test.tsx`
- Modify: `web/src/layout/AppShell.tsx`

- [ ] 写失败测试，断言待准备、模拟练习、题库、复盘与成长，以及简历、经历与故事、学习资料和旧 ViewMode 映射。
- [ ] 运行目标测试确认 RED。
- [ ] 用现有回调组合入口，不改服务、Query Key 或 Story/Knowledge 写入。
- [ ] 重跑导航、AppShell 和面试测试确认 GREEN。

### Task 5：实现无损结构化简历编辑

**Files:**
- Create: `web/src/lib/structuredResume.ts`
- Create: `web/src/lib/structuredResume.test.ts`
- Modify: `web/src/components/ResumeEditorDrawer.tsx`
- Modify: `web/src/components/ResumeLibraryView.tsx`
- Modify: `web/src/components/ResumeLibraryView.module.css`

- [ ] 写失败测试，覆盖已知字段编辑、条目增删排序、未知顶层/嵌套字段 round-trip、非法/不兼容数据阻止普通覆盖。
- [ ] 运行目标测试确认 RED。
- [ ] 实现以原 JSON 为基底的结构化 draft/patch/validation，并添加普通表单、高级 JSON、只读恢复与未保存确认。
- [ ] 将可见文案改为“初稿”“上传现有简历”，把样例入口移入更多菜单。
- [ ] 重跑简历相关测试确认 GREEN。

### Task 6：渐进 Offer 与统一设置

**Files:**
- Create: `web/src/components/offerWorkspaceModel.ts`
- Create: `web/src/components/offerWorkspaceModel.test.ts`
- Modify: `web/src/components/OfferCenterView.tsx`
- Modify: `web/src/components/OfferCenterView.test.tsx`
- Modify: `web/src/components/SettingsView.tsx`
- Modify: `web/src/layout/AppShell.tsx`
- Modify: `web/src/layout/TopBar.tsx`

- [ ] 写失败测试，覆盖 Offer 0/1/2+、用户进入比较后才显示维度，以及设置五分区和唯一入口。
- [ ] 运行目标测试确认 RED。
- [ ] 实现渐进披露和待确认事实；把顶栏齿轮直接路由到设置 AI 与模型分区，移除独立 AI 设置页面状态。
- [ ] 重跑 Offer、Settings、AppShell 测试确认 GREEN。

### Task 7：用户语言、机械门禁与桌面样式

**Files:**
- Modify: `web/src/components/KnowledgeSourcesView.tsx`
- Modify: `web/src/layout/Sidebar.tsx`
- Modify: `web/src/theme/tokens.css`
- Modify: `web/src/features/workspaceExperience/workspaceExperienceGate.test.ts`

- [ ] 写失败测试，固定 Knowledge 用户语言、四个一级业务导航、独立设置、单 Provider/controller、无新增 Chat/SSE/Journal/Pending/HITL/移动/设计系统依赖。
- [ ] 运行目标测试确认 RED。
- [ ] 完成文案和 768-1179/1180+ 桌面样式，保持键盘、焦点与 reduced-motion。
- [ ] 重跑门禁与相关组件测试确认 GREEN。

### Task 8：完整验证、浏览器验收和独立 CR

**Files:**
- Create: `docs/reports/2026-08-21-desktop-workspace-experience-redesign-verification.md`

- [ ] 运行 `cd web; npm test -- --run` 和 `npm run build`，记录数量与退出码。
- [ ] 运行 `uv run pytest tests/test_chat_api.py -q`、`uv run ruff check .`、`uv run mypy src`、`uv run oc smoke --static-dir web/dist`。
- [ ] 用内置浏览器在 768、1024、1440 宽度走查完整链路、旧入口、控制台、请求和 SSE。
- [ ] 发起独立子代理 CR，修复或记录所有 P0/P1/P2，再重新运行受影响 gate。
- [ ] 写验证报告，记录 `2f6e895`、Runtime integration baseline=待集成、最终提交、allowlist、浏览器证据、破坏性 UI 变化、后端契约、未实施项和风险。
- [ ] 分开执行 `git add` 与 `git commit -m "refactor: AI 重构桌面求职工作区"`，不 push、不 merge。
