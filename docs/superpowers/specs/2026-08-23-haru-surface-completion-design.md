# Haru Desktop Surface Completion 设计

## 文档状态

- 状态：已确认，可实施
- 日期：2026-08-23
- 固定基线：`aaecf5dfa6ce913ecaf00b25a0e88bcf46096eeb`；基线必须是当前 `HEAD` 的祖先
- 实施 worktree：`D:\Users\yuqi.chen\offerpilot\.worktrees\refactor-20260823-haru-surface-completion`
- 允许修改范围由 `assistantSurfaceGate.test.ts` 的 canonical allowlist 固定，SHA-256 为 `154a203e19bf57f54bcb4de5af182fca0da861a54e6cbfd822b7f3c32cce5307`

## 1. 目标与边界

OfferPilot 桌面端需要一个稳定的 Haru surface：Haru 作为常驻桌面陪伴入口，Chat drawer 作为可聚焦的对话窗口，Pilot workspace 作为完整任务页。三种 surface 共享一个 Provider 和一个 conversation controller；业务页面只提交上下文和打开意图，不拥有第二份请求、通知或生命周期状态。

本次只完成前端 surface、设置中的 Haru/外观入口、AppShell 接线和相应测试文档。保留现有 Chat API、SSE 解析、HITL 确认和后端领域模型，不新增 API、数据库表、Agent runtime、Journal 或移动端产品依赖。

## 2. 不变量与所有权

### 2.1 Provider

`AssistantSurfaceProvider` 是唯一的 surface 状态边界，负责：

- 当前 surface（mascot、Haru chat、Pilot workspace）与打开/关闭动作；
- completion notice 的 SPA 内存态、dismiss 和“查看原对话”入口；
- request key 的一次性交接，消费后不可重复消费；
- 全局任务状态与一次请求一次 terminal lifecycle 去重。

Provider 不执行网络请求，也不解析 SSE；它只接收 controller 的状态报告并发布 context value。

### 2.2 Controller

`usePilotConversationController.ts` 是唯一的 Chat transport owner：

- 继续调用现有 `streamChat` / `streamConfirmAction`；
- 继续持有 AbortController、pending/HITL、conversation 选择和 stop 行为；
- 通过绑定的 task-state reporter 向 Provider 报告 running、completed、failed；
- 对每个请求只允许一次 terminal lifecycle，取消/关闭不得伪造 completed；
- 只在 controller 内导入 `@/services/chat`，surface 组件不自行 `fetch`、读取流或创建 EventSource。

Haru 的 UI 组件只消费 context/controller actions。`AppShell` 只负责将页面上下文、附件和导航意图传入 surface，不保留 `pilotMascotNotification`、conversation request 或 reply lifecycle 的镜像 state/ref。

## 3. Surface 行为契约

### 3.1 打开与关闭

- Haru anchor 在桌面内容区右下角，与主内容和设置入口同层，不遮挡主操作；紧凑桌面允许退化为 rail，但不得出现第二个 Haru。
- 点击 Haru 打开 drawer；“展开到 Pilot”只改变 surface，不创建新 conversation，不丢失草稿上下文或附件。
- drawer close 只关闭 surface，不停止活动请求、不清空 conversation、不丢失 pending/HITL。停止必须由显式 Stop action 触发。
- completion notice 的主操作只打开原 conversation；notice dismiss 只清除提示，不删除消息。

### 3.2 请求生命周期

一次请求的可观察序列为 `idle → running → completed|failed → idle`；completion notice 可在任务状态回到 idle 后继续保留，直到用户查看、关闭或发起下一次请求。无 terminal lifecycle 的 confirmation/undo lease 也会在结束后回到 idle。同一 request generation 的重复回调只能产生一个通知；前台 surface 已可见时不再制造重复 notice。停止、关闭和切换页面都保留真实的 controller 状态。

原对话 notice、后台完成和失败恢复均为当前 SPA 会话内状态。持久去重、跨刷新恢复和服务端通知不在本阶段范围。

### 3.3 Context identity

页面上下文的 identity 使用 `page/view + entity.kind + entity.id + semantic filters`；展示 label 变化不能被当作新 identity。controller 同时维护：

- following context：页面当前希望使用的上下文；
- pinned conversation context：当前 conversation 已绑定的上下文；
- request snapshot：发送时冻结的上下文与附件。

上下文发生变化且 conversation 仍绑定旧上下文时展示 change notice，给出“切换到当前页面”和“保持原上下文”两条明确动作。存在活动请求或 pending confirmation 时禁止切换；切换后只改变后续发送的 pinned context，不回写已完成消息。

## 4. 可访问性与偏好

- Haru、drawer、notice、context notice、Stop 和 HITL 操作必须有可读的名称、可见焦点和正确的 button/dialog 语义。
- drawer 是 modeless dialog：打开时焦点进入可操作区域，背景页面仍可操作；Escape 关闭当前 surface，不能误触发 stop 或确认；Tab 顺序稳定且不进入不可见 surface。
- assistant streaming/live lifecycle 使用 `aria-live` 的低噪声区域，状态文案不重复朗读整段消息。
- 所有动效遵守 `prefers-reduced-motion`；设置中的 Haru animation preference 与 visible/zoom preference 使用同一持久化边界。关闭动画不应改变请求或上下文语义。
- 768–1179px 为紧凑桌面，`>=1180px` 为主要桌面验收宽度；不新增移动端布局或依赖。

## 5. 允许路径与机械门禁

允许路径只有：

```text
web/src/features/assistantSurface/**
web/src/features/pilotMascot/**
web/src/layout/AppShell.tsx
web/src/layout/AppShell*.test.*
web/src/components/SettingsView.tsx
web/src/components/SettingsView*.test.*
docs/superpowers/specs/2026-08-23-haru-surface-completion-design.md
docs/superpowers/plans/2026-08-23-haru-surface-completion.md
docs/reports/2026-08-23-haru-surface-completion-verification.md
```

gate 合并 `BASELINE..HEAD` committed diff、unstaged diff、staged diff、normal untracked，以及上述三个 ignored docs 的存在性；rename 按 old/new 路径展开。任何其他路径立即失败。gate 还固定 baseline ancestor 和 allowlist hash。

源码负向边界检查生产 `.ts/.tsx`（排除测试）并要求：Chat service/import 与 stream owner 只有 controller；surface 之外不出现 `EventSource`、`fetch`、`ReadableStream`、`getReader`；不出现 Journal、AgentRun、Ledger、ToolMessage、`variant="haru"` 或 `runId` 镜像；只有一个 Provider/controller construction owner；AppShell 不保留重复通知/request/lifecycle 状态；`web/package.json` 不引入 React Native/Capacitor 运行时。

## 6. 测试与验收

合成测试不包含真实用户内容、密钥或业务数据。定向 gate 至少覆盖：

- baseline ancestor、精确 allowlist 和 canonical hash；
- committed/staged/unstaged/untracked 路径合并检查；
- Chat transport ownership 与负向源码边界；
- Provider/controller 单 owner 和 AppShell 单挂载；
- 移动依赖排除。

功能测试继续覆盖 reducer、Provider、Golden、HaruChatWindow、PilotMascot preference/runtime/safe-area、keyboard/focus、context notice 和一次请求一次 lifecycle。完整交接还需前端 test/build、后端兼容 gate、ruff/mypy、静态 smoke 与内置浏览器在 768、1024、1440 宽度走查。

## 7. 明确延期能力

以下能力不是本次 surface completion 的隐含交付项，必须保持 deferred：

- Compact Confirmation 新 UI；
- Journal/AgentRun/Ledger UI、后台 Agent queue、SSE replay；
- 持久化通知去重、跨刷新 completion notice、服务端推送；
- 自动切换上下文、自动发送、无 HITL 的写操作；
- 正式 `interview_notes` / `mock_sessions` 数据模型；
- 移动端 surface、React Native/Capacitor 依赖；
- 新 Chat API、SSE parser、数据库迁移或领域模型改名。

## 8. 最终集成顺序

1. 先在固定基线上落地 Task 0 gate、spec 和 plan，并确认 gate 能在当前未完成实现上给出可解释的 RED。
2. 先完成 Agent Loop/runtime owner 的上游集成；该集成完成前不宣称组合验证通过。
3. 在同一 worktree 接入 Provider/controller、surface UI、AppShell 单 owner、context notice、Haru placement、偏好和可访问性测试。
4. 先跑定向 RED/GREEN，再跑前端 build、后端兼容 gate、ruff/mypy、静态 smoke 和浏览器验收。
5. 做独立 CR，修复 P0/P1/P2 或记录剩余风险；重新执行受影响 gate。
6. 最后更新 verification report，记录最终 commit、allowlist hash、Runtime integration baseline、测试证据和 deferred 清单，再由 root 统一集成；本子任务不 merge、不 push。
