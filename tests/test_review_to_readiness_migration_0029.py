from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError as SQLAlchemyIntegrityError
from sqlalchemy.exc import StatementError
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import offerpilot.db as database
from offerpilot.db import init_database
from offerpilot.models import (
    APPLICATION_FOREIGN_KEY_MODELS,
    AdaptivePracticePlan,
    Base,
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
FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures" / "review_readiness"
PRE_0029_SCHEMA_SHA256 = "39eb6d1e8a5fc924460f2ac9e36e3e8f6c20e56f72bf77f364b89121c120b72d"


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


@pytest.fixture(scope="module")
def exact_integer_sqlalchemy_engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    db_path = tmp_path_factory.mktemp("exact-integer-bind") / "bind.db"
    factory = init_database(db_path)
    engine = factory.kw["bind"]
    try:
        yield engine
    finally:
        engine.dispose()


def _table_columns(connection: sqlite3.Connection, table: str) -> dict[str, tuple[object, ...]]:
    return {str(row[1]): row for row in connection.execute(f"PRAGMA table_info({table})")}


def _uuid(seed: int) -> str:
    return f"00000000-0000-4000-8000-{seed:012d}"


def _product_route_values(seed: int) -> dict[str, object]:
    return {
        "operation_id": _uuid(seed),
        "action_call_id": _uuid(seed + 1),
        "action_name": "confirm_interview_story",
        "request_origin": "current",
        "schema_version": 1,
        "source_kind": "story_proposal",
        "source_id": 1,
        "source_revision": 1,
        "route_payload_json": "{}",
        "route_payload_fingerprint": HMAC_A,
        "route_binding_fingerprint": HMAC_B,
        "request_idempotency_fingerprint": HMAC_C,
        "semantic_claim_fingerprint": None,
        "historical_request_token_fingerprint": None,
    }


def _create_fixed_pre_0029_database(path: Path) -> None:
    fixture_text = (FIXTURE_DIRECTORY / "pre_0029_0028_schema.sql").read_text(
        encoding="utf-8"
    )
    fixture_bytes = (FIXTURE_DIRECTORY / "pre_0029_0028_schema.sql").read_bytes()
    assert hashlib.sha256(fixture_bytes).hexdigest() == PRE_0029_SCHEMA_SHA256
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.executescript(fixture_text)
        connection.execute(
            "INSERT INTO applications(id,company_name,position_name,created_at,updated_at) "
            "VALUES (1,'历史公司','历史岗位','2026-08-29 01:00:00.000001',"
            "'2026-08-29 01:00:01.000002')"
        )
        connection.execute(
            "INSERT INTO application_events(id,application_id,event_type,status,created_at) "
            "VALUES (1,1,'interview','completed','2026-08-29 01:00:02.000003')"
        )
        connection.execute(
            "INSERT INTO interview_notes(id,application_id,application_event_id,company,"
            "position,questions,created_at) VALUES (1,1,1,'历史公司','历史岗位',"
            "'逐字正文\\u0000保留','2026-08-29 01:00:03.000004')"
        )
        connection.execute(
            "INSERT INTO interview_review_proposals(id,note_id,application_event_id,"
            "idempotency_key,input_snapshot_json,source_fingerprint,proposal_json,"
            "proposal_hash,created_at) VALUES (1,1,1,'legacy-proposal','{\"n\":1}',"
            "?,?,?,'2026-08-29 01:00:04.000005')",
            (SHA_A, '{"focuses":["历史"]}', SHA_B),
        )
        for story_id, story_status in enumerate(
            ("generating", "ready", "confirmed"),
            start=1,
        ):
            connection.execute(
                "INSERT INTO interview_story_proposal_attempts("
                "id,idempotency_key,entrypoint,attempt_status,input_snapshot_json,"
                "source_fingerprint,proposal_json,proposal_hash,created_at,updated_at) "
                "VALUES (?,?, 'ui',?,'{}',?,'{}',?,'2026-08-29 01:00:05.000006',"
                "'2026-08-29 01:00:06.000007')",
                (story_id, f"legacy-story-{story_id}", story_status, SHA_A, SHA_B),
            )
        for plan_id, status in enumerate(("in_progress", "completed"), start=1):
            connection.execute(
                "INSERT INTO adaptive_practice_plans("
                "id,application_id,application_event_id,interview_note_id,"
                "interview_review_proposal_id,focus_id,start_idempotency_key,"
                "start_input_fingerprint,source_fingerprint,source_path,source_excerpt,"
                "source_hash,drill_kind,title,observation,reason,prompt,status,revision,"
                "response_text,reflection_text,self_assessment,completion_idempotency_key,"
                "completion_fingerprint,completed_at,created_at,updated_at) VALUES ("
                "?,1,1,1,1,?,?,?,?,'/questions','逐字证据',?,'behavioral','标题',"
                "'观察','原因','提示',?,?,?,?,'ready',?,?,?,"
                "'2026-08-29 01:00:07.000008','2026-08-29 01:00:08.000009')",
                (
                    plan_id,
                    f"focus-{plan_id}",
                    f"legacy-start-{plan_id}",
                    SHA_A,
                    SHA_B,
                    SHA_C,
                    status,
                    2 if status == "completed" else 1,
                    "answer" if status == "completed" else "",
                    "reflection" if status == "completed" else "",
                    f"legacy-complete-{plan_id}" if status == "completed" else None,
                    SHA_A if status == "completed" else "",
                    "2026-08-29 01:00:09.000010" if status == "completed" else None,
                ),
            )

        typed_tools = (
            "create_application",
            "update_application_status",
            "create_application_event",
            "update_application_event",
            "delete_application_event",
            "add_note",
            "update_note",
            "delete_note",
            "update_offer",
            "save_offer_assessment",
            "resume_update_career_intent",
            "resume_rewrite_highlight",
        )
        legacy_tools = (
            "save_application_jd_version",
            "create_application_submission_snapshot",
            "record_application_outcome",
        )
        operation_ids: dict[str, str] = {}
        for ordinal, (adapter_kind, tool_name) in enumerate(
            [("typed", tool) for tool in typed_tools]
            + [("legacy_deterministic", tool) for tool in legacy_tools],
            start=1,
        ):
            operation_id = _uuid(700 + ordinal)
            operation_ids[tool_name] = operation_id
            undo_json = (
                '{"undo":"required"}'
                if tool_name
                in {
                    "create_application",
                    "update_application_status",
                    "create_application_event",
                    "add_note",
                }
                else None
            )
            _insert_fixed_terminal_operation(
                connection,
                operation_id=operation_id,
                operation_role="primary",
                parent_operation_id=None,
                parent_terminal_sha=None,
                tool_call_id=f"old-call-{ordinal}",
                tool_name=tool_name,
                adapter_kind=adapter_kind,
                result_contract=(
                    "typed_json_v1"
                    if adapter_kind == "typed"
                    else "legacy_string_v1"
                ),
                undo_json=undo_json,
                delivery_outcome="final_response",
                transition_seed=8000 + ordinal * 10,
            )
        compensation_pairs = (
            ("undo:update_application_status", "update_application_status"),
            ("undo:create_application", "create_application"),
            ("undo:create_application_event", "create_application_event"),
            ("undo:add_note", "add_note"),
        )
        for ordinal, (tool_name, parent_tool) in enumerate(compensation_pairs, start=1):
            _insert_fixed_terminal_operation(
                connection,
                operation_id=_uuid(750 + ordinal),
                operation_role="compensation",
                parent_operation_id=operation_ids[parent_tool],
                parent_terminal_sha=SHA_C,
                tool_call_id=None,
                tool_name=tool_name,
                adapter_kind="compensation",
                result_contract="compensation_json_v1",
                undo_json=None,
                delivery_outcome="none",
                transition_seed=9000 + ordinal * 10,
            )
        connection.commit()
    finally:
        connection.close()


def _insert_fixed_terminal_operation(
    connection: sqlite3.Connection,
    *,
    operation_id: str,
    operation_role: str,
    parent_operation_id: str | None,
    parent_terminal_sha: str | None,
    tool_call_id: str | None,
    tool_name: str,
    adapter_kind: str,
    result_contract: str,
    undo_json: str | None,
    delivery_outcome: str,
    transition_seed: int,
) -> None:
    is_compensation = operation_role == "compensation"
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
          delivery_owner_token_fingerprint,delivery_lease_expires_at,created_at,approved_at,
          claimed_at,rejected_at,committed_at,failed_at,delivered_at,updated_at
        ) VALUES (
          :operation_id,:operation_role,:parent_operation_id,:parent_terminal_sha,
          NULL,NULL,:tool_call_id,:tool_name,:adapter_kind,'proposed',
          :fingerprint_key_id,:proposal_fingerprint,NULL,
          :confirmation_token_fingerprint,:authorization_scope_fingerprint,
          :operation_request_fingerprint,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,
          'pending',NULL,NULL,NULL,NULL,NULL,0,NULL,NULL,
          '2026-08-29 03:00:00.000001',NULL,NULL,NULL,NULL,NULL,NULL,
          '2026-08-29 03:00:00.000001'
        )
        """,
        {
            "operation_id": operation_id,
            "operation_role": operation_role,
            "parent_operation_id": parent_operation_id,
            "parent_terminal_sha": parent_terminal_sha,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "adapter_kind": adapter_kind,
            "fingerprint_key_id": UUID_KEY,
            "proposal_fingerprint": None if is_compensation else HMAC_A,
            "confirmation_token_fingerprint": None if is_compensation else HMAC_C,
            "authorization_scope_fingerprint": None if is_compensation else HMAC_D,
            "operation_request_fingerprint": HMAC_A if is_compensation else None,
        },
    )
    for seq, state in enumerate(("proposed", "approved", "claimed"), start=1):
        connection.execute(
            "INSERT INTO write_operation_transitions(id,operation_id,seq,state,created_at) "
            "VALUES (?,?,?,?,?)",
            (
                _uuid(transition_seed + seq),
                operation_id,
                seq,
                state,
                f"2026-08-29 03:00:0{seq}.{seq:06d}",
            ),
        )
    connection.execute(
        """
        UPDATE write_operations SET
          status='committed',input_fingerprint=:input_fingerprint,
          operation_request_fingerprint=:operation_request_fingerprint,
          result_contract=:result_contract,result_json='{\"old\":true}',
          visible_result='逐字结果',transport_json='{\"transport\":true}',
          undo_json=:undo_json,terminal_payload_sha256=:terminal_payload_sha256,
          delivery_status=:delivery_status,delivery_outcome=:delivery_outcome,
          delivery_message_count=:delivery_message_count,
          delivery_manifest_sha256=:delivery_manifest_sha256,
          delivery_generation=:delivery_generation,
          approved_at='2026-08-29 03:00:01.000002',
          claimed_at='2026-08-29 03:00:02.000003',
          committed_at='2026-08-29 03:00:03.000004',
          delivered_at='2026-08-29 03:00:03.000004',
          updated_at='2026-08-29 03:00:04.000005'
        WHERE id=:operation_id
        """,
        {
            "operation_id": operation_id,
            "input_fingerprint": HMAC_B,
            "operation_request_fingerprint": HMAC_A,
            "result_contract": result_contract,
            "undo_json": undo_json,
            "terminal_payload_sha256": SHA_C,
            "delivery_status": "not_applicable" if is_compensation else "completed",
            "delivery_outcome": delivery_outcome,
            "delivery_message_count": 0 if is_compensation else 2,
            "delivery_manifest_sha256": None if is_compensation else SHA_A,
            "delivery_generation": 0 if is_compensation else 1,
        },
    )
    connection.execute(
        "INSERT INTO write_operation_transitions(id,operation_id,seq,state,created_at) "
        "VALUES (?, ?, 4, 'committed', '2026-08-29 03:00:04.000004')",
        (_uuid(transition_seed + 4), operation_id),
    )


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
    schema_version: object = 1,
    source_id: object = 1,
    source_revision: object = 1,
    route_payload_fingerprint: str = HMAC_A,
    route_binding_fingerprint: str = HMAC_B,
    request_idempotency_fingerprint: str | None = None,
) -> None:
    request_fingerprint = (
        request_idempotency_fingerprint
        if request_idempotency_fingerprint is not None
        else HMAC_C[:-12] + operation_id[-12:]
    )
    connection.execute(
        """
        INSERT INTO product_action_proposals(
          operation_id,action_call_id,action_name,request_origin,schema_version,
          source_kind,source_id,source_revision,route_payload_json,
          route_payload_fingerprint,route_binding_fingerprint,
          request_idempotency_fingerprint,semantic_claim_fingerprint,
          historical_request_token_fingerprint,created_at,terminalized_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,?)
        """,
        (
            operation_id,
            action_call_id,
            action_name,
            request_origin,
            schema_version,
            source_kind,
            source_id,
            source_revision,
            route_payload,
            route_payload_fingerprint,
            route_binding_fingerprint,
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


def test_product_action_exact_integer_storage_and_default_are_affinity_safe(
    migrated_db: tuple[Path, sqlite3.Connection],
) -> None:
    _path, connection = migrated_db
    columns = _table_columns(connection, "product_action_proposals")
    for column_name in ("schema_version", "source_id", "source_revision"):
        declared_type = str(columns[column_name][2]).upper()
        assert "INT" not in declared_type
        assert declared_type in {"", "BLOB"}
    assert columns["schema_version"][3:5] == (1, "1")
    assert columns["source_id"][3:5] == (1, None)
    assert columns["source_revision"][3:5] == (1, None)
    table_sql = str(
        connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='product_action_proposals'"
        ).fetchone()[0]
    ).upper()
    for column_name in ("schema_version", "source_id", "source_revision"):
        declaration = table_sql.split(column_name.upper(), 1)[1].split(",", 1)[0]
        assert "INT" not in declaration

    operation_id = _uuid(90)
    action_call_id = _uuid(91)
    _insert_product_primary(
        connection,
        operation_id=operation_id,
        action_call_id=action_call_id,
        action_name="confirm_interview_story",
    )
    connection.execute(
        """
        INSERT INTO product_action_proposals(
          operation_id,action_call_id,action_name,request_origin,source_kind,
          source_id,source_revision,route_payload_json,route_payload_fingerprint,
          route_binding_fingerprint,request_idempotency_fingerprint
        ) VALUES (?,?,'confirm_interview_story','current','story_proposal',1,1,
          '{}',?,?,?)
        """,
        (operation_id, action_call_id, HMAC_A, HMAC_B, HMAC_C),
    )
    assert connection.execute(
        "SELECT typeof(schema_version),typeof(source_id),typeof(source_revision) "
        "FROM product_action_proposals WHERE operation_id=?",
        (operation_id,),
    ).fetchone() == ("integer", "integer", "integer")


def test_base_metadata_create_all_has_both_primary_scope_checks_and_exact_integer_storage(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'create-all.db'}")
    Base.metadata.create_all(engine)
    with engine.connect() as sqlalchemy_connection:
        table_sql = str(
            sqlalchemy_connection.execute(
                text(
                    "SELECT sql FROM sqlite_master WHERE type='table' "
                    "AND name='write_operations'"
                )
            ).scalar_one()
        )
        assert "ck_write_operations_typed_primary_scope_bound" in table_sql
        assert "ck_write_operations_product_action_scope_bound" in table_sql
        route_columns = list(
            sqlalchemy_connection.execute(text("PRAGMA table_info(product_action_proposals)"))
        )
        route_types = {str(row[1]): str(row[2]).upper() for row in route_columns}
        assert all(
            "INT" not in route_types[name]
            for name in ("schema_version", "source_id", "source_revision")
        )
        with pytest.raises(SQLAlchemyIntegrityError):
            sqlalchemy_connection.execute(
                text(
                    """
                    INSERT INTO write_operations(
                      id,operation_role,parent_operation_id,parent_terminal_payload_sha256,
                      conversation_id,agent_run_id,tool_call_id,tool_name,adapter_kind,status,
                      fingerprint_key_id,proposal_fingerprint,
                      confirmation_token_fingerprint,authorization_scope_fingerprint,
                      delivery_status,delivery_generation
                    ) VALUES (:id,'primary',NULL,NULL,NULL,NULL,'typed-call',
                      'create_application','typed','proposed',:key,:proposal,:confirmation,
                      NULL,'pending',0)
                    """
                ),
                {
                    "id": _uuid(92),
                    "key": UUID_KEY,
                    "proposal": HMAC_A,
                    "confirmation": HMAC_B,
                },
            )
    engine.dispose()
    product_table = Base.metadata.tables["product_action_proposals"]
    for column_name in ("schema_version", "source_id", "source_revision"):
        assert (
            product_table.columns[column_name].type.compile(dialect=postgresql.dialect())
            == "INTEGER"
        )


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


@pytest.mark.parametrize(
    ("column_name", "invalid_value"),
    [
        ("schema_version", 1.0),
        ("schema_version", "1"),
        ("source_id", 1.0),
        ("source_id", "1"),
        ("source_revision", 1.0),
        ("source_revision", "1"),
    ],
)
def test_product_action_exact_integer_columns_reject_real_and_text_affinity_inputs(
    migrated_db: tuple[Path, sqlite3.Connection],
    column_name: str,
    invalid_value: object,
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
    values: dict[str, object] = {
        "schema_version": 1,
        "source_id": 1,
        "source_revision": 1,
    }
    values[column_name] = invalid_value
    with pytest.raises(sqlite3.IntegrityError):
        _insert_product_route(
            connection,
            operation_id=operation_id,
            action_call_id=action_call_id,
            action_name="confirm_interview_story",
            source_kind="story_proposal",
            schema_version=values["schema_version"],
            source_id=values["source_id"],
            source_revision=values["source_revision"],
        )


@pytest.mark.parametrize("surface", ["core", "orm"])
@pytest.mark.parametrize(
    ("column_name", "invalid_value"),
    [
        (column_name, invalid_value)
        for column_name in ("schema_version", "source_id", "source_revision")
        for invalid_value in (True, 1.0, "1")
    ],
)
def test_exact_integer_sqlalchemy_bind_rejects_before_entering_sqlite_driver(
    exact_integer_sqlalchemy_engine: Engine,
    surface: str,
    column_name: str,
    invalid_value: object,
) -> None:
    values = _product_route_values(116)
    values[column_name] = invalid_value
    executed_statements: list[str] = []

    def observe_driver_entry(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        executed_statements.append(statement)

    event.listen(
        exact_integer_sqlalchemy_engine,
        "before_cursor_execute",
        observe_driver_entry,
    )
    try:
        with pytest.raises(StatementError) as exc_info:
            if surface == "core":
                with exact_integer_sqlalchemy_engine.begin() as connection:
                    connection.execute(ProductActionProposal.__table__.insert(), values)
            else:
                with Session(exact_integer_sqlalchemy_engine) as session:
                    session.add(ProductActionProposal(**values))
                    session.flush()
        assert isinstance(exc_info.value.orig, TypeError)
        assert executed_statements == []
    finally:
        event.remove(
            exact_integer_sqlalchemy_engine,
            "before_cursor_execute",
            observe_driver_entry,
        )


def test_raw_sqlite_boolean_uses_the_documented_integer_wire_alias(
    migrated_db: tuple[Path, sqlite3.Connection],
) -> None:
    _path, connection = migrated_db
    operation_id = _uuid(118)
    action_call_id = _uuid(119)
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
        schema_version=True,
    )
    assert connection.execute(
        "SELECT schema_version,typeof(schema_version) FROM product_action_proposals "
        "WHERE operation_id=?",
        (operation_id,),
    ).fetchone() == (1, "integer")


@pytest.mark.parametrize(
    ("uuid_column", "invalid_uuid"),
    [
        ("operation_id", "00000000-0000-4000-8000-00000000000"),
        ("operation_id", "00000000-0000-4000-8000-0000000000000"),
        ("operation_id", "00000000-0000-4000-8000-00000000000A"),
        ("operation_id", "00000000-0000-4000-8000-00000000000z"),
        ("action_call_id", "00000000-0000-4000-8000-00000000000"),
        ("action_call_id", "00000000-0000-4000-8000-0000000000000"),
        ("action_call_id", "00000000-0000-4000-8000-00000000000A"),
        ("action_call_id", "00000000-0000-4000-8000-00000000000z"),
    ],
)
def test_product_action_uuid_columns_reject_malformed_uppercase_and_wrong_length(
    migrated_db: tuple[Path, sqlite3.Connection],
    uuid_column: str,
    invalid_uuid: str,
) -> None:
    _path, connection = migrated_db
    operation_id = invalid_uuid if uuid_column == "operation_id" else _uuid(112)
    action_call_id = invalid_uuid if uuid_column == "action_call_id" else _uuid(113)
    if uuid_column == "operation_id":
        with pytest.raises(sqlite3.IntegrityError):
            _insert_product_primary(
                connection,
                operation_id=operation_id,
                action_call_id=action_call_id,
                action_name="confirm_interview_story",
            )
        return
    _insert_product_primary(
        connection,
        operation_id=operation_id,
        action_call_id=action_call_id,
        action_name="confirm_interview_story",
    )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_product_route(
            connection,
            operation_id=operation_id,
            action_call_id=action_call_id,
            action_name="confirm_interview_story",
            source_kind="story_proposal",
        )


@pytest.mark.parametrize(
    ("fingerprint_column", "invalid_fingerprint"),
    [
        (column_name, invalid_value)
        for column_name in (
            "route_payload_fingerprint",
            "route_binding_fingerprint",
            "request_idempotency_fingerprint",
            "semantic_claim_fingerprint",
            "historical_request_token_fingerprint",
        )
        for invalid_value in (
            "hmac-sha256:" + "a" * 63,
            "hmac-sha256:" + "a" * 65,
            "hmac-sha256:" + "A" * 64,
            "hmac-sha257:" + "a" * 64,
        )
    ],
)
def test_product_action_hmac_columns_reject_malformed_uppercase_and_wrong_length(
    migrated_db: tuple[Path, sqlite3.Connection],
    fingerprint_column: str,
    invalid_fingerprint: str,
) -> None:
    _path, connection = migrated_db
    operation_id = _uuid(114)
    action_call_id = _uuid(115)
    is_semantic = fingerprint_column == "semantic_claim_fingerprint"
    is_historical = fingerprint_column == "historical_request_token_fingerprint"
    action_name = "save_review_readiness_signal" if is_semantic else "confirm_interview_story"
    _insert_product_primary(
        connection,
        operation_id=operation_id,
        action_call_id=action_call_id,
        action_name=action_name,
    )
    kwargs: dict[str, object] = {
        "route_payload_fingerprint": HMAC_A,
        "route_binding_fingerprint": HMAC_B,
        "request_idempotency_fingerprint": HMAC_C,
        "semantic_claim": HMAC_D if is_semantic else None,
        "historical_request": HMAC_D if is_historical else None,
    }
    if fingerprint_column == "semantic_claim_fingerprint":
        kwargs["semantic_claim"] = invalid_fingerprint
    elif fingerprint_column == "historical_request_token_fingerprint":
        kwargs["historical_request"] = invalid_fingerprint
    else:
        kwargs[fingerprint_column] = invalid_fingerprint
    with pytest.raises(sqlite3.IntegrityError):
        _insert_product_route(
            connection,
            operation_id=operation_id,
            action_call_id=action_call_id,
            action_name=action_name,
            source_kind="review_focus" if is_semantic else "story_proposal",
            request_origin="historical_story_bridge" if is_historical else "current",
            **kwargs,
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


@pytest.mark.parametrize("invalid_json", ["{", "[]", '"text"', "null", "1"])
def test_product_action_active_route_requires_valid_top_level_json_object(
    migrated_db: tuple[Path, sqlite3.Connection],
    invalid_json: str,
) -> None:
    _path, connection = migrated_db
    operation_id = _uuid(126)
    action_call_id = _uuid(127)
    _insert_product_primary(
        connection,
        operation_id=operation_id,
        action_call_id=action_call_id,
        action_name="confirm_interview_story",
    )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_product_route(
            connection,
            operation_id=operation_id,
            action_call_id=action_call_id,
            action_name="confirm_interview_story",
            source_kind="story_proposal",
            route_payload=invalid_json,
        )


@pytest.mark.parametrize(
    ("route_payload", "terminalized_at"),
    [
        (None, None),
        ("{}", "2026-08-30 01:02:03.000001"),
        (None, "2026-08-30 01:02:03.000001"),
    ],
)
def test_product_action_route_insert_rejects_every_non_active_lifecycle_shape(
    migrated_db: tuple[Path, sqlite3.Connection],
    route_payload: str | None,
    terminalized_at: str | None,
) -> None:
    _path, connection = migrated_db
    operation_id = _uuid(128)
    action_call_id = _uuid(129)
    _insert_product_primary(
        connection,
        operation_id=operation_id,
        action_call_id=action_call_id,
        action_name="confirm_interview_story",
    )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_product_route(
            connection,
            operation_id=operation_id,
            action_call_id=action_call_id,
            action_name="confirm_interview_story",
            source_kind="story_proposal",
            route_payload=route_payload,
            terminalized_at=terminalized_at,
        )


def _attempt_product_commit_with_delivery(
    connection: sqlite3.Connection,
    *,
    operation_id: str,
    delivery_status: str,
    delivery_generation: int,
    delivery_outcome: str | None,
    delivery_message_count: int | None,
    delivery_owner: str | None = None,
    delivery_lease: str | None = None,
    delivery_manifest: str | None = None,
    delivery_failure_code: str | None = None,
    delivered: bool = False,
    undo_json: str | None = "{}",
) -> None:
    connection.execute(
        """
        UPDATE write_operations SET
          status='committed',input_fingerprint=?,operation_request_fingerprint=?,
          result_contract='product_action_json_v1',result_json='{}',visible_result='saved',
          transport_json='{}',undo_json=?,terminal_payload_sha256=?,
          delivery_status=?,delivery_generation=?,delivery_outcome=?,delivery_message_count=?,
          delivery_owner_token_fingerprint=?,delivery_lease_expires_at=?,
          delivery_manifest_sha256=?,delivery_failure_code=?,
          delivery_next_operation_id=CASE WHEN ?='chained_pending' THEN id ELSE NULL END,
          approved_at=CURRENT_TIMESTAMP,claimed_at=CURRENT_TIMESTAMP,
          committed_at=CURRENT_TIMESTAMP,
          delivered_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END
        WHERE id=?
        """,
        (
            HMAC_D,
            HMAC_C,
            undo_json,
            SHA_C,
            delivery_status,
            delivery_generation,
            delivery_outcome,
            delivery_message_count,
            delivery_owner,
            delivery_lease,
            delivery_manifest,
            delivery_failure_code,
            delivery_outcome,
            delivered,
            operation_id,
        ),
    )


@pytest.mark.parametrize(
    (
        "delivery_status",
        "delivery_generation",
        "delivery_outcome",
        "delivery_message_count",
        "delivery_owner",
        "delivery_lease",
        "delivery_manifest",
        "delivery_failure_code",
        "delivered",
    ),
    [
        ("pending", 1, None, None, HMAC_A, "2026-08-30 02:00:00", None, None, False),
        ("completed", 1, "final_response", 2, None, None, SHA_A, None, True),
        ("completed", 1, "chained_pending", 2, None, None, SHA_A, None, True),
        ("failed", 1, "fallback", 2, None, None, SHA_A, "delivery_failed", True),
        ("not_applicable", 0, None, 0, None, None, None, None, True),
        ("not_applicable", 0, "final_response", 0, None, None, None, None, True),
    ],
)
def test_product_action_terminal_rejects_chat_delivery_and_nonexact_outcomes(
    migrated_db: tuple[Path, sqlite3.Connection],
    delivery_status: str,
    delivery_generation: int,
    delivery_outcome: str | None,
    delivery_message_count: int | None,
    delivery_owner: str | None,
    delivery_lease: str | None,
    delivery_manifest: str | None,
    delivery_failure_code: str | None,
    delivered: bool,
) -> None:
    _path, connection = migrated_db
    operation_id = _uuid(130)
    action_call_id = _uuid(131)
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
    with pytest.raises(sqlite3.IntegrityError):
        _attempt_product_commit_with_delivery(
            connection,
            operation_id=operation_id,
            delivery_status=delivery_status,
            delivery_generation=delivery_generation,
            delivery_outcome=delivery_outcome,
            delivery_message_count=delivery_message_count,
            delivery_owner=delivery_owner,
            delivery_lease=delivery_lease,
            delivery_manifest=delivery_manifest,
            delivery_failure_code=delivery_failure_code,
            delivered=delivered,
        )


def test_product_action_committed_requires_action_undo_payload(
    migrated_db: tuple[Path, sqlite3.Connection],
) -> None:
    _path, connection = migrated_db
    operation_id = _uuid(132)
    action_call_id = _uuid(133)
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
    with pytest.raises(sqlite3.IntegrityError):
        _attempt_product_commit_with_delivery(
            connection,
            operation_id=operation_id,
            delivery_status="not_applicable",
            delivery_generation=0,
            delivery_outcome="none",
            delivery_message_count=0,
            delivered=True,
            undo_json=None,
        )


@pytest.mark.parametrize(
    ("action_name", "compensation_name"),
    [
        (action_name, compensation_name)
        for action_name, exact_compensation in (
            ("confirm_interview_story", "undo:confirm_interview_story"),
            ("save_review_readiness_signal", "undo:save_review_readiness_signal"),
        )
        for compensation_name in (
            "undo:confirm_interview_story",
            "undo:save_review_readiness_signal",
            "undo:update_application_status",
            "undo:create_application",
            "undo:create_application_event",
            "undo:add_note",
        )
        if compensation_name != exact_compensation
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
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE product_action_proposals SET route_payload_json='{}' "
            "WHERE operation_id=?",
            (operation_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE product_action_proposals SET terminalized_at=NULL WHERE operation_id=?",
            (operation_id,),
        )


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
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "DELETE FROM interview_readiness_signal_versions WHERE id=?",
            (retracted_version_id,),
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
    source_app, _source_event, signal_id, version_id = _create_signal_version(
        connection,
        app_seed=4,
        operation_seed=440,
    )
    target_app = source_app
    target_event = int(
        connection.execute(
            "INSERT INTO application_events(application_id,event_type,status) "
            "VALUES (?,'interview','scheduled')",
            (source_app,),
        ).lastrowid
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
    connection.execute(
        "UPDATE adaptive_practice_plans SET status='completed',revision=2,"
        "response_text='answer',reflection_text='reflection',self_assessment='ready',"
        "completion_idempotency_key=start_idempotency_key || '-complete',"
        "completion_fingerprint=?,completed_at=CURRENT_TIMESTAMP",
        (SHA_A,),
    )
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
        "SELECT status,readiness_signal_version_id,target_application_event_id,"
        "source_fingerprint,target_fingerprint FROM adaptive_practice_plans "
        "WHERE start_idempotency_key='start-2'"
    ).fetchone() == ("completed", version_id, None, SHA_B, SHA_C)
    connection.execute(
        "DELETE FROM interview_readiness_signals WHERE id=?",
        (signal_id,),
    )
    row = connection.execute(
        "SELECT status,origin_contract,readiness_signal_version_id,target_application_event_id,"
        "source_fingerprint,target_fingerprint FROM adaptive_practice_plans "
        "WHERE start_idempotency_key='start-1'"
    ).fetchone()
    assert row == (
        "completed",
        "confirmed_readiness_signal_v1",
        None,
        target_event,
        SHA_B,
        SHA_C,
    )
    assert connection.execute(
        "SELECT status,readiness_signal_version_id,target_application_event_id,"
        "source_fingerprint,target_fingerprint FROM adaptive_practice_plans "
        "WHERE start_idempotency_key='start-2'"
    ).fetchone() == ("completed", None, None, SHA_B, SHA_C)
    connection.execute("DELETE FROM application_events WHERE id=?", (target_event,))
    row = connection.execute(
        "SELECT status,readiness_signal_version_id,target_application_event_id,"
        "source_fingerprint,target_fingerprint FROM adaptive_practice_plans "
        "WHERE start_idempotency_key='start-1'"
    ).fetchone()
    assert row == ("completed", None, None, SHA_B, SHA_C)

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


def test_normal_init_upgrades_fixed_0028_history_without_changing_ledger_bytes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "fixed-0028.db"
    _create_fixed_pre_0029_database(db_path)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchone() == (68,)
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
        ).fetchone() == (95,)
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger'"
        ).fetchone() == (40,)
        assert connection.execute(
            "SELECT version FROM schema_migrations "
            "WHERE version IN ('0026_write_operation_ledger','0028_scoped_tool_authority') "
            "ORDER BY version"
        ).fetchall() == [
            ("0026_write_operation_ledger",),
            ("0028_scoped_tool_authority",),
        ]
        old_ledger_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='write_operations'"
            ).fetchone()[0]
        )
        assert "ck_write_operations_manifest" in old_ledger_sql
        assert "ck_write_operations_typed_primary_scope_bound" in old_ledger_sql
        assert "product_action" not in old_ledger_sql
        old_trigger_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert {
            "trg_write_operation_compensation_insert",
            "trg_write_operation_terminal_immutable",
            "trg_write_operation_delivery_generation",
            "trg_write_operation_delivery_immutable",
            "trg_write_operation_transition_insert",
            "trg_write_operation_transition_immutable",
            "trg_write_operation_transition_delete",
            "trg_write_operation_scope_insert",
            "trg_write_operation_scope_fingerprint_immutable",
            "trg_write_operation_scope_identity_immutable",
            "trg_write_operation_scope_conversation_immutable",
            "trg_write_operation_scope_status",
        } <= old_trigger_names
        operation_columns = [
            str(row[1]) for row in connection.execute("PRAGMA table_info(write_operations)")
        ]
        transition_columns = [
            str(row[1])
            for row in connection.execute("PRAGMA table_info(write_operation_transitions)")
        ]
        before_operations = connection.execute(
            "SELECT " + ",".join(f'\"{name}\"' for name in operation_columns)
            + " FROM write_operations ORDER BY id"
        ).fetchall()
        before_transitions = connection.execute(
            "SELECT " + ",".join(f'\"{name}\"' for name in transition_columns)
            + " FROM write_operation_transitions ORDER BY operation_id,seq,id"
        ).fetchall()
        assert len(before_operations) == 19
        assert len(before_transitions) == 19 * 4
        assert connection.execute(
            "SELECT adapter_kind,count(*) FROM write_operations "
            "GROUP BY adapter_kind ORDER BY adapter_kind"
        ).fetchall() == [
            ("compensation", 4),
            ("legacy_deterministic", 3),
            ("typed", 12),
        ]
        assert "content_revision" not in _table_columns(connection, "interview_notes")
        assert "proposal_schema_version" not in _table_columns(
            connection, "interview_review_proposals"
        )
        assert "product_action_generation" not in _table_columns(
            connection, "interview_story_proposal_attempts"
        )
        assert "origin_contract" not in _table_columns(
            connection, "adaptive_practice_plans"
        )

    baseline = json.loads(
        (FIXTURE_DIRECTORY / "review_to_readiness_baseline_c5a020c.json").read_text(
            encoding="utf-8"
        )
    )
    assert baseline["source_baseline"] == "c5a020cbedd8ff64f6188f51c10d8f4daa7c7dff"
    assert (
        baseline["provider_tools"],
        baseline["legacy_deterministic"],
        baseline["agent_compensations"],
    ) == (25, 3, 4)

    first = init_database(db_path)
    _dispose(first)
    second = init_database(db_path)
    _dispose(second)
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        after_operations = connection.execute(
            "SELECT " + ",".join(f'\"{name}\"' for name in operation_columns)
            + " FROM write_operations ORDER BY id"
        ).fetchall()
        after_transitions = connection.execute(
            "SELECT " + ",".join(f'\"{name}\"' for name in transition_columns)
            + " FROM write_operation_transitions ORDER BY operation_id,seq,id"
        ).fetchall()
        assert after_operations == before_operations
        assert after_transitions == before_transitions
        assert connection.execute(
            "SELECT adapter_kind,count(*) FROM write_operations "
            "GROUP BY adapter_kind ORDER BY adapter_kind"
        ).fetchall() == [
            ("compensation", 4),
            ("legacy_deterministic", 3),
            ("typed", 12),
        ]
        assert connection.execute(
            "SELECT questions,content_revision,updated_at,created_at FROM interview_notes "
            "WHERE id=1"
        ).fetchone() == (
            "逐字正文\\u0000保留",
            1,
            "2026-08-29 01:00:03.000004",
            "2026-08-29 01:00:03.000004",
        )
        assert connection.execute(
            "SELECT proposal_schema_version,source_note_revision,proposal_json,"
            "proposal_hash,created_at FROM interview_review_proposals WHERE id=1"
        ).fetchone() == (
            1,
            None,
            '{"focuses":["历史"]}',
            SHA_B,
            "2026-08-29 01:00:04.000005",
        )
        assert connection.execute(
            "SELECT attempt_status,product_action_operation_id,product_action_generation "
            "FROM interview_story_proposal_attempts ORDER BY id"
        ).fetchall() == [
            ("generating", None, 0),
            ("ready", None, 0),
            ("confirmed", None, 0),
        ]
        assert connection.execute(
            "SELECT status,origin_contract,readiness_signal_version_id,"
            "target_application_event_id,target_fingerprint,source_excerpt "
            "FROM adaptive_practice_plans ORDER BY id"
        ).fetchall() == [
            ("in_progress", "legacy_review_focus_v1", None, None, None, "逐字证据"),
            ("completed", "legacy_review_focus_v1", None, None, None, "逐字证据"),
        ]
        indexes = {
            str(row[1])
            for row in connection.execute("PRAGMA index_list(adaptive_practice_plans)")
        }
        assert "uq_adaptive_practice_proposal_focus" not in indexes
        assert {
            "uq_adaptive_practice_legacy_proposal_focus",
            "uq_adaptive_practice_signal_target",
        } <= indexes
        assert connection.execute(
            "SELECT count(*) FROM schema_migrations "
            "WHERE version='0029_review_to_readiness_feedback'"
        ).fetchone() == (1,)
        route_columns = _table_columns(connection, "product_action_proposals")
        route_table_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='product_action_proposals'"
            ).fetchone()[0]
        ).upper()
        for column_name in ("schema_version", "source_id", "source_revision"):
            declared_type = str(route_columns[column_name][2]).upper()
            assert "INT" not in declared_type
            assert declared_type in {"", "BLOB"}
            declaration = route_table_sql.split(column_name.upper(), 1)[1].split(",", 1)[0]
            assert "INT" not in declaration
        operation_id = _uuid(990)
        action_call_id = _uuid(991)
        _insert_product_primary(
            connection,
            operation_id=operation_id,
            action_call_id=action_call_id,
            action_name="confirm_interview_story",
        )
        connection.execute(
            """
            INSERT INTO product_action_proposals(
              operation_id,action_call_id,action_name,request_origin,source_kind,
              source_id,source_revision,route_payload_json,route_payload_fingerprint,
              route_binding_fingerprint,request_idempotency_fingerprint
            ) VALUES (?,?,'confirm_interview_story','current','story_proposal',1,1,
              '{}',?,?,?)
            """,
            (operation_id, action_call_id, HMAC_A, HMAC_B, HMAC_C),
        )
        assert connection.execute(
            "SELECT typeof(schema_version),typeof(source_id),typeof(source_revision) "
            "FROM product_action_proposals WHERE operation_id=?",
            (operation_id,),
        ).fetchone() == ("integer", "integer", "integer")
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_forced_rebuild_preserves_every_write_operation_and_transition_column(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "history.db"
    factory = init_database(db_path)
    engine = factory.kw["bind"]
    operation_id = _uuid(600)
    with engine.begin() as connection:
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


def test_forced_rebuild_preserves_domain_history_and_practice_indexes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "domain-history.db"
    factory = init_database(db_path)
    engine = factory.kw["bind"]
    with engine.begin() as connection:
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
    _create_fixed_pre_0029_database(db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)

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
    engine.dispose()
