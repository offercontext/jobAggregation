import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateIndex, CreateTable

from offerpilot.models import Base

SessionFactory = sessionmaker[Session]


# KI-02 起新表 knowledge_sources/origins/snapshots/evidence/evidence_fts/jobs 由本模块创建并维护，
# 不再视为 legacy；只保留旧自动 Wiki 占位实现的表名作为破坏性重置对象。
KNOWLEDGE_LEGACY_TABLES = (
    "knowledge_bases",
    "knowledge_documents",
    "knowledge_chunks",
    "knowledge_chunks_fts",
    "knowledge_wiki_pages",
    "knowledge_wiki_pages_fts",
    "knowledge_page_versions",
    "knowledge_index_entries",
    "knowledge_page_evidence",
    "knowledge_wikilinks",
    "knowledge_reviews",
    "knowledge_review_revisions",
    "knowledge_review_jobs",
    "knowledge_config_versions",
)


def _remove_recovery_path(path: Path) -> None:
    """清理恢复目录中的文件、目录或符号链接，不跟随链接。"""

    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    except OSError:
        pass


def init_database(db_path: Path) -> SessionFactory:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        pool_size=5,
        max_overflow=0,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    _reset_incompatible_v01_tables(engine)
    _ensure_schema_migrations(engine)
    mock_interview_migration_needed = _prepare_event_bound_mock_interview_migration(engine)
    _reset_knowledge_legacy_tables(engine, db_path.parent)
    Base.metadata.create_all(engine)
    _ensure_context_projector_manifest_v2_schema(engine)
    _ensure_write_operation_ledger_schema(engine)
    _ensure_column(
        engine,
        "conversations",
        "pending_confirmation_claim_id",
        "TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        engine,
        "conversations",
        "pending_confirmation_claimed_at",
        "DATETIME",
    )
    _record_migration(
        engine,
        "0025_pending_confirmation_claim",
        "Add private Pending Action confirmation claim identity and lease",
    )
    _record_migration(
        engine,
        "0024_durable_execution_journal",
        "Add fail-open durable Agent Run journal tables",
    )
    _record_migration(
        engine,
        "0027_context_projector_manifest_v2",
        "Allow privacy-bounded Context Projector manifests up to 64 KiB",
    )
    if mock_interview_migration_needed:
        _record_migration(
            engine,
            "0016_event_bound_mock_interview",
            "Replace legacy MockSession with event-bound text mock interview tables",
        )
    _ensure_application_jd_versions_schema(engine)
    _ensure_interview_story_schema(engine)
    _ensure_application_outcome_schema(engine)
    _ensure_adaptive_interview_practice_schema(engine)
    _ensure_voice_coaching_schema(engine)
    _ensure_interview_studio_schema(engine)
    _ensure_offer_negotiation_schema(engine)
    interview_review_history_rebuilt = _ensure_interview_review_history_schema(engine)
    interview_knowledge_event_added = _ensure_column(
        engine,
        "knowledge_captured_source_metadata",
        "application_event_id",
        "INTEGER",
    )
    if interview_review_history_rebuilt or interview_knowledge_event_added:
        _record_migration(
            engine,
            "0015_interview_review_history_retention",
            "Retain interview review and confirmed knowledge history after note changes",
        )
    opportunity_fit_v2_migrations = [
        _ensure_column(
            engine,
            "opportunity_fit_review_stages",
            "provider_call_token",
            "TEXT NOT NULL DEFAULT ''",
        ),
        _ensure_column(
            engine,
            "opportunity_fit_review_stages",
            "lease_expires_at",
            "DATETIME",
        ),
    ]
    if any(opportunity_fit_v2_migrations):
        _record_migration(
            engine,
            "0013_opportunity_fit_v2",
            "Add neutral two-stage opportunity fit review sessions and stages",
        )
    _ensure_column(
        engine,
        "opportunity_fit_reviews",
        "proposal_schema_version",
        "INTEGER NOT NULL DEFAULT 1",
    )
    _record_migration(
        engine,
        "0014_opportunity_fit_v1_schema_marker",
        "Mark legacy opportunity fit reviews as schema version 1",
    )
    # InterviewNote existed before event-bound review notes.  create_all creates
    # the column on a fresh database; existing databases need the nullable column
    # added before the migration-only indexes are created.
    _ensure_column(
        engine,
        "interview_notes",
        "application_event_id",
        "INTEGER REFERENCES application_events(id) ON DELETE SET NULL",
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE knowledge_captured_source_metadata "
                "SET application_event_id = ("
                "SELECT CASE WHEN COUNT(DISTINCT application_event_id) = 1 "
                "THEN MIN(application_event_id) END FROM interview_review_proposals "
                "WHERE interview_review_proposals.note_id = knowledge_captured_source_metadata.origin_note_id "
                "AND interview_review_proposals.application_event_id IS NOT NULL) "
                "WHERE application_event_id IS NULL"
            )
        )
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_notes_event "
                "ON interview_notes(application_event_id)"
            )
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_interview_notes_event_main "
                "ON interview_notes(application_event_id) "
                "WHERE application_event_id IS NOT NULL"
            )
        )
    _record_migration(
        engine,
        "0010_interview_review_proposals",
        "Add event-bound interview review proposals",
    )
    _record_migration(
        engine,
        "0011_confirmed_interview_knowledge_capture",
        "Add confirmed interview knowledge capture",
    )
    _record_migration(
        engine,
        "0012_interview_preparation_proposals",
        "Add evidence-gated interview preparation proposals",
    )
    _record_migration(
        engine,
        "0013_opportunity_fit_v2",
        "Add neutral two-stage opportunity fit review sessions and stages",
    )
    # ``attempt_id`` was added after the initial KI-10 schema.  Add it before
    # creating the integrity triggers below so existing databases can use the
    # same association checks as fresh databases.
    if _ensure_column(engine, "knowledge_jobs", "attempt_id", "INTEGER"):
        _record_migration(
            engine,
            "0008_knowledge_job_attempt_id",
            "Add Knowledge Brief Attempt association to jobs",
        )
    _ensure_knowledge_fts(engine)
    _ensure_knowledge_integrity_constraints(engine)
    _recover_knowledge_deletions(engine, db_path.parent)
    # KI-07：补齐 KnowledgeJob 持久队列所需列；旧库升级保证 attempt_token 存在，
    # 否则 lease claim 无法防迟到提交。
    knowledge_job_migrations = [
        _ensure_column(engine, "knowledge_jobs", "attempt_token", "TEXT DEFAULT ''"),
    ]
    if any(knowledge_job_migrations):
        _record_migration(
            engine,
            "0006_knowledge_job_attempt_token",
            "Add knowledge_jobs.attempt_token for KI-07 lease correctness",
        )
    # KI-10 / Spec §11.1 / §11.4：Brief Attempt 固定 fallback 候选、记录实际成功
    # Provider，并持久化 Provider 层重试计数与 next retry，保证重启后不从零开始。
    knowledge_brief_attempt_migrations = [
        _ensure_column(
            engine,
            "knowledge_brief_attempts",
            "fallback_provider_id",
            "TEXT DEFAULT ''",
        ),
        _ensure_column(
            engine,
            "knowledge_brief_attempts",
            "fallback_provider_model",
            "TEXT DEFAULT ''",
        ),
        _ensure_column(
            engine,
            "knowledge_brief_attempts",
            "actual_provider_id",
            "TEXT DEFAULT ''",
        ),
        _ensure_column(
            engine,
            "knowledge_brief_attempts",
            "actual_provider_model",
            "TEXT DEFAULT ''",
        ),
        _ensure_column(
            engine,
            "knowledge_brief_attempts",
            "provider_retry_count",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        _ensure_column(
            engine,
            "knowledge_brief_attempts",
            "next_retry_at",
            "DATETIME",
        ),
    ]
    if any(knowledge_brief_attempt_migrations):
        _record_migration(
            engine,
            "0007_knowledge_brief_attempt_ki10",
            "Add fallback/actual provider and retry fields for KI-10",
        )
    # KBR-02：frontmatter 白名单 provenance 沿 Source 所有权（author/published_at），
    # metadata extraction version 沿 Snapshot 所有权。加列兼容旧库。
    knowledge_provenance_migrations = [
        _ensure_column(engine, "knowledge_sources", "author", "TEXT DEFAULT ''"),
        _ensure_column(engine, "knowledge_sources", "published_at", "DATETIME"),
        _ensure_column(
            engine,
            "knowledge_extraction_snapshots",
            "metadata_extraction_version",
            "TEXT DEFAULT ''",
        ),
    ]
    if any(knowledge_provenance_migrations):
        _record_migration(
            engine,
            "0009_knowledge_provenance_kbr02",
            "Add Source author/published_at and Snapshot metadata_extraction_version for KBR-02",
        )
    _recover_knowledge_runtime(engine, db_path.parent)
    _record_migration(engine, "0001_base_schema", "Create current application tables")

    chat_migrations = [
        _ensure_column(engine, "conversations", "mode", "TEXT DEFAULT 'general'"),
        _ensure_column(engine, "conversations", "title_source", "TEXT DEFAULT 'manual'"),
        _ensure_column(engine, "conversations", "context_type", "TEXT DEFAULT 'workspace'"),
        _ensure_column(engine, "conversations", "context_ref", "TEXT DEFAULT ''"),
        _ensure_column(engine, "conversations", "pinned_at", "DATETIME"),
        _ensure_column(engine, "conversations", "archived_at", "DATETIME"),
        _ensure_column(engine, "conversations", "pending_tool_call_id", "TEXT DEFAULT ''"),
        _ensure_column(engine, "conversations", "pending_tool_name", "TEXT DEFAULT ''"),
        _ensure_column(engine, "conversations", "pending_args", "TEXT DEFAULT ''"),
        _ensure_column(engine, "conversations", "pending_human", "TEXT DEFAULT ''"),
        _ensure_column(engine, "conversations", "clarification_tool_call_id", "TEXT DEFAULT ''"),
        _ensure_column(engine, "conversations", "clarification_tool_name", "TEXT DEFAULT ''"),
        _ensure_column(engine, "conversations", "clarification_args", "TEXT DEFAULT ''"),
        _ensure_column(engine, "conversations", "clarification_human", "TEXT DEFAULT ''"),
        _ensure_column(engine, "conversations", "clarification_question", "TEXT DEFAULT ''"),
        _ensure_column(engine, "conversations", "last_write_undo_json", "TEXT DEFAULT ''"),
        _ensure_column(engine, "chat_messages", "provider_blocks", "TEXT DEFAULT ''"),
    ]
    if any(chat_migrations):
        _record_migration(engine, "0002_chat_state_columns", "Add durable chat state columns")
    # 0028 depends on the historical chat scope columns.  Very old databases
    # acquire those columns above before the scoped-authority migration
    # canonicalizes legacy mode values or installs scope triggers.
    _ensure_scoped_tool_authority_schema(engine)
    _ensure_review_to_readiness_feedback_schema(engine)

    resume_migrations = [
        _ensure_column(engine, "resumes", "name", "TEXT DEFAULT ''"),
        _ensure_column(engine, "resumes", "file_path", "TEXT DEFAULT ''"),
        _ensure_column(engine, "resumes", "parsed_data", "TEXT DEFAULT ''"),
        _ensure_column(engine, "resumes", "parse_status", "TEXT DEFAULT 'pending'"),
        _ensure_column(engine, "resumes", "title", "TEXT DEFAULT ''"),
        _ensure_column(engine, "resumes", "is_master", "INTEGER DEFAULT 0"),
        _ensure_column(engine, "resumes", "parent_resume_id", "INTEGER"),
        _ensure_column(engine, "resumes", "source", "TEXT DEFAULT 'manual'"),
        _ensure_column(engine, "resumes", "source_file_path", "TEXT DEFAULT ''"),
        _ensure_column(engine, "resumes", "content_json", "TEXT DEFAULT '{}'"),
        _ensure_column(engine, "resumes", "deleted_at", "DATETIME"),
    ]
    resume_backfilled = _backfill_resume_v01(engine)
    if any(resume_migrations):
        _record_migration(engine, "0003_resume_content_columns", "Add resume content columns")
        _record_migration(engine, "0004_resume_v01_columns", "Add resume v0.1 columns")
    elif resume_backfilled:
        _record_migration(engine, "0004_resume_v01_columns", "Add resume v0.1 columns")

    application_migrations = [
        _ensure_column(engine, "applications", "first_pending_at", "DATETIME"),
        _ensure_column(engine, "applications", "first_applied_at", "DATETIME"),
        _ensure_column(engine, "applications", "first_written_test_at", "DATETIME"),
        _ensure_column(engine, "applications", "first_interview_at", "DATETIME"),
        _ensure_column(engine, "applications", "first_offer_at", "DATETIME"),
        _ensure_column(engine, "applications", "closed_reason", "TEXT DEFAULT ''"),
        _ensure_column(engine, "applications", "closed_at", "DATETIME"),
        _ensure_column(engine, "applications", "deleted_at", "DATETIME"),
    ]
    application_backfilled = _backfill_application_lifecycle(engine)
    if any(application_migrations) or application_backfilled:
        _record_migration(
            engine,
            "0005_application_lifecycle_columns",
            "Add application lifecycle and soft-delete columns",
        )
    _record_migration(
        engine,
        "0006_application_evidence_bundles",
        "Add immutable application evidence bundles",
    )
    _record_migration(
        engine,
        "0007_material_revision_proposals",
        "Add evidence-gated material revision proposals",
    )
    _record_migration(
        engine,
        "0008_opportunity_fit_reviews",
        "Add immutable opportunity fit reviews",
    )
    return sessionmaker(bind=engine, expire_on_commit=False)


def _ensure_knowledge_fts(engine) -> None:  # type: ignore[no-untyped-def]
    """创建 Evidence FTS5 虚拟表并验证 trigram tokenizer 可用。

    FTS5 不可用属于 Spec §13 中 `fts_unavailable` 错误码，必须启动期失败而非静默吞掉。
    """
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_evidence_fts USING fts5(
                        evidence_id UNINDEXED,
                        source_id UNINDEXED,
                        source_title,
                        heading_path,
                        content,
                        tokenize = 'trigram'
                    )
                    """
                )
            )
            probe = conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_evidence_fts'"
                )
            ).fetchone()
            if probe is None:
                raise RuntimeError("fts_unavailable: knowledge_evidence_fts virtual table missing")
    except OperationalError as exc:
        message = str(exc).lower()
        if "fts5" in message or "no such module" in message:
            raise RuntimeError(
                "fts_unavailable: SQLite FTS5 / trigram tokenizer not available"
            ) from exc
        raise


def _ensure_knowledge_integrity_constraints(engine) -> None:  # type: ignore[no-untyped-def]
    """补齐模型暂未声明的活动引用与队列一致性约束。

    KnowledgeSource 的 active_* 字段和若干历史引用需要与 Source/Snapshot 保持
    同源；这些约束用 SQLite trigger 实现，不重建现有表，兼容已经存在的数据库。
    活动 Job/Attempt 使用部分唯一索引，防止并发 rebuild 产生两个正式候选。
    """
    with engine.begin() as conn:
        # 早期开发版本曾创建过过严的 active_snapshot trigger；启动时重建为下面的
        # 正确同源约束，既允许合法代际切换，也拒绝指向其他 Source/不存在 Snapshot。
        conn.execute(text("DROP TRIGGER IF EXISTS trg_knowledge_source_snapshot_ref"))
        # 这些触发器在旧数据库中可能已经存在；定义发生变化时必须先删除，
        # 否则 ``CREATE IF NOT EXISTS`` 会静默保留旧版的宽松约束。
        for trigger_name in (
            "trg_knowledge_evidence_neighbor_ref",
            "trg_knowledge_evidence_neighbor_ref_insert",
            "trg_knowledge_job_snapshot_ref",
            "trg_knowledge_job_snapshot_ref_update",
        ):
            conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name}"))
        # Brief Job 的唯一性包含 Snapshot。SQLite UNIQUE 对 NULL 不互相约束，不能把
        # Extraction/Delete 与 Brief 共用一个 (source_id, kind, snapshot_id) 索引，
        # 否则同一 Source 会出现多个 snapshot_id=NULL 的 Extract Job。
        conn.execute(text("DROP INDEX IF EXISTS uq_knowledge_active_job_source_kind"))
        conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_active_job_source_kind
                ON knowledge_jobs (source_id, kind)
                WHERE source_id IS NOT NULL
                  AND kind IN ('extract', 'delete')
                  AND status IN ('pending', 'running')
                  AND canceled = 0
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_active_brief_source_snapshot
                ON knowledge_jobs (source_id, snapshot_id)
                WHERE source_id IS NOT NULL
                  AND kind = 'brief'
                  AND snapshot_id IS NOT NULL
                  AND status IN ('pending', 'running')
                  AND canceled = 0
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_active_attempt_source
                ON knowledge_brief_attempts (source_id)
                WHERE status IN ('pending', 'processing')
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_knowledge_jobs_attempt "
                "ON knowledge_jobs (attempt_id)"
            )
        )

        trigger_sql = (
            """
            CREATE TRIGGER IF NOT EXISTS trg_knowledge_source_snapshot_ref
            BEFORE UPDATE OF active_snapshot_id ON knowledge_sources
            WHEN NEW.active_snapshot_id IS NOT NULL
             AND NOT EXISTS (
                SELECT 1 FROM knowledge_extraction_snapshots
                WHERE id = NEW.active_snapshot_id AND source_id = NEW.id
             )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_source_active_snapshot_mismatch');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_knowledge_source_brief_ref
            BEFORE UPDATE OF active_brief_id ON knowledge_sources
            WHEN NEW.active_brief_id IS NOT NULL
             AND NOT EXISTS (
                SELECT 1 FROM knowledge_source_briefs
                WHERE id = NEW.active_brief_id AND source_id = NEW.id
             )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_source_active_brief_mismatch');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_knowledge_evidence_snapshot_ref
            BEFORE INSERT ON knowledge_evidence
            WHEN NOT EXISTS (
                SELECT 1 FROM knowledge_extraction_snapshots
                WHERE id = NEW.snapshot_id AND source_id = NEW.source_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_evidence_snapshot_mismatch');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_knowledge_evidence_asset_ref
            BEFORE INSERT ON knowledge_evidence
            WHEN NEW.asset_id IS NOT NULL
             AND NOT EXISTS (
                SELECT 1 FROM knowledge_source_assets
                WHERE id = NEW.asset_id AND source_id = NEW.source_id
             )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_evidence_asset_mismatch');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_knowledge_evidence_asset_ref_update
            BEFORE UPDATE OF asset_id, source_id ON knowledge_evidence
            WHEN NEW.asset_id IS NOT NULL
             AND NOT EXISTS (
                SELECT 1 FROM knowledge_source_assets
                WHERE id = NEW.asset_id AND source_id = NEW.source_id
             )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_evidence_asset_mismatch');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_knowledge_evidence_neighbor_ref
            BEFORE UPDATE OF previous_evidence_id, next_evidence_id ON knowledge_evidence
            WHEN (
                NEW.previous_evidence_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM knowledge_evidence
                    WHERE id = NEW.previous_evidence_id
                      AND source_id = NEW.source_id
                      AND snapshot_id = NEW.snapshot_id
                )
            ) OR (
                NEW.next_evidence_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM knowledge_evidence
                    WHERE id = NEW.next_evidence_id
                      AND source_id = NEW.source_id
                      AND snapshot_id = NEW.snapshot_id
                )
            )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_evidence_neighbor_mismatch');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_knowledge_evidence_neighbor_ref_insert
            BEFORE INSERT ON knowledge_evidence
            WHEN (
                NEW.previous_evidence_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM knowledge_evidence
                    WHERE id = NEW.previous_evidence_id
                      AND source_id = NEW.source_id
                      AND snapshot_id = NEW.snapshot_id
                )
            ) OR (
                NEW.next_evidence_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM knowledge_evidence
                    WHERE id = NEW.next_evidence_id
                      AND source_id = NEW.source_id
                      AND snapshot_id = NEW.snapshot_id
                )
            )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_evidence_neighbor_mismatch');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_knowledge_job_snapshot_ref
            BEFORE INSERT ON knowledge_jobs
            WHEN NEW.snapshot_id IS NOT NULL
             AND (
                NEW.source_id IS NULL
                OR NOT EXISTS (
                SELECT 1 FROM knowledge_extraction_snapshots
                WHERE id = NEW.snapshot_id AND source_id = NEW.source_id
                )
             )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_job_snapshot_mismatch');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_knowledge_job_snapshot_ref_update
            BEFORE UPDATE OF snapshot_id, source_id ON knowledge_jobs
            WHEN NEW.snapshot_id IS NOT NULL
             AND (
                NEW.source_id IS NULL
                OR NOT EXISTS (
                SELECT 1 FROM knowledge_extraction_snapshots
                WHERE id = NEW.snapshot_id AND source_id = NEW.source_id
                )
             )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_job_snapshot_mismatch');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_knowledge_job_attempt_ref
            BEFORE INSERT ON knowledge_jobs
            WHEN NEW.attempt_id IS NOT NULL
             AND NOT EXISTS (
                SELECT 1 FROM knowledge_brief_attempts
                WHERE id = NEW.attempt_id
                  AND source_id = NEW.source_id
                  AND snapshot_id = NEW.snapshot_id
             )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_job_attempt_mismatch');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_knowledge_job_attempt_ref_update
            BEFORE UPDATE OF attempt_id, source_id, snapshot_id ON knowledge_jobs
            WHEN NEW.attempt_id IS NOT NULL
             AND NOT EXISTS (
                SELECT 1 FROM knowledge_brief_attempts
                WHERE id = NEW.attempt_id
                  AND source_id = NEW.source_id
                  AND snapshot_id = NEW.snapshot_id
             )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_job_attempt_mismatch');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_knowledge_brief_snapshot_ref
            BEFORE INSERT ON knowledge_source_briefs
            WHEN NOT EXISTS (
                SELECT 1 FROM knowledge_extraction_snapshots
                WHERE id = NEW.snapshot_id AND source_id = NEW.source_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_brief_snapshot_mismatch');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_knowledge_brief_snapshot_ref_update
            BEFORE UPDATE OF snapshot_id, source_id ON knowledge_source_briefs
            WHEN NOT EXISTS (
                SELECT 1 FROM knowledge_extraction_snapshots
                WHERE id = NEW.snapshot_id AND source_id = NEW.source_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_brief_snapshot_mismatch');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_knowledge_brief_attempt_snapshot_ref
            BEFORE INSERT ON knowledge_brief_attempts
            WHEN NOT EXISTS (
                SELECT 1 FROM knowledge_extraction_snapshots
                WHERE id = NEW.snapshot_id AND source_id = NEW.source_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_brief_attempt_snapshot_mismatch');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_knowledge_brief_attempt_snapshot_ref_update
            BEFORE UPDATE OF snapshot_id, source_id ON knowledge_brief_attempts
            WHEN NOT EXISTS (
                SELECT 1 FROM knowledge_extraction_snapshots
                WHERE id = NEW.snapshot_id AND source_id = NEW.source_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_brief_attempt_snapshot_mismatch');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_knowledge_brief_attempt_ref
            BEFORE INSERT ON knowledge_source_briefs
            WHEN NOT EXISTS (
                SELECT 1 FROM knowledge_brief_attempts
                WHERE id = NEW.winning_attempt_id
                  AND source_id = NEW.source_id
                  AND snapshot_id = NEW.snapshot_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_brief_attempt_mismatch');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_knowledge_brief_attempt_ref_update
            BEFORE UPDATE OF winning_attempt_id, source_id, snapshot_id
                ON knowledge_source_briefs
            WHEN NOT EXISTS (
                SELECT 1 FROM knowledge_brief_attempts
                WHERE id = NEW.winning_attempt_id
                  AND source_id = NEW.source_id
                  AND snapshot_id = NEW.snapshot_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_brief_attempt_mismatch');
            END
            """,
        )
        for sql in trigger_sql:
            conn.execute(text(sql))


def _reset_incompatible_v01_tables(engine) -> None:  # type: ignore[no-untyped-def]
    with engine.begin() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        application_event_columns = (
            {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(application_events)")).fetchall()
            }
            if "application_events" in tables
            else set()
        )
        question_columns = (
            {row[1] for row in conn.execute(text("PRAGMA table_info(questions)")).fetchall()}
            if "questions" in tables
            else set()
        )
        conversation_columns = (
            {row[1] for row in conn.execute(text("PRAGMA table_info(conversations)")).fetchall()}
            if "conversations" in tables
            else set()
        )
        mock_columns = (
            {row[1] for row in conn.execute(text("PRAGMA table_info(mock_sessions)")).fetchall()}
            if "mock_sessions" in tables
            else set()
        )
        reset_application_events = "application_events" in tables and (
            "subtype" not in application_event_columns
            or "tags" not in application_event_columns
            or "duration_minutes" not in application_event_columns
            or "remind_at" not in application_event_columns
        )
        reset_questions = "questions" in tables and (
            "knowledge_base_id" in question_columns or "topic" not in question_columns
        )
        reset_conversations = "conversations" in tables and "offer_id" in conversation_columns
        reset_mock_sessions = "mock_sessions" in tables and "knowledge_base_id" in mock_columns
        drop_tables: list[str] = []
        if "events" in tables:
            drop_tables.append("events")
        if reset_application_events:
            drop_tables.append("application_events")
        if reset_questions:
            drop_tables.extend(["question_reviews", "questions"])
        if reset_conversations:
            drop_tables.extend(["chat_messages", "mock_sessions", "conversations"])
        elif reset_mock_sessions:
            drop_tables.append("mock_sessions")
        if not drop_tables:
            return
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        for table in drop_tables:
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        conn.execute(text("PRAGMA foreign_keys=ON"))


def _reset_knowledge_legacy_tables(engine, data_dir: Path) -> None:  # type: ignore[no-untyped-def]
    with engine.begin() as conn:
        existing = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
            ).fetchall()
        }
        legacy_present = any(table in existing for table in KNOWLEDGE_LEGACY_TABLES)
        already_migrated = (
            conn.execute(
                text(
                    "SELECT version FROM schema_migrations WHERE version = 'knowledge_rewrite_reset'"
                )
            ).fetchone()
            is not None
        )

    knowledge_runtime_dir = data_dir / "knowledge"
    # KI-02 之后 knowledge/ 目录可能含有合法 Source 原件；只在尚未迁移（首次启动）且目录非空时
    # 才视为 legacy，避免清空用户已上传的 Source。
    runtime_legacy_present = (
        (not already_migrated)
        and knowledge_runtime_dir.exists()
        and any(knowledge_runtime_dir.iterdir())
    )

    if not legacy_present and not runtime_legacy_present:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO schema_migrations (version, description) "
                    "VALUES ('knowledge_rewrite_reset', 'Knowledge rewrite base schema applied')"
                )
            )
        return

    if already_migrated and not legacy_present:
        return

    if legacy_present:
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=OFF"))
            for table in KNOWLEDGE_LEGACY_TABLES:
                conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
            conn.execute(text("PRAGMA foreign_keys=ON"))

    if runtime_legacy_present:
        shutil.rmtree(knowledge_runtime_dir, ignore_errors=True)

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT OR IGNORE INTO schema_migrations (version, description) "
                "VALUES ('knowledge_rewrite_reset', 'Knowledge rewrite legacy tables dropped')"
            )
        )


def session_factory_for_data_dir(data_dir: Path) -> SessionFactory:
    return init_database(data_dir / "data.db")


def journal_session_factory_for_data_dir(data_dir: Path) -> SessionFactory:
    """Open the existing database through an independent, low-wait Journal pool."""

    engine = create_engine(
        f"sqlite:///{data_dir / 'data.db'}",
        connect_args={"check_same_thread": False, "timeout": 0.05},
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.0,
    )

    @event.listens_for(engine, "connect")
    def _enable_journal_foreign_keys(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    return sessionmaker(bind=engine)


def _recover_knowledge_runtime(engine, data_dir: Path) -> None:  # type: ignore[no-untyped-def]
    """KI-07：Spec §6 / §12 启动恢复。

    职责：
    1. 清理 ``knowledge/staging/`` 残留目录（任何进程崩溃都可能留下半写入的 staging）。
    2. 清理 ``knowledge/sources/<source_id>/`` 中无 ``knowledge_sources`` 记录的孤儿
       目录（rename 后、commit 前崩溃）。
    3. 保留过期 running Job 的持久重试信息，并将其放回 ``pending``；应用创建
       ``KnowledgeWorkerRuntime`` 后会继续按 lease 规则消费。不能在运行时启动前标记
       ``failed``，否则真正的 Worker 恢复会看不到该 Job。

    必须在 ``_recover_knowledge_deletions`` 之后执行——delete Job 的恢复由后者负责
    （连 Source 行 + 所有 Job 一并清理）。本函数只处理 extract/brief Job。
    KBR-07 一次性 reset 不再参与启动恢复；旧 quarantine/manifest 协议已删除。
    """

    knowledge_dir = data_dir / "knowledge"
    staging_root = knowledge_dir / "staging"
    if staging_root.exists() and staging_root.is_dir() and not staging_root.is_symlink():
        for child in staging_root.iterdir():
            _remove_recovery_path(child)

    sources_root = knowledge_dir / "sources"
    if sources_root.exists() and sources_root.is_dir() and not sources_root.is_symlink():
        with engine.begin() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                ).fetchall()
            }
            if "knowledge_sources" not in tables:
                return
            existing_ids = {
                int(row[0])
                for row in conn.execute(text("SELECT id FROM knowledge_sources")).fetchall()
            }
        for child in sources_root.iterdir():
            try:
                child_id = int(child.name)
            except ValueError:
                continue
            if child_id not in existing_ids:
                _remove_recovery_path(child)

    with engine.begin() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        if "knowledge_jobs" not in tables:
            return
        # 用 Python 端 now.isoformat() 与写入侧 datetime.now(timezone.utc) 保持时区与
        # 格式一致；CURRENT_TIMESTAMP 在 SQLite 返回无 tz 的 "YYYY-MM-DD HH:MM:SS"，
        # 与带 +00:00 的 ISO 字符串按字节比较时结果不稳定。
        now_iso = datetime.now(timezone.utc).isoformat()
        stale_jobs = conn.execute(
            text(
                """
                SELECT id, kind, source_id, attempt_id, snapshot_id
                FROM knowledge_jobs
                WHERE status = 'running'
                  AND kind != 'delete'
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at < :now
                """
            ),
            {"now": now_iso},
        ).fetchall()
        if not stale_jobs:
            return
        for job_id, kind, source_id, attempt_id, snapshot_id in stale_jobs:
            conn.execute(
                text(
                    """
                    UPDATE knowledge_jobs
                    SET status = 'pending',
                        stage = 'recovered_pending',
                        error_code = '',
                        error_message = '',
                        lease_expires_at = NULL,
                        lease_owner = '',
                        heartbeat_at = NULL,
                        updated_at = :now
                    WHERE id = :jid
                    """
                ),
                {"jid": job_id, "now": now_iso},
            )
            if kind == "brief":
                # 只终结该 Job 绑定的 Attempt，不能按 Source 批量更新，否则同一
                # Source 的新 Snapshot 候选会被旧 lease 恢复误标失败。
                if attempt_id is not None:
                    conn.execute(
                        text(
                            """
                            UPDATE knowledge_brief_attempts
                            SET status = 'failed',
                                error_code = 'job_lease_expired',
                                error_message = 'Brief Job lease expired during restart recovery',
                                updated_at = :now
                            WHERE id = :attempt_id
                              AND status = 'processing'
                            """
                        ),
                        {"attempt_id": attempt_id, "now": now_iso},
                    )
                elif source_id is not None and snapshot_id is not None:
                    # 旧库没有 attempt_id 时，至少用 Snapshot 约束回退匹配，避免
                    # 误伤同 Source 的其他代际 Attempt。
                    conn.execute(
                        text(
                            """
                            UPDATE knowledge_brief_attempts
                            SET status = 'failed',
                                error_code = 'job_lease_expired',
                                error_message = 'Brief Job lease expired during restart recovery',
                                updated_at = :now
                            WHERE source_id = :sid
                              AND snapshot_id = :snapshot_id
                              AND status = 'processing'
                            """
                        ),
                        {
                            "sid": source_id,
                            "snapshot_id": snapshot_id,
                            "now": now_iso,
                        },
                    )


def _delete_retrieval_traces_for_source(conn, source_id: int) -> None:  # type: ignore[no-untyped-def]
    """按 Trace 的结构化 filters/hits 清理指定 Source 的评估记录。"""
    rows = conn.execute(
        text("SELECT id, filters_json, hits_json FROM knowledge_retrieval_traces")
    ).fetchall()
    trace_ids: list[int] = []
    for trace_id, filters_json, hits_json in rows:
        try:
            filters = json.loads(filters_json or "{}")
        except (TypeError, json.JSONDecodeError):
            filters = {}
        try:
            hits = json.loads(hits_json or "[]")
        except (TypeError, json.JSONDecodeError):
            hits = []
        source_ids = filters.get("source_ids") if isinstance(filters, dict) else None
        if isinstance(source_ids, list) and any(
            str(value) == str(source_id) for value in source_ids
        ):
            trace_ids.append(int(trace_id))
            continue
        if isinstance(hits, list) and any(
            isinstance(hit, dict) and str(hit.get("source_id")) == str(source_id) for hit in hits
        ):
            trace_ids.append(int(trace_id))
    for trace_id in trace_ids:
        conn.execute(
            text("DELETE FROM knowledge_retrieval_traces WHERE id = :trace_id"),
            {"trace_id": trace_id},
        )


def _recover_knowledge_deletions(engine, data_dir: Path) -> None:  # type: ignore[no-untyped-def]
    """KI-06：启动恢复完成 Spec §5.4 异常中断的删除流程。

    场景:
    1. ``complete_purge`` 事务已提交 → Source 行不存在,但 quarantine 目录残留(物理
       删除失败或进程崩溃)。本函数物理删除 quarantine 子目录。
    2. ``begin_delete`` 已标记 lifecycle=deleting,但 ``complete_purge`` 未执行(进程
       崩溃)。本函数:
       a. 尝试完成事务清理:删除 FTS / Evidence / Snapshot / Asset / Origin / Job /
          Source 行。
       b. 物理删除 quarantine 目录。
       c. 写入 ``knowledge_logs``(source_deleted, succeeded)。

    Spec §6 / §12：启动恢复负责完成异常中断的删除。任何 quarantine 子目录对应的
    Source 行若不存在 → 物理删除 quarantine。
    """
    knowledge_dir = data_dir / "knowledge"
    quarantine_root = knowledge_dir / "quarantine"
    sources_root = knowledge_dir / "sources"

    # 根目录本身若是符号链接，任何 move/rmtree 都可能越出 data_dir；保留 deleting
    # 状态等待人工修复路径，不触碰外部目标。
    if sources_root.is_symlink() or quarantine_root.is_symlink():
        return
    if (sources_root.exists() and not sources_root.is_dir()) or (
        quarantine_root.exists() and not quarantine_root.is_dir()
    ):
        return

    # 关键点：不能因为 quarantine 根目录不存在就跳过 deleting Source。进程可能
    # 在 begin_delete 提交后、创建根目录前崩溃；此时仍应恢复数据库状态。
    with engine.begin() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        if "knowledge_sources" not in tables:
            return
        deleting_sources = [
            int(row[0])
            for row in conn.execute(
                text("SELECT id FROM knowledge_sources WHERE lifecycle = 'deleting'")
            ).fetchall()
        ]

    for source_id in deleting_sources:
        source_dir = sources_root / str(source_id)
        quarantine_dir = quarantine_root / str(source_id)

        # 删除链接本身而不是跟随链接，避免恢复流程接触 data_dir 外的文件。
        for link in (source_dir, quarantine_dir):
            if link.is_symlink():
                try:
                    link.unlink()
                except OSError:
                    pass

        # 若尚未完成 rename，先尝试把正式目录移入 quarantine。移动失败时保留
        # deleting 行与原件，等待下一次启动重试，绝不先删数据库行。
        if source_dir.exists() and not quarantine_dir.exists():
            try:
                quarantine_root.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source_dir), str(quarantine_dir))
            except OSError:
                continue

        try:
            with engine.begin() as conn:
                # active_* 是非 FK 引用，先清空再删除目标行，保持引用完整性。
                conn.execute(
                    text(
                        "UPDATE knowledge_sources SET active_snapshot_id = NULL, "
                        "active_brief_id = NULL WHERE id = :sid"
                    ),
                    {"sid": source_id},
                )
                conn.execute(
                    text("DELETE FROM knowledge_evidence_fts WHERE source_id = :sid"),
                    {"sid": source_id},
                )
                conn.execute(
                    text("DELETE FROM knowledge_evidence WHERE source_id = :sid"),
                    {"sid": source_id},
                )
                conn.execute(
                    text("DELETE FROM knowledge_extraction_snapshots WHERE source_id = :sid"),
                    {"sid": source_id},
                )
                conn.execute(
                    text("DELETE FROM knowledge_source_assets WHERE source_id = :sid"),
                    {"sid": source_id},
                )
                conn.execute(
                    text("DELETE FROM knowledge_source_origins WHERE source_id = :sid"),
                    {"sid": source_id},
                )
                # KI-09：Spec §5.4 删除时清理 Brief / Attempt；存在性检查避免旧库未建表。
                if "knowledge_source_briefs" in tables:
                    conn.execute(
                        text("DELETE FROM knowledge_source_briefs WHERE source_id = :sid"),
                        {"sid": source_id},
                    )
                if "knowledge_brief_attempts" in tables:
                    conn.execute(
                        text("DELETE FROM knowledge_brief_attempts WHERE source_id = :sid"),
                        {"sid": source_id},
                    )
                if "knowledge_retrieval_traces" in tables:
                    _delete_retrieval_traces_for_source(conn, source_id)
                conn.execute(
                    text("DELETE FROM knowledge_jobs WHERE source_id = :sid"),
                    {"sid": source_id},
                )
                conn.execute(
                    text("DELETE FROM knowledge_sources WHERE id = :sid"),
                    {"sid": source_id},
                )
                if "knowledge_logs" in tables:
                    conn.execute(
                        text(
                            "INSERT INTO knowledge_logs (source_id, action, result) "
                            "VALUES (:sid, 'source_deleted', 'succeeded')"
                        ),
                        {"sid": source_id},
                    )
        except OperationalError:
            # 事务失败 → 留给下次启动重试，物理目录保持不动。
            continue

        # 数据库提交后再删除物理目录；失败会留下 quarantine/orphan，由下一次
        # 启动继续清理，且不会重新暴露已删除 Source。
        for path in (quarantine_dir, source_dir):
            _remove_recovery_path(path)

    # 处理孤儿 quarantine 目录（Source 行已被事务删除）并保留根目录本身。
    if quarantine_root.exists():
        for child in quarantine_root.iterdir():
            if child.is_dir():
                _remove_recovery_path(child)
            elif child.is_symlink() or child.is_file():
                _remove_recovery_path(child)


def _ensure_schema_migrations(engine) -> None:  # type: ignore[no-untyped-def]
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )


def _record_migration(engine, version: str, description: str) -> None:  # type: ignore[no-untyped-def]
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT OR IGNORE INTO schema_migrations (version, description)
                VALUES (:version, :description)
                """
            ),
            {"version": version, "description": description},
        )


def _ensure_context_projector_manifest_v2_schema(engine) -> None:  # type: ignore[no-untyped-def]
    """Rebuild only the snapshot table when an existing database still has V1 checks."""

    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'agent_context_snapshots'"
            )
        ).first()
        if row is None or row[0] is None:
            return
        create_sql = str(row[0])
        if "manifest_schema_version IN (1, 2)" in create_sql:
            return
        if "manifest_schema_version = 1" not in create_sql:
            raise RuntimeError("unsupported agent_context_snapshots schema")
        rebuilt_sql = create_sql.replace(
            "manifest_schema_version = 1",
            "manifest_schema_version IN (1, 2)",
        ).replace(
            "length(CAST(manifest_json AS BLOB)) <= 16384",
            "((manifest_schema_version = 1 AND length(CAST(manifest_json AS BLOB)) <= 16384) "
            "OR (manifest_schema_version = 2 AND length(CAST(manifest_json AS BLOB)) <= 65536))",
        )
        columns = [
            str(item[1])
            for item in conn.execute(text("PRAGMA table_info(agent_context_snapshots)"))
        ]
        quoted = ", ".join(f'"{column}"' for column in columns)
        conn.exec_driver_sql(
            "ALTER TABLE agent_context_snapshots RENAME TO agent_context_snapshots_0027"
        )
        conn.exec_driver_sql(rebuilt_sql)
        conn.exec_driver_sql(
            f"INSERT INTO agent_context_snapshots ({quoted}) "
            f"SELECT {quoted} FROM agent_context_snapshots_0027"
        )
        conn.exec_driver_sql("DROP TABLE agent_context_snapshots_0027")
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_agent_context_segment_step "
            "ON agent_context_snapshots (run_id, execution_segment_id, model_step)"
        )


def _ensure_write_operation_ledger_schema(engine) -> None:  # type: ignore[no-untyped-def]
    """Install the additive 0026 control columns and cross-row integrity triggers."""

    _ensure_column(engine, "conversations", "pending_operation_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(engine, "conversations", "last_write_operation_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(engine, "chat_messages", "operation_id", "TEXT")
    _ensure_column(engine, "chat_messages", "delivery_kind", "TEXT")
    _ensure_column(engine, "chat_messages", "delivery_ordinal", "INTEGER")
    _ensure_column(engine, "chat_messages", "tool_calls", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(engine, "chat_messages", "tool_call_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(engine, "chat_messages", "provider_blocks", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(engine, "chat_messages", "created_at", "DATETIME")
    _ensure_column(engine, "write_operations", "parent_terminal_payload_sha256", "TEXT")
    _rebuild_chat_messages_for_write_operation_integrity(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE write_operations AS child
                SET parent_terminal_payload_sha256 = (
                    SELECT parent.terminal_payload_sha256
                    FROM write_operations AS parent
                    WHERE parent.id = child.parent_operation_id
                )
                WHERE child.operation_role = 'compensation'
                  AND child.parent_terminal_payload_sha256 IS NULL
                """
            )
        )
        invalid_delivery_rows = conn.scalar(
            text(
                """
                SELECT count(*) FROM chat_messages AS message
                LEFT JOIN write_operations AS operation
                  ON operation.id = message.operation_id
                WHERE (message.operation_id IS NULL AND
                       (message.delivery_kind IS NOT NULL OR message.delivery_ordinal IS NOT NULL))
                   OR (message.operation_id IS NOT NULL AND (
                       operation.id IS NULL
                       OR operation.conversation_id <> message.conversation_id
                       OR message.delivery_kind IS NULL
                       OR message.delivery_ordinal IS NULL
                       OR NOT (
                         (message.delivery_kind = 'origin_tool_result'
                          AND message.delivery_ordinal = 0
                          AND message.role = 'tool' AND message.tool_call_id <> ''
                          AND operation.tool_call_id = message.tool_call_id)
                         OR
                         (message.delivery_kind = 'continuation_message'
                          AND message.delivery_ordinal >= 1
                          AND ((message.role = 'tool' AND message.tool_call_id <> '')
                               OR (message.role = 'assistant' AND message.tool_call_id = '')))
                       )
                   ))
                """
            )
        )
        if invalid_delivery_rows:
            raise RuntimeError("invalid legacy operation delivery rows")
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_messages_operation_ordinal "
                "ON chat_messages(operation_id, delivery_ordinal) WHERE operation_id IS NOT NULL"
            )
        )
        conn.execute(text("DROP TRIGGER IF EXISTS trg_write_operation_compensation_insert"))
        conn.execute(
            text(
                """
                CREATE TRIGGER trg_write_operation_compensation_insert
                BEFORE INSERT ON write_operations
                WHEN NEW.operation_role = 'compensation'
                BEGIN
                    SELECT CASE WHEN NOT EXISTS (
                        SELECT 1 FROM write_operations parent
                        WHERE parent.id = NEW.parent_operation_id
                          AND parent.operation_role = 'primary'
                          AND parent.status = 'committed'
                          AND parent.terminal_payload_sha256 = NEW.parent_terminal_payload_sha256
                          AND length(NEW.parent_terminal_payload_sha256) = 71
                          AND substr(NEW.parent_terminal_payload_sha256,1,7) = 'sha256:'
                          AND substr(NEW.parent_terminal_payload_sha256,8) NOT GLOB '*[^0-9a-f]*'
                    ) THEN RAISE(ABORT, 'invalid compensation parent') END;
                END
                """
            )
        )
        conn.execute(text("DROP TRIGGER IF EXISTS trg_write_operation_terminal_immutable"))
        conn.execute(
            text(
                """
                CREATE TRIGGER trg_write_operation_terminal_immutable
                BEFORE UPDATE ON write_operations
                WHEN OLD.status <> 'proposed' AND (
                    NEW.status <> OLD.status OR
                    NEW.operation_role IS NOT OLD.operation_role OR
                    NEW.parent_operation_id IS NOT OLD.parent_operation_id OR
                    NEW.parent_terminal_payload_sha256 IS NOT OLD.parent_terminal_payload_sha256 OR
                    NEW.agent_run_id IS NOT OLD.agent_run_id OR
                    NEW.tool_call_id IS NOT OLD.tool_call_id OR
                    NEW.tool_name IS NOT OLD.tool_name OR
                    NEW.adapter_kind IS NOT OLD.adapter_kind OR
                    NEW.fingerprint_key_id IS NOT OLD.fingerprint_key_id OR
                    NEW.proposal_fingerprint IS NOT OLD.proposal_fingerprint OR
                    NEW.input_fingerprint IS NOT OLD.input_fingerprint OR
                    NEW.confirmation_token_fingerprint IS NOT OLD.confirmation_token_fingerprint OR
                    NEW.operation_request_fingerprint IS NOT OLD.operation_request_fingerprint OR
                    NEW.result_contract IS NOT OLD.result_contract OR
                    NEW.result_json IS NOT OLD.result_json OR
                    NEW.visible_result IS NOT OLD.visible_result OR
                    NEW.transport_json IS NOT OLD.transport_json OR
                    NEW.undo_json IS NOT OLD.undo_json OR
                    NEW.terminal_payload_sha256 IS NOT OLD.terminal_payload_sha256 OR
                    NEW.failure_category IS NOT OLD.failure_category OR
                    NEW.failure_code IS NOT OLD.failure_code OR
                    NEW.approved_at IS NOT OLD.approved_at OR
                    NEW.claimed_at IS NOT OLD.claimed_at OR
                    NEW.rejected_at IS NOT OLD.rejected_at OR
                    NEW.committed_at IS NOT OLD.committed_at OR
                    NEW.failed_at IS NOT OLD.failed_at
                )
                BEGIN
                    SELECT RAISE(ABORT, 'write operation terminal is immutable');
                END
                """
            )
        )
        conn.execute(text("DROP TRIGGER IF EXISTS trg_write_operation_compensation_update"))
        conn.execute(
            text(
                """
                CREATE TRIGGER trg_write_operation_compensation_update
                BEFORE UPDATE OF parent_operation_id, operation_role ON write_operations
                WHEN NEW.operation_role = 'compensation'
                BEGIN
                    SELECT CASE WHEN NOT EXISTS (
                        SELECT 1 FROM write_operations parent
                        WHERE parent.id = NEW.parent_operation_id
                          AND parent.operation_role = 'primary'
                          AND parent.status = 'committed'
                          AND parent.terminal_payload_sha256 = NEW.parent_terminal_payload_sha256
                          AND length(NEW.parent_terminal_payload_sha256) = 71
                          AND substr(NEW.parent_terminal_payload_sha256,1,7) = 'sha256:'
                          AND substr(NEW.parent_terminal_payload_sha256,8) NOT GLOB '*[^0-9a-f]*'
                    ) THEN RAISE(ABORT, 'invalid compensation parent') END;
                END
                """
            )
        )
        conn.execute(text("DROP TRIGGER IF EXISTS trg_write_operation_delivery_generation"))
        conn.execute(
            text(
                """
                CREATE TRIGGER trg_write_operation_delivery_generation
                BEFORE UPDATE ON write_operations
                WHEN NEW.delivery_generation < OLD.delivery_generation
                  OR NEW.delivery_generation > OLD.delivery_generation + 1
                  OR (
                    NEW.delivery_generation = OLD.delivery_generation + 1
                    AND OLD.delivery_generation > 0
                    AND (
                      OLD.delivery_status <> 'pending'
                      OR OLD.delivery_lease_expires_at > unixepoch('now')
                      OR NEW.delivery_status <> 'pending'
                    )
                  )
                  OR (
                    OLD.status <> 'proposed'
                    AND NEW.delivery_generation = OLD.delivery_generation
                    AND NEW.delivery_status = 'pending'
                    AND NEW.delivery_owner_token_fingerprint IS NOT OLD.delivery_owner_token_fingerprint
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid delivery generation');
                END
                """
            )
        )
        conn.execute(text("DROP TRIGGER IF EXISTS trg_write_operation_delivery_immutable"))
        conn.execute(
            text(
                """
                CREATE TRIGGER trg_write_operation_delivery_immutable
                BEFORE UPDATE ON write_operations
                WHEN OLD.delivery_status IN ('completed','failed','not_applicable') AND (
                    NEW.delivery_status IS NOT OLD.delivery_status OR
                    NEW.delivery_failure_code IS NOT OLD.delivery_failure_code OR
                    NEW.delivery_outcome IS NOT OLD.delivery_outcome OR
                    NEW.delivery_message_count IS NOT OLD.delivery_message_count OR
                    NEW.delivery_manifest_sha256 IS NOT OLD.delivery_manifest_sha256 OR
                    NEW.delivery_next_operation_id IS NOT OLD.delivery_next_operation_id OR
                    NEW.delivery_generation IS NOT OLD.delivery_generation OR
                    NEW.delivery_owner_token_fingerprint IS NOT OLD.delivery_owner_token_fingerprint OR
                    NEW.delivery_lease_expires_at IS NOT OLD.delivery_lease_expires_at OR
                    NEW.delivered_at IS NOT OLD.delivered_at
                )
                BEGIN
                    SELECT RAISE(ABORT, 'write operation delivery is immutable');
                END
                """
            )
        )
        conn.execute(text("DROP TRIGGER IF EXISTS trg_write_operation_chained_delivery"))
        conn.execute(
            text(
                """
                CREATE TRIGGER trg_write_operation_chained_delivery
                BEFORE UPDATE OF delivery_next_operation_id ON write_operations
                WHEN NEW.delivery_next_operation_id IS NOT NULL
                BEGIN
                    SELECT CASE WHEN NOT EXISTS (
                        SELECT 1 FROM write_operations child
                        WHERE child.id = NEW.delivery_next_operation_id
                          AND child.operation_role = 'primary'
                          AND child.status = 'proposed'
                          AND child.conversation_id = NEW.conversation_id
                    ) THEN RAISE(ABORT, 'invalid chained operation') END;
                END
                """
            )
        )
        conn.execute(text("DROP TRIGGER IF EXISTS trg_write_operation_transition_insert"))
        conn.execute(
            text(
                """
                CREATE TRIGGER trg_write_operation_transition_insert
                BEFORE INSERT ON write_operation_transitions
                BEGIN
                    SELECT CASE WHEN NOT (
                      (NEW.seq = 1 AND NEW.state = 'proposed'
                       AND NOT EXISTS (SELECT 1 FROM write_operation_transitions
                                       WHERE operation_id = NEW.operation_id))
                      OR
                      (NEW.seq = 2 AND NEW.state IN ('approved','rejected')
                       AND EXISTS (SELECT 1 FROM write_operation_transitions
                                   WHERE operation_id = NEW.operation_id
                                     AND seq = 1 AND state = 'proposed')
                       AND EXISTS (SELECT 1 FROM write_operations
                                   WHERE id = NEW.operation_id
                                     AND ((NEW.state = 'approved' AND status = 'proposed')
                                          OR status = 'rejected')))
                      OR
                      (NEW.seq = 3 AND NEW.state = 'claimed'
                       AND EXISTS (SELECT 1 FROM write_operation_transitions
                                   WHERE operation_id = NEW.operation_id
                                     AND seq = 2 AND state = 'approved'))
                      OR
                      (NEW.seq = 4 AND NEW.state IN ('committed','failed')
                       AND EXISTS (SELECT 1 FROM write_operation_transitions
                                   WHERE operation_id = NEW.operation_id
                                     AND seq = 3 AND state = 'claimed')
                       AND EXISTS (SELECT 1 FROM write_operations
                                   WHERE id = NEW.operation_id AND status = NEW.state))
                    ) THEN RAISE(ABORT, 'invalid write operation transition') END;
                END
                """
            )
        )
        conn.execute(text("DROP TRIGGER IF EXISTS trg_write_operation_transition_immutable"))
        conn.execute(
            text(
                """
                CREATE TRIGGER trg_write_operation_transition_immutable
                BEFORE UPDATE ON write_operation_transitions
                BEGIN
                    SELECT RAISE(ABORT, 'write operation transition is immutable');
                END
                """
            )
        )
        conn.execute(text("DROP TRIGGER IF EXISTS trg_write_operation_transition_delete"))
        conn.execute(
            text(
                """
                CREATE TRIGGER trg_write_operation_transition_delete
                BEFORE DELETE ON write_operation_transitions
                BEGIN
                    SELECT RAISE(ABORT, 'write operation transition is immutable');
                END
                """
            )
        )
        conn.execute(text("DROP TRIGGER IF EXISTS trg_write_operation_chat_restrict"))
        conn.execute(
            text(
                """
                CREATE TRIGGER trg_write_operation_chat_restrict
                BEFORE DELETE ON write_operations
                WHEN EXISTS (SELECT 1 FROM chat_messages
                             WHERE operation_id = OLD.id)
                BEGIN
                    SELECT RAISE(ABORT, 'operation delivery messages exist');
                END
                """
            )
        )
        conn.execute(text("DROP TRIGGER IF EXISTS trg_chat_message_operation_insert"))
        conn.execute(
            text(
                """
                CREATE TRIGGER trg_chat_message_operation_insert
                BEFORE INSERT ON chat_messages
                BEGIN
                    SELECT CASE WHEN
                      (NEW.operation_id IS NULL AND
                       (NEW.delivery_kind IS NOT NULL OR NEW.delivery_ordinal IS NOT NULL))
                      OR
                      (NEW.operation_id IS NOT NULL AND (
                        NEW.delivery_kind IS NULL OR NEW.delivery_ordinal IS NULL OR
                        NOT (
                          (NEW.delivery_kind = 'origin_tool_result'
                           AND NEW.delivery_ordinal = 0
                           AND NEW.role = 'tool' AND NEW.tool_call_id <> '')
                          OR
                          (NEW.delivery_kind = 'continuation_message'
                           AND NEW.delivery_ordinal >= 1
                           AND ((NEW.role = 'tool' AND NEW.tool_call_id <> '')
                                OR (NEW.role = 'assistant' AND NEW.tool_call_id = '')))
                        ) OR NOT EXISTS (
                        SELECT 1 FROM write_operations op
                        WHERE op.id = NEW.operation_id
                          AND op.conversation_id = NEW.conversation_id
                          AND (NEW.delivery_kind <> 'origin_tool_result'
                               OR op.tool_call_id = NEW.tool_call_id)
                        )
                      ))
                    THEN RAISE(ABORT, 'invalid operation delivery message') END;
                END
                """
            )
        )
        conn.execute(text("DROP TRIGGER IF EXISTS trg_chat_message_operation_update"))
        conn.execute(
            text(
                """
                CREATE TRIGGER trg_chat_message_operation_update
                BEFORE UPDATE OF operation_id, conversation_id, role, content, tool_calls,
                    tool_call_id, provider_blocks, delivery_kind, delivery_ordinal
                ON chat_messages
                WHEN OLD.operation_id IS NOT NULL
                BEGIN
                    SELECT RAISE(ABORT, 'operation delivery message is immutable');
                END
                """
            )
        )
        conn.execute(text("DROP TRIGGER IF EXISTS trg_chat_message_operation_bind"))
        conn.execute(
            text(
                """
                CREATE TRIGGER trg_chat_message_operation_bind
                BEFORE UPDATE OF operation_id, conversation_id, role, content, tool_calls,
                    tool_call_id, provider_blocks, delivery_kind, delivery_ordinal
                ON chat_messages
                WHEN OLD.operation_id IS NULL
                BEGIN
                    SELECT CASE WHEN
                      (NEW.operation_id IS NULL AND
                       (NEW.delivery_kind IS NOT NULL OR NEW.delivery_ordinal IS NOT NULL))
                      OR
                      (NEW.operation_id IS NOT NULL AND (
                        NEW.delivery_kind IS NULL OR NEW.delivery_ordinal IS NULL OR
                        NOT (
                          (NEW.delivery_kind = 'origin_tool_result'
                           AND NEW.delivery_ordinal = 0
                           AND NEW.role = 'tool' AND NEW.tool_call_id <> '')
                          OR
                          (NEW.delivery_kind = 'continuation_message'
                           AND NEW.delivery_ordinal >= 1
                           AND ((NEW.role = 'tool' AND NEW.tool_call_id <> '')
                                OR (NEW.role = 'assistant' AND NEW.tool_call_id = '')))
                        ) OR NOT EXISTS (
                          SELECT 1 FROM write_operations op
                          WHERE op.id = NEW.operation_id
                            AND op.conversation_id = NEW.conversation_id
                            AND (NEW.delivery_kind <> 'origin_tool_result'
                                 OR op.tool_call_id = NEW.tool_call_id)
                        )
                      ))
                    THEN RAISE(ABORT, 'invalid operation delivery message') END;
                END
                """
            )
        )
    _record_migration(
        engine,
        "0026_write_operation_ledger",
        "Add durable Agent write operation ledger and fenced delivery identity",
    )


def _conversation_mode_is_valid_sql(value_sql: str) -> str:
    """Return a SQLite-only, fail-closed Unicode mode predicate.

    SQLite's TEXT functions replace malformed UTF-8 while retaining the raw
    bytes.  Consequently ``length()``, ``unicode()`` and ``GLOB`` alone accept
    values such as ``CAST(X'EDA080' AS TEXT)``.  Scan the underlying bytes as
    canonical UTF-8 before applying the product's code-point and edge-space
    rules.  The scanner deliberately accepts all valid non-control Unicode,
    including U+FFFD; this is not an ASCII allowlist.
    """

    edge_whitespace = (
        "160, 5760, 8192, 8193, 8194, 8195, 8196, 8197, 8198, "
        "8199, 8200, 8201, 8202, 8232, 8233, 8239, 8287, 12288"
    )
    return f"""
        typeof({value_sql}) = 'text'
        AND EXISTS (
            WITH RECURSIVE
              mode_input(hex_bytes) AS (
                SELECT hex(CAST({value_sql} AS BLOB))
              ),
              mode_scan(byte_pos, code_points, is_valid) AS (
                SELECT 1, 0, 1
                UNION ALL
                SELECT
                  byte_pos + CASE
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN '20' AND '7E' THEN 2
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN 'C2' AND 'DF' THEN 4
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN 'E0' AND 'EF' THEN 6
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN 'F0' AND 'F4' THEN 8
                    ELSE 2
                  END,
                  code_points + 1,
                  CASE
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN '20' AND '7E'
                      THEN 1
                    WHEN substr(hex_bytes, byte_pos, 2) = 'C2'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN 'A0' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN 'C3' AND 'DF'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN '80' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) = 'E0'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN 'A0' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 4, 2) BETWEEN '80' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN 'E1' AND 'EC'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN '80' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 4, 2) BETWEEN '80' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) = 'ED'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN '80' AND '9F'
                       AND substr(hex_bytes, byte_pos + 4, 2) BETWEEN '80' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN 'EE' AND 'EF'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN '80' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 4, 2) BETWEEN '80' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) = 'F0'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN '90' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 4, 2) BETWEEN '80' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 6, 2) BETWEEN '80' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) BETWEEN 'F1' AND 'F3'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN '80' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 4, 2) BETWEEN '80' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 6, 2) BETWEEN '80' AND 'BF'
                    WHEN substr(hex_bytes, byte_pos, 2) = 'F4'
                      THEN substr(hex_bytes, byte_pos + 2, 2) BETWEEN '80' AND '8F'
                       AND substr(hex_bytes, byte_pos + 4, 2) BETWEEN '80' AND 'BF'
                       AND substr(hex_bytes, byte_pos + 6, 2) BETWEEN '80' AND 'BF'
                    ELSE 0
                  END
                FROM mode_scan, mode_input
                WHERE is_valid
                  AND byte_pos <= length(hex_bytes)
                  AND code_points < 64
                  AND length(hex_bytes) <= 512
              )
            SELECT 1
            FROM mode_scan, mode_input
            WHERE is_valid
              AND byte_pos = length(hex_bytes) + 1
              AND code_points BETWEEN 1 AND 64
              AND length(hex_bytes) <= 512
        )
        AND {value_sql} = trim({value_sql})
        AND unicode(substr({value_sql}, 1, 1)) NOT IN ({edge_whitespace})
        AND unicode(substr({value_sql}, -1, 1)) NOT IN ({edge_whitespace})
    """


def _ensure_scoped_tool_authority_schema(engine) -> None:  # type: ignore[no-untyped-def]
    """Install the additive Conversation scope and authorization guards (0028)."""

    _ensure_column(
        engine,
        "write_operations",
        "authorization_scope_fingerprint",
        "TEXT",
    )
    with engine.begin() as conn:
        # Only the two historical empty representations are canonicalized.  An
        # unknown or malformed legacy mode remains observable for the Source /
        # Authority fail-closed boundary.
        conn.execute(
            text(
                "UPDATE conversations SET mode = 'general' "
                "WHERE mode IS NULL OR mode = ''"
            )
        )

    # Add the monotonic revision only after legacy mode values have been
    # canonicalized.  On a pre-0028 database SQLite fills every existing row
    # with the declared zero default; a partially applied/reopened database
    # keeps its already persisted revision unchanged.
    _ensure_column(
        engine,
        "conversations",
        "scope_revision",
        "INTEGER NOT NULL DEFAULT 0",
    )

    with engine.begin() as conn:
        conversation_triggers = (
            "trg_conversations_scope_insert",
            "trg_conversations_scope_update",
            "trg_conversations_scope_revision_unchanged",
            "trg_conversations_mode_insert",
            "trg_conversations_mode_update",
        )
        for trigger_name in conversation_triggers:
            conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name}"))

        conn.execute(
            text(
                """
                CREATE TRIGGER trg_conversations_scope_insert
                BEFORE INSERT ON conversations
                BEGIN
                    SELECT CASE WHEN
                        typeof(NEW.scope_revision) <> 'integer'
                        OR NEW.scope_revision <> 0
                    THEN RAISE(ABORT, 'conversation scope revision must start at zero') END;
                    SELECT CASE WHEN
                        typeof(NEW.scope_revision) <> 'integer'
                        OR NEW.scope_revision < 0
                        OR NEW.scope_revision > 9223372036854775807
                    THEN RAISE(ABORT, 'conversation scope revision is out of range') END;
                END
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TRIGGER trg_conversations_scope_update
                BEFORE UPDATE ON conversations
                WHEN NEW.context_type IS NOT OLD.context_type
                  OR NEW.context_ref IS NOT OLD.context_ref
                  OR NEW.mode IS NOT OLD.mode
                BEGIN
                    SELECT CASE WHEN OLD.scope_revision = 9223372036854775807
                        THEN RAISE(ABORT, 'conversation scope revision overflow') END;
                    SELECT CASE WHEN
                        typeof(NEW.scope_revision) <> 'integer'
                        OR NEW.scope_revision < 0
                        OR NEW.scope_revision > 9223372036854775807
                        OR NEW.scope_revision <> OLD.scope_revision + 1
                    THEN RAISE(ABORT, 'conversation scope revision must increment') END;
                END
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TRIGGER trg_conversations_scope_revision_unchanged
                BEFORE UPDATE ON conversations
                WHEN NEW.context_type IS OLD.context_type
                  AND NEW.context_ref IS OLD.context_ref
                  AND NEW.mode IS OLD.mode
                  AND NEW.scope_revision IS NOT OLD.scope_revision
                BEGIN
                    SELECT RAISE(ABORT, 'conversation scope revision changed without scope mutation');
                END
                """
            )
        )
        mode_is_valid = _conversation_mode_is_valid_sql("NEW.mode")
        conn.execute(
            text(
                f"""
                CREATE TRIGGER trg_conversations_mode_insert
                BEFORE INSERT ON conversations
                BEGIN
                    SELECT CASE WHEN NOT ({mode_is_valid})
                      THEN RAISE(ABORT, 'invalid conversation mode') END;
                END
                """
            )
        )
        conn.execute(
            text(
                f"""
                CREATE TRIGGER trg_conversations_mode_update
                BEFORE UPDATE OF mode ON conversations
                BEGIN
                    SELECT CASE WHEN NOT ({mode_is_valid})
                      THEN RAISE(ABORT, 'invalid conversation mode') END;
                END
                """
            )
        )

        operation_triggers = (
            "trg_write_operation_scope_insert",
            "trg_write_operation_scope_fingerprint_immutable",
            "trg_write_operation_scope_identity_immutable",
            "trg_write_operation_scope_conversation_immutable",
            "trg_write_operation_scope_status",
        )
        for trigger_name in operation_triggers:
            conn.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name}"))

        conn.execute(
            text(
                """
                CREATE TRIGGER trg_write_operation_scope_insert
                BEFORE INSERT ON write_operations
                WHEN (
                    NEW.operation_role = 'primary'
                    AND NEW.adapter_kind = 'typed'
                    AND NEW.status = 'proposed'
                    AND NEW.authorization_scope_fingerprint IS NULL
                  ) OR (
                    NEW.authorization_scope_fingerprint IS NOT NULL
                    AND (
                      length(NEW.authorization_scope_fingerprint) <> 76
                      OR substr(NEW.authorization_scope_fingerprint,1,12) <> 'hmac-sha256:'
                      OR substr(NEW.authorization_scope_fingerprint,13) GLOB '*[^0-9a-f]*'
                    )
                  )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid authorization scope fingerprint');
                END
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TRIGGER trg_write_operation_scope_fingerprint_immutable
                BEFORE UPDATE OF authorization_scope_fingerprint ON write_operations
                WHEN NEW.authorization_scope_fingerprint IS NOT OLD.authorization_scope_fingerprint
                BEGIN
                    SELECT RAISE(ABORT, 'authorization scope fingerprint is immutable');
                END
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TRIGGER trg_write_operation_scope_identity_immutable
                BEFORE UPDATE ON write_operations
                WHEN NEW.operation_role IS NOT OLD.operation_role
                  OR NEW.adapter_kind IS NOT OLD.adapter_kind
                  OR NEW.tool_name IS NOT OLD.tool_name
                  OR NEW.tool_call_id IS NOT OLD.tool_call_id
                  OR NEW.fingerprint_key_id IS NOT OLD.fingerprint_key_id
                  OR NEW.proposal_fingerprint IS NOT OLD.proposal_fingerprint
                  OR NEW.confirmation_token_fingerprint IS NOT OLD.confirmation_token_fingerprint
                  OR NEW.parent_operation_id IS NOT OLD.parent_operation_id
                BEGIN
                    SELECT RAISE(ABORT, 'write operation authority identity is immutable');
                END
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TRIGGER trg_write_operation_scope_conversation_immutable
                BEFORE UPDATE OF conversation_id ON write_operations
                WHEN (OLD.conversation_id IS NULL AND NEW.conversation_id IS NOT NULL)
                  OR (OLD.conversation_id IS NOT NULL AND NEW.conversation_id IS NOT NULL
                      AND NEW.conversation_id <> OLD.conversation_id)
                BEGIN
                    SELECT RAISE(ABORT, 'write operation conversation binding is immutable');
                END
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TRIGGER trg_write_operation_scope_status
                BEFORE UPDATE OF status ON write_operations
                WHEN OLD.operation_role = 'primary'
                  AND OLD.adapter_kind = 'typed'
                  AND OLD.status = 'proposed'
                  AND OLD.authorization_scope_fingerprint IS NULL
                  AND NEW.status NOT IN ('proposed','rejected')
                BEGIN
                    SELECT RAISE(ABORT, 'unbound typed operation cannot become terminal');
                END
                """
            )
        )

    _record_migration(
        engine,
        "0028_scoped_tool_authority",
        "Add Conversation scope revision and Write Operation authorization fingerprint",
    )


def _review_to_readiness_table_sql(engine, table_name: str, replacement: str) -> str:  # type: ignore[no-untyped-def]
    compiled = str(CreateTable(Base.metadata.tables[table_name]).compile(dialect=engine.dialect))
    needle = f"CREATE TABLE {table_name} ("
    if needle not in compiled:
        raise RuntimeError(f"cannot compile controlled rebuild for {table_name}")
    return compiled.replace(needle, f"CREATE TABLE {replacement} (", 1)


def _compile_review_to_readiness_indexes(engine, table_name: str) -> list[str]:  # type: ignore[no-untyped-def]
    return [
        str(CreateIndex(index).compile(dialect=engine.dialect))
        for index in sorted(
            Base.metadata.tables[table_name].indexes,
            key=lambda item: item.name or "",
        )
    ]


def _review_to_readiness_migration_checkpoint(_checkpoint: str) -> None:
    """Named no-op hook used to prove that every 0029 DDL step rolls back."""


def _ensure_review_to_readiness_feedback_schema(
    engine: Engine,
    *,
    force_rebuild: bool = False,
) -> None:
    """Install the destructive 0029 rebuild and its cross-row SQLite guards."""

    write_table_sql = _review_to_readiness_table_sql(
        engine,
        "write_operations",
        "write_operations_0029",
    )
    practice_table_sql = _review_to_readiness_table_sql(
        engine,
        "adaptive_practice_plans",
        "adaptive_practice_plans_0029",
    )
    note_table_sql = _review_to_readiness_table_sql(
        engine,
        "interview_notes",
        "interview_notes_0029",
    )
    write_index_sql = _compile_review_to_readiness_indexes(engine, "write_operations")
    practice_index_sql = _compile_review_to_readiness_indexes(
        engine,
        "adaptive_practice_plans",
    )

    raw = engine.raw_connection()
    cursor = raw.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = OFF")
        cursor.execute("BEGIN IMMEDIATE")
        marker_exists = cursor.execute(
            "SELECT 1 FROM schema_migrations "
            "WHERE version='0029_review_to_readiness_feedback'"
        ).fetchone() is not None

        if not marker_exists:
            additive_columns = (
                (
                    "interview_review_proposals",
                    "proposal_schema_version",
                    "INTEGER NOT NULL DEFAULT 1",
                ),
                (
                    "interview_review_proposals",
                    "source_note_revision",
                    "INTEGER",
                ),
                (
                    "interview_story_proposal_attempts",
                    "product_action_operation_id",
                    "VARCHAR(36) REFERENCES write_operations(id) ON DELETE RESTRICT",
                ),
                (
                    "interview_story_proposal_attempts",
                    "product_action_generation",
                    "INTEGER NOT NULL DEFAULT 0",
                ),
            )
            for table_name, column_name, definition in additive_columns:
                columns = {
                    str(row[1])
                    for row in cursor.execute(f"PRAGMA table_info({table_name})")
                }
                if column_name not in columns:
                    cursor.execute(
                        f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {definition}'
                    )
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_interview_story_attempt_product_action_operation "
                "ON interview_story_proposal_attempts(product_action_operation_id) "
                "WHERE product_action_operation_id IS NOT NULL"
            )

            note_columns = [
                str(row[1]) for row in cursor.execute("PRAGMA table_info(interview_notes)")
            ]
            expected_note_columns = [
                column.name for column in Base.metadata.tables["interview_notes"].columns
            ]
            if note_columns != expected_note_columns:
                unknown_note_columns = set(note_columns) - set(expected_note_columns)
                if unknown_note_columns:
                    raise RuntimeError("unsupported pre-0029 interview_notes columns")
                note_triggers = [
                    (str(row[0]), str(row[1]))
                    for row in cursor.execute(
                        "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                        "AND sql IS NOT NULL AND instr(lower(sql),'interview_notes') > 0 "
                        "ORDER BY name"
                    )
                ]
                note_indexes = [
                    str(row[0])
                    for row in cursor.execute(
                        "SELECT sql FROM sqlite_master WHERE type='index' "
                        "AND tbl_name='interview_notes' AND sql IS NOT NULL ORDER BY name"
                    )
                ]
                for trigger_name, _statement in note_triggers:
                    cursor.execute(f'DROP TRIGGER "{trigger_name}"')
                cursor.execute("DROP TABLE IF EXISTS interview_notes_0029")
                cursor.execute(note_table_sql)
                target_note_columns = ",".join(
                    f'\"{column}\"' for column in expected_note_columns
                )
                note_source_expressions: list[str] = []
                for column in expected_note_columns:
                    if column in note_columns:
                        note_source_expressions.append(f'\"{column}\"')
                    elif column == "content_revision":
                        note_source_expressions.append("1")
                    elif column == "updated_at":
                        note_source_expressions.append("coalesce(created_at,CURRENT_TIMESTAMP)")
                    else:
                        raise RuntimeError("unsupported interview note migration column")
                cursor.execute(
                    f"INSERT INTO interview_notes_0029 ({target_note_columns}) "
                    f"SELECT {','.join(note_source_expressions)} FROM interview_notes"
                )
                cursor.execute("DROP TABLE interview_notes")
                cursor.execute("ALTER TABLE interview_notes_0029 RENAME TO interview_notes")
                for statement in note_indexes:
                    cursor.execute(statement)
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_notes_app ON interview_notes(application_id)"
                )
                for _trigger_name, statement in note_triggers:
                    cursor.execute(statement)

            invalid_proposals = cursor.execute(
                """
                SELECT count(*) FROM interview_review_proposals
                WHERE NOT (
                  (typeof(proposal_schema_version) = 'integer'
                   AND proposal_schema_version = 1
                   AND source_note_revision IS NULL)
                  OR
                  (typeof(proposal_schema_version) = 'integer'
                   AND proposal_schema_version = 2
                   AND typeof(source_note_revision) = 'integer'
                   AND source_note_revision >= 1)
                )
                """
            ).fetchone()[0]
            if invalid_proposals:
                raise RuntimeError("invalid pre-0029 interview review proposal history")
            invalid_story_attempts = cursor.execute(
                """
                SELECT count(*) FROM interview_story_proposal_attempts
                WHERE NOT (
                  typeof(product_action_generation) = 'integer'
                  AND product_action_generation >= 0
                  AND (
                    (product_action_generation = 0
                     AND product_action_operation_id IS NULL)
                    OR
                    (product_action_generation >= 1
                     AND product_action_operation_id IS NOT NULL)
                  )
                )
                """
            ).fetchone()[0]
            if invalid_story_attempts:
                raise RuntimeError("invalid pre-0029 story product action history")

        persisted_write_sql = str(
            cursor.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='write_operations'"
            ).fetchone()[0]
        )
        persisted_practice_sql = str(
            cursor.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='adaptive_practice_plans'"
            ).fetchone()[0]
        )
        should_rebuild = force_rebuild or (
            not marker_exists
            and (
                "ck_write_operations_product_action_scope_bound" not in persisted_write_sql
                or "ck_adaptive_practice_origin_contract" not in persisted_practice_sql
                or "uq_adaptive_practice_proposal_focus" in persisted_practice_sql
            )
        )
        if should_rebuild:
            operation_columns = [
                str(row[1]) for row in cursor.execute("PRAGMA table_info(write_operations)")
            ]
            expected_operation_columns = [
                column.name for column in Base.metadata.tables["write_operations"].columns
            ]
            if set(operation_columns) != set(expected_operation_columns):
                raise RuntimeError("unsupported pre-0029 write_operations columns")
            operation_before = cursor.execute(
                "SELECT "
                + ",".join(f'\"{column}\"' for column in operation_columns)
                + " FROM write_operations ORDER BY id"
            ).fetchall()
            transition_columns = [
                str(row[1])
                for row in cursor.execute("PRAGMA table_info(write_operation_transitions)")
            ]
            transitions_before = cursor.execute(
                "SELECT "
                + ",".join(f'\"{column}\"' for column in transition_columns)
                + " FROM write_operation_transitions ORDER BY operation_id,seq,id"
            ).fetchall()
            operation_triggers = [
                (str(row[0]), str(row[1]))
                for row in cursor.execute(
                    "SELECT name,sql FROM sqlite_master "
                    "WHERE type='trigger' AND sql IS NOT NULL "
                    "AND instr(lower(sql),'write_operations') > 0 "
                    "ORDER BY name"
                )
            ]
            for trigger_name, _statement in operation_triggers:
                cursor.execute(f'DROP TRIGGER "{trigger_name}"')

            cursor.execute("DROP TABLE IF EXISTS write_operations_0029")
            cursor.execute(write_table_sql)
            quoted_operations = ",".join(f'\"{column}\"' for column in operation_columns)
            cursor.execute(
                f"INSERT INTO write_operations_0029 ({quoted_operations}) "
                f"SELECT {quoted_operations} FROM write_operations"
            )
            cursor.execute("DROP TABLE write_operations")
            cursor.execute("ALTER TABLE write_operations_0029 RENAME TO write_operations")
            for statement in write_index_sql:
                cursor.execute(statement)
            for _trigger_name, statement in operation_triggers:
                cursor.execute(statement)

            operation_after = cursor.execute(
                "SELECT "
                + ",".join(f'\"{column}\"' for column in operation_columns)
                + " FROM write_operations ORDER BY id"
            ).fetchall()
            transitions_after = cursor.execute(
                "SELECT "
                + ",".join(f'\"{column}\"' for column in transition_columns)
                + " FROM write_operation_transitions ORDER BY operation_id,seq,id"
            ).fetchall()
            if operation_after != operation_before:
                raise RuntimeError("0029 changed historical write operation bytes")
            if transitions_after != transitions_before:
                raise RuntimeError("0029 changed historical transition bytes")

            practice_columns = [
                str(row[1])
                for row in cursor.execute("PRAGMA table_info(adaptive_practice_plans)")
            ]
            expected_practice_columns = [
                column.name for column in Base.metadata.tables["adaptive_practice_plans"].columns
            ]
            unknown_practice_columns = set(practice_columns) - set(expected_practice_columns)
            if unknown_practice_columns:
                raise RuntimeError("unsupported pre-0029 adaptive_practice_plans columns")
            cursor.execute("DROP TABLE IF EXISTS adaptive_practice_plans_0029")
            cursor.execute(practice_table_sql)
            target_columns = ",".join(
                f'\"{column}\"' for column in expected_practice_columns
            )
            source_expressions: list[str] = []
            for column in expected_practice_columns:
                if column in practice_columns:
                    source_expressions.append(f'\"{column}\"')
                elif column == "origin_contract":
                    source_expressions.append("'legacy_review_focus_v1'")
                else:
                    source_expressions.append("NULL")
            cursor.execute(
                f"INSERT INTO adaptive_practice_plans_0029 ({target_columns}) "
                f"SELECT {','.join(source_expressions)} FROM adaptive_practice_plans"
            )
            _review_to_readiness_migration_checkpoint("before_adaptive_swap")
            cursor.execute("DROP TABLE adaptive_practice_plans")
            cursor.execute(
                "ALTER TABLE adaptive_practice_plans_0029 RENAME TO adaptive_practice_plans"
            )
            for statement in practice_index_sql:
                cursor.execute(statement)

        trigger_names = (
            "trg_product_action_route_insert",
            "trg_product_action_route_identity_immutable",
            "trg_product_action_route_terminalize",
            "trg_product_action_route_delete",
            "trg_product_action_parent_terminal_guard",
            "trg_product_action_parent_terminalize_route",
            "trg_interview_readiness_signal_source_immutable",
            "trg_interview_readiness_signal_current_insert",
            "trg_interview_readiness_signal_current_update",
            "trg_interview_readiness_signal_version_immutable",
            "trg_interview_readiness_signal_version_delete",
            "trg_interview_review_proposal_contract_insert",
            "trg_interview_review_proposal_contract_update",
            "trg_interview_story_product_action_insert",
            "trg_interview_story_product_action_update",
            "trg_adaptive_practice_v2_insert",
            "trg_adaptive_practice_v2_identity_immutable",
            "trg_adaptive_practice_v2_locator_monotonic",
            "trg_write_operation_compensation_insert",
            "trg_write_operation_compensation_update",
        )
        for trigger_name in trigger_names:
            cursor.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")

        cursor.execute(
            """
            CREATE TRIGGER trg_product_action_route_insert
            BEFORE INSERT ON product_action_proposals
            BEGIN
                SELECT CASE WHEN NEW.route_payload_json IS NULL
                    OR NEW.terminalized_at IS NOT NULL
                    OR NOT EXISTS (
                      SELECT 1 FROM write_operations parent
                      WHERE parent.id = NEW.operation_id
                        AND parent.operation_role = 'primary'
                        AND parent.adapter_kind = 'product_action'
                        AND parent.status = 'proposed'
                        AND parent.tool_call_id = NEW.action_call_id
                        AND parent.tool_name = NEW.action_name
                    )
                  THEN RAISE(ABORT, 'invalid product action parent') END;
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER trg_product_action_route_identity_immutable
            BEFORE UPDATE ON product_action_proposals
            WHEN NEW.operation_id IS NOT OLD.operation_id
              OR NEW.action_call_id IS NOT OLD.action_call_id
              OR NEW.action_name IS NOT OLD.action_name
              OR NEW.request_origin IS NOT OLD.request_origin
              OR NEW.schema_version IS NOT OLD.schema_version
              OR NEW.source_kind IS NOT OLD.source_kind
              OR NEW.source_id IS NOT OLD.source_id
              OR NEW.source_revision IS NOT OLD.source_revision
              OR NEW.route_payload_fingerprint IS NOT OLD.route_payload_fingerprint
              OR NEW.route_binding_fingerprint IS NOT OLD.route_binding_fingerprint
              OR NEW.request_idempotency_fingerprint IS NOT OLD.request_idempotency_fingerprint
              OR NEW.semantic_claim_fingerprint IS NOT OLD.semantic_claim_fingerprint
              OR NEW.historical_request_token_fingerprint IS NOT OLD.historical_request_token_fingerprint
              OR NEW.created_at IS NOT OLD.created_at
            BEGIN
                SELECT RAISE(ABORT, 'product action route identity is immutable');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER trg_product_action_route_terminalize
            BEFORE UPDATE OF route_payload_json, terminalized_at ON product_action_proposals
            WHEN NEW.route_payload_json IS NOT OLD.route_payload_json
              OR NEW.terminalized_at IS NOT OLD.terminalized_at
            BEGIN
                SELECT CASE WHEN NOT (
                  OLD.route_payload_json IS NOT NULL AND OLD.terminalized_at IS NULL
                  AND NEW.route_payload_json IS NULL AND NEW.terminalized_at IS NOT NULL
                  AND EXISTS (
                    SELECT 1 FROM write_operations parent
                    WHERE parent.id = OLD.operation_id
                      AND parent.operation_role = 'primary'
                      AND parent.adapter_kind = 'product_action'
                      AND parent.status IN ('rejected','committed','failed')
                      AND parent.tool_call_id = OLD.action_call_id
                      AND parent.tool_name = OLD.action_name
                  )
                ) THEN RAISE(ABORT, 'invalid product action route terminalization') END;
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER trg_product_action_route_delete
            BEFORE DELETE ON product_action_proposals
            BEGIN
                SELECT RAISE(ABORT, 'product action route is immutable');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER trg_product_action_parent_terminal_guard
            BEFORE UPDATE OF status ON write_operations
            WHEN OLD.operation_role = 'primary'
              AND OLD.adapter_kind = 'product_action'
              AND OLD.status = 'proposed'
              AND NEW.status IN ('rejected','committed','failed')
            BEGIN
                SELECT CASE WHEN NOT EXISTS (
                  SELECT 1 FROM product_action_proposals route
                  WHERE route.operation_id = OLD.id
                    AND route.action_call_id = OLD.tool_call_id
                    AND route.action_name = OLD.tool_name
                    AND route.route_payload_json IS NOT NULL
                    AND route.terminalized_at IS NULL
                ) THEN RAISE(ABORT, 'product action route missing') END;
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER trg_product_action_parent_terminalize_route
            AFTER UPDATE OF status ON write_operations
            WHEN OLD.operation_role = 'primary'
              AND OLD.adapter_kind = 'product_action'
              AND OLD.status = 'proposed'
              AND NEW.status IN ('rejected','committed','failed')
            BEGIN
                UPDATE product_action_proposals
                   SET route_payload_json = NULL,
                       terminalized_at = CASE NEW.status
                         WHEN 'rejected' THEN NEW.rejected_at
                         WHEN 'committed' THEN NEW.committed_at
                         ELSE NEW.failed_at
                       END
                 WHERE operation_id = NEW.id
                   AND action_call_id = NEW.tool_call_id
                   AND action_name = NEW.tool_name;
                SELECT CASE WHEN changes() <> 1
                  THEN RAISE(ABORT, 'product action route terminalization failed') END;
            END
            """
        )

        cursor.execute(
            """
            CREATE TRIGGER trg_interview_readiness_signal_source_immutable
            BEFORE UPDATE ON interview_readiness_signals
            WHEN NEW.application_id IS NOT OLD.application_id
              OR (NEW.source_event_id IS NOT OLD.source_event_id
                  AND (OLD.source_event_id IS NULL OR NEW.source_event_id IS NOT NULL))
              OR (NEW.source_note_id IS NOT OLD.source_note_id
                  AND (OLD.source_note_id IS NULL OR NEW.source_note_id IS NOT NULL))
              OR (NEW.source_proposal_id IS NOT OLD.source_proposal_id
                  AND (OLD.source_proposal_id IS NULL OR NEW.source_proposal_id IS NOT NULL))
            BEGIN
                SELECT RAISE(ABORT, 'readiness signal source identity is immutable');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER trg_interview_readiness_signal_current_insert
            BEFORE INSERT ON interview_readiness_signals
            WHEN NEW.current_version_id IS NOT NULL
            BEGIN
                SELECT CASE WHEN NOT EXISTS (
                  SELECT 1 FROM interview_readiness_signal_versions version
                  WHERE version.id = NEW.current_version_id AND version.signal_id = NEW.id
                ) THEN RAISE(ABORT, 'invalid readiness signal current version') END;
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER trg_interview_readiness_signal_current_update
            BEFORE UPDATE OF current_version_id ON interview_readiness_signals
            WHEN NEW.current_version_id IS NOT NULL
            BEGIN
                SELECT CASE WHEN NOT EXISTS (
                  SELECT 1 FROM interview_readiness_signal_versions version
                  WHERE version.id = NEW.current_version_id AND version.signal_id = NEW.id
                ) THEN RAISE(ABORT, 'invalid readiness signal current version') END;
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER trg_interview_readiness_signal_version_immutable
            BEFORE UPDATE ON interview_readiness_signal_versions
            BEGIN
                SELECT RAISE(ABORT, 'readiness signal version is immutable');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER trg_interview_readiness_signal_version_delete
            BEFORE DELETE ON interview_readiness_signal_versions
            WHEN EXISTS (
              SELECT 1 FROM interview_readiness_signals signal WHERE signal.id = OLD.signal_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'readiness signal version delete requires aggregate owner');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER trg_interview_review_proposal_contract_insert
            BEFORE INSERT ON interview_review_proposals
            WHEN NOT (
              (typeof(NEW.proposal_schema_version) = 'integer'
               AND NEW.proposal_schema_version = 1
               AND NEW.source_note_revision IS NULL)
              OR
              (typeof(NEW.proposal_schema_version) = 'integer'
               AND NEW.proposal_schema_version = 2
               AND typeof(NEW.source_note_revision) = 'integer'
               AND NEW.source_note_revision >= 1)
            )
            BEGIN
                SELECT RAISE(ABORT, 'invalid interview review proposal source revision');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER trg_interview_review_proposal_contract_update
            BEFORE UPDATE OF proposal_schema_version, source_note_revision
            ON interview_review_proposals
            WHEN NOT (
              (typeof(NEW.proposal_schema_version) = 'integer'
               AND NEW.proposal_schema_version = 1
               AND NEW.source_note_revision IS NULL)
              OR
              (typeof(NEW.proposal_schema_version) = 'integer'
               AND NEW.proposal_schema_version = 2
               AND typeof(NEW.source_note_revision) = 'integer'
               AND NEW.source_note_revision >= 1)
            )
            BEGIN
                SELECT RAISE(ABORT, 'invalid interview review proposal source revision');
            END
            """
        )
        story_product_action_truth = """
          typeof(NEW.product_action_generation) = 'integer'
          AND NEW.product_action_generation >= 0
          AND (
            (NEW.product_action_generation = 0
             AND NEW.product_action_operation_id IS NULL)
            OR
            (NEW.product_action_generation >= 1
             AND NEW.product_action_operation_id IS NOT NULL)
          )
        """
        cursor.execute(
            f"""
            CREATE TRIGGER trg_interview_story_product_action_insert
            BEFORE INSERT ON interview_story_proposal_attempts
            WHEN NOT ({story_product_action_truth})
            BEGIN
                SELECT RAISE(ABORT, 'invalid story product action generation');
            END
            """
        )
        cursor.execute(
            f"""
            CREATE TRIGGER trg_interview_story_product_action_update
            BEFORE UPDATE OF product_action_operation_id, product_action_generation
            ON interview_story_proposal_attempts
            WHEN NOT ({story_product_action_truth})
            BEGIN
                SELECT RAISE(ABORT, 'invalid story product action generation');
            END
            """
        )

        cursor.execute(
            """
            CREATE TRIGGER trg_adaptive_practice_v2_insert
            BEFORE INSERT ON adaptive_practice_plans
            WHEN NEW.origin_contract = 'confirmed_readiness_signal_v1'
              AND (NEW.readiness_signal_version_id IS NULL
                   OR NEW.target_application_event_id IS NULL)
            BEGIN
                SELECT RAISE(ABORT, 'new readiness practice requires live source and target');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER trg_adaptive_practice_v2_identity_immutable
            BEFORE UPDATE ON adaptive_practice_plans
            WHEN NEW.origin_contract IS NOT OLD.origin_contract
              OR NEW.source_fingerprint IS NOT OLD.source_fingerprint
              OR NEW.target_fingerprint IS NOT OLD.target_fingerprint
            BEGIN
                SELECT RAISE(ABORT, 'adaptive practice source identity is immutable');
            END
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER trg_adaptive_practice_v2_locator_monotonic
            BEFORE UPDATE ON adaptive_practice_plans
            WHEN (NEW.readiness_signal_version_id IS NOT OLD.readiness_signal_version_id
                  AND (OLD.readiness_signal_version_id IS NULL
                       OR NEW.readiness_signal_version_id IS NOT NULL))
              OR (NEW.target_application_event_id IS NOT OLD.target_application_event_id
                  AND (OLD.target_application_event_id IS NULL
                       OR NEW.target_application_event_id IS NOT NULL))
            BEGIN
                SELECT RAISE(ABORT, 'adaptive practice locator is monotonic');
            END
            """
        )

        product_compensation_parent = """
          SELECT 1 FROM write_operations parent
          WHERE parent.id = NEW.parent_operation_id
            AND parent.operation_role = 'primary'
            AND parent.status = 'committed'
            AND parent.terminal_payload_sha256 = NEW.parent_terminal_payload_sha256
            AND length(NEW.parent_terminal_payload_sha256) = 71
            AND substr(NEW.parent_terminal_payload_sha256,1,7) = 'sha256:'
            AND substr(NEW.parent_terminal_payload_sha256,8) NOT GLOB '*[^0-9a-f]*'
            AND (
              (parent.adapter_kind = 'typed' AND (
                (parent.tool_name = 'update_application_status'
                 AND NEW.tool_name = 'undo:update_application_status') OR
                (parent.tool_name = 'create_application'
                 AND NEW.tool_name = 'undo:create_application') OR
                (parent.tool_name = 'create_application_event'
                 AND NEW.tool_name = 'undo:create_application_event') OR
                (parent.tool_name = 'add_note' AND NEW.tool_name = 'undo:add_note')
              )) OR
              (parent.adapter_kind = 'product_action' AND (
                (parent.tool_name = 'confirm_interview_story'
                 AND NEW.tool_name = 'undo:confirm_interview_story') OR
                (parent.tool_name = 'save_review_readiness_signal'
                 AND NEW.tool_name = 'undo:save_review_readiness_signal')
              ))
            )
        """
        cursor.execute(
            f"""
            CREATE TRIGGER trg_write_operation_compensation_insert
            BEFORE INSERT ON write_operations
            WHEN NEW.operation_role = 'compensation'
            BEGIN
                SELECT CASE WHEN NOT EXISTS ({product_compensation_parent})
                  THEN RAISE(ABORT, 'invalid compensation parent') END;
            END
            """
        )
        cursor.execute(
            f"""
            CREATE TRIGGER trg_write_operation_compensation_update
            BEFORE UPDATE OF parent_operation_id, parent_terminal_payload_sha256,
                             operation_role, tool_name ON write_operations
            WHEN NEW.operation_role = 'compensation'
            BEGIN
                SELECT CASE WHEN NOT EXISTS ({product_compensation_parent})
                  THEN RAISE(ABORT, 'invalid compensation parent') END;
            END
            """
        )

        integrity = cursor.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise RuntimeError("0029 integrity check failed")
        foreign_key_violations = cursor.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_violations:
            raise RuntimeError("0029 foreign key check failed")
        if not marker_exists:
            cursor.execute(
                "INSERT INTO schema_migrations(version,description) VALUES (?,?)",
                (
                    "0029_review_to_readiness_feedback",
                    "Add review-to-readiness Product Action, Signal, and Practice V2 schema",
                ),
            )
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        try:
            cursor.execute("PRAGMA foreign_keys = ON")
        finally:
            cursor.close()
            raw.close()


def _rebuild_chat_messages_for_write_operation_integrity(engine) -> None:  # type: ignore[no-untyped-def]
    with engine.connect() as conn:
        table_sql = str(
            conn.scalar(
                text(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'chat_messages'"
                )
            )
            or ""
        )
        foreign_keys = list(conn.execute(text("PRAGMA foreign_key_list(chat_messages)")))
    has_operation_fk = any(
        str(row[2]) == "write_operations" and str(row[3]) == "operation_id" for row in foreign_keys
    )
    if (
        "ck_chat_messages_delivery_group" in table_sql
        and "ck_chat_messages_delivery_shape" in table_sql
        and has_operation_fk
    ):
        return

    raw = engine.raw_connection()
    cursor = raw.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = OFF")
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("DROP TABLE IF EXISTS chat_messages_0026")
        cursor.execute(
            """
            CREATE TABLE chat_messages_0026 (
                id INTEGER NOT NULL PRIMARY KEY,
                conversation_id INTEGER NOT NULL,
                role VARCHAR NOT NULL,
                content VARCHAR NOT NULL DEFAULT '',
                tool_calls VARCHAR NOT NULL DEFAULT '',
                tool_call_id VARCHAR NOT NULL DEFAULT '',
                provider_blocks VARCHAR NOT NULL DEFAULT '',
                operation_id VARCHAR(36),
                delivery_kind VARCHAR,
                delivery_ordinal INTEGER,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT ck_chat_messages_delivery_group CHECK (
                    (operation_id IS NULL AND delivery_kind IS NULL AND delivery_ordinal IS NULL)
                    OR
                    (operation_id IS NOT NULL AND delivery_kind IS NOT NULL AND delivery_ordinal IS NOT NULL)
                ),
                CONSTRAINT ck_chat_messages_delivery_shape CHECK (
                    operation_id IS NULL
                    OR (delivery_kind = 'origin_tool_result' AND delivery_ordinal = 0
                        AND role = 'tool' AND tool_call_id <> '')
                    OR (delivery_kind = 'continuation_message' AND delivery_ordinal >= 1
                        AND ((role = 'tool' AND tool_call_id <> '')
                             OR (role = 'assistant' AND tool_call_id = '')))
                ),
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
                FOREIGN KEY(operation_id) REFERENCES write_operations(id) ON DELETE RESTRICT
            )
            """
        )
        cursor.execute(
            """
            INSERT INTO chat_messages_0026 (
                id, conversation_id, role, content, tool_calls, tool_call_id,
                provider_blocks, operation_id, delivery_kind, delivery_ordinal, created_at
            )
            SELECT id, conversation_id, role, coalesce(content, ''),
                   coalesce(tool_calls, ''), coalesce(tool_call_id, ''),
                   coalesce(provider_blocks, ''), operation_id, delivery_kind,
                   delivery_ordinal, coalesce(created_at, CURRENT_TIMESTAMP)
            FROM chat_messages
            """
        )
        violations = cursor.execute(
            "PRAGMA foreign_key_check(chat_messages_0026)"
        ).fetchall()
        if violations:
            raise RuntimeError("chat message foreign key migration failed")
        # A previous 0026 attempt may already have installed this cross-table
        # trigger. SQLite reparses it during ALTER TABLE, so remove it inside
        # the same transaction before swapping chat_messages; the caller
        # recreates the trigger immediately after the rebuild.
        cursor.execute("DROP TRIGGER IF EXISTS trg_write_operation_chat_restrict")
        cursor.execute("DROP TABLE chat_messages")
        cursor.execute("ALTER TABLE chat_messages_0026 RENAME TO chat_messages")
        cursor.execute(
            "CREATE INDEX idx_chat_messages_conv ON chat_messages(conversation_id)"
        )
        raw.commit()
        cursor.execute("PRAGMA foreign_keys = ON")
    except Exception:
        raw.rollback()
        raise
    finally:
        try:
            cursor.execute("PRAGMA foreign_keys = ON")
        finally:
            cursor.close()
            raw.close()


def _ensure_offer_negotiation_schema(engine) -> None:  # type: ignore[no-untyped-def]
    """Record the additive Offer comparison and negotiation schema migration."""

    _ensure_column(
        engine,
        "offer_negotiation_briefs",
        "confirmation_key",
        "TEXT NOT NULL DEFAULT ''",
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_offer_negotiation_proposals_status "
                "ON offer_negotiation_proposals(attempt_status)"
            )
        )
        conn.execute(
            text(
                "INSERT OR IGNORE INTO schema_migrations (version, description) "
                "VALUES ('0017_offer_comparison_negotiation', "
                "'Add Offer comparison dimensions, values, negotiation proposals and briefs')"
            )
        )


def _ensure_application_jd_versions_schema(engine) -> None:  # type: ignore[no-untyped-def]
    """Create immutable Application JD versions and additive identity columns."""

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS application_jd_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_id INTEGER NOT NULL
                        REFERENCES applications(id) ON DELETE CASCADE,
                    version_number INTEGER NOT NULL,
                    jd_text TEXT NOT NULL,
                    content_sha256 VARCHAR NOT NULL,
                    source_url VARCHAR,
                    source_kind VARCHAR NOT NULL,
                    idempotency_key VARCHAR NOT NULL,
                    request_fingerprint_sha256 VARCHAR NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_application_jd_versions_application_version
                        UNIQUE (application_id, version_number),
                    CONSTRAINT uq_application_jd_versions_application_key
                        UNIQUE (application_id, idempotency_key)
                )
                """
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_application_jd_versions_app_version "
                "ON application_jd_versions(application_id, version_number)"
            )
        )

    for table in (
        "jd_analyses",
        "resume_matches",
        "application_material_kits",
        "material_revision_proposals",
        "opportunity_fit_reviews",
        "opportunity_fit_review_sessions",
        "opportunity_fit_review_stages",
        "interview_preparation_proposals",
        "mock_interview_attempts",
    ):
        _ensure_column(engine, table, "jd_version_id", "INTEGER")

    _record_migration(
        engine,
        "0018_application_jd_versions",
        "Add immutable Application JD version history and identity columns",
    )


def _ensure_interview_story_schema(engine) -> None:  # type: ignore[no-untyped-def]
    """Record the additive, independent Interview Story schema migration."""

    # Phase-one Story databases created before the release-audit hardening need
    # the same bounded repair evidence as fresh databases.  Keep it additive so
    # immutable Stories, Versions, and Attempts remain readable.
    _ensure_column(
        engine,
        "interview_story_proposal_attempts",
        "repair_count",
        "INTEGER NOT NULL DEFAULT 0",
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_interview_story_attempt_status "
                "ON interview_story_proposal_attempts(attempt_status)"
            )
        )
        conn.execute(
            text(
                "INSERT OR IGNORE INTO schema_migrations (version, description) "
                "VALUES ('0019_interview_story_library', "
                "'Add versioned interview stories with evidence-gated proposal attempts')"
            )
        )


def _ensure_application_outcome_schema(engine) -> None:  # type: ignore[no-untyped-def]
    """Record the additive immutable application outcome schema migration."""

    _record_migration(
        engine,
        "0020_application_outcome_feedback",
        "Add frozen application submission snapshots and append-only outcome feedback",
    )


def _ensure_adaptive_interview_practice_schema(engine) -> None:  # type: ignore[no-untyped-def]
    """Record the additive deterministic adaptive-practice schema."""

    _record_migration(
        engine,
        "0021_adaptive_interview_practice",
        "Add evidence-backed adaptive interview practice plans",
    )


def _ensure_voice_coaching_schema(engine) -> None:  # type: ignore[no-untyped-def]
    """Record the additive immutable voice-coaching snapshot schema."""

    _record_migration(
        engine,
        "0022_voice_coaching_snapshots",
        "Add immutable user-confirmed local voice coaching snapshots",
    )


def _ensure_interview_studio_schema(engine) -> None:  # type: ignore[no-untyped-def]
    """Add the dual-context contract used by real and quick interview practice."""

    attempt_columns = _table_info(engine, "mock_interview_attempts")
    if attempt_columns and (
        "context_kind" not in attempt_columns
        or attempt_columns.get("application_id", (None, 0))[1] == 1
        or attempt_columns.get("event_id", (None, 0))[1] == 1
    ):
        _rebuild_mock_interview_attempts_for_context(engine)
    voice_columns = _table_info(engine, "voice_coaching_snapshots")
    if voice_columns and (
        "context_kind" not in voice_columns
        or voice_columns.get("application_id", (None, 0))[1] == 1
        or voice_columns.get("event_id", (None, 0))[1] == 1
    ):
        _rebuild_voice_coaching_snapshots_for_context(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_mock_interview_attempts_context "
                "ON mock_interview_attempts(context_kind, practice_case_id)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_voice_coaching_snapshots_context "
                "ON voice_coaching_snapshots(context_kind, practice_case_id)"
            )
        )
    _record_migration(
        engine,
        "0023_immersive_interview_studio",
        "Add immutable quick-practice cases and dual interview contexts",
    )


def _table_info(engine, table: str) -> dict[str, tuple[str, int]]:  # type: ignore[no-untyped-def]
    with engine.connect() as conn:
        return {
            str(row[1]): (str(row[2]), int(row[3]))
            for row in conn.execute(text(f"PRAGMA table_info({table})"))
        }


def _rebuild_mock_interview_attempts_for_context(engine) -> None:  # type: ignore[no-untyped-def]
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.exec_driver_sql(
            """
            CREATE TABLE mock_interview_attempts_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                context_kind VARCHAR NOT NULL DEFAULT 'application_event',
                application_id INTEGER,
                event_id INTEGER,
                practice_case_id INTEGER REFERENCES interview_practice_cases(id) ON DELETE RESTRICT,
                resume_id INTEGER NOT NULL,
                jd_version_id INTEGER,
                idempotency_key VARCHAR NOT NULL,
                input_snapshot_json TEXT NOT NULL,
                source_fingerprint VARCHAR NOT NULL,
                attempt_status VARCHAR NOT NULL,
                generation_revision INTEGER NOT NULL DEFAULT 1,
                provider_call_token VARCHAR NOT NULL DEFAULT '',
                provider_lease_until DATETIME,
                current_turn_no INTEGER NOT NULL DEFAULT 0,
                transcript_fingerprint VARCHAR NOT NULL,
                failure_category VARCHAR NOT NULL DEFAULT '',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at DATETIME,
                cancelled_at DATETIME,
                CONSTRAINT uq_mock_interview_attempts_context_key
                    UNIQUE (context_kind, application_id, event_id, practice_case_id, idempotency_key),
                CONSTRAINT ck_mock_interview_attempt_context CHECK (
                    (context_kind = 'application_event' AND application_id IS NOT NULL AND event_id IS NOT NULL AND practice_case_id IS NULL)
                    OR (context_kind = 'quick_practice' AND application_id IS NULL AND event_id IS NULL AND practice_case_id IS NOT NULL)
                )
            )
            """
        )
        conn.exec_driver_sql(
            """
            INSERT INTO mock_interview_attempts_new (
                id, context_kind, application_id, event_id, practice_case_id,
                resume_id, jd_version_id, idempotency_key, input_snapshot_json,
                source_fingerprint, attempt_status, generation_revision,
                provider_call_token, provider_lease_until, current_turn_no,
                transcript_fingerprint, failure_category, created_at, completed_at,
                cancelled_at
            )
            SELECT id, 'application_event', application_id, event_id, NULL,
                   resume_id, jd_version_id, idempotency_key, input_snapshot_json,
                   source_fingerprint, attempt_status, generation_revision,
                   provider_call_token, provider_lease_until, current_turn_no,
                   transcript_fingerprint, failure_category, created_at, completed_at,
                   cancelled_at
            FROM mock_interview_attempts
            """
        )
        conn.exec_driver_sql("DROP TABLE mock_interview_attempts")
        conn.exec_driver_sql(
            "ALTER TABLE mock_interview_attempts_new RENAME TO mock_interview_attempts"
        )
        conn.exec_driver_sql(
            "CREATE INDEX idx_mock_interview_attempts_event "
            "ON mock_interview_attempts(application_id, event_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX idx_mock_interview_attempts_context "
            "ON mock_interview_attempts(context_kind, practice_case_id)"
        )
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        conn.commit()


def _rebuild_voice_coaching_snapshots_for_context(engine) -> None:  # type: ignore[no-untyped-def]
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.exec_driver_sql(
            """
            CREATE TABLE voice_coaching_snapshots_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER NOT NULL REFERENCES mock_interview_attempts(id) ON DELETE CASCADE,
                turn_id INTEGER NOT NULL REFERENCES mock_interview_turns(id) ON DELETE CASCADE,
                context_kind VARCHAR NOT NULL DEFAULT 'application_event',
                application_id INTEGER,
                event_id INTEGER,
                practice_case_id INTEGER REFERENCES interview_practice_cases(id) ON DELETE RESTRICT,
                idempotency_key VARCHAR NOT NULL,
                request_fingerprint_sha256 VARCHAR NOT NULL,
                question_text_snapshot TEXT NOT NULL,
                confirmed_answer_text_snapshot TEXT NOT NULL,
                answer_sha256 VARCHAR NOT NULL,
                measurement_source VARCHAR NOT NULL DEFAULT 'local_browser_measurement',
                total_duration_ms INTEGER NOT NULL,
                voiced_duration_ms INTEGER NOT NULL,
                pause_count INTEGER NOT NULL,
                longest_pause_ms INTEGER NOT NULL,
                speech_rate_cpm INTEGER,
                filler_occurrences_json TEXT NOT NULL DEFAULT '[]',
                reflection_text TEXT NOT NULL DEFAULT '',
                focus_kind VARCHAR,
                origin_snapshot_id INTEGER REFERENCES voice_coaching_snapshots_new(id) ON DELETE SET NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_voice_coaching_snapshots_turn UNIQUE (turn_id),
                CONSTRAINT uq_voice_coaching_snapshots_key UNIQUE (idempotency_key),
                CONSTRAINT ck_voice_coaching_snapshot_context CHECK (
                    (context_kind = 'application_event' AND application_id IS NOT NULL AND event_id IS NOT NULL AND practice_case_id IS NULL)
                    OR (context_kind = 'quick_practice' AND application_id IS NULL AND event_id IS NULL AND practice_case_id IS NOT NULL)
                )
            )
            """
        )
        conn.exec_driver_sql(
            """
            INSERT INTO voice_coaching_snapshots_new (
                id, attempt_id, turn_id, context_kind, application_id, event_id,
                practice_case_id, idempotency_key, request_fingerprint_sha256,
                question_text_snapshot, confirmed_answer_text_snapshot, answer_sha256,
                measurement_source, total_duration_ms, voiced_duration_ms, pause_count,
                longest_pause_ms, speech_rate_cpm, filler_occurrences_json,
                reflection_text, focus_kind, origin_snapshot_id, created_at
            )
            SELECT id, attempt_id, turn_id, 'application_event', application_id, event_id,
                   NULL, idempotency_key, request_fingerprint_sha256,
                   question_text_snapshot, confirmed_answer_text_snapshot, answer_sha256,
                   measurement_source, total_duration_ms, voiced_duration_ms, pause_count,
                   longest_pause_ms, speech_rate_cpm, filler_occurrences_json,
                   reflection_text, focus_kind, origin_snapshot_id, created_at
            FROM voice_coaching_snapshots
            """
        )
        conn.exec_driver_sql("DROP TABLE voice_coaching_snapshots")
        conn.exec_driver_sql(
            "ALTER TABLE voice_coaching_snapshots_new RENAME TO voice_coaching_snapshots"
        )
        conn.exec_driver_sql(
            "CREATE INDEX idx_voice_coaching_snapshots_created "
            "ON voice_coaching_snapshots(created_at, id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX idx_voice_coaching_snapshots_application_event "
            "ON voice_coaching_snapshots(application_id, event_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX idx_voice_coaching_snapshots_attempt "
            "ON voice_coaching_snapshots(attempt_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX idx_voice_coaching_snapshots_context "
            "ON voice_coaching_snapshots(context_kind, practice_case_id)"
        )
        conn.commit()
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")


def _ensure_column(engine, table: str, column: str, definition: str) -> bool:  # type: ignore[no-untyped-def]
    with engine.begin() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        if any(row[1] == column for row in rows):
            return False
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))
        return True


def _ensure_interview_review_history_schema(engine) -> bool:  # type: ignore[no-untyped-def]
    """Make review proposals survive note deletion without rewriting their payloads."""
    with engine.connect() as conn:
        columns = conn.execute(text("PRAGMA table_info(interview_review_proposals)")).fetchall()
        foreign_keys = conn.execute(
            text("PRAGMA foreign_key_list(interview_review_proposals)")
        ).fetchall()
    note_column = next((row for row in columns if row[1] == "note_id"), None)
    note_foreign_key = next((row for row in foreign_keys if row[3] == "note_id"), None)
    if note_column is None or (
        note_column[3] == 0
        and note_foreign_key is not None
        and str(note_foreign_key[6]).upper() == "SET NULL"
    ):
        return False

    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        conn.execute(
            text(
                "ALTER TABLE interview_review_proposals RENAME TO interview_review_proposals_legacy"
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE interview_review_proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    note_id INTEGER REFERENCES interview_notes(id) ON DELETE SET NULL,
                    application_event_id INTEGER REFERENCES application_events(id) ON DELETE SET NULL,
                    idempotency_key VARCHAR NOT NULL,
                    input_snapshot_json VARCHAR NOT NULL,
                    source_fingerprint VARCHAR NOT NULL,
                    proposal_json VARCHAR NOT NULL,
                    proposal_hash VARCHAR NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_interview_review_proposals_note_key
                        UNIQUE (note_id, idempotency_key)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO interview_review_proposals (
                    id, note_id, application_event_id, idempotency_key,
                    input_snapshot_json, source_fingerprint, proposal_json,
                    proposal_hash, created_at
                )
                SELECT id, note_id, application_event_id, idempotency_key,
                       input_snapshot_json, source_fingerprint, proposal_json,
                       proposal_hash, created_at
                FROM interview_review_proposals_legacy
                """
            )
        )
        conn.execute(text("DROP TABLE interview_review_proposals_legacy"))
        conn.execute(
            text(
                "CREATE INDEX idx_interview_review_proposals_note ON interview_review_proposals(note_id)"
            )
        )
        conn.commit()
        conn.execute(text("PRAGMA foreign_keys=ON"))
    return True


def _prepare_event_bound_mock_interview_migration(engine) -> bool:  # type: ignore[no-untyped-def]
    """Remove the destructive legacy MockSession path before creating new tables."""

    with engine.begin() as conn:
        already_migrated = conn.execute(
            text(
                "SELECT 1 FROM schema_migrations WHERE version = '0016_event_bound_mock_interview'"
            )
        ).first()
        if already_migrated is not None:
            return False

        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        if "mock_sessions" not in tables:
            return True

        conn.execute(text("PRAGMA foreign_keys=OFF"))
        conversation_ids = []
        if "conversations" in tables:
            conversation_ids = [
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT ms.conversation_id FROM mock_sessions ms "
                        "JOIN conversations c ON c.id = ms.conversation_id "
                        "WHERE c.mode = 'mock_interview'"
                    )
                ).fetchall()
            ]
        if conversation_ids and "chat_messages" in tables:
            placeholders = ", ".join(
                f":conversation_{index}" for index in range(len(conversation_ids))
            )
            params = {
                f"conversation_{index}": value for index, value in enumerate(conversation_ids)
            }
            conn.execute(
                text(f"DELETE FROM chat_messages WHERE conversation_id IN ({placeholders})"),
                params,
            )
        if conversation_ids and "conversations" in tables:
            placeholders = ", ".join(
                f":conversation_{index}" for index in range(len(conversation_ids))
            )
            params = {
                f"conversation_{index}": value for index, value in enumerate(conversation_ids)
            }
            conn.execute(
                text(
                    f"DELETE FROM conversations WHERE id IN ({placeholders}) AND mode = 'mock_interview'"
                ),
                params,
            )
        conn.execute(text("DROP INDEX IF EXISTS idx_mock_sessions_conv"))
        conn.execute(text("DROP INDEX IF EXISTS idx_mock_sessions_status"))
        conn.execute(text("DROP TABLE IF EXISTS mock_sessions"))
        conn.execute(text("PRAGMA foreign_keys=ON"))
        return True
    return True


def _backfill_resume_v01(engine) -> bool:  # type: ignore[no-untyped-def]
    changed = False
    with engine.begin() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        if "resumes" not in tables:
            return False
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(resumes)")).fetchall()}
        required = {
            "id",
            "name",
            "file_path",
            "parsed_data",
            "title",
            "is_master",
            "source_file_path",
            "content_json",
            "deleted_at",
        }
        if not required.issubset(columns):
            return False

        result = conn.execute(
            text(
                """
                UPDATE resumes
                SET title = name
                WHERE deleted_at IS NULL
                  AND (title IS NULL OR trim(title) = '')
                  AND name IS NOT NULL
                  AND trim(name) != ''
                """
            )
        )
        changed = changed or bool(result.rowcount)

        result = conn.execute(
            text(
                """
                UPDATE resumes
                SET source_file_path = file_path
                WHERE deleted_at IS NULL
                  AND (source_file_path IS NULL OR trim(source_file_path) = '')
                  AND file_path IS NOT NULL
                  AND trim(file_path) != ''
                """
            )
        )
        changed = changed or bool(result.rowcount)

        rows = conn.execute(
            text(
                """
                SELECT id, parsed_data, content_json
                FROM resumes
                WHERE deleted_at IS NULL
                  AND parsed_data IS NOT NULL
                  AND trim(parsed_data) != ''
                """
            )
        ).fetchall()
        for resume_id, parsed_data, content_json in rows:
            if str(content_json or "").strip() not in {"", "{}"}:
                continue
            conn.execute(
                text("UPDATE resumes SET content_json = :content_json WHERE id = :id"),
                {
                    "id": resume_id,
                    "content_json": json.dumps(
                        {"raw_text": parsed_data},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            )
            changed = True

        master_rows = conn.execute(
            text(
                """
                SELECT id
                FROM resumes
                WHERE deleted_at IS NULL
                  AND is_master = 1
                ORDER BY id ASC
                """
            )
        ).fetchall()
        if not master_rows:
            first_active = conn.execute(
                text(
                    """
                    SELECT id
                    FROM resumes
                    WHERE deleted_at IS NULL
                    ORDER BY id ASC
                    LIMIT 1
                    """
                )
            ).fetchone()
            if first_active is not None:
                conn.execute(
                    text("UPDATE resumes SET is_master = 1 WHERE id = :id"),
                    {"id": first_active[0]},
                )
                changed = True
        elif len(master_rows) > 1:
            keep_id = master_rows[0][0]
            result = conn.execute(
                text(
                    """
                    UPDATE resumes
                    SET is_master = 0
                    WHERE deleted_at IS NULL
                      AND is_master = 1
                      AND id != :keep_id
                    """
                ),
                {"keep_id": keep_id},
            )
            changed = changed or bool(result.rowcount)
    return changed


def _backfill_application_lifecycle(engine) -> bool:  # type: ignore[no-untyped-def]
    changed = False
    field_by_status = {
        "pending": "first_pending_at",
        "applied": "first_applied_at",
        "written_test": "first_written_test_at",
        "interview": "first_interview_at",
        "offer": "first_offer_at",
        "closed": "closed_at",
    }
    with engine.begin() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        if "applications" not in tables:
            return False
        columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(applications)")).fetchall()
        }
        required = {
            "status",
            "applied_at",
            "created_at",
            "updated_at",
            "deleted_at",
            *field_by_status.values(),
        }
        if not required.issubset(columns):
            return False

        for status, field in field_by_status.items():
            result = conn.execute(
                text(
                    f"""
                    UPDATE applications
                    SET {field} = COALESCE(updated_at, applied_at, created_at, CURRENT_TIMESTAMP)
                    WHERE deleted_at IS NULL
                      AND status = :status
                      AND {field} IS NULL
                    """
                ),
                {"status": status},
            )
            changed = changed or bool(result.rowcount)
    return changed
