from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import text

from offerpilot.db import init_database


STORY_TABLES = {
    "interview_stories",
    "interview_story_versions",
    "interview_story_version_evidence_links",
    "interview_story_user_assertions",
    "interview_story_proposal_attempts",
}


def _dispose(factory) -> None:  # type: ignore[no-untyped-def]
    factory.kw["bind"].dispose()


def _table_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


def test_fresh_database_creates_story_phase_one_tables_and_records_0019(tmp_path: Path) -> None:
    db_path = tmp_path / "story.db"

    factory = init_database(db_path)
    with factory() as session:
        migrations = set(
            session.execute(text("SELECT version FROM schema_migrations")).scalars()
        )
    _dispose(factory)

    tables = _table_names(db_path)

    assert STORY_TABLES <= tables
    assert "0019_interview_story_library" in migrations
    assert "interview_story_usage" not in tables


def test_story_attempt_schema_persists_the_bounded_provider_repair_count(tmp_path: Path) -> None:
    """The CDP egress audit must not infer an internal repair from CONNECT count."""

    db_path = tmp_path / "story-repair-count.db"
    factory = init_database(db_path)
    with factory() as session:
        columns = {
            row[1]: row
            for row in session.execute(
                text("PRAGMA table_info(interview_story_proposal_attempts)")
            )
        }
    _dispose(factory)

    assert "repair_count" in columns
    assert columns["repair_count"][3] == 1
    assert str(columns["repair_count"][4]).strip("'") == "0"
    assert columns["product_action_operation_id"][3] == 0
    assert columns["product_action_generation"][3] == 1
    assert str(columns["product_action_generation"][4]).strip("'") == "0"


def test_story_schema_adds_repair_count_to_a_pre_audit_attempt_table(tmp_path: Path) -> None:
    """The additive 0019 schema update must upgrade isolated pre-audit data."""

    db_path = tmp_path / "pre-audit-story.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE interview_story_proposal_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_story_id INTEGER,
                idempotency_key TEXT NOT NULL,
                entrypoint TEXT NOT NULL,
                entry_context_json TEXT NOT NULL DEFAULT '{}',
                attempt_status TEXT NOT NULL,
                generation_revision INTEGER NOT NULL DEFAULT 1,
                provider_call_token TEXT NOT NULL DEFAULT '',
                provider_lease_until DATETIME,
                input_snapshot_json TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                proposal_json TEXT NOT NULL DEFAULT '',
                proposal_hash TEXT NOT NULL DEFAULT '',
                failure_category TEXT NOT NULL DEFAULT '',
                confirmation_token_hash TEXT NOT NULL DEFAULT '',
                confirmation_payload_hash TEXT NOT NULL DEFAULT '',
                confirmed_story_id INTEGER,
                confirmed_story_version_id INTEGER,
                confirmed_at DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO interview_story_proposal_attempts(
                idempotency_key, entrypoint, attempt_status, input_snapshot_json, source_fingerprint
            ) VALUES ('pre-audit-story-repair-01', 'ui', 'ready', '{}', 'source');
            """
        )
        connection.commit()

    factory = init_database(db_path)
    with factory() as session:
        row = session.execute(
            text(
                "SELECT repair_count,product_action_operation_id,product_action_generation "
                "FROM interview_story_proposal_attempts "
                "WHERE idempotency_key = 'pre-audit-story-repair-01'"
            )
        ).one()
    _dispose(factory)

    assert row == (0, None, 0)


def test_story_migration_coexists_with_jd_0018_marker_and_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "jd-then-story.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO schema_migrations(version, description)
            VALUES ('0018_application_jd_versions', 'application JD versions');
            CREATE TABLE application_jd_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL,
                version_number INTEGER NOT NULL,
                jd_text TEXT NOT NULL
            );
            """
        )
        connection.commit()

    factory = init_database(db_path)
    with factory() as session:
        migrations = set(
            session.execute(text("SELECT version FROM schema_migrations")).scalars()
        )
        jd_columns = {
            row[1]
            for row in session.execute(text("PRAGMA table_info(application_jd_versions)"))
        }
    _dispose(factory)

    assert STORY_TABLES <= _table_names(db_path)
    assert {"0018_application_jd_versions", "0019_interview_story_library"} <= migrations
    assert {"id", "application_id", "version_number", "jd_text"} <= jd_columns
