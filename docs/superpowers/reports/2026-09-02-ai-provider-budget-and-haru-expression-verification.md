# AI Provider 预算配置与 Haru 表情修复验收报告

## 范围

- 分支：`fix/20260902-ai-budget-haru-expression`
- 固定 baseline：`7fd8953ee4b7fc9c1ff01c9c64d2e3957bffce40`
- 设计提交：`a64540e docs: AI 设计模型预算配置与 Haru 表情修复`

## 改动

- AI Provider 设置、备份、连接测试与前端类型完整携带 `context_window`、`max_output_tokens`。
- 启用、默认或 Fallback Provider 必须提供正整数预算，且上下文窗口必须大于最大输出与固定协议预留之和。
- 历史 `0/0` 配置仍可加载；未被选择的禁用 Provider 可以保留待补全状态。旧式设置更新不会清零或无故阻塞既有配置，但新的 active/fallback 选择及顺序变化必须通过预算校验。
- 预算失败使用专门的用户文案，引导回到 AI 设置，不改变普通 Provider 错误的既有安全表达。
- Haru 的 `error` 表情从永久 `f02` 改为一秒后回到 neutral；错误状态、提示卡和重试入口继续保留。

## 验证

- 后端 Settings API：`41 passed`。
- 前端全量：`2116 passed / 2 failed / 1 teardown error`。两项失败分别为历史 Desktop Task Flow 固定 allowlist 拒绝本分支后端路径，以及并行全量运行下 Review Readiness 性能审计 `46.741s > 30s`；teardown error 来自无关的 Adaptive Practice 通知定时器。
- Review Readiness 性能门禁单独运行：`133 passed`，生产审计 `14.327s < 30s`。
- Adaptive Practice 文件单独运行：`23 passed`，无 teardown error。
- 前端定向：AI 设置、service/diagnostics 合同与 Haru Runtime `46 passed`。
- Ruff：通过。
- Mypy：通过。
- TypeScript：通过。
- Production build：通过；保留既有 `1,645.76 kB` 主 chunk warning。
- Static smoke：通过；两次验收产生的合成 Application `#267`、`#268` 已按产品删除语义清理。
- 内置浏览器：
  - AI 设置显示两个 token 必填项、历史 `0/0` 待补全提示和 Provider 标签；
  - 一条 Haru 真实 Provider 请求完成，随后使用离线 Transport 场景验证错误状态；
  - 服务离线时错误提示保持可见，等待超过一秒后 Haru 已恢复 neutral，不再固定为惊讶表情；
  - 测试创建的 Conversation `#649` 已删除。
- 独立 CR：最终 `APPROVED`，无开放 P0/P1/P2。
- `git diff --check`：提交前终检。

## 破坏性变化

无 Schema、Migration、公开 Chat/SSE、Pending、Ledger 或业务数据契约变化。新的设置保存契约要求所有启用、默认和 Fallback Provider 填写明确预算；旧配置仍可进入应用完成补全。

## 剩余风险

- Provider 的真实上下文能力仍由用户依据官方模型规格填写；系统不会根据模型名称猜测。
- Runtime 继续保留旧 `0 → 32768/4096` 兼容解释，以免旧配置阻止应用启动；未补全配置仍可能在真实请求时受到兼容窗口限制，但错误文案会明确引导用户修正。
- 前端完整门禁中的历史 Desktop Task Flow allowlist 只接受其冻结项目路径，本分支的后端文件会被预期拒绝；未修改该历史资产。
- 既有依赖审计和测试 warning 未在本期处理。
