from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from offerpilot.ai.write_operations import ledger_fingerprint
from offerpilot.db import init_database
from offerpilot.models import (
    InterviewReadinessSignal,
    InterviewReadinessSignalEvidence,
    InterviewReadinessSignalVersion,
    ProductActionProposal,
    WriteOperation,
    WriteOperationTransition,
)
import offerpilot.product_actions.coordinator as coordinator_module
from offerpilot.product_actions.catalog import ProductActionCatalogV1
from offerpilot.product_actions.contracts import (
    ProductActionProofRegistryV1,
    canonical_product_action_json,
)
from offerpilot.product_actions.coordinator import ProductActionCoordinator
from offerpilot.product_actions.issuer import (
    LedgerKeyProfileStoreV1,
    ReviewReadinessActionIssuer,
)
from offerpilot.product_actions.repository import ProductActionProposalRepository
from offerpilot.review_readiness.candidates import project_readiness_candidates
from offerpilot.review_readiness.repository import ReadinessSignalRepository

from tests.product_actions.conftest import KEY_ONE
from tests.review_readiness_support import seed_review_candidate


def _coordinator(  # type: ignore[no-untyped-def]
    session_factory,
    *,
    capability_check=None,
    candidate_projector=project_readiness_candidates,
):
    registry = ProductActionProofRegistryV1()
    catalog = ProductActionCatalogV1(registry)
    keys = LedgerKeyProfileStoreV1((KEY_ONE,), active_key_id=KEY_ONE.key_id)
    issuer = ReviewReadinessActionIssuer(catalog, registry, keys)
    proposal_repository = ProductActionProposalRepository(
        session_factory,
        catalog=catalog,
        proof_registry=registry,
        key_profiles=keys,
    )
    signals = ReadinessSignalRepository(session_factory, proof_registry=registry)
    return ProductActionCoordinator(
        session_factory,
        catalog=catalog,
        proposal_repository=proposal_repository,
        review_issuer=issuer,
        proof_registry=registry,
        key_profiles=keys,
        readiness_repository=signals,
        capability_check=capability_check
        or (
            lambda capability: (
                capability == "application.interview_readiness_feedback.write"
            )
        ),
        candidate_projector=candidate_projector,
    )


def test_signal_aggregate_and_ledger_terminal_commit_together(tmp_path) -> None:
    session_factory = init_database(tmp_path / "signal.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    proposed = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request={
            "proposal_id": seeded["proposal_id"],
            "focus_id": seeded["focus_id"],
            "expected_note_revision": seeded["note_revision"],
            "expected_candidate_fingerprint": candidate.candidate_fingerprint,
            "idempotency_key": "33333333-3333-4333-8333-333333333333",
            "user_note": "下次练习先讲约束。",
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
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None
        version = session.get(InterviewReadinessSignalVersion, signal.current_version_id)
        assert version is not None
        evidence = list(
            session.scalars(
                select(InterviewReadinessSignalEvidence)
                .where(InterviewReadinessSignalEvidence.signal_version_id == version.id)
                .order_by(InterviewReadinessSignalEvidence.ordinal)
            )
        )
        operation = session.get(WriteOperation, proposed.operation_id)
        route = session.get(ProductActionProposal, proposed.operation_id)
        transitions = tuple(
            session.scalars(
                select(WriteOperationTransition)
                .where(WriteOperationTransition.operation_id == proposed.operation_id)
                .order_by(WriteOperationTransition.seq)
            )
        )
    assert signal.revision == 1
    assert version.statement_text == seeded["focus_text"]
    assert [item.ordinal for item in evidence] == [0]
    assert operation is not None
    assert operation.status == "committed"
    assert operation.delivery_status == "not_applicable"
    assert operation.delivery_outcome == "none"
    effective_payload_sha256 = "sha256:" + hashlib.sha256(
        canonical_product_action_json(
            {"user_note": "下次练习先讲约束。"}
        ).encode("utf-8")
    ).hexdigest()
    assert operation.input_fingerprint == ledger_fingerprint(
        KEY_ONE,
        "product-action-input-v1",
        {
            "operation_request_fingerprint": operation.operation_request_fingerprint,
            "authorization_scope_fingerprint": (
                operation.authorization_scope_fingerprint
            ),
            "effective_payload_sha256": effective_payload_sha256,
        },
    )
    assert json.loads(operation.result_json) == {
        "schema_version": 1,
        "action_name": "save_review_readiness_signal",
        "outcome": "created",
        "signal_id": signal.id,
        "signal_version_id": version.id,
        "signal_revision": 1,
        "source_status": "current",
    }
    assert json.loads(operation.transport_json) == {
        "schema_version": 1,
        "operation_id": proposed.operation_id,
        "action_name": "save_review_readiness_signal",
        "status": "committed",
        "result": json.loads(operation.result_json),
    }
    assert json.loads(operation.undo_json) == {
        "kind": "retract_review_readiness_signal_v1",
        "signal_id": signal.id,
        "created_version_id": version.id,
        "expected_current_version_id": version.id,
        "expected_signal_revision": 1,
        "parent_operation_id": proposed.operation_id,
    }
    assert operation.visible_result == "已保存为下次准备重点。"
    assert route is not None
    assert route.route_payload_json is None
    assert route.terminalized_at is not None
    assert [(item.seq, item.state) for item in transitions] == [
        (1, "proposed"),
        (2, "approved"),
        (3, "claimed"),
        (4, "committed"),
    ]


def test_signal_and_terminal_roll_back_together_on_post_executor_failure(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "rollback.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    proposed = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request={
            "proposal_id": seeded["proposal_id"],
            "focus_id": seeded["focus_id"],
            "expected_note_revision": seeded["note_revision"],
            "expected_candidate_fingerprint": candidate.candidate_fingerprint,
            "idempotency_key": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "user_note": "",
        },
    )

    def fail_aggregate(_payload, _limit):
        raise coordinator_module.ProductActionCoordinatorError(
            "product_action_input_too_large",
            status_code=422,
        )

    monkeypatch.setattr(coordinator_module, "_enforce_action_aggregate", fail_aggregate)
    try:
        coordinator.decide(
            operation_id=proposed.operation_id,
            request={
                "confirmation_token": proposed.confirmation_token,
                "decision": "approve",
            },
        )
    except coordinator_module.ProductActionCoordinatorError as exc:
        assert exc.code == "product_action_input_too_large"
    else:
        raise AssertionError("post-executor terminal failure must propagate")

    with session_factory() as session:
        signal_count = len(tuple(session.scalars(select(InterviewReadinessSignal))))
        operation = session.get(WriteOperation, proposed.operation_id)
        transitions = tuple(
            session.scalars(
                select(WriteOperationTransition)
                .where(WriteOperationTransition.operation_id == proposed.operation_id)
                .order_by(WriteOperationTransition.seq)
            )
        )
    assert signal_count == 0
    assert operation is not None and operation.status == "proposed"
    assert [(item.seq, item.state) for item in transitions] == [(1, "proposed")]
