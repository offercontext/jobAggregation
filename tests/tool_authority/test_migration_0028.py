"""Red tests for the scoped-tool-authority additive migration."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from offerpilot.db import _ensure_scoped_tool_authority_schema, init_database
from offerpilot.repositories.chat import ChatRepository


def _engine(tmp_path: Path) -> tuple[sessionmaker[Session], Engine]:
    sessions = init_database(tmp_path / "offerpilot.db")
    return sessions, sessions.kw["bind"]


def test_fresh_database_has_0028_columns_and_records_migration(tmp_path: Path) -> None:
    sessions, engine = _engine(tmp_path)
    try:
        with engine.connect() as conn:
            conversation = conn.execute(text("PRAGMA table_info(conversations)")).mappings().all()
            operation = conn.execute(text("PRAGMA table_info(write_operations)")).mappings().all()
            migrations = conn.execute(
                text("SELECT version FROM schema_migrations WHERE version LIKE '0028_%'")
            ).scalars().all()
        scope = next(item for item in conversation if item["name"] == "scope_revision")
        auth = next(item for item in operation if item["name"] == "authorization_scope_fingerprint")
        assert scope["type"].upper() == "INTEGER"
        assert scope["notnull"] == 1
        assert scope["dflt_value"] == "0"
        assert auth["notnull"] == 0
        assert migrations == ["0028_scoped_tool_authority"]
    finally:
        engine.dispose()


def test_migration_is_repeatable_and_new_conversations_start_at_revision_zero(
    tmp_path: Path,
) -> None:
    sessions, engine = _engine(tmp_path)
    engine.dispose()
    sessions = init_database(tmp_path / "offerpilot.db")
    engine = sessions.kw["bind"]
    try:
        conversation = ChatRepository(sessions).create_conversation("new")
        assert conversation.scope_revision == 0
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT count(*) FROM schema_migrations WHERE version = '0028_scoped_tool_authority'")
            ).scalar_one() == 1
    finally:
        engine.dispose()


def test_scope_revision_trigger_requires_zero_on_insert_and_increments_raw_changes(
    tmp_path: Path,
) -> None:
    _sessions, engine = _engine(tmp_path)
    try:
        with pytest.raises(Exception, match="scope_revision"):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO conversations "
                        "(title,title_source,mode,context_type,context_ref,scope_revision) "
                        "VALUES ('bad','fallback','general','workspace','',1)"
                    )
                )
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO conversations "
                    "(title,title_source,mode,context_type,context_ref,scope_revision) "
                    "VALUES ('ok','fallback','general','workspace','',0)"
                )
            )
            conn.execute(text("UPDATE conversations SET title='changed' WHERE id=1"))
            assert conn.execute(text("SELECT scope_revision FROM conversations WHERE id=1")).scalar_one() == 0
            with pytest.raises(Exception, match="scope revision"):
                conn.execute(text("UPDATE conversations SET context_ref='x' WHERE id=1"))
            conn.execute(
                text(
                    "UPDATE conversations SET context_ref='x', scope_revision=1 WHERE id=1"
                )
            )
            assert conn.execute(text("SELECT scope_revision FROM conversations WHERE id=1")).scalar_one() == 1
            conn.execute(text("UPDATE conversations SET context_ref='x' WHERE id=1"))
            assert conn.execute(text("SELECT scope_revision FROM conversations WHERE id=1")).scalar_one() == 1
    finally:
        engine.dispose()


def test_backup_copy_can_be_reopened_after_engine_dispose(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    sessions = init_database(source)
    engine = sessions.kw["bind"]
    ChatRepository(sessions).create_conversation("backup")
    engine.dispose()
    restored = tmp_path / "restored.db"
    shutil.copy2(source, restored)
    restored_sessions = init_database(restored)
    restored_engine = restored_sessions.kw["bind"]
    try:
        with restored_engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM conversations")).scalar_one() == 1
            assert conn.execute(text("SELECT scope_revision FROM conversations")).scalar_one() == 0
    finally:
        restored_engine.dispose()


def _legacy_schema_engine() -> Engine:
    """Build the smallest pre-0028 schema needed to exercise additive guards.

    This intentionally does not use ``Base.metadata``: an upgrade test must
    prove that 0028 works against a real old table that has neither the new
    columns nor the new model checks.
    """

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE schema_migrations (
                version TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE conversations (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                mode TEXT,
                context_type TEXT,
                context_ref TEXT
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE write_operations (
                id TEXT PRIMARY KEY,
                operation_role TEXT NOT NULL,
                parent_operation_id TEXT,
                conversation_id INTEGER,
                tool_call_id TEXT,
                tool_name TEXT NOT NULL,
                adapter_kind TEXT NOT NULL,
                status TEXT NOT NULL,
                fingerprint_key_id TEXT NOT NULL,
                proposal_fingerprint TEXT,
                confirmation_token_fingerprint TEXT,
                delivery_status TEXT,
                delivery_generation INTEGER,
                delivery_outcome TEXT
            )
            """
        )
    return engine


def _legacy_operation(
    conn: Connection,
    *,
    operation_id: str,
    adapter_kind: str = "typed",
    operation_role: str = "primary",
    status: str = "proposed",
    authorization_scope_fingerprint: str | None = None,
    conversation_id: int | None = 1,
) -> None:
    values = {
        "id": operation_id,
        "operation_role": operation_role,
        "conversation_id": conversation_id,
        "tool_call_id": "call-" + operation_id[-4:] if operation_role == "primary" else None,
        "tool_name": (
            "update_application_status"
            if operation_role == "primary" and adapter_kind == "typed"
            else "save_application_jd_version"
            if operation_role == "primary"
            else "undo:add_note"
        ),
        "adapter_kind": adapter_kind,
        "status": status,
        "fingerprint_key_id": "00000000-0000-4000-8000-000000000001",
        "proposal_fingerprint": (
            "hmac-sha256:" + "a" * 64 if operation_role == "primary" else None
        ),
        "confirmation_token_fingerprint": (
            "hmac-sha256:" + "b" * 64 if operation_role == "primary" else None
        ),
    }
    columns = {
        str(row[1]) for row in conn.execute(text("PRAGMA table_info(write_operations)"))
    }
    if "authorization_scope_fingerprint" in columns:
        values["authorization_scope_fingerprint"] = authorization_scope_fingerprint
        auth_column = ", authorization_scope_fingerprint"
        auth_value = ", :authorization_scope_fingerprint"
    else:
        auth_column = ""
        auth_value = ""
    conn.execute(
        text(
            f"""
            INSERT INTO write_operations (
                id, operation_role, parent_operation_id, conversation_id,
                tool_call_id, tool_name, adapter_kind, status,
                fingerprint_key_id, proposal_fingerprint,
                confirmation_token_fingerprint, delivery_status,
                delivery_generation, delivery_outcome{auth_column}
            ) VALUES (
                :id, :operation_role, NULL, :conversation_id,
                :tool_call_id, :tool_name, :adapter_kind, :status,
                :fingerprint_key_id, :proposal_fingerprint,
                :confirmation_token_fingerprint, 'pending', 0, NULL{auth_value}
            )
            """
        ),
        values,
    )


def test_upgrade_normalizes_only_empty_modes_and_keeps_legacy_rows_usable() -> None:
    engine = _legacy_schema_engine()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO conversations(id,title,mode,context_type,context_ref) "
                    "VALUES (1,'null',NULL,'workspace',''),(2,'empty','', 'workspace',''),"
                    "(3,'unknown','custom-private','workspace','legacy'),"
                    "(4,'invalid','bad\x01mode','workspace','legacy')"
                )
            )
            _legacy_operation(conn, operation_id="00000000-0000-4000-8000-000000000001")
            _legacy_operation(
                conn,
                operation_id="00000000-0000-4000-8000-000000000002",
                adapter_kind="legacy_deterministic",
            )
            _legacy_operation(
                conn,
                operation_id="00000000-0000-4000-8000-000000000003",
                status="committed",
            )
            _legacy_operation(
                conn,
                operation_id="00000000-0000-4000-8000-000000000004",
                operation_role="compensation",
                adapter_kind="compensation",
            )
        _ensure_scoped_tool_authority_schema(engine)
        with engine.connect() as conn:
            assert [
                tuple(row)
                for row in conn.execute(
                    text("SELECT id,mode,scope_revision FROM conversations ORDER BY id")
                ).all()
            ] == [
                (1, "general", 0),
                (2, "general", 0),
                (3, "custom-private", 0),
                (4, "bad\x01mode", 0),
            ]
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE conversations SET title='renamed' WHERE id IN (3,4)")
            )
            # Legacy nullable authorization remains usable for delivery/replay
            # updates; only new Typed proposals are bound by 0028.
            conn.execute(
                text("UPDATE write_operations SET delivery_outcome='final_response' WHERE id=:id"),
                {"id": "00000000-0000-4000-8000-000000000002"},
            )
            conn.execute(
                text("UPDATE write_operations SET delivery_outcome='replay' WHERE id=:id"),
                {"id": "00000000-0000-4000-8000-000000000003"},
            )
            conn.execute(
                text("UPDATE write_operations SET delivery_outcome='none' WHERE id=:id"),
                {"id": "00000000-0000-4000-8000-000000000004"},
            )
    finally:
        engine.dispose()


def test_upgrade_status_and_authority_guards_are_fail_closed() -> None:
    engine = _legacy_schema_engine()
    hmac_fingerprint = "hmac-sha256:" + "c" * 64
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO conversations(id,title,mode,context_type,context_ref) "
                    "VALUES (1,'one','general','workspace',''),(2,'two','general','workspace','')"
                )
            )
            _legacy_operation(
                conn,
                operation_id="00000000-0000-4000-8000-000000000001",
            )
            _legacy_operation(
                conn,
                operation_id="00000000-0000-4000-8000-000000000003",
                adapter_kind="legacy_deterministic",
            )
        _ensure_scoped_tool_authority_schema(engine)

        with engine.begin() as conn:
            _legacy_operation(
                conn,
                operation_id="00000000-0000-4000-8000-000000000002",
                authorization_scope_fingerprint=hmac_fingerprint,
            )

        with pytest.raises(Exception, match="unbound typed operation"):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE write_operations SET status='committed' "
                        "WHERE id='00000000-0000-4000-8000-000000000001'"
                    )
                )
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE write_operations SET status='rejected' "
                    "WHERE id='00000000-0000-4000-8000-000000000001'"
                )
            )
            conn.execute(
                text(
                    "UPDATE write_operations SET status='committed' "
                    "WHERE id='00000000-0000-4000-8000-000000000002'"
                )
            )
        with pytest.raises(Exception, match="invalid authorization scope fingerprint"):
            with engine.begin() as conn:
                _legacy_operation(
                    conn,
                    operation_id="00000000-0000-4000-8000-000000000004",
                    authorization_scope_fingerprint="sha256:" + "d" * 64,
                )
        with pytest.raises(Exception, match="authorization scope fingerprint is immutable"):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE write_operations SET authorization_scope_fingerprint=:fp "
                        "WHERE id='00000000-0000-4000-8000-000000000002'"
                    ),
                    {"fp": "hmac-sha256:" + "e" * 64},
                )
        with pytest.raises(Exception, match="conversation binding is immutable"):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE write_operations SET conversation_id=2 "
                        "WHERE id='00000000-0000-4000-8000-000000000002'"
                    )
                )
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE write_operations SET conversation_id=NULL "
                    "WHERE id='00000000-0000-4000-8000-000000000002'"
                )
            )
        with pytest.raises(Exception, match="conversation binding is immutable"):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE write_operations SET conversation_id=2 "
                        "WHERE id='00000000-0000-4000-8000-000000000002'"
                    )
                )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("operation_role", "compensation"),
        ("adapter_kind", "legacy_deterministic"),
        ("tool_name", "create_application"),
        ("tool_call_id", "other-call"),
        ("fingerprint_key_id", "00000000-0000-4000-8000-000000000002"),
        ("proposal_fingerprint", "hmac-sha256:" + "d" * 64),
        ("confirmation_token_fingerprint", "hmac-sha256:" + "e" * 64),
        ("parent_operation_id", "00000000-0000-4000-8000-000000000099"),
    ],
)
def test_upgrade_freezes_every_authority_identity_column(
    column: str, value: str,
) -> None:
    engine = _legacy_schema_engine()
    operation_id = "00000000-0000-4000-8000-000000000001"
    try:
        with engine.begin() as conn:
            _legacy_operation(conn, operation_id=operation_id)
        _ensure_scoped_tool_authority_schema(engine)
        with pytest.raises(Exception, match="authority identity is immutable"):
            with engine.begin() as conn:
                conn.execute(
                    text(f"UPDATE write_operations SET {column}=:value WHERE id=:id"),
                    {"value": value, "id": operation_id},
                )
    finally:
        engine.dispose()


def test_scope_revision_trigger_rejects_invalid_insert_and_overflow(tmp_path: Path) -> None:
    _sessions, engine = _engine(tmp_path)
    try:
        for revision in (-1, 1, 9223372036854775807):
            with pytest.raises(Exception, match="scope revision"):
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            "INSERT INTO conversations "
                            "(title,title_source,mode,context_type,context_ref,scope_revision) "
                            "VALUES ('bad','fallback','general','workspace','',:revision)"
                        ),
                        {"revision": revision},
                    )

        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO conversations "
                    "(title,title_source,mode,context_type,context_ref,scope_revision) "
                    "VALUES ('max','fallback','general','workspace','',0)"
                )
            )
            # Reach the boundary only through a legacy repair fixture; normal
            # writes must never skip revisions to get there.
            conn.execute(text("DROP TRIGGER trg_conversations_scope_update"))
            conn.execute(text("DROP TRIGGER trg_conversations_scope_revision_unchanged"))
            conn.execute(
                text(
                    "UPDATE conversations SET context_ref='boundary', "
                    "scope_revision=9223372036854775807 WHERE title='max'"
                )
            )
        _ensure_scoped_tool_authority_schema(engine)
        with pytest.raises(Exception, match="scope revision overflow"):
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE conversations SET context_ref='changed' WHERE title='max'")
                )
    finally:
        engine.dispose()


def test_scope_trigger_compares_raw_values_and_mode_update_is_lexical(tmp_path: Path) -> None:
    _sessions, engine = _engine(tmp_path)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO conversations "
                    "(title,title_source,mode,context_type,context_ref,scope_revision) "
                    "VALUES ('scope','fallback','general','workspace','',0)"
                )
            )
            conn.execute(
                text("UPDATE conversations SET mode='GENERAL', scope_revision=1 WHERE title='scope'")
            )
            assert conn.execute(
                text("SELECT mode,scope_revision FROM conversations WHERE title='scope'")
            ).one() == ("GENERAL", 1)
        with pytest.raises(Exception, match="invalid conversation mode"):
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE conversations SET mode='\tbad' WHERE title='scope'")
                )
        with pytest.raises(Exception, match="scope revision changed"):
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE conversations SET scope_revision=2 WHERE title='scope'")
                )
        with pytest.raises(Exception, match="scope revision must increment"):
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE conversations SET context_ref='new' WHERE title='scope'")
                )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "mode",
    [
        "\x01invalid",
        "invalid\x1f",
        "invalid\x7f",
        "invalid\x80",
        "invalid\x9f",
        "\u00a0invalid",
        "invalid\u3000",
        "a" * 65,
        "é" * 129,
    ],
)
def test_mode_trigger_rejects_control_edge_whitespace_and_bounds(tmp_path: Path, mode: str) -> None:
    _sessions, engine = _engine(tmp_path)
    try:
        with pytest.raises(Exception, match="invalid conversation mode"):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO conversations "
                        "(title,title_source,mode,context_type,context_ref,scope_revision) "
                        "VALUES ('bad','fallback',:mode,'workspace','',0)"
                    ),
                    {"mode": mode},
                )
    finally:
        engine.dispose()
