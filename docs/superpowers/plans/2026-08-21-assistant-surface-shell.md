# Assistant Surface Shell 与 Haru Chat MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用唯一前端 Conversation Controller 同时驱动 Haru 轻量对话和 Pilot 完整工作区，并完成首批导航入口重组。

**Architecture:** `AssistantSurfaceProvider` 实例化一次 `usePilotConversationController`，Surface reducer 只控制 `mascot / haru_chat / pilot_workspace` 呈现。Haru 和现有 ChatPanel 消费相同 context；Chat HTTP/SSE/HITL service 与 payload 保持不变。

**Tech Stack:** React 18、TypeScript、Ant Design、CSS Modules、Vitest、现有 Live2D/Pixi 与 Chat service。

---

### Task 1: 建立只读 golden 与状态机测试

**Files:**
- Create: `web/src/features/assistantSurface/assistantSurfaceReducer.test.ts`
- Create: `web/src/features/assistantSurface/assistantSurfaceGolden.test.ts`
- Test: `web/src/components/ChatPanel/deterministicPilotConfirmation.test.tsx`

- [ ] 写 reducer 失败测试，固定封闭 Surface、互斥主界面、Pending 展开、关闭不停止和旧 Pilot route 映射。
- [ ] 写源码 golden，固定 ChatPanel `drawer/rail/page`、唯一 `streamChat/streamConfirmAction` service、approve/reject payload、页面上下文、显式停止和后台 lifecycle。
- [ ] 运行 `npm test -- --run src/features/assistantSurface/assistantSurfaceReducer.test.ts src/features/assistantSurface/assistantSurfaceGolden.test.ts`，确认因模块缺失失败。
- [ ] 实现最小 reducer 与只读 presentation helpers，使 reducer 测试通过；golden 在后续抽取中持续作为回归门禁。

### Task 2: 抽取唯一 Conversation Controller

**Files:**
- Create: `web/src/features/assistantSurface/usePilotConversationController.ts`
- Create: `web/src/features/assistantSurface/AssistantSurfaceProvider.tsx`
- Create: `web/src/features/assistantSurface/AssistantSurfaceProvider.test.tsx`
- Modify: `web/src/components/ChatPanel/index.tsx`

- [ ] 写 Provider 所有权失败测试：同一 Provider 下两个消费者得到严格相同 Controller 标识，重复 Provider 边界不允许被 AppShell 创建。
- [ ] 从 ChatPanel 搬迁会话、消息、SSE、Pending、确认、停止、重试、上下文与附件状态；保留 `streamChat` 和 `streamConfirmAction` 原调用点及事件顺序。
- [ ] Controller 返回只读状态与动作：`turns`、`conversationId`、`pending`、`taskState`、`sendMessage`、`stopActiveRequest`、`selectConversation`、`handleConfirm`、`retry` 和上下文/附件操作。
- [ ] ChatPanel 改为 context 消费者，只保留 drawer 尺寸、滚动、面板展开和呈现逻辑；旧直接渲染测试用兼容 Provider 包裹。
- [ ] 运行 Provider、ChatPanel、service、context 定向测试，确认现有 payload、SSE 与 HITL 行为不变。

### Task 3: 实现 Haru Chat MVP

**Files:**
- Create: `web/src/features/assistantSurface/HaruDock.tsx`
- Create: `web/src/features/assistantSurface/HaruChatWindow.tsx`
- Create: `web/src/features/assistantSurface/CompactMessageRenderer.tsx`
- Create: `web/src/features/assistantSurface/assistantPresentation.ts`
- Create: `web/src/features/assistantSurface/AssistantSurface.module.css`
- Create: `web/src/features/assistantSurface/HaruChatWindow.test.tsx`
- Modify: `web/src/features/pilotMascot/PilotMascot.tsx`
- Modify: `web/src/features/pilotMascot/PilotMascot.module.css`

- [ ] 写 Haru 失败测试：最近 8 条、共享 conversation、流式 delta、关闭后继续、停止单次、Pending 跳 Pilot、fallback、Escape/焦点返回和 reduced motion。
- [ ] 实现确定性消息裁剪，不调用任何 service；Pending 始终渲染完整文字引导。
- [ ] 实现文本 composer、停止、上下文标签、任务状态和展开按钮；所有按钮提供至少 40px 命中区与明确 aria label。
- [ ] 让 PilotMascot 仅作为 Dock 触发器，Live2D 加载失败仍保留普通按钮；不新增持续动画。
- [ ] 运行 Haru、PilotMascot 与 accessibility 定向测试。

### Task 4: AppShell 接入与入口重组

**Files:**
- Create: `web/src/features/assistantSurface/PilotWorkspace.tsx`
- Modify: `web/src/layout/AppShell.tsx`
- Modify: `web/src/layout/Sidebar.tsx`
- Modify: `web/src/layout/navigation.ts`
- Modify: `web/src/layout/navigation.test.ts`
- Modify: `web/src/layout/CommandPalette.tsx`
- Modify: `web/src/layout/CommandPalette.test.ts`
- Modify: `web/src/layout/AppShell.mascot.test.tsx`

- [ ] 写 AppShell 失败测试，固定一个 Provider、Haru/Pilot 互斥、流式展开不增加请求、旧 `pilot` ViewMode 继续进入完整工作区。
- [ ] 用单一 `assistantSurface` 替换 `chatOpen/pilotDrawerOpen` 竞争状态；Provider 在 AppShell 生命周期内常驻。
- [ ] 将一级导航改为今日、投递、面试、资料、设置；保留旧 ViewMode 并把题库/知识/提醒放入对应模块 tab。
- [ ] 命令面板可见文案改为“快速打开”，加入“问 Haru”和“打开 Pilot 工作区”。
- [ ] 保持看板/列表使用现有 ViewMode 和筛选状态，不改业务页面或后端 schema。
- [ ] 运行 navigation、CommandPalette、AppShell 与所有 ChatPanel 测试。

### Task 5: 完整验证与独立 CR

**Files:**
- Create: `docs/reports/2026-08-21-assistant-surface-shell-verification.md`
- Modify: any file required to close P0/P1/P2 review findings

- [ ] 运行 `cd web; npm test -- --run`，记录测试数和退出码。
- [ ] 运行 `cd web; npm run build`，确认 TypeScript 与 Vite 构建退出码为 0。
- [ ] 运行 `uv run pytest tests/test_chat_api.py -q`、`uv run ruff check .`、`uv run mypy src` 和 `uv run oc smoke --static-dir web/dist`。
- [ ] 使用内置浏览器验证普通对话、流式展开、关闭后继续、停止、Pending 批准/拒绝、上下文切换、旧深链、fallback、宽屏、768-1179px 和 reduced motion。
- [ ] 启动独立代码审查，修复全部 P0/P1/P2，再重跑受影响测试和完整 gate。
- [ ] 执行 `git diff --check` 和禁止模式搜索，确认未修改 `src/offerpilot/**`、未引入 Journal 状态、`variant="haru"`、第二套 service 或 SSE parser。
