# Claude Code 项目入口

先读取 [AGENTS.md](AGENTS.md)，跨 Agent 的工作流、授权边界、业务约束、验证与汇报规则统一以该入口为准。写改 Markdown 时读取 [文档规范](docs/architecture/documentation-rules.md)。

## 按任务定位

- 产品 PRD / ADR / Check 表：AGENTS.md §4 的飞书入口。
- REST / SQLite / CLI 契约：[python-rewrite-contract.md](docs/python-rewrite-contract.md)。
- Knowledge / Memory / retrieval：[knowledge-system.md](docs/architecture/knowledge-system.md)。
- 发布验收：[p0-release-checklist.md](docs/p0-release-checklist.md) 与 `scripts/release-gate.sh`。
- 飞书文档或画板编辑：[操作参考](docs/architecture/lark-document-operations.md)。

## 开发定位

以下是代码入口，不替代当前实现与测试；不要依赖固定路由数量、文件行数或历史导航清单。

| 任务 | 入口 |
|---|---|
| CLI / 服务启动 | `src/offerpilot/cli.py` |
| HTTP / middleware / SPA | `src/offerpilot/api.py` |
| 领域查询 | `src/offerpilot/repositories/` |
| 数据模型 / 迁移 / schema | `src/offerpilot/models.py`、`db.py`、`schemas.py`（均在 `src/offerpilot/`） |
| Agent / provider / tools | `src/offerpilot/ai/` |
| 配置 / Skill 信任 | `src/offerpilot/config.py`、`src/offerpilot/skills.py` |
| 前端导航 / 页面 / API 客户端 | `web/src/` 下的 layout、components、features、services |

- 后端测试在 `tests/`；前端测试在源码附近的 `*.test.ts(x)`。repository 测试使用临时目录中的真实 SQLite，不 mock repository。
- 安装依赖：仓库根目录 `uv sync`，`web/` 目录 `npm install`。
- 开发启动：根目录 `uv run oc start`；前端开发在 `web/` 执行 `npm run dev`。
- 定向测试：`uv run pytest tests/<file>.py`；前端在 `web/` 执行 `npm test -- --run <file>`。完整 gate 的适用范围见 AGENTS.md §7。
- `.claude/settings.json` 注册了文档提醒 hook；它只在 Claude Code 的匹配工具调用中提示，不是跨 Agent 的 Git hook，也不替代验证。
