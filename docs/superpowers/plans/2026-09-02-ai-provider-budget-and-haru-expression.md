# AI Provider 预算配置与 Haru 表情修复实施计划

## Task 1：冻结设置契约

1. 在 `tests/test_settings_api.py` 增加 GET/backup 返回预算、PUT round-trip、非法启用 Provider 和连接测试不联网测试。
2. 运行定向测试确认 RED。
3. 修改 `src/offerpilot/api.py`，完整读写字段并实现封闭校验。
4. 运行定向测试确认 GREEN。

## Task 2：补齐前端设置

1. 在 `web/src/components/AISettingsDrawer.test.ts` 与 service 测试中增加字段、必填规则和 payload 门禁。
2. 运行 Vitest 确认 RED。
3. 修改 `web/src/services/chat.ts` 与 `web/src/components/AISettingsDrawer.tsx`，加入类型、整数输入、待补全提示和启用 Provider 校验。
4. 运行定向 Vitest 与 TypeScript。

## Task 3：修正预算错误表达

1. 增加 `_provider_error_message` 定向测试，锁定预算错误与普通 Provider 错误的不同文案。
2. 运行测试确认 RED。
3. 修改 `src/offerpilot/pilot_runtime/composition.py`，仅映射封闭预算错误 code。
4. 运行 Pilot Runtime / Chat API 定向测试。

## Task 4：修复 Haru 固定错误表情

1. 在 `live2dRuntime.test.ts` 增加 `error → f02 → neutral`、activity replacement 和 dispose 测试。
2. 运行测试确认 RED。
3. 修改 `live2dRuntime.ts`，将 error 纳入瞬态 expression reset。
4. 运行 mascot focused tests。

## Task 5：集成验证与验收

1. 运行 Ruff、Mypy、相关后端矩阵、前端全量测试与生产构建。
2. 启动隔离分支本地服务，用内置浏览器验证设置字段和 Haru 表情恢复。
3. 运行 `git diff --check`。
4. 启动独立代码复审，关闭所有 P0/P1/P2。
5. 分开 `git add` 与 `git commit`，提交实现和验收结果；不 push、不 merge。
