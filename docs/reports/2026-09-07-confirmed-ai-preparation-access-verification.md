# 已确认 AI 投递的面试准备资格修复验收

## 范围

用户批准在上一批体验修复基础上继续解决：经用户确认创建的 AI 投递不能生成面试准备。工作分支 `fix/20260907-audit-priority-flows`，本轮起点 `615888b`；不合并、不推送，不触碰根工作区。

原审计投递 276 的 source=ai；准备 POST 在 Provider 前因人工来源白名单返回404。真实 Ledger 有该投递的 primary/typed/create_application/committed 记录及完整 proposed→approved→claimed→committed transitions。

## 改动

- 新增只读 `can_prepare_application`：保留 cli/manual/web，未知来源拒绝。AI 仅在唯一创建 Ledger、完整 terminal digest、精确目标ID及创建时间、完整确认/授权指纹与顺序 transitions 全部通过时允许。
- 不把 AI 加入全局人工来源白名单，不修改 Application.source；不更改岗位评估、材料包、工具权限或业务写入授权。
- V1/V2 快照构建在现有 Session 中检查；Provider 返回后重新核验，不落已失效来源的 ready 结果。
- V2 ready/lease 快速返回也核验当前资格。来源资格失效时POST拒绝且Provider不重跑；GET历史仍沿用原有可见性。
- 旧仓储测试的虚构 source=test 改为真实人工来源 web；未知来源拒绝另有负向覆盖。未禁用或删除 Ledger 不可变 trigger。

## 测试证据

- RED：真实 `/api/chat` 提议→`/api/chat/confirm` 批准创建→JD/简历/事件→准备POST，旧代码返回404。确认前不存在投递。
- GREEN：首次创建返回201，重放200，模型调用数分别+1/+0。
- 17项专项测试通过，覆盖损坏摘要、缺少确认、重复候选、目标ID/创建时间/source不符、bool ID、授权缺失、未知来源、删除、V1/V2 Provider后漂移、ready重放与历史读取。
- 复审发现V2快速返回绕过资格检查，补测确认RED（200而非404），补检查后17项重新通过。
- 独立CR最终无剩余P0/P1/P2；Ruff通过，Mypy改动3个源文件通过。
- 关联矩阵 `pytest tests/test_confirmed_ai_preparation_access.py tests/test_interview_preparation_repository.py tests/test_interview_preparation_api.py -q --tb=short -x`：129 passed，229条既有弃用警告，850.91秒。该进程启动后才补V2快速返回检查；最终代码另重跑17项专项与4项受影响的既有冻结重放/租约回归，全部通过。计数有重叠，不相加宣称146项独立覆盖。
- 使用既有 `fix-20260904-schedule-practice-surfaces/.venv/Scripts/python.exe`，显式设置PYTHONPATH为本修复worktree的src；未改依赖环境。

## 本地实际验证与已知限制

- 使用原合成投递276、事件9、样例简历25、JD版本4，未篡改来源或绕过确认。
- 从浏览器生成进入真实Provider，不再404。保留生成过程截图：`D:/Users/yuqi.chen/.offerpilot/verification/audit-priority-fixes-20260907/10-confirmed-ai-preparation-generating.jpg`。
- 另发现现有前端冷启动竞态：准备抽屉的初始JD状态可能在异步查询完成前冻结为空；关闭后重开可取得版本并生成，但旧空草稿的显示仍可能为“尚未填写”。这是独立的前端来源交接问题，本轮不声称修复，不能将其误报为后端授权失败。
- 无Schema/migration、业务数据删除或历史来源改写。无完整发布级全量后端、前端、Docker或外置发布门禁声明。本轮不修改前端。
- 缺少完整可验证Ledger的旧AI投递仍拒绝准备生成；这不是无条件允许所有AI来源。

### 真实AI最终补记

- 浏览器发起的原请求创建准备记录3（03:58:29 UTC），前端先出现等待超时/结果待确认；后台随后完成为 `ready/safe_empty`。没有重复发起新生成。
- 使用数据库中原幂等键与同一输入回读：HTTP200，记录仍为3，generation_revision仍为1。新增自动化测试同时证明重放Provider为0。
- 新验证页可打开该历史结果，显示“暂无可验证的面试准备建议”，条目数0。因此本轮证明已确认AI来源通路与幂等回读可用，**不宣称模型生成了有用的面试建议**。本轮未扩展到建议质量/前端超时协议修复。
- [超时状态](D:/Users/yuqi.chen/.offerpilot/verification/audit-priority-fixes-20260907/11-confirmed-ai-preparation-unknown.jpg)；[历史回读安全空结果](D:/Users/yuqi.chen/.offerpilot/verification/audit-priority-fixes-20260907/12-confirmed-ai-preparation-readback.jpg)。截图保存在本地，不进Git。
- 最后新增V2资格检查后的既有ready/expired/heartbeat replay四项回归：4 passed，53 deselected。
- 完成后本地8080重启到最终源码；worktree保留本轮提交，未merge/push。无原有数据删除，只新增上述合成准备记录。
