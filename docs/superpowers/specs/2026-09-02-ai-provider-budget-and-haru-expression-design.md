# AI Provider 预算配置与 Haru 表情修复设计

状态：已批准

## 背景

当前 Provider Profile 已有 `context_window` 与 `max_output_tokens`，但设置 API 和前端没有读写这两个字段。旧配置中的 `0` 会在 Agent Context Projector 内被兼容解释为 `32,768 / 4,096`，完整工具面与必保上下文可能因此在联网前触发 `mandatory_surface_over_budget`。现有错误文案把该本地预算失败显示为“AI 连接失败”，无法指导用户修正配置。

同一失败会让 Haru 长时间处于 `error` activity。Live2D runtime 把 `error` 映射到持续的 `f02` 表情；该资源会张嘴、抬眉，视觉上像固定惊讶。错误文字仍应保留，但装饰性表情不应持续占用角色脸部。

## 目标

- 在每个 Provider 配置中展示并保存必填的上下文窗口与单次最大输出 token 数。
- 新增、编辑、测试连接和保存时拒绝缺失、非整数或非正数预算。
- 旧 `0` 配置仍可加载并进入设置页面，但必须显示“配置待补全”，用户补全前不能通过保存或连接测试。
- Agent 预算不足时使用可操作的中文配置提示，不泄露 Provider、Prompt 或密钥细节。
- Haru 的错误表情只作为短暂反馈；错误文字和状态仍持续存在，随后脸部回到 neutral。
- 不改变数据库 Schema、Provider 工具契约、Agent/HITL/Ledger/SSE 行为或密钥存储方式。

## Provider 预算契约

### 数据边界

`AIProviderProfile` 继续保留兼容默认值 `0`，以便旧配置不阻止应用启动。设置 GET、设置备份和连接测试草稿必须显式携带：

```text
context_window: integer
max_output_tokens: integer
```

设置 PUT 中只要提交 `providers`，每个启用的 Provider 都必须满足：

```text
context_window > 0
max_output_tokens > 0
context_window > max_output_tokens + framing_reserve
```

禁用 Provider 可以保留 `0`，但启用或设为 active/fallback 前必须补全。未提交 `providers` 的旧式设置更新继续保留已有 Provider 数值，不因其他设置保存而清零。

### 前端

Provider 编辑区新增两个整数输入：

- `上下文窗口（tokens）`
- `单次最大输出（tokens）`

启用 Provider 时均为必填正整数，并校验输出必须小于上下文窗口。配置为 `0` 的旧 Provider 显示“预算配置待补全”。保存、应用到 Provider 列表和测试连接共用同一校验，payload 原样携带数值。

本期不内置模型名称到窗口大小的自动映射。用户应按实际 Provider/模型文档填写，避免把“现代模型通常支持大窗口”误写成所有 Provider 的统一事实。

### 运行时与错误文案

保留旧 `0 → 32,768 / 4,096` 的 Agent 运行时兼容解释，避免本次小修扩大为全量配置迁移；但设置页面会把 `0` 显示为待补全，并阻止连接测试。用户保存正整数后，Context Projector 使用显式值。

以下本地投影错误统一显示为“模型上下文配置不足”，并指向 AI 设置中的两个字段：

- `mandatory_surface_over_budget`
- `invalid_provider_budget`
- `adapter_context_window_exceeded`

错误仍保持现有 HTTP/SSE failure envelope，不增加 Provider 调用、fallback 或重试。

## Haru 表情契约

`error` activity 继续存在，文字、错误标识和用户可操作入口不变。Live2D 的 `f02` 只播放一次并在固定 1 秒后恢复 `neutral`，与 `speaking/success` 的瞬态表情使用同一清理机制。

规则：

- `error → neutral` 的计时只影响表情，不改 activity，不清除错误。
- 新 activity 到达时取消旧 timer，并按新状态选择表情。
- dispose 时取消 timer。
- reduced-motion 或 animation off 下不播放表情，保持既有静态语义。
- 文本继续是错误状态的权威表达，Live2D 失败仍 fail-open。

## 验证

- 后端：设置 GET/PUT/backup round-trip、旧式 PUT 保留、启用 Provider 非法预算、连接测试 Provider 0 调用、密钥脱敏。
- 前端：类型与 payload、两个必填整数输入、旧 0 待补全、启用/禁用校验。
- Runtime：预算错误中文映射，不改变普通 Provider 错误。
- Haru：`error` 立即 `f02`，1 秒后 neutral；新 activity 和 dispose 取消迟到 reset。
- 浏览器：设置页可见并保存两个字段；重现错误后 Haru 短暂反馈并恢复 neutral，错误文字仍保留。

## 非目标

- 不自动探测模型窗口。
- 不把所有旧配置自动迁移到 256K。
- 不改变 Token estimator、Product input cap 或完整工具面。
- 不清除用户的错误记录，也不改变 Haru 的其他状态含义。
