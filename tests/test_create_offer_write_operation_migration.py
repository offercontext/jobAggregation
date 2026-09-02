from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Barrier, BrokenBarrierError
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

import offerpilot.db as database
from offerpilot.db import init_database
from offerpilot.models import Base
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


def test_concurrent_init_database_converges_create_offer_migration_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "concurrent-pre-0030-create-offer.db"
    _create_fixed_pre_0029_database(db_path)

    pre_0030_engine = create_engine(f"sqlite:///{db_path}")
    try:
        Base.metadata.create_all(pre_0030_engine)
        database._ensure_review_to_readiness_feedback_schema(pre_0030_engine)
    finally:
        pre_0030_engine.dispose()

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        operation_columns = [
            str(row[1]) for row in connection.execute("PRAGMA table_info(write_operations)")
        ]
        operation_before = connection.execute(
            "SELECT "
            + ",".join(f'"{column}"' for column in operation_columns)
            + " FROM write_operations ORDER BY id"
        ).fetchall()
        transition_columns = [
            str(row[1])
            for row in connection.execute("PRAGMA table_info(write_operation_transitions)")
        ]
        transition_before = connection.execute(
            "SELECT "
            + ",".join(f'"{column}"' for column in transition_columns)
            + " FROM write_operation_transitions ORDER BY operation_id,seq,id"
        ).fetchall()

    migration_barrier = Barrier(2)
    original_migration = database._ensure_create_offer_write_operation_schema

    def skip_already_applied_0029(engine: Engine) -> None:
        del engine

    monkeypatch.setattr(
        database,
        "_ensure_review_to_readiness_feedback_schema",
        skip_already_applied_0029,
    )

    def synchronized_migration(engine: Engine) -> None:
        try:
            migration_barrier.wait(timeout=30)
        except BrokenBarrierError as exc:
            raise AssertionError("both init_database calls must reach 0030") from exc
        original_migration(engine)

    monkeypatch.setattr(
        database,
        "_ensure_create_offer_write_operation_schema",
        synchronized_migration,
    )

    def initialize() -> None:
        factory = init_database(db_path)
        _dispose(factory)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="init-database") as executor:
        futures = [executor.submit(initialize) for _ in range(2)]
        for future in futures:
            future.result(timeout=60)

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
