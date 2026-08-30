import sqlite3

from sqlalchemy import text

from offerpilot.db import init_database


def test_interview_review_schema_is_created_and_idempotent(tmp_path):
    first = init_database(tmp_path / "data.db")
    first.kw["bind"].dispose()

    second = init_database(tmp_path / "data.db")
    with second() as session:
        note_columns = {
            row[1] for row in session.execute(text("PRAGMA table_info(interview_notes)"))
        }
        proposal_columns = {
            row[1]
            for row in session.execute(text("PRAGMA table_info(interview_review_proposals)"))
        }
        note_indexes = {
            row[1] for row in session.execute(text("PRAGMA index_list(interview_notes)"))
        }
        migrations = {
            row[0]
            for row in session.execute(
                text("SELECT version FROM schema_migrations")
            )
        }
    second.kw["bind"].dispose()

    assert "application_event_id" in note_columns
    assert {"content_revision", "updated_at"} <= note_columns
    assert {
        "id",
        "note_id",
        "application_event_id",
        "idempotency_key",
        "input_snapshot_json",
        "source_fingerprint",
        "proposal_json",
        "proposal_hash",
        "proposal_schema_version",
        "source_note_revision",
        "created_at",
    } <= proposal_columns
    assert "idx_notes_event" in note_indexes
    assert "uq_interview_notes_event_main" in note_indexes
    assert "0010_interview_review_proposals" in migrations


def test_interview_review_migration_adds_column_to_existing_legacy_notes_table(tmp_path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE interview_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER,
                company TEXT NOT NULL,
                position TEXT NOT NULL,
                round TEXT NOT NULL DEFAULT '',
                date TEXT NOT NULL DEFAULT '',
                questions TEXT NOT NULL DEFAULT '',
                self_reflection TEXT NOT NULL DEFAULT '',
                difficulty_points TEXT NOT NULL DEFAULT '',
                mood TEXT NOT NULL DEFAULT '',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            "INSERT INTO interview_notes (company, position) VALUES ('旧公司', '旧岗位')"
        )
        connection.commit()
    finally:
        connection.close()

    session_factory = init_database(db_path)
    with session_factory() as session:
        note = session.execute(
            text(
                "SELECT company, position, application_event_id, content_revision, updated_at "
                "FROM interview_notes WHERE id = 1"
            )
        ).one()
        note_indexes = {
            row[1] for row in session.execute(text("PRAGMA index_list(interview_notes)"))
        }
        migration = session.execute(
            text(
                "SELECT version FROM schema_migrations "
                "WHERE version = '0010_interview_review_proposals'"
            )
        ).scalar_one_or_none()
    session_factory.kw["bind"].dispose()

    assert note[:4] == ("旧公司", "旧岗位", None, 1)
    assert note.updated_at is not None
    assert "idx_notes_event" in note_indexes
    assert "uq_interview_notes_event_main" in note_indexes
    assert migration == "0010_interview_review_proposals"
