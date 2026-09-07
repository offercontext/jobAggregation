# 已确认 AI 投递的面试准备资格修复计划

> 本会话按 executing-plans/TDD 内联实施，独立子代理复核；不合并、不推送。

**Goal:** 仅允许有完整已确认创建记录的 AI 投递生成面试准备。

**Architecture:** 保留人工来源的既有资格，增加 Ledger 只读证明。生成快照构建与 Provider 后回写重新核验；历史读取不增加门禁。其他岗位评估/材料包入口不修改。

**Tech Stack:** FastAPI、SQLAlchemy、SQLite、pytest。

## Task 1：真实确认链回归

- 新建 `tests/test_confirmed_ai_preparation_access.py`：ScriptedModel 先返回 create_application；断言确认前 applications 为空；真实 `/api/chat/confirm` 批准后返回 source=ai；创建JD、简历、事件，再调用 preparation。
- `python -m pytest tests/test_confirmed_ai_preparation_access.py -q`：旧行为为404，成功创建应为201、重放200；必须看到此RED才实现。初稿误写创建200，按既有HTTP契约纠正。
- 断言首次准备Provider加1，同key重放Provider加0。

## Task 2：只读来源资格

- 新建 `src/offerpilot/repositories/application_preparation_access.py`，接口 `can_prepare_application(session, application) -> bool`。
- deleted拒绝；cli/manual/web允许；其他来源除ai均拒绝。
- ai只接受唯一的primary/typed/create_application/committed记录，JSON目标id精确关联；复用 `payload_from_operation` 验证终态payload摘要，校验结果的record_type/source/id/application_id/created_at，拒绝失败字段与缺失授权指纹。
- transitions必须按seq严格为proposed/approved/claimed/committed；不读取/记录原始参数或密钥，不从可见回复解析ID，不调用Provider，不新增表/字段。
- 在 `_build_v1_snapshot` 中，在既有可见Application查询之后校验；V2复用该函数。API初步检查只保留不可见判断，把来源资格交给同Session的仓储校验。
- 历史list/detail继续原有可见性，不把旧历史屏蔽。其他HUMAN_APPLICATION_SOURCES引用不改。
- CR补充：V2 `_try_frozen_existing` 快速返回也必须同Session核验资格；历史GET不受该POST检查影响。

## Task 3：负向验证和收口

- 缺少Ledger、未知来源、已删除、错误目标ID、损坏摘要、错误创建时间、缺少approved transition、重复候选均拒绝。
- 复用真实确认fixture，在隔离事务中注入异常后rollback；不修改真实用户DB。
- Provider后撤销/来源漂移继续由快照重建拒绝，不保存ready结果。
- 定向pytest与既有preparation API/repository、Ruff、Mypy；独立CR。
- 重启本地修复服务，原审计Application276点击生成，实际调用AI并保存截图；不伪造来源。
- 报告说明旧AI无Ledger仍不支持；中文conventional commit，保持worktree干净，不merge/push。

## 执行记录

- 真实确认链RED已复现，来源检查已实现；17项专项GREEN通过。
- 独立CR发现并关闭V2 ready replay绕过检查，另有4项既有冻结重放/租约回归通过。
- 真实Provider已调用，记录3最终ready/safe_empty，原请求回读200且generation_revision保持1；未生成可用建议，不将安全空结果包装为内容验收通过。
- 冷启动JD初始化竞态与长请求前端超时单独记录，未混入本轮来源权限修复。
