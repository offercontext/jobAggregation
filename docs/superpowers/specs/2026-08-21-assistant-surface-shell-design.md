# Assistant Surface Shell 与 Haru Chat MVP 设计

状态：已确认，可实施
日期：2026-08-21
固定基线：`b05d915bbb52b2740f6801b4ec46ee8f4ccda2e2`

## 1. 目标

OfferPilot 只保留一套前端会话所有权。`AssistantSurfaceProvider` 持有唯一的 `usePilotConversationController()` 实例，Haru 小窗与 Pilot 工作区只负责呈现同一份会话、消息、Pending、请求、附件草稿和上下文状态。

用户形成单一认知：平时问 Haru，复杂任务进入 Pilot。界面切换不是任务切换，关闭界面也不是停止任务。

## 2. 固定产品关系

- Haru 是助手形象、状态提示和轻量交流入口。
- Pilot 工作区承载长对话、历史、附件、复杂确认和完整上下文管理。
- 同一时刻只显示 `mascot`、`haru_chat`、`pilot_workspace` 三种主要界面之一。
- `idle`、`running`、`waiting_confirmation`、`completed`、`failed` 是唯一的前端任务状态。
- Haru 与 Pilot 使用同一个 `conversation_id`、消息列表、Pending、当前请求和取消控制。

## 3. 架构

```text
AppShell
  -> AssistantSurfaceProvider
       -> usePilotConversationController (唯一实例)
       -> HaruDock / HaruChatWindow
       -> PilotWorkspace / ChatPanel
            -> 现有 Chat HTTP、SSE、HITL service
```

`usePilotConversationController` 从 `ChatPanel` 抽取会话状态和副作用，包括：

- 会话列表、当前会话、消息加载、恢复与切换；
- 普通发送、SSE delta、单一 AbortController 和停止；
- Pending、批准、修改、拒绝、重试、撤销与对账；
- 当前页面上下文、执行期上下文快照和附件草稿；
- 当前错误、任务状态、后台完成通知和瞬态请求状态。

`ChatPanel` 保留呈现专属状态，例如 drawer 尺寸、上下文面板展开和滚动锚点。它不再创建会话 Controller。独立渲染 `ChatPanel` 的旧测试通过兼容边界自动提供一个局部 Provider；AppShell 中只能由顶层 Provider 提供共享实例。

## 4. Surface 状态机

```text
mascot --点击 Haru--> haru_chat
haru_chat --展开--> pilot_workspace
pilot_workspace --关闭--> mascot
haru_chat --关闭--> mascot
任意对话界面 --旧 Pilot 深链/快速打开--> pilot_workspace
```

切换 Surface 只改变呈现，不得调用 Chat service、创建 SSE、清空消息、清空 Pending 或变更会话。`view=pilot` 继续作为完整工作区的兼容路由；进入该 view 时 Surface 同步为 `pilot_workspace`，离开后 Controller 继续存活。

## 5. 请求与上下文

- 当前请求由 Controller 中唯一的 `AbortController` 表示。
- Haru 关闭后 Provider 不卸载，现有请求继续；只有显式停止或既有替换语义可以调用 `abort()`。
- SSE 返回的 `run_id` 仅保存在当前请求内存中，不生成、不写 LocalStorage、不作为刷新恢复凭据。
- 请求开始时冻结 `page_context`、会话上下文和附件快照。执行期间页面切换只更新下一次请求的 following context，不污染当前请求。
- UI 状态不读取 Durable Journal。状态只来自当前请求、Pending、ChatMessage、Ledger 现有投影或领域 Job。

## 6. Haru Chat MVP

Haru 小窗包含：

- 最近 8 条可见消息，使用同一 `UITurn`；
- 文本输入、发送、SSE 流式内容和显式停止；
- 当前上下文标签、确定性结果裁剪、任务状态；
- 展开到 Pilot 和关闭；
- 成功/失败仅一次的 `aria-live="polite"` 通知；
- Live2D 失败后的普通 Haru 按钮。

Haru 不呈现附件编辑、会话列表、复杂确认或写操作表单。出现 Pending 时显示“有一项操作等你确认”和“到 Pilot 查看并确认”，点击后打开同一会话的 Pilot 工作区。

紧凑内容仅执行确定性裁剪，不新增 Provider 调用或第二套摘要请求。

## 7. 导航与兼容

一级导航调整为：

1. 今日：`dashboard`，提醒通过今日入口可达；
2. 投递：`board`，看板与列表保留同模块视图切换；
3. 面试：`interview`，题库通过面试模块可达；
4. 资料：`resumes`，简历与知识资料在同模块可达；
5. 设置：`settings`，保持底部弱入口。

`pilot` 不再出现在一级导航，但 `ViewMode='pilot'`、旧页面跳转和深链继续工作。命令面板改名“快速打开”，保留“问 Haru”和“打开 Pilot 工作区”。旧 `board`、`applications-list`、`questions`、`knowledge`、`reminders` 等 ViewMode 不删除。

## 8. 可访问性与视觉

- 延续 React 18、Ant Design、CSS Modules、现有图标和 Live2D/Pixi，不新增设计系统或全局状态库。
- 视觉为安静、任务导向的既有产品演进：设计变体 4、动效 3、信息密度 5。
- 交互控件最小命中区 40px；标题使用平衡换行，正文使用 pretty wrapping。
- 打开 Haru 后焦点进入输入框；Escape 关闭并将焦点还给 Haru；展开后焦点进入 Pilot 输入区或消息区。
- 默认无持续动画，状态变化只用可中断的 transform/opacity 动效；`prefers-reduced-motion` 下禁用位移和 Live2D 状态动画。
- Pilot 全屏时隐藏 Haru 本体，设置页只保留弱入口。

## 9. 不实施

- Compact Confirmation 与任何前端风险推断；
- 主动业务提醒、稍后提醒和跨刷新活动请求恢复；
- 第二次模型摘要、第二套 Chat service 或第二套 SSE parser；
- 后端、数据库、Tool Pipeline、Ledger、Journal、Provider 或 API/SSE Schema 修改；
- 投递详情、今日页、面试业务、简历编辑器或 Knowledge 模型的全面重写。

## 10. 验收

- 只有一个 Controller 和一条活动 SSE；Haru/Pilot 展开不重发、不丢 delta。
- Haru 关闭后同一 SPA 请求继续；显式停止只调用一次真实取消。
- Pending 在 Haru 中只引导到 Pilot，approve/modify/reject 继续使用既有契约。
- 页面 context 支持 following/pinned，执行期切页不污染已发送 payload。
- Live2D 失败 fallback、reduced motion、Escape 和焦点返回可用。
- Pilot 旧 route/ViewMode、看板/列表筛选状态和旧入口映射继续有效。
- 源码不存在 Journal UI 状态读取、`variant="haru"`、第二套 Chat service 或第二个 SSE parser。
- 全量前端测试、构建、指定后端测试、ruff、mypy 和静态 smoke 通过；浏览器完成宽屏与 768-1179px 验收。
