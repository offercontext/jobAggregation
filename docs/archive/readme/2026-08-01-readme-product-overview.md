# README Product Overview Implementation Plan

> 历史施工计划，已归档。下文命令、勾选框与技能要求仅供追溯，不再作为当前执行指令；截图与 README 维护以 [用户指南维护记录](../../product-manual/维护记录.md)为准。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the technical, duplicated README with a concise Chinese-first product overview that accurately presents OfferPilot through five real, bright, wide-screen screenshots.

**Architecture:** This is a documentation-only change. A narrow public README becomes the canonical entry point; a small pytest module verifies its product claims, local startup paths, screenshot references, and image dimensions. Five versioned PNG files are generated from an isolated local deployment using a Chinese synthetic candidate and are referenced by relative Markdown paths.

**Tech Stack:** Markdown, pytest, Pillow, local `uv run oc start`, React production build, Codex in-app browser.

---

## File structure

| Path | Responsibility |
| --- | --- |
| `README.md` | Concise public, Chinese-first product introduction and quick start. |
| `tests/test_cutover_files.py` | Replace obsolete README assertions for the former technical inventory with stable public-runtime assertions. |
| `tests/test_readme_product_overview.py` | Verify the five README screenshot files, relative links, public boundaries, image format, and wide dimensions. |
| `docs/assets/readme/2026-08-01/*.png` | Five checked-in screenshots from the isolated Chinese “筱哲” demonstration. |

`docs/*` is ignored by default. Force-add the plan, screenshots, and other documentation assets with `git add -f`; do not change `.gitignore` just to track this release artifact.

### Task 1: Replace obsolete README contract assertions with product-overview checks

**Files:**

- Modify: `tests/test_cutover_files.py`
- Create: `tests/test_readme_product_overview.py`

- [ ] **Step 1: Replace the README assertions that require an internal v0.1 inventory**

  In `tests/test_cutover_files.py`, keep `test_readme_describes_python_first_runtime`, but make it verify the durable source-start facts instead of FastAPI wording:

  ```python
  def test_readme_describes_python_first_runtime():
      readme = (ROOT / "README.md").read_text(encoding="utf-8")

      assert "uv sync" in readme
      assert "uv run oc start" in readme
      assert "docker build -t offerpilot ." in readme
      assert "go build" not in readme
      assert "Go 1.22" not in readme
  ```

  Replace `test_readme_documents_current_v01_contract` with the following public product promise test. Do not retain assertions for LiteLLM, Skill registry commands, wakeups, FTS retrieval, auth token storage, or historical schema details: those are implementation details deliberately removed from the README.

  ```python
  def test_readme_states_the_product_boundary_and_core_capabilities():
      readme = (ROOT / "README.md").read_text(encoding="utf-8")

      for text in [
          "# OfferPilot — 本地优先的 AI 求职工作台",
          "管理简历与投递",
          "评估岗位匹配、准备投递材料",
          "准备面试、进行文本模拟与复盘",
          "汇总已确认的经验与知识",
          "比较 Offer 已知事实，准备谈薪沟通",
          "不自动投递",
          "不替用户决定",
          "SQLite",
          "AGPLv3",
          "## English",
      ]:
          assert text in readme
  ```

  In `test_docker_smoke_scripts_document_container_smoke_path` and `test_local_smoke_scripts_exercise_oc_start_with_built_spa`, remove only the assertions that require the public README to list release smoke scripts. Preserve the script-level behavior assertions; smoke-script documentation belongs in the release checklist, not this concise README.

- [ ] **Step 2: Add the initially failing screenshot/Markdown contract test**

  Create `tests/test_readme_product_overview.py` with the exact file list and public checks below. Pillow is already a project dependency, so do not add a dependency.

  ```python
  from pathlib import Path

  from PIL import Image


  ROOT = Path(__file__).resolve().parents[1]
  SCREENSHOTS = [
      "01-workspace-overview.png",
      "02-application-materials.png",
      "03-pilot-confirmation.png",
      "04-interview-practice.png",
      "05-offer-negotiation.png",
  ]
  ASSET_DIR = ROOT / "docs" / "assets" / "readme" / "2026-08-01"


  def test_readme_references_five_wide_product_screenshots():
      readme = (ROOT / "README.md").read_text(encoding="utf-8")

      for filename in SCREENSHOTS:
          relative_path = f"docs/assets/readme/2026-08-01/{filename}"
          assert relative_path in readme
          image_path = ASSET_DIR / filename
          assert image_path.is_file()
          with Image.open(image_path) as image:
              assert image.format == "PNG"
              width, height = image.size
              assert width >= 1440
              assert height >= 800
              assert width / height >= 1.25
              colors = image.convert("RGB").resize((64, 36)).getcolors(64 * 36)
              assert colors is not None and len(colors) > 16


  def test_readme_keeps_pilot_and_offer_negotiation_visible():
      readme = (ROOT / "README.md").read_text(encoding="utf-8")

      assert "Pilot 有何不同" in readme
      assert "谈薪" in readme
      assert "自动完成投递" not in readme
      assert "替你筛选最优 Offer" not in readme
  ```

- [ ] **Step 3: Run the new tests and confirm the expected failure**

  Run:

  ```powershell
  uv run pytest tests/test_cutover_files.py tests/test_readme_product_overview.py -q
  ```

  Expected: failure because `README.md` still contains the former contract, `tests/test_readme_product_overview.py` has no matching PNG assets, and the five relative image paths do not exist yet.

- [ ] **Step 4: Commit the test contract**

  ```powershell
  git add tests/test_cutover_files.py tests/test_readme_product_overview.py
  git commit -m "test: AI verify README product overview assets"
  ```

### Task 2: Create the isolated Chinese demonstration and five source screenshots

**Files:**

- Create: `docs/assets/readme/2026-08-01/01-workspace-overview.png`
- Create: `docs/assets/readme/2026-08-01/02-application-materials.png`
- Create: `docs/assets/readme/2026-08-01/03-pilot-confirmation.png`
- Create: `docs/assets/readme/2026-08-01/04-interview-practice.png`
- Create: `docs/assets/readme/2026-08-01/05-offer-negotiation.png`

- [ ] **Step 1: Start an isolated local deployment without touching user data**

  Use a temporary directory outside the repository. Copy only the existing `~/.offerpilot/config.json` into the temporary data directory when it exists; do not print, edit, commit, or screenshot its contents. Build the SPA and start the application against the temporary directory on an unused loopback port.

  ```powershell
  $repo = (Get-Location).Path
  $tempData = Join-Path ([IO.Path]::GetTempPath()) ("offerpilot-readme-" + [guid]::NewGuid())
  New-Item -ItemType Directory -Force -Path $tempData | Out-Null
  $sourceConfig = Join-Path $HOME ".offerpilot\config.json"
  if (Test-Path -LiteralPath $sourceConfig) {
    Copy-Item -LiteralPath $sourceConfig -Destination (Join-Path $tempData "config.json")
  }
  Set-Location "$repo\web"
  npm.cmd run build
  Set-Location $repo
  $port = 18080
  $previousOfferpilotData = $env:OFFERPILOT_DATA
  $env:OFFERPILOT_DATA = $tempData
  $server = Start-Process -FilePath "uv" -ArgumentList @("run", "oc", "start", "--port", "$port") -PassThru -WindowStyle Hidden
  ```

  Poll `http://127.0.0.1:$port/api/health` until it returns HTTP 200 before opening the in-app browser. If port 18080 is occupied, choose another currently unused loopback port and use that single port consistently. Restore the previous `OFFERPILOT_DATA` value and remove `$tempData` in a `finally` block after all captures and checks.

- [ ] **Step 2: Create only the synthetic Chinese scenario through the running product**

  In the in-app browser, set light theme and a `1600 × 1000` or wider desktop viewport. Create and use only these synthetic records:

  - candidate / resume title: `筱哲 · AI 应用工程师`;
  - application: `云栖智能（演示）` / `AI 应用工程师`, status `已投递`;
  - Chinese JD with Python、FastAPI、LLM 应用、可观测性、上海混合办公 requirements;
  - one future valid interview event: `技术一面`;
  - one synthetic Offer for `云栖智能（演示）`, with clearly fictional compensation notes;
  - one Pilot question: `请为云栖智能新增一条 AI 应用工程师投递，先展示确认卡，不要直接保存。`

  Never use the user’s existing resumes, applications, chat history, offers, browser tabs, provider diagnostics, API keys, or personal identifiers. When a product action writes data, use the visible confirmation control; do not seed the database directly.

- [ ] **Step 3: Capture the exact five screenshots from the desktop UI**

  Save each native browser screenshot as a 1440px-or-wider PNG in `docs/assets/readme/2026-08-01/`, preserving the exact names below:

  | File | Required visible state |
  | --- | --- |
  | `01-workspace-overview.png` | Bright desktop workspace, Chinese navigation, Chinese “筱哲” scenario, and meaningful application progress; no collapsed/mobile layout. |
  | `02-application-materials.png` | The `云栖智能（演示）` application detail showing its material/fit context and a current or frozen source label. It must not expose model raw output or a loading/error screen. |
  | `03-pilot-confirmation.png` | The Pilot right rail fully visible beside the workspace, including the Chinese user question, Pilot response, and full-width human confirmation card. It must visibly communicate that the write is awaiting user confirmation. |
  | `04-interview-practice.png` | Interview preparation, text mock interview, or review in the same Chinese scenario. Show a real ready/confirmed state, not a blank precondition form, skeleton, or provider error. |
  | `05-offer-negotiation.png` | Existing Offer center plus the single-offer negotiation/coaching entry point or active coaching context. Do not imply the unmerged offer-comparison redesign exists. |

  Do not crop a narrow panel into a wide canvas or add decorative mockups. The captured viewport itself must be wide. Do not include browser chrome, blank lower space, scrollbars caused by capture overflow, overlayed devtools, error toasts, credentials, or real user data.

- [ ] **Step 4: Visually inspect each source image before README use**

  Open every saved PNG with the local image viewer. For each image confirm: bright theme; Chinese text is readable at 100%; no empty canvas below the app; no truncated modal/card; no private information; and its required state in the table is plainly visible. Re-capture any image that fails one of these checks.

  Run the automated asset test after the five images exist:

  ```powershell
  uv run pytest tests/test_readme_product_overview.py -q
  ```

  Expected: the image checks pass once README is updated in Task 3; until then the test may fail only for missing README links.

- [ ] **Step 5: Stop and clean the demonstration deployment**

  ```powershell
  if ($server -and -not $server.HasExited) { Stop-Process -Id $server.Id -Force }
  if ($null -eq $previousOfferpilotData) {
    Remove-Item Env:OFFERPILOT_DATA -ErrorAction SilentlyContinue
  } else {
    $env:OFFERPILOT_DATA = $previousOfferpilotData
  }
  Remove-Item -LiteralPath $tempData -Recurse -Force
  ```

  Verify no `offerpilot-readme-*` temporary directory remains and that the screenshots are the only retained demonstration artifacts.

### Task 3: Rewrite the public README around the approved product narrative

**Files:**

- Modify: `README.md`
- Modify: `tests/test_cutover_files.py`
- Create: `docs/assets/readme/2026-08-01/*.png`

- [ ] **Step 1: Replace the old duplicated inventory with the approved Chinese-first structure**

  Replace `README.md` in full. Use precisely these top-level Chinese sections, in order:

  ```markdown
  # OfferPilot — 本地优先的 AI 求职工作台

  > 把简历、投递、面试与 Offer 放在一个由你掌控的本地工作台；AI 提供建议，你决定下一步。

  ## 它能帮你做什么
  ## 真实界面
  ## Pilot 有何不同
  ## 快速开始
  ## 隐私与边界
  ## English
  ## 许可证
  ```

  Under `它能帮你做什么`, include only these five short capability bullets:

  ```markdown
  - 管理简历与投递
  - 评估岗位匹配、准备投递材料
  - 准备面试、进行文本模拟与复盘
  - 汇总已确认的经验与知识
  - 比较 Offer 已知事实，准备谈薪沟通
  ```

  Under `真实界面`, insert the five screenshots in numerical order using relative Markdown paths and the exact one-line captions from the design document. Give each `alt` text that names the feature in Chinese. Do not use HTML width attributes, image tables, image collages, or external image URLs.

- [ ] **Step 2: State Pilot and safety boundaries without technical or marketing overclaim**

  Keep the Pilot section to two or three Chinese sentences covering the current context, visible suggestions/confirmation cards, and user control. The privacy/boundary section must explicitly state local SQLite storage, user-configured AI provider, no automatic application submission, no external action without confirmation, and no decision to accept/reject an Offer on behalf of the user.

  Do not include: current implementation internals, LiteLLM, provider lists, API key JSON examples, CLI inventories, technical stack table, historical roadmap, future offer comparison, offer ranking, salary web lookup, guarantee claims, or any line implying that Pilot can write without confirmation.

- [ ] **Step 3: Include only two verified start paths**

  Under `快速开始`, provide a Docker route that builds the repository image and runs it locally:

  ```bash
  docker build -t offerpilot .
  docker run --rm -p 8080:8080 -v offerpilot-data:/data offerpilot
  ```

  Follow it with the source route:

  ```bash
  git clone https://github.com/offercontext/offerpilot.git
  cd offerpilot
  uv sync
  cd web && npm ci && npm run build
  cd ..
  uv run oc start
  ```

  The compact English section must repeat only: the English one-line positioning, five-category product summary in prose, the source start command block, the local/confirmation/no-auto-apply boundary, and the AGPLv3 link. Do not recreate the former full English technical inventory.

- [ ] **Step 4: Run the documentation contract tests**

  Run:

  ```powershell
  uv run pytest tests/test_cutover_files.py tests/test_readme_product_overview.py -q
  ```

  Expected: PASS. If an assertion fails because a removed internal phrase is still required, update that test only when it is a former README-internal detail; do not reintroduce the detail solely to satisfy a stale assertion.

- [ ] **Step 5: Commit the README and images**

  The screenshots and most docs paths are ignored. Force-add only the five listed images, never the isolated runtime data/configuration directory.

  ```powershell
  git add README.md tests/test_cutover_files.py tests/test_readme_product_overview.py
  git add -f docs/assets/readme/2026-08-01/01-workspace-overview.png
  git add -f docs/assets/readme/2026-08-01/02-application-materials.png
  git add -f docs/assets/readme/2026-08-01/03-pilot-confirmation.png
  git add -f docs/assets/readme/2026-08-01/04-interview-practice.png
  git add -f docs/assets/readme/2026-08-01/05-offer-negotiation.png
  git commit -m "docs: AI refresh product README"
  ```

### Task 4: Final documentation and visual verification

**Files:**

- Verify: `README.md`
- Verify: `docs/assets/readme/2026-08-01/*.png`
- Verify: `tests/test_cutover_files.py`
- Verify: `tests/test_readme_product_overview.py`

- [ ] **Step 1: Verify Markdown and all image links resolve locally**

  Run the automated tests and a Markdown parse without changing files:

  ```powershell
  uv run pytest tests/test_cutover_files.py tests/test_readme_product_overview.py -q
  uv run python -c "from pathlib import Path; from markdown_it import MarkdownIt; tokens = MarkdownIt().parse(Path('README.md').read_text(encoding='utf-8')); assert any(token.type == 'image' for token in tokens); print('README Markdown parsed')"
  git diff --check main..HEAD
  ```

  Expected: both commands exit 0; `git diff --check` has no output.

- [ ] **Step 2: Perform a final side-by-side visual review**

  Read the rendered README in a Markdown preview and open each PNG at full size. Confirm that the first screen explains the product before any technical commands; each image remains legible without clicking; the Pilot image includes prompt, response, and confirmation; and the Offer image visibly includes negotiation support. Reject and replace images with empty bottom area, a narrow/mobile shell, a cropped confirmation card, unreadable text, raw model content, or sensitive information.

- [ ] **Step 3: Confirm repository scope and clean status**

  ```powershell
  git diff --name-only main..HEAD
  git status --short --branch
  ```

  Expected changed paths are the README, the two README-focused tests, five screenshot assets, and the design/plan documents. There must be no backend, API, database, AI behavior, UI implementation, user config, temporary data directory, or unrelated worktree changes.

- [ ] **Step 4: Record the final handoff accurately**

  Report the two test commands and their exit results, that screenshots were taken from an isolated local deployment using synthetic candidate “筱哲”, that light/wide visual inspection passed, and any command that could not run. Do not claim a real AI acceptance test was run merely because screenshots show an existing UI state.
