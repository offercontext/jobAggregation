# OfferPilot Python Cutover Verification

> 历史快照：记录 2026-07-06 的 Python 切换验收，不代表当前测试数量或发布状态。当前发布要求见 [P0 发布清单](../p0-release-checklist.md)。

Date: 2026-07-06
Branch: `feature/20260705-python-rewrite`

## Cutover Summary

OfferPilot has been cut over to a Python-first backend and CLI:

- Backend/API: FastAPI under `src/offerpilot/api.py`
- CLI: Typer `oc` console script under `src/offerpilot/cli.py`
- Persistence: SQLite + SQLAlchemy models/repositories under `src/offerpilot`
- AI: provider adapters, tool-loop safety, and reusable workflows under
  `src/offerpilot/ai`
- Frontend: existing React/Vite SPA served by the Python process from `web/dist`
- Packaging: `pyproject.toml`, `uv.lock`, Python Dockerfile, and `uv tool install`
- Legacy Go runtime: removed from the active source tree after user-approved
  final cutover

## Verification Evidence

Latest local checks performed during cutover:

| Area | Command / evidence | Result |
|---|---|---|
| Python tests | `uv run pytest -q` | 85 passed, 1 Starlette/httpx deprecation warning |
| Python lint | `uv run ruff check .` | passed |
| Python type check | `uv run mypy src` | passed |
| Frontend tests | `npm test` in `web/` | 29 passed |
| Frontend build | `npm run build` in `web/` | passed |
| Local install path | `./scripts/install.sh --source . --install-dir <tmp>` then `<tmp>/oc --help` | installed `oc`; CLI help showed Python commands |
| HTTP smoke | Python `oc start --port 8765` + health/home/app/dashboard requests | API and SPA fallback returned expected data |
| Browser smoke | In-app browser loaded `http://127.0.0.1:8765/` | React root rendered, title correct, no console errors |
| Docker runtime | Static Dockerfile tests | Dockerfile uses Python/uv and copies `web/dist`; Docker CLI unavailable locally |

## CR-Style Review

### Blocking Issues

None found in the verified local cutover path.

### Accepted Constraints

- Docker could not be built locally because the `docker` command is not
  installed in this environment. The Dockerfile is covered by static tests and
  should be built in CI or on a machine with Docker before release.
- SQLite migration remains idempotent startup DDL rather than Alembic. This is
  acceptable for this cutover because existing table and column names are
  preserved, and compatibility columns are additive.
- API and CLI share AI workflow code for newly completed CLI AI commands. Some
  older API helper functions can still be consolidated in a later cleanup.

### Follow-Up Recommendations

- Add CI jobs for Python tests, frontend tests/build, and Docker build.
- Add browser smoke automation as a scripted test once the project chooses a
  standard browser runner.
- Consolidate remaining API-only AI helper functions into `offerpilot.ai.workflows`.
