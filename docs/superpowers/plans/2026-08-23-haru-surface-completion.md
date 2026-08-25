# Haru Desktop Surface Completion 实施计划

> **For agentic workers:** 使用 test-driven-development 按任务推进；每个任务先写可观察的 RED，再实现最小 GREEN，最后运行受影响 gate。所有文件必须落在本计划的 allowlist 内。

**Goal:** 从原始实现基线 `aaecf5dfa6ce913ecaf00b25a0e88bcf46096eeb` 完成 Haru desktop surface 的单 Provider、单 controller、稳定 context identity、一次请求一次 lifecycle、可访问性、偏好和桌面布局交接，并在当前组合基线 `7c36957176445c5213a31b013251fbbce8d610db` 上收口。

**Architecture:** `AssistantSurfaceProvider` 统一 surface、通知和 request handoff；`usePilotConversationControllerState` 统一 Chat transport、pending/HITL、stop、conversation context 和 task lifecycle；HaruDock/HaruChatWindow/PilotWorkspace 只消费它们。AppShell 只提供页面 context、附件、导航与业务回调，不再镜像 request/notification/lifecycle state。

**Tech Stack:** React 18、TypeScript、Ant Design、CSS Modules、Vitest、现有 Chat service/SSE parser、现有 Live2D runtime。禁止新增 API、后端模型、Journal/AgentRun/Ledger、移动依赖或第二套设计系统。

**Baseline:** `aaecf5dfa6ce913ecaf00b25a0e88bcf46096eeb` 是历史实现起点；当前可执行 gate 使用 `7c36957176445c5213a31b013251fbbce8d610db`，且后者必须为当前 `HEAD` 的 ancestor。

**Allowlist:**

```text
.gitattributes
web/src/features/assistantSurface/**
web/src/features/pilotMascot/**
web/src/layout/AppShell.tsx
web/src/layout/AppShell*.test.*
web/src/components/ChatPanel/index.tsx
web/src/components/SettingsView.tsx
web/src/components/SettingsView*.test.*
docs/superpowers/specs/2026-08-23-haru-surface-completion-design.md
docs/superpowers/plans/2026-08-23-haru-surface-completion.md
docs/reports/2026-08-23-haru-surface-completion-verification.md
```

当前组合 canonical allowlist JSON 的 SHA-256 固定为 `b1698f9b89b23effcb6adc604c5d4457a26a36c6d2207b4bbe49c70cae290eb8`。

---

### Task 0：建立基线、路径和源码边界 gate

**Files:**

- Create: `web/src/features/assistantSurface/assistantSurfaceGate.test.ts`
- Create: `docs/superpowers/specs/2026-08-23-haru-surface-completion-design.md`
- Create: `docs/superpowers/plans/2026-08-23-haru-surface-completion.md`
- Create when final evidence is available: `docs/reports/2026-08-23-haru-surface-completion-verification.md`

- [ ] 固定 baseline ancestor、精确 allowlist、canonical hash 和当前 worktree 事实。
- [ ] 合并 committed (`BASELINE..HEAD`)、unstaged、staged、normal untracked 与 ignored docs；rename 展开为 old/new 路径。
- [ ] 对 surface 生产源码执行负向边界：Chat service/stream 仅 controller；无新增 EventSource/fetch/ReadableStream/getReader、Journal/AgentRun/Ledger/ToolMessage、`variant="haru"`、重复 `runId` 或移动运行时依赖。
- [ ] 断言单 Provider/controller construction owner，AppShell 不持有重复通知、request、reply lifecycle state。
- [ ] 在当前实现未完成时允许 gate 明确 RED；不得把 RED 伪装成组合 GREEN。

运行：

```powershell
cd web
npm.cmd test -- --run src/features/assistantSurface/assistantSurfaceGate.test.ts
```

### Task 1：收敛 Provider 与 controller 所有权

**Files:**

- Modify: `web/src/features/assistantSurface/AssistantSurfaceProvider.tsx`
- Modify: `web/src/features/assistantSurface/usePilotConversationController.ts`
- Modify: `web/src/features/assistantSurface/AssistantSurfaceProvider.test.tsx`
- Modify: `web/src/features/assistantSurface/assistantSurfaceReducer.ts`
- Modify: `web/src/features/assistantSurface/assistantSurfaceReducer.test.ts`

- [ ] 先补测试：Provider 只挂载一次，open/close/expand/request handoff 只经过 context，消费 request key 后不可重复消费。
- [ ] 先补测试：相同 request 的 running/completed/failed 回调只产生一次 terminal lifecycle；关闭 drawer 不停止 request。
- [ ] 让 Provider 只管理 surface/notice/request generation；让 controller 继续独占 Chat service、AbortController、pending/HITL 和 stream stop。
- [ ] 保留原有 reducer 语义，完成 notice 只在后台 terminal 事件触发，前台完成不重复提示。
- [ ] 运行 Provider、reducer、Golden 定向测试。

### Task 2：完成 Haru surface 与三种布局

**Files:**

- Modify: `web/src/features/assistantSurface/HaruDock.tsx`
- Modify: `web/src/features/assistantSurface/HaruChatWindow.tsx`
- Modify: `web/src/features/assistantSurface/PilotWorkspace.tsx`
- Modify: `web/src/features/assistantSurface/assistantSurface.css` 或现有 surface CSS module
- Create/Modify: `web/src/features/assistantSurface/haruWindowPosition.ts`
- Create/Modify: `web/src/features/assistantSurface/haruWindowPosition.test.ts`
- Modify: `web/src/features/assistantSurface/HaruChatWindow.test.tsx`

- [ ] 先写 drawer/rail/page 的合成 Golden：Haru anchor、唯一 surface、expand/close 行为和不丢草稿/附件。
- [ ] 保持 close 不停止请求；Stop 仍调用 controller stop；completion notice 点击只打开原 conversation。
- [ ] 实现桌面 anchor 与紧凑桌面 rail 的确定性位置，避免遮挡主操作；不引入 `variant="haru"`。
- [ ] 加入 drawer focus、Escape、Tab 边界、button/dialog 名称和低噪声 `aria-live`。
- [ ] 在 `prefers-reduced-motion` 下关闭视觉动画而不改变行为。
- [ ] 运行 HaruChatWindow、assistant presentation、position 和 Golden 测试。

### Task 3：固定 context identity 与 context-change notice

**Files:**

- Modify: `web/src/features/assistantSurface/usePilotConversationController.ts`
- Modify: `web/src/features/assistantSurface/PilotWorkspace.tsx`
- Modify: `web/src/features/assistantSurface/HaruChatWindow.tsx`
- Modify: `web/src/features/assistantSurface/AssistantSurfaceProvider.tsx`
- Modify: `web/src/features/assistantSurface/AssistantSurfaceProvider.test.tsx`
- Modify: `web/src/features/assistantSurface/assistantSurfaceGolden.test.ts`

- [ ] 先写 identity 测试：同一 view/entity/semantic filters 仅 label 变化不触发 notice；entity 或语义筛选变化触发 notice。
- [ ] 先写状态测试：following、pinned、request snapshot 三者可观察且相互不误写；active request/pending 时切换动作不可执行。
- [ ] 提供“切换到当前页面”和“保持原上下文”动作；切换只影响后续发送，不回写旧消息。
- [ ] 验证 context notice、message、attachments 在 drawer 与 Pilot workspace 间保留。
- [ ] 运行 context/controller/Golden 定向测试。

### Task 4：迁移 AppShell 接线并删除重复 state

**Files:**

- Modify: `web/src/layout/AppShell.tsx`
- Modify: `web/src/layout/AppShell*.test.*`
- Modify: `web/src/features/assistantSurface/AssistantSurfaceProvider.test.tsx`

- [ ] 先写源码门禁失败断言：AppShell 不出现 `pilotMascotNotification`、conversation request 镜像、reply lifecycle handler 或第二个 surface Provider。
- [ ] 将 open/expand/notice/context/lifecycle 回调改为 Provider/controller context；页面只生成 `PilotPageContext` 和业务导航意图。
- [ ] 保留业务页面原有服务、Query Key、HITL 和附件回调，不把 Chat service 引回 AppShell。
- [ ] 验证 drawer、rail、Pilot workspace、interview studio 的 surface 互斥关系。
- [ ] 运行 AppShell、Haru、PilotMascot 和 gate 测试。

### Task 5：完成 Haru/外观设置与偏好

**Files:**

- Modify: `web/src/components/SettingsView.tsx`
- Modify: `web/src/components/SettingsView*.test.*`
- Modify: `web/src/features/pilotMascot/pilotMascotPreference.ts`
- Modify: `web/src/features/pilotMascot/pilotMascotPreference.test.ts`
- Modify: `web/src/features/pilotMascot/PilotMascot.tsx`
- Modify: `web/src/features/pilotMascot/PilotMascot.test.tsx`
- Modify: `web/src/features/pilotMascot/live2dRuntime.test.ts`
- Modify: `web/src/features/pilotMascot/pilotMascotSafeArea.test.ts`

- [ ] 先写 preference 测试：visible、zoom、animation level 的默认值、持久化、非法值回退和 reduced-motion 优先级。
- [ ] 设置页保留 AI/model、data/backup、Haru/appearance、voice、advanced/diagnostics 分区；Haru 入口只能有一个 owner。
- [ ] Haru visible/zoom/animation 变化即时反映到 surface，刷新后不改变 controller/context 语义。
- [ ] 保留 Live2D fallback、safe area、resize、reduced-motion 和 keyboard 行为。
- [ ] 运行设置与 PilotMascot 定向测试。

### Task 6：组合验证、浏览器验收和独立 CR

**Files:**

- Create: `docs/reports/2026-08-23-haru-surface-completion-verification.md`

- [ ] 运行定向前端测试，记录文件数、测试数、失败项和 gate RED/GREEN 原因。
- [ ] 运行完整前端 `npm.cmd test -- --run`、`npm.cmd run build`。
- [ ] 运行 `uv run pytest tests/test_chat_api.py -q`、`uv run ruff check .`、`uv run mypy src`、`uv run oc smoke --static-dir web/dist`；不可运行的命令记录原因，不声称通过。
- [ ] 用内置浏览器在 768、1024、1440 宽度走查：打开/关闭/展开、发送/停止、后台完成 notice、原对话跳转、context switch、HITL、focus/Escape 和 reduced-motion。
- [ ] 发起独立 CR；P0/P1/P2 必须修复，接受的剩余风险写入报告。
- [ ] 报告写明最终 commit、baseline、allowlist/hash、验证证据、破坏性 UI 变化、后端契约、Runtime integration baseline 和 deferred 清单。
- [ ] 最终集成顺序固定为：Agent Loop/runtime owner 上游合入 → 本分支 surface/controller 组合 → 定向 gate → 完整 gate/browser → CR → root 统一集成；本计划不 push、不 merge。

## 延期能力（不可在实现中顺手扩展）

Compact Confirmation、Journal/AgentRun/Ledger UI、SSE replay、后台 Agent queue、持久去重、跨刷新通知、自动上下文切换、无 HITL 写操作、正式面试笔记/模拟会话模型、移动端产品和新 Chat/API/SSE parser 均不属于本计划。
