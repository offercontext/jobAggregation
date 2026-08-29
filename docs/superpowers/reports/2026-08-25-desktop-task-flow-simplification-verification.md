# Desktop Task Flow Simplification 验收报告

状态：Tool Metadata 已同步到当前分支；桌面任务流组合验收通过，具备本地合并条件。当前分支未 push、尚未合并到 `main`。

## 基线

- Baseline：`0c10e05e256eb757d5f89a8b009dcea193f2fc78`
- Tool Metadata 主线：`c845e83ff026ce5c1eb9a0a0cc51975f459f6f39`
- Desktop 独立实现：`78195000bd94fdfd6fe8508033e0a8687fde3323`
- 组合提交：`3b7864fa`（第一父提交为 Desktop，第二父提交为 Tool Metadata）
- Branch：`refactor/20260825-desktop-task-flow-simplification`
- Worktree：`D:\Users\yuqi.chen\offerpilot\.worktrees\refactor-20260825-desktop-task-flow-simplification`
- Tool Metadata 已先快进合并到本地 `main`，再以显式 merge commit 同步到本分支；桌面代码没有覆盖 Tool Metadata 的后端实现。
- 按 `package-lock.json` 执行了独立 `npm ci`；未修改依赖清单或 lockfile。安装审计仍报告 13 个既有依赖漏洞。

## 范围门禁

- Desktop 独立项目范围仍限定在 `web/**` 与本项目三份设计、计划、验收文档。
- 禁止触碰的 Chat transport、Tool metadata、Pending/HITL、HTTP/SSE、Controller ownership 文件均无 diff。
- `src/offerpilot/**`、数据库、migration、后端 schema、Ledger、Journal、Context Projector 与 Agent Runtime 均无 diff。
- `git diff --check`：通过。
- 组合后旧门禁按 `baseline..HEAD` 扫描时准确拒绝了 139 个 Tool Metadata 路径。该门禁已修正为固定、只读的历史项目范围 `0c10e05..7819500`，继续证明 Desktop 独立改动只落在批准范围，同时不把后续主线 merge 误归类为越界。
- `desktopTaskFlowGate.test.ts` 同时固定 baseline、项目终点、allowlist、forbidden paths、单一 Assistant owner、无 Chat transport 引入及主题/768 视觉门禁；不得自更新或扫描未跟踪文件绕过历史范围。

## 自动化验证

| 命令 | 结果 |
|---|---|
| `cd web && npm test -- --run` | 188 files / 1403 tests 全通过，1032.39s |
| `cd web && npx tsc -b --pretty false` | 通过；最终 production build 再次执行同一 TypeScript project build |
| `cd web && npm run build` | 通过，3957 modules，24.83s |
| `cd web && npx vitest run src/components/AddOfferForm.test.tsx src/components/offerWorkspaceModel.test.ts --maxWorkers=1 --minWorkers=1` | 最终类型收口后 2 files / 4 tests 全通过 |
| Chat API 组合矩阵 | 40 passed / 341 deselected；覆盖 sync/SSE、多 ToolCall、HITL、chained Pending、terminal replay 与 delivery recovery |
| Agent Loop / Pilot confirmation 组合矩阵 | 23 passed / 97 deselected |
| Context Projector / Tool Metadata 关键门禁 | 186 passed |
| `uv run ruff check .` | 通过 |
| `uv run mypy src` | 通过 |
| `uv run oc smoke --static-dir web/dist` | health、SPA fallback、写入确认与 Chat card smoke 全通过 |
| `git diff --check` | 通过 |

非阻塞 warning：

- Vitest 仍输出仓库既有的 React `act()`、测试 stub DOM 属性与 jsdom `getComputedStyle` warning；无测试失败。
- 首次全量 Vitest 在 Windows 并行进程中出现一次 `spawnSync git ENOENT`；同一 gate 隔离运行通过，未修改 gate，随后相同全量命令 188 / 188 files 全通过。该现象按进程环境瞬态记录，不作为产品代码失败掩盖。
- Chat API 测试输出 FastAPI / Starlette lifespan 与 TestClient deprecation warning；无测试失败。
- Vite 仍提示主 chunk 约 1531 kB，超过 1500 kB warning 阈值。

## 浏览器矩阵

使用内置 Codex Browser、最新 production build 与隔离的临时验收数据完成真实前端走查。

- 768 / 1024 / 1280 / 1440：逐一打开今日、投递、面试、Offer、素材库、设置；24 个组合均满足 `scrollWidth <= innerWidth`。
- 导航层级：主要任务 / 常用资料 / 辅助分组、页面相关 TopBar 操作与完整 `aria-label` 均可见。
- 今日：今日重点 / 提醒 / 日历可达；下一项真实行动、其他待办、近期日程与折叠分析层级正确。
- 投递：看板 / 列表切换同步 URL；详情概览 / 准备 / 进展三段可达，每个状态仅一个视觉 primary；进展仅展示只读投影。
- 面试：即将进行 / 已完成 / 自由练习三入口可达；题库与快速练习位于自由练习。
- 素材库：简历 / 经历素材 / 参考资料三个区域保持独立 route identity 与写入边界。
- Offer：用隔离数据逐项验证 0 / 1 / 2+ 状态；空状态、单份补全、多份比较、准备谈薪、返回所属投递均可用。
- 最终组合回归额外创建一份已绑定 Offer 与一份历史未绑定 Offer：已绑定记录保留谈薪、返回投递和详情；未绑定记录仅保留只读查看，表单字段全部禁用且无保存入口。
- 从同一 Offer 页面展开 Haru 与 Pilot 后，谈薪选择器只列出已绑定 Offer；历史未绑定记录不会进入 Context selector 或谈薪执行路径。
- Haru / Pilot：首次说明文案完整；从 Haru 展开 Pilot 后，服务端访问日志仅新增 conversation/settings 读取，没有新增 `POST /api/chat`、SSE 或 stream 请求；随后页面 context 切换也没有 Chat/SSE 请求。
- 旧深链：`?view=list`、`#/applications/list`、`/calendar`、`?view=offers`、`#/materials/reviews` 均进入正确工作区；Command Palette 仍保留看板、列表、日历、Offer、Haru、Pilot 等旧入口。
- 键盘：页面切换后主内容恢复焦点；投递 tabs 的 Home/End roving 与面试 tabs 的 End + Enter 激活通过。
- Reduced motion：CDP 仿真 `prefers-reduced-motion: reduce` 后媒体查询命中，页面进入动画计算值为 `animation-name: none`、`animation-duration: 0s`。
- 控制台：最终走查 `error=[]`、`warning=[]`。
- 暗色主题：修复新详情卡片与 onboarding 卡片对比度；最终 production build 回归中，onboarding 进度文字计算色为 `rgb(165, 180, 252)`、卡片背景为 `rgb(28, 26, 46)`，onboarding 与投递详情键盘焦点分别显示 3px / 2px 同色实线轮廓。自动化 gate 同时校验 light/dark 文本对比度不低于 4.5:1、焦点轮廓对比度不低于 3:1。
- Haru 在 768–900 宽度使用 150×238 frame，避免遮挡关键内容。
- 验收截图：
  - `D:\Users\yuqi.chen\.codex\visualizations\2026\08\25\01a037ee-b0a3-7991-96a3-86dc752a4c68\desktop-task-flow-1440.png`
  - `D:\Users\yuqi.chen\.codex\visualizations\2026\08\25\01a037ee-b0a3-7991-96a3-86dc752a4c68\desktop-task-flow-768.png`

浏览器验收结束后已恢复默认 1280×720 viewport，并停止临时静态与 API 服务。

## 独立 Code Review

独立 reviewer `final_cr` 完成多轮复核。发现并关闭的问题包括：

- Application 详情在事件 / 复盘 / JD 查询 loading 或 error 时显示假空态或开放错误写入口。
- 已有复盘误开新建、终止事件误驱动阶段 / 下一时间 / 准备动作。
- TopBar 与页面内部重复 primary，Calendar 重复 primary，Resume 上传存在重复 controller。
- Interview 数据源失败时仍推导准备状态与动作。
- 概览“下一时间”在事件 loading 时仍显示“待安排”。
- 暗色详情卡片可读性与 768 宽度 Haru 遮挡问题由最终浏览器走查发现并修复。
- 暗色 onboarding 进度文字与 onboarding / 投递详情键盘焦点轮廓对比度不足；改用主题感知实色 token 并新增实际颜色组合门禁。

最终复核：无未关闭 P0 / P1 / P2。

组合复核另发现并关闭两项 P2：

- 绑定的 Offer 在所属 Application 已软删除或不可见时，“返回所属投递”原先静默无动作；现在显示稳定的禁用态“所属投递当前不可见”，`AppShell` 防御路径也会给出同一反馈。
- 验收报告声明历史未绑定 Offer 只读，但卡片、比较抽屉和编辑表单原先仍暴露谈薪或写入口；现在该类记录只允许查看和比较，表单无保存入口且字段禁用，任何谈薪与更新调用均为 0 次。

第二轮组合复核继续发现并关闭一项 P1 与一项 P2：

- Pilot 当前 Offer、Offer 选择器和谈薪 Drawer 原先仍可绕过只读限制；现在 ContextPanel 只暴露已绑定 Offer，选择器过滤未绑定记录，AppShell 和 Drawer 各自保留最终防线，直接传入未绑定 Offer 时 Provider 与谈薪读写 API 均为 0 次。
- 所有入口统一复用 `listOfferBindingState()` 的正整数判断；`undefined/null/0/负数/NaN/Infinity/小数` 均为 unbound，只有正整数 Application ID 可进入谈薪流程。

历史范围门禁还补回了当前工作树和未跟踪文件检查，既不会把 Tool Metadata merge 误报为 Desktop 越界，也不会放过新的越界本地文件。

组合复核保留非阻塞 P3：Application 详情未接收 `offersLoading`，在 Offer 查询完成前可能短暂显示空时间线；`OfferCard` 的可选 `applicationLinkState` 仍允许未来调用方传入与真实 binding 矛盾的值，但当前唯一生产调用方已正确计算。旧 hash 深链携带额外 query suffix 及面试大列表分页性能也可后续收口。本期未扩大这些既有边界。

## 破坏性变化

- 无后端、API、HTTP/SSE、HITL、Ledger、Journal、Context Projector 或 Agent Runtime 契约变化。
- 新建 Offer 的前端表单现在要求显式绑定 Application；历史未绑定 Offer 只能只读查看和比较，不可编辑、补绑定或进入谈薪准备。
- URL view 同步为加法兼容；旧 query、path、hash deep link 与内部 view identity 继续可用。

## 集成状态与剩余风险

- Tool Metadata 已先进入本地 `main`，当前分支已同步该提交并完成 Agent/Chat/Context/Metadata 交界的机器化组合矩阵；不再保留“等待 Tool Metadata”的前置条件。
- Application-JD 独立发布门禁仍依赖 release orchestrator 提供 `OFFERPILOT_APPLICATION_JD_BASELINE_FILE` 与 `OFFERPILOT_APPLICATION_JD_ALLOWLIST_FILE`。本次未伪造输入，也不宣称该外部门禁通过。
- 非阻塞技术债为既有依赖审计、测试 deprecation/act warning、Vite 主 chunk 体积 warning，以及上述 P3；本项目未扩大这些问题。
