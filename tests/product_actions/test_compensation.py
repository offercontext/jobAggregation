from __future__ import annotations

import copy
import hashlib
import gc
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from uuid import UUID, uuid5

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from offerpilot.agent_runtime.journal import SafeRunRecorder
from offerpilot.ai.client import ConfiguredAIClient
from offerpilot.ai.write_operations import ledger_fingerprint, payload_from_operation
from offerpilot.db import init_database
from offerpilot.models import (
    ApplicationEvent,
    AgentContextSnapshot,
    AgentEvent,
    AgentRun,
    ChatMessage,
    InterviewNote,
    InterviewReadinessSignal,
    InterviewReadinessSignalEvidence,
    InterviewReadinessSignalVersion,
    InterviewReviewProposal,
    Conversation,
    ProductActionProposal,
    WriteOperation,
    WriteOperationTransition,
)
from offerpilot.product_actions.catalog import (
    ProductActionCatalogV1,
    ProductActionCompensationCatalogV1,
)
from offerpilot.product_actions.compensation import (
    PRODUCT_ACTION_COMPENSATION_NAMESPACE,
    READINESS_SIGNAL_RETRACTION_VERSION_NAMESPACE,
    ProductActionCompensationCoordinator,
    ProductActionCompensationError,
    ProductActionCompensationProofRegistryV1,
    ProductActionCompensationStale,
    ReadinessSignalProductActionUndoProof,
    ReadinessSignalUndoIssuer,
    product_action_compensation_input_fingerprint,
    product_action_compensation_operation_id,
    product_action_compensation_request_fingerprint,
    readiness_signal_retraction_domain_key,
    _decode_json_object,
    _enforce_compensation_terminal_budgets,
    _CompensationExecutionUowV1,
)
from offerpilot.product_actions.contracts import (
    ProductActionExecutionAuthorization,
    ProductActionIntegrityError,
    ProductActionProofRegistryV1,
    canonical_product_action_json,
)
from offerpilot.product_actions.coordinator import ProductActionCoordinator
from offerpilot.product_actions.issuer import LedgerKeyProfileStoreV1, ReviewReadinessActionIssuer
from offerpilot.product_actions.repository import ProductActionProposalRepository
from offerpilot.pilot_runtime.compensation import CompensationHandlerRegistry
from offerpilot.review_readiness.candidates import project_readiness_candidates
from offerpilot.review_readiness.repository import (
    ReadinessSignalRepository,
    ReadinessSignalRetractionResultV1,
    _CompensationDomainClaimV1,
)
from offerpilot.repositories.agent_runs import AgentRunRepository
from offerpilot.repositories.chat import ChatRepository

from tests.product_actions.conftest import KEY_ONE, KEY_TWO
from tests.review_readiness_support import seed_review_candidate


PARENT_OPERATION_ID = "00000000-0000-4000-8000-000000000001"
PARENT_TERMINAL_DIGEST = "sha256:" + "a" * 64


def _create_committed_signal(session_factory):  # type: ignore[no-untyped-def]
    seeded = seed_review_candidate(session_factory)
    registry = ProductActionProofRegistryV1()
    catalog = ProductActionCatalogV1(registry)
    keys = LedgerKeyProfileStoreV1((KEY_ONE, KEY_TWO), active_key_id=KEY_ONE.key_id)
    issuer = ReviewReadinessActionIssuer(catalog, registry, keys)
    proposal_repository = ProductActionProposalRepository(
        session_factory,
        catalog=catalog,
        proof_registry=registry,
        key_profiles=keys,
    )
    signals = ReadinessSignalRepository(session_factory, proof_registry=registry)
    coordinator = ProductActionCoordinator(
        session_factory,
        catalog=catalog,
        proposal_repository=proposal_repository,
        review_issuer=issuer,
        proof_registry=registry,
        key_profiles=keys,
        readiness_repository=signals,
        capability_check=lambda capability: (
            capability == "application.interview_readiness_feedback.write"
        ),
        candidate_projector=project_readiness_candidates,
    )
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    proposed = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request={
            "proposal_id": seeded["proposal_id"],
            "focus_id": seeded["focus_id"],
            "expected_note_revision": seeded["note_revision"],
            "expected_candidate_fingerprint": candidate.candidate_fingerprint,
            "idempotency_key": "33333333-3333-4333-8333-333333333333",
            "user_note": "下次练习先讲清边界条件。",
        },
    )
    decided = coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": proposed.confirmation_token,
            "decision": "approve",
        },
    )
    assert decided.status == "committed"
    return seeded, proposed, signals, keys


def _seed_signal_compensation_proposal(
    session_factory,  # type: ignore[no-untyped-def]
    *,
    parent_operation_id: str,
) -> str:
    with session_factory() as session:
        parent = session.get(WriteOperation, parent_operation_id)
        assert parent is not None
        parent_payload = payload_from_operation(parent)
        operation_id = product_action_compensation_operation_id(
            parent_operation_id,
            "undo:save_review_readiness_signal",
        )
        request_fingerprint = product_action_compensation_request_fingerprint(
            KEY_ONE,
            operation_id=operation_id,
            parent_operation_id=parent_operation_id,
            parent_action_name="save_review_readiness_signal",
            parent_terminal_payload_sha256=parent_payload.digest,
            compensation_kind="undo:save_review_readiness_signal",
        )
        now = datetime.now(timezone.utc)
        session.add(
            WriteOperation(
                id=operation_id,
                operation_role="compensation",
                parent_operation_id=parent_operation_id,
                parent_terminal_payload_sha256=parent_payload.digest,
                conversation_id=None,
                agent_run_id=None,
                tool_call_id=None,
                tool_name="undo:save_review_readiness_signal",
                adapter_kind="compensation",
                status="proposed",
                fingerprint_key_id=KEY_ONE.key_id,
                operation_request_fingerprint=request_fingerprint,
                delivery_status="pending",
                delivery_generation=0,
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add_all(
            WriteOperationTransition(
                id=str(
                    uuid5(
                        PRODUCT_ACTION_COMPENSATION_NAMESPACE,
                        operation_id + f":{seq}",
                    )
                ),
                operation_id=operation_id,
                seq=seq,
                state=state,
                created_at=now,
            )
            for seq, state in (
                (1, "proposed"),
                (2, "approved"),
                (3, "claimed"),
            )
        )
        session.commit()
        return operation_id


def _compensation_stack(session_factory, *, capability_check=None):  # type: ignore[no-untyped-def]
    registry = ProductActionCompensationProofRegistryV1()
    execution_registry = ProductActionProofRegistryV1()
    catalog = ProductActionCompensationCatalogV1()
    keys = LedgerKeyProfileStoreV1((KEY_ONE, KEY_TWO), active_key_id=KEY_ONE.key_id)
    check = capability_check or (
        lambda capability: capability == "application.interview_readiness_feedback.write"
    )
    repository = ReadinessSignalRepository(
        session_factory,
        proof_registry=execution_registry,
    )
    issuer = ReadinessSignalUndoIssuer(
        session_factory,
        catalog=catalog,
        proof_registry=registry,
        key_profiles=keys,
        capability_check=check,
    )
    coordinator = ProductActionCompensationCoordinator(
        session_factory,
        catalog=catalog,
        proof_registry=registry,
        execution_registry=execution_registry,
        key_profiles=keys,
        capability_check=check,
        readiness_repository=repository,
    )
    return issuer, coordinator


def _append_exact_later_signal_version(
    session_factory,  # type: ignore[no-untyped-def]
    signal_id: int,
    *,
    advance_revision: bool,
    point_current: bool = True,
) -> int:
    with session_factory() as session:
        signal = session.get(InterviewReadinessSignal, signal_id)
        assert signal is not None and signal.current_version_id is not None
        active = session.get(
            InterviewReadinessSignalVersion,
            signal.current_version_id,
        )
        assert active is not None
        evidence = tuple(
            session.scalars(
                select(InterviewReadinessSignalEvidence)
                .where(InterviewReadinessSignalEvidence.signal_version_id == active.id)
                .order_by(InterviewReadinessSignalEvidence.ordinal)
            )
        )
        active_values = (
            signal.id,
            active.version_number + 1,
            active.id,
            active.statement_text,
            active.user_note + " later edit",
            active.source_note_revision,
            active.source_note_fingerprint,
            active.source_proposal_hash,
            active.candidate_fingerprint,
        )
        revision = signal.revision + (1 if advance_revision else 0)
    engine = session_factory.kw["bind"]
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute(
            """
            INSERT INTO interview_readiness_signal_versions(
              signal_id,version_number,parent_version_id,disposition,schema_version,
              statement_text,user_note,source_note_revision,source_note_fingerprint,
              source_proposal_hash,candidate_fingerprint,domain_idempotency_key,
              write_operation_id,created_at
            ) VALUES (?,?,?,'active','readiness-signal-v1',?,?,?,?,?,?,?,?,'2026-08-31')
            """,
            (
                *active_values,
                "99999999-9999-4999-8999-999999999991",
                "99999999-9999-4999-8999-999999999992",
            ),
        )
        later_id = cursor.lastrowid
        for row in evidence:
            cursor.execute(
                """
                INSERT INTO interview_readiness_signal_evidence(
                  signal_version_id,ordinal,source_path,excerpt,excerpt_sha256,
                  source_field_sha256
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    later_id,
                    row.ordinal,
                    row.source_path,
                    row.excerpt,
                    row.excerpt_sha256,
                    row.source_field_sha256,
                ),
            )
        if point_current:
            cursor.execute(
                """
                UPDATE interview_readiness_signals
                SET current_version_id=?, revision=?
                WHERE id=?
                """,
                (later_id, revision, signal_id),
            )
        raw.commit()
        cursor.execute("PRAGMA foreign_keys=ON")
        return later_id
    finally:
        raw.close()


@pytest.mark.parametrize(
    ("compensation_kind", "expected"),
    (
        (
            "undo:confirm_interview_story",
            "b599fc91-b4d3-526b-bbe8-a0e05a56cd2e",
        ),
        (
            "undo:save_review_readiness_signal",
            "760d1d9a-87d2-5e91-98b1-6df229bc8869",
        ),
    ),
)
def test_compensation_operation_identity_is_cross_process_stable(
    compensation_kind: str,
    expected: str,
) -> None:
    assert PRODUCT_ACTION_COMPENSATION_NAMESPACE == UUID(
        "1c914194-602c-54fa-b770-853a5ac87a2b"
    )
    assert (
        product_action_compensation_operation_id(
            PARENT_OPERATION_ID,
            compensation_kind,
        )
        == expected
    )
    script = (
        "from offerpilot.product_actions.compensation import "
        "product_action_compensation_operation_id as f;"
        f"print(f({PARENT_OPERATION_ID!r},{compensation_kind!r}))"
    )
    assert subprocess.check_output([sys.executable, "-c", script], text=True).strip() == expected


def test_compensation_fingerprints_use_the_closed_design_envelopes() -> None:
    operation_id = product_action_compensation_operation_id(
        PARENT_OPERATION_ID,
        "undo:save_review_readiness_signal",
    )
    undo = {
        "kind": "retract_review_readiness_signal_v1",
        "signal_id": 1,
        "created_version_id": 2,
        "expected_current_version_id": 2,
        "expected_signal_revision": 1,
        "parent_operation_id": PARENT_OPERATION_ID,
    }
    request = product_action_compensation_request_fingerprint(
        KEY_ONE,
        operation_id=operation_id,
        parent_operation_id=PARENT_OPERATION_ID,
        parent_action_name="save_review_readiness_signal",
        parent_terminal_payload_sha256=PARENT_TERMINAL_DIGEST,
        compensation_kind="undo:save_review_readiness_signal",
    )
    assert request == (
        "hmac-sha256:454d354a5bbc7745065181d6c253834ab4f984373a7a45c2113f4a54ccf6699e"
    )
    expected_request = ledger_fingerprint(
        KEY_ONE,
        "product-action-compensation-request-v1",
        {
            "request_kind": "product_action_compensation_v1",
            "operation_id": operation_id,
            "parent_operation_id": PARENT_OPERATION_ID,
            "parent_action_name": "save_review_readiness_signal",
            "parent_terminal_payload_sha256": PARENT_TERMINAL_DIGEST,
            "compensation_kind": "undo:save_review_readiness_signal",
        },
    )
    assert request == expected_request
    input_fingerprint = product_action_compensation_input_fingerprint(
        KEY_ONE,
        operation_request_fingerprint=request,
        parent_terminal_payload_sha256=PARENT_TERMINAL_DIGEST,
        validated_undo_json=undo,
    )
    assert input_fingerprint == (
        "hmac-sha256:42672a52dc0aa2c5ea3d714fd936ca312a704e464fa0c46f8622bb682a885b01"
    )
    assert input_fingerprint == ledger_fingerprint(
        KEY_ONE,
        "product-action-compensation-input-v1",
        {
            "operation_request_fingerprint": request,
            "parent_terminal_payload_sha256": PARENT_TERMINAL_DIGEST,
            "validated_undo_json": undo,
        },
    )

    story_operation_id = product_action_compensation_operation_id(
        PARENT_OPERATION_ID,
        "undo:confirm_interview_story",
    )
    story_undo = {
        "kind": "archive_created_story_v1",
        "story_id": 11,
        "created_version_id": 21,
        "expected_current_version_id": 21,
        "expected_story_revision": 1,
        "expected_status": "active",
    }
    story_request = product_action_compensation_request_fingerprint(
        KEY_ONE,
        operation_id=story_operation_id,
        parent_operation_id=PARENT_OPERATION_ID,
        parent_action_name="confirm_interview_story",
        parent_terminal_payload_sha256=PARENT_TERMINAL_DIGEST,
        compensation_kind="undo:confirm_interview_story",
    )
    assert story_request == (
        "hmac-sha256:904add7456e18acbf42f08d6694808349f664946f620fe7211a40b0e5efd2cab"
    )
    assert product_action_compensation_input_fingerprint(
        KEY_ONE,
        operation_request_fingerprint=story_request,
        parent_terminal_payload_sha256=PARENT_TERMINAL_DIGEST,
        validated_undo_json=story_undo,
    ) == (
        "hmac-sha256:16d48d281263900beee63a8bb271e4390c32189a429cf3ca8a17435689f24d6d"
    )


def test_signal_retraction_key_is_deterministic_and_proofs_are_not_constructible() -> None:
    operation_id = "760d1d9a-87d2-5e91-98b1-6df229bc8869"
    assert READINESS_SIGNAL_RETRACTION_VERSION_NAMESPACE == UUID(
        "6fc5aa59-d7c6-53e0-9f3e-98aac460b593"
    )
    assert readiness_signal_retraction_domain_key(operation_id) == str(
        uuid5(
            READINESS_SIGNAL_RETRACTION_VERSION_NAMESPACE,
            operation_id + ":signal-retraction",
        )
    )
    assert readiness_signal_retraction_domain_key(operation_id) == (
        "40e59326-f319-5d91-8bb0-ef986879f744"
    )
    with pytest.raises(TypeError):
        ReadinessSignalProductActionUndoProof()


def test_signal_retraction_appends_immutable_byte_copies_after_source_deletion(tmp_path) -> None:
    session_factory = init_database(tmp_path / "signal-retraction.sqlite3")
    seeded, proposed, repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None and signal.current_version_id is not None
        parent_version = session.get(
            InterviewReadinessSignalVersion,
            signal.current_version_id,
        )
        assert parent_version is not None
        parent_evidence = tuple(
            session.scalars(
                select(InterviewReadinessSignalEvidence)
                .where(
                    InterviewReadinessSignalEvidence.signal_version_id
                    == parent_version.id
                )
                .order_by(InterviewReadinessSignalEvidence.ordinal)
            )
        )
        parent_fields = (
            parent_version.statement_text,
            parent_version.user_note,
            parent_version.source_note_revision,
            parent_version.source_note_fingerprint,
            parent_version.source_proposal_hash,
            parent_version.candidate_fingerprint,
        )
        evidence_fields = tuple(
            (
                row.ordinal,
                row.source_path,
                row.excerpt,
                row.excerpt_sha256,
                row.source_field_sha256,
            )
            for row in parent_evidence
        )
        signal_id = signal.id
        parent_version_id = parent_version.id
        expected_revision = signal.revision
        session.execute(
            delete(InterviewReviewProposal).where(
                InterviewReviewProposal.id == seeded["proposal_id"]
            )
        )
        session.commit()

    proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    result = coordinator.execute(proof)
    compensation_id = result.operation_id

    with session_factory() as session:
        signal = session.get(InterviewReadinessSignal, signal_id)
        assert signal is not None and signal.current_version_id is not None
        retracted = session.get(
            InterviewReadinessSignalVersion,
            signal.current_version_id,
        )
        assert retracted is not None
        copied_evidence = tuple(
            session.scalars(
                select(InterviewReadinessSignalEvidence)
                .where(
                    InterviewReadinessSignalEvidence.signal_version_id == retracted.id
                )
                .order_by(InterviewReadinessSignalEvidence.ordinal)
            )
        )
        versions = tuple(
            session.scalars(
                select(InterviewReadinessSignalVersion)
                .where(InterviewReadinessSignalVersion.signal_id == signal_id)
                .order_by(InterviewReadinessSignalVersion.version_number)
            )
        )

    assert result.result["retracted_version_id"] == retracted.id
    assert result.result["signal_revision"] == expected_revision + 1
    assert signal.revision == expected_revision + 1
    assert len(versions) == 2
    assert versions[0].id == parent_version_id
    assert retracted.parent_version_id == parent_version_id
    assert retracted.version_number == versions[0].version_number + 1
    assert retracted.disposition == "retracted"
    assert (
        retracted.statement_text,
        retracted.user_note,
        retracted.source_note_revision,
        retracted.source_note_fingerprint,
        retracted.source_proposal_hash,
        retracted.candidate_fingerprint,
    ) == parent_fields
    assert retracted.domain_idempotency_key == readiness_signal_retraction_domain_key(
        compensation_id
    )
    assert retracted.write_operation_id == compensation_id
    assert tuple(
        (
            row.ordinal,
            row.source_path,
            row.excerpt,
            row.excerpt_sha256,
            row.source_field_sha256,
        )
        for row in copied_evidence
    ) == evidence_fields


def test_signal_retraction_rejects_later_edit_without_partial_history(tmp_path) -> None:
    session_factory = init_database(tmp_path / "signal-retraction-stale.sqlite3")
    _seeded, proposed, repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None and signal.current_version_id is not None
        signal_id = signal.id
        signal.revision += 1
        session.commit()

    with pytest.raises(ProductActionCompensationError):
        issuer.issue(
            application_id=_seeded["application_id"],
            signal_id=signal_id,
            parent_operation_id=proposed.operation_id,
        )

    with session_factory() as session:
        versions = tuple(
            session.scalars(
                select(InterviewReadinessSignalVersion).where(
                    InterviewReadinessSignalVersion.signal_id == signal_id
                )
            )
        )
    assert len(versions) == 1


def test_signal_retraction_requires_the_exact_proposed_compensation_parent(tmp_path) -> None:
    session_factory = init_database(tmp_path / "signal-retraction-owner.sqlite3")
    _seeded, proposed, repository, _keys = _create_committed_signal(session_factory)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None and signal.current_version_id is not None
        signal_id = signal.id
        version_id = signal.current_version_id
        revision = signal.revision
    compensation_id = _seed_signal_compensation_proposal(
        session_factory,
        parent_operation_id=proposed.operation_id,
    )
    with session_factory() as session:
        with pytest.raises(TypeError):
            repository.retract_signal_in_session(
                session,
                signal_id=signal_id,
                expected_current_version_id=version_id,
                expected_signal_revision=revision,
                parent_operation_id=proposed.operation_id,
                compensation_operation_id=compensation_id,
            )
        session.rollback()
    with session_factory() as session:
        versions = tuple(
            session.scalars(
                select(InterviewReadinessSignalVersion).where(
                    InterviewReadinessSignalVersion.signal_id == signal_id
                )
            )
        )
        operation = session.get(WriteOperation, compensation_id)
    assert len(versions) == 1
    assert operation is not None and operation.status == "proposed"


def test_raw_domain_helper_rejects_forged_claim_without_mutation(tmp_path) -> None:
    session_factory = init_database(tmp_path / "raw-domain-helper.sqlite3")
    _seeded, proposed, repository, _keys = _create_committed_signal(session_factory)
    compensation_id = _seed_signal_compensation_proposal(
        session_factory,
        parent_operation_id=proposed.operation_id,
    )
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None and signal.current_version_id is not None
        with pytest.raises(ProductActionIntegrityError) as captured:
            repository._retract_signal_authorized_in_session(
                session,
                signal_id=signal.id,
                expected_current_version_id=signal.current_version_id,
                expected_signal_revision=signal.revision,
                parent_operation_id=proposed.operation_id,
                compensation_operation_id=compensation_id,
                authorization=object(),  # type: ignore[arg-type]
                authorization_binding=(),
                domain_claim=object(),  # type: ignore[arg-type]
            )
        session.rollback()
    assert captured.value.code == "readiness_signal_compensation_domain_claim"
    with session_factory() as session:
        assert len(tuple(session.scalars(select(InterviewReadinessSignalVersion)))) == 1


def test_raw_domain_helper_rejects_object_new_exact_claim_and_replay(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "raw-domain-exact-claim.sqlite3")
    _seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    repository = coordinator._readiness_repository
    compensation_id = product_action_compensation_operation_id(
        proposed.operation_id,
        "undo:save_review_readiness_signal",
    )
    forged = object.__new__(_CompensationDomainClaimV1)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None and signal.current_version_id is not None
        with pytest.raises(ProductActionIntegrityError) as captured:
            repository._retract_signal_authorized_in_session(
                session,
                signal_id=signal.id,
                expected_current_version_id=signal.current_version_id,
                expected_signal_revision=signal.revision,
                parent_operation_id=proposed.operation_id,
                compensation_operation_id=compensation_id,
                authorization=object(),  # type: ignore[arg-type]
                authorization_binding=(),
                domain_claim=forged,
            )
        session.rollback()
    assert captured.value.code == "readiness_signal_compensation_domain_claim"

    original = repository._retract_signal_authorized_in_session
    captured_claims: list[_CompensationDomainClaimV1] = []

    def capture_claim(session, **kwargs):  # type: ignore[no-untyped-def]
        captured_claims.append(kwargs["domain_claim"])
        projected = original(session, **kwargs)
        operation = session.get(WriteOperation, compensation_id)
        transitions = tuple(
            (row.seq, row.state)
            for row in session.scalars(
                select(WriteOperationTransition)
                .where(WriteOperationTransition.operation_id == compensation_id)
                .order_by(WriteOperationTransition.seq)
            )
        )
        assert operation is not None and operation.status == "committed"
        assert transitions == (
            (1, "proposed"),
            (2, "approved"),
            (3, "claimed"),
            (4, "committed"),
        )
        return projected

    monkeypatch.setattr(repository, "_retract_signal_authorized_in_session", capture_claim)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    result = coordinator.execute(
        issuer.issue(
            application_id=1,
            signal_id=signal_id,
            parent_operation_id=proposed.operation_id,
        )
    )
    assert result.status == "committed"
    assert len(captured_claims) == 1
    with session_factory() as session:
        with pytest.raises(ProductActionIntegrityError) as replayed:
            original(
                session,
                signal_id=signal_id,
                expected_current_version_id=1,
                expected_signal_revision=1,
                parent_operation_id=proposed.operation_id,
                compensation_operation_id=compensation_id,
                authorization=object(),  # type: ignore[arg-type]
                authorization_binding=(),
                domain_claim=captured_claims[0],
            )
        assert replayed.value.code == "readiness_signal_compensation_domain_claim"
        session.rollback()
    with session_factory() as session:
        assert len(tuple(session.scalars(select(InterviewReadinessSignalVersion)))) == 2


def test_retraction_rejects_arbitrary_execution_binding_before_domain_mutation(
    tmp_path,
) -> None:
    session_factory = init_database(tmp_path / "raw-domain-binding.sqlite3")
    _seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    _issuer, coordinator = _compensation_stack(session_factory)
    repository = coordinator._readiness_repository
    compensation_id = _seed_signal_compensation_proposal(
        session_factory,
        parent_operation_id=proposed.operation_id,
    )
    binding = ("arbitrary",)
    authorization = repository._proof_registry._issue(
        ProductActionExecutionAuthorization,
        action_name="save_review_readiness_signal",
        binding=binding,
        publication_refreshable=False,
    )
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None and signal.current_version_id is not None
        with pytest.raises(ProductActionIntegrityError) as captured:
            repository.retract_signal_in_session(
                session,
                signal_id=signal.id,
                expected_current_version_id=signal.current_version_id,
                expected_signal_revision=signal.revision,
                parent_operation_id=proposed.operation_id,
                compensation_operation_id=compensation_id,
                authorization=authorization,
                authorization_binding=binding,
            )
        session.rollback()
    assert captured.value.code == "readiness_signal_compensation_authorization_binding"
    assert repository._proof_registry._records == {}
    assert repository._proof_registry._retired[authorization].state == "revoked"
    with session_factory() as session:
        assert len(tuple(session.scalars(select(InterviewReadinessSignalVersion)))) == 1


@pytest.mark.parametrize(
    "commit_route",
    ("connection_commit", "root_transaction_commit"),
)
def test_forged_execution_uow_cannot_persist_domain_through_connection_commit(
    tmp_path,
    commit_route,
) -> None:
    session_factory = init_database(tmp_path / f"connection-bypass-{commit_route}.sqlite3")
    _seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    _issuer, coordinator = _compensation_stack(session_factory)
    repository = coordinator._readiness_repository
    compensation_id = _seed_signal_compensation_proposal(
        session_factory,
        parent_operation_id=proposed.operation_id,
    )
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        operation = session.get(WriteOperation, compensation_id)
        assert signal is not None and signal.current_version_id is not None
        assert operation is not None
        binding = (
            "product_action_compensation_execution_v1",
            compensation_id,
            proposed.operation_id,
            operation.parent_terminal_payload_sha256,
            "undo:save_review_readiness_signal",
            canonical_product_action_json(
                {
                    "kind": "retract_review_readiness_signal_v1",
                    "signal_id": signal.id,
                    "created_version_id": signal.current_version_id,
                    "expected_current_version_id": signal.current_version_id,
                    "expected_signal_revision": signal.revision,
                    "parent_operation_id": proposed.operation_id,
                }
            ),
            "hmac-sha256:" + "1" * 64,
        )
        authorization = repository._proof_registry._issue(
            ProductActionExecutionAuthorization,
            action_name="save_review_readiness_signal",
            binding=binding,
            publication_refreshable=False,
        )
        forged_uow = object.__new__(_CompensationExecutionUowV1)
        with pytest.raises(ProductActionIntegrityError) as captured:
            repository.retract_signal_in_session(
                session,
                signal_id=signal.id,
                expected_current_version_id=signal.current_version_id,
                expected_signal_revision=signal.revision,
                parent_operation_id=proposed.operation_id,
                compensation_operation_id=compensation_id,
                authorization=authorization,
                authorization_binding=binding,
                execution_uow=forged_uow,
            )
        assert captured.value.code == "readiness_signal_compensation_execution_uow"
        connection = session.connection()
        if commit_route == "connection_commit":
            connection.commit()
        else:
            transaction = connection.get_transaction()
            assert transaction is not None
            transaction.commit()
    with session_factory() as session:
        versions = tuple(session.scalars(select(InterviewReadinessSignalVersion)))
        operation = session.get(WriteOperation, compensation_id)
    assert len(versions) == 1
    assert operation is not None and operation.status == "proposed"


def test_revoked_execution_authorization_is_not_accepted_as_consumed(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "revoked-not-consumed.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    repository = coordinator._readiness_repository
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id

    def revoke_and_return(
        _session,
        *,
        authorization,
        authorization_binding,
        **_kwargs,
    ):  # type: ignore[no-untyped-def]
        coordinator._execution_registry.revoke(authorization)
        return ReadinessSignalRetractionResultV1(signal_id, 999, 2)

    monkeypatch.setattr(repository, "retract_signal_in_session", revoke_and_return)
    proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    with pytest.raises(ProductActionIntegrityError) as captured:
        coordinator.execute(proof)
    assert captured.value.code == "product_action_compensation_authorization_unclaimed"


def test_unclaimed_mapped_stale_revokes_authorization_without_leak(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "stale-auth-revoke.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    repository = coordinator._readiness_repository
    captured_authorizations = []
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id

    def unclaimed_stale(
        _session,
        *,
        authorization,
        **_kwargs,
    ):  # type: ignore[no-untyped-def]
        captured_authorizations.append(authorization)
        raise ProductActionCompensationStale("readiness_signal_undo_stale")

    monkeypatch.setattr(repository, "retract_signal_in_session", unclaimed_stale)
    proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    with session_factory() as session:
        signal = session.get(InterviewReadinessSignal, signal_id)
        assert signal is not None
        signal.revision += 1
        session.commit()
    result = coordinator.execute(proof)
    assert result.status == "failed"
    assert len(captured_authorizations) == 1
    retired = coordinator._execution_registry._retired[captured_authorizations[0]]
    assert retired.state == "revoked"
    assert coordinator._execution_registry._records == {}


def test_signal_owner_issuer_short_circuits_capability_before_database_access() -> None:
    queried = False

    def forbidden_factory():
        nonlocal queried
        queried = True
        raise AssertionError("database must not be queried")

    registry = ProductActionCompensationProofRegistryV1()
    issuer = ReadinessSignalUndoIssuer(
        forbidden_factory,
        catalog=ProductActionCompensationCatalogV1(),
        proof_registry=registry,
        key_profiles=LedgerKeyProfileStoreV1(
            (KEY_ONE, KEY_TWO),
            active_key_id=KEY_ONE.key_id,
        ),
        capability_check=lambda _capability: False,
    )
    with pytest.raises(ProductActionCompensationError) as captured:
        issuer.issue(
            application_id=1,
            signal_id=1,
            parent_operation_id=PARENT_OPERATION_ID,
        )
    assert captured.value.code == "product_action_compensation_permission_denied"
    assert queried is False


def test_signal_compensation_is_owner_scoped_atomic_and_terminal_replay_safe(tmp_path) -> None:
    session_factory = init_database(tmp_path / "signal-compensation.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
        active_version_id = signal.current_version_id

    proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    with pytest.raises(TypeError):
        copy.copy(proof)
    with pytest.raises(TypeError):
        copy.deepcopy(proof)
    with pytest.raises(ProductActionCompensationError) as wrong_owner:
        issuer.issue(
            application_id=seeded["application_id"] + 1,
            signal_id=signal_id,
            parent_operation_id=proposed.operation_id,
        )
    assert wrong_owner.value.code == "product_action_compensation_not_found"

    result = coordinator.execute(proof)
    assert result.status == "committed"
    assert result.replayed is False
    assert dict(result.result) == {
        "kind": "review_readiness_signal_retracted_v1",
        "signal_id": signal_id,
        "retracted_version_id": result.result["retracted_version_id"],
        "signal_revision": 2,
    }
    compensation_id = product_action_compensation_operation_id(
        proposed.operation_id,
        "undo:save_review_readiness_signal",
    )
    assert result.operation_id == compensation_id

    with pytest.raises(ProductActionCompensationError) as reused:
        coordinator.execute(proof)
    assert reused.value.code == "product_action_compensation_proof_invalid"

    with session_factory() as session:
        signal = session.get(InterviewReadinessSignal, signal_id)
        operation = session.get(WriteOperation, compensation_id)
        transitions = tuple(
            session.scalars(
                select(WriteOperationTransition)
                .where(WriteOperationTransition.operation_id == compensation_id)
                .order_by(WriteOperationTransition.seq)
            )
        )
        versions_before_replay = tuple(
            session.scalars(
                select(InterviewReadinessSignalVersion)
                .where(InterviewReadinessSignalVersion.signal_id == signal_id)
                .order_by(InterviewReadinessSignalVersion.version_number)
            )
        )
    assert signal is not None and signal.current_version_id != active_version_id
    assert operation is not None
    assert operation.operation_role == "compensation"
    assert operation.adapter_kind == "compensation"
    assert operation.status == "committed"
    assert operation.parent_operation_id == proposed.operation_id
    assert operation.tool_call_id is None
    assert operation.conversation_id is None
    assert operation.agent_run_id is None
    assert operation.proposal_fingerprint is None
    assert operation.confirmation_token_fingerprint is None
    assert operation.authorization_scope_fingerprint is None
    assert operation.result_contract == "compensation_json_v1"
    assert operation.undo_json is None
    assert operation.delivery_status == "not_applicable"
    assert operation.delivery_outcome == "none"
    assert operation.delivery_message_count == 0
    assert [(item.seq, item.state) for item in transitions] == [
        (1, "proposed"),
        (2, "approved"),
        (3, "claimed"),
        (4, "committed"),
    ]
    assert operation.visible_result == "已撤销准备重点，并保留历史版本。"
    assert json.loads(operation.transport_json or "null") == {
        "schema_version": 1,
        "operation_id": compensation_id,
        "compensation_kind": "undo:save_review_readiness_signal",
        "status": "committed",
        "result": dict(result.result),
    }
    assert "下次练习" not in (operation.result_json or "")
    assert "下次练习" not in (operation.visible_result or "")
    assert "下次练习" not in (operation.transport_json or "")

    replay_proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    replay = coordinator.execute(replay_proof)
    assert replay.status == "committed"
    assert replay.replayed is True
    assert dict(replay.result) == dict(result.result)
    with session_factory() as session:
        versions_after_replay = tuple(
            session.scalars(
                select(InterviewReadinessSignalVersion).where(
                    InterviewReadinessSignalVersion.signal_id == signal_id
                )
            )
        )
    assert len(versions_after_replay) == len(versions_before_replay) == 2


def test_unmapped_signal_executor_error_rolls_back_to_exact_proposed(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "signal-compensation-error.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    repository = coordinator._readiness_repository
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )

    def fail_unmapped(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("sensitive executor failure")

    monkeypatch.setattr(repository, "retract_signal_in_session", fail_unmapped)
    coordinator._readiness_repository = repository
    with pytest.raises(RuntimeError, match="sensitive executor failure"):
        coordinator.execute(proof)

    compensation_id = product_action_compensation_operation_id(
        proposed.operation_id,
        "undo:save_review_readiness_signal",
    )
    with session_factory() as session:
        operation = session.get(WriteOperation, compensation_id)
        transitions = tuple(
            session.scalars(
                select(WriteOperationTransition)
                .where(WriteOperationTransition.operation_id == compensation_id)
                .order_by(WriteOperationTransition.seq)
            )
        )
        versions = tuple(
            session.scalars(
                select(InterviewReadinessSignalVersion).where(
                    InterviewReadinessSignalVersion.signal_id == signal_id
                )
            )
        )
    assert operation is not None and operation.status == "proposed"
    assert [(item.seq, item.state) for item in transitions] == [(1, "proposed")]
    assert len(versions) == 1
    assert coordinator._proof_registry._retired[proof] == "revoked"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("failure_category", "stale_state"),
        ("failure_code", "corrupt"),
        ("approved_at", datetime(2026, 8, 31, tzinfo=timezone.utc)),
        ("claimed_at", datetime(2026, 8, 31, tzinfo=timezone.utc)),
        ("rejected_at", datetime(2026, 8, 31, tzinfo=timezone.utc)),
        ("committed_at", datetime(2026, 8, 31, tzinfo=timezone.utc)),
        ("failed_at", datetime(2026, 8, 31, tzinfo=timezone.utc)),
        ("delivered_at", datetime(2026, 8, 31, tzinfo=timezone.utc)),
        ("delivery_owner_token_fingerprint", "hmac-sha256:" + "1" * 64),
        ("delivery_lease_expires_at", 1),
        ("delivery_manifest_sha256", "sha256:" + "1" * 64),
        ("delivery_next_operation_id", "__parent_operation__"),
        ("delivery_failure_code", "corrupt"),
        ("delivery_outcome", "none"),
        ("delivery_message_count", 0),
    ),
)
def test_proposed_ledger_rejects_each_nullable_terminal_and_delivery_field(
    tmp_path,
    monkeypatch,
    field,
    value,
) -> None:
    session_factory = init_database(tmp_path / f"proposed-shape-{field}.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    repository = coordinator._readiness_repository
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id

    def leave_proposed(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("leave exact proposed")

    monkeypatch.setattr(repository, "retract_signal_in_session", leave_proposed)
    with pytest.raises(RuntimeError):
        coordinator.execute(
            issuer.issue(
                application_id=seeded["application_id"],
                signal_id=signal_id,
                parent_operation_id=proposed.operation_id,
            )
        )
    monkeypatch.undo()
    operation_id = product_action_compensation_operation_id(
        proposed.operation_id,
        "undo:save_review_readiness_signal",
    )
    with session_factory() as session:
        operation = session.get(WriteOperation, operation_id)
        assert operation is not None and operation.status == "proposed"
        session.execute(text("PRAGMA ignore_check_constraints=ON"))
        if field == "delivery_next_operation_id":
            session.execute(
                text("DROP TRIGGER IF EXISTS trg_write_operation_chained_delivery")
            )
        setattr(
            operation,
            field,
            proposed.operation_id if value == "__parent_operation__" else value,
        )
        session.commit()
    proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    with pytest.raises(ProductActionIntegrityError) as captured:
        coordinator.execute(proof)
    assert captured.value.code == "product_action_compensation_proposed_shape"


@pytest.mark.parametrize("stage", ("before_proposal", "before_execution"))
def test_active_version_cardinality_is_exact_before_each_uow(
    tmp_path,
    monkeypatch,
    stage,
) -> None:
    session_factory = init_database(tmp_path / f"active-cardinality-{stage}.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    repository = coordinator._readiness_repository
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    if stage == "before_execution":
        def leave_proposed(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("leave proposed")

        monkeypatch.setattr(repository, "retract_signal_in_session", leave_proposed)
        with pytest.raises(RuntimeError):
            coordinator.execute(
                issuer.issue(
                    application_id=seeded["application_id"],
                    signal_id=signal_id,
                    parent_operation_id=proposed.operation_id,
                )
            )
        monkeypatch.undo()
    proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    _append_exact_later_signal_version(
        session_factory,
        signal_id,
        advance_revision=False,
        point_current=False,
    )
    with pytest.raises(ProductActionIntegrityError) as captured:
        coordinator.execute(proof)
    assert captured.value.code == "product_action_compensation_owner_cardinality"


def test_mapped_signal_stale_closes_failed_without_domain_mutation(tmp_path) -> None:
    session_factory = init_database(tmp_path / "signal-compensation-stale.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )

    with session_factory() as session:
        signal = session.get(InterviewReadinessSignal, signal_id)
        assert signal is not None
        signal.revision += 1
        session.commit()
    result = coordinator.execute(proof)
    assert result.status == "failed"
    assert result.replayed is False
    assert dict(result.result) == {
        "kind": "review_readiness_signal_retracted_v1",
        "code": "readiness_signal_undo_stale",
    }
    compensation_id = product_action_compensation_operation_id(
        proposed.operation_id,
        "undo:save_review_readiness_signal",
    )
    with session_factory() as session:
        operation = session.get(WriteOperation, compensation_id)
        transitions = tuple(
            session.scalars(
                select(WriteOperationTransition)
                .where(WriteOperationTransition.operation_id == compensation_id)
                .order_by(WriteOperationTransition.seq)
            )
        )
        versions = tuple(
            session.scalars(
                select(InterviewReadinessSignalVersion).where(
                    InterviewReadinessSignalVersion.signal_id == signal_id
                )
            )
        )
    assert operation is not None and operation.status == "failed"
    assert operation.failure_category == "stale_state"
    assert operation.failure_code == "readiness_signal_undo_stale"
    assert [(item.seq, item.state) for item in transitions] == [
        (1, "proposed"),
        (2, "approved"),
        (3, "claimed"),
        (4, "failed"),
    ]
    assert len(versions) == 1


@pytest.mark.parametrize("drift", ("revision", "pointer", "pointer_revision"))
def test_exact_signal_cas_drift_reconciles_failed_terminal_and_replay(
    tmp_path,
    drift,
) -> None:
    session_factory = init_database(tmp_path / f"signal-stale-{drift}.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    if drift == "revision":
        with session_factory() as session:
            signal = session.get(InterviewReadinessSignal, signal_id)
            assert signal is not None
            signal.revision += 1
            session.commit()
    else:
        _append_exact_later_signal_version(
            session_factory,
            signal_id,
            advance_revision=drift == "pointer_revision",
        )
    result = coordinator.execute(proof)
    assert result.status == "failed"
    assert result.replayed is False
    replay = coordinator.execute(
        issuer.issue(
            application_id=seeded["application_id"],
            signal_id=signal_id,
            parent_operation_id=proposed.operation_id,
        )
    )
    assert replay.status == "failed"
    assert replay.replayed is True
    with session_factory() as session:
        versions = tuple(
            session.scalars(
                select(InterviewReadinessSignalVersion).where(
                    InterviewReadinessSignalVersion.signal_id == signal_id
                )
            )
        )
    assert len(versions) == (1 if drift == "revision" else 2)


def test_owner_issuer_rejects_parent_with_corrupt_transition_prefix(tmp_path) -> None:
    session_factory = init_database(tmp_path / "corrupt-parent-prefix.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, _coordinator = _compensation_stack(session_factory)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
        session.execute(text("DROP TRIGGER IF EXISTS trg_write_operation_transition_delete"))
        session.execute(
            delete(WriteOperationTransition).where(
                WriteOperationTransition.operation_id == proposed.operation_id,
                WriteOperationTransition.seq == 3,
            )
        )
        session.commit()
    with pytest.raises(ProductActionIntegrityError) as captured:
        issuer.issue(
            application_id=seeded["application_id"],
            signal_id=signal_id,
            parent_operation_id=proposed.operation_id,
        )
    assert captured.value.code == "product_action_parent_transition_prefix"


def test_execution_partial_prefix_fails_closed_without_repair_or_second_executor(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "partial-compensation.sqlite3")
    seeded, proposed, repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    repository = coordinator._readiness_repository
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    first_proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    executions = 0

    def fail_once(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal executions
        executions += 1
        raise RuntimeError("executor must remain single-shot")

    monkeypatch.setattr(repository, "retract_signal_in_session", fail_once)
    coordinator._readiness_repository = repository
    with pytest.raises(RuntimeError, match="single-shot"):
        coordinator.execute(first_proof)
    compensation_id = product_action_compensation_operation_id(
        proposed.operation_id,
        "undo:save_review_readiness_signal",
    )
    with session_factory() as session:
        session.execute(text("DROP TRIGGER IF EXISTS trg_write_operation_transition_delete"))
        session.execute(
            delete(WriteOperationTransition).where(
                WriteOperationTransition.operation_id == compensation_id,
                WriteOperationTransition.seq == 1,
            )
        )
        session.commit()
    second_proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    with pytest.raises(ProductActionIntegrityError) as captured:
        coordinator.execute(second_proof)
    assert captured.value.code == "partial_product_action_compensation"
    assert executions == 1
    with session_factory() as session:
        assert session.get(WriteOperation, compensation_id) is not None
        assert tuple(
            session.scalars(
                select(WriteOperationTransition).where(
                    WriteOperationTransition.operation_id == compensation_id
                )
            )
        ) == ()


def test_execution_fresh_reconciliation_distinguishes_absent_proposed_unreadable(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "execution-reconcile-truth.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    repository = coordinator._readiness_repository
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    record = coordinator._proof_registry.peek_signal(proof)
    compensation_id = product_action_compensation_operation_id(
        proposed.operation_id,
        "undo:save_review_readiness_signal",
    )
    with pytest.raises(ProductActionIntegrityError) as absent:
        coordinator._reconcile_execution(record, compensation_id)
    assert absent.value.code == "product_action_compensation_execution_absent"

    def leave_proposed(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("leave proposed")

    monkeypatch.setattr(repository, "retract_signal_in_session", leave_proposed)
    with pytest.raises(RuntimeError):
        coordinator.execute(proof)
    with pytest.raises(ProductActionCompensationError) as proposed_state:
        coordinator._reconcile_execution(record, compensation_id)
    assert proposed_state.value.code == "operation_result_unknown"
    original_load = coordinator._load_state

    def unreadable(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OperationalError("SELECT", {}, RuntimeError("unreadable"))

    monkeypatch.setattr(coordinator, "_load_state", unreadable)
    with pytest.raises(ProductActionCompensationError) as unreadable_state:
        coordinator._reconcile_execution(record, compensation_id)
    assert unreadable_state.value.code == "operation_result_unknown"
    monkeypatch.setattr(coordinator, "_load_state", original_load)


def test_proposal_commit_unknown_reconciles_without_second_domain_execution(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "proposal-commit-unknown.sqlite3")
    seeded, proposed, repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    repository = coordinator._readiness_repository
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    original_commit = Session.commit
    commits = 0
    executions = 0
    original_retract = repository.retract_signal_in_session

    def commit_then_raise_once(self):  # type: ignore[no-untyped-def]
        nonlocal commits
        commits += 1
        original_commit(self)
        if commits == 1:
            raise OperationalError("COMMIT", {}, RuntimeError("response lost"))

    def counted_retract(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal executions
        executions += 1
        return original_retract(*args, **kwargs)

    monkeypatch.setattr(Session, "commit", commit_then_raise_once)
    monkeypatch.setattr(repository, "retract_signal_in_session", counted_retract)
    coordinator._readiness_repository = repository
    result = coordinator.execute(proof)
    assert result.status == "committed"
    assert executions == 1
    with session_factory() as session:
        versions = tuple(
            session.scalars(
                select(InterviewReadinessSignalVersion).where(
                    InterviewReadinessSignalVersion.signal_id == signal_id
                )
            )
        )
    assert len(versions) == 2


def test_existing_proposal_uses_persisted_key_after_active_rotation(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "compensation-key-rotation.sqlite3")
    seeded, proposed, repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    repository = coordinator._readiness_repository
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    first = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )

    def fail(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("leave exact proposed")

    monkeypatch.setattr(repository, "retract_signal_in_session", fail)
    coordinator._readiness_repository = repository
    with pytest.raises(RuntimeError, match="leave exact proposed"):
        coordinator.execute(first)
    coordinator._key_profiles.activate(KEY_TWO.key_id)
    monkeypatch.undo()
    second = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    result = coordinator.execute(second)
    assert result.status == "committed"
    with session_factory() as session:
        operation = session.get(WriteOperation, result.operation_id)
    assert operation is not None
    assert operation.fingerprint_key_id == KEY_ONE.key_id


def test_twenty_way_race_has_one_signal_executor_and_one_compensation(tmp_path) -> None:
    session_factory = init_database(tmp_path / "compensation-race.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    proofs = tuple(
        issuer.issue(
            application_id=seeded["application_id"],
            signal_id=signal_id,
            parent_operation_id=proposed.operation_id,
        )
        for _ in range(20)
    )
    executions = 0
    counter_lock = Lock()
    original = coordinator._readiness_repository.retract_signal_in_session

    def counted(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal executions
        with counter_lock:
            executions += 1
        return original(*args, **kwargs)

    coordinator._readiness_repository.retract_signal_in_session = counted

    def run(proof):  # type: ignore[no-untyped-def]
        try:
            return coordinator.execute(proof).status
        except ProductActionCompensationError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=20) as pool:
        outcomes = tuple(pool.map(run, proofs))
    assert outcomes.count("committed") == 20
    assert executions == 1
    compensation_id = product_action_compensation_operation_id(
        proposed.operation_id,
        "undo:save_review_readiness_signal",
    )
    with session_factory() as session:
        operations = tuple(
            session.scalars(
                select(WriteOperation).where(
                    WriteOperation.id == compensation_id,
                    WriteOperation.operation_role == "compensation",
                )
            )
        )
        versions = tuple(
            session.scalars(
                select(InterviewReadinessSignalVersion).where(
                    InterviewReadinessSignalVersion.signal_id == signal_id
                )
            )
        )
    assert len(operations) == 1
    assert len(versions) == 2


def test_signal_compensation_never_enters_provider_or_agent_fallback(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "compensation-no-fallback.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    provider_calls = 0
    agent_fallback_calls = 0

    def forbidden_provider(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("compensation must remain provider-free")

    def forbidden_agent_fallback(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal agent_fallback_calls
        agent_fallback_calls += 1
        raise AssertionError("compensation must not enter Agent fallback")

    monkeypatch.setattr(ConfiguredAIClient, "complete", forbidden_provider)
    monkeypatch.setattr(CompensationHandlerRegistry, "resolve", forbidden_agent_fallback)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    result = coordinator.execute(
        issuer.issue(
            application_id=seeded["application_id"],
            signal_id=signal_id,
            parent_operation_id=proposed.operation_id,
        )
    )
    assert result.status == "committed"
    assert provider_calls == 0
    assert agent_fallback_calls == 0


@pytest.mark.parametrize(
    "scenario",
    ("success", "mapped_failed", "terminal_replay", "commit_unknown"),
)
def test_compensation_never_calls_conversation_pending_or_journal_writers(
    tmp_path,
    monkeypatch,
    scenario,
) -> None:
    session_factory = init_database(tmp_path / f"no-adjacent-writers-{scenario}.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    calls = {"conversation": 0, "pending": 0, "journal_store": 0, "journal_writer": 0}

    def forbidden(category):  # type: ignore[no-untyped-def]
        def call(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            calls[category] += 1
            raise AssertionError(f"compensation called adjacent {category} writer")

        return call

    for method_name in (
        "set_last_write_undo",
        "clear_last_write_undo",
        "clear_last_write_undo_if_matches",
    ):
        monkeypatch.setattr(
            ChatRepository,
            method_name,
            forbidden("conversation"),
        )
    for method_name in (
        "set_pending_action",
        "persist_pending_action",
        "clear_pending_action",
        "resolve_pending_confirmation",
        "replace_pending_confirmation",
    ):
        monkeypatch.setattr(ChatRepository, method_name, forbidden("pending"))
    for method_name in (
        "create_run_and_initial_segment",
        "append_event",
        "append_event_bound",
    ):
        monkeypatch.setattr(
            AgentRunRepository,
            method_name,
            forbidden("journal_store"),
        )
    for method_name in (
        "append_event",
        "append_prepared_event_bound",
        "record_approval_and_resume_bound",
    ):
        monkeypatch.setattr(
            SafeRunRecorder,
            method_name,
            forbidden("journal_writer"),
        )

    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    if scenario == "mapped_failed":
        with session_factory() as session:
            signal = session.get(InterviewReadinessSignal, signal_id)
            assert signal is not None
            signal.revision += 1
            session.commit()
    if scenario == "commit_unknown":
        original_commit = Session.commit
        commits = 0

        def commit_then_lose(self):  # type: ignore[no-untyped-def]
            nonlocal commits
            commits += 1
            original_commit(self)
            if commits == 2:
                raise OperationalError("COMMIT", {}, RuntimeError("response lost"))

        monkeypatch.setattr(Session, "commit", commit_then_lose)
    result = coordinator.execute(proof)
    if scenario == "mapped_failed":
        assert result.status == "failed"
    else:
        assert result.status == "committed"
    if scenario == "commit_unknown":
        assert result.replayed is True
    if scenario == "terminal_replay":
        replay = coordinator.execute(
            issuer.issue(
                application_id=seeded["application_id"],
                signal_id=signal_id,
                parent_operation_id=proposed.operation_id,
            )
        )
        assert replay.status == "committed" and replay.replayed is True
    assert calls == {
        "conversation": 0,
        "pending": 0,
        "journal_store": 0,
        "journal_writer": 0,
    }


def test_concurrent_terminal_proposal_reconciliation_never_reexecutes_domain(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "terminal-proposal-race.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    first = coordinator.execute(
        issuer.issue(
            application_id=seeded["application_id"],
            signal_id=signal_id,
            parent_operation_id=proposed.operation_id,
        )
    )
    assert first.status == "committed"
    proofs = tuple(
        issuer.issue(
            application_id=seeded["application_id"],
            signal_id=signal_id,
            parent_operation_id=proposed.operation_id,
        )
        for _ in range(20)
    )
    domain_calls = 0

    def forbidden_domain(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal domain_calls
        domain_calls += 1
        raise AssertionError("terminal reconciliation must not execute domain")

    monkeypatch.setattr(
        coordinator._readiness_repository,
        "retract_signal_in_session",
        forbidden_domain,
    )
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = tuple(pool.map(coordinator.execute, proofs))
    assert all(result.status == "committed" and result.replayed for result in results)
    assert domain_calls == 0


def test_execution_commit_unknown_replays_terminal_without_second_executor(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "execution-commit-unknown.sqlite3")
    seeded, proposed, repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    repository = coordinator._readiness_repository
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    original_commit = Session.commit
    commits = 0
    executions = 0
    original_retract = repository.retract_signal_in_session

    def commit_then_lose_execution_response(self):  # type: ignore[no-untyped-def]
        nonlocal commits
        commits += 1
        original_commit(self)
        if commits == 2:
            raise OperationalError("COMMIT", {}, RuntimeError("execution response lost"))

    def counted(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal executions
        executions += 1
        return original_retract(*args, **kwargs)

    monkeypatch.setattr(Session, "commit", commit_then_lose_execution_response)
    monkeypatch.setattr(repository, "retract_signal_in_session", counted)
    coordinator._readiness_repository = repository
    result = coordinator.execute(proof)
    assert result.status == "committed"
    assert result.replayed is True
    assert executions == 1


def test_failed_terminal_commit_unknown_reconciles_exact_drift(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "failed-commit-unknown.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    with session_factory() as session:
        signal = session.get(InterviewReadinessSignal, signal_id)
        assert signal is not None
        signal.revision += 1
        session.commit()
    original_commit = Session.commit
    commits = 0

    def commit_then_lose_failed_response(self):  # type: ignore[no-untyped-def]
        nonlocal commits
        commits += 1
        original_commit(self)
        if commits == 2:
            raise OperationalError("COMMIT", {}, RuntimeError("failed response lost"))

    monkeypatch.setattr(Session, "commit", commit_then_lose_failed_response)
    result = coordinator.execute(proof)
    assert result.status == "failed"
    assert result.replayed is True
    assert result.result["code"] == "readiness_signal_undo_stale"


def test_execution_commit_unknown_rejects_identical_active_retracted_tamper(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "execution-unknown-tamper.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    original_commit = Session.commit
    commits = 0

    def commit_tamper_then_lose(self):  # type: ignore[no-untyped-def]
        nonlocal commits
        commits += 1
        original_commit(self)
        if commits != 2:
            return
        engine = session_factory.kw["bind"]
        raw = engine.raw_connection()
        try:
            cursor = raw.cursor()
            cursor.execute(
                "DROP TRIGGER IF EXISTS "
                "trg_interview_readiness_signal_version_immutable"
            )
            cursor.execute(
                """
                UPDATE interview_readiness_signal_versions
                SET user_note=user_note || ' identical commit-unknown tamper'
                WHERE signal_id=?
                """,
                (signal_id,),
            )
            raw.commit()
        finally:
            raw.close()
        raise OperationalError("COMMIT", {}, RuntimeError("tampered response lost"))

    monkeypatch.setattr(Session, "commit", commit_tamper_then_lose)
    with pytest.raises(ProductActionIntegrityError) as captured:
        coordinator.execute(proof)
    assert captured.value.code == "product_action_compensation_active_aggregate"


def test_cross_container_and_revoked_capability_proofs_fail_before_execution(tmp_path) -> None:
    session_factory = init_database(tmp_path / "proof-boundary.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    allowed = True

    def capability(_capability: str) -> bool:
        return allowed

    issuer, coordinator = _compensation_stack(
        session_factory,
        capability_check=capability,
    )
    _other_issuer, other_coordinator = _compensation_stack(session_factory)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    cross = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    with pytest.raises(ProductActionCompensationError) as cross_error:
        other_coordinator.execute(cross)
    assert cross_error.value.code == "product_action_compensation_proof_invalid"
    revoked = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    allowed = False
    with pytest.raises(ProductActionCompensationError) as denied:
        coordinator.execute(revoked)
    assert denied.value.code == "product_action_compensation_permission_denied"
    allowed = True
    with pytest.raises(ProductActionCompensationError) as reused:
        coordinator.execute(revoked)
    assert reused.value.code == "product_action_compensation_proof_invalid"


def test_capability_toctou_is_rechecked_under_lock_before_compensation_query(tmp_path) -> None:
    session_factory = init_database(tmp_path / "capability-toctou.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    checks = 0

    def capability(_capability: str) -> bool:
        nonlocal checks
        checks += 1
        return checks <= 2

    issuer, coordinator = _compensation_stack(
        session_factory,
        capability_check=capability,
    )
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
    proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal.id,
        parent_operation_id=proposed.operation_id,
    )
    with pytest.raises(ProductActionCompensationError) as captured:
        coordinator.execute(proof)
    assert captured.value.code == "product_action_compensation_permission_denied"
    compensation_id = product_action_compensation_operation_id(
        proposed.operation_id,
        "undo:save_review_readiness_signal",
    )
    with session_factory() as session:
        assert session.get(WriteOperation, compensation_id) is None
        assert tuple(
            session.scalars(
                select(WriteOperationTransition).where(
                    WriteOperationTransition.operation_id == compensation_id
                )
            )
        ) == ()


@pytest.mark.parametrize("variant", ("noncanonical", "duplicate"))
def test_parent_persisted_json_must_be_duplicate_free_canonical(tmp_path, variant) -> None:
    session_factory = init_database(tmp_path / f"parent-{variant}.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, _coordinator = _compensation_stack(session_factory)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        parent = session.get(WriteOperation, proposed.operation_id)
        assert signal is not None and parent is not None and parent.result_json is not None
        session.execute(text("DROP TRIGGER IF EXISTS trg_write_operation_terminal_immutable"))
        if variant == "noncanonical":
            parent.result_json = parent.result_json.replace(":", ": ", 1)
        else:
            member = '"action_name":"save_review_readiness_signal"'
            parent.result_json = parent.result_json.replace(member, member + "," + member, 1)
        session.commit()
    with pytest.raises(ProductActionIntegrityError):
        issuer.issue(
            application_id=seeded["application_id"],
            signal_id=signal.id,
            parent_operation_id=proposed.operation_id,
        )


def test_execution_serialization_failure_rolls_back_domain_and_seq2_to_seq4(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "serialization-rollback.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )

    def serialization_failure(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise ValueError("closed projector failure")

    monkeypatch.setattr(
        "offerpilot.product_actions.compensation.build_terminal_payload",
        serialization_failure,
    )
    with pytest.raises(ValueError, match="closed projector failure"):
        coordinator.execute(proof)
    compensation_id = product_action_compensation_operation_id(
        proposed.operation_id,
        "undo:save_review_readiness_signal",
    )
    with session_factory() as session:
        operation = session.get(WriteOperation, compensation_id)
        transitions = tuple(
            session.scalars(
                select(WriteOperationTransition)
                .where(WriteOperationTransition.operation_id == compensation_id)
                .order_by(WriteOperationTransition.seq)
            )
        )
        versions = tuple(
            session.scalars(
                select(InterviewReadinessSignalVersion).where(
                    InterviewReadinessSignalVersion.signal_id == signal_id
                )
            )
        )
    assert operation is not None and operation.status == "proposed"
    assert [(row.seq, row.state) for row in transitions] == [(1, "proposed")]
    assert len(versions) == 1


def test_terminal_replay_proof_rejects_later_edit_aba_and_domain_corruption(tmp_path) -> None:
    session_factory = init_database(tmp_path / "terminal-aba.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    committed = coordinator.execute(
        issuer.issue(
            application_id=seeded["application_id"],
            signal_id=signal_id,
            parent_operation_id=proposed.operation_id,
        )
    )
    replay_proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    with session_factory() as session:
        signal = session.get(InterviewReadinessSignal, signal_id)
        assert signal is not None and signal.current_version_id is not None
        retracted = session.get(
            InterviewReadinessSignalVersion,
            signal.current_version_id,
        )
        assert retracted is not None
        session.execute(
            text(
                "DROP TRIGGER IF EXISTS "
                "trg_interview_readiness_signal_version_immutable"
            )
        )
        retracted.user_note += "tampered"
        session.commit()
    with pytest.raises(ProductActionIntegrityError):
        coordinator.execute(replay_proof)
    with session_factory() as session:
        operation = session.get(WriteOperation, committed.operation_id)
        assert operation is not None and operation.status == "committed"


def test_active_aggregate_corruption_wins_over_legitimate_pointer_revision_stale(
    tmp_path,
) -> None:
    session_factory = init_database(tmp_path / "active-corruption-stale.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None and signal.current_version_id is not None
        signal_id = signal.id
    proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    with session_factory() as session:
        signal = session.get(InterviewReadinessSignal, signal_id)
        assert signal is not None and signal.current_version_id is not None
        active = session.get(InterviewReadinessSignalVersion, signal.current_version_id)
        assert active is not None
        session.execute(
            text(
                "DROP TRIGGER IF EXISTS "
                "trg_interview_readiness_signal_version_immutable"
            )
        )
        active.user_note += "same-shape-corruption"
        signal.revision += 1
        session.commit()
    with pytest.raises(ProductActionIntegrityError) as captured:
        coordinator.execute(proof)
    assert captured.value.code == "product_action_compensation_active_aggregate"


def test_terminal_replay_rejects_identical_active_retracted_tamper(tmp_path) -> None:
    session_factory = init_database(tmp_path / "identical-terminal-tamper.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    coordinator.execute(
        issuer.issue(
            application_id=seeded["application_id"],
            signal_id=signal_id,
            parent_operation_id=proposed.operation_id,
        )
    )
    replay_proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    with session_factory() as session:
        versions = tuple(
            session.scalars(
                select(InterviewReadinessSignalVersion)
                .where(InterviewReadinessSignalVersion.signal_id == signal_id)
                .order_by(InterviewReadinessSignalVersion.version_number)
            )
        )
        assert len(versions) == 2
        session.execute(
            text(
                "DROP TRIGGER IF EXISTS "
                "trg_interview_readiness_signal_version_immutable"
            )
        )
        for version in versions:
            version.user_note += "identical-corruption"
        session.commit()
    with pytest.raises(ProductActionIntegrityError) as captured:
        coordinator.execute(replay_proof)
    assert captured.value.code == "product_action_compensation_active_aggregate"


def test_terminal_replay_rejects_extra_version_and_pointer_away_back_aba(tmp_path) -> None:
    session_factory = init_database(tmp_path / "terminal-extra-version.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    coordinator.execute(
        issuer.issue(
            application_id=seeded["application_id"],
            signal_id=signal_id,
            parent_operation_id=proposed.operation_id,
        )
    )
    replay_proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    with session_factory() as session:
        signal = session.get(InterviewReadinessSignal, signal_id)
        assert signal is not None and signal.current_version_id is not None
        original_current = signal.current_version_id
        original_revision = signal.revision
        current = session.get(InterviewReadinessSignalVersion, original_current)
        assert current is not None
        values = (
            signal_id,
            current.version_number + 1,
            current.id,
            current.statement_text,
            current.user_note,
            current.source_note_revision,
            current.source_note_fingerprint,
            current.source_proposal_hash,
            current.candidate_fingerprint,
        )
    engine = session_factory.kw["bind"]
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute(
            """
            INSERT INTO interview_readiness_signal_versions(
              signal_id,version_number,parent_version_id,disposition,schema_version,
              statement_text,user_note,source_note_revision,source_note_fingerprint,
              source_proposal_hash,candidate_fingerprint,domain_idempotency_key,
              write_operation_id,created_at
            ) VALUES (?,?,?,'retracted','readiness-signal-v1',?,?,?,?,?,?,?,?,'2026-08-31')
            """,
            (*values, "99999999-9999-4999-8999-999999999998", "99999999-9999-4999-8999-999999999997"),
        )
        extra_id = cursor.lastrowid
        raw.commit()
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        raw.close()
    with session_factory() as session:
        signal = session.get(InterviewReadinessSignal, signal_id)
        assert signal is not None
        signal.current_version_id = extra_id
        signal.revision = original_revision + 1
        session.commit()
    with session_factory() as session:
        signal = session.get(InterviewReadinessSignal, signal_id)
        assert signal is not None
        signal.current_version_id = original_current
        signal.revision = original_revision
        session.commit()
    with pytest.raises(ProductActionIntegrityError) as captured:
        coordinator.execute(replay_proof)
    assert captured.value.code == "product_action_compensation_terminal_domain"


def test_signal_undo_survives_note_event_and_proposal_source_deletion(tmp_path) -> None:
    session_factory = init_database(tmp_path / "all-source-deletion.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    with session_factory() as session:
        session.execute(
            delete(InterviewReviewProposal).where(
                InterviewReviewProposal.id == seeded["proposal_id"]
            )
        )
        session.execute(delete(InterviewNote).where(InterviewNote.id == seeded["note_id"]))
        session.execute(
            delete(ApplicationEvent).where(ApplicationEvent.id == seeded["event_id"])
        )
        session.commit()
    result = coordinator.execute(proof)
    assert result.status == "committed"


@pytest.mark.parametrize(
    ("sizes", "valid"),
    (
        ((4_096, 1_024, 4_096, 3_072), True),
        ((4_097, 0, 0, 0), False),
        ((0, 1_025, 0, 0), False),
        ((0, 0, 4_097, 0), False),
        ((4_096, 1_024, 4_096, 3_073), False),
    ),
)
def test_compensation_terminal_budget_boundaries(sizes, valid) -> None:
    values = tuple("x" * size for size in sizes)
    if valid:
        _enforce_compensation_terminal_budgets(*values)
    else:
        with pytest.raises(ProductActionIntegrityError) as captured:
            _enforce_compensation_terminal_budgets(*values)
        assert captured.value.code == "product_action_compensation_terminal_budget"


@pytest.mark.parametrize("constant", ("NaN", "Infinity", "-Infinity"))
def test_persisted_json_rejects_nonfinite_constants_as_integrity(constant) -> None:
    with pytest.raises(ProductActionIntegrityError) as captured:
        _decode_json_object('{"value":' + constant + "}", "persisted_json")
    assert captured.value.code == "persisted_json"


def test_proposal_commit_unknown_all_absent_rebuilds_once_before_executor(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "proposal-absent-rebuild.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    repository = coordinator._readiness_repository
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    original_commit = Session.commit
    commits = 0
    executions = 0
    original_retract = repository.retract_signal_in_session

    def lose_before_first_commit(self):  # type: ignore[no-untyped-def]
        nonlocal commits
        commits += 1
        if commits == 1:
            self.rollback()
            raise OperationalError("COMMIT", {}, RuntimeError("not committed"))
        return original_commit(self)

    def counted(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal executions
        executions += 1
        return original_retract(*args, **kwargs)

    monkeypatch.setattr(Session, "commit", lose_before_first_commit)
    monkeypatch.setattr(repository, "retract_signal_in_session", counted)
    assert coordinator.execute(proof).status == "committed"
    assert executions == 1


def test_unreadable_proposal_reconciliation_returns_unknown_without_executor(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "proposal-unreadable.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    original_commit = Session.commit
    original_load = coordinator._load_state
    loads = 0

    def commit_then_lose(self):  # type: ignore[no-untyped-def]
        original_commit(self)
        raise OperationalError("COMMIT", {}, RuntimeError("response lost"))

    def unreadable_on_fresh_read(session, operation_id):  # type: ignore[no-untyped-def]
        nonlocal loads
        loads += 1
        if loads == 3:
            raise OperationalError("SELECT", {}, RuntimeError("database unreadable"))
        return original_load(session, operation_id)

    monkeypatch.setattr(Session, "commit", commit_then_lose)
    monkeypatch.setattr(coordinator, "_load_state", unreadable_on_fresh_read)
    with pytest.raises(ProductActionCompensationError) as captured:
        coordinator.execute(proof)
    assert captured.value.code == "operation_result_unknown"
    assert captured.value.retryable is True


def test_proposed_compensation_survives_parent_evidence_corruption_and_cap(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "evidence-corruption.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    repository = coordinator._readiness_repository
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    first = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )

    def leave_proposed(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("leave proposed")

    monkeypatch.setattr(repository, "retract_signal_in_session", leave_proposed)
    with pytest.raises(RuntimeError, match="leave proposed"):
        coordinator.execute(first)
    monkeypatch.undo()
    second = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    with session_factory() as session:
        evidence = session.scalar(select(InterviewReadinessSignalEvidence))
        assert evidence is not None
        evidence.excerpt = "x" * 8_193
        evidence.excerpt_sha256 = "sha256:" + hashlib.sha256(
            evidence.excerpt.encode()
        ).hexdigest()
        session.commit()
    with pytest.raises(ProductActionIntegrityError):
        coordinator.execute(second)
    compensation_id = product_action_compensation_operation_id(
        proposed.operation_id,
        "undo:save_review_readiness_signal",
    )
    with session_factory() as session:
        operation = session.get(WriteOperation, compensation_id)
        versions = tuple(
            session.scalars(
                select(InterviewReadinessSignalVersion).where(
                    InterviewReadinessSignalVersion.signal_id == signal_id
                )
            )
        )
    assert operation is not None and operation.status == "proposed"
    assert len(versions) == 1


def test_execution_dbapi_failure_rolls_back_to_proposed_without_domain_write(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "execution-dbapi.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    repository = coordinator._readiness_repository
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )

    def unavailable(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OperationalError("INSERT", {}, RuntimeError("database unavailable"))

    monkeypatch.setattr(repository, "retract_signal_in_session", unavailable)
    with pytest.raises(OperationalError):
        coordinator.execute(proof)
    compensation_id = product_action_compensation_operation_id(
        proposed.operation_id,
        "undo:save_review_readiness_signal",
    )
    with session_factory() as session:
        operation = session.get(WriteOperation, compensation_id)
        transitions = tuple(
            session.scalars(
                select(WriteOperationTransition)
                .where(WriteOperationTransition.operation_id == compensation_id)
                .order_by(WriteOperationTransition.seq)
            )
        )
        versions = tuple(
            session.scalars(
                select(InterviewReadinessSignalVersion).where(
                    InterviewReadinessSignalVersion.signal_id == signal_id
                )
            )
        )
    assert operation is not None and operation.status == "proposed"
    assert [(row.seq, row.state) for row in transitions] == [(1, "proposed")]
    assert len(versions) == 1


def test_terminal_replay_revalidates_parent_digest_and_writes_no_adjacent_surfaces(
    tmp_path,
) -> None:
    session_factory = init_database(tmp_path / "terminal-parent-mismatch.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    registry = coordinator._proof_registry
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
        before = (
            len(tuple(session.scalars(select(Conversation)))),
            len(tuple(session.scalars(select(ProductActionProposal)))),
            len(tuple(session.scalars(select(ChatMessage)))),
            len(tuple(session.scalars(select(AgentRun)))),
            len(tuple(session.scalars(select(AgentEvent)))),
            len(tuple(session.scalars(select(AgentContextSnapshot)))),
        )
    first_proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    coordinator.execute(first_proof)
    replay_proof = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    with session_factory() as session:
        parent = session.get(WriteOperation, proposed.operation_id)
        assert parent is not None
        session.execute(text("DROP TRIGGER IF EXISTS trg_write_operation_terminal_immutable"))
        parent.terminal_payload_sha256 = "sha256:" + "0" * 64
        session.commit()
    with pytest.raises(ProductActionIntegrityError):
        coordinator.execute(replay_proof)
    del replay_proof, first_proof
    gc.collect()
    assert len(registry._records) == 0
    assert len(registry._retired) == 0
    with session_factory() as session:
        after = (
            len(tuple(session.scalars(select(Conversation)))),
            len(tuple(session.scalars(select(ProductActionProposal)))),
            len(tuple(session.scalars(select(ChatMessage)))),
            len(tuple(session.scalars(select(AgentRun)))),
            len(tuple(session.scalars(select(AgentEvent)))),
            len(tuple(session.scalars(select(AgentContextSnapshot)))),
        )
    assert after == before


def test_orphan_domain_state_with_proposed_compensation_is_integrity_error(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "orphan-domain-proposed.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    repository = coordinator._readiness_repository
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id

    def leave_proposed(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("leave proposed")

    monkeypatch.setattr(repository, "retract_signal_in_session", leave_proposed)
    with pytest.raises(RuntimeError):
        coordinator.execute(
            issuer.issue(
                application_id=seeded["application_id"],
                signal_id=signal_id,
                parent_operation_id=proposed.operation_id,
            )
        )
    monkeypatch.undo()
    with session_factory() as session:
        signal = session.get(InterviewReadinessSignal, signal_id)
        assert signal is not None
        signal.revision += 1
        session.commit()
    with pytest.raises(ProductActionIntegrityError) as captured:
        issuer.issue(
            application_id=seeded["application_id"],
            signal_id=signal_id,
            parent_operation_id=proposed.operation_id,
        )
    assert captured.value.code == "product_action_compensation_orphan_domain"


@pytest.mark.parametrize(
    "corruption",
    ("duplicate", "nonfinite", "oversized", "ledger_shape"),
)
def test_terminal_replay_rejects_persisted_terminal_json_and_cap_corruption(
    tmp_path,
    corruption,
) -> None:
    session_factory = init_database(tmp_path / f"terminal-{corruption}.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    terminal = coordinator.execute(
        issuer.issue(
            application_id=seeded["application_id"],
            signal_id=signal_id,
            parent_operation_id=proposed.operation_id,
        )
    )
    replay = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    with session_factory() as session:
        operation = session.get(WriteOperation, terminal.operation_id)
        assert operation is not None and operation.result_json is not None
        session.execute(text("DROP TRIGGER IF EXISTS trg_write_operation_terminal_immutable"))
        if corruption == "duplicate":
            member = '"kind":"review_readiness_signal_retracted_v1"'
            operation.result_json = operation.result_json.replace(
                member,
                member + "," + member,
                1,
            )
        elif corruption == "nonfinite":
            parsed = json.loads(operation.result_json)
            parsed["signal_revision"] = float("nan")
            operation.result_json = json.dumps(
                parsed,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        elif corruption == "oversized":
            operation.visible_result = "x" * 1_025
        else:
            session.execute(text("PRAGMA ignore_check_constraints=ON"))
            operation.agent_run_id = "99999999-9999-4999-8999-999999999999"
        session.commit()
    with pytest.raises(ProductActionIntegrityError):
        coordinator.execute(replay)


def test_replay_rejects_noncanonical_version_domain_uuid(tmp_path) -> None:
    session_factory = init_database(tmp_path / "domain-key-corruption.sqlite3")
    seeded, proposed, _repository, _keys = _create_committed_signal(session_factory)
    issuer, coordinator = _compensation_stack(session_factory)
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        signal_id = signal.id
    coordinator.execute(
        issuer.issue(
            application_id=seeded["application_id"],
            signal_id=signal_id,
            parent_operation_id=proposed.operation_id,
        )
    )
    replay = issuer.issue(
        application_id=seeded["application_id"],
        signal_id=signal_id,
        parent_operation_id=proposed.operation_id,
    )
    with session_factory() as session:
        signal = session.get(InterviewReadinessSignal, signal_id)
        assert signal is not None and signal.current_version_id is not None
        version = session.get(InterviewReadinessSignalVersion, signal.current_version_id)
        assert version is not None
        session.execute(text("PRAGMA ignore_check_constraints=ON"))
        session.execute(
            text(
                "DROP TRIGGER IF EXISTS "
                "trg_interview_readiness_signal_version_immutable"
            )
        )
        version.domain_idempotency_key = version.domain_idempotency_key.upper()
        session.commit()
    with pytest.raises(ProductActionIntegrityError) as captured:
        coordinator.execute(replay)
    assert captured.value.code == "readiness_signal_version_integrity"
