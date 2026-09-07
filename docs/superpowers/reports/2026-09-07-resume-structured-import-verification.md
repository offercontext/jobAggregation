# PDF 简历结构化导入验收

分支：`fix/20260907-resume-structured-import`。Baseline：`070cec7abc5bde94e5d33873f449825bd2c27971`。

## 修复范围

- PDF 上传仍只保存原件并提取文字，不自动调用 AI。
- 新增只读 AI 分类预览与人工确认的空白字段合并；结果必须为封闭路径、原文值及连续原文依据。
- 原文独立显示；上传简历的“其他”改为 `additional_text`，非上传简历保持原行为。
- 只填空白，保留已有标量、整个非空数组、未知内容、来源、主简历及父子关系。确认在自有 `BEGIN IMMEDIATE` 事务中核验源指纹；不创建 Pending/Ledger 或提升事实资格。
- 候选窗口显式同意、取消、代际失效、错误聚焦、结果未知回读；回读不自动关闭/重跑 AI，源变动禁止旧提交。

## 破坏性变化

无表或 migration；无原文件、原文、业务数据删除。新增两个 opt-in API，不改变旧上传/PATCH/工具协议。`import_review` 只表示用户接受分类，不表示内容已认证。上传记录“其他”的展示位置变化，原有提取全文仍可在“PDF 提取原文”查看。

## 验证记录

使用 worktree 的既有虚拟环境，设置 `PYTHONPATH=src`，未执行依赖同步。以下为最终修订后的验证结果。

- 原简历 API / module workflows：12 passed。
- JD/resume AI API / resume Tool Pipeline：30 passed。
- 新结构化导入及两个 Provider 边界门禁：41 passed（140.22s）；最后防御性修订新增2个测试，最终模块结果见下面补充记录。
- 最终结构化导入模块单独重跑：41 passed（139.75s）；加既有2项边界门禁、12项API/workflow和30项AI/tool回归，累计85个不同后端测试通过。
- 前端编辑器/候选/简历库矩阵：8 files / 35 passed（78.16s），包括结果未知回读、422保留候选、高级JSON无修改不误判脏状态。最后新增异常raw_text回归后，3个编辑器文件11 tests通过；累计8 files / 36个不同测试通过，并非最后重新执行了一次8文件组合。
- 完整 Mypy：166 source files 通过；目标后端文件 Ruff 通过。
- Build：通过；主 chunk 约1654 kB，保留既有 warning。
- `git diff --check`：通过。
- `oc smoke --static-dir web/dist`：通过，包含健康检查、SPA、Chat确认及卡片回归；其数据影响见下节，不属于隔离浏览器测试。

最后复审补充：已解码dict原先直接返回，可绕过已知字段类型校验；前端非字符串raw_text被当作缺失回填。新增后端2例与前端1例先观察失败，再改为dict同等校验、仅缺失键回填，类型错误保持recovery。此后目标Ruff与修改模块Mypy通过。此前完整Mypy166文件通过；不将局部复验冒充再次完整运行。

独立CR已关闭结果未知回读、来源身份、高级JSON误判dirty及最后的malformed来源P2，无剩余P0/P1/P2。复审者独立运行前一版39个后端测试与24个前端测试，最后定向再验证2个后端测试和4个前端测试通过；未修改文件。最终 `npm run build`（包含TypeScript）通过，3978 modules，主chunk1654.05 kB。

后端命令分组（均以 `.venv/Scripts/python.exe -m` 执行，`PYTHONPATH=src`）：

```text
pytest tests/test_resume_structured_import.py tests/test_context_projector_source_gates.py::test_fixed_non_agent_manifest_has_13_functions_and_18_calls tests/test_context_projector_source_gates.py::test_raw_provider_boundary_manifest_resolves_exactly_five_functions -q
pytest tests/test_resumes_api.py tests/test_module_workflows.py -q
pytest tests/test_jd_resume_ai_api.py tests/tool_pipeline/test_resumes.py -q
mypy src
ruff check src/offerpilot/resume_structured_import.py src/offerpilot/ai/resume_structured_import.py src/offerpilot/ai/workflows.py src/offerpilot/api.py src/offerpilot/repositories/resumes.py tests/test_resume_structured_import.py
```

前端在 `web` 下运行：

```text
node node_modules/vitest/vitest.mjs run src/components/ResumeImportReview.test.tsx src/components/ResumeEditorDrawer.import.test.tsx src/components/ResumeEditorDrawer.mount.test.tsx src/lib/resumeImport.test.ts src/lib/structuredResume.test.ts src/components/ResumeEditorDrawer.structured.test.ts src/components/ResumeLibraryView.test.ts src/components/ResumeLibraryView.versionCompare.mount.test.tsx --maxWorkers=2 --minWorkers=1
npm run build
```

### Smoke 数据影响

这次实际运行的 `oc smoke` 未覆盖 `OFFERPILOT_DATA`，使用了默认本地数据目录，创建 `Smoke Co` 投递 #277 和3个测试会话，并以模拟模型完成该测试投递的确认写入。没有调用外部AI，未删除这些记录或其他用户数据。三个会话请求为 `move this application to offer`、`create application card regression`、`create event card regression`；后两条创建卡片被拒绝。此项已告知用户，不将本次 smoke 描述为隔离数据库验收。

## 隔离浏览器

本节浏览器流程通过独立8082服务、独立 data 目录与注入 `SyntheticModel` 进行，不接触8080用户数据；上一节 smoke 是不同流程。通过真实文件选择器上传合成中文 PDF，走真实HTTP/数据库/前端链路；该注入模型验证集成，不等同于 real-AI 质量验收，外部 Provider 请求为0。

步骤：上传→检查独立只读原文→显式开始分类→核对17项候选→取消Excel勾选→确认16项→教育/工作/项目各模块回读→技能只有Python/SQL→“其他”为空→刷新保留状态→再次预览全部已有字段受保护→取消不写。

截图目录：`D:/Users/yuqi.chen/.offerpilot/verification/resume-structured-import-20260907/`。

| 截图 | 正在做什么 |
| --- | --- |
| `01-original.png` | 查看独立保存的PDF提取原文 |
| `02-explicit-consent.png` | 用户明确确认发送原文给AI前的提示 |
| `03-candidates.png` | 预览候选及对应原文依据；初版长弹窗，随后收口为固定滚动区 |
| `04-education-saved.png` | 确认后教育经历进入结构化字段 |
| `05-experience-saved.png` | 工作经历与亮点进入对应模块 |
| `06-selected-skills.png` | 只写入勾选的技能 |
| `07-other-empty.png` | “其他”不再堆放PDF全文 |
| `08-protected-review.png` | 再次解析不会覆盖非空字段；确认/取消保持可达 |
| `09-final-education.png` | 最终版本重新确认教育经历的结构化展示 |

未用用户真实简历或真实AI凭据。合成PDF和隔离数据库保留在上述验收目录，不纳入Git。
验收结束后已停止8082隔离服务；8080部署未替换。

## UI 细节检查（quick）

范围仅 `ResumeImportReview` 与原文入口，React + Ant Design，沿用既有样式，不新增UI库。

| 类别 | 证据/结论 |
| --- | --- |
| Typography | 中文候选/原文标签可读；未调整全局字体 |
| Surfaces | 长候选改为60vh内部滚动，页脚动作保留；原文只读 |
| Animations | 未新增动画；阶段按钮独立key避免沿用加载图标；未做10%动画回放 |
| Icons | 沿用Ant Design原组件；未引入其他图标 |
| Performance | 未新增动效库或网络请求；候选数及输入有界 |

| 严重性 | 位置 | 修改前 | 修改后 | 原因 |
| --- | --- | --- | --- | --- |
| MEDIUM | ResumeImportReview 候选窗口 | 长内容把确认推到页面底部 | 有界内部滚动且保留页脚 | 操作持续可达，减少重复寻找 |

未采用新增阴影/主题：与现有页面保持一致。本轮无新动画，因此不作动画质量完成声明。

## 剩余风险和排除项

- 未进行真实Provider/个人PDF质量验收；AI可能漏分，必须人工核对，未识别内容留在原文。
- 不支持扫描件OCR；严格原文支持意味着需要改写的内容应使用普通编辑器，而非通过分类确认补写。
- 不自动重排/追加已有非空经历列表。
- 保留既有FastAPI/React/jsdom warning、bundle warning。本次是聚焦修复验证，不宣称全量后端/前端、Docker或外部发布门禁通过。
- 未授权merge/push/替换8080，本分支保持隔离。日历为另一分支独立提交 `b82f8a2`，未混入此提交。
