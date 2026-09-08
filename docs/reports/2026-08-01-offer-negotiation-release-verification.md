# Offer 比较与谈薪发布验证报告

日期：2026-08-04
分支：`feat/20260801-offer-negotiation`
范围：仅发布收口；未新增 Offer、谈薪、Pilot、HITL 或证据门控语义；未推送、未合并。

本文件是该次 Offer 谈薪迭代的历史发布验证报告；重复的 artifacts 路径占位文件已移除。下文结论仅适用于记录的验收基线，不代表当前主线发布状态。

## 本轮收口变更

- 日历测试改用测试内创建的投递时间推导月份，消除固定月份导致的日期敏感失败。
- smoke 隔离数据库销毁前停止 Knowledge runtime 并释放数据库 engine，避免后台 worker 在临时目录删除后继续访问 SQLite。
- 新增前端稳定分组门禁脚本，逐组持久化 Vitest JSON 报告、文本日志和完成标记，再执行覆盖集合聚合。
- 前端门禁每次重新发现当前测试集合，并将 `web/src`、前端配置/锁文件和分组脚本内容绑定到 manifest；负向测试使用 `tmp_path` 最小仓库副本，不改写真实源码。
- `RepositoryRoot` 会规范化尾部分隔符，并在计算相对路径前校验根目录边界；补充尾部 `\`、省略参数默认根目录和 `web`/`web-evil` 相邻前缀目录拒绝回归。
- 补充 worker 停止后 dispose 顺序及 worker 未退出时禁止 dispose 的回归测试。
- 面试准备仅新增 Repository 内部租约心跳与 fencing CAS：30 秒租约、10 秒续签、统一 UTC 时钟边界；不改变 API、数据库结构、Provider 输入、证据校验或失败语义。
- 心跳续签结果未知时立即停止后续续签，最终仍由 fencing CAS 判定是否可写；补充了锁失败、迟到结果和资源清理回归。

## 静态与测试门禁

| 检查 | 结果 |
|---|---|
| `uv run ruff check .` | 通过 |
| `uv run mypy src` | 通过，64 个源文件 |
| `npm.cmd run build`（`web`） | 通过 |
| `uv run pytest tests/test_calendar_api.py -q` | 3 passed |
| `git diff --check origin/main..HEAD` | 通过 |

### 后端五组门禁

完整收集 manifest：**1767 个 node id**。五组均退出码 0；聚合校验确认无重复、并集与完整 manifest 一致，五组完成标记、JUnit 和收集摘要均匹配。

| 分组 | 收集/执行 | 允许 skip |
|---|---:|---:|
| agent | 423 / 423 passed | 0 |
| domain | 70 / 70 passed | 0 |
| knowledge | 659 / 655 passed | 4 |
| proposals | 302 / 302 passed | 0 |
| misc | 312 / 312 passed | 0 |
| 合计 | 1767 / 1763 passed | 4 |

Knowledge 的 4 个 skip 仅为既定 Windows 符号链接权限条件，node id 为：

- `tests/test_knowledge_ingest_integrity.py::test_failed_commit_cleanup_does_not_follow_symlink`
- `tests/test_knowledge_reset.py::test_cli_rejects_knowledge_root_symlink_with_external_sentinels`
- `tests/test_knowledge_reset.py::test_cli_rejects_legacy_reset_root_symlink_with_external_sentinels`
- `tests/test_knowledge_reset.py::test_cli_does_not_follow_nested_escape_symlink`

分组命令：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows-pytest-groups.ps1 -Group <group> -ResultDir <temp>
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows-pytest-groups.ps1 -Aggregate -ResultDir <temp>
```

前端稳定分组门禁：**103 个文件、727 个测试全部通过**；10 个分组均退出码 0，实际 Vitest JSON 结果文件集合与 manifest 完全一致，收集文件无重复且并集完整：components-core、components-chat、components-interview、components-offer、components-support、features、layout、lib、services、theme。

前端门禁与 smoke 收口专项：`uv run pytest tests/test_frontend_vitest_groups.py tests/test_smoke.py -q` 为 **64 passed**；包含省略 `RepositoryRoot` 的默认根目录、尾部分隔根目录、相邻前缀目录拒绝和最小 fixture 四类路径回归。

| 前端分组 | 文件 / 测试 | 退出码 |
|---|---:|---:|
| components-core | 22 / 108 | 0 |
| components-chat | 11 / 182 | 0 |
| components-interview | 11 / 45 | 0 |
| components-offer | 8 / 45 | 0 |
| components-support | 8 / 32 | 0 |
| features | 12 / 129 | 0 |
| layout | 10 / 65 | 0 |
| lib | 9 / 73 | 0 |
| services | 11 / 47 | 0 |
| theme | 1 / 1 | 0 |

## 隔离运行时验收

| 命令 | 结果 |
|---|---|
| `uv run oc smoke --static-dir web/dist` | 通过 |
| `uv run oc verify --profile local --static-dir web/dist` | 通过；临时隔离目录清理完成 |
| `uv run oc verify --profile real-ai --static-dir web/dist` | 通过 |
| `uv run oc verify-offer-negotiation --static-dir web/dist` | 既有 Offer 专项验收已通过；本轮面试准备租约修复未重复调用该专用入口 |

real-AI 复跑未再出现 `unable to open database file`。本轮完整 real-AI 已通过；Offer 专项的历史验收结果仍按上方独立记录保留，不以本轮面试准备租约修复替代。此前的 Provider 超时记录不再代表本轮最终 real-AI 结果。

真实配置只在隔离临时目录静默复制，未输出、修改或保留密钥；正式数据目录未作为验收写入目标。

## 本地浏览器键盘验收

使用临时隔离服务和中文 Offer“星云数据｜后端工程师”，通过内置浏览器完成 Pilot 主动选择 Offer 后打开覆盖式谈薪工作区：

- 对话框存在 `role="dialog"`、`aria-modal="true"`，动态可访问名称为“为 星云数据 准备谈薪”。
- 打开后焦点进入“关闭”按钮，且焦点位于对话框内部。
- `Shift+Tab` 从首个焦点循环到最后一个可用输入控件；`Tab` 从最后一个可用控件循环回首个控件；焦点未离开对话框。
- `Escape` 关闭对话框，并将焦点恢复到“准备谈薪”触发按钮。
- 本次键盘验收仅执行本地 UI 操作，未调用 Provider，未创建谈薪 Proposal 或 Brief。

## 清理与剩余风险

- 已停止临时服务、浏览器标签及测试后台进程，并删除本轮创建的临时数据、Vitest JSON 报告、manifest 和分组结果目录。
- 产品代码与测试门禁没有新增 skip；4 个既定符号链接权限 skip 已逐项核验。
- 本轮发布门禁与完整 real-AI verify 均通过；未推送、未合并。后续若 Provider 再次出现超时，仍须按既有结果未知语义记录并重新验收，不能通过放宽 JSON、证据、HITL 或错误语义解决。
- 本报告不包含密钥、简历/JD 原文、模型原文或 Provider 原始请求标识。

## 2026-08-04 面试准备租约心跳追加验收

实现基线：`6fcbee907f6cbf4684a0bb50b63db3db9e17003d`。本追加记录覆盖该基线之后的面试准备 Repository、并发回归与发布门禁；未修改计划文件、设计文件、API、迁移、前端、Offer 或证据契约。

### 代码与门禁

| 命令 | 退出码 / 结果 |
|---|---|
| `uv run pytest tests/test_interview_preparation_repository.py tests/test_interview_preparation_api.py tests/test_interview_preparation_ai.py tests/test_interview_preparation_migrations.py tests/test_smoke.py -q` | 0；103 passed |
| `uv run ruff check src tests` | 0 |
| `uv run mypy src` | 0；64 source files |
| `git diff --check 6fcbee907f6cbf4684a0bb50b63db3db9e17003d..HEAD` | 0 |

后端分组最终收集 **1766** 项，分组结果为：agent `423/423`、domain `70/70`、knowledge `659/655`（4 个既定 Windows 符号链接权限 skip）、proposals `302/302`、misc `312/312`；每组退出码 0，aggregate 退出码 0，node id 无重复且并集与完整 manifest 一致。允许 skip 仍仅为本报告上方列出的 4 项及其精确原因。

前端分组重新收集 **103 个文件、727 个测试**，10 组退出码均为 0，aggregate 退出码为 0；实际结果文件集合与 manifest 完全一致，无重复 node id。前端未因本次后端改动产生源码变化。

### 隔离运行时与真实 Provider

- `uv run oc verify --profile local --static-dir web/dist`：退出码 0；临时数据清理完成。
- 面试准备专项 real-AI：使用现有 `config.json` 的静默隔离副本和临时数据目录；退出码 0。请求体 **183 bytes**，耗时 **60889 ms**，单一 Attempt，响应 `201 ready/normal`，脱敏诊断中未输出配置、模型原文或完整请求。源数据目录前后文件哈希一致，临时数据库、服务、worker 和 engine 均已清理。
- `uv run oc verify --profile real-ai --static-dir web/dist`：退出码 0；完整流程通过，包含面试准备、材料、岗位评估、面试复盘、知识沉淀与模拟面试等既有验收步骤。

本轮未放宽严格 JSON、证据校验、HITL、502 或幂等语义；没有新增业务重试或 Provider 调用。发布剩余风险仅为外部 Provider 响应稳定性，不由本次租约修复掩盖或重新分类。

## 2026-08-05 面试准备租约复审修订

本轮代码提交：`ebf9fa7`（租约心跳有界重试、接管后关闭数据库 session）与 `7af5a01`（Provider 调用链及 fencing 回归）。当前代码验收基线为 `7af5a01`；未修改 API、数据库迁移、前端、Offer、证据契约或错误语义。

### 最新验证

| 命令 | 退出码 / 结果 |
|---|---|
| `uv run pytest tests/test_interview_preparation_repository.py tests/test_interview_preparation_api.py tests/test_interview_preparation_ai.py tests/test_interview_preparation_migrations.py -q` | 0；51 passed |
| `uv run pytest tests/test_smoke.py -q` | 0；56 passed |
| `uv run ruff check .` | 0 |
| `uv run mypy src` | 0；64 source files |
| `npm.cmd run build`（`web`） | 0 |
| `uv run oc smoke --static-dir web/dist` | 0；Smoke passed |
| `uv run oc verify --profile local --static-dir web/dist` | 0；local verify passed |
| `uv run oc verify --profile real-ai --static-dir web/dist` | 0；完整 real-AI verify passed |

后端五组最终以同一完整 manifest 聚合通过：**1766 collected，1762 passed，4 个既定 Windows 符号链接权限 skip**；agent `423/423`、domain `70/70`、knowledge `659/655`、proposals `302/302`、misc `312/312`，无重复 node id，aggregate 退出码 0。前端十组以当前 `web/src` manifest 聚合通过：**103 个文件、727 tests**，无重复且结果集合与 manifest 一致。

real-AI 首次复跑在面试准备请求出现一次 `ReadTimeout`；未修改配置、契约或重试语义，随后以同一真实配置的静默隔离副本重跑完整流程并通过。该次波动仍记录为外部 Provider 稳定性风险；没有输出密钥、配置、用户原文、模型原文或完整 Provider 请求。

临时数据库、服务、worker、engine、前端测试进程与浏览器相关临时资源均已清理；本次门禁结果目录仍保留在系统 Temp 供复核（当前执行环境拒绝删除命令），不在工作树且不含配置或密钥。工作区干净，未推送、未合并。

## 2026-08-05 当前 HEAD 后端五组门禁重跑

本次重跑起始 commit：`f6b633c4ac247131f3d92ca882a2489981426a1c`。新增的 lease replay 回归测试已纳入同一份完整 manifest；结果目录保留于系统 Temp：`D:\Users\yuqi.chen\AppData\Local\Temp\offerpilot-offer-negotiation-backend-20260805-113702`。

执行命令：

```powershell
uv run pytest --collect-only -q --disable-warnings
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows-pytest-groups.ps1 -Group <group> -ResultDir <temp>
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows-pytest-groups.ps1 -Aggregate -ResultDir <temp>
```

结果：完整 manifest **1767 collected**；五组均退出码 0，aggregate 退出码 0，无重复 node id，实际并集与 manifest 完全一致。agent `423/423 passed`、domain `70/70 passed`、knowledge `659 collected / 655 passed / 4 allowed skips`、proposals `303/303 passed`、misc `312/312 passed`，合计 **1767 collected / 1763 passed / 4 allowed skips**。4 个 skip 仍仅为既定 Windows 符号链接权限条件，node id 与原因逐项匹配。
