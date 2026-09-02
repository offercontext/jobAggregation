# Offer 谈薪准备到 Pilot 对话交接设计

**状态：已批准**

## 目标

保留 Offer 卡片唯一主入口“准备谈薪”，在结构化谈薪任务中恢复“和 Pilot 深聊这份 Offer”的次级入口。交接只切换到既有 Haru/Pilot Conversation owner，不新增 Provider、SSE、Conversation 或写入路径。

## 交互

```text
Offer 卡片
  → 准备谈薪
  → 结构化谈薪准备
  → 和 Pilot 深聊这份 Offer
  → 既有 nego_coach 对话
```

- 次级入口位于谈薪步骤卡内，不回到 Offer 卡片与主入口竞争。
- 点击后带入可信的当前 Offer attachment、Application scope，以及用户已填写的目标、顾虑和沟通场景；同一 Application 存在多份 Offer 时，点击的 Offer 替换任何 ambient Offer attachment。
- 带入内容只成为可编辑的输入框草稿；用户显式发送前，消息、Provider、SSE 和工具调用均为 0。
- 结构化任务先通过既有 Core Task close/recovery 协议关闭，草稿继续由 AppShell owner store 保留。
- 历史视图、未绑定 Offer、结果未知、执行中或来源已变化时不提供交接入口。
- 若 Pilot 已有进行中的请求或待确认操作，保持当前谈薪任务并提示用户先处理，不替换或中断既有请求。

## 状态与所有权

- `OfferNegotiationDrawer` 只发出 typed handoff intent，不访问 Chat service。
- `ApplicationDetail` 校验当前 Core Task generation，并在 handoff 成功后关闭当前 owner。
- `AppShell` 校验 Offer/Application 归属和 Pilot 空闲状态，签发新的 `ChatStartRequest`。
- `ChatStartRequest.composerDraft` 与既有 `initialMessage` 正交：前者只预填，后者仍保留自动发送语义。
- Composer 草稿由唯一 `PilotConversationController` 瞬态持有，并绑定 `application + offer + mode` scope。Haru 与展开后的 Pilot 在同一 scope 读取同一份草稿；切换其他 Offer 或通用 Chat 时清除，不进入 ChatMessage、Pending、Ledger、Journal 或本地持久化。
- 精确 Offer attachment 随该 draft context 进入首次及后续显式发送；它只为 Context Projector 提供经服务端校验的事实来源，不绕过 Tool Pipeline 的 binding/capability 校验。
- 新建对话、选择已有对话或成功发送后清除共享草稿；发送失败保留草稿。

## 文案与无障碍

- 按钮：`和 Pilot 深聊这份 Offer`
- 说明：`会带入当前 Offer 和已填写内容，消息由你决定是否发送。`
- 使用语义化 Button、可见键盘焦点与现有 Ant Design/CSS token；不增加持续动画。

## 验收

- Offer 卡片仍只有一个“准备谈薪”主入口。
- Drawer handoff 只触发一次 typed callback，不调用谈薪或 Chat service。
- 新请求使用 `mode=nego_coach`、当前 Application scope 和唯一准确 Offer attachment；两份 Offer 属于同一 Application 时也不得漂移。
- 目标、顾虑、场景进入共享输入草稿，Haru/Pilot 切换不丢失。
- 关闭后打开通用 Chat 或另一份 Offer 时不保留旧谈薪草稿；关闭后回到同一 Offer 时可继续编辑。
- 点击交接本身 Provider/SSE/消息为 0；显式发送后恰好调用一次现有 Chat 路径。
- stale generation、busy/result-unknown/source-changed/history/unbound/Pilot busy 均 fail-closed。
