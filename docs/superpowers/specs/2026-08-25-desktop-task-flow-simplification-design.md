# Desktop Task Flow Simplification 设计

## 目标、基线与范围

本项目在 `0c10e05e256eb757d5f89a8b009dcea193f2fc78` 上继续收敛 OfferPilot 桌面端任务流。目标不是删除能力或改写业务状态机，而是让普通求职者从“下一步求职行动”进入既有能力，并把内部工程概念移到高级信息。

工作分支固定为 `refactor/20260825-desktop-task-flow-simplification`，改动仅允许落在 `web/**` 与本项目三份规格、计划和验收文档中。禁止修改后端、数据库、HTTP/SSE、Tool schema、Pending/HITL、Ledger/Journal、Context Projector 与 Agent Runtime。

视觉方向为克制、任务优先的桌面工作区：变化度 5/10、动效 3/10、密度 5/10。沿用现有 Design Token、Ant Design 组件和图标，不引入新字体、渐变堆叠或装饰性卡片。

## 基线观察

- 基线一级导航为今日、投递、面试、资料，Offer 位于投递子标签，设置为弱入口。
- 基线 TopBar 在所有页面固定显示“添加投递”，并突出连续投递天数。
- `ViewMode` 由 AppShell 内部状态维护，没有 URL 恢复机制；实际列表 canonical ID 为 `applications-list`。
- `board` 与 `applications-list` 共享 `ApplicationViewState`；ApplicationDetail、Fit、Material、Offer、Interview 都已存在稳定服务和恢复路径。
- AppShell 只有一个 `AssistantSurfaceProvider` 和一个 `usePilotConversationController` owner；Haru 与 Pilot 共享 turns、pending、active request 与 page context。
- 基线 1440×900 浏览器 golden 已捕获。静态前端在 API 不可用时如实显示“加载失败”，同时验证了原导航、固定 TopBar 与 Haru dock 的可见层级。
- 完整基线测试在依赖缺失时得到 179 个文件通过、5 个文件仅因缺少 `pixi.js` 导入失败；按 lockfile 恢复独立依赖后，原 5 个文件的 54 项测试通过，production build 通过。
- `refactor/20260825-tool-metadata-convergence` 在开工时相对共同基线没有 `web/**` 交叉 diff。合并后仍必须更新 main 并重跑组合验收。

## 固定信息架构

```text
主要任务
├─ 今日
├─ 投递
├─ 面试
└─ Offer

常用资料
└─ 素材库

辅助
└─ 设置

全局浮动：Haru → Pilot 完整工作区
```

Haru/Pilot 不进入导航。投递内部只保留看板、列表；今日内部承接提醒、日历；素材库内部保持简历、经历素材、参考资料三种领域边界；Offer 成为一级入口但不产生第二份 Offer 状态。

## 路由兼容

所有现有 `ViewMode` 保留。URL 使用 `?view=<canonical-id>` 保存新导航状态，同时读取旧路径、hash 与别名：

| 旧入口 | 恢复到 |
|---|---|
| `list`、`/applications/list` | `applications-list` |
| `applications`、`/applications` | `board` |
| `/calendar` | `calendar`（今日模块） |
| `/offers` | `offers`（一级 Offer） |
| `materials`、`/materials/*` | 对应 `resumes/reviews/knowledge` |

浏览器前进、后退只恢复视图，不创建业务写入。Command Palette 继续使用相同 canonical ID。`context_type`、`context_ref`、`PilotPageContext` 与 `pageContextKey` identity 不变；只更新展示 label。

## 页面任务流

### 今日

固定顺序仍是当前最重要行动、其他待办、未来七天日程、本周进度和折叠数据分析。日历从近期日程直接进入 `calendar`。无待办时优先展示本周已收到 Offer；否则展示本周最近一次真实投递；两者都来自现有事实，不生成评分或新记录。

### 投递详情

详情顶部继续由既有 stage model 提供唯一突出主动作。正文变成：

- 概览：当前阶段、最近变化、下一时间、备注、JD 摘要；
- 准备：岗位快速判断与深入分析、简历/JD、投递准备、沟通、日程、复盘和结果记录的既有入口；
- 进展：只读投影 Application、ApplicationEvent、Schedule、Interview 与归属 Offer。

Tabs 只重排已有组件，不改 API、mutation、幂等键、冲突/unknown 恢复或父级草稿所有权。进展区不得调用 Application 状态写入。

### 面试

首页按既有事件时间和状态投影为即将进行、已完成、自由练习。真实面试和自由练习复用现有 readiness/Interview Studio；文字和语音是同一练习内的回答模式。题库、快速练习、复盘、成长和经历素材仍可达，但不再形成两个竞争的正式练习入口。任何 Story/Knowledge 保存仍要求用户确认。

### 素材库与准备文案

入口名称固定为简历、经历素材、参考资料。复盘沉淀属于用户确认后的个人结论，不与外部参考资料合并。用户可见术语映射到“投递准备”“本次投递记录”“快速判断”“深入分析”等；hash、worker、队列和内部 ID 默认放入“高级信息”。来源变化、失败、重试、unknown 与冻结版本必须继续可见。

简历选择不实现自动“最匹配”。业务写入前必须使用可信绑定版本或用户明确选择；有歧义时要求选择。

### Offer

零份、一份、多份继续由现有 workspace model 渐进展示。前端新建要求明确绑定 Application；历史未绑定记录显示兼容警告。OfferCard 只有一个“准备谈薪”主入口，并提供返回所属投递。详情页与 Offer 中心传递同一个 Offer 对象及现有编辑/谈薪回调。

### Haru / Pilot

首次空状态解释 Haru 是 Pilot 的轻量窗口。Pending 根据当前 page/entity label 解释将修改的对象，按钮为“查看修改内容”。展开只切换 surface；不重新发送消息、不创建 controller、不改变 SSE 生命周期，关闭 Haru 也不停止运行中的 Agent。

## TopBar 与主操作

TopBar 保持稳定位置，按当前页面接收一个可选 `primaryAction`：今日/投递添加投递，面试开始练习，Offer 录入 Offer，简历上传简历，经历素材添加经历。进入单条投递详情后不展示全局主动作，避免与 stage primary 竞争。所有主动作有完整文字、`aria-label` 和可见焦点。

## 可访问性与桌面矩阵

- 导航使用分组 heading、`aria-current` 和至少 40px 点击区域；
- Tabs、时间线、Collapse 和按钮保留语义与键盘操作；
- 页面或详情切换后将焦点恢复到主要内容；
- 颜色不作为唯一状态信号；
- 动效遵守 `prefers-reduced-motion`，不使用 `transition: all`；
- 768、1024、1280、1440 宽度不得产生页面级横向溢出，关键操作不得被裁切。

## 不变量与请求预算

- applications/events/offers/resumes/practice 继续沿用 AppShell 与现有 React Query keys；不新增集合查询；
- Haru/Pilot 仍只有一个 Provider、controller、active request 和 SSE 消费者；
- surface 展开/收起新增 Chat API 数为 0，新增 SSE 数为 0；
- 页面导航新增业务写入数为 0；
- 不新增前端 Tool 名称或工具展示真值。

## 验收

测试覆盖导航映射、旧深链、TopBar action、三段详情、面试三分法、素材库术语、Offer 0/1/2+ 与归属、Haru/Pilot 单 owner、焦点和 reduced motion。最终执行完整前端测试/build、指定后端兼容 gate、ruff、mypy、static smoke、diff/allowlist gate、4 档浏览器矩阵和独立子代理 CR。
