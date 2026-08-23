# Haru Desktop Surface Completion 验证报告

## 验证基线与范围

- 分支：`refactor/20260823-haru-surface-completion`
- 固定 baseline：`aaecf5dfa6ce913ecaf00b25a0e88bcf46096eeb`
- allowlist canonical hash：`154a203e19bf57f54bcb4de5af182fca0da861a54e6cbfd822b7f3c32cce5307`
- 验证日期：2026-08-23 至 2026-08-24（Asia/Shanghai）
- 本报告仅覆盖桌面前端 Surface、上下文切换、状态所有权、定位、焦点、可访问性和本地外观设置。

## 实现核对

- `AssistantSurfaceProvider` 统一拥有 Surface、任务展示、SPA 内 completion notice、通知对应 Conversation 和打开原 Conversation 的动作。
- AppShell 只挂载一个稳定 `PilotWorkspace` / `ChatPanel` owner；page、rail、drawer 只改变呈现，不因 Surface 切换重新挂载或创建第二条请求路径。
- Offer scope 仅在 idle 时退出；active request、原始 Pending 与会话恢复得到的 hydrated Pending 均阻止关闭动作提前清理上下文。
- 普通回复、确认与撤销共用单 request lease；确认/撤销按 `running -> completed|failed -> idle` 上报，terminal 通知按 request generation 去重。
- 已有会话使用封闭的 view/entity kind/entity ID 比较；页面变化只显示显式切换提示，运行中请求继续使用冻结的 `requestContextSnapshot`。
- Haru 小窗由真实 anchor rect 定位，确定性选择左右和上下展开方向，并保持 12px 视口安全边距。
- Haru 是 modeless dialog；打开聚焦输入框，Escape 关闭并把焦点还给 Haru，展开和 Pending 跳转把焦点交给 Pilot。
- 本地设置支持显示/隐藏、角色大小、重置位置、完整/简洁/关闭动画，并动态遵循系统 reduced-motion。
- Live2D 加载失败时保留原生 Haru button fallback。

## 自动化验证

| 命令 | 结果 |
| --- | --- |
| `cd web; npm test -- --run` | 184 files、1325 tests 全部通过 |
| `cd web; npx tsc -b --pretty false` | 通过 |
| `cd web; npm run build` | 通过，3954 modules transformed |
| `uv run pytest tests/test_chat_api.py -q` | 360 passed；仅既有 FastAPI/Starlette deprecation warnings |
| `uv run ruff check .` | `All checks passed!` |
| `uv run mypy src` | 129 source files，无问题 |
| `uv run oc smoke --static-dir web/dist` | health、SPA、写入确认和卡片 smoke 全部通过 |
| `git diff --check` | 通过；仅 Git 的 LF/CRLF 工作区提示 |

生产构建仍报告既有主 chunk 大于 1500 kB 的警告；本期未增加 bundle 拆分工作。

## 浏览器验收

使用内置 Codex browser 对真实构建和本地 API 走查：

- 768 × 900：Haru anchor 位于 `611.75..727.75`，小窗位于 `212..599.75`，间距 12px，无横向溢出。
- 1024 × 900：Haru anchor 位于 `835..951`，小窗位于 `431..823`，间距 12px，无横向溢出；打开后 composer 获得焦点。
- 1440 × 900：Pilot page、隐藏 Haru 后的 380px rail 和主内容均无横向溢出；主工作区不被导航或滚动条遮挡。
- 顶部 anchor 会向下展开；左右拖动后小窗按 anchor 侧向重新定位；resize 后重新计算。
- 最终构建在 1280 × 720 再验：小窗 bounds 为 `left=687, top=88, right=1079, bottom=708`；打开聚焦 composer，Escape 后焦点返回 Haru；展开后焦点进入 Pilot textarea。
- 已有 Conversation 与设置页之间能显示“当前会话 / 当前页面”的显式上下文差异；切换动作不清空会话内容、附件或 Pending。
- Haru 打开、关闭、再次打开和 Haru -> Pilot 展开期间，服务端日志没有新增 Chat API 或 SSE 请求。
- 浏览器控制台无新增 error/warn；Live2D 可正常加载，fallback 由组件测试覆盖。

## 独立 Code Review

最终独立 CR 对单 owner、关闭/展开不中止、Offer scope、hydrated Pending、上下文冻结、completion notice、Pending 焦点、定位、可访问性、设置和 allowlist 逐项复审，结论为无开放 P0/P1/P2。相关 AppShell 与 allowlist/gate 定向复核 45/45 tests 通过。

## 破坏性变化

- 无 API、SSE、Pending payload、数据库或 migration 变化。
- 无本地数据破坏性迁移。
- AppShell 内部 Assistant 状态所有权和 ChatPanel 挂载结构已收口；这属于前端内部实现变化。

## 剩余风险与后续顺序

- Compact Confirmation、`ActionPresentationPolicy`、持久主动提醒、Journal/Run 恢复、SSE replay 和后台 Agent Queue 明确未实现。
- 本分支不得直接合并；需等待 Agent Loop 合并 main 后再更新本分支、解决组合冲突并重跑完整前后端与浏览器矩阵。
- 对话列表的行按钮命中区在窄栏自动化点击时可能与会话行重叠；该既有 ChatPanel 几何问题不在本期 allowlist，未在本分支扩改。
- 前端完整测试仍输出既有 jsdom/React `act(...)` 警告，但没有失败用例。

## 完成声明

Haru 桌面 Surface、上下文切换、状态所有权、定位和可访问性完成收口。

不声明原始 Haru/Pilot 全部设计已完成。
