# PDF 简历结构化导入 Implementation Plan

**Goal:** 已保存 PDF 文字通过 AI 生成可核对候选，用户确认后原子地只填空白，原文不丢失。

**Architecture:** Non-Agent 只读预览 + 版本校验的人工合并。使用字段候选 `{path,value,evidence}`，path 为封闭结构化路径，value 与 evidence 必须可回溯到当前原文。前端仅展示/编辑候选；服务端重新验证并合并。

**Tech Stack:** Python/SQLAlchemy/FastAPI；React/Ant Design/Vitest。不新增依赖或表。

## 1. 服务端契约与事务（先RED，后GREEN）

- [x] 新建 `tests/test_resume_structured_import.py`，合成中文原文：`林晓\n产品运营\n教育经历\n滨海大学 计算机 2020-09\n技能 Python`。
- [x] 断言字段候选 `contact.name=林晓`、`education.0.school=滨海大学`、`skills.0=Python` 可构造内容；未知路径、重复路径、非连续数组、原文不支持、超限拒绝。
- [ ] 最初后端RED的独立运行日志未保留，不补写通过声明；最终新增测试及边界门禁41 passed。实际验证使用既有虚拟环境与 `PYTHONPATH=src`，未运行 `uv` 同步。
- [x] 新建 `resume_structured_import.py`（纯规则）和 `ai/resume_structured_import.py`（调用编排）。严格结果唯一形状 `{fields:[{path,value,evidence}]}`，每个字段值均为字符串，原文/结果各128KiB、值/证据各8KiB、每数组100项、总字段最多1000。拒绝bool/空白值/未知键/NaN/重复JSON键。使用现有 `workflows.complete_json(strict_json=True)`，补strict模式拒绝重复键；不加SDK入口。
- [x] POST `/api/resumes/{id}/structure-preview` 返回 `{resume_id,source_fingerprint,fields}`；POST `/api/resumes/{id}/structure-confirm` 输入 `{source_fingerprint,fields}`，输出当前 Resume。错误代码固定，既有上传API不变。
- [x] `repositories/resumes.py` 添加专用确认方法：BEGIN IMMEDIATE→查未删除upload→版本比较→原文证据校验→只补空白→commit→回读。版本不一致409。锁外调用Provider，外部Session不得用于此自有事务方法。
- [x] 已有内容、source、is_master、parent、raw_text/parsed_data全部保留；仅新增 `content_json.import_review={version:1,raw_text_sha256:...}` 描述人工接受分类，不提升事实权限。
- [x] 覆盖未配置/空原文/超限Provider0、一次逻辑生成、失败安全文案、源在Provider期间变动、确认冲突/删除、重复确认、双连接竞态、回滚。

## 2. 前端展示与生命周期（先RED，后GREEN）

- [x] 新增类型与services：`ResumeImportField`、`ResumeImportPreview`、`previewResumeStructure(id,signal)`、`confirmResumeStructure(id,input,signal)`；沿用130秒HTTP客户端。
- [x] 新建 `ResumeImportReview.tsx` 及 jsdom测试：显式按钮才AI请求；正在分类/失败/候选未保存/已核对；checkbox选择字段，value可改但证据只读；存在的模块标注保留；取消不写。
- [x] 切换resume或关闭unmount时AbortController+generation失效，迟到结果绝对no-op；确认中阻止重复，结果未知保留候选并要求回读，不自动再次AI。
- [x] Editor有未保存内容时禁止开始分类/确认并提示先保存；确认完成由现有onSaved刷新并关闭分类候选。原编辑器草稿不得被异步结果覆盖。
- [x] 上传来源增加独立只读“PDF 提取原文”；“其他”写 `additional_text`，原 `raw_text` 不改。非upload的rawText保持旧行为。高级JSON仍保留原契约，显式手工编辑不冒充AI确认。
- [x] `node node_modules/vitest/vitest.mjs run src/components/ResumeImportReview.test.tsx src/components/ResumeEditorDrawer.import.test.tsx src/lib/structuredResume.test.ts --maxWorkers=1 --minWorkers=1` RED→GREEN。

## 3. 验收与提交

- [x] Python定向与原Resume API、Provider边界门禁；Ruff/Mypy；编辑器/上传/事实来源前端回归与TypeScript/build。
- [x] 真实内置浏览器使用合成中文简历：查看原文、分类、核对、确认保存、刷新；只有明确授权时用真实用户简历或真实AI凭据。本轮不默认发送用户上传原件。
- [x] 独立规格复核→质量CR，关闭问题后小步中文conventional commit。不merge/push，不替换8080。

本计划由主线程实施前端集成，独立实现任务承担后端；两边文件无交叉写入。子代理提示模板文件在本机缺失，以明确契约、TDD、逐项自审和独立CR替代，不声称使用了不存在的模板。
