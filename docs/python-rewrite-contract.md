<!-- 超限原因: 本文冻结 Python rewrite 的跨模块兼容面，并保留 Go/旧 Knowledge 历史基线。 -->

# OfferPilot Python Rewrite Contract

> Status: Python cutover completed; see the [2026-07-06 verification snapshot](archive/python-cutover-verification.md).
> 本文保留迁移兼容契约与历史 Go 基线，不是当前 API 全量清单，也不是待执行迁移计划。下文的 Current、Phase、测试数量与 Go 路径按其历史基线阅读；后续明确修订以 [AGENTS.md](../AGENTS.md) 的领域红线、相关实施契约和当前代码／测试为准，未被修订的安全约束继续保留。
> Baseline branch: `feature/20260705-python-rewrite`.
> Source baseline: Go backend at `443c933` from `main`.
> **[KR] Knowledge 历史基线**：旧 knowledge API / 表 / AI tool / CLI 条目（标记 `[KR-deprecated]`）不得作为长期兼容要求重新引入。当前方向见末尾 [Knowledge System](#knowledge-system) 及其链接的唯一架构事实源。

This document freezes the compatibility surface for rewriting OfferPilot from Go
to Python. The Python backend may improve internal structure, but it must keep
the REST API, SQLite data, CLI behavior, and AI write-confirmation semantics
listed here unless a later contract revision explicitly says otherwise.

## Baseline Verification

Run from the worktree root unless noted:

| Command | Current result | Migration use |
|---|---|---|
| `go test ./...` | Historical baseline passed before cutover | Go behavior reference used before deleting the legacy implementation |
| `npm test` in `web/` | Passed, 29 tests | Frontend utility baseline |
| `npm run build` in `web/` | Passed | React API compatibility smoke baseline |
| `npm ci` in `web/` | Passed with 5 audit findings | Dependency audit is tracked as a risk, not part of this rewrite |

## Global Runtime Contract

| Surface | Current behavior | Python rewrite requirement |
|---|---|---|
| Data directory | `cmd/oc/main.go` uses `OFFERPILOT_DATA`; otherwise `~/.offerpilot`; creates directory `0755` | Keep the same environment variable and default directory |
| Database path | CLI and server use `data.db` under the data directory | Keep `data.db`; never create a parallel default path |
| Config path | `config.json` under the data directory | Keep JSON field names and defaults |
| Config defaults | `base_url=https://api.openai.com/v1`, `model=gpt-4o`, `local_port=8080`, `chat_auto_approve_writes=false`, `fallback_provider_id=""` | Preserve defaults and missing-file behavior |
| Docker data | Docker image sets `OFFERPILOT_DATA=/data` and declares `/data` as a volume | Keep a first-class Docker data path in the final cutover image |
| HTTP prefix | All JSON APIs live under `/api`; frontend assets are served from root | Keep `/api` prefix so current React services work |
| JSON casing | API and DB structs expose snake_case JSON fields | Keep snake_case field names |
| CORS | Allows all origins, methods `GET, POST, PUT, DELETE, OPTIONS`, headers `Content-Type, Authorization` | Preserve for local frontend development |
| Time format | Go `time.Time` encodes as RFC3339-like JSON strings; SQLite stores DATETIME text/current timestamp | Python must serialize datetime fields as frontend-compatible strings |
| API errors | `respondError` returns JSON object `{"error": string}` | Preserve error shape for all Python routes |
| HTTP methods | CORS allows `GET, POST, PUT, DELETE, OPTIONS`; no `PATCH` is advertised | Keep compatibility methods first; add PATCH only after frontend contract revision |

## REST API Contract

Route registration lives in `internal/api/router.go`. The React frontend calls
these endpoints through `web/src/services/*` with `baseURL: /api`.

API invariants:

- All API routes are mounted under `/api`; React SPA fallback is outside this prefix.
- JSON responses set `Content-Type: application/json`.
- `OPTIONS` returns `200`.
- Delete endpoints often do not detect no-row deletes today; preserve this unless a test-backed contract revision changes it.

### Applications And Dashboard

| Endpoint | Request | Response | Compatibility notes | Tests to preserve |
|---|---|---|---|---|
| `GET /api/applications?status=` | Optional `status` query | `Application[]`, ordered by `applied_at DESC` | Empty status returns all. Fields match `db.Application` and `web/src/types/application.ts`. | List all, filter by status, empty list |
| `POST /api/applications` | `ApplicationInput`: `company_name`, `position_name`, optional `job_url`, `status`, `notes` | `201 Application` | Requires `company_name` and `position_name`; default `status=applied`, `source=web`, `applied_at=now`. | Required-field 400, default fields, created response |
| `GET /api/applications/{id}` | Path integer `id` | `200 Application` | Bad ID gives 400; missing row gives 404. | Bad id, not found, found |
| `PUT /api/applications/{id}` | Full desired application object, not merge | `200 Application` | Frontend currently sends complete desired state. Python must not silently turn this into partial merge unless frontend is changed. | Full update preserves expected fields |
| `DELETE /api/applications/{id}` | Path integer `id` | `200 {"message":"Deleted"}` | Current handler does not 404 when no row was deleted. | Delete existing and repeated delete behavior |
| `GET /api/dashboard` | None | `{"total": number, "board": Record<string, Application[]>}` | Board groups applications by `status`; missing statuses may be absent. | Status grouping and total |

### Schedule, Calendar, Notes, Offers

| Endpoint | Request | Response | Source | Migration priority |
|---|---|---|---|---|
| `GET /api/application-events` | Optional month/application/event_type filters | `ApplicationEvent[]` | `src/offerpilot/api.py`, `web/src/services/events.ts` | v0.1 |
| `POST /api/application-events` | Application event request body | `201 ApplicationEvent` | `src/offerpilot/api.py` | v0.1 |
| `GET /api/application-events/{id}` | Path id | `ApplicationEvent` | `src/offerpilot/api.py` | v0.1 |
| `PUT /api/application-events/{id}` | Application event request body | `ApplicationEvent` | `src/offerpilot/api.py` | v0.1 |
| `DELETE /api/application-events/{id}` | Path id | status JSON | `src/offerpilot/api.py` | v0.1 |
| `GET /api/calendar` | Query filters | `CalendarEntry[]` | `internal/api/calendar.go`, `web/src/services/calendar.ts` | Phase 5 |
| `GET /api/applications/{id}/notes` | Path app id | `InterviewNote[]` | `internal/api/notes.go` | Phase 5 |
| `POST /api/applications/{id}/notes` | Note body | `InterviewNote` | `internal/api/notes.go` | Phase 5 |
| `GET /api/notes` | Optional filters | `InterviewNote[]` | `internal/api/notes.go` | Phase 5 |
| `POST /api/notes` | Standalone note body | `InterviewNote` | `internal/api/notes.go` | Phase 5 |
| `PUT /api/notes/{id}` | Note body | `InterviewNote` | `internal/api/notes.go` | Phase 5 |
| `DELETE /api/notes/{id}` | Path id | status JSON | `internal/api/notes.go` | Phase 5 |
| `GET /api/offers` | Optional `status` | `Offer[]` | `internal/api/offers.go`, `web/src/services/offers.ts` | Phase 5 |
| `POST /api/offers` | Offer body | `Offer` | `internal/api/offers.go` | Phase 5 |
| `GET /api/offers/compare?ids=` | Comma-separated IDs | Comparison data | `internal/api/offers.go` | Phase 5 |
| `GET /api/offers/{id}` | Path id | `Offer` | `internal/api/offers.go` | Phase 5 |
| `PUT /api/offers/{id}` | Offer body | `Offer` | `internal/api/offers.go` | Phase 5 |
| `DELETE /api/offers/{id}` | Path id | status JSON | `internal/api/offers.go` | Phase 5 |

Compatibility footnotes:

- Events store duration as strings such as `30m`, but the API exposes `duration_minutes`.
- Event type validation allows `written_test`, `interview`, and `assessment`.
- Calendar `month` parsing falls back to the current month instead of returning 400.
- Offers use `422` for semantic validation errors such as bad status, missing company/position, negative money, or invalid application.
- Offer update currently keeps `application_id` immutable even if the request body contains one.

### AI Analysis, Resume, Knowledge, Questions, Material Kits, Mock

| Endpoint | Request | Response | Source | Migration priority |
|---|---|---|---|---|
| `POST /api/jd/analyze` | `jd_text` or `jd_url`, optional `application_id` | JD analysis result and saved row | `internal/api/jd.go`, `web/src/services/ai.ts` | Phase 5 after AI minimum |
| `GET /api/jd/analyses` | Optional `application_id` | `JDAnalysis[]` | `internal/api/jd.go` | Phase 5 |
| `GET /api/jd/analyses/{id}` | Path id | `JDAnalysis` | `internal/api/jd.go` | Phase 5 |
| `POST /api/resumes` | v0.1 structured resume body: `title`, `source`, `content_json` | `Resume` with completion metadata | `src/offerpilot/api.py`, `web/src/services/resumes.ts` | v0.1 complete |
| `GET /api/resumes` | None | Active `Resume[]` | `src/offerpilot/api.py`, `web/src/services/resumes.ts` | v0.1 complete |
| `POST /api/resumes/upload` | Multipart PDF upload, one file <= 10 MB | Resume row with `source=upload`, file path, text parse status | `src/offerpilot/api.py`, `web/src/services/resumes.ts` | v0.1 complete |
| `POST /api/resumes/from-sample` | Optional sample id/title | Structured sample resume | `src/offerpilot/api.py`, `web/src/services/resumes.ts` | v0.1 complete |
| `GET /api/resumes/{id}` | Path id | `Resume` | `src/offerpilot/api.py` | v0.1 complete |
| `PATCH /api/resumes/{id}` | Partial v0.1 fields: `title`, `content_json`, `career_intent`, `is_master`, `source` | Updated `Resume` | `src/offerpilot/api.py`, `web/src/services/resumes.ts` | v0.1 complete |
| `POST /api/resumes/{id}/copy` | Optional title | Copied non-master resume | `src/offerpilot/api.py`, `web/src/services/resumes.ts` | v0.1 complete |
| `DELETE /api/resumes/{id}` | Path id | status JSON | `src/offerpilot/api.py`, `web/src/services/resumes.ts` | v0.1 complete |
| `POST /api/resumes/{id}/match` | JD text/url and optional app id | Match result | `src/offerpilot/api.py`, `web/src/services/resumes.ts` | v0.2/deep JD flow deferred; compatibility endpoint present |
| `GET /api/resumes/{id}/matches` | Path id | `ResumeMatch[]` | `src/offerpilot/api.py` | v0.2/deep JD flow deferred; compatibility endpoint present |
| `PUT /api/resumes/{id}/text` | Text body | status JSON | `src/offerpilot/api.py` | compatibility endpoint present |
| `GET /api/resumes/{id}/file` | Path id | File download | `src/offerpilot/api.py` | compatibility endpoint present |
| `GET /api/knowledge-documents` | Optional query filter | `KnowledgeDocument[]` | `src/offerpilot/api.py` | v0.1 **[KR-deprecated]** |
| `POST /api/knowledge-documents` | Document body, no knowledge base id | `KnowledgeDocument` | `src/offerpilot/api.py` | v0.1 **[KR-deprecated]** |
| `POST /api/knowledge-documents/import` | Import body | `KnowledgeDocument` | `internal/api/knowledge.go` | Phase 5 **[KR-deprecated]** |
| `GET /api/knowledge-documents/{id}` | Path id | `KnowledgeDocument` | `internal/api/knowledge.go` | Phase 5 **[KR-deprecated]** |
| `PUT /api/knowledge-documents/{id}` | Document body | `KnowledgeDocument` | `internal/api/knowledge.go` | Phase 5 **[KR-deprecated]** |
| `DELETE /api/knowledge-documents/{id}` | Path id | status JSON | `internal/api/knowledge.go` | Phase 5 **[KR-deprecated]** |
| `GET /api/knowledge/search` | Query text, optional base/limit | `KnowledgeSearchResult[]` | `internal/api/knowledge.go` | Phase 5 **[KR-deprecated]** |
| `GET /api/questions` | Optional filters | `Question[]` | `internal/api/questions.go` | Phase 5 |
| `POST /api/questions` | Manual question body | `Question` | `internal/api/questions.go` | Phase 5 |
| `POST /api/questions/generate` | Source/kb/app/count | Generated result | `internal/api/questions.go` | Phase 5 after AI minimum |
| `GET /api/questions/due` | None | `Question[]` | `internal/api/questions.go` | Phase 5 |
| `GET /api/questions/stats` | None | Practice stats | `internal/api/questions.go` | Phase 5 |
| `GET /api/questions/{id}` | Path id | `Question` | `internal/api/questions.go` | Phase 5 |
| `PUT /api/questions/{id}` | Question body | `Question` | `internal/api/questions.go` | Phase 5 |
| `DELETE /api/questions/{id}` | Path id | status JSON | `internal/api/questions.go` | Phase 5 |
| `POST /api/questions/{id}/reviews` | Rating/note | Review result | `internal/api/questions.go` | Phase 5 |
| `GET /api/applications/{id}/material-kit` | Path app id | Material kit | `internal/api/material_kits.go` | Phase 5 |
| `POST /api/applications/{id}/material-kit/generate` | Resume/JD inputs | Material kit | `internal/api/material_kits.go` | Phase 5 after AI minimum |
| `PUT /api/material-kits/{id}` | Status/content body | Material kit | `internal/api/material_kits.go` | Phase 5 |
| `GET /api/mock/sessions` | Optional filters | `MockSession[]` | `internal/api/mock.go` | Phase 5 |
| `POST /api/mock/sessions` | Mock config | Create response | `internal/api/mock.go` | Phase 5 after AI minimum |
| `GET /api/mock/sessions/{id}` | Path id | Detail response | `internal/api/mock.go` | Phase 5 |
| `POST /api/mock/sessions/{id}/end` | End body | Feedback response | `internal/api/mock.go` | Phase 5 after AI minimum |
| `DELETE /api/mock/sessions/{id}` | Path id | status JSON | `internal/api/mock.go` | Phase 5 |

Compatibility footnotes:

- JD analyze returns a parsed result object, while list/get analysis endpoints expose `result` as a raw JSON string.
- Resume upload accepts one real PDF up to 10 MB. Invalid `.pdf` bytes return `400`; valid PDFs without extractable text may still create a `parse-failed` resume.
- Material-kit `GET` returns `404` for missing kit; the frontend maps that to `null`.
- Knowledge document import accepts `.md` or `.txt` up to 1 MB and uses `source_type=upload`.
- Question delete returns `204 No Content`, unlike most delete endpoints.
- Question generation clamps count in the AI layer to default 8 and max 20.
- Search/default limits differ by endpoint: knowledge search default is 5 and max is 10; AI tool document listing default is 10 and max is 20.
- Mock interview frontend calls `/api/mock/*`; create returns session plus conversation details.
- Mock interview end can save a note for an already completed session when `auto_save_note` is true.

### Chat And Settings

| Endpoint | Request | Response | Compatibility notes | Tests to preserve |
|---|---|---|---|---|
| `POST /api/chat` | `{"message": string, "conversation_id": number, "context_type"?: string, "context_ref"?: string, "mode"?: string}` | Either `{"type":"message","conversation_id":id,"message":text,"degraded"?:true}` or `{"type":"confirmation_required","conversation_id":id,"pending_action":{"tool_name":name,"human":text}}` | `conversation_id=0` creates a new conversation. Application-scoped threads use `context_type=application`, `context_ref=<application_id>`. Missing context defaults to workspace. | New conversation, existing conversation, tools-unsupported fallback, pending write |
| `POST /api/chat/confirm` | `{"conversation_id": number, "approved": boolean}` | Same `ChatResponse` union | Last stored message must be assistant with `tool_calls`; approve executes, reject records refusal tool result. | Approve, reject, no pending action |
| `GET /api/chat/conversations` | None | `Conversation[]` | Includes `mode`, `context_type`, and `context_ref`; `offer_id` is not part of the v0.1 conversation contract. | List order/update time |
| `GET /api/chat/conversations/{id}` | Path id | `ChatMessage[]` | `tool_calls` remains a JSON string in DB/API model. | Preserve tool metadata |
| `DELETE /api/chat/conversations/{id}` | Path id | `{"status":"deleted"}` | Deletes messages first, then conversation. | Cascade/delete behavior |
| `GET /api/settings` | None | `{"chat_auto_approve_writes": bool, "active_provider_id": string, "fallback_provider_id": string, "providers": ProviderProfile[], "base_url": string, "model": string, "has_api_key": bool, "runtime_mode": string, "auth_enabled": bool, "has_auth_token": bool, "log_level": string}` | Never returns raw API key; provider profiles expose `has_api_key` only. | No secret exposure, multi-provider summary |
| `PUT /api/settings` | `chat_auto_approve_writes`, optional `active_provider_id`, optional `fallback_provider_id`, optional `providers`, optional `base_url`, optional `model`, optional `api_key`, runtime/auth/log fields | Same as GET | Blank/missing API key must not erase existing key; omitted provider list preserves existing profiles. Invalid fallback clears to empty. | Update fields, preserve/replace secret, preserve provider list |
| `POST /api/settings/providers/test` | Either `{"provider_id": string}` or `{"provider": ProviderProfile + optional api_key}` | `{"ok": true, "provider_id": string, "model": string, "latency_ms": number, "message": "连接成功"}` or `{"ok": false, "error": string}` | Runs a minimal non-tool model call; errors are readable and sanitized. | Saved provider test, draft provider test, no key leakage |
| `GET /api/settings/backup` | None | Safe JSON backup with runtime/auth/log flags, active/fallback ids, provider profiles with `has_api_key` only | v0.1 export only; no restore/import. | Backup excludes plaintext `api_key` |

Chat/settings compatibility footnotes:

- `GET /api/chat/conversations/{id}` does not currently 404 just because the message list is empty.
- `PUT /api/settings` preserves the existing API key when `api_key` is blank or omitted.
- AI calls try `active_provider_id` first; if it fails and `fallback_provider_id` is valid, enabled, and keyed, the fallback provider is attempted and the provider event is written to diagnostics logs.
- New chat without explicit context creates a workspace conversation. Offer-specific UI may set `mode=nego_coach`, but persistent conversation context is still `context_type/context_ref`.

## SQLite Schema Contract

`internal/db/db.go` is the canonical current migration source. It uses
`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, and a small
`ensureColumn` mechanism for older databases.

There is currently no `schema_migrations` table. Python may introduce Alembic
for new migrations, but startup must remain compatible with databases produced
by this idempotent Go migrator.

| Table | Columns and constraints | Indexes / compatibility notes |
|---|---|---|
| `applications` | `id` PK autoincrement; `company_name` not null; `position_name` not null; `job_url` default empty; `status` not null default `applied`; `source` not null default `cli`; `notes` default empty; `applied_at`, `created_at`, `updated_at` not null default current timestamp | `idx_applications_status`; first Python milestone must fully support this table |
| `application_events` | `id`; `application_id` not null FK cascade; `event_type` in `written_test/interview/offer_step/deadline/custom`; `subtype`; `tags` JSON text default `[]`; `round` default 0; nullable `scheduled_at`; `duration_minutes`; `location`; `notes`; nullable `remind_at`; `status`; `created_at` | `idx_application_events_app`, `idx_application_events_type`; old `events` is dropped, not kept compatible |
| `interview_notes` | `id`; nullable `application_id` FK set null; `company`, `position` not null; `round`, `date`, `questions`, `self_reflection`, `difficulty_points`, `mood`; `created_at` | `idx_notes_app` |
| `resumes` | `id`; `name`; `file_path`; `parsed_data`; `parse_status` default `pending`; `title`; `is_master`; nullable `parent_resume_id`; `source`; `source_file_path`; `content_json`; nullable `deleted_at`; `created_at` | v0.1 structured resume model; startup repairs add/backfill all v0.1 columns and keep exactly one active master when possible |
| `jd_analyses` | `id`; nullable `application_id` FK set null; `jd_source` default `text`; `jd_text` not null; `result` not null; `created_at` | `idx_jd_app` |
| `resume_matches` | `id`; `resume_id` not null FK cascade; nullable `application_id` FK set null; `jd_text` not null; `result` not null; `created_at` | `idx_matches_resume` |
| `offers` | `id`; nullable `application_id` FK set null; `company_name`, `position_name` not null; `status` default `pending`; `base_monthly`, `months_per_year`, `signing_bonus`; `equity`, `perks`, `deadline`, `notes`, `assessment`; timestamps | `idx_offers_app`, `idx_offers_status`; `total_cash` is derived, not stored |
| `conversations` | `id`; `title` default `新对话`; `mode`; `context_type` default `workspace`; `context_ref` default empty; pending action fields; `created_at`, `updated_at` | Existing local tables with `offer_id` are reset during v0.1 convergence |
| `chat_messages` | `id`; `conversation_id` FK cascade; `role`; `content`; `tool_calls`; `tool_call_id`; `provider_blocks`; `created_at` | `idx_chat_messages_conv`; `provider_blocks` migration column must be retained |
| `knowledge_documents` | `id`; `title`; `content`; `tags` JSON text default `[]`; `doc_kind`; `status`; `source_type`; `source_name`; `source_refs`; `summary_type`; `generation_meta`; `superseded_by`; `confirmed_at`; timestamps | Single library + tags; old `knowledge_bases` multi-library model is dropped **[KR-deprecated]** |
| `knowledge_chunks` | `id`; `document_id` FK cascade; `chunk_index`; `content`; `embedding`; `embedding_model`; `created_at` | `idx_knowledge_chunks_document` **[KR-deprecated]** |
| `knowledge_chunks_fts` | FTS5 virtual table with `chunk_id`, `document_id`, `content` | v0.1 keeps keyword search foundation; embedding fields are present for later hybrid retrieval **[KR-deprecated]** |
| `questions` | `id`; nullable `application_id`; `topic`; `category`; `difficulty` default `medium`; `question` not null; `reference_answer`; `tags`; `source_type`; `status`; practice fields; `question_hash`; timestamps | `idx_questions_topic`, `idx_questions_status`, `idx_questions_next_review`, `idx_questions_hash`; old `knowledge_base_id` tables are reset |
| `question_reviews` | `id`; `question_id` FK cascade; `rating`; `note`; `created_at` | `idx_question_reviews_question` |
| `application_material_kits` | `id`; `application_id` not null unique FK cascade; nullable `resume_id`, `jd_analysis_id`; `jd_snapshot`; `status` default `draft`; `content_json`; timestamps | `idx_material_kits_app`, `idx_material_kits_status` |
| `mock_sessions` | `id`; `conversation_id` FK cascade; nullable app/kb FKs; title/config/progress/status fields; scoring fields; feedback; timestamps | `idx_mock_sessions_conv`, `idx_mock_sessions_status` |

Python schema rules:

- Enable foreign keys for every SQLite connection.
- Keep a single writer connection policy equivalent to Go's `SetMaxOpenConns(1)`.
- Preserve existing table and column names exactly.
- First Python migration must be additive and idempotent against existing Go-created databases.
- Do not rewrite or vacuum user databases during startup.
- Add explicit tests that create a legacy-style database and run Python migration on it.
- Resume upload compatibility must keep relative DB paths like `resumes/<id>_<basename>.pdf`; note that current file write plus DB update is best-effort rather than transactional.

## CLI Contract

Root command is `oc`, currently implemented with Cobra in `internal/cli`.
Python should use Typer, but command names, flags, required inputs, outputs, and
database/config side effects should remain compatible.

| Command | Flags / args | Current behavior | Python priority |
|---|---|---|---|
| `oc --port, -p` | global port, default 8080 | Used by `start` | Phase 2 |
| `oc start` | global port | Initializes `data.db`, starts local HTTP server and frontend/static fallback | Phase 2 |
| `oc add` | `--company/-c` required, `--position` required, `--url/-u`, `--notes/-n` | Creates application with `status=applied`, `source=cli`, prints ID/status | Phase 3 |
| `oc list` | `--status/-s` exact filter | Prints compact table or empty message; orders by `applied_at DESC` | Phase 3 |
| `oc config` | `--api-key`, `--base-url`, `--model`, `--auto-approve[=bool]` | Loads/saves `config.json`, masks API key as first 4 plus last 2 chars, prints config | Phase 3 minimum, full in Phase 4 |
| `oc analyze` | `--jd/-j`, `--jd-url/-u`, `--app/-a` | Requires exactly one JD source; `--jd -` reads stdin; AI JD analysis saves result | Phase 5 |
| `oc resume add` | `--file/-f` required, `--name/-n` | Saves resume text/file; no extension validation despite help text | Phase 5 |
| `oc resume list` | none | Prints resumes; unnamed rows display `(unnamed)` | Phase 5 |
| `oc resume match` | `--resume/-r`, `--jd/-j`, `--jd-url/-u`, `--app/-a` | Requires resume and JD source; if both JD text and URL are non-empty, current code lets text win | Phase 5 |
| `oc note add` | `--app/-a`, `--company`, `--position`, `--round/-r`, `--date`, `--questions/-q`, `--reflection/-f`, `--difficulty`, `--mood` | Requires positive app id today; `--questions -` reads stdin; company/position can backfill from app | Phase 5 |
| `oc note list` | `--app/-a` | Prints notes; `--app 0` means all notes | Phase 5 |
| `oc offer add` | company/position/app/base/months/signing/equity/perks/deadline/notes flags | Creates offer; company/position may backfill from `--app`; validates non-negative base/signing and months >= 1 | Phase 5 |
| `oc offer list` | `--status/-s` exact filter | Prints offers; no enum validation for status | Phase 5 |
| `oc offer update [id]` | status/base/months/signing | Updates selected fields; no-op update still rewrites and prints success | Phase 5 |
| `oc offer delete [id]` | id arg | Deletes offer; missing row currently still prints deleted because affected rows are not checked | Phase 5 |
| `oc offer compare [id1,id2,...]` | comma list arg | Invalid numeric token errors; missing IDs are skipped; all-missing exits success with no-match message | Phase 5 |
| `oc question generate` | `--source/-s`, `--kb`, `--app/-a`, `--count/-n` | Source `knowledge` requires `--kb`; AI layer clamps count but CLI prints requested count first | Phase 5 **[KR-deprecated]** |
| `oc question list` | `--status`, `--kb` | Prints questions; status has no CLI enum validation | Phase 5 |

CLI compatibility details:

- Root and parent commands should preserve useful help/usage behavior. The Go
  version does not set Cobra `SilenceUsage` or `SilenceErrors`.
- Commands return non-zero by printing `Error: ...` from `cmd/oc/main.go`.
- Python should add CLI golden tests around stdout, stderr, exit codes, and
  temp `OFFERPILOT_DATA`; the Go tree currently has no `internal/cli/*_test.go`.

## AI Tool Contract

The registry lives in `internal/ai/tools.go` and `internal/ai/offer_tools.go`.
The agent loop lives in `internal/ai/agent.go`.

### AI Loop Rules

| Rule | Current behavior | Python requirement |
|---|---|---|
| Provider abstraction | `ai.Client` supports OpenAI-compatible and Anthropic-style tool calls | Keep provider interface behind one app-level AI client |
| Provider selection | Config supports an active provider profile plus an optional fallback provider profile. LiteLLM model routing prefixes provider names unless the model already includes a provider prefix. | Preserve active-first/fallback-second behavior and keep provider adapters isolated behind one interface |
| Tool loop | Max 8 iterations; model sees full history plus tools; only first tool call is executed per assistant turn | Preserve max iteration and one-tool-per-turn semantics |
| Unknown tool | Converts to a tool result string `错误：未知工具 "name"` and continues | Preserve non-crashing behavior |
| Write gate | If `tool.Write` and auto-approve is false, return pending action before executing handler | Mandatory |
| Pending persistence | Pending write is persisted as an assistant message with `tool_calls`; no tool result is stored until confirm/reject | Mandatory |
| Confirm approve | Executes the pending tool, stores tool result, continues model loop | Mandatory |
| Confirm reject | Stores refusal text as tool result, continues model loop | Mandatory |
| Auto approve | `chat_auto_approve_writes=true` lets write tools execute immediately | Preserve config behavior, default false |
| Tools unsupported | If provider rejects tools, chat endpoint may run summary fallback and return `degraded:true` | Preserve or explicitly mark as deferred before AI phase |
| Persistence risk | Tool execution and chat persistence are not one DB transaction today | Add tests for current behavior before considering transactional improvements |

### Registered Tools

| Tool | Kind | Params | Data dependency | Migration priority |
|---|---|---|---|---|
| `list_applications` | read | optional `status` | `applications` | Phase 4 |
| `get_application` | read | `id` | `applications` | Phase 4 |
| `list_jd_analyses` | read | optional `application_id` | `jd_analyses` | Phase 5 |
| `get_jd_analysis` | read | `id` | `jd_analyses` | Phase 5 |
| `list_resumes` | read | none | `resumes` | Phase 5 |
| `get_resume` | read | `id` | `resumes` | Phase 5 |
| `list_notes` | read | optional `application_id` | `interview_notes` | Phase 5 |
| `list_application_events` | read | optional `month`, `application_id`, `event_type` | `application_events` | v0.1 |
| `get_application_event` | read | `id` | `application_events` | v0.1 |
| `list_knowledge_documents` | read | optional `query` | `knowledge_documents` | v0.1 **[KR-deprecated]** |
| `get_knowledge_document` | read | `id` | `knowledge_documents` | Phase 5 **[KR-deprecated]** |
| `search_knowledge` | read | `query`, optional `limit` | `knowledge_chunks_fts` | v0.1 **[KR-deprecated]** |
| `list_offers` | read | optional `status` | `offers` | Phase 5 |
| `get_offer` | read | `id` | `offers` | Phase 5 |
| `compare_offers` | read | `ids[]` | `offers` | Phase 5 |
| `create_application` | write | `company_name`, `position_name`, optional `job_url`, `status` | `applications` | Phase 4 |
| `update_application_status` | write | `id`, `status` | `applications` | Phase 4 |
| `add_note` | write | app/company/position/round/date/questions/reflection/difficulty/mood | `interview_notes`, optional `applications` | Phase 5 |
| `update_note` | write | `id` plus note fields | `interview_notes` | Phase 5 |
| `delete_note` | write | `id` | `interview_notes` | Phase 5 |
| `create_knowledge_document` | write | title, content, tags | `knowledge_documents`, chunks | v0.1 **[KR-deprecated]** |
| `update_knowledge_document` | write | `id` plus optional fields | `knowledge_documents`, chunks | Phase 5 **[KR-deprecated]** |
| `delete_knowledge_document` | write | `id` | `knowledge_documents`, chunks | Phase 5 **[KR-deprecated]** |
| `create_application_event` | write | app id, event_type, subtype, tags, round, scheduled_at, duration_minutes, remind_at | `application_events` | v0.1 |
| `update_application_event` | write | id plus application event fields | `application_events` | v0.1 |
| `delete_application_event` | write | `id` | `application_events` | v0.1 |
| `update_offer` | write | id plus offer fields | `offers` | Phase 5 |
| `save_offer_assessment` | write | `id`, `assessment` string | `offers.assessment` | Phase 5 |

## Phased Migration Scope

| Phase | Scope | Exit criteria |
|---|---|---|
| Phase 2: Python skeleton | `pyproject.toml`, app package, config, SQLite session, FastAPI app, health route, Typer `start`, test/lint/type commands | Python service starts; health endpoint passes; config path and data dir tests pass |
| Phase 3: Applications vertical | `applications` model/repository/service/API, dashboard API, `oc add/list/config`, frontend smoke against existing React | Existing React dashboard and application CRUD work against Python backend |
| Phase 4: AI minimum | AI client abstraction, chat persistence, `list_applications`, `get_application`, `create_application`, `update_application_status`, pending confirm/reject | Write tool cannot mutate data before confirm unless auto-approve is true |
| Phase 5: Module migration | Events, calendar, resumes, JD, notes, offers, knowledge, questions, material kits, mock interview, Docker/docs | Each module has compatibility tests and smoke coverage before moving on |

## Deferred Or Explicitly Non-Goals For First Python Cut

- Do not change React API calls unless a compatibility test proves Go behavior is broken and the contract is revised.
- Do not replace SQLite with another database.
- Do not introduce LangChain/LangGraph before the Phase 4 minimum AI loop is green.
- Do not upgrade frontend dependencies as part of backend migration.
- Legacy Go implementation may be removed after the user-approved final Python cutover.

## Current Risk Register

| Risk | Why it matters | Mitigation |
|---|---|---|
| Existing user SQLite data can be damaged by non-idempotent migrations | Local database is user-owned product data | Add migration tests against Go-created and legacy-style schemas before any Python startup writes |
| FTS5 support may vary by Python SQLite build | Knowledge 的 P0 检索计划依赖 SQLite FTS | 在后续 Knowledge 实施 Spec 中定义启动检查和降级策略；当前契约不沿用旧 Page FTS 方案 |
| Chat pending action is stateful across DB messages | A small persistence mismatch can execute or lose writes | Port `agent.go` semantics with tests before adding broad tools |
| Tool execution and message persistence can diverge | Current Go flow executes a confirmed write before persisting follow-up chat messages | Preserve behavior first; consider transactional hardening only after compatibility tests |
| Frontend expects some full-object updates | `PUT /api/applications/{id}` is not a patch today | Preserve current behavior for Applications; document any future partial update separately |
| `npm audit` reports vulnerabilities | Frontend dependency risk exists but is not migration behavior | Track separately; do not mix dependency upgrade into backend rewrite |
| AI providers differ in tool-call payload shape | OpenAI-compatible and Anthropic paths are both present | Keep provider adapters isolated behind one interface and test both payload parsers |

## Knowledge System

Knowledge 的长期产品职责、领域模型和数据流以
[Knowledge 系统主文档](./architecture/knowledge-system.md) 为唯一事实源。

具体迁移、API、Schema 和发布门禁查阅相关实施 Spec、当前代码与测试；本文不据历史基线判断
当前实现进度，也不再保留已废弃的 Page、Index、Wikilink 和 Review 契约。
