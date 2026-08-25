# Desktop Task Flow Simplification 验收报告

状态：当前分支独立验收通过；等待 Tool Metadata 合并后的组合验收。当前分支未 push、未 merge。

## 基线

- Baseline：`0c10e05e256eb757d5f89a8b009dcea193f2fc78`
- Branch：`refactor/20260825-desktop-task-flow-simplification`
- Worktree：`D:\Users\yuqi.chen\offerpilot\.worktrees\refactor-20260825-desktop-task-flow-simplification`
- Tool Metadata 分支相对同一 baseline 的已提交及未提交 `web/**` 交叉文件均为 0。
- 按 `package-lock.json` 执行了独立 `npm ci`；未修改依赖清单或 lockfile。安装审计仍报告 13 个既有依赖漏洞。

## 范围门禁

- 最终改动限定在 `web/**` 与本项目三份设计、计划、验收文档。
- 禁止触碰的 Chat transport、Tool metadata、Pending/HITL、HTTP/SSE、Controller ownership 文件均无 diff。
- `src/offerpilot/**`、数据库、migration、后端 schema、Ledger、Journal、Context Projector 与 Agent Runtime 均无 diff。
- 未跟踪文件仅有 7 个本项目新增的 `web/**` 文件；三份文档因仓库 `docs/*` ignore 规则在提交时显式 force-add。
- `git diff --check`：通过。
- `desktopTaskFlowGate.test.ts` 固定 baseline、allowlist、forbidden paths、单一 Assistant owner、无 Chat transport 引入及主题/768 视觉门禁。

## 自动化验证

| 命令 | 结果 |
|---|---|
| `cd web && npm test -- --run` | 187 files / 1392 tests 全通过，661.45s |
| `cd web && npx tsc -b --pretty false` | 通过 |
| `cd web && npm run build` | 通过，3954 modules，2m 21s |
| `uv run pytest tests/test_chat_api.py -q` | 369 passed，1173.79s |
| `uv run ruff check .` | 通过 |
| `uv run mypy src` | 140 source files，无问题 |
| `uv run oc smoke --static-dir web/dist` | health、SPA fallback、写入确认与 Chat card smoke 全通过 |
| `git diff --check` | 通过 |

非阻塞 warning：

- Vitest 仍输出仓库既有的 React `act()`、测试 stub DOM 属性与 jsdom `getComputedStyle` warning；无测试失败。
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

## 破坏性变化

- 无后端、API、HTTP/SSE、HITL、Ledger、Journal、Context Projector 或 Agent Runtime 契约变化。
- 新建 Offer 的前端表单现在要求显式绑定 Application；历史未绑定 Offer 仍可只读查看并显示说明，编辑时不会暗改归属。
- URL view 同步为加法兼容；旧 query、path、hash deep link 与内部 view identity 继续可用。

## 集成状态与剩余风险

- Tool Metadata 尚未合并到 `main`。依产品要求，必须先合并该分支，再更新本分支到最新 `main`，完整执行 workspace/application Chat、sync、SSE、read-tool 多调用、approve/modify/reject、chained Pending、Legacy resume/replay/recovery、Haru→Pilot 与页面 context 切换组合矩阵，才可申请最终 merge。
- 当前报告只证明 Desktop Task Flow Simplification 分支的独立验收，不证明上述组合验收已经完成。
- 非阻塞技术债为既有依赖审计、测试 deprecation/act warning 与 Vite 主 chunk 体积 warning；本项目未扩大这些问题。
