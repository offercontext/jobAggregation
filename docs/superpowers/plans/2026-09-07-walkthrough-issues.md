# 筱哲走查问题修复计划

**目标：** 修复产品说明书走查中能复现的缺陷；区分产品问题、模型回答和采集环境故障，不以放宽 HITL、Surface 或 Ledger 校验消除报错。

**架构：** 沿用现有 Controller、QueryClient、Runtime 和领域边界。基于 main `46fa54a` 的隔离 worktree 实施；保留原数据库与演示数据，不自动合并、推送或替换部署。

**技术栈：** React、TypeScript、Vitest、Python、pytest、SQLite。

## 执行顺序

- [x] 1. 题库预热空队列后新增/编辑/删除刷新 due 缓存，保持生成和评分行为；RED→GREEN。
- [x] 2. 新建 Offer 按投递预填公司岗位，保留手改，编辑态绑定不变；RED→GREEN。
- [x] 3. 来源加载等待、日期/时长缺失展示、改写中文标签、Offer 状态域与内部来源过滤均有回归；快速练习崩溃保留待查。
- [x] 4. detached continuation 使用有效批准参数，原提案审计不变；终态无 Undo 清旧 owner、拒绝保留、撤销冲突读取 error_code。未确诊项不伪造修复。
- [x] 5. 定向测试、Ruff/Mypy、类型检查、构建与独立复审；隔离浏览器验收预填/手改保护、题库新题进入复习、无日期展示。
- [x] 6. `docs/BUGS.md` 记录根因和保留项；只提交本次精确范围，不提交数据和密钥。

测试命令：在 `web` 执行 `node node_modules/vitest/vitest.mjs run src/components/QuestionBankView.modes.test.tsx src/components/AddOfferForm.test.tsx`；最终执行 `npm run build` 与扩展定向矩阵。

## 验证与保留项

- 后端 confirmation 73 passed、confirmation_cutover 17 passed、interview_stories_repository 28 passed。
- 前端 9 文件原 205 passed；补 Offer 任务摘要回归后 model 88 passed，补实际 Undo 错误交互后组件 14 passed，共覆盖 207 项唯一测试（分批执行）。
- Ruff 变更源码/测试通过；Mypy 3 个源码通过；最终 TypeScript 与生产构建通过。
- 规格及最终独立质量复审通过，无开放 P0/P1/P2。质量复审发现的 error_code 读取问题已按真实组件 RED→GREEN 修正。
- 隔离 8092 使用演示库副本，亮色 1920×1080。截图位于 `D:/Users/yuqi.chen/.offerpilot/verification/walkthrough-fixes-20260907/`：`offer-prefill.png`、`question-queue.png`、`event-missing-date.png`。只在副本新增一道中文题并完成一次评分；原 8080/8091 与数据库不变。
- 未跑全量后端/前端、Docker 或 real-AI gate；可控模型回归不等于真实模型验收。保留已有约 1.66 MB chunk warning 和 deprecation warning。
- U01、U02、U05、U10、U12 尚需复现；U03/U09 只关闭已确认来源加载竞态，不抹除合法不可用状态。历史失效 Undo 不批量重写。
- 无 Schema/API/SSE 变更或破坏性数据操作，未合并、推送或替换用户部署。验收副本的签名密钥不进入仓库、截图或日志。
