# Desktop Task Flow Simplification 实施计划

## Task 0：固定基线与所有权

1. 在指定 worktree/branch 校验 `0c10e05e...`、dirty state 和并行分支 `web/**` 交叉。
2. 安装 lockfile 依赖，记录基线测试、build、浏览器 golden 与请求所有权。
3. 创建设计、计划、验收文档和最终精确文件 allowlist。

## Task 1：导航、路由与 TopBar

1. 先更新 `navigation`、Sidebar、TopBar、Command Palette 与 PilotPageContext 测试。
2. 重组模块与 tab，保留全部 ViewMode/canonical identity。
3. 增加 URL/path/hash alias 解析、history 前进后退测试。
4. AppShell 接入页面 primary action、主内容焦点恢复和稳定 URL。

## Task 2：投递详情

1. 用组件/source contract 固定概览、准备、进展和只读边界。
2. 重排 ApplicationDetail；复用所有现有 Drawer、draft 与回调。
3. 从 AppShell 传入同一 application 的 Offer 只读摘要。
4. 验证 tab 切换、冲突/unknown 恢复和无 Application 自动写入。

## Task 3：任务语言与素材库

1. 更新 Fit/Material/Knowledge/SourceState 的用户文案测试。
2. 把 Triage/Deep Review 等翻译为快速判断/深入分析，内部标识保持不变。
3. 把工程诊断折叠到高级信息，保留来源冲突、失败与重试。
4. 验证简历选择没有启发式自动最匹配。

## Task 4：面试事件中心

1. 固定即将进行、已完成、自由练习的确定性分组测试。
2. 复用 InterviewReadiness 与 InterviewStudio，保留题库/成长/经历素材入口。
3. 验证文本/语音共用同一练习，不产生 Story/Memory 自动写入。

## Task 5：Offer 工作区

1. 保留既有 0/1/2+ workspace model 测试。
2. 新建 UI 要求 Application 绑定，历史无绑定显示兼容状态。
3. 合并谈薪入口，增加返回所属投递；TopBar token 触发现有录入表单。
4. 验证比较只读、编辑 identity 与谈薪恢复路径不变。

## Task 6：Haru/Pilot 认知

1. 更新首次说明与对象感知 Pending 文案测试。
2. 只改 Haru 展示，不改 Provider/controller/chat service/types。
3. 回归 Haru → Pilot turns/pending/request identity 与请求计数。

## Task 7：验收、CR 与提交

1. 运行聚焦测试、完整 `npm test -- --run`、`npm run build`。
2. 运行 `pytest tests/test_chat_api.py -q`、ruff、mypy、static smoke、diff check。
3. 用内置浏览器覆盖 768/1024/1280/1440、旧深链、键盘焦点、reduced motion、console 和请求数。
4. 生成精确 allowlist/验收报告，启动独立子代理 CR，关闭所有 P0/P1/P2。
5. 确认只改允许范围，按规范分开执行 git add 和 git commit；不 push、不 merge。

## 集成后追加门禁

Tool Metadata 分支合并 main 后，本分支更新 main 并重跑 workspace/application Chat、sync/SSE、多 read-tool、approve/modify/reject、chained Pending、legacy resume、terminal replay、delivery recovery、Haru→Pilot、page context 以及 Provider/Tool/HTTP/SSE/业务写入计数。该外部前置条件未满足前不得申请最终合并。
