from sqlalchemy import inspect, text

from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.db import init_database
from offerpilot.repositories.chat import ChatRepository


def test_confirmation_claim_column_and_migration_are_durable(tmp_path) -> None:
    session_factory = init_database(tmp_path / "data.db")
    engine = session_factory.kw["bind"]

    columns = {column["name"] for column in inspect(engine).get_columns("conversations")}
    with engine.connect() as connection:
        migration_count = connection.execute(
            text(
                "SELECT COUNT(*) FROM schema_migrations "
                "WHERE version = '0025_pending_confirmation_claim'"
            )
        ).scalar_one()

    assert {"pending_confirmation_claim_id", "pending_confirmation_claimed_at"} <= columns
    assert migration_count == 1


def test_confirmation_claim_migration_upgrades_0024_database_idempotently(tmp_path) -> None:
    db_path = tmp_path / "data.db"
    original_factory = init_database(db_path)
    repo = ChatRepository(original_factory)
    conversation = repo.create_conversation("existing")
    pending = PendingAction("write-1", "update_application_status", '{"id":1}', "update")
    assert repo.set_pending_action(conversation.id, pending) is True
    original_engine = original_factory.kw["bind"]
    with original_engine.begin() as connection:
        connection.execute(
            text("DELETE FROM schema_migrations WHERE version = '0025_pending_confirmation_claim'")
        )
        connection.execute(text("ALTER TABLE conversations DROP COLUMN pending_confirmation_claimed_at"))
        connection.execute(text("ALTER TABLE conversations DROP COLUMN pending_confirmation_claim_id"))
    original_engine.dispose()

    upgraded_factory = init_database(db_path)
    upgraded_engine = upgraded_factory.kw["bind"]
    upgraded = ChatRepository(upgraded_factory).get_conversation(conversation.id)
    assert upgraded is not None
    assert upgraded.pending_tool_call_id == pending.tool_call_id
    assert upgraded.pending_confirmation_claim_id == ""
    assert upgraded.pending_confirmation_claimed_at is None
    assert {
        "pending_confirmation_claim_id",
        "pending_confirmation_claimed_at",
    } <= {column["name"] for column in inspect(upgraded_engine).get_columns("conversations")}

    upgraded_engine.dispose()
    repeated_factory = init_database(db_path)
    repeated_engine = repeated_factory.kw["bind"]
    with repeated_engine.connect() as connection:
        migration_count = connection.execute(
            text(
                "SELECT COUNT(*) FROM schema_migrations "
                "WHERE version = '0025_pending_confirmation_claim'"
            )
        ).scalar_one()
    assert migration_count == 1
    repeated_engine.dispose()
