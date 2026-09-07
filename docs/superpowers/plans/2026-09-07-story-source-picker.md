# 面试故事材料选择 Implementation Plan

**Goal:** 将内部路径列表改成用户可理解的材料→片段→确认整理流程。

**Architecture:** 新增纯展示标签/分组函数和无网络请求的受控选择组件；原 Drawer 继续唯一持有 selections、来源查询、生成、人工保存与 Product Action 控制。不改变 API、来源路径、ID、证据或 AI 调用次数。

**Tech Stack:** React、Ant Design、CSS Modules、Vitest。用户已批准方案，由主线程实施、子代理做最终CR；不因执行方式选择再次暂停。

## 1. 材料展示与身份

- [x] 新增 `web/src/lib/storySourcePresentation.ts` 与测试：路径映射中文，未知路径固定“其他内容”，不能返回原路径；mock 按 attempt 分组、按 turn 卡片成对呈现，问题与回答分别选择；不修改原 selection。
- [x] 新增 `web/src/components/StorySourcePicker.tsx`、CSS与挂载测试。默认全未选、材料折叠；按类别筛选与搜索只影响可见面，不清空勾选；已选内容可单独查看；长预览展开；frozen禁止选择。
- [x] RED：`node node_modules/vitest/vitest.mjs run src/lib/storySourcePresentation.test.ts src/components/StorySourcePicker.test.tsx --maxWorkers=2 --minWorkers=1`。GREEN使用同命令。

## 2. Drawer接入

- [x] 修改 `InterviewStoryDrawer.tsx`：替换三块平铺List；第一步选材料，第二步展开片段及补充说明，第三步底部确认。保留显式AI同意、原生成handler与手动保存；footer显示总选择数，生成按钮固定可达。
- [x] 中文标签也用于手工证据选择项，实际option value/link身份不变。修正同attempt多turn的preview查找，使其按完整path匹配。
- [x] 更新 `InterviewStoryDrawer.interaction.test.tsx` 的显示文案断言，新增不暴露路径、原payload不变及改变来源需重新同意测试；继续运行整个文件的恢复/确认/Undo矩阵。
- [x] 不自动选择来源、不把提问当回答、不屏蔽或删除Smoke/乱码材料、不调用模型做摘要。API只提供截短预览，UI不得承诺完整原文或不存在的日期。

## 3. 验证与交付

- [x] 运行三个目标测试文件、相关Story/ReviewReadiness测试、`npm run build`与`git diff --check`；失败按根因修复。
- [x] 内置浏览器在独立前端端口以合成数据或只读真实来源走查：材料折叠、搜索、选中、长预览、固定footer、键盘和暗色。不得触发真实生成/保存，不替换8080。
- [x] 子代理CR关闭问题；在 `docs/BUGS.md` 记录证据。显式git add后独立commit，仅本分支，不merge/push。

## 测试关键断言

```ts
expect(storySourceLabel('resume_version', '/content_json/skills/0')).toBe('技能 · 第1项');
expect(storySourceLabel('mock_turn', '/turns/004/answer')).toBe('第4题 · 我的回答');
expect(onToggle).toHaveBeenCalledWith({ source_kind: 'mock_turn', source_id: 7, path: '/turns/004/answer' });
expect(proposal).not.toHaveBeenCalled(); // 搜索、展开、选择不是模型调用
```

## 验证记录（2026-09-07）

- 独立 CR 两项 P2（模拟面试稳定记录号、手工证据材料标题）已修复并复审；无剩余 P0/P1/P2。截图保留交互验收时的示例编号，最终编号使用稳定记录号，不再按列表位置编号。

- 三个目标测试文件最终复跑：32 passed。
- Story / ReviewReadiness 关联矩阵：12 files / 284 tests；首次 283 passed，性能门禁在并行构建时 35.17s 超过 30s。
- 停止构建后独立复跑完整 `reviewReadinessNegativeFixtures.test.ts`：133 passed，性能检查 20.98s；阈值与测试代码未修改。关联用例全部覆盖通过，不宣称首次全绿。
- `npm run build`（包含 `tsc -b`）：通过；保留现有大 chunk warning（主包约 1658 kB）。最后文案改动由目标测试复验。
- 浏览器截图：仓库外 `D:/Users/yuqi.chen/.offerpilot/verification/story-source-picker-20260907/01-material-groups.png` 与 `02-selected-answer-confirmation.png`。
- 只读走查材料分组、搜索保留选择、已选内容、预览展开、键盘 Space、暗色及固定 footer；未点击最终生成/保存。独立端口 5175 已停止，8080 未替换。
- 无后端/API/Schema 变更，未跑后端全量或真实 AI。本次不新增 Provider 路径，生成、保存与 unknown recovery 由现有自动化覆盖。
