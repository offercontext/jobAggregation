from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from types import MappingProxyType
from typing import Any, cast

import pytest
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import offerpilot.product_actions.coordinator as coordinator_module
from offerpilot.ai.write_operations import (
    TerminalPayload,
    WriteOperationError,
    build_terminal_payload,
)
from offerpilot.db import init_database
from offerpilot.models import (
    Application,
    InterviewNote,
    InterviewReadinessSignal,
    InterviewStoryProposalAttempt,
    ProductActionProposal,
    WriteOperation,
    WriteOperationTransition,
)
from offerpilot.product_actions.coordinator import (
    ProductActionDeclaredExecutorFailureV1,
    ProductActionCoordinatorError,
    ProductActionHandlerResultV1,
    ProductActionPreClaimDispositionV1,
    ProductActionPreflightV1,
    ProductActionStoryWriteConflict,
    ProductActionTerminalBudgetsV1,
    ProductActionTerminalProjectionV1,
    TrustedProductActionDecisionV1,
    seal_interview_story_product_action_handler,
)
from offerpilot.product_actions.contracts import (
    JSONValue,
    ProductActionExecutionAuthorization,
    ProductActionIntegrityError,
    canonical_product_action_json,
)
from offerpilot.product_actions.issuer import InterviewStoryActionIssuer
from offerpilot.review_readiness.candidates import project_readiness_candidates

from tests.review_readiness_support import seed_review_candidate
from tests.test_review_readiness_repository import _coordinator
from tests.product_actions.conftest import KEY_TWO, raw_json, story_route


def _digest(value: Mapping[str, JSONValue]) -> str:
    return "sha256:" + hashlib.sha256(
        canonical_product_action_json(dict(value)).encode("utf-8")
    ).hexdigest()


def _exact_utf8_bytes(size: int) -> str:
    multibyte, remainder = divmod(size, 3)
    value = "界" * multibyte + "x" * remainder
    assert len(value.encode("utf-8")) == size
    return value


def _exact_json_bytes(size: int) -> dict[str, JSONValue]:
    assert size >= 8
    value: dict[str, JSONValue] = {"v": _exact_utf8_bytes(size - 8)}
    assert len(canonical_product_action_json(value).encode("utf-8")) == size
    return value


class _FakeStoryHandler:
    action_name = "confirm_interview_story"
    terminal_budgets = ProductActionTerminalBudgetsV1(
        result_bytes=4_096,
        visible_bytes=1_024,
        transport_bytes=4_096,
        undo_bytes=32_768,
        aggregate_bytes=49_152,
    )
    declared_executor_failures = (
        ProductActionDeclaredExecutorFailureV1(
            exception_type=ProductActionStoryWriteConflict,
            failure_category="conflict",
            failure_code="product_action_story_write_conflict",
        ),
    )
    declared_preclaim_dispositions = ("source_changed",)

    def __init__(self, registry, *, outcome: str = "success") -> None:
        self._registry = registry
        self.outcome = outcome
        self.executions = 0

    def external_preflight(self, route_payload, decision):  # type: ignore[no-untyped-def]
        del route_payload
        effective: dict[str, JSONValue] = {
            "content": "story content",
            "decision": decision.decision,
        }
        return ProductActionPreflightV1(
            self.action_name,
            effective,
            _digest(effective),
            object(),
        )

    def locked_recheck(
        self,
        session: Session,
        route_payload: Mapping[str, JSONValue],
        trusted: TrustedProductActionDecisionV1,
    ) -> ProductActionPreflightV1 | ProductActionPreClaimDispositionV1:
        del route_payload
        if self.outcome in {"mapped_stale", "mapped_stale_bad"}:
            attempt = session.get(InterviewStoryProposalAttempt, 51)
            assert attempt is not None
            attempt.attempt_status = "invalidated"
            attempt.failure_category = "source_changed"
            if self.outcome == "mapped_stale_bad":
                session.execute(
                    text(
                        "UPDATE interview_story_proposal_attempts "
                        "SET proposal_hash = :proposal_hash WHERE id = 51"
                    ),
                    {"proposal_hash": "sha256:" + "9" * 64},
                )
            return ProductActionPreClaimDispositionV1(
                "confirm_interview_story",
                "source_changed",
            )
        effective = dict(trusted.effective_payload)
        return ProductActionPreflightV1(
            self.action_name,
            effective,
            _digest(effective),
            trusted.trusted_source,
        )

    def execute_in_session(
        self,
        session: Session,
        *,
        operation_id: str,
        operation_request_fingerprint: str,
        route_payload: Mapping[str, JSONValue],
        trusted: TrustedProductActionDecisionV1,
        authorization: ProductActionExecutionAuthorization,
        authorization_binding: tuple[object, ...],
    ) -> ProductActionHandlerResultV1:
        del operation_request_fingerprint, route_payload, trusted
        self.executions += 1
        with self._registry.claim(
            authorization,
            proof_type=ProductActionExecutionAuthorization,
            action_name=self.action_name,
            expected_binding=authorization_binding,
        ):
            session.add(
                Application(
                    company_name="SAVEPOINT mutation",
                    position_name="must roll back",
                    source="product-action-test",
                )
            )
            session.flush()
            if self.outcome == "declared_conflict":
                raise ProductActionStoryWriteConflict
            if self.outcome == "undeclared_error":
                raise RuntimeError("must not terminalize")
        result: dict[str, JSONValue] = {
            "schema_version": 1,
            "action_name": self.action_name,
            "outcome": "created",
            "story_id": 91,
            "story_version_id": 92,
            "story_revision": 1,
        }
        undo: dict[str, JSONValue] = {
            "kind": "archive_created_story_v1",
            "story_id": 91,
            "created_version_id": 92,
            "expected_current_version_id": 92,
            "expected_story_revision": 1,
            "expected_status": "active",
        }
        return ProductActionHandlerResultV1(result, "已保存到经历素材。", undo)

    def stage_terminal_input_in_session(
        self,
        session: Session,
        *,
        operation_id: str,
        effective_payload_sha256: str,
    ) -> None:
        del session, operation_id, effective_payload_sha256

    def project_committed_terminal(
        self,
        operation_id: str,
        result: ProductActionHandlerResultV1,
    ) -> ProductActionTerminalProjectionV1:
        transport: dict[str, JSONValue] = {
            "schema_version": 1,
            "operation_id": operation_id,
            "action_name": self.action_name,
            "status": "committed",
            "result": dict(result.result),
            "legacy_direct_commit": {
                "status_code": 201,
                "body": {"story_id": 91, "version_id": 92, "created": True},
            },
            "legacy_reconciliation_or_replay": {
                "status_code": 200,
                "body": {"story_id": 91, "version_id": 92, "created": False},
            },
        }
        return ProductActionTerminalProjectionV1(
            result.result,
            result.visible_result,
            transport,
            result.undo,
        )

    def project_failed_terminal(
        self,
        operation_id: str,
        failure: ProductActionDeclaredExecutorFailureV1,
    ) -> ProductActionTerminalProjectionV1:
        result: dict[str, JSONValue] = {
            "schema_version": 1,
            "action_name": self.action_name,
            "outcome": "failed",
            "code": failure.failure_code,
        }
        transport: dict[str, JSONValue] = {
            "schema_version": 1,
            "operation_id": operation_id,
            "action_name": self.action_name,
            "status": "failed",
            "code": failure.failure_code,
        }
        return ProductActionTerminalProjectionV1(
            result,
            "经历素材写入发生冲突，请刷新后重试。",
            transport,
            None,
        )

    def terminal_replay_effective_payload_sha256(
        self,
        operation_id: str,
        decision,
    ) -> str:  # type: ignore[no-untyped-def]
        del operation_id
        return _digest({"content": "story content", "decision": decision.decision})

    def persisted_terminal_effective_payload_sha256(self, operation_id: str) -> str:
        del operation_id
        return _digest({"content": "story content", "decision": "approve"})


def _publish_fake_story(coordinator, handler):  # type: ignore[no-untyped-def]
    issuer = InterviewStoryActionIssuer(
        coordinator._catalog,
        coordinator._proof_registry,
        coordinator._key_profiles,
    )
    prepared = issuer.prepare(route_payload_raw=raw_json(story_route()))
    with coordinator._session_factory() as session:
        uow = coordinator._proposal_repository.begin_publication_uow(session)
        coordinator._proposal_repository.publish_bundle_in_session(
            session,
            uow,
            prepared,
        )
        session.add(
            InterviewStoryProposalAttempt(
                id=51,
                target_story_id=52,
                idempotency_key="story-handler-fixture",
                entrypoint="product-action-test",
                entry_context_json="{}",
                attempt_status="ready",
                generation_revision=6,
                input_snapshot_json="{}",
                source_fingerprint="sha256:" + "5" * 64,
                proposal_json="{}",
                proposal_hash="sha256:" + "4" * 64,
                failure_category="",
                product_action_operation_id=prepared.operation_id,
                product_action_generation=8,
            )
        )
        session.commit()
    return prepared


def test_signal_proposal_replays_and_conflicting_input_is_rejected(tmp_path) -> None:
    session_factory = init_database(tmp_path / "coordinator.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    request = {
        "proposal_id": seeded["proposal_id"],
        "focus_id": seeded["focus_id"],
        "expected_note_revision": seeded["note_revision"],
        "expected_candidate_fingerprint": candidate.candidate_fingerprint,
        "idempotency_key": "33333333-3333-4333-8333-333333333333",
        "user_note": "",
    }

    first = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"], request=request
    )
    replay = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"], request=request
    )

    assert first.created is True
    assert replay.created is False
    assert first.operation_id == replay.operation_id
    assert first.confirmation_token == replay.confirmation_token

    with pytest.raises(ProductActionCoordinatorError) as error:
        coordinator.propose_readiness_signal(
            note_id=seeded["note_id"],
            request={**request, "user_note": "different"},
        )
    assert error.value.code == "product_action_idempotency_conflict"


def test_reject_is_terminal_without_source_recheck(tmp_path) -> None:
    session_factory = init_database(tmp_path / "reject.sqlite3")
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
            "idempotency_key": "44444444-4444-4444-8444-444444444444",
            "user_note": "",
        },
    )

    rejected = coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": proposed.confirmation_token,
            "decision": "reject",
        },
    )
    replay = coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": proposed.confirmation_token,
            "decision": "reject",
        },
    )

    assert rejected.status == "rejected"
    assert replay.status == "rejected"
    assert replay.replayed is True
    with session_factory() as session:
        operation = session.get(WriteOperation, proposed.operation_id)
        route = session.get(ProductActionProposal, proposed.operation_id)
        transitions = tuple(
            session.scalars(
                select(WriteOperationTransition)
                .where(WriteOperationTransition.operation_id == proposed.operation_id)
                .order_by(WriteOperationTransition.seq)
            )
        )
    assert operation is not None
    assert operation.input_fingerprint is None
    assert operation.result_contract == "rejection_json_v1"
    assert operation.undo_json is None
    assert operation.delivery_status == "not_applicable"
    assert operation.delivery_outcome == "none"
    assert route is not None and route.route_payload_json is None
    assert route.terminalized_at is not None
    assert [(item.seq, item.state) for item in transitions] == [
        (1, "proposed"),
        (2, "rejected"),
    ]


def test_approve_terminal_replay_and_committed_focus_locator_create_no_ledger(
    tmp_path,
) -> None:
    session_factory = init_database(tmp_path / "approve-replay.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    request = {
        "proposal_id": seeded["proposal_id"],
        "focus_id": seeded["focus_id"],
        "expected_note_revision": seeded["note_revision"],
        "expected_candidate_fingerprint": candidate.candidate_fingerprint,
        "idempotency_key": "66666666-6666-4666-8666-666666666666",
        "user_note": "下次先讲清约束。",
    }
    proposed = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"], request=request
    )
    decided = coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": proposed.confirmation_token,
            "decision": "approve",
        },
    )
    replay = coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": proposed.confirmation_token,
            "decision": "approve",
        },
    )
    with session_factory() as session:
        before = session.scalar(select(func.count()).select_from(WriteOperation))
    located = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request={
            **request,
            "idempotency_key": "77777777-7777-4777-8777-777777777777",
        },
    )
    with session_factory() as session:
        after = session.scalar(select(func.count()).select_from(WriteOperation))

    assert decided.direct_commit is True
    assert decided.completion_kind == "direct_commit"
    assert replay.replayed is True
    assert replay.completion_kind == "replay"
    assert replay.direct_commit is False
    assert replay.result == decided.result
    assert type(decided.transport) is MappingProxyType
    assert type(decided.transport["result"]) is MappingProxyType
    assert replay.transport == decided.transport
    assert decided.legacy_projection is None
    with pytest.raises(TypeError):
        cast(Any, decided.transport)["status"] = "tampered"
    assert located.status == "already_confirmed"
    assert located.confirmation_token is None
    assert after == before == 1


def test_same_request_key_is_terminal_replay_before_semantic_locator(tmp_path) -> None:
    session_factory = init_database(tmp_path / "idempotency-before-semantic.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    request = {
        "proposal_id": seeded["proposal_id"],
        "focus_id": seeded["focus_id"],
        "expected_note_revision": seeded["note_revision"],
        "expected_candidate_fingerprint": candidate.candidate_fingerprint,
        "idempotency_key": "71717171-7171-4171-8171-717171717171",
        "user_note": "exact request",
    }
    proposed = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"], request=request
    )
    coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": proposed.confirmation_token,
            "decision": "approve",
        },
    )

    replay = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"], request=request
    )
    with pytest.raises(ProductActionCoordinatorError) as conflict:
        coordinator.propose_readiness_signal(
            note_id=seeded["note_id"],
            request={**request, "user_note": "different"},
        )
    located = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request={
            **request,
            "idempotency_key": "72727272-7272-4272-8272-727272727272",
        },
    )

    assert replay.operation_id == proposed.operation_id
    assert replay.status == "committed"
    assert replay.replayed is True
    assert conflict.value.code == "product_action_idempotency_conflict"
    assert located.status == "already_confirmed"


def test_rejected_request_replay_requires_exact_input_and_owner_scope(tmp_path) -> None:
    session_factory = init_database(tmp_path / "rejected-request-identity.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    request = {
        "proposal_id": seeded["proposal_id"],
        "focus_id": seeded["focus_id"],
        "expected_note_revision": seeded["note_revision"],
        "expected_candidate_fingerprint": candidate.candidate_fingerprint,
        "idempotency_key": "73737373-7373-4373-8373-737373737373",
        "user_note": "exact rejected request",
    }
    proposed = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"], request=request
    )
    coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": proposed.confirmation_token,
            "decision": "reject",
        },
    )

    exact = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"], request=request
    )
    with pytest.raises(ProductActionCoordinatorError) as note_conflict:
        coordinator.propose_readiness_signal(
            note_id=seeded["note_id"],
            request={**request, "user_note": "different"},
        )
    with pytest.raises(ProductActionCoordinatorError) as focus_conflict:
        coordinator.propose_readiness_signal(
            note_id=seeded["note_id"],
            request={**request, "focus_id": "different-focus"},
        )
    with pytest.raises(ProductActionCoordinatorError) as missing_owner:
        coordinator.propose_readiness_signal(note_id=999_999, request=request)

    assert exact.operation_id == proposed.operation_id
    assert exact.status == "rejected"
    assert exact.replayed is True
    assert note_conflict.value.code == "product_action_idempotency_conflict"
    assert focus_conflict.value.code == "product_action_idempotency_conflict"
    assert missing_owner.value.code == "review_readiness_not_found"
    assert missing_owner.value.status_code == 404


def test_proposed_replay_never_reissues_full_token_after_source_change(tmp_path) -> None:
    session_factory = init_database(tmp_path / "proposed-source-changed.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    request = {
        "proposal_id": seeded["proposal_id"],
        "focus_id": seeded["focus_id"],
        "expected_note_revision": seeded["note_revision"],
        "expected_candidate_fingerprint": candidate.candidate_fingerprint,
        "idempotency_key": "74747474-7474-4474-8474-747474747474",
        "user_note": "",
    }
    proposed = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"], request=request
    )
    with session_factory() as session:
        session.execute(
            update(InterviewNote)
            .where(InterviewNote.id == seeded["note_id"])
            .values(content_revision=InterviewNote.content_revision + 1)
        )
        session.commit()

    with pytest.raises(ProductActionCoordinatorError) as stale:
        coordinator.propose_readiness_signal(
            note_id=seeded["note_id"],
            request=request,
        )
    rejection = coordinator.recover_rejection_control(
        application_id=seeded["application_id"],
        operation_id=proposed.operation_id,
    )

    assert stale.value.code == "review_readiness_source_changed"
    assert rejection.confirmation_token != proposed.confirmation_token
    assert rejection.allowed_decisions == ("reject",)


def test_active_key_rotation_recovers_persisted_proposed_identity(tmp_path) -> None:
    session_factory = init_database(tmp_path / "active-key-rotation.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    request = {
        "proposal_id": seeded["proposal_id"],
        "focus_id": seeded["focus_id"],
        "expected_note_revision": seeded["note_revision"],
        "expected_candidate_fingerprint": candidate.candidate_fingerprint,
        "idempotency_key": "73737373-7373-4373-8373-737373737373",
        "user_note": "persisted profile",
    }
    first = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"], request=request
    )
    coordinator._key_profiles.activate(KEY_TWO.key_id)

    replay = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"], request=request
    )

    assert replay.created is False
    assert replay.operation_id == first.operation_id
    assert replay.confirmation_token == first.confirmation_token


def test_confirmed_focus_does_not_hide_other_focus_or_break_its_locator(tmp_path) -> None:
    focuses = [
        {
            "id": f"focus-{index}",
            "text": f"Preparation focus {index}",
            "evidence_refs": [
                {
                    "source": "interview_note",
                    "path": "/difficulty_points",
                    "excerpt": "cache consistency tradeoffs",
                }
            ],
        }
        for index in range(2)
    ]
    session_factory = init_database(tmp_path / "per-focus.sqlite3")
    seeded = seed_review_candidate(session_factory, practice_focuses=focuses)
    with session_factory() as session:
        candidates = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates
    coordinator = _coordinator(session_factory)

    first_request = {
        "proposal_id": seeded["proposal_id"],
        "focus_id": candidates[0].focus_id,
        "expected_note_revision": seeded["note_revision"],
        "expected_candidate_fingerprint": candidates[0].candidate_fingerprint,
        "idempotency_key": "74747474-7474-4474-8474-747474747474",
        "user_note": "",
    }
    first = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"], request=first_request
    )
    coordinator.decide(
        operation_id=first.operation_id,
        request={"confirmation_token": first.confirmation_token, "decision": "approve"},
    )
    second = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request={
            **first_request,
            "focus_id": candidates[1].focus_id,
            "expected_candidate_fingerprint": candidates[1].candidate_fingerprint,
            "idempotency_key": "75757575-7575-4575-8575-757575757575",
        },
    )
    located = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request={
            **first_request,
            "idempotency_key": "76767676-7676-4676-8676-767676767676",
        },
    )

    assert second.status == "proposed"
    assert located.status == "already_confirmed"


def test_modify_replay_is_bound_to_exact_edited_payload(tmp_path) -> None:
    session_factory = init_database(tmp_path / "modify-replay.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    proposal_request = {
        "proposal_id": seeded["proposal_id"],
        "focus_id": seeded["focus_id"],
        "expected_note_revision": seeded["note_revision"],
        "expected_candidate_fingerprint": candidate.candidate_fingerprint,
        "idempotency_key": "88888888-8888-4888-8888-888888888888",
        "user_note": "old",
    }
    proposed = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request=proposal_request,
    )
    exact = {
        "confirmation_token": proposed.confirmation_token,
        "decision": "modify",
        "edited_payload": {"user_note": "new"},
    }

    assert coordinator.decide(operation_id=proposed.operation_id, request=exact).status == (
        "committed"
    )
    assert coordinator.decide(
        operation_id=proposed.operation_id,
        request=exact,
    ).replayed is True
    assert coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request=proposal_request,
    ).status == "committed"
    with pytest.raises(ProductActionCoordinatorError) as proposal_conflict:
        coordinator.propose_readiness_signal(
            note_id=seeded["note_id"],
            request={**proposal_request, "user_note": "different proposal"},
        )
    with pytest.raises(ProductActionCoordinatorError) as error:
        coordinator.decide(
            operation_id=proposed.operation_id,
            request={
                **exact,
                "edited_payload": {"user_note": "different"},
            },
        )
    assert error.value.code == "product_action_request_conflict"
    assert proposal_conflict.value.code == "product_action_idempotency_conflict"


def test_capability_denial_short_circuits_before_any_sql_or_projection(tmp_path) -> None:
    session_factory = init_database(tmp_path / "capability.sqlite3")
    statements: list[str] = []
    engine = session_factory.kw["bind"]

    def observe(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        statements.append(statement)

    def forbidden_projector(_note_id, _proposal_id, _session):
        raise AssertionError("candidate projection must not run")

    event.listen(engine, "before_cursor_execute", observe)
    try:
        coordinator = _coordinator(
            session_factory,
            capability_check=lambda _capability: False,
            candidate_projector=forbidden_projector,
        )
        with pytest.raises(ProductActionCoordinatorError) as error:
            coordinator.propose_readiness_signal(
                note_id=1,
                request={
                    "proposal_id": 1,
                    "focus_id": "focus-1",
                    "expected_note_revision": 1,
                    "expected_candidate_fingerprint": "sha256:" + "0" * 64,
                    "idempotency_key": "99999999-9999-4999-8999-999999999999",
                    "user_note": "",
                },
            )
    finally:
        event.remove(engine, "before_cursor_execute", observe)

    assert error.value.code == "review_readiness_not_found"
    assert statements == []


def test_reject_never_rechecks_source_or_capability(tmp_path) -> None:
    session_factory = init_database(tmp_path / "reject-zero.sqlite3")
    seeded = seed_review_candidate(session_factory)
    calls = {"candidate": 0, "capability": 0}

    def candidate_projector(note_id, proposal_id, session):
        calls["candidate"] += 1
        return project_readiness_candidates(note_id, proposal_id, session)

    def capability_check(capability):
        calls["capability"] += 1
        return capability == "application.interview_readiness_feedback.write"

    coordinator = _coordinator(
        session_factory,
        capability_check=capability_check,
        candidate_projector=candidate_projector,
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
            "idempotency_key": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "user_note": "",
        },
    )
    calls.update(candidate=0, capability=0)

    rejected = coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": proposed.confirmation_token,
            "decision": "reject",
        },
    )

    assert rejected.status == "rejected"
    assert calls == {"candidate": 0, "capability": 0}


def test_rejection_recovery_issues_reject_scoped_credential(tmp_path, monkeypatch) -> None:
    session_factory = init_database(tmp_path / "rejection-credential.sqlite3")
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
            "idempotency_key": "78787878-7878-4878-8878-787878787878",
            "user_note": "",
        },
    )
    recovery = coordinator.recover_rejection_control(
        application_id=seeded["application_id"],
        operation_id=proposed.operation_id,
    )
    executions = 0

    def forbidden_executor(*_args, **_kwargs):
        nonlocal executions
        executions += 1
        raise AssertionError("rejection credential must never reach the executor")

    monkeypatch.setattr(
        coordinator._readiness_repository,
        "create_signal_in_session",
        forbidden_executor,
    )
    with pytest.raises(ProductActionCoordinatorError) as approve:
        coordinator.decide(
            operation_id=proposed.operation_id,
            request={
                "confirmation_token": recovery.confirmation_token,
                "decision": "approve",
            },
        )
    with pytest.raises(ProductActionCoordinatorError) as modify:
        coordinator.decide(
            operation_id=proposed.operation_id,
            request={
                "confirmation_token": recovery.confirmation_token,
                "decision": "modify",
                "edited_payload": {"user_note": "forbidden"},
            },
        )
    rejected = coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": recovery.confirmation_token,
            "decision": "reject",
        },
    )

    assert recovery.confirmation_token != proposed.confirmation_token
    assert recovery.confirmation_token == (
        "556f41d0ab6a56d54c3960a8cd5f860c57d79e2cd49c4f0e21655b28c1b4ceac"
    )
    assert approve.value.code == "product_action_stale"
    assert modify.value.code == "product_action_stale"
    assert executions == 0
    assert rejected.status == "rejected"


def test_primary_input_fingerprint_is_cross_process_canonical_golden() -> None:
    script = textwrap.dedent(
        """
        from offerpilot.ai.write_operations import LedgerKeyDomain, ledger_fingerprint

        key = LedgerKeyDomain(
            "11111111-1111-4111-8111-111111111111",
            b"1" * 32,
        )
        envelope = {
            "operation_request_fingerprint": "hmac-sha256:" + "1" * 64,
            "authorization_scope_fingerprint": "hmac-sha256:" + "2" * 64,
            "effective_payload_sha256": "sha256:" + "3" * 64,
        }
        print(ledger_fingerprint(key, "product-action-input-v1", envelope))
        """
    )
    expected = (
        "hmac-sha256:"
        "88e78c9289de272d2452a011664ffcc5a2408cb35389e75cd237df632f72fa1f"
    )

    outputs = []
    for seed in ("1", "911"):
        environment = dict(os.environ)
        environment["PYTHONHASHSEED"] = seed
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        outputs.append(completed.stdout.strip())

    assert outputs == [expected, expected]


def test_proposal_commit_unknown_rebuilds_once_with_fresh_proof(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "proposal-unknown.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    original_commit = Session.commit
    attempts = 0

    def fail_before_first_commit(session):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OperationalError(
                "COMMIT",
                {},
                sqlite3.OperationalError("commit result unknown"),
            )
        return original_commit(session)

    monkeypatch.setattr(Session, "commit", fail_before_first_commit)
    proposed = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request={
            "proposal_id": seeded["proposal_id"],
            "focus_id": seeded["focus_id"],
            "expected_note_revision": seeded["note_revision"],
            "expected_candidate_fingerprint": candidate.candidate_fingerprint,
            "idempotency_key": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "user_note": "",
        },
    )

    assert attempts == 2
    assert proposed.status == "proposed"
    assert proposed.confirmation_token is not None


def test_proposal_commit_unknown_after_commit_reconciles_without_republication(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "proposal-after-commit.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    original_commit = Session.commit
    attempts = 0

    def commit_then_lose_result(session):
        nonlocal attempts
        attempts += 1
        original_commit(session)
        if attempts == 1:
            raise OperationalError(
                "COMMIT",
                {},
                sqlite3.OperationalError("commit result unknown"),
            )

    monkeypatch.setattr(Session, "commit", commit_then_lose_result)
    proposed = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request={
            "proposal_id": seeded["proposal_id"],
            "focus_id": seeded["focus_id"],
            "expected_note_revision": seeded["note_revision"],
            "expected_candidate_fingerprint": candidate.candidate_fingerprint,
            "idempotency_key": "cececece-cece-4ece-8ece-cececececece",
            "user_note": "",
        },
    )

    with session_factory() as session:
        operation_count = session.scalar(select(func.count()).select_from(WriteOperation))
    assert attempts == 1
    assert proposed.status == "proposed"
    assert proposed.created is False
    assert proposed.replayed is True
    assert operation_count == 1


@pytest.mark.parametrize(
    ("boundary", "integrity_code", "expected_error"),
    [
        ("publication", "partial_product_action_bundle", "integrity"),
        ("publication", "product_action_bundle_unreadable", "unknown"),
        ("decision", "partial_product_action_bundle", "integrity"),
        ("decision", "product_action_bundle_unreadable", "unknown"),
    ],
)
def test_coordinator_fails_closed_on_partial_or_unreadable_bundle_states(
    tmp_path,
    monkeypatch,
    boundary,
    integrity_code,
    expected_error,
) -> None:
    session_factory = init_database(
        tmp_path / f"{boundary}-{integrity_code}.sqlite3"
    )
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    proposal_request = {
        "proposal_id": seeded["proposal_id"],
        "focus_id": seeded["focus_id"],
        "expected_note_revision": seeded["note_revision"],
        "expected_candidate_fingerprint": candidate.candidate_fingerprint,
        "idempotency_key": "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd",
        "user_note": "",
    }
    proposed = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request=proposal_request,
    )

    def fail_load(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise ProductActionIntegrityError(integrity_code)

    monkeypatch.setattr(coordinator._proposal_repository, "load_bundle", fail_load)
    if integrity_code == "partial_product_action_bundle":
        monkeypatch.setattr(
            coordinator._proposal_repository,
            "load_bundle_in_session",
            fail_load,
        )

    def invoke():
        if boundary == "publication":
            return coordinator.propose_readiness_signal(
                note_id=seeded["note_id"],
                request=proposal_request,
            )
        return coordinator.decide(
            operation_id=proposed.operation_id,
            request={
                "confirmation_token": proposed.confirmation_token,
                "decision": "approve",
            },
        )

    if expected_error == "integrity":
        with pytest.raises(ProductActionIntegrityError) as error:
            invoke()
        assert error.value.code == integrity_code
    else:
        with pytest.raises(ProductActionCoordinatorError) as error:
            invoke()
        assert error.value.code == "operation_result_unknown"
        assert error.value.status_code == 503
        assert error.value.retryable is True
    with session_factory() as session:
        operation = session.get(WriteOperation, proposed.operation_id)
        signal_count = session.scalar(
            select(func.count()).select_from(InterviewReadinessSignal)
        )
    assert operation is not None and operation.status == "proposed"
    assert signal_count == 0


def test_decision_commit_unknown_reconciles_terminal_without_reexecution(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "decision-unknown.sqlite3")
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
            "idempotency_key": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "user_note": "",
        },
    )
    original_commit = Session.commit
    attempts = 0

    def commit_then_lose_result(session):
        nonlocal attempts
        attempts += 1
        original_commit(session)
        if attempts == 1:
            raise OperationalError(
                "COMMIT",
                {},
                sqlite3.OperationalError("commit result unknown"),
            )

    monkeypatch.setattr(Session, "commit", commit_then_lose_result)
    decided = coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": proposed.confirmation_token,
            "decision": "approve",
        },
    )

    assert attempts == 1
    assert decided.status == "committed"
    assert decided.replayed is True
    assert decided.direct_commit is False
    assert decided.completion_kind == "reconciliation"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(WriteOperation)) == 1


def test_decision_commit_unknown_before_commit_retries_exact_payload_once(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "decision-before-commit.sqlite3")
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
            "idempotency_key": "79797979-7979-4979-8979-797979797979",
            "user_note": "",
        },
    )
    original_commit = Session.commit
    original_executor = coordinator._readiness_repository.create_signal_in_session
    commit_attempts = 0
    executions = 0

    def fail_before_first_commit(session):
        nonlocal commit_attempts
        commit_attempts += 1
        if commit_attempts == 1:
            raise OperationalError(
                "COMMIT",
                {},
                sqlite3.OperationalError("commit result unknown"),
            )
        return original_commit(session)

    def count_executor(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal executions
        executions += 1
        return original_executor(*args, **kwargs)

    monkeypatch.setattr(Session, "commit", fail_before_first_commit)
    monkeypatch.setattr(
        coordinator._readiness_repository,
        "create_signal_in_session",
        count_executor,
    )
    result = coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": proposed.confirmation_token,
            "decision": "approve",
        },
    )

    with session_factory() as session:
        signal_count = session.scalar(
            select(func.count()).select_from(InterviewReadinessSignal)
        )
    assert result.completion_kind == "reconciliation"
    assert commit_attempts == executions == 2
    assert signal_count == 1


def test_twenty_same_key_proposals_converge_on_one_operation(tmp_path) -> None:
    session_factory = init_database(tmp_path / "twenty.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    request = {
        "proposal_id": seeded["proposal_id"],
        "focus_id": seeded["focus_id"],
        "expected_note_revision": seeded["note_revision"],
        "expected_candidate_fingerprint": candidate.candidate_fingerprint,
        "idempotency_key": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        "user_note": "",
    }

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = tuple(
            pool.map(
                lambda _index: coordinator.propose_readiness_signal(
                    note_id=seeded["note_id"],
                    request=request,
                ),
                range(20),
            )
        )

    assert len({item.operation_id for item in results}) == 1
    assert len({item.confirmation_token for item in results}) == 1
    assert sum(item.created for item in results) == 1
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(WriteOperation)) == 1


def test_semantic_loser_is_not_persisted_and_can_win_after_rejection(tmp_path) -> None:
    session_factory = init_database(tmp_path / "semantic-loser.sqlite3")
    seeded = seed_review_candidate(session_factory)
    with session_factory() as session:
        candidate = project_readiness_candidates(
            seeded["note_id"], seeded["proposal_id"], session
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    common = {
        "proposal_id": seeded["proposal_id"],
        "focus_id": seeded["focus_id"],
        "expected_note_revision": seeded["note_revision"],
        "expected_candidate_fingerprint": candidate.candidate_fingerprint,
        "user_note": "",
    }
    winner = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request={
            **common,
            "idempotency_key": "11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        },
    )
    loser_request = {
        **common,
        "idempotency_key": "22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    }

    with pytest.raises(ProductActionCoordinatorError) as conflict:
        coordinator.propose_readiness_signal(
            note_id=seeded["note_id"],
            request=loser_request,
        )
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(WriteOperation)) == 1
    coordinator.decide(
        operation_id=winner.operation_id,
        request={
            "confirmation_token": winner.confirmation_token,
            "decision": "reject",
        },
    )
    accepted = coordinator.propose_readiness_signal(
        note_id=seeded["note_id"],
        request=loser_request,
    )

    assert conflict.value.code == "review_readiness_action_in_progress"
    assert accepted.created is True
    assert accepted.operation_id != winner.operation_id


def test_source_change_blocks_approve_but_never_blocks_reject(tmp_path) -> None:
    session_factory = init_database(tmp_path / "source-change-reject.sqlite3")
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
            "idempotency_key": "ffffffff-ffff-4fff-8fff-ffffffffffff",
            "user_note": "",
        },
    )
    with session_factory() as session:
        session.execute(
            update(InterviewNote)
            .where(InterviewNote.id == seeded["note_id"])
            .values(content_revision=InterviewNote.content_revision + 1)
        )
        session.commit()

    with pytest.raises(ProductActionCoordinatorError) as stale:
        coordinator.decide(
            operation_id=proposed.operation_id,
            request={
                "confirmation_token": proposed.confirmation_token,
                "decision": "approve",
            },
        )
    with session_factory() as session:
        operation = session.get(WriteOperation, proposed.operation_id)
    rejected = coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": proposed.confirmation_token,
            "decision": "reject",
        },
    )

    assert stale.value.code == "review_readiness_source_changed"
    assert operation is not None and operation.status == "proposed"
    assert rejected.status == "rejected"


def test_executor_base_exception_rolls_back_and_propagates(tmp_path, monkeypatch) -> None:
    session_factory = init_database(tmp_path / "base-exception.sqlite3")
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
            "idempotency_key": "12121212-1212-4212-8212-121212121212",
            "user_note": "",
        },
    )

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        coordinator._readiness_repository,
        "create_signal_in_session",
        interrupt,
    )
    with pytest.raises(KeyboardInterrupt):
        coordinator.decide(
            operation_id=proposed.operation_id,
            request={
                "confirmation_token": proposed.confirmation_token,
                "decision": "approve",
            },
        )

    with session_factory() as session:
        operation = session.get(WriteOperation, proposed.operation_id)
        transitions = tuple(
            session.scalars(
                select(WriteOperationTransition)
                .where(WriteOperationTransition.operation_id == proposed.operation_id)
                .order_by(WriteOperationTransition.seq)
            )
        )
    assert operation is not None and operation.status == "proposed"
    assert [(item.seq, item.state) for item in transitions] == [(1, "proposed")]
    assert coordinator._proof_registry._records == {}


def test_story_conflict_exception_is_not_declared_by_signal_handler(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "signal-story-conflict.sqlite3")
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
            "idempotency_key": "80808080-8080-4080-8080-808080808080",
            "user_note": "",
        },
    )

    def raise_after_authorization(*_args, **kwargs):  # type: ignore[no-untyped-def]
        authorization = kwargs["authorization"]
        binding = kwargs["authorization_binding"]
        with coordinator._proof_registry.claim(
            authorization,
            proof_type=ProductActionExecutionAuthorization,
            action_name="save_review_readiness_signal",
            expected_binding=binding,
        ):
            raise ProductActionStoryWriteConflict

    monkeypatch.setattr(
        coordinator._readiness_repository,
        "create_signal_in_session",
        raise_after_authorization,
    )
    with pytest.raises(ProductActionStoryWriteConflict):
        coordinator.decide(
            operation_id=proposed.operation_id,
            request={
                "confirmation_token": proposed.confirmation_token,
                "decision": "approve",
            },
        )
    with session_factory() as session:
        operation = session.get(WriteOperation, proposed.operation_id)
        transitions = tuple(
            session.scalars(
                select(WriteOperationTransition)
                .where(WriteOperationTransition.operation_id == proposed.operation_id)
                .order_by(WriteOperationTransition.seq)
            )
        )
    assert operation is not None and operation.status == "proposed"
    assert [(item.seq, item.state) for item in transitions] == [(1, "proposed")]


def test_terminal_load_recomputes_primary_input_fingerprint(tmp_path) -> None:
    session_factory = init_database(tmp_path / "input-tamper.sqlite3")
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
            "idempotency_key": "13131313-1313-4313-8313-131313131313",
            "user_note": "",
        },
    )
    coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": proposed.confirmation_token,
            "decision": "approve",
        },
    )
    with session_factory.kw["bind"].begin() as connection:
        connection.exec_driver_sql(
            "DROP TRIGGER IF EXISTS trg_write_operation_terminal_immutable"
        )
    with session_factory() as session:
        session.execute(
            update(WriteOperation)
            .where(WriteOperation.id == proposed.operation_id)
            .values(input_fingerprint="hmac-sha256:" + "0" * 64)
        )
        session.commit()

    with pytest.raises(ProductActionIntegrityError) as error:
        coordinator.get_state(proposed.operation_id)
    assert error.value.code == "product_action_input_fingerprint"


def test_fake_story_handler_uses_action_local_transport_budget_and_completion_kind(
    tmp_path,
) -> None:
    session_factory = init_database(tmp_path / "fake-story-success.sqlite3")
    bootstrap = _coordinator(session_factory)
    handler = _FakeStoryHandler(bootstrap._proof_registry)
    coordinator = _coordinator(
        session_factory,
        capability_check=lambda _capability: True,
        additional_handlers=(seal_interview_story_product_action_handler(handler),),
    )
    handler._registry = coordinator._proof_registry

    def replaced_projector(*_args, **_kwargs):
        raise AssertionError("sealed registration must snapshot handler methods")

    def replaced_stage(*_args, **_kwargs):
        raise AssertionError("sealed registration must snapshot handler methods")

    handler.project_committed_terminal = replaced_projector  # type: ignore[method-assign]
    handler.stage_terminal_input_in_session = replaced_stage  # type: ignore[method-assign]
    prepared = _publish_fake_story(coordinator, handler)

    direct = coordinator.decide(
        operation_id=prepared.operation_id,
        request={
            "confirmation_token": prepared.confirmation_token,
            "decision": "approve",
        },
    )
    replay = coordinator.decide(
        operation_id=prepared.operation_id,
        request={
            "confirmation_token": prepared.confirmation_token,
            "decision": "approve",
        },
    )

    with session_factory() as session:
        operation = session.get(WriteOperation, prepared.operation_id)
    assert direct.completion_kind == "direct_commit"
    assert replay.completion_kind == "replay"
    assert direct.legacy_projection is not None
    assert replay.legacy_projection is not None
    assert direct.legacy_projection["status_code"] == 201
    assert replay.legacy_projection["status_code"] == 200
    assert type(direct.transport) is MappingProxyType
    assert type(direct.legacy_projection["body"]) is MappingProxyType
    assert handler.executions == 1
    assert operation is not None
    transport = json.loads(operation.transport_json)
    assert transport["legacy_direct_commit"]["status_code"] == 201
    assert transport["legacy_reconciliation_or_replay"]["status_code"] == 200


def test_fake_story_handler_requires_sealed_registration(tmp_path) -> None:
    session_factory = init_database(tmp_path / "fake-story-registration.sqlite3")
    bootstrap = _coordinator(session_factory)
    handler = _FakeStoryHandler(bootstrap._proof_registry)

    with pytest.raises(TypeError, match="registration is required"):
        _coordinator(
            session_factory,
            capability_check=lambda _capability: True,
            additional_handlers=(handler,),
        )


def test_fake_story_handler_requires_exact_terminal_input_stager(tmp_path) -> None:
    session_factory = init_database(tmp_path / "fake-story-stager-contract.sqlite3")
    bootstrap = _coordinator(session_factory)
    handler = _FakeStoryHandler(bootstrap._proof_registry)
    handler.stage_terminal_input_in_session = None  # type: ignore[method-assign]

    with pytest.raises(TypeError, match="handler contract is invalid"):
        seal_interview_story_product_action_handler(handler)


def test_attempt_bound_story_recovery_exposes_full_then_rejection_only_credentials(
    tmp_path,
) -> None:
    session_factory = init_database(tmp_path / "fake-story-recovery.sqlite3")
    bootstrap = _coordinator(session_factory)
    handler = _FakeStoryHandler(bootstrap._proof_registry)
    coordinator = _coordinator(
        session_factory,
        capability_check=lambda _capability: True,
        additional_handlers=(seal_interview_story_product_action_handler(handler),),
    )
    handler._registry = coordinator._proof_registry
    prepared = _publish_fake_story(coordinator, handler)

    full = coordinator.recover_story_owner(
        attempt_id=51,
        operation_id=prepared.operation_id,
    )
    route_only = coordinator.recover_story_rejection_control(
        attempt_id=51,
        operation_id=prepared.operation_id,
    )
    assert full.confirmation_token == prepared.confirmation_token
    assert full.allowed_decisions == ("approve", "modify", "reject")
    assert full.rejection_only is False
    assert route_only.confirmation_token != prepared.confirmation_token
    assert route_only.allowed_decisions == ("reject",)
    assert route_only.rejection_only is True

    with session_factory() as session:
        attempt = session.get(InterviewStoryProposalAttempt, 51)
        assert attempt is not None
        attempt.attempt_status = "invalidated"
        session.commit()
    stale = coordinator.recover_story_owner(
        attempt_id=51,
        operation_id=prepared.operation_id,
    )
    assert stale.rejection_only is True
    assert stale.allowed_decisions == ("reject",)
    with pytest.raises(ProductActionCoordinatorError) as denied:
        coordinator.decide(
            operation_id=prepared.operation_id,
            request={
                "confirmation_token": stale.confirmation_token,
                "decision": "approve",
            },
        )
    assert denied.value.code == "product_action_stale"
    assert handler.executions == 0
    with pytest.raises(ProductActionCoordinatorError) as hidden:
        coordinator.recover_story_rejection_control(
            attempt_id=52,
            operation_id=prepared.operation_id,
        )
    assert hidden.value.code == "interview_story_not_found"
    assert hidden.value.status_code == 404
    rejected = coordinator.decide(
        operation_id=prepared.operation_id,
        request={
            "confirmation_token": stale.confirmation_token,
            "decision": "reject",
        },
    )
    assert rejected.status == "rejected"
    assert handler.executions == 0


def test_fake_story_preclaim_stale_disposition_commits_domain_only(tmp_path) -> None:
    session_factory = init_database(tmp_path / "fake-story-preclaim-stale.sqlite3")
    bootstrap = _coordinator(session_factory)
    handler = _FakeStoryHandler(bootstrap._proof_registry, outcome="mapped_stale")
    coordinator = _coordinator(
        session_factory,
        capability_check=lambda _capability: True,
        additional_handlers=(seal_interview_story_product_action_handler(handler),),
    )
    handler._registry = coordinator._proof_registry
    prepared = _publish_fake_story(coordinator, handler)

    with pytest.raises(ProductActionCoordinatorError) as error:
        coordinator.decide(
            operation_id=prepared.operation_id,
            request={
                "confirmation_token": prepared.confirmation_token,
                "decision": "approve",
            },
        )

    with session_factory() as session:
        operation = session.get(WriteOperation, prepared.operation_id)
        domain_rows = session.scalar(select(func.count()).select_from(Application))
        attempt = session.get(InterviewStoryProposalAttempt, 51)
        transitions = tuple(
            session.scalars(
                select(WriteOperationTransition)
                .where(WriteOperationTransition.operation_id == prepared.operation_id)
                .order_by(WriteOperationTransition.seq)
            )
        )
    assert error.value.code == "product_action_stale"
    assert operation is not None and operation.status == "proposed"
    assert domain_rows == 0
    assert attempt is not None
    assert attempt.attempt_status == "invalidated"
    assert attempt.failure_category == "source_changed"
    assert [(item.seq, item.state) for item in transitions] == [(1, "proposed")]
    assert handler.executions == 0


def test_fake_story_preclaim_disposition_rolls_back_raw_dml_before_exact_cas(
    tmp_path,
) -> None:
    session_factory = init_database(tmp_path / "fake-story-preclaim-bad.sqlite3")
    bootstrap = _coordinator(session_factory)
    handler = _FakeStoryHandler(bootstrap._proof_registry, outcome="mapped_stale_bad")
    coordinator = _coordinator(
        session_factory,
        capability_check=lambda _capability: True,
        additional_handlers=(seal_interview_story_product_action_handler(handler),),
    )
    handler._registry = coordinator._proof_registry
    prepared = _publish_fake_story(coordinator, handler)

    with pytest.raises(ProductActionCoordinatorError) as error:
        coordinator.decide(
            operation_id=prepared.operation_id,
            request={
                "confirmation_token": prepared.confirmation_token,
                "decision": "approve",
            },
        )

    with session_factory() as session:
        operation = session.get(WriteOperation, prepared.operation_id)
        attempt = session.get(InterviewStoryProposalAttempt, 51)
        domain_rows = session.scalar(select(func.count()).select_from(Application))
    assert error.value.code == "product_action_stale"
    assert operation is not None and operation.status == "proposed"
    assert attempt is not None and attempt.attempt_status == "invalidated"
    assert attempt.failure_category == "source_changed"
    assert attempt.proposal_hash == "sha256:" + "4" * 64
    assert domain_rows == 0
    assert handler.executions == 0


def test_fake_story_failed_terminal_codec_rejects_sensitive_extra_fields(tmp_path) -> None:
    session_factory = init_database(tmp_path / "fake-story-failed-codec.sqlite3")
    bootstrap = _coordinator(session_factory)
    handler = _FakeStoryHandler(bootstrap._proof_registry, outcome="declared_conflict")
    original_projector = handler.project_failed_terminal

    def unsafe_failed_projector(operation_id, failure):  # type: ignore[no-untyped-def]
        projection = original_projector(operation_id, failure)
        result = {**projection.result, "sensitive_source": "must-not-persist"}
        return ProductActionTerminalProjectionV1(
            result,
            projection.visible_result,
            projection.transport,
            projection.undo,
        )

    handler.project_failed_terminal = unsafe_failed_projector  # type: ignore[method-assign]
    coordinator = _coordinator(
        session_factory,
        capability_check=lambda _capability: True,
        additional_handlers=(seal_interview_story_product_action_handler(handler),),
    )
    handler._registry = coordinator._proof_registry
    prepared = _publish_fake_story(coordinator, handler)

    with pytest.raises(ProductActionIntegrityError) as error:
        coordinator.decide(
            operation_id=prepared.operation_id,
            request={
                "confirmation_token": prepared.confirmation_token,
                "decision": "approve",
            },
        )
    with session_factory() as session:
        operation = session.get(WriteOperation, prepared.operation_id)
        domain_rows = session.scalar(select(func.count()).select_from(Application))
    assert error.value.code == "product_action_terminal_projector"
    assert operation is not None and operation.status == "proposed"
    assert domain_rows == 0


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_code"),
    [
        (
            "declared_conflict",
            "failed",
            "product_action_story_write_conflict",
        ),
        ("undeclared_error", "proposed", None),
    ],
)
def test_fake_story_executor_savepoint_only_terminalizes_declared_conflict(
    tmp_path,
    outcome,
    expected_status,
    expected_code,
) -> None:
    session_factory = init_database(tmp_path / f"fake-story-{outcome}.sqlite3")
    bootstrap = _coordinator(session_factory)
    handler = _FakeStoryHandler(bootstrap._proof_registry, outcome=outcome)
    coordinator = _coordinator(
        session_factory,
        capability_check=lambda _capability: True,
        additional_handlers=(seal_interview_story_product_action_handler(handler),),
    )
    handler._registry = coordinator._proof_registry
    prepared = _publish_fake_story(coordinator, handler)
    request = {
        "confirmation_token": prepared.confirmation_token,
        "decision": "approve",
    }

    if outcome == "undeclared_error":
        with pytest.raises(RuntimeError, match="must not terminalize"):
            coordinator.decide(operation_id=prepared.operation_id, request=request)
    else:
        result = coordinator.decide(
            operation_id=prepared.operation_id,
            request=request,
        )
        assert result.status == "failed"
        assert result.result["code"] == expected_code
    with session_factory() as session:
        operation = session.get(WriteOperation, prepared.operation_id)
        domain_rows = session.scalar(select(func.count()).select_from(Application))
        transitions = tuple(
            session.scalars(
                select(WriteOperationTransition)
                .where(WriteOperationTransition.operation_id == prepared.operation_id)
                .order_by(WriteOperationTransition.seq)
            )
        )
    assert operation is not None and operation.status == expected_status
    assert domain_rows == 0
    assert [(item.seq, item.state) for item in transitions] == (
        [(1, "proposed")]
        if outcome == "undeclared_error"
        else [
            (1, "proposed"),
            (2, "approved"),
            (3, "claimed"),
            (4, "failed"),
        ]
    )


def test_fake_story_commit_unknown_reports_reconciliation_without_reexecution(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "fake-story-reconcile.sqlite3")
    bootstrap = _coordinator(session_factory)
    handler = _FakeStoryHandler(bootstrap._proof_registry)
    coordinator = _coordinator(
        session_factory,
        capability_check=lambda _capability: True,
        additional_handlers=(seal_interview_story_product_action_handler(handler),),
    )
    handler._registry = coordinator._proof_registry
    prepared = _publish_fake_story(coordinator, handler)
    original_commit = Session.commit
    attempts = 0

    def commit_then_lose_result(session):
        nonlocal attempts
        attempts += 1
        original_commit(session)
        if attempts == 1:
            raise OperationalError(
                "COMMIT",
                {},
                sqlite3.OperationalError("commit result unknown"),
            )

    monkeypatch.setattr(Session, "commit", commit_then_lose_result)
    result = coordinator.decide(
        operation_id=prepared.operation_id,
        request={
            "confirmation_token": prepared.confirmation_token,
            "decision": "approve",
        },
    )

    assert result.completion_kind == "reconciliation"
    assert result.legacy_projection is not None
    assert result.legacy_projection["status_code"] == 200
    assert handler.executions == 1
    assert attempts == 1


def test_fake_story_commit_unknown_before_commit_retries_once_and_converges(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory = init_database(tmp_path / "fake-story-before-commit.sqlite3")
    bootstrap = _coordinator(session_factory)
    handler = _FakeStoryHandler(bootstrap._proof_registry)
    coordinator = _coordinator(
        session_factory,
        capability_check=lambda _capability: True,
        additional_handlers=(seal_interview_story_product_action_handler(handler),),
    )
    handler._registry = coordinator._proof_registry
    prepared = _publish_fake_story(coordinator, handler)
    original_commit = Session.commit
    attempts = 0

    def fail_before_first_commit(session):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OperationalError(
                "COMMIT",
                {},
                sqlite3.OperationalError("commit result unknown"),
            )
        return original_commit(session)

    monkeypatch.setattr(Session, "commit", fail_before_first_commit)
    result = coordinator.decide(
        operation_id=prepared.operation_id,
        request={
            "confirmation_token": prepared.confirmation_token,
            "decision": "approve",
        },
    )

    with session_factory() as session:
        operation = session.get(WriteOperation, prepared.operation_id)
        domain_rows = session.scalar(select(func.count()).select_from(Application))
    assert result.completion_kind == "reconciliation"
    assert result.legacy_projection is not None
    assert result.legacy_projection["status_code"] == 200
    assert attempts == handler.executions == 2
    assert operation is not None and operation.status == "committed"
    assert domain_rows == 1


@pytest.mark.parametrize(
    ("profile", "budgets"),
    [
        ("signal", (4_096, 1_024, 4_096, 4_096)),
        ("story", (4_096, 1_024, 4_096, 32_768)),
    ],
)
@pytest.mark.parametrize(
    ("field", "budget_index"),
    [("result", 0), ("visible", 1), ("transport", 2), ("undo", 3)],
)
def test_action_local_terminal_fields_enforce_utf8_n_and_n_plus_one(
    profile,
    budgets,
    field,
    budget_index,
) -> None:
    del profile
    limit = budgets[budget_index]

    def build(size: int) -> TerminalPayload:
        result: dict[str, JSONValue] = {}
        visible = ""
        transport: dict[str, JSONValue] = {}
        undo: dict[str, JSONValue] = {}
        if field == "result":
            result = _exact_json_bytes(size)
        elif field == "visible":
            visible = _exact_utf8_bytes(size)
        elif field == "transport":
            transport = _exact_json_bytes(size)
        else:
            undo = _exact_json_bytes(size)
        return build_terminal_payload(
            status="committed",
            result_contract="product_action_json_v1",
            result=result,
            visible_result=visible,
            transport=transport,
            undo=undo,
            failure_category=None,
            failure_code=None,
            budgets=budgets,
        )

    exact = build(limit)
    encoded_fields = (
        exact.result_json.encode("utf-8"),
        exact.visible_result.encode("utf-8"),
        exact.transport_json.encode("utf-8"),
        (exact.undo_json or "").encode("utf-8"),
    )
    assert len(encoded_fields[budget_index]) == limit
    with pytest.raises(WriteOperationError, match="operation_result_too_large"):
        build(limit + 1)


@pytest.mark.parametrize("aggregate_limit", [16_384, 49_152])
def test_action_local_terminal_aggregate_enforces_utf8_n_and_n_plus_one(
    aggregate_limit,
) -> None:
    exact = TerminalPayload(
        status="committed",
        result_contract="product_action_json_v1",
        result_json=_exact_utf8_bytes(aggregate_limit),
        visible_result="",
        transport_json="",
        undo_json=None,
        failure_category=None,
        failure_code=None,
        digest="sha256:" + "0" * 64,
    )
    coordinator_module._enforce_action_aggregate(exact, aggregate_limit)
    oversized = TerminalPayload(
        status=exact.status,
        result_contract=exact.result_contract,
        result_json=_exact_utf8_bytes(aggregate_limit + 1),
        visible_result=exact.visible_result,
        transport_json=exact.transport_json,
        undo_json=exact.undo_json,
        failure_category=exact.failure_category,
        failure_code=exact.failure_code,
        digest=exact.digest,
    )
    with pytest.raises(ProductActionCoordinatorError) as error:
        coordinator_module._enforce_action_aggregate(oversized, aggregate_limit)
    assert error.value.code == "product_action_input_too_large"


def test_fake_story_domain_title_cap_precedes_undo_budget_and_rolls_back_all(
    tmp_path,
) -> None:
    session_factory = init_database(tmp_path / "fake-story-budget.sqlite3")
    bootstrap = _coordinator(session_factory)
    handler = _FakeStoryHandler(bootstrap._proof_registry)
    original_projector = handler.project_committed_terminal

    def overflow_projector(operation_id, result):  # type: ignore[no-untyped-def]
        projection = original_projector(operation_id, result)
        projected_result = dict(projection.result)
        projected_result["outcome"] = "version_appended"
        transport = dict(projection.transport)
        transport["result"] = projected_result
        return ProductActionTerminalProjectionV1(
            projected_result,
            projection.visible_result,
            transport,
            {
                "kind": "restore_story_pointer_v1",
                "story_id": 91,
                "created_version_id": 92,
                "previous_current_version_id": 90,
                "previous_title": "界" * 32_769,
                "expected_post_revision": 1,
            },
        )

    handler.project_committed_terminal = overflow_projector  # type: ignore[method-assign]
    coordinator = _coordinator(
        session_factory,
        capability_check=lambda _capability: True,
        additional_handlers=(seal_interview_story_product_action_handler(handler),),
    )
    handler._registry = coordinator._proof_registry
    prepared = _publish_fake_story(coordinator, handler)
    with pytest.raises(ProductActionIntegrityError) as error:
        coordinator.decide(
            operation_id=prepared.operation_id,
            request={
                "confirmation_token": prepared.confirmation_token,
                "decision": "approve",
            },
        )
    with session_factory() as session:
        operation = session.get(WriteOperation, prepared.operation_id)
        domain_rows = session.scalar(select(func.count()).select_from(Application))
    assert operation is not None and operation.status == "proposed"
    assert domain_rows == 0
    assert error.value.code == "product_action_terminal_projector"


@pytest.mark.parametrize(
    "mutation",
    ["result_schema_bool", "transport_schema_bool", "undo_revision_bool"],
)
def test_fake_story_terminal_codec_rejects_bool_for_exact_integer_fields(
    tmp_path,
    mutation,
) -> None:
    session_factory = init_database(tmp_path / f"fake-story-codec-{mutation}.sqlite3")
    bootstrap = _coordinator(session_factory)
    handler = _FakeStoryHandler(bootstrap._proof_registry)
    original_projector = handler.project_committed_terminal

    def bool_projector(operation_id, result):  # type: ignore[no-untyped-def]
        projection = original_projector(operation_id, result)
        projected_result = dict(projection.result)
        transport = dict(projection.transport)
        undo = projection.undo
        if mutation == "result_schema_bool":
            projected_result["schema_version"] = True
            transport["result"] = projected_result
        elif mutation == "transport_schema_bool":
            transport["schema_version"] = True
        else:
            projected_result["outcome"] = "version_appended"
            transport["result"] = projected_result
            undo = {
                "kind": "restore_story_pointer_v1",
                "story_id": 91,
                "created_version_id": 92,
                "previous_current_version_id": 90,
                "previous_title": "原标题",
                "expected_post_revision": True,
            }
        return ProductActionTerminalProjectionV1(
            projected_result,
            projection.visible_result,
            transport,
            undo,
        )

    handler.project_committed_terminal = bool_projector  # type: ignore[method-assign]
    coordinator = _coordinator(
        session_factory,
        capability_check=lambda _capability: True,
        additional_handlers=(seal_interview_story_product_action_handler(handler),),
    )
    handler._registry = coordinator._proof_registry
    prepared = _publish_fake_story(coordinator, handler)

    with pytest.raises(ProductActionIntegrityError) as error:
        coordinator.decide(
            operation_id=prepared.operation_id,
            request={
                "confirmation_token": prepared.confirmation_token,
                "decision": "approve",
            },
        )
    with session_factory() as session:
        operation = session.get(WriteOperation, prepared.operation_id)
        domain_rows = session.scalar(select(func.count()).select_from(Application))
    assert error.value.code == "product_action_terminal_projector"
    assert operation is not None and operation.status == "proposed"
    assert domain_rows == 0


@pytest.mark.parametrize(
    ("title_length", "expected_status"),
    [(200, "committed"), (201, "proposed")],
)
def test_fake_story_restore_title_uses_exact_domain_boundary(
    tmp_path,
    title_length,
    expected_status,
) -> None:
    session_factory = init_database(
        tmp_path / f"fake-story-title-{title_length}.sqlite3"
    )
    bootstrap = _coordinator(session_factory)
    handler = _FakeStoryHandler(bootstrap._proof_registry)
    original_projector = handler.project_committed_terminal

    def restore_projector(operation_id, result):  # type: ignore[no-untyped-def]
        projection = original_projector(operation_id, result)
        projected_result = dict(projection.result)
        projected_result["outcome"] = "version_appended"
        transport = dict(projection.transport)
        transport["result"] = projected_result
        return ProductActionTerminalProjectionV1(
            projected_result,
            projection.visible_result,
            transport,
            {
                "kind": "restore_story_pointer_v1",
                "story_id": 91,
                "created_version_id": 92,
                "previous_current_version_id": 90,
                "previous_title": "题" * title_length,
                "expected_post_revision": 1,
            },
        )

    handler.project_committed_terminal = restore_projector  # type: ignore[method-assign]
    coordinator = _coordinator(
        session_factory,
        capability_check=lambda _capability: True,
        additional_handlers=(seal_interview_story_product_action_handler(handler),),
    )
    handler._registry = coordinator._proof_registry
    prepared = _publish_fake_story(coordinator, handler)

    if expected_status == "committed":
        result = coordinator.decide(
            operation_id=prepared.operation_id,
            request={
                "confirmation_token": prepared.confirmation_token,
                "decision": "approve",
            },
        )
        assert result.status == "committed"
    else:
        with pytest.raises(ProductActionIntegrityError) as error:
            coordinator.decide(
                operation_id=prepared.operation_id,
                request={
                    "confirmation_token": prepared.confirmation_token,
                    "decision": "approve",
                },
            )
        assert error.value.code == "product_action_terminal_projector"
    with session_factory() as session:
        operation = session.get(WriteOperation, prepared.operation_id)
        domain_rows = session.scalar(select(func.count()).select_from(Application))
    assert operation is not None and operation.status == expected_status
    assert domain_rows == (1 if expected_status == "committed" else 0)


@pytest.mark.parametrize("mutation", ["missing_replay", "nonexact_status"])
def test_fake_story_transport_must_freeze_both_legacy_outcomes(
    tmp_path,
    mutation,
) -> None:
    session_factory = init_database(tmp_path / f"fake-story-transport-{mutation}.sqlite3")
    bootstrap = _coordinator(session_factory)
    handler = _FakeStoryHandler(bootstrap._proof_registry)
    original_projector = handler.project_committed_terminal

    def incomplete_projector(operation_id, result):  # type: ignore[no-untyped-def]
        projection = original_projector(operation_id, result)
        transport = dict(projection.transport)
        if mutation == "missing_replay":
            transport.pop("legacy_reconciliation_or_replay")
        else:
            direct = dict(transport["legacy_direct_commit"])
            direct["status_code"] = 201.0
            transport["legacy_direct_commit"] = direct
        return ProductActionTerminalProjectionV1(
            projection.result,
            projection.visible_result,
            transport,
            projection.undo,
        )

    handler.project_committed_terminal = incomplete_projector  # type: ignore[method-assign]
    coordinator = _coordinator(
        session_factory,
        capability_check=lambda _capability: True,
        additional_handlers=(seal_interview_story_product_action_handler(handler),),
    )
    handler._registry = coordinator._proof_registry
    prepared = _publish_fake_story(coordinator, handler)
    with pytest.raises(ProductActionIntegrityError) as error:
        coordinator.decide(
            operation_id=prepared.operation_id,
            request={
                "confirmation_token": prepared.confirmation_token,
                "decision": "approve",
            },
        )
    with session_factory() as session:
        operation = session.get(WriteOperation, prepared.operation_id)
        domain_rows = session.scalar(select(func.count()).select_from(Application))
    assert error.value.code == "product_action_terminal_projector"
    assert operation is not None and operation.status == "proposed"
    assert domain_rows == 0
