# Assistant Surface Shell 与 Haru Chat MVP 验证报告

- 日期：2026-08-21
- 分支：`refactor/20260821-assistant-surface-shell`
- Worktree：`D:\Users\yuqi.chen\offerpilot\.worktrees\refactor-20260821-assistant-surface-shell`
- 基线：`b05d915bbb52b2740f6801b4ec46ee8f4ccda2e2`

## 验证结果

| 范围 | 命令或方式 | 结果 |
| --- | --- | --- |
| 前端完整测试 | `cd web && npm test -- --run` | 通过：171 个文件、1251 个测试 |
| 助手回归测试 | `cd web && npm test -- --run src/components/ChatPanel/deterministicPilotConfirmation.test.tsx src/components/ChatPanel/layout.test.ts src/features/assistantSurface/assistantSurfaceGolden.test.ts src/features/assistantSurface/HaruChatWindow.test.tsx src/features/assistantSurface/AssistantSurfaceProvider.test.tsx` | 通过：5 个文件、76 个测试 |
| TypeScript | `cd web && npx tsc -b --pretty false` | 通过 |
| 前端生产构建 | `cd web && npm run build` | 通过：3950 个模块；保留现有大 chunk 警告 |
| Chat API | `uv run pytest tests/test_chat_api.py -q` | 通过：347 个测试；输出 2125 条既有弃用警告 |
| Python lint | `uv run ruff check .` | 通过 |
| Python 类型检查 | `uv run mypy src` | 通过：119 个源文件无问题 |
| 静态与 API smoke | `uv run oc smoke --static-dir web/dist` | 通过：health、SPA fallback、投递创建、确认写入、Pending 清理及确认卡字段均通过 |
| Diff 格式 | `git diff --check` | 通过 |

完整前端测试输出包含仓库既有的 React `act(...)`、jsdom `getComputedStyle` 和非布尔 DOM 属性警告，但进程退出码为 0，没有失败用例。

## 浏览器走查

使用内置 Codex Browser 对 `http://127.0.0.1:8080/` 的生产构建进行真实走查，结果如下：

- 主导航显示“今日 / 投递 / 面试 / 资料 / 设置”，没有顶层 Pilot 导航。
- “快速打开”包含“问 Haru / 打开 Pilot 工作区 / 打开设置”。
- Haru 角色和 Live2D 素材正常加载；轻量对话展示当前上下文、任务状态与附件数量。
- Haru 可展开至 Pilot 工作区；展开后没有新增请求或重复 SSE 的可见迹象。
- Pilot 与 ContextPanel 的设置入口统一为“打开设置”，并进入统一设置页。
- 投递页的看板与列表可在同一入口切换；列表保留搜索、状态和排序控件。
- 浏览器控制台 `error` / `warn` 日志为空。

由于验证数据目录未配置 API key，浏览器走查没有发送真实模型请求；请求租约、显式停止、后台继续、Pending 跳转和过期响应隔离由自动化测试覆盖。

## Code Review

- 独立子代理完成多轮 CR。
- 已修复：confirmation 进行中创建新会话导致的隐藏租约、selection/request 并发 busy 竞态、重复 start request effect、过期新会话响应覆盖当前会话、历史会话的 draft context 残留、统一设置文案等问题。
- 最终复核结论：无剩余 P0/P1/P2。
- Controller 负责共享会话状态、请求租约、SSE 服务、上下文快照和停止语义；ChatPanel 保留领域事件到 UI 状态的编排与渲染绑定。后续若进一步收紧模块边界，可把剩余编排函数继续下沉，但当前共享入口不再各自持有请求或 SSE 生命周期。

## 已知风险

- `npm ci` 的 audit 汇总为 13 个依赖漏洞（3 moderate、7 high、3 critical）；本次没有执行可能引入破坏性升级的 `npm audit fix`。
- Vite 报告主 chunk 约 1.52 MB，超过当前 1.5 MB 警告阈值；不影响构建通过。
- Docker smoke 未运行；本次使用仓库的本地 `oc smoke` gate。

## 破坏性变化

无后端 API、数据库 schema 或数据迁移变化。主导航和助手入口的信息架构调整属于本次明确要求的用户界面变化；旧 Pilot 路由继续保留。
