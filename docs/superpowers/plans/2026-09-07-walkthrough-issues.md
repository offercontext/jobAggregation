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

## 第二轮真实流程与说明书补齐

- [x] 重跑已修复的准备/复盘入口、题库、Offer、修改确认和 Undo，逐步截图。
- [x] U12：复盘 POST 同步等待真实 AI，现有 10 秒客户端时限与同类 130 秒不一致。测试 POST 覆盖为 130000ms、GET 保留 10000ms、失败单次请求且原 idempotency key 不变，再最小实现；不改后端、自动重试或结果未知恢复。
- [x] 调查面试准备 excerpt_mismatch 与模拟面试：保存真实失败证据，测试证明根因后修复，禁止放宽来源验证或伪造成功。
- [x] U05 已缩小到 Chromium 151 本地 SpeechRecognition.available 原生调用可独立导致 renderer crash；文字回答挂载不应探测语音。新增文字模式零探测、显式语音才探测、切回/卸载忽略迟到结果回归。只延迟可选探测，不宣称修复浏览器原生实现，不做 UA 绕过。
- [ ] 复盘历史恢复、确认练习重点及后续准备/练习闭环；补齐剩余页面和 Pilot 双入口，以真实可用能力为准。
- [ ] 定向测试、独立规格/质量复审、隔离 8092 真实 AI 重验；更新说明书 coverage、issues 和逐张截图说明。保留未完成项，不替换 8080/8091，不合并推送。

- 后端 confirmation 73 passed、confirmation_cutover 17 passed、interview_stories_repository 28 passed。
- 前端 9 文件原 205 passed；补 Offer 任务摘要回归后 model 88 passed，补实际 Undo 错误交互后组件 14 passed，共覆盖 207 项唯一测试（分批执行）。
- Ruff 变更源码/测试通过；Mypy 3 个源码通过；最终 TypeScript 与生产构建通过。
- 规格及最终独立质量复审通过，无开放 P0/P1/P2。质量复审发现的 error_code 读取问题已按真实组件 RED→GREEN 修正。
- 隔离 8092 使用演示库副本，亮色 1920×1080。截图位于 `D:/Users/yuqi.chen/.offerpilot/verification/walkthrough-fixes-20260907/`：`offer-prefill.png`、`question-queue.png`、`event-missing-date.png`。只在副本新增一道中文题并完成一次评分；原 8080/8091 与数据库不变。
- 未跑全量后端/前端、Docker 或 real-AI gate；可控模型回归不等于真实模型验收。保留已有约 1.66 MB chunk warning 和 deprecation warning。
- U01、U02、U05、U10、U12 尚需复现；U03/U09 只关闭已确认来源加载竞态，不抹除合法不可用状态。历史失效 Undo 不批量重写。
- 无 Schema/API/SSE 变更或破坏性数据操作，未合并、推送或替换用户部署。验收副本的签名密钥不进入仓库、截图或日志。

## 第二轮证据（2026-09-07，覆盖前述保留项的最新状态）

- Preparation：V1/V2格式修复现在再次携带完全相同的冻结输入，不携带无效模型原文；V2封闭来源路径、摘录和8/1000/5限额进入prompt。最多两次、Provider异常不重试、校验失败安全空结果不变。新增RED最初3失败，GREEN32通过；父代理最终32项全部通过。
- Voice：父代理恢复旧无条件probe后，文字模式回归按预期失败；恢复修复后本轮Voice36项与Review service10项共46通过。独立质量审查的微任务/有效回调断言已补，最终独立只读CR无P0/P1/P2。
- 后端扩展矩阵：preparation AI/API + review API共95项，94 passed / 1 failed，38分44秒。失败为`test_raw_excessive_json_depth_is_safe_422_before_repository_or_provider[deep-object]`：当前运行时可解码2000层对象，既有测试预期422。使用未修改main（46fa54a）与其源码路径单独重跑同一节点，同样失败（13.04秒），确认不是本次改动引入；未删除测试、放宽断言或新增排除来伪造通过。
- Ruff变更Python源码/测试通过，Mypy目标源码通过，TypeScript和生产构建通过；保留约1.664MB主chunk warning。未跑全量后端/前端或Docker。
- 8092真实AI：快速模拟面试完成5轮文字问答和最终反馈；远帆准备生成非空；复盘建议超过130秒后仍可用原key恢复，未新建重复尝试；保存准备重点、专项练习、下一场准备形成闭环。Chat新建投递撤销、Product Action重点撤销均实际成功。
- 续走查另补Offer双入口修改与接受/拒绝、参考资料上传/归档、会话搜索/重命名/归档/停止、手动简历编辑、Pilot故事来源确认与保存。截图和失败证据在演示目录，未进入源码仓库。
- 新发现且未掩盖：Pilot日程创建恢复但15:00确认/23:00日历偏差；模型正文虚构按钮/联动状态；原生语音仍可能崩溃；用户修改后模型反向建议、旧未来已完成日程提醒和技术路径文案仍保留。全功能说明书和所有双入口尚未完成。
- 原8080/8091和真实用户数据未改；无Schema/API/SSE变化。演示副本中按用户授权新增、编辑、归档及撤销虚构记录，不声称这些操作无数据副作用。
