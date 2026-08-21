# Desktop Workspace Experience Redesign 验证报告

日期：2026-08-21

## 基线与范围

- 第二阶段起始 baseline：`2f6e895e02b86f33052a2e507e9b0404bb82f4b5`（`refactor: AI 统一助手界面与 Haru 对话`）。
- Pilot Runtime integration baseline：尚未产生；Pilot Runtime 尚未按约定先合并到 `main`，因此本报告不声称组合集成已经验收。
- 最终提交：本报告所在的唯一交付提交，标题为 `refactor: AI 重构桌面求职工作区体验`；提交后以 `git rev-parse HEAD` 回读其不可自引用的 SHA。
- 工作树：`D:\Users\yuqi.chen\offerpilot\.worktrees\refactor-20260821-assistant-surface-shell`。
- 分支：`refactor/20260821-assistant-surface-shell`。
- 未 push、未 merge，未触碰根工作区或 Pilot Runtime worktree。

固定改动 allowlist：

```text
web/**
docs/superpowers/specs/**
docs/superpowers/plans/**
docs/reports/**
```

Allowlist canonical JSON 的 SHA-256：

```text
6806fa6815a5eb009934d12178e92d32bbe4ce7163fbe407266e1ad505c3e873
```

门禁文件：`web/workspace-experience-gate.json`。最终审计覆盖 52 个改动文件（含本报告），allowlist 外文件为 0；没有 `src/offerpilot/**`、数据库、migration、API schema、SSE、Agent、Repository 或移动端依赖进入 diff。

## 实施结果

- 一级业务导航收敛为“今日、投递、面试、资料”，设置保留为独立弱入口；Pilot/Haru 继续由单一 `AssistantSurfaceProvider` 和 Conversation Controller 控制。
- 今日页按“一个主行动、最多三条其他待办、未来 7 天、本周进度、默认折叠分析”重排，并由确定性纯模型选择主行动；AppShell 继续持有共享查询，避免 Dashboard 重复取数。
- 投递详情改成阶段化岗位工作区，固定顶部只保留一个阶段主操作和更多菜单；Offer 阶段主操作会进入 Offer 工作区，面试结束与是否已有复盘分别建模。
- 面试整合为“待准备、模拟练习、题库、复盘与成长”；资料整合为“简历、经历与故事、学习资料”；旧视图仍有明确映射。
- 简历默认使用结构化编辑，支持章节、增删、排序、字段提示、保存、取消和未保存确认；未知扩展字段无损 round-trip，非法历史 JSON 进入恢复模式；结构化草稿与高级 JSON 双向同步。
- Knowledge 可见术语收敛为“来源依据、资料导读、保存版本、内容整理、处理记录”，内部标识和任务细节放入技术语境。
- Offer 使用 0 / 1 / 2+ 渐进展示，不将缺失值伪装为 0；只有 2 份以上显示比较入口。
- 设置统一为“AI 与模型、数据与备份、Haru 与外观、语音、高级与诊断”，齿轮直接进入 AI 与模型，高级日志和运行时详情默认折叠，未创建第二套 AI 设置状态。
- 新增合成 golden 与源码/机械门禁，覆盖导航、旧路由、单一 Assistant 所有权、无页面直连 SSE/Pending/Journal、看板/列表共享状态、无后端和移动端范围扩张。

## 验证结果

### 前端

- `cd web && npm test -- --run --reporter=dot --silent`：通过，181 个测试文件、1286 个测试全部通过。
- `cd web && npm run build`：通过，3950 个模块完成生产构建；仅保留既有大 chunk 提示，没有编译错误。
- 定向回归覆盖：Resume 结构化编辑 → 高级 JSON → 保存；未知字段保留与非法 JSON 恢复；Offer 阶段主操作；已有复盘的已完成面试；Knowledge 旧术语负向门禁；今日页唯一主行动；Offer 0/1/2+；设置入口与导航门禁。
- `git diff --check`：通过；仅报告 Windows 工作副本的 LF/CRLF 转换提示，无 whitespace error。

### 后端兼容门禁

- `uv run pytest tests/test_chat_api.py -q`：通过，347 passed；存在 FastAPI/Starlette 既有弃用警告。
- `uv run ruff check .`：通过。
- `uv run mypy src`：通过，119 个 source files 无错误。
- `uv run oc smoke --static-dir web/dist`：通过；health、SPA fallback、创建投递、Chat pending、确认写入、pending 清理、创建投递卡片和创建日程卡片全部通过。

## 浏览器证据

使用内置 Codex Browser，服务于隔离临时数据目录，不读取或写入用户正式数据。

- 1440 × 900：完整桌面布局、200px 导航与主内容区无横向溢出；Haru 入口可见。
- 1024 × 900：紧凑桌面无关键内容遮挡；设置五分区完整，高级与诊断默认收起，日志不在默认视图暴露。
- 768 × 900：主内容宽度约 568px，无横向溢出或关键内容裁切；未执行 `<768px` 移动端验收。
- 完整走查：今日主行动 → 新增投递 → 看板/列表切换 → 岗位工作区 → 面试四区 → 资料三区 → 结构化简历 → Knowledge → Offer → 设置 → Haru → Pilot。
- 额外回归：将隔离投递切换到 Offer 后，“查看 Offer 与截止时间”准确进入 Offer tab；结构化姓名修改后打开高级 JSON，JSON 中保留最新值；取消时出现未保存确认。
- 控制台错误和警告：0。
- 同一次最终页面链路的 API 请求中未发现重复请求；`applications`、`application-events`、`offers`、`questions/stats`、`resumes`、`knowledge/notes`、`onboarding` 与 material-kit 请求各 1 次。
- 未发现重复 SSE；隔离环境没有配置 API Key，因此没有伪造一次实时模型流或确认事务。Haru/Pilot 正确显示不可发送的诚实状态，实时 SSE、approve/modify/reject/timeout 留待 Runtime 组合验收。

## 独立 CR

独立子代理以 baseline `2f6e895e02b86f33052a2e507e9b0404bb82f4b5` 审查全部 diff，重点检查业务语义、Resume 数据安全、Assistant 双状态、context、旧路由、危险操作、API Key、主操作竞争、Journal、后端与移动端范围。

初审发现并关闭：

1. Resume 结构化草稿切入高级 JSON 时可能回写旧数据：改为从当前草稿生成高级 JSON，并在切回时解析回草稿，增加真实 mounted interaction test。
2. Offer 阶段主按钮错误打开投递事实 Drawer：改为由 AppShell 导航到 Offer 工作区，增加点击行为测试与浏览器回归。
3. 已有复盘的历史面试错误退回“准备本轮面试”：拆分“面试已结束”和“复盘已存在”，增加行为测试。
4. Knowledge 搜索和技术提示残留 Evidence/Extraction/Source/Origin：统一用户可见文案并增加旧术语负向测试。

最终独立复核结论：P0 = 0，P1 = 0，P2 = 0，未发现新的可操作问题。

## 内部破坏性 UI 变化

- 一级导航和页面入口已重排；原“练习、题库、复盘、简历、Knowledge、Offer、AI Settings”等独立入口被映射进新的四个业务工作区，不再作为同级一级入口展示。
- Dashboard 信息密度与主操作优先级改变，详细分析默认折叠。
- Application Detail 顶部并列操作被替换为一个阶段主操作和更多菜单。
- 简历 JSON 编辑器不再是默认入口；合法数据默认进入结构化表单，无法安全解析的数据进入恢复/高级编辑流程。
- Offer 比较设置只在 2 份以上且用户主动进入比较时展示。
- AI Settings 不再保留独立 Drawer 状态源，统一进入设置中心。

这些变化只影响前端信息架构和交互入口；旧深链继续映射，不是后端契约迁移。

## 后端契约保持

- 未修改后端文件、API 字段、数据库、migration、领域表或写入语义。
- 未重命名 React Query service key 或后端 Knowledge/Resume/Story/Offer 模型。
- 写操作继续使用现有 API、校验和 HITL 确认边界。
- Haru/Pilot 继续共享 Conversation Controller、请求、pending、context 与附件；页面没有读取 Journal 作为 UI 真值，也没有新增 Chat service 或 SSE parser。

## 未实施能力

本阶段明确未实施：Haru Compact Confirmation、ActionPresentationPolicy 后端字段、主动业务提醒和持久去重、Journal Run 驱动 UI、SSE reconnect/replay、刷新后恢复普通 Provider 请求、后台 Agent Queue、移动端、新 Memory/Knowledge retrieval/Summary、领域数据模型合并。

## 剩余风险

- Pilot Runtime 尚未合并，不能把当前结果解释为 Runtime + UI 的最终组合验收。合并顺序仍应是 Runtime 验收并合并 `main` → 本分支更新新 `main` → 重跑组合 gate → 独立 CR → 用户批准。
- 隔离环境无 API Key，未执行真实模型流、SSE、approve/modify/reject/timeout 和 terminal replay；这些属于 Runtime 合并后的必跑矩阵。
- 生产构建仍有既有主 chunk 大小提示，本次未引入新设计系统或动画依赖，也未在本阶段做无关代码拆包。
- 768px 下沿用现有紧凑桌面 Haru 可见性策略；`<768px` 移动产品形态不在范围内。
