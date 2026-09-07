# Architecture Rules

> 显式约束注册表。每条 RULE 描述"绕过代价极大"或"被破坏过 ≥1 次"的规则。
> 触发与使用指南见 [`documentation-rules.md`](./documentation-rules.md) §5。

## 当前记录

_(intentional empty registry — 当前无记录。首次新增时按下方模板,从 RULE-1 起编号。)_

> **不是 placeholder**:这是真实的"暂无记录"状态,与"禁止 TODO/待补充"规范不冲突。当首条 RULE 落地后,本段会被具体条目替换。

## RULE 模板

```markdown
### RULE-N: [一句话规则]

**规则**:[具体的、可验证的规则描述,不要模糊]

**Why**:[为什么这条规则存在。来自 bug 引用 BUG-XX,来自 ADR 引用 ADR-XXXX。≤ 5 行]

**How to apply**:[具体执行方式。改动 X 时检查什么、测试如何覆盖、grep 什么关键字验证]

**关联**:[BUG-XX / ADR-XXXX / 相关 RULE-XX]
```

## 何时新增 RULE

- bug 修复后,某个 bug 揭示了"不该再被破坏"的规则(尤其已出现 2 次以上)
- 架构决策落地,有"绕过代价极大"特性(如领域红线、依赖方向、单一入口)
- code review 反复纠正,reviewer 在 3 个 PR 里都纠正了同一件事

**不应新增**:语言通用规范(交给 linter)、ADR 已完整说明的决策、一次性 bug 无通用教训、实现细节。
