# 文档规范

写改 Markdown 时使用本规范。指令优先级与授权边界见 [AGENTS.md](../../AGENTS.md) §0；流程建议不能增加未要求的审批或文档工作。

## 1. 按变更选择文档

- 改变架构决策（模块边界、协议、依赖方向、领域契约）时写改 `docs/architecture/decisions/00NN-*.md`；普通措辞修订和实现细节不需要 ADR。
- Bug 修复在 `docs/BUGS.md` 记录现象、根因、修复、教训；只有可复用约束才登记到 [rules.md](rules.md)。
- 命令、配置或安装行为变化，更新其实际维护位置；仅涉及公开使用承诺时更新 README，不要求同时在 AGENTS.md 复制。
- 迭代结束后将仍有效的决策归入事实源。spec / plan 可作历史快照，标明状态或被替代关系；归档、删除按明确维护任务执行，不根据日期或版本名自动删文件。
- 其余情况优先修改现有文档，不为普通实现选择额外生成 ADR、RULE 或报告。

## 2. 长度与结构

| 文档 | 建议上限 |
|---|---|
| 根 AGENTS.md、docs 下普通文档 | 300 行 |
| ADR（含备选方案） | 800 行 |
| RULE 的 Why | 5 行 |
| BUGS 单条 | 60 行 |

这些是软上限；确需超限时在文档头说明原因，不为凑行数拆散契约。新工程文档通常放 `docs/architecture/` 或 `docs/superpowers/`，现有其他事实源路径保留。不提交未完成的占位内容；模板中的填写提示和明确的“暂无记录”状态不属于未完成文档。

## 3. 事实源与引用

同一规范只维护一处，其余文件链接到它。公开文档可保留用户理解所需摘要，必须与事实源一致；历史快照不自动成为当前执行指令。

| 内容 | 维护位置 |
|---|---|
| 授权、Git、工作流、领域红线、验收规则 | [AGENTS.md](../../AGENTS.md)（领域红线 §6） |
| Python rewrite 契约 | [python-rewrite-contract.md](../python-rewrite-contract.md) |
| P0 发布清单 | [p0-release-checklist.md](../p0-release-checklist.md) |
| 产品 PRD / ADR / Check 表 | AGENTS.md §4 的飞书 wiki |
| Knowledge 架构 | [knowledge-system.md](knowledge-system.md) |
| 架构决策 | `decisions/00NN-*.md` |
| 补充工程约束 | [rules.md](rules.md) |
| 飞书编辑经验 | [lark-document-operations.md](lark-document-operations.md) |

AGENTS.md 保留跨任务约束及路由；工具专用流程放相关参考。CLAUDE.md 和 `.claude/rules/` 只作入口适配，不重复业务规范。

## 4. ADR 必需内容

ADR 包含标题与编号、Status / 日期、Decider、依据，以及以下段落：

- Context：问题、业务需求和约束。
- Decision：决定的行为或边界。
- Consequences：收益、代价、风险。
- Alternatives Considered：至少两个真实备选及未采用原因。
- Related：关联文档或规则。

不要为了填写模板捏造备选或决策人；缺失的信息影响决策时再询问。

## 5. RULE 的使用

涉及领域事件、对话上下文、版本边界、auth、迁移、Agent runtime、Skill 信任、provider routing 或 HITL 时，读取 [rules.md](rules.md) 中适用条目，并遵守 AGENTS.md §6。

RULE 用于修复或审查证明有必要持续维护的约束，不记录语言常识或复制 ADR。条目格式与新增条件统一在 rules.md。

若请求与现有规则冲突，先核对用户是否已明确授权改变该约束；没有授权时指出具体冲突并询问，已获授权则同步更新事实源并验证。PR 或最终汇报记录理由，写下理由本身不构成绕过授权。

## 6. 提交前检查

检查本次修改的链接、事实源一致性、占位内容和 diff；只补与改动相关的文档。验证强度按 AGENTS.md §7。

`.claude/hooks/pre-commit-doc-check.sh` 已在 `.claude/settings.json` 注册为 Claude Code 的 PreToolUse 提醒，输出到 stderr、exit 0。它不阻塞提交，也不保证 Codex 或直接 Git CLI 会执行；其他入口自行完成适用检查。
