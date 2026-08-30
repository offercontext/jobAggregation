from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from offerpilot.ai.write_operations import (
    LedgerKeyDomain,
    build_terminal_payload,
    ledger_fingerprint,
)
from offerpilot.models import (
    InterviewStoryProposalAttempt,
    ProductActionProposal,
    WriteOperation,
    WriteOperationTransition,
)
from offerpilot.product_actions.catalog import ProductActionCatalogV1
from offerpilot.product_actions.contracts import (
    HistoricalStoryRouteProof,
    ProductActionContractError,
    ProductActionIntegrityError,
    ProductActionProofRegistryV1,
)
from offerpilot.product_actions.issuer import (
    InterviewStoryActionIssuer,
    LedgerKeyProfileStoreV1,
    ReviewReadinessActionIssuer,
)
from offerpilot.product_actions.repository import ProductActionProposalRepository
from tests.product_actions.conftest import (
    KEY_ONE,
    KEY_TWO,
    raw_json,
    signal_route,
    story_route,
)


def _repository(
    sessions: Any,
    product_core: tuple[object, ...],
) -> tuple[ProductActionProposalRepository, ReviewReadinessActionIssuer]:
    catalog, registry, profiles, signal_issuer, _story_issuer = product_core
    return (
        ProductActionProposalRepository(
            sessions,
            catalog=catalog,
            proof_registry=registry,
            key_profiles=profiles,
        ),
        signal_issuer,
    )


def _terminalize_rejected(sessions: Any, operation_id: str, key: LedgerKeyDomain) -> None:
    with sessions() as session:
        operation = session.get(WriteOperation, operation_id)
        assert operation is not None
        request_fingerprint = ledger_fingerprint(
            key,
            "product-action-request-v1",
            {
                "operation_id": operation.id,
                "decision": "reject",
            },
        )
        payload = build_terminal_payload(
            status="rejected",
            result_contract="rejection_json_v1",
            result={
                "schema_version": 1,
                "action_name": operation.tool_name,
                "decision": "rejected",
            },
            visible_result="已取消保存准备重点。",
            transport={
                "schema_version": 1,
                "operation_id": operation.id,
                "action_name": operation.tool_name,
                "status": "rejected",
                "result": {"decision": "rejected"},
            },
            undo=None,
            failure_category=None,
            failure_code=None,
        )
        now = datetime.now(timezone.utc)
        operation.status = "rejected"
        operation.operation_request_fingerprint = request_fingerprint
        operation.result_contract = payload.result_contract
        operation.result_json = payload.result_json
        operation.visible_result = payload.visible_result
        operation.transport_json = payload.transport_json
        operation.terminal_payload_sha256 = payload.digest
        operation.rejected_at = now
        operation.delivery_status = "not_applicable"
        operation.delivery_generation = 0
        operation.delivery_outcome = "none"
        operation.delivery_message_count = 0
        operation.delivered_at = now
        session.add(
            WriteOperationTransition(
                id=str(uuid4()),
                operation_id=operation_id,
                seq=2,
                state="rejected",
            )
        )
        session.commit()


def _insert_historical_ready_attempt(sessions: Any) -> None:
    with sessions() as session:
        session.add(
            InterviewStoryProposalAttempt(
                id=51,
                target_story_id=52,
                idempotency_key="historical_story_attempt_0051",
                entrypoint="ui",
                entry_context_json="{}",
                attempt_status="ready",
                generation_revision=6,
                provider_call_token="",
                provider_lease_until=None,
                input_snapshot_json="{}",
                source_fingerprint="sha256:" + "5" * 64,
                proposal_json="{}",
                proposal_hash="sha256:" + "4" * 64,
                repair_count=0,
                failure_category="",
                confirmation_token_hash="",
                confirmation_payload_hash="",
                confirmed_story_id=None,
                confirmed_story_version_id=None,
                product_action_operation_id=None,
                product_action_generation=0,
                confirmed_at=None,
            )
        )
        session.commit()


def test_publication_writes_only_the_exact_parent_route_seq1_bundle_and_returns_preissued_token(
    product_database: Any,
    product_core: tuple[object, ...],
) -> None:
    repository, issuer = _repository(product_database, product_core)
    prepared = issuer.prepare(route_payload_raw=raw_json(signal_route()))
    preissued_token = prepared.confirmation_token
    product_core[2].activate(KEY_TWO.key_id)  # type: ignore[attr-defined]

    result = repository.publish_bundle(prepared)

    assert result.classification == "exact_proposed"
    assert result.created is True
    assert result.confirmation_token == preissued_token
    with product_database() as session:
        operation = session.get(WriteOperation, prepared.operation_id)
        route = session.get(ProductActionProposal, prepared.operation_id)
        transitions = list(
            session.scalars(
                select(WriteOperationTransition)
                .where(WriteOperationTransition.operation_id == prepared.operation_id)
                .order_by(WriteOperationTransition.seq)
            )
        )
        assert operation is not None and route is not None
        assert operation.operation_role == "primary"
        assert operation.adapter_kind == "product_action"
        assert operation.conversation_id is None
        assert operation.agent_run_id is None
        assert operation.tool_call_id == prepared.action_call_id == route.action_call_id
        assert operation.fingerprint_key_id == KEY_ONE.key_id
        assert operation.confirmation_token_fingerprint == prepared.confirmation_token_fingerprint
        assert route.route_payload_json == prepared.route_payload_json
        assert [(item.seq, item.state) for item in transitions] == [(1, "proposed")]


def test_session_bound_publication_joins_caller_uow_and_never_commits_or_rolls_back(
    product_database: Any,
    product_core: tuple[object, ...],
) -> None:
    repository, issuer = _repository(product_database, product_core)
    prepared = issuer.prepare(route_payload_raw=raw_json(signal_route()))
    _insert_historical_ready_attempt(product_database)

    with product_database() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        attempt = session.get(InterviewStoryProposalAttempt, 51)
        assert attempt is not None
        attempt.failure_category = "caller-uow-marker"
        result = repository.publish_bundle_in_session(session, prepared)
        assert result.created is True
        assert session.in_transaction()
        assert session.get(WriteOperation, prepared.operation_id) is not None
        session.rollback()

    assert repository.reconcile_publication(prepared).classification == "all_absent"
    with product_database() as session:
        attempt = session.get(InterviewStoryProposalAttempt, 51)
        assert attempt is not None
        assert attempt.failure_category == ""


def test_session_bound_publication_requires_caller_owned_transaction_before_sql(
    product_database: Any,
    product_core: tuple[object, ...],
) -> None:
    repository, issuer = _repository(product_database, product_core)
    prepared = issuer.prepare(route_payload_raw=raw_json(signal_route()))

    with product_database() as session:
        with pytest.raises(ProductActionContractError, match="publication_uow_required"):
            repository.publish_bundle_in_session(session, prepared)

    fresh = issuer.prepare(route_payload_raw=raw_json(signal_route()))
    with product_database() as session, session.begin():
        with pytest.raises(ProductActionContractError, match="publication_uow_required"):
            repository.publish_bundle_in_session(session, fresh)

    assert repository.reconcile_publication(prepared).classification == "all_absent"


def test_session_bound_load_reuses_caller_writer_session_and_full_integrity_validation(
    product_database: Any,
    product_core: tuple[object, ...],
) -> None:
    repository, issuer = _repository(product_database, product_core)
    prepared = issuer.prepare(route_payload_raw=raw_json(signal_route()))
    repository.publish_bundle(prepared)

    with product_database() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        loaded = repository.load_bundle_in_session(session, prepared.operation_id)
        assert loaded.classification == "exact_proposed"
        assert loaded.transition_prefix == ((1, "proposed"),)
        assert session.in_transaction()
        session.rollback()

    with product_database() as session, session.begin():
        with pytest.raises(ProductActionContractError, match="publication_uow_required"):
            repository.load_bundle_in_session(session, prepared.operation_id)


def test_historical_story_bridge_requires_locked_exact_legacy_ready_attempt_and_baseline_token(
    product_database: Any,
    product_core: tuple[object, ...],
) -> None:
    catalog, registry, profiles, _signal_issuer, story_issuer = product_core
    assert isinstance(story_issuer, InterviewStoryActionIssuer)
    repository = ProductActionProposalRepository(
        product_database,
        catalog=catalog,  # type: ignore[arg-type]
        proof_registry=registry,  # type: ignore[arg-type]
        key_profiles=profiles,  # type: ignore[arg-type]
    )
    route = raw_json(story_route(product_action_generation=1))

    with pytest.raises(ProductActionContractError, match="historical_story_bridge_repository_only"):
        story_issuer.prepare(
            route_payload_raw=route,
            request_origin="historical_story_bridge",
            historical_confirmation_token="legacy_token_0001",
        )

    _insert_historical_ready_attempt(product_database)
    with product_database() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        result = repository.publish_historical_story_bridge_in_session(
            session,
            issuer=story_issuer,
            route_payload_raw=route,
            legacy_confirmation_token="legacy_token_0001",
        )
        assert result.classification == "exact_proposed"
        assert result.created is True
        assert result.bundle is not None
        assert result.bundle.route.request_origin == "historical_story_bridge"
        assert result.bundle.route.historical_request_token_fingerprint is not None
        assert {
            record.proof_type for record in registry._records.values()  # type: ignore[attr-defined]
        } == {HistoricalStoryRouteProof}
        session.rollback()


@pytest.mark.parametrize(
    ("attempt_change", "token"),
    [
        ({"attempt_status": "confirmed"}, "legacy_token_0001"),
        ({"generation_revision": 7}, "legacy_token_0001"),
        ({"proposal_hash": "sha256:" + "9" * 64}, "legacy_token_0001"),
        ({"confirmation_token_hash": "sha256:" + "8" * 64}, "legacy_token_0001"),
        ({}, "short"),
        ({}, "legacy token with spaces"),
    ],
)
def test_historical_story_bridge_rejects_every_non_baseline_attempt_or_token_before_signing(
    product_database: Any,
    product_core: tuple[object, ...],
    attempt_change: dict[str, object],
    token: str,
) -> None:
    catalog, registry, profiles, _signal_issuer, story_issuer = product_core
    repository = ProductActionProposalRepository(
        product_database,
        catalog=catalog,  # type: ignore[arg-type]
        proof_registry=registry,  # type: ignore[arg-type]
        key_profiles=profiles,  # type: ignore[arg-type]
    )
    _insert_historical_ready_attempt(product_database)
    with product_database() as session:
        attempt = session.get(InterviewStoryProposalAttempt, 51)
        assert attempt is not None
        for name, value in attempt_change.items():
            setattr(attempt, name, value)
        session.commit()

    with product_database() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        with pytest.raises(ProductActionContractError, match="historical_story_bridge"):
            repository.publish_historical_story_bridge_in_session(
                session,
                issuer=story_issuer,  # type: ignore[arg-type]
                route_payload_raw=raw_json(story_route(product_action_generation=1)),
                legacy_confirmation_token=token,
            )
        session.rollback()

    with product_database() as session:
        assert session.scalar(select(text("count(*)")).select_from(WriteOperation)) == 0


def test_exact_retry_uses_a_fresh_proof_but_same_identity_and_does_not_insert_alias(
    product_database: Any,
    product_core: tuple[object, ...],
) -> None:
    repository, issuer = _repository(product_database, product_core)
    first = issuer.prepare(route_payload_raw=raw_json(signal_route()))
    assert repository.publish_bundle(first).created is True
    retry = issuer.prepare(route_payload_raw=raw_json(signal_route()))

    replay = repository.publish_bundle(retry)

    assert replay.classification == "exact_proposed"
    assert replay.created is False
    assert replay.confirmation_token == first.confirmation_token
    with product_database() as session:
        assert session.scalar(select(WriteOperation).where(WriteOperation.id == first.operation_id))
        assert session.scalar(select(ProductActionProposal)) is not None
        assert session.scalar(select(WriteOperationTransition)) is not None
        assert session.scalar(select(text("count(*)")).select_from(WriteOperation)) == 1


def test_load_and_reconciliation_validate_exact_terminal_prefix_without_decision_fingerprint(
    product_database: Any,
    product_core: tuple[object, ...],
) -> None:
    repository, issuer = _repository(product_database, product_core)
    prepared = issuer.prepare(route_payload_raw=raw_json(signal_route()))
    repository.publish_bundle(prepared)
    _terminalize_rejected(product_database, prepared.operation_id, KEY_ONE)

    loaded = repository.load_bundle(prepared.operation_id)
    reconciled = repository.reconcile_publication(prepared)

    assert loaded.classification == "exact_terminal"
    assert loaded.operation.status == "rejected"
    assert loaded.route.route_payload_json is None
    assert loaded.transition_prefix == ((1, "proposed"), (2, "rejected"))
    assert reconciled.classification == "exact_terminal"
    assert reconciled.confirmation_token is None
    with pytest.raises(ProductActionIntegrityError, match="action_identity_mismatch"):
        issuer.recover_confirmation_token(loaded)


@pytest.mark.parametrize(
    ("table", "column", "expected_error"),
    [
        ("product_action_proposals", "route_binding_fingerprint", "proposal_fingerprint"),
        ("write_operations", "proposal_fingerprint", "proposal_fingerprint"),
        (
            "write_operations",
            "authorization_scope_fingerprint",
            "proposal_fingerprint",
        ),
        (
            "write_operations",
            "confirmation_token_fingerprint",
            "confirmation_token_fingerprint",
        ),
    ],
)
def test_terminal_load_revalidates_persisted_proposal_route_and_token_hmac_chain(
    product_database: Any,
    product_core: tuple[object, ...],
    table: str,
    column: str,
    expected_error: str,
) -> None:
    repository, issuer = _repository(product_database, product_core)
    prepared = issuer.prepare(route_payload_raw=raw_json(signal_route()))
    repository.publish_bundle(prepared)
    _terminalize_rejected(product_database, prepared.operation_id, KEY_ONE)
    missing_registry = ProductActionProofRegistryV1()
    missing_catalog = ProductActionCatalogV1(missing_registry)
    missing_profiles = LedgerKeyProfileStoreV1((KEY_TWO,), active_key_id=KEY_TWO.key_id)
    missing_repository = ProductActionProposalRepository(
        product_database,
        catalog=missing_catalog,
        proof_registry=missing_registry,
        key_profiles=missing_profiles,
    )
    with pytest.raises(ProductActionIntegrityError, match="missing_key_profile"):
        missing_repository.load_bundle(prepared.operation_id)
    engine = product_database.kw["bind"]
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.exec_driver_sql(
            "DROP TRIGGER IF EXISTS trg_product_action_route_identity_immutable"
        )
        connection.exec_driver_sql(
            "DROP TRIGGER IF EXISTS trg_write_operation_scope_identity_immutable"
        )
        connection.exec_driver_sql(
            "DROP TRIGGER IF EXISTS trg_write_operation_scope_fingerprint_immutable"
        )
        connection.exec_driver_sql(
            "DROP TRIGGER IF EXISTS trg_write_operation_terminal_immutable"
        )
        connection.exec_driver_sql(
            f"UPDATE {table} SET {column}=? WHERE "
            + ("operation_id=?" if table == "product_action_proposals" else "id=?"),
            ("hmac-sha256:" + "9" * 64, prepared.operation_id),
        )
        connection.commit()

    with pytest.raises(ProductActionIntegrityError, match=expected_error):
        repository.load_bundle(prepared.operation_id)
    with pytest.raises(ProductActionIntegrityError, match=expected_error):
        repository.reconcile_publication(prepared)


@pytest.mark.parametrize(
    "corruption",
    [
        "parent_only",
        "route_only",
        "transition_only",
        "parent_route",
        "parent_transition",
        "route_transition",
        "extra_transition",
        "duplicate_state",
        "wrong_state",
        "wrong_order",
    ],
)
def test_every_partial_or_malformed_bundle_is_integrity_failure_and_never_repaired(
    product_database: Any,
    product_core: tuple[object, ...],
    corruption: str,
) -> None:
    repository, issuer = _repository(product_database, product_core)
    prepared = issuer.prepare(route_payload_raw=raw_json(signal_route()))
    repository.publish_bundle(prepared)
    engine = product_database.kw["bind"]
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.exec_driver_sql("DROP TRIGGER IF EXISTS trg_product_action_route_delete")
        connection.exec_driver_sql("DROP TRIGGER IF EXISTS trg_write_operation_transition_delete")
        connection.exec_driver_sql("DROP TRIGGER IF EXISTS trg_write_operation_transition_insert")
        connection.exec_driver_sql("DROP TRIGGER IF EXISTS trg_write_operation_transition_immutable")
        if corruption == "parent_only":
            connection.exec_driver_sql(
                "DELETE FROM product_action_proposals WHERE operation_id=?",
                (prepared.operation_id,),
            )
            connection.exec_driver_sql(
                "DELETE FROM write_operation_transitions WHERE operation_id=?",
                (prepared.operation_id,),
            )
        elif corruption == "route_only":
            connection.exec_driver_sql(
                "DELETE FROM write_operation_transitions WHERE operation_id=?",
                (prepared.operation_id,),
            )
            connection.exec_driver_sql(
                "DELETE FROM write_operations WHERE id=?",
                (prepared.operation_id,),
            )
        elif corruption == "transition_only":
            connection.exec_driver_sql(
                "DELETE FROM product_action_proposals WHERE operation_id=?",
                (prepared.operation_id,),
            )
            connection.exec_driver_sql(
                "DELETE FROM write_operations WHERE id=?",
                (prepared.operation_id,),
            )
        elif corruption == "parent_route":
            connection.exec_driver_sql(
                "DELETE FROM write_operation_transitions WHERE operation_id=?",
                (prepared.operation_id,),
            )
        elif corruption == "parent_transition":
            connection.exec_driver_sql(
                "DELETE FROM product_action_proposals WHERE operation_id=?",
                (prepared.operation_id,),
            )
        elif corruption == "route_transition":
            connection.exec_driver_sql(
                "DELETE FROM write_operations WHERE id=?",
                (prepared.operation_id,),
            )
        elif corruption == "extra_transition":
            connection.exec_driver_sql(
                "INSERT INTO write_operation_transitions(id,operation_id,seq,state) "
                "VALUES (?,?,2,'approved')",
                (str(uuid4()), prepared.operation_id),
            )
        elif corruption == "duplicate_state":
            connection.exec_driver_sql(
                "INSERT INTO write_operation_transitions(id,operation_id,seq,state) "
                "VALUES (?,?,2,'proposed')",
                (str(uuid4()), prepared.operation_id),
            )
        elif corruption == "wrong_state":
            connection.exec_driver_sql(
                "UPDATE write_operation_transitions SET state='approved' "
                "WHERE operation_id=? AND seq=1",
                (prepared.operation_id,),
            )
        else:
            connection.exec_driver_sql(
                "UPDATE write_operation_transitions SET seq=2 "
                "WHERE operation_id=? AND seq=1",
                (prepared.operation_id,),
            )
        connection.commit()

    with pytest.raises(ProductActionIntegrityError):
        repository.load_bundle(prepared.operation_id)
    with pytest.raises(ProductActionIntegrityError):
        repository.reconcile_publication(prepared)
    with product_database() as session:
        rows = session.execute(
            text(
                "SELECT count(*) FROM write_operations WHERE id=:id "
                "UNION ALL SELECT count(*) FROM product_action_proposals WHERE operation_id=:id "
                "UNION ALL SELECT count(*) FROM write_operation_transitions WHERE operation_id=:id"
            ),
            {"id": prepared.operation_id},
        ).scalars().all()
    assert sum(rows) < 3 or corruption in {
        "extra_transition",
        "duplicate_state",
        "wrong_state",
        "wrong_order",
    }


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("product_action_proposals", "route_payload_fingerprint", "malformed"),
        (
            "product_action_proposals",
            "action_call_id",
            "99999999-9999-4999-8999-999999999999",
        ),
        (
            "write_operations",
            "fingerprint_key_id",
            "99999999-9999-4999-8999-999999999999",
        ),
    ],
)
def test_malformed_mismatched_or_missing_key_bundle_fails_closed_as_integrity(
    product_database: Any,
    product_core: tuple[object, ...],
    table: str,
    column: str,
    value: str,
) -> None:
    repository, issuer = _repository(product_database, product_core)
    prepared = issuer.prepare(route_payload_raw=raw_json(signal_route()))
    repository.publish_bundle(prepared)
    engine = product_database.kw["bind"]
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.exec_driver_sql(
            "DROP TRIGGER IF EXISTS trg_product_action_route_identity_immutable"
        )
        connection.exec_driver_sql(
            "DROP TRIGGER IF EXISTS trg_write_operation_scope_identity_immutable"
        )
        connection.exec_driver_sql(
            f"UPDATE {table} SET {column}=? WHERE "
            + ("operation_id=?" if table == "product_action_proposals" else "id=?"),
            (value, prepared.operation_id),
        )
        connection.commit()

    with pytest.raises(ProductActionIntegrityError):
        repository.load_bundle(prepared.operation_id)
    with pytest.raises(ProductActionIntegrityError):
        repository.reconcile_publication(prepared)


def test_all_absent_reconciliation_is_read_only_and_does_not_publish(
    product_database: Any,
    product_core: tuple[object, ...],
) -> None:
    repository, issuer = _repository(product_database, product_core)
    prepared = issuer.prepare(route_payload_raw=raw_json(signal_route()))

    result = repository.reconcile_publication(prepared)

    assert result.classification == "all_absent"
    with product_database() as session:
        assert session.scalar(select(text("count(*)")).select_from(WriteOperation)) == 0
        assert session.scalar(select(text("count(*)")).select_from(ProductActionProposal)) == 0


def test_unreadable_reconciliation_is_bounded_and_never_generates_a_new_key(
    product_database: Any,
    product_core: tuple[object, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, issuer = _repository(product_database, product_core)
    prepared = issuer.prepare(route_payload_raw=raw_json(signal_route()))
    profiles = product_core[2]
    active_before = profiles.active().key_id  # type: ignore[attr-defined]

    def unreadable(*_args: object, **_kwargs: object) -> object:
        raise OperationalError("SELECT", {}, RuntimeError("unreadable"))

    monkeypatch.setattr(repository, "_load_rows", unreadable)
    result = repository.reconcile_publication(prepared)

    assert result.classification == "unreadable"
    assert profiles.active().key_id == active_before  # type: ignore[attr-defined]


def test_restart_recovery_uses_stored_key_after_rotation_and_missing_profile_fails_closed(
    product_database: Any,
    product_core: tuple[object, ...],
) -> None:
    repository, issuer = _repository(product_database, product_core)
    prepared = issuer.prepare(route_payload_raw=raw_json(signal_route()))
    repository.publish_bundle(prepared)
    product_core[2].activate(KEY_TWO.key_id)  # type: ignore[attr-defined]
    bundle = repository.load_bundle(prepared.operation_id)

    assert issuer.recover_confirmation_token(bundle) == prepared.confirmation_token

    missing_registry = ProductActionProofRegistryV1()
    missing_catalog = ProductActionCatalogV1(missing_registry)
    missing_profiles = LedgerKeyProfileStoreV1((KEY_TWO,), active_key_id=KEY_TWO.key_id)
    restarted = ReviewReadinessActionIssuer(
        missing_catalog,
        missing_registry,
        missing_profiles,
    )
    with pytest.raises(ProductActionIntegrityError, match="missing_key_profile"):
        restarted.recover_confirmation_token(bundle)


@pytest.mark.parametrize("field", ["schema_version", "source_id", "source_revision"])
@pytest.mark.parametrize("invalid", [True, 1.0, "1"])
def test_repository_revalidates_every_direct_integer_before_opening_a_session(
    product_database: Any,
    product_core: tuple[object, ...],
    field: str,
    invalid: object,
) -> None:
    repository, issuer = _repository(product_database, product_core)
    prepared = issuer.prepare(route_payload_raw=raw_json(signal_route()))
    object.__setattr__(prepared, field, invalid)
    calls = 0

    def forbidden_session() -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("invalid integer reached SQL")

    repository.session_factory = forbidden_session  # type: ignore[assignment]
    with pytest.raises(ProductActionContractError, match="exact_integer"):
        repository.publish_bundle(prepared)
    assert calls == 0


def test_publication_cleanup_propagates_baseexception_and_revokes_route_proof(
    product_database: Any,
    product_core: tuple[object, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, issuer = _repository(product_database, product_core)
    prepared = issuer.prepare(route_payload_raw=raw_json(signal_route()))

    def interrupted(_prepared: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(repository, "_publish_once", interrupted)
    with pytest.raises(KeyboardInterrupt):
        repository.publish_bundle(prepared)
    with pytest.raises(ValueError, match="revoked"):
        product_core[1].claim(  # type: ignore[attr-defined]
            prepared.route_proof,
            proof_type=type(prepared.route_proof),
            action_name=prepared.action_name,
            expected_binding=prepared.proof_binding,
        )


def test_commit_unknown_all_absent_replays_at_most_once(
    product_database: Any,
    product_core: tuple[object, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, issuer = _repository(product_database, product_core)
    prepared = issuer.prepare(route_payload_raw=raw_json(signal_route()))
    real_publish_once = repository._publish_once  # noqa: SLF001
    calls = 0

    def first_commit_unknown_then_real(item: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError("COMMIT", {}, RuntimeError("lost response"))
        return real_publish_once(item)  # type: ignore[arg-type]

    monkeypatch.setattr(repository, "_publish_once", first_commit_unknown_then_real)

    result = repository.publish_bundle(prepared)

    assert result.classification == "exact_proposed"
    assert result.created is True
    assert calls == 2


def test_commit_unknown_exact_bundle_reconciles_in_a_fresh_session_without_replay(
    product_database: Any,
    product_core: tuple[object, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, issuer = _repository(product_database, product_core)
    prepared = issuer.prepare(route_payload_raw=raw_json(signal_route()))
    real_publish_once = repository._publish_once  # noqa: SLF001
    calls = 0

    def commit_then_lose_response(item: object) -> object:
        nonlocal calls
        calls += 1
        real_publish_once(item)  # type: ignore[arg-type]
        raise OperationalError("COMMIT", {}, RuntimeError("lost response"))

    monkeypatch.setattr(repository, "_publish_once", commit_then_lose_response)

    result = repository.publish_bundle(prepared)

    assert result.classification == "exact_proposed"
    assert result.created is False
    assert result.confirmation_token == prepared.confirmation_token
    assert calls == 1
