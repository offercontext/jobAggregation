from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from offerpilot.db import init_database
from tests.test_review_to_readiness_migration_0029 import (
    SHA_C,
    _create_fixed_pre_0029_database,
    _insert_fixed_terminal_operation,
    _uuid,
)


def _dispose(factory: object) -> None:
    bind = getattr(factory, "kw")["bind"]
    bind.dispose()


def test_pre_0030_database_rebuilds_current_offer_checks_and_compensation_trigger(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "pre-0030-create-offer.db"
    _create_fixed_pre_0029_database(db_path)

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_fixed_terminal_operation(
                connection,
                operation_id=_uuid(301),
                operation_role="compensation",
                parent_operation_id=_uuid(300),
                parent_terminal_sha=SHA_C,
                tool_call_id=None,
                tool_name="undo:create_offer",
                adapter_kind="compensation",
                result_contract="compensation_json_v1",
                undo_json=None,
                delivery_outcome="none",
                transition_seed=30_100,
            )
        operation_columns = [
            str(row[1]) for row in connection.execute("PRAGMA table_info(write_operations)")
        ]
        transition_columns = [
            str(row[1])
            for row in connection.execute("PRAGMA table_info(write_operation_transitions)")
        ]
        operation_before = connection.execute(
            "SELECT "
            + ",".join(f'"{column}"' for column in operation_columns)
            + " FROM write_operations ORDER BY id"
        ).fetchall()
        transition_before = connection.execute(
            "SELECT "
            + ",".join(f'"{column}"' for column in transition_columns)
            + " FROM write_operation_transitions ORDER BY operation_id,seq,id"
        ).fetchall()

    factory = init_database(db_path)
    _dispose(factory)
    second_factory = init_database(db_path)
    _dispose(second_factory)

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        table_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='write_operations'"
            ).fetchone()[0]
        )
        trigger_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='trigger' AND name='trg_write_operation_compensation_insert'"
            ).fetchone()[0]
        )
        assert "'create_offer'" in table_sql
        assert "'undo:create_offer'" in table_sql
        assert "parent.tool_name = 'create_offer'" in trigger_sql
        assert "NEW.tool_name = 'undo:create_offer'" in trigger_sql
        assert connection.execute(
            "SELECT count(*) FROM schema_migrations "
            "WHERE version='0029_review_to_readiness_feedback'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM schema_migrations "
            "WHERE version='0030_create_offer_write_operation'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT "
            + ",".join(f'"{column}"' for column in operation_columns)
            + " FROM write_operations ORDER BY id"
        ).fetchall() == operation_before
        assert connection.execute(
            "SELECT "
            + ",".join(f'"{column}"' for column in transition_columns)
            + " FROM write_operation_transitions ORDER BY operation_id,seq,id"
        ).fetchall() == transition_before

        primary_id = _uuid(300)
        _insert_fixed_terminal_operation(
            connection,
            operation_id=primary_id,
            operation_role="primary",
            parent_operation_id=None,
            parent_terminal_sha=None,
            tool_call_id="create-offer-call",
            tool_name="create_offer",
            adapter_kind="typed",
            result_contract="typed_json_v1",
            undo_json="{}",
            delivery_outcome="final_response",
            transition_seed=30_000,
        )

        _insert_fixed_terminal_operation(
            connection,
            operation_id=_uuid(302),
            operation_role="compensation",
            parent_operation_id=primary_id,
            parent_terminal_sha=SHA_C,
            tool_call_id=None,
            tool_name="undo:create_offer",
            adapter_kind="compensation",
            result_contract="compensation_json_v1",
            undo_json=None,
            delivery_outcome="none",
            transition_seed=30_200,
        )
        connection.commit()

        assert connection.execute(
            "SELECT tool_name FROM write_operations WHERE id=?", (_uuid(302),)
        ).fetchone() == ("undo:create_offer",)
