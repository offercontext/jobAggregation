# Haru Desktop Surface Completion 验证报告

## 验证基线与范围

- 分支：`refactor/20260823-haru-surface-completion`
- 原始实现 baseline：`aaecf5dfa6ce913ecaf00b25a0e88bcf46096eeb`
- 当前组合 baseline：`7c36957176445c5213a31b013251fbbce8d610db`
- 当前 allowlist canonical hash：`b1698f9b89b23effcb6adc604c5d4457a26a36c6d2207b4bbe49c70cae290eb8`
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
- Agent Loop 与 Scoped Authority 前置主线已经合入当前历史；组合冲突、完整前后端矩阵和浏览器复核已在下方“主线组合收口”中关闭。
- 对话列表的行按钮命中区在窄栏自动化点击时可能与会话行重叠；该既有 ChatPanel 几何问题不在本期 allowlist，未在本分支扩改。
- 前端完整测试仍输出既有 jsdom/React `act(...)` 警告，但没有失败用例。

## 完成声明

Haru 桌面 Surface、上下文切换、状态所有权、定位和可访问性完成收口。

不声明原始 Haru/Pilot 全部设计已完成。

## 2026-08-25 主线组合收口

本分支已在 `Scoped Capability & Binding Enforcement` 最终提交
`7c36957176445c5213a31b013251fbbce8d610db` 之上完成 rebase。原 Haru 两个提交与
rebase 后提交通过 `git range-diff` 验证为补丁等价；组合期间未改变后端 API、SSE、
Pending、数据库或领域写入契约。

独立组合 CR 首轮发现并关闭两项问题：

- Pending confirmation 原先未进入 Haru 状态映射；现统一显示为
  `waiting_confirmation`，使用橙色状态点、明确文案和安全 Live2D 动作。
- 稳定挂载的 Chat owner 原先始终激活 transport；现只有 Pilot/Haru 对话可见、请求
  运行、确认保存或存在 Pending 时激活。关闭后的空闲 Haru 保持 owner，但不继续发起
  settings、Conversation、Chat 或 SSE 请求。

Windows 组合矩阵还发现授权 golden 在重新 checkout 后会被 `core.autocrlf` 改成 CRLF，
使 canonical byte gate 失败。`.gitattributes` 现将
`tests/fixtures/tool_authority/*.json` 固定为 LF；资产内容和 canonical fingerprint 均未改变。
Haru source gate 的组合 baseline、精确 allowlist 与 canonical hash 已同步更新，新的 hash 为
`b1698f9b89b23effcb6adc604c5d4457a26a36c6d2207b4bbe49c70cae290eb8`。

### 组合验证

| 命令或检查 | 结果 |
| --- | --- |
| Haru mascot / Live2D / AppShell 定向 Vitest | 4 files、47 tests 通过 |
| Haru source gate | 1 file、6 tests 通过 |
| Assistant Surface / Haru / AppShell / ChatPanel 组合 Vitest | 38 files、376 tests 通过 |
| 前端全量 `npm test -- --run` | 184 files、1326 tests 通过 |
| `npx tsc -b --pretty false` | 通过 |
| `npm run build` | 通过，3951 modules transformed；既有 1.55 MB 主 chunk 警告保留 |
| `uv run pytest tests/test_chat_api.py -q` | 369 passed |
| Agent Loop / Pilot Runtime / Tool Authority / Journal 组合矩阵 | 1429 passed；唯一 CRLF byte gate 失败已按根因修复 |
| 修复后 canonical private asset 定向回归 | 1 passed |
| `uv run ruff check .` | 通过 |
| `uv run mypy src` | 140 source files 通过 |
| `uv run oc smoke --static-dir web/dist` | 通过 |
| `git diff --check` | 通过，仅 Git 的 LF/CRLF 提示 |

内置浏览器使用隔离临时数据目录检查真实构建：Haru 能打开轻量对话；关闭后 1.2 秒观察窗
内没有新增 Chat/SSE 网络请求；控制台没有 error 或 warning。浏览器验收服务随后已停止。

最终独立组合 CR 无剩余 P0/P1/P2/P3。Docker daemon 和外置 Application-JD
baseline/allowlist 不属于本次组合输入，因此不宣称相应发布门禁通过。
