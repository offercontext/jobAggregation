from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text

import offerpilot.db as database
from offerpilot.db import init_database
from offerpilot.models import (
    APPLICATION_FOREIGN_KEY_MODELS,
    AdaptivePracticePlan,
    InterviewNote,
    InterviewReadinessSignal,
    InterviewReadinessSignalEvidence,
    InterviewReadinessSignalVersion,
    InterviewReviewProposal,
    InterviewStoryProposalAttempt,
    ProductActionProposal,
    WriteOperation,
)


HMAC_A = "hmac-sha256:" + "a" * 64
HMAC_B = "hmac-sha256:" + "b" * 64
HMAC_C = "hmac-sha256:" + "c" * 64
HMAC_D = "hmac-sha256:" + "d" * 64
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
UUID_KEY = "10000000-0000-4000-8000-000000000001"


def _dispose(factory) -> None:  # type: ignore[no-untyped-def]
    factory.kw["bind"].dispose()


@pytest.fixture
def migrated_db(tmp_path: Path) -> Iterator[tuple[Path, sqlite3.Connection]]:
    db_path = tmp_path / "review-readiness-0029.db"
    factory = init_database(db_path)
    _dispose(factory)
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield db_path, connection
    finally:
        connection.close()


def _table_columns(connection: sqlite3.Connection, table: str) -> dict[str, tuple[object, ...]]:
    return {str(row[1]): row for row in connection.execute(f"PRAGMA table_info({table})")}


def _uuid(seed: int) -> str:
    return f"00000000-0000-4000-8000-{seed:012d}"


def _insert_application_graph(
    connection: sqlite3.Connection,
    *,
    app_seed: int = 1,
) -> tuple[int, int, int, int]:
    cursor = connection.execute(
        "INSERT INTO applications(company_name,position_name) VALUES (?,?)",
        (f"Company {app_seed}", "Engineer"),
    )
    application_id = int(cursor.lastrowid)
    cursor = connection.execute(
        "INSERT INTO application_events(application_id,event_type,status) "
        "VALUES (?,'interview','completed')",
        (application_id,),
    )
    event_id = int(cursor.lastrowid)
    cursor = connection.execute(
        "INSERT INTO interview_notes(application_id,application_event_id,company,position) "
        "VALUES (?,?,?,?)",
        (application_id, event_id, f"Company {app_seed}", "Engineer"),
    )
    note_id = int(cursor.lastrowid)
    cursor = connection.execute(
        "INSERT INTO interview_review_proposals("
        "note_id,application_event_id,idempotency_key,input_snapshot_json,"
        "source_fingerprint,proposal_json,proposal_hash,proposal_schema_version,"
        "source_note_revision) VALUES (?,?,?,'{}',?,'{}',?,2,1)",
        (note_id, event_id, f"proposal-{app_seed}", SHA_A, SHA_B),
    )
    return application_id, event_id, note_id, int(cursor.lastrowid)


def _insert_product_primary(
    connection: sqlite3.Connection,
    *,
    operation_id: str,
    action_call_id: str,
    action_name: str,
) -> None:
    connection.execute(
        """
        INSERT INTO write_operations(
          id,operation_role,parent_operation_id,parent_terminal_payload_sha256,
          conversation_id,agent_run_id,tool_call_id,tool_name,adapter_kind,status,
          fingerprint_key_id,proposal_fingerprint,input_fingerprint,
          confirmation_token_fingerprint,authorization_scope_fingerprint,
          operation_request_fingerprint,result_contract,result_json,visible_result,
          transport_json,undo_json,terminal_payload_sha256,failure_category,failure_code,
          delivery_status,delivery_failure_code,delivery_outcome,delivery_message_count,
          delivery_manifest_sha256,delivery_next_operation_id,delivery_generation,
          delivery_owner_token_fingerprint,delivery_lease_expires_at,approved_at,claimed_at,
          rejected_at,committed_at,failed_at,delivered_at
        ) VALUES (
          ?,'primary',NULL,NULL,NULL,NULL,?,?,'product_action','proposed',
          ?,?,NULL,?,?,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,
          'pending',NULL,NULL,NULL,NULL,NULL,0,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL
        )
        """,
        (operation_id, action_call_id, action_name, UUID_KEY, HMAC_A, HMAC_B, HMAC_C),
    )


def _insert_product_route(
    connection: sqlite3.Connection,
    *,
    operation_id: str,
    action_call_id: str,
    action_name: str,
    source_kind: str,
    request_origin: str = "current",
    semantic_claim: str | None = None,
    historical_request: str | None = None,
    route_payload: str | None = "{}",
    terminalized_at: str | None = None,
) -> None:
    request_fingerprint = HMAC_C[:-12] + operation_id[-12:]
    connection.execute(
        """
        INSERT INTO product_action_proposals(
          operation_id,action_call_id,action_name,request_origin,schema_version,
          source_kind,source_id,source_revision,route_payload_json,
          route_payload_fingerprint,route_binding_fingerprint,
          request_idempotency_fingerprint,semantic_claim_fingerprint,
          historical_request_token_fingerprint,created_at,terminalized_at
        ) VALUES (?,?,?,?,1,?,1,1,?,?,?,?,?,?,CURRENT_TIMESTAMP,?)
        """,
        (
            operation_id,
            action_call_id,
            action_name,
            request_origin,
            source_kind,
            route_payload,
            HMAC_A,
            HMAC_B,
            request_fingerprint,
            semantic_claim,
            historical_request,
            terminalized_at,
        ),
    )


def _transition(connection: sqlite3.Connection, operation_id: str, seq: int, state: str) -> None:
    connection.execute(
        "INSERT INTO write_operation_transitions(id,operation_id,seq,state) VALUES (?,?,?,?)",
        (_uuid(900000000 + int(operation_id[-6:]) * 10 + seq), operation_id, seq, state),
    )


def _commit_product_primary(
    connection: sqlite3.Connection,
    *,
    operation_id: str,
) -> None:
    _transition(connection, operation_id, 2, "approved")
    _transition(connection, operation_id, 3, "claimed")
    connection.execute(
        """
        UPDATE write_operations SET
          status='committed',input_fingerprint=?,operation_request_fingerprint=?,
          result_contract='product_action_json_v1',result_json='{}',visible_result='saved',
          transport_json='{}',undo_json='{}',terminal_payload_sha256=?,
          delivery_status='not_applicable',delivery_outcome='none',delivery_message_count=0,
          approved_at=CURRENT_TIMESTAMP,claimed_at=CURRENT_TIMESTAMP,
          committed_at=CURRENT_TIMESTAMP,delivered_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (HMAC_D, HMAC_C, SHA_C, operation_id),
    )
    _transition(connection, operation_id, 4, "committed")


def _create_committed_product_operation(
    connection: sqlite3.Connection,
    *,
    seed: int,
    action_name: str = "save_review_readiness_signal",
) -> str:
    operation_id = _uuid(seed)
    action_call_id = _uuid(seed + 1000)
    _insert_product_primary(
        connection,
        operation_id=operation_id,
        action_call_id=action_call_id,
        action_name=action_name,
    )
    _insert_product_route(
        connection,
        operation_id=operation_id,
        action_call_id=action_call_id,
        action_name=action_name,
        source_kind=("review_focus" if action_name == "save_review_readiness_signal" else "story_proposal"),
        semantic_claim=(HMAC_D if action_name == "save_review_readiness_signal" else None),
    )
    _transition(connection, operation_id, 1, "proposed")
    _commit_product_primary(connection, operation_id=operation_id)
    return operation_id


def _create_signal_version(
    connection: sqlite3.Connection,
    *,
    app_seed: int,
    operation_seed: int,
) -> tuple[int, int, int, int]:
    application_id, event_id, note_id, proposal_id = _insert_application_graph(
        connection,
        app_seed=app_seed,
    )
    operation_id = _create_committed_product_operation(connection, seed=operation_seed)
    cursor = connection.execute(
        """
        INSERT INTO interview_readiness_signals(
          application_id,source_event_id,source_note_id,source_proposal_id,focus_id,
          current_version_id,revision
        ) VALUES (?,?,?,?,?,NULL,1)
        """,
        (application_id, event_id, note_id, proposal_id, f"focus-{app_seed}"),
    )
    signal_id = int(cursor.lastrowid)
    cursor = connection.execute(
        """
        INSERT INTO interview_readiness_signal_versions(
          signal_id,version_number,parent_version_id,disposition,schema_version,
          statement_text,user_note,source_note_revision,source_note_fingerprint,
          source_proposal_hash,candidate_fingerprint,domain_idempotency_key,
          write_operation_id
        ) VALUES (?,1,NULL,'active','readiness-signal-v1','Focus','',1,?,?,?,?,?)
        """,
        (signal_id, SHA_A, SHA_B, SHA_C, _uuid(operation_seed + 2000), operation_id),
    )
    version_id = int(cursor.lastrowid)
    connection.execute(
        "INSERT INTO interview_readiness_signal_evidence("
        "signal_version_id,ordinal,source_path,excerpt,excerpt_sha256,source_field_sha256"
        ") VALUES (?,0,'/questions','evidence',?,?)",
        (version_id, SHA_A, SHA_B),
    )
    connection.execute(
        "UPDATE interview_readiness_signals SET current_version_id=? WHERE id=?",
        (version_id, signal_id),
    )
    return application_id, event_id, signal_id, version_id


def _v2_plan_values(
    *,
    application_id: int,
    version_id: int,
    target_event_id: int,
    seed: int,
) -> tuple[object, ...]:
    return (
        application_id,
        target_event_id,
        1,
        1,
        f"focus-{seed}",
        f"start-{seed}",
        SHA_A,
        SHA_B,
        "/questions",
        "excerpt",
        SHA_C,
        "behavioral",
        "title",
        "observation",
        "reason",
        "prompt",
        "confirmed_readiness_signal_v1",
        version_id,
        target_event_id,
        SHA_C,
    )


V2_PLAN_INSERT = """
INSERT INTO adaptive_practice_plans(
  application_id,application_event_id,interview_note_id,interview_review_proposal_id,
  focus_id,start_idempotency_key,start_input_fingerprint,source_fingerprint,
  source_path,source_excerpt,source_hash,drill_kind,title,observation,reason,prompt,
  origin_contract,readiness_signal_version_id,target_application_event_id,target_fingerprint
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def test_0029_fresh_schema_has_exact_columns_defaults_models_and_marker(
    migrated_db: tuple[Path, sqlite3.Connection],
) -> None:
    _path, connection = migrated_db
    note = _table_columns(connection, "interview_notes")
    proposal = _table_columns(connection, "interview_review_proposals")
    story = _table_columns(connection, "interview_story_proposal_attempts")
    practice = _table_columns(connection, "adaptive_practice_plans")

    assert note["content_revision"][3:] == (1, "1", 0)
    assert note["updated_at"][3] == 1
    assert proposal["proposal_schema_version"][3:] == (1, "1", 0)
    assert proposal["source_note_revision"][3] == 0
    assert story["product_action_operation_id"][3] == 0
    assert story["product_action_generation"][3:] == (1, "0", 0)
    assert practice["origin_contract"][3:] == (1, "'legacy_review_focus_v1'", 0)
    assert practice["readiness_signal_version_id"][3] == 0
    assert practice["target_application_event_id"][3] == 0
    assert practice["target_fingerprint"][3] == 0
    assert connection.execute(
        "SELECT count(*) FROM schema_migrations "
        "WHERE version='0029_review_to_readiness_feedback'"
    ).fetchone() == (1,)

    assert ProductActionProposal.__table__.name == "product_action_proposals"
    assert InterviewReadinessSignal.__table__.name == "interview_readiness_signals"
    assert InterviewReadinessSignalVersion.__table__.name == "interview_readiness_signal_versions"
    assert InterviewReadinessSignalEvidence.__table__.name == "interview_readiness_signal_evidence"
    assert InterviewReadinessSignal in APPLICATION_FOREIGN_KEY_MODELS
    assert {
        InterviewNote,
        InterviewReviewProposal,
        InterviewStoryProposalAttempt,
        AdaptivePracticePlan,
        WriteOperation,
    }


def test_0029_is_repeatable_and_database_integrity_is_clean(tmp_path: Path) -> None:
    db_path = tmp_path / "repeat.db"
    first = init_database(db_path)
    _dispose(first)
    second = init_database(db_path)
    _dispose(second)
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT count(*) FROM schema_migrations "
            "WHERE version='0029_review_to_readiness_feedback'"
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    ("action_name", "source_kind", "origin", "semantic", "historical"),
    [
        ("confirm_interview_story", "review_focus", "current", None, None),
        ("save_review_readiness_signal", "story_proposal", "current", HMAC_D, None),
        ("save_review_readiness_signal", "review_focus", "historical_story_bridge", HMAC_D, HMAC_A),
        ("confirm_interview_story", "story_proposal", "current", HMAC_D, None),
        ("confirm_interview_story", "story_proposal", "current", None, HMAC_A),
        ("confirm_interview_story", "story_proposal", "historical_story_bridge", None, None),
    ],
)
def test_product_action_route_rejects_invalid_mapping_and_conditional_fingerprints(
    migrated_db: tuple[Path, sqlite3.Connection],
    action_name: str,
    source_kind: str,
    origin: str,
    semantic: str | None,
    historical: str | None,
) -> None:
    _path, connection = migrated_db
    operation_id = _uuid(100)
    action_call_id = _uuid(101)
    _insert_product_primary(
        connection,
        operation_id=operation_id,
        action_call_id=action_call_id,
        action_name=action_name,
    )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_product_route(
            connection,
            operation_id=operation_id,
            action_call_id=action_call_id,
            action_name=action_name,
            source_kind=source_kind,
            request_origin=origin,
            semantic_claim=semantic,
            historical_request=historical,
        )


@pytest.mark.parametrize(
    ("action_name", "source_kind", "origin", "semantic", "historical", "seed"),
    [
        (
            "confirm_interview_story",
            "story_proposal",
            "current",
            None,
            None,
            102,
        ),
        (
            "confirm_interview_story",
            "story_proposal",
            "historical_story_bridge",
            None,
            HMAC_D,
            104,
        ),
        (
            "save_review_readiness_signal",
            "review_focus",
            "current",
            HMAC_D,
            None,
            106,
        ),
    ],
)
def test_product_action_route_accepts_exact_current_and_historical_shapes(
    migrated_db: tuple[Path, sqlite3.Connection],
    action_name: str,
    source_kind: str,
    origin: str,
    semantic: str | None,
    historical: str | None,
    seed: int,
) -> None:
    _path, connection = migrated_db
    operation_id = _uuid(seed)
    action_call_id = _uuid(seed + 1)
    _insert_product_primary(
        connection,
        operation_id=operation_id,
        action_call_id=action_call_id,
        action_name=action_name,
    )
    _insert_product_route(
        connection,
        operation_id=operation_id,
        action_call_id=action_call_id,
        action_name=action_name,
        source_kind=source_kind,
        request_origin=origin,
        semantic_claim=semantic,
        historical_request=historical,
    )
    assert connection.execute(
        "SELECT request_origin,action_name,source_kind FROM product_action_proposals "
        "WHERE operation_id=?",
        (operation_id,),
    ).fetchone() == (origin, action_name, source_kind)


@pytest.mark.parametrize("schema_version", [0, 2, 1.5, "not-one"])
def test_product_action_route_requires_integer_one_not_bool_coercions(
    migrated_db: tuple[Path, sqlite3.Connection], schema_version: object
) -> None:
    _path, connection = migrated_db
    operation_id = _uuid(110)
    action_call_id = _uuid(111)
    _insert_product_primary(
        connection,
        operation_id=operation_id,
        action_call_id=action_call_id,
        action_name="confirm_interview_story",
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO product_action_proposals(
              operation_id,action_call_id,action_name,request_origin,schema_version,
              source_kind,source_id,source_revision,route_payload_json,
              route_payload_fingerprint,route_binding_fingerprint,
              request_idempotency_fingerprint,created_at
            ) VALUES (?,?,?,'current',?,'story_proposal',1,1,'{}',?,?,?,CURRENT_TIMESTAMP)
            """,
            (
                operation_id,
                action_call_id,
                "confirm_interview_story",
                schema_version,
                HMAC_A,
                HMAC_B,
                HMAC_C,
            ),
        )


def test_product_action_route_bytes_parent_identity_active_terminal_and_no_delete(
    migrated_db: tuple[Path, sqlite3.Connection],
) -> None:
    _path, connection = migrated_db
    with pytest.raises(sqlite3.IntegrityError):
        _insert_product_route(
            connection,
            operation_id=_uuid(120),
            action_call_id=_uuid(121),
            action_name="confirm_interview_story",
            source_kind="story_proposal",
        )

    operation_id = _uuid(122)
    action_call_id = _uuid(123)
    _insert_product_primary(
        connection,
        operation_id=operation_id,
        action_call_id=action_call_id,
        action_name="confirm_interview_story",
    )
    _insert_product_route(
        connection,
        operation_id=operation_id,
        action_call_id=action_call_id,
        action_name="confirm_interview_story",
        source_kind="story_proposal",
        route_payload='{"x":"' + ("界" * 5458) + 'ab"}',
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE product_action_proposals SET route_binding_fingerprint=? WHERE operation_id=?",
            (HMAC_D, operation_id),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE product_action_proposals SET route_payload_json=NULL,terminalized_at=CURRENT_TIMESTAMP "
            "WHERE operation_id=?",
            (operation_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "DELETE FROM product_action_proposals WHERE operation_id=?",
            (operation_id,),
        )

    too_large_operation = _uuid(124)
    too_large_call = _uuid(125)
    _insert_product_primary(
        connection,
        operation_id=too_large_operation,
        action_call_id=too_large_call,
        action_name="confirm_interview_story",
    )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_product_route(
            connection,
            operation_id=too_large_operation,
            action_call_id=too_large_call,
            action_name="confirm_interview_story",
            source_kind="story_proposal",
            route_payload='{"x":"' + ("界" * 5458) + 'abc"}',
        )


@pytest.mark.parametrize(
    ("action_name", "compensation_name"),
    [
        ("save_review_readiness_signal", "undo:confirm_interview_story"),
        ("confirm_interview_story", "undo:save_review_readiness_signal"),
        ("save_review_readiness_signal", "undo:add_note"),
        ("confirm_interview_story", "undo:create_application"),
    ],
)
def test_product_compensation_rejects_every_cross_pair(
    migrated_db: tuple[Path, sqlite3.Connection],
    action_name: str,
    compensation_name: str,
) -> None:
    _path, connection = migrated_db
    parent_id = _create_committed_product_operation(
        connection,
        seed=200,
        action_name=action_name,
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO write_operations(
              id,operation_role,parent_operation_id,parent_terminal_payload_sha256,
              conversation_id,agent_run_id,tool_call_id,tool_name,adapter_kind,status,
              fingerprint_key_id,proposal_fingerprint,input_fingerprint,
              confirmation_token_fingerprint,authorization_scope_fingerprint,
              operation_request_fingerprint,delivery_status,delivery_generation
            ) VALUES (?,'compensation',?,?,NULL,NULL,NULL,?,'compensation','proposed',
              ?,NULL,NULL,NULL,NULL,?,'pending',0)
            """,
            (_uuid(201), parent_id, SHA_C, compensation_name, UUID_KEY, HMAC_A),
        )


@pytest.mark.parametrize(
    ("action_name", "compensation_name", "seed"),
    [
        (
            "confirm_interview_story",
            "undo:confirm_interview_story",
            220,
        ),
        (
            "save_review_readiness_signal",
            "undo:save_review_readiness_signal",
            230,
        ),
    ],
)
def test_product_compensation_exact_pairs_accept_compensation_json_terminal(
    migrated_db: tuple[Path, sqlite3.Connection],
    action_name: str,
    compensation_name: str,
    seed: int,
) -> None:
    _path, connection = migrated_db
    parent_id = _create_committed_product_operation(
        connection,
        seed=seed,
        action_name=action_name,
    )
    compensation_id = _uuid(seed + 1)
    connection.execute(
        """
        INSERT INTO write_operations(
          id,operation_role,parent_operation_id,parent_terminal_payload_sha256,
          conversation_id,agent_run_id,tool_call_id,tool_name,adapter_kind,status,
          fingerprint_key_id,proposal_fingerprint,input_fingerprint,
          confirmation_token_fingerprint,authorization_scope_fingerprint,
          operation_request_fingerprint,delivery_status,delivery_generation
        ) VALUES (?,'compensation',?,?,NULL,NULL,NULL,?,'compensation','proposed',
          ?,NULL,NULL,NULL,NULL,?,'pending',0)
        """,
        (compensation_id, parent_id, SHA_C, compensation_name, UUID_KEY, HMAC_A),
    )
    _transition(connection, compensation_id, 1, "proposed")
    _transition(connection, compensation_id, 2, "approved")
    _transition(connection, compensation_id, 3, "claimed")
    connection.execute(
        """
        UPDATE write_operations SET
          status='committed',input_fingerprint=?,result_contract='compensation_json_v1',
          result_json='{}',visible_result='undone',transport_json='{}',
          terminal_payload_sha256=?,delivery_status='not_applicable',
          delivery_outcome='none',delivery_message_count=0,
          approved_at=CURRENT_TIMESTAMP,claimed_at=CURRENT_TIMESTAMP,
          committed_at=CURRENT_TIMESTAMP,delivered_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (HMAC_B, SHA_A, compensation_id),
    )
    _transition(connection, compensation_id, 4, "committed")
    assert connection.execute(
        "SELECT operation_role,adapter_kind,tool_name,result_contract,delivery_status "
        "FROM write_operations WHERE id=?",
        (compensation_id,),
    ).fetchone() == (
        "compensation",
        "compensation",
        compensation_name,
        "compensation_json_v1",
        "not_applicable",
    )


@pytest.mark.parametrize(
    ("operation_role", "adapter_kind", "tool_name"),
    [
        ("primary", "compensation", "confirm_interview_story"),
        ("compensation", "product_action", "undo:confirm_interview_story"),
        ("primary", "product_action", "undo:confirm_interview_story"),
        ("compensation", "compensation", "confirm_interview_story"),
    ],
)
def test_product_primary_and_compensation_manifests_are_mutually_exclusive(
    migrated_db: tuple[Path, sqlite3.Connection],
    operation_role: str,
    adapter_kind: str,
    tool_name: str,
) -> None:
    _path, connection = migrated_db
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO write_operations(
              id,operation_role,parent_operation_id,parent_terminal_payload_sha256,
              conversation_id,agent_run_id,tool_call_id,tool_name,adapter_kind,status,
              fingerprint_key_id,proposal_fingerprint,confirmation_token_fingerprint,
              authorization_scope_fingerprint,operation_request_fingerprint,
              delivery_status,delivery_generation
            ) VALUES (?,?,NULL,NULL,NULL,NULL,?,?,?,?,?,?,?,?,?,'pending',0)
            """,
            (
                _uuid(250),
                operation_role,
                _uuid(251) if operation_role == "primary" else None,
                tool_name,
                adapter_kind,
                "proposed",
                UUID_KEY,
                HMAC_A if operation_role == "primary" else None,
                HMAC_B if operation_role == "primary" else None,
                HMAC_C if operation_role == "primary" else None,
                HMAC_D if operation_role == "compensation" else None,
            ),
        )


def test_write_operation_manifest_rejects_unknown_product_actions(
    migrated_db: tuple[Path, sqlite3.Connection],
) -> None:
    _path, connection = migrated_db
    with pytest.raises(sqlite3.IntegrityError):
        _insert_product_primary(
            connection,
            operation_id=_uuid(260),
            action_call_id=_uuid(261),
            action_name="unknown_product_action",
        )


def test_parent_terminal_transition_clears_route_in_same_statement(
    migrated_db: tuple[Path, sqlite3.Connection],
) -> None:
    _path, connection = migrated_db
    operation_id = _uuid(300)
    action_call_id = _uuid(301)
    _insert_product_primary(
        connection,
        operation_id=operation_id,
        action_call_id=action_call_id,
        action_name="save_review_readiness_signal",
    )
    _insert_product_route(
        connection,
        operation_id=operation_id,
        action_call_id=action_call_id,
        action_name="save_review_readiness_signal",
        source_kind="review_focus",
        semantic_claim=HMAC_D,
    )
    _transition(connection, operation_id, 1, "proposed")
    _commit_product_primary(connection, operation_id=operation_id)
    assert connection.execute(
        "SELECT route_payload_json,terminalized_at FROM product_action_proposals "
        "WHERE operation_id=?",
        (operation_id,),
    ).fetchone()[0] is None


def test_signal_semantic_claim_is_unique_only_while_route_is_active(
    migrated_db: tuple[Path, sqlite3.Connection],
) -> None:
    _path, connection = migrated_db
    first_operation = _uuid(304)
    first_call = _uuid(305)
    _insert_product_primary(
        connection,
        operation_id=first_operation,
        action_call_id=first_call,
        action_name="save_review_readiness_signal",
    )
    _insert_product_route(
        connection,
        operation_id=first_operation,
        action_call_id=first_call,
        action_name="save_review_readiness_signal",
        source_kind="review_focus",
        semantic_claim=HMAC_D,
    )
    second_operation = _uuid(306)
    second_call = _uuid(307)
    _insert_product_primary(
        connection,
        operation_id=second_operation,
        action_call_id=second_call,
        action_name="save_review_readiness_signal",
    )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_product_route(
            connection,
            operation_id=second_operation,
            action_call_id=second_call,
            action_name="save_review_readiness_signal",
            source_kind="review_focus",
            semantic_claim=HMAC_D,
        )
    _transition(connection, first_operation, 1, "proposed")
    _commit_product_primary(connection, operation_id=first_operation)
    _insert_product_route(
        connection,
        operation_id=second_operation,
        action_call_id=second_call,
        action_name="save_review_readiness_signal",
        source_kind="review_focus",
        semantic_claim=HMAC_D,
    )


def test_product_action_parent_terminal_without_exact_active_route_is_rejected(
    migrated_db: tuple[Path, sqlite3.Connection],
) -> None:
    _path, connection = migrated_db
    operation_id = _uuid(310)
    action_call_id = _uuid(311)
    _insert_product_primary(
        connection,
        operation_id=operation_id,
        action_call_id=action_call_id,
        action_name="confirm_interview_story",
    )
    _transition(connection, operation_id, 1, "proposed")
    with pytest.raises(sqlite3.IntegrityError):
        _commit_product_primary(connection, operation_id=operation_id)


@pytest.mark.parametrize("terminal_status", ["rejected", "failed"])
def test_product_action_rejected_and_failed_terminals_clear_the_route(
    migrated_db: tuple[Path, sqlite3.Connection],
    terminal_status: str,
) -> None:
    _path, connection = migrated_db
    operation_id = _uuid(320 if terminal_status == "rejected" else 330)
    action_call_id = _uuid(321 if terminal_status == "rejected" else 331)
    _insert_product_primary(
        connection,
        operation_id=operation_id,
        action_call_id=action_call_id,
        action_name="confirm_interview_story",
    )
    _insert_product_route(
        connection,
        operation_id=operation_id,
        action_call_id=action_call_id,
        action_name="confirm_interview_story",
        source_kind="story_proposal",
    )
    _transition(connection, operation_id, 1, "proposed")
    if terminal_status == "rejected":
        connection.execute(
            """
            UPDATE write_operations SET status='rejected',operation_request_fingerprint=?,
              result_contract='rejection_json_v1',result_json='{}',visible_result='cancelled',
              transport_json='{}',terminal_payload_sha256=?,delivery_status='not_applicable',
              delivery_outcome='none',delivery_message_count=0,
              rejected_at=CURRENT_TIMESTAMP,delivered_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (HMAC_D, SHA_A, operation_id),
        )
        _transition(connection, operation_id, 2, "rejected")
    else:
        _transition(connection, operation_id, 2, "approved")
        _transition(connection, operation_id, 3, "claimed")
        connection.execute(
            """
            UPDATE write_operations SET status='failed',input_fingerprint=?,
              operation_request_fingerprint=?,result_contract='product_action_json_v1',
              result_json='{}',visible_result='failed',transport_json='{}',
              terminal_payload_sha256=?,failure_category='conflict',failure_code='story_conflict',
              delivery_status='not_applicable',delivery_outcome='none',delivery_message_count=0,
              approved_at=CURRENT_TIMESTAMP,claimed_at=CURRENT_TIMESTAMP,
              failed_at=CURRENT_TIMESTAMP,delivered_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (HMAC_B, HMAC_D, SHA_A, operation_id),
        )
        _transition(connection, operation_id, 4, "failed")
    assert connection.execute(
        "SELECT route_payload_json,terminalized_at FROM product_action_proposals "
        "WHERE operation_id=?",
        (operation_id,),
    ).fetchone()[0] is None


def test_signal_composite_parent_fk_delete_order_and_direct_delete_guard(
    migrated_db: tuple[Path, sqlite3.Connection],
) -> None:
    _path, connection = migrated_db
    app1, _event1, signal1, version1 = _create_signal_version(
        connection,
        app_seed=1,
        operation_seed=400,
    )
    retraction_operation_id = _create_committed_product_operation(connection, seed=405)
    retracted_cursor = connection.execute(
        """
        INSERT INTO interview_readiness_signal_versions(
          signal_id,version_number,parent_version_id,disposition,schema_version,
          statement_text,user_note,source_note_revision,source_note_fingerprint,
          source_proposal_hash,candidate_fingerprint,domain_idempotency_key,
          write_operation_id
        ) VALUES (?,2,?,'retracted','readiness-signal-v1','Focus','',1,?,?,?,?,?)
        """,
        (
            signal1,
            version1,
            SHA_A,
            SHA_B,
            SHA_C,
            _uuid(2405),
            retraction_operation_id,
        ),
    )
    retracted_version_id = int(retracted_cursor.lastrowid)
    connection.execute(
        "INSERT INTO interview_readiness_signal_evidence("
        "signal_version_id,ordinal,source_path,excerpt,excerpt_sha256,source_field_sha256"
        ") VALUES (?,0,'/questions','evidence',?,?)",
        (retracted_version_id, SHA_A, SHA_B),
    )
    connection.execute(
        "UPDATE interview_readiness_signals "
        "SET current_version_id=?,revision=2 WHERE id=?",
        (retracted_version_id, signal1),
    )
    _app2, _event2, signal2, _version2 = _create_signal_version(
        connection,
        app_seed=2,
        operation_seed=410,
    )
    operation_id = _create_committed_product_operation(connection, seed=420)
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO interview_readiness_signal_versions(
              signal_id,version_number,parent_version_id,disposition,schema_version,
              statement_text,user_note,source_note_revision,source_note_fingerprint,
              source_proposal_hash,candidate_fingerprint,domain_idempotency_key,
              write_operation_id
            ) VALUES (?,2,?,'retracted','readiness-signal-v1','Focus','',1,?,?,?,?,?)
            """,
            (signal2, version1, SHA_A, SHA_B, SHA_C, _uuid(2420), operation_id),
        )
        connection.commit()
    connection.rollback()

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "DELETE FROM interview_readiness_signal_versions WHERE id=?",
            (version1,),
        )

    connection.execute("DELETE FROM applications WHERE id=?", (app1,))
    connection.commit()
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute(
        "SELECT count(*) FROM interview_readiness_signals WHERE id=?", (signal1,)
    ).fetchone() == (0,)


@pytest.mark.parametrize(
    "source_column",
    ["source_event_id", "source_note_id", "source_proposal_id"],
)
def test_signal_source_locators_only_degrade_non_null_to_null(
    migrated_db: tuple[Path, sqlite3.Connection],
    source_column: str,
) -> None:
    _path, connection = migrated_db
    _app, _event, signal_id, _version = _create_signal_version(
        connection,
        app_seed=3,
        operation_seed=430,
    )
    original = connection.execute(
        f"SELECT {source_column} FROM interview_readiness_signals WHERE id=?",
        (signal_id,),
    ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            f"UPDATE interview_readiness_signals SET {source_column}={source_column}+1 "
            "WHERE id=?",
            (signal_id,),
        )
    connection.execute(
        f"UPDATE interview_readiness_signals SET {source_column}=NULL WHERE id=?",
        (signal_id,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            f"UPDATE interview_readiness_signals SET {source_column}=? WHERE id=?",
            (original, signal_id),
        )


def test_adaptive_v2_truth_partial_uniques_fingerprints_and_locator_history(
    migrated_db: tuple[Path, sqlite3.Connection],
) -> None:
    _path, connection = migrated_db
    source_app, _source_event, _signal, version_id = _create_signal_version(
        connection,
        app_seed=4,
        operation_seed=440,
    )
    target_app, target_event, _note, _proposal = _insert_application_graph(
        connection,
        app_seed=5,
    )
    values = _v2_plan_values(
        application_id=target_app,
        version_id=version_id,
        target_event_id=target_event,
        seed=1,
    )
    connection.execute(V2_PLAN_INSERT, values)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(V2_PLAN_INSERT, (*values[:5], "different-key", *values[6:]))
    second_target_event = int(
        connection.execute(
            "INSERT INTO application_events(application_id,event_type,status) "
            "VALUES (?,'interview','scheduled')",
            (target_app,),
        ).lastrowid
    )
    second_target_values = list(
        _v2_plan_values(
            application_id=target_app,
            version_id=version_id,
            target_event_id=second_target_event,
            seed=2,
        )
    )
    second_target_values[4] = values[4]
    connection.execute(V2_PLAN_INSERT, tuple(second_target_values))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE adaptive_practice_plans SET target_fingerprint=? WHERE start_idempotency_key='start-1'",
            (SHA_A,),
        )
    connection.execute(
        "DELETE FROM application_events WHERE id=?",
        (second_target_event,),
    )
    assert connection.execute(
        "SELECT readiness_signal_version_id,target_application_event_id,"
        "source_fingerprint,target_fingerprint FROM adaptive_practice_plans "
        "WHERE start_idempotency_key='start-2'"
    ).fetchone() == (version_id, None, SHA_B, SHA_C)
    connection.execute("DELETE FROM applications WHERE id=?", (source_app,))
    row = connection.execute(
        "SELECT origin_contract,readiness_signal_version_id,target_application_event_id,"
        "source_fingerprint,target_fingerprint FROM adaptive_practice_plans "
        "WHERE start_idempotency_key='start-1'"
    ).fetchone()
    assert row == ("confirmed_readiness_signal_v1", None, target_event, SHA_B, SHA_C)
    assert connection.execute(
        "SELECT readiness_signal_version_id,target_application_event_id,"
        "source_fingerprint,target_fingerprint FROM adaptive_practice_plans "
        "WHERE start_idempotency_key='start-2'"
    ).fetchone() == (None, None, SHA_B, SHA_C)
    connection.execute("DELETE FROM application_events WHERE id=?", (target_event,))
    row = connection.execute(
        "SELECT readiness_signal_version_id,target_application_event_id,"
        "source_fingerprint,target_fingerprint FROM adaptive_practice_plans "
        "WHERE start_idempotency_key='start-1'"
    ).fetchone()
    assert row == (None, None, SHA_B, SHA_C)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            V2_PLAN_INSERT,
            _v2_plan_values(
                application_id=target_app,
                version_id=version_id,
                target_event_id=target_event,
                seed=3,
            ),
        )


@pytest.mark.parametrize(
    ("origin", "version_id", "target_id", "target_fingerprint"),
    [
        ("legacy_review_focus_v1", 1, None, None),
        ("legacy_review_focus_v1", None, 1, None),
        ("legacy_review_focus_v1", None, None, SHA_A),
        ("confirmed_readiness_signal_v1", None, None, None),
        ("confirmed_readiness_signal_v1", 1, 1, "bad"),
        ("unknown", None, None, None),
    ],
)
def test_adaptive_origin_truth_table_rejects_invalid_shapes(
    migrated_db: tuple[Path, sqlite3.Connection],
    origin: str,
    version_id: int | None,
    target_id: int | None,
    target_fingerprint: str | None,
) -> None:
    _path, connection = migrated_db
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO adaptive_practice_plans(
              application_id,application_event_id,interview_note_id,
              interview_review_proposal_id,focus_id,start_idempotency_key,
              start_input_fingerprint,source_fingerprint,source_path,source_excerpt,
              source_hash,drill_kind,title,observation,reason,prompt,origin_contract,
              readiness_signal_version_id,target_application_event_id,target_fingerprint
            ) VALUES (1,1,1,1,'f',? ,?,?,'/questions','e',?,'d','t','o','r','p',?,?,?,?)
            """,
            (
                _uuid(500),
                SHA_A,
                SHA_B,
                SHA_C,
                origin,
                version_id,
                target_id,
                target_fingerprint,
            ),
        )


def test_migration_preserves_every_write_operation_and_transition_column(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "history.db"
    factory = init_database(db_path)
    engine = factory.kw["bind"]
    operation_id = _uuid(600)
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM schema_migrations WHERE version='0029_review_to_readiness_feedback'")
        )
        connection.execute(
            text("INSERT INTO conversations(id,title) VALUES (1,'history')")
        )
        connection.execute(
            text(
                """
                INSERT INTO write_operations(
                  id,operation_role,conversation_id,agent_run_id,tool_call_id,tool_name,
                  adapter_kind,status,fingerprint_key_id,proposal_fingerprint,
                  confirmation_token_fingerprint,authorization_scope_fingerprint,
                  delivery_status,delivery_generation,created_at,updated_at
                ) VALUES (:id,'primary',1,NULL,'call','add_note','typed','proposed',
                  :key,:proposal,:confirmation,:scope,'pending',0,
                  '2026-08-29 01:02:03.000001','2026-08-29 01:02:04.000002')
                """
            ),
            {
                "id": operation_id,
                "key": UUID_KEY,
                "proposal": HMAC_A,
                "confirmation": HMAC_B,
                "scope": HMAC_C,
            },
        )
        connection.execute(
            text(
                "INSERT INTO write_operation_transitions(id,operation_id,seq,state,created_at) "
                "VALUES (:id,:operation_id,1,'proposed','2026-08-29 01:02:05.000003')"
            ),
            {"id": _uuid(601), "operation_id": operation_id},
        )
        op_columns = [
            str(row[1])
            for row in connection.execute(text("PRAGMA table_info(write_operations)"))
        ]
        before_operation = tuple(
            connection.execute(
                text("SELECT " + ",".join(f'\"{name}\"' for name in op_columns) + " FROM write_operations WHERE id=:id"),
                {"id": operation_id},
            ).one()
        )
        transition_columns = [
            str(row[1])
            for row in connection.execute(text("PRAGMA table_info(write_operation_transitions)"))
        ]
        before_transition = tuple(
            connection.execute(
                text("SELECT " + ",".join(f'\"{name}\"' for name in transition_columns) + " FROM write_operation_transitions WHERE operation_id=:id"),
                {"id": operation_id},
            ).one()
        )

    database._ensure_review_to_readiness_feedback_schema(engine, force_rebuild=True)

    with engine.connect() as connection:
        after_operation = tuple(
            connection.execute(
                text("SELECT " + ",".join(f'\"{name}\"' for name in op_columns) + " FROM write_operations WHERE id=:id"),
                {"id": operation_id},
            ).one()
        )
        after_transition = tuple(
            connection.execute(
                text("SELECT " + ",".join(f'\"{name}\"' for name in transition_columns) + " FROM write_operation_transitions WHERE operation_id=:id"),
                {"id": operation_id},
            ).one()
        )
    _dispose(factory)

    assert after_operation == before_operation
    assert after_transition == before_transition


def test_0029_preserves_domain_history_and_replaces_ordinary_practice_unique(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "domain-history.db"
    factory = init_database(db_path)
    engine = factory.kw["bind"]
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM schema_migrations WHERE version='0029_review_to_readiness_feedback'")
        )
        connection.execute(
            text(
                "INSERT INTO applications(id,company_name,position_name) "
                "VALUES (1,'History','Engineer')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO application_events(id,application_id,event_type,status) "
                "VALUES (1,1,'interview','completed')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO interview_notes("
                "id,application_id,application_event_id,company,position,questions,"
                "content_revision,created_at,updated_at) "
                "VALUES (1,1,1,'历史公司','历史岗位','原始正文',1,"
                "'2026-08-29 02:03:04.000001','2026-08-29 02:03:05.000002')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO interview_review_proposals("
                "id,note_id,application_event_id,idempotency_key,input_snapshot_json,"
                "source_fingerprint,proposal_json,proposal_hash,proposal_schema_version,"
                "source_note_revision,created_at) VALUES (1,1,1,'legacy-proposal','{}',"
                ":source,'{}',:proposal,1,NULL,'2026-08-29 02:03:06.000003')"
            ),
            {"source": SHA_A, "proposal": SHA_B},
        )
        connection.execute(
            text(
                "INSERT INTO interview_story_proposal_attempts("
                "id,idempotency_key,entrypoint,attempt_status,input_snapshot_json,"
                "source_fingerprint,product_action_operation_id,product_action_generation) "
                "VALUES (1,'legacy-story','ui','ready','{}',:source,NULL,0)"
            ),
            {"source": SHA_A},
        )
        connection.execute(
            text(
                "INSERT INTO adaptive_practice_plans("
                "id,application_id,application_event_id,interview_note_id,"
                "interview_review_proposal_id,focus_id,start_idempotency_key,"
                "start_input_fingerprint,source_fingerprint,source_path,source_excerpt,"
                "source_hash,drill_kind,title,observation,reason,prompt,origin_contract) "
                "VALUES (1,1,1,1,1,'legacy-focus','legacy-start',:start,:source,"
                "'/questions','原始证据',:hash,'behavioral','标题','观察','原因','提示',"
                "'legacy_review_focus_v1')"
            ),
            {"start": SHA_A, "source": SHA_B, "hash": SHA_C},
        )
        connection.execute(text("DROP INDEX uq_adaptive_practice_legacy_proposal_focus"))
        connection.execute(
            text(
                "CREATE UNIQUE INDEX uq_adaptive_practice_proposal_focus "
                "ON adaptive_practice_plans(interview_review_proposal_id,focus_id)"
            )
        )
        before = {
            "note": tuple(
                connection.execute(
                    text(
                        "SELECT company,position,questions,content_revision,created_at,updated_at "
                        "FROM interview_notes WHERE id=1"
                    )
                ).one()
            ),
            "proposal": tuple(
                connection.execute(
                    text(
                        "SELECT proposal_schema_version,source_note_revision,proposal_json,"
                        "proposal_hash,created_at FROM interview_review_proposals WHERE id=1"
                    )
                ).one()
            ),
            "story": tuple(
                connection.execute(
                    text(
                        "SELECT attempt_status,product_action_operation_id,"
                        "product_action_generation FROM interview_story_proposal_attempts WHERE id=1"
                    )
                ).one()
            ),
            "practice": tuple(
                connection.execute(
                    text(
                        "SELECT origin_contract,readiness_signal_version_id,"
                        "target_application_event_id,target_fingerprint,source_excerpt "
                        "FROM adaptive_practice_plans WHERE id=1"
                    )
                ).one()
            ),
        }

    database._ensure_review_to_readiness_feedback_schema(engine, force_rebuild=True)

    with engine.connect() as connection:
        after = {
            "note": tuple(
                connection.execute(
                    text(
                        "SELECT company,position,questions,content_revision,created_at,updated_at "
                        "FROM interview_notes WHERE id=1"
                    )
                ).one()
            ),
            "proposal": tuple(
                connection.execute(
                    text(
                        "SELECT proposal_schema_version,source_note_revision,proposal_json,"
                        "proposal_hash,created_at FROM interview_review_proposals WHERE id=1"
                    )
                ).one()
            ),
            "story": tuple(
                connection.execute(
                    text(
                        "SELECT attempt_status,product_action_operation_id,"
                        "product_action_generation FROM interview_story_proposal_attempts WHERE id=1"
                    )
                ).one()
            ),
            "practice": tuple(
                connection.execute(
                    text(
                        "SELECT origin_contract,readiness_signal_version_id,"
                        "target_application_event_id,target_fingerprint,source_excerpt "
                        "FROM adaptive_practice_plans WHERE id=1"
                    )
                ).one()
            ),
        }
        indexes = {
            str(row[1])
            for row in connection.execute(text("PRAGMA index_list(adaptive_practice_plans)"))
        }
    _dispose(factory)

    assert after == before
    assert "uq_adaptive_practice_proposal_focus" not in indexes
    assert {
        "uq_adaptive_practice_legacy_proposal_focus",
        "uq_adaptive_practice_signal_target",
    } <= indexes


def test_migration_rolls_back_rebuild_and_marker_when_swap_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "rollback.db"
    factory = init_database(db_path)
    engine = factory.kw["bind"]
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM schema_migrations WHERE version='0029_review_to_readiness_feedback'")
        )

    def fail_swap(checkpoint: str) -> None:
        if checkpoint == "before_adaptive_swap":
            raise RuntimeError("injected 0029 swap failure")

    monkeypatch.setattr(database, "_review_to_readiness_migration_checkpoint", fail_swap)
    with pytest.raises(RuntimeError, match="injected 0029 swap failure"):
        database._ensure_review_to_readiness_feedback_schema(engine, force_rebuild=True)

    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM schema_migrations WHERE version='0029_review_to_readiness_feedback'")
        ).scalar_one() == 0
        assert connection.execute(
            text("SELECT count(*) FROM sqlite_master WHERE type='table' AND name LIKE '%_0029'")
        ).scalar_one() == 0
        assert connection.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
    _dispose(factory)
