#!/usr/bin/env bash
# PreToolUse hook: git commit 前提醒文档自检
# 触发条件: Bash 工具 + 命令含 "git commit" + staged 改动含 .md 文件
# 行为: 输出清单到 stderr, exit 0 (不阻塞)
# 注册位置: .claude/settings.json; 仅 Claude Code 匹配的工具调用触发

set -euo pipefail

input=$(cat)

tool_name=$(echo "$input" | jq -r '.tool_name // empty' 2>/dev/null || echo "")
if [[ "$tool_name" != "Bash" ]]; then
  exit 0
fi

command=$(echo "$input" | jq -r '.tool_input.command // empty' 2>/dev/null || echo "")
if ! echo "$command" | grep -qE 'git commit'; then
  exit 0
fi

# 仅当 staged 改动含 .md 文件时才提醒
if ! git diff --cached --name-only 2>/dev/null | grep -qE '\.md$'; then
  exit 0
fi

cat <<'EOF' >&2

文档自检：检查本次 Markdown 改动的链接、事实源一致性、占位内容和 diff。
仅架构决策需要 ADR；普通措辞修改不额外生成文档。
历史 spec / plan 不按日期或版本自动删除；归档按明确维护任务执行。
验证范围、文档选择和例外见 docs/architecture/documentation-rules.md。
这是非阻塞提醒，不要求额外审批；仅处理本次任务适用的项目。

EOF

exit 0
