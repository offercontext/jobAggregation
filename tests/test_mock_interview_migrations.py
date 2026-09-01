from pathlib import Path
import sqlite3

from sqlalchemy import text

from offerpilot.db import init_database


def _write_legacy_mock_database(path: Path, *, with_named_indexes: bool = True) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, description TEXT NOT NULL);
            CREATE TABLE applications (id INTEGER PRIMARY KEY, company_name TEXT NOT NULL, position_name TEXT NOT NULL);
            CREATE TABLE conversations (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'general'
            );
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE mock_sessions (
                id INTEGER PRIMARY KEY,
                conversation_id INTEGER NOT NULL,
                application_id INTEGER,
                title TEXT NOT NULL,
                role TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'in_progress'
            );
            INSERT INTO conversations(id, title, mode) VALUES
                (1, '旧模拟', 'mock_interview'),
                (2, '普通聊天', 'general');
            INSERT INTO chat_messages(id, conversation_id, role, content) VALUES
                (1, 1, 'user', '旧模拟消息'),
                (2, 2, 'user', '普通聊天消息');
            INSERT INTO mock_sessions(id, conversation_id, title, role)
                VALUES (1, 1, '旧会话', '工程师');
            """
        )
        if with_named_indexes:
            connection.executescript(
                """
                CREATE INDEX idx_mock_sessions_conv ON mock_sessions(conversation_id);
                CREATE INDEX idx_mock_sessions_status ON mock_sessions(status);
                """
            )
        connection.commit()
    finally:
        connection.close()


def _table_names(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        connection.close()


def test_0016_drops_mock_rows_and_messages_but_preserves_normal_chat(tmp_path):
    path = tmp_path / "legacy.db"
    _write_legacy_mock_database(path)

    factory = init_database(path)

    with factory() as session:
        assert "mock_sessions" not in _table_names(path)
        assert session.execute(
            text("SELECT COUNT(*) FROM chat_messages WHERE conversation_id = 1")
        ).scalar_one() == 0
        assert session.execute(
            text("SELECT content FROM chat_messages WHERE conversation_id = 2")
        ).scalar_one() == "普通聊天消息"
        assert session.execute(
            text("SELECT COUNT(*) FROM schema_migrations WHERE version = '0016_event_bound_mock_interview'")
        ).scalar_one() == 1


def test_0016_handles_legacy_named_indexes_before_dropping_mock_table(tmp_path):
    path = tmp_path / "legacy.db"
    _write_legacy_mock_database(path, with_named_indexes=True)

    init_database(path)

    assert "mock_sessions" not in _table_names(path)


def test_0016_creates_new_tables_and_is_idempotent(tmp_path):
    path = tmp_path / "legacy.db"
    _write_legacy_mock_database(path)

    init_database(path)
    init_database(path)

    tables = _table_names(path)
    assert {
        "mock_interview_attempts",
        "mock_interview_turns",
        "mock_interview_feedback_proposals",
        "mock_interview_review_drafts",
    } <= tables


def test_0016_preserves_formal_interview_notes_but_creates_no_legacy_mock_data(tmp_path):
    path = tmp_path / "legacy.db"
    _write_legacy_mock_database(path)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
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
            );
            INSERT INTO interview_notes(id, company, position)
            VALUES (1, '公司', '职位');
            """
        )
        connection.commit()
    finally:
        connection.close()

    factory = init_database(path)

    with factory() as session:
        assert session.execute(text("SELECT COUNT(*) FROM interview_notes WHERE id = 1")).scalar_one() == 1
        assert session.execute(text("SELECT COUNT(*) FROM mock_interview_attempts")).scalar_one() == 0


def test_0023_rebuilds_legacy_attempt_and_voice_rows_without_losing_history(tmp_path):
    path = tmp_path / "legacy-interview-studio.db"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE mock_interview_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL,
                event_id INTEGER NOT NULL,
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
                cancelled_at DATETIME
            );
            CREATE TABLE mock_interview_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER NOT NULL,
                turn_no INTEGER NOT NULL,
                question_idempotency_key VARCHAR NOT NULL,
                turn_idempotency_key VARCHAR NOT NULL DEFAULT '',
                question_text TEXT NOT NULL DEFAULT '',
                answer_text TEXT NOT NULL DEFAULT '',
                question_source_snapshot_json TEXT NOT NULL DEFAULT '{}',
                answer_sha256 VARCHAR NOT NULL DEFAULT '',
                turn_status VARCHAR NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE voice_coaching_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER NOT NULL,
                turn_id INTEGER NOT NULL,
                application_id INTEGER NOT NULL,
                event_id INTEGER NOT NULL,
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
                origin_snapshot_id INTEGER,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO mock_interview_attempts (
                id, application_id, event_id, resume_id, idempotency_key,
                input_snapshot_json, source_fingerprint, attempt_status,
                transcript_fingerprint
            ) VALUES (
                10, 7, 8, 9, 'legacy-attempt-key', '{}', 'legacy-source',
                'completed', 'legacy-transcript'
            );
            INSERT INTO mock_interview_turns (
                id, attempt_id, turn_no, question_idempotency_key,
                question_text, answer_text, turn_status
            ) VALUES (
                20, 10, 1, 'legacy-question-key', '旧问题', '旧回答', 'answered'
            );
            INSERT INTO voice_coaching_snapshots (
                id, attempt_id, turn_id, application_id, event_id,
                idempotency_key, request_fingerprint_sha256, question_text_snapshot,
                confirmed_answer_text_snapshot, answer_sha256, total_duration_ms,
                voiced_duration_ms, pause_count, longest_pause_ms
            ) VALUES (
                30, 10, 20, 7, 8, 'legacy-voice-key', 'legacy-voice-fingerprint',
                '旧问题', '旧回答', 'legacy-answer-sha', 1000, 800, 1, 200
            );
            """
        )
        connection.commit()
    finally:
        connection.close()

    factory = init_database(path)

    with factory() as session:
        attempt = session.execute(
            text(
                "SELECT context_kind, application_id, event_id, practice_case_id, "
                "idempotency_key, attempt_status FROM mock_interview_attempts WHERE id = 10"
            )
        ).one()
        assert tuple(attempt) == ("application_event", 7, 8, None, "legacy-attempt-key", "completed")
        snapshot = session.execute(
            text(
                "SELECT context_kind, application_id, event_id, practice_case_id, "
                "idempotency_key, confirmed_answer_text_snapshot "
                "FROM voice_coaching_snapshots WHERE id = 30"
            )
        ).one()
        assert tuple(snapshot) == ("application_event", 7, 8, None, "legacy-voice-key", "旧回答")
