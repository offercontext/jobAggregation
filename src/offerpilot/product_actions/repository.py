"""Session-owned publication and integrity reads for Product Action bundles."""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass, fields
from types import SimpleNamespace
from typing import Any, Literal, NoReturn, SupportsIndex, cast

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from offerpilot.ai.write_operations import payload_from_operation
from offerpilot.models import (
    InterviewStoryProposalAttempt,
    ProductActionProposal,
    WriteOperation,
    WriteOperationTransition,
)
from offerpilot.product_actions.catalog import ProductActionCatalogV1
from offerpilot.product_actions.contracts import (
    EXPECTED_PREFIX,
    PRODUCT_ACTION_NAMES,
    HistoricalStoryRouteProof,
    ProductActionContractError,
    ProductActionIntegrityError,
    ProductActionProofRegistryV1,
    ProductActionRouteProof,
    decode_product_action_route_payload,
    require_product_action_hmac,
    require_product_action_uuid,
)
from offerpilot.product_actions.issuer import (
    InterviewStoryActionIssuer,
    LedgerKeyProfileStoreV1,
    PreparedProductActionProposalV1,
    _derive,
    _derive_historical_request_token_fingerprint,
    _derive_persisted_proposal_identity,
    _prepared_from_derived_identity,
    _proof_binding,
    _route_payload,
    validate_prepared_product_action,
)


PublicationClassification = Literal[
    "all_absent",
    "exact_proposed",
    "exact_terminal",
    "unreadable",
]


@dataclass(frozen=True, slots=True, repr=False)
class ProductActionOperationSnapshotV1:
    id: str
    operation_role: str
    parent_operation_id: str | None
    parent_terminal_payload_sha256: str | None
    conversation_id: int | None
    agent_run_id: str | None
    tool_call_id: str | None
    tool_name: str
    adapter_kind: str
    status: str
    fingerprint_key_id: str
    proposal_fingerprint: str | None
    input_fingerprint: str | None
    confirmation_token_fingerprint: str | None
    authorization_scope_fingerprint: str | None
    operation_request_fingerprint: str | None
    result_contract: str | None
    result_json: str | None
    visible_result: str | None
    transport_json: str | None
    undo_json: str | None
    terminal_payload_sha256: str | None
    failure_category: str | None
    failure_code: str | None
    delivery_status: str
    delivery_failure_code: str | None
    delivery_generation: int
    delivery_owner_token_fingerprint: str | None
    delivery_lease_expires_at: int | None
    delivery_outcome: str | None
    delivery_message_count: int | None
    delivery_manifest_sha256: str | None
    delivery_next_operation_id: str | None
    delivered_at: object | None
    approved_at: object | None
    claimed_at: object | None
    rejected_at: object | None
    committed_at: object | None
    failed_at: object | None
    created_at: object
    updated_at: object


@dataclass(frozen=True, slots=True, repr=False)
class ProductActionRouteSnapshotV1:
    operation_id: str
    action_call_id: str
    action_name: str
    request_origin: str
    schema_version: int
    source_kind: str
    source_id: int
    source_revision: int
    route_payload_json: str | None
    route_payload_fingerprint: str
    route_binding_fingerprint: str
    request_idempotency_fingerprint: str
    semantic_claim_fingerprint: str | None
    historical_request_token_fingerprint: str | None
    created_at: object
    terminalized_at: object | None


@dataclass(frozen=True, slots=True, repr=False)
class ProductActionBundleV1:
    classification: Literal["exact_proposed", "exact_terminal"]
    operation: ProductActionOperationSnapshotV1
    route: ProductActionRouteSnapshotV1
    transition_prefix: tuple[tuple[int, str], ...]


@dataclass(frozen=True, slots=True, repr=False)
class ProductActionPublicationV1:
    classification: PublicationClassification
    operation_id: str
    action_call_id: str
    confirmation_token: str | None
    created: bool
    bundle: ProductActionBundleV1 | None = None


_PUBLICATION_UOW_CONSTRUCTION_SEAL = object()


class ProductActionPublicationUoWV1:
    """Opaque evidence for one Repository-issued BEGIN IMMEDIATE transaction."""

    __slots__ = (
        "_repository",
        "_session",
        "_transaction",
        "_driver_connection",
        "_seal",
    )
    _repository: object
    _session: Session
    _transaction: object
    _driver_connection: object
    _seal: tuple[object, Session, object, object]

    def __new__(
        cls,
        construction_seal: object | None = None,
        *_args: object,
        **_kwargs: object,
    ) -> ProductActionPublicationUoWV1:
        if construction_seal is not _PUBLICATION_UOW_CONSTRUCTION_SEAL:
            raise TypeError("Product Action publication UoW is Repository-issued")
        return object.__new__(cls)

    def __init__(
        self,
        construction_seal: object | None = None,
        repository: object | None = None,
        session: Session | None = None,
        transaction: object | None = None,
        driver_connection: object | None = None,
    ) -> None:
        if construction_seal is not _PUBLICATION_UOW_CONSTRUCTION_SEAL or any(
            value is None
            for value in (repository, session, transaction, driver_connection)
        ):
            raise TypeError("Product Action publication UoW is Repository-issued")
        object.__setattr__(self, "_repository", repository)
        object.__setattr__(self, "_session", session)
        object.__setattr__(self, "_transaction", transaction)
        object.__setattr__(self, "_driver_connection", driver_connection)
        object.__setattr__(
            self,
            "_seal",
            (repository, session, transaction, driver_connection),
        )

    def __setattr__(self, name: str, value: object) -> NoReturn:
        del name, value
        raise AttributeError("Product Action publication UoW is sealed")

    def __repr__(self) -> str:
        return "<ProductActionPublicationUoWV1>"

    @staticmethod
    def _serialization_error() -> NoReturn:
        raise TypeError("Product Action publication UoW cannot be copied or serialized")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        del protocol
        self._serialization_error()

    def __getstate__(self) -> NoReturn:
        self._serialization_error()

    def __copy__(self) -> NoReturn:
        self._serialization_error()

    def __deepcopy__(self, memo: dict[int, object]) -> NoReturn:
        del memo
        self._serialization_error()


@dataclass(frozen=True, slots=True, repr=False)
class _RawBundleRows:
    operation: dict[str, Any] | None
    route: dict[str, Any] | None
    transitions: tuple[dict[str, Any], ...]


class _PublicationUnresolved(RuntimeError):
    def __init__(self, result: ProductActionPublicationV1) -> None:
        self.result = result
        super().__init__(result.classification)


class _PublicationCommitUnknown(RuntimeError):
    pass


_OPERATION_COLUMNS = tuple(field.name for field in fields(ProductActionOperationSnapshotV1))
_ROUTE_COLUMNS = tuple(field.name for field in fields(ProductActionRouteSnapshotV1))
_LEGACY_CONFIRMATION_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class ProductActionProposalRepository:
    """The sole Task 3 owner of parent+route+seq1 Product Action publication."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        catalog: ProductActionCatalogV1,
        proof_registry: ProductActionProofRegistryV1,
        key_profiles: LedgerKeyProfileStoreV1,
    ) -> None:
        if (
            not callable(session_factory)
            or type(catalog) is not ProductActionCatalogV1
            or type(proof_registry) is not ProductActionProofRegistryV1
            or type(key_profiles) is not LedgerKeyProfileStoreV1
            or catalog._registry is not proof_registry
        ):
            raise TypeError("Product Action Repository composition is invalid")
        self.session_factory = session_factory
        self._catalog = catalog
        self._proof_registry = proof_registry
        self._key_profiles = key_profiles

    @staticmethod
    def _route_proof_type(
        prepared: PreparedProductActionProposalV1,
    ) -> type[ProductActionRouteProof] | type[HistoricalStoryRouteProof]:
        if prepared.request_origin == "historical_story_bridge":
            return HistoricalStoryRouteProof
        return ProductActionRouteProof

    def begin_publication_uow(self, session: Session) -> ProductActionPublicationUoWV1:
        """Acquire BEGIN IMMEDIATE while leaving commit/rollback to the caller."""

        if not isinstance(session, Session) or session.in_transaction():
            raise ProductActionContractError("publication_uow_required")
        try:
            session.execute(text("BEGIN IMMEDIATE"))
            transaction = session.get_transaction()
            driver_connection = session.connection().connection.driver_connection
            if (
                transaction is None
                or getattr(transaction, "is_active", False) is not True
                or getattr(driver_connection, "in_transaction", False) is not True
            ):
                raise ProductActionContractError("publication_uow_required")
            return ProductActionPublicationUoWV1(
                _PUBLICATION_UOW_CONSTRUCTION_SEAL,
                self,
                session,
                transaction,
                driver_connection,
            )
        except BaseException:
            try:
                session.rollback()
            except BaseException:
                pass
            raise

    def _require_publication_uow(
        self,
        session: Session,
        publication_uow: object,
    ) -> None:
        if type(publication_uow) is not ProductActionPublicationUoWV1:
            raise ProductActionContractError("publication_uow_required")
        typed = publication_uow
        try:
            sealed = typed._seal
            if (
                len(sealed) != 4
                or sealed[0] is not typed._repository
                or sealed[1] is not typed._session
                or sealed[2] is not typed._transaction
                or sealed[3] is not typed._driver_connection
                or typed._repository is not self
                or typed._session is not session
                or session.get_transaction() is not typed._transaction
                or getattr(typed._transaction, "is_active", False) is not True
            ):
                raise ProductActionContractError("publication_uow_required")
            driver_connection = session.connection().connection.driver_connection
            if (
                driver_connection is not typed._driver_connection
                or getattr(driver_connection, "in_transaction", False) is not True
            ):
                raise ProductActionContractError("publication_uow_required")
        except (AttributeError, TypeError) as exc:
            raise ProductActionContractError("publication_uow_required") from exc

    def publish_bundle_in_session(
        self,
        session: Session,
        publication_uow: ProductActionPublicationUoWV1,
        prepared: PreparedProductActionProposalV1,
    ) -> ProductActionPublicationV1:
        """Join a caller-owned BEGIN IMMEDIATE UoW without ending its transaction."""

        self._require_publication_uow(session, publication_uow)
        validate_prepared_product_action(
            prepared,
            catalog=self._catalog,
            key_profiles=self._key_profiles,
        )
        with self._proof_registry.claim(
            prepared.route_proof,
            proof_type=self._route_proof_type(prepared),
            action_name=prepared.action_name,
            expected_binding=prepared.proof_binding,
        ):
            return self._publish_bundle_in_session_unclaimed(session, prepared)

    def publish_historical_story_bridge_in_session(
        self,
        session: Session,
        publication_uow: ProductActionPublicationUoWV1,
        *,
        issuer: InterviewStoryActionIssuer,
        route_payload_raw: bytes,
        legacy_confirmation_token: str,
    ) -> ProductActionPublicationV1:
        """Validate a pre-0029 ready Attempt under the caller's write lock."""

        self._require_publication_uow(session, publication_uow)
        if (
            type(issuer) is not InterviewStoryActionIssuer
            or issuer._catalog is not self._catalog
            or issuer._registry is not self._proof_registry
            or issuer._key_profiles is not self._key_profiles
        ):
            raise TypeError("Historical Story issuer composition is invalid")
        if (
            type(legacy_confirmation_token) is not str
            or _LEGACY_CONFIRMATION_TOKEN.fullmatch(legacy_confirmation_token) is None
        ):
            raise ProductActionContractError("historical_story_bridge_token_invalid")
        decoded = decode_product_action_route_payload(
            route_payload_raw,
            action_name="confirm_interview_story",
            request_origin="historical_story_bridge",
        )
        payload = _route_payload(decoded)
        attempt_id = payload["attempt_id"]
        generation_revision = payload["generation_revision"]
        requested_generation = payload["product_action_generation"]
        if (
            type(attempt_id) is not int
            or type(generation_revision) is not int
            or type(requested_generation) is not int
        ):
            raise ProductActionContractError("historical_story_bridge_exact_integer")
        attempt = session.get(InterviewStoryProposalAttempt, attempt_id)
        if attempt is None:
            raise ProductActionContractError("historical_story_bridge_attempt_missing")
        if (
            type(attempt.id) is not int
            or type(attempt.generation_revision) is not int
            or type(attempt.product_action_generation) is not int
            or attempt.id != attempt_id
            or attempt.attempt_status != "ready"
            or attempt.generation_revision != generation_revision
            or attempt.proposal_hash != payload["proposal_hash"]
            or attempt.source_fingerprint != payload["source_fingerprint"]
            or attempt.target_story_id != payload["target_story_id"]
            or requested_generation != 1
            or attempt.product_action_generation != 0
            or attempt.product_action_operation_id is not None
            or attempt.confirmation_token_hash != ""
            or attempt.confirmation_payload_hash != ""
            or attempt.confirmed_story_id is not None
            or attempt.confirmed_story_version_id is not None
            or attempt.confirmed_at is not None
        ):
            raise ProductActionContractError("historical_story_bridge_attempt_not_exact_ready")
        key = self._key_profiles.active()
        historical_fingerprint = _derive_historical_request_token_fingerprint(
            key,
            attempt_id=attempt_id,
            generation_revision=generation_revision,
            proposal_hash=cast(str, payload["proposal_hash"]),
            legacy_confirmation_token=legacy_confirmation_token,
        )
        values = _derive(
            route=decoded,
            catalog=self._catalog,
            key=key,
            historical_request_token_fingerprint=historical_fingerprint,
        )
        binding = _proof_binding(values, decoded)
        proof = cast(
            HistoricalStoryRouteProof,
            self._proof_registry._issue(
                HistoricalStoryRouteProof,
                action_name="confirm_interview_story",
                binding=binding,
            ),
        )
        try:
            prepared = _prepared_from_derived_identity(
                route=decoded,
                key=key,
                values=values,
                historical_request_token_fingerprint=historical_fingerprint,
                proof=proof,
                binding=binding,
            )
            return self.publish_bundle_in_session(session, publication_uow, prepared)
        except BaseException:
            try:
                self._proof_registry.revoke(proof)
            except ValueError:
                pass
            raise

    def publish_bundle(
        self,
        prepared: PreparedProductActionProposalV1,
    ) -> ProductActionPublicationV1:
        validate_prepared_product_action(
            prepared,
            catalog=self._catalog,
            key_profiles=self._key_profiles,
        )
        try:
            with self._proof_registry.claim(
                prepared.route_proof,
                proof_type=self._route_proof_type(prepared),
                action_name=prepared.action_name,
                expected_binding=prepared.proof_binding,
            ):
                try:
                    return self._publish_once(prepared)
                except _PublicationCommitUnknown:
                    first = self.reconcile_publication(prepared)
                    if first.classification in {"exact_proposed", "exact_terminal"}:
                        return first
                    if first.classification == "unreadable":
                        raise _PublicationUnresolved(first)
                    try:
                        return self._publish_once(prepared)
                    except _PublicationCommitUnknown:
                        final = self.reconcile_publication(prepared)
                        if final.classification in {"exact_proposed", "exact_terminal"}:
                            return final
                        raise _PublicationUnresolved(final)
        except _PublicationUnresolved as exc:
            return exc.result

    def _publish_once(
        self,
        prepared: PreparedProductActionProposalV1,
    ) -> ProductActionPublicationV1:
        with self.session_factory() as session:
            try:
                self.begin_publication_uow(session)
                result = self._publish_bundle_in_session_unclaimed(session, prepared)
                try:
                    session.commit()
                except DBAPIError as exc:
                    raise _PublicationCommitUnknown from exc
                return result
            except BaseException:
                try:
                    session.rollback()
                except BaseException:
                    pass
                raise

    def _publish_bundle_in_session_unclaimed(
        self,
        session: Session,
        prepared: PreparedProductActionProposalV1,
    ) -> ProductActionPublicationV1:
        rows = self._load_rows(session, prepared.operation_id)
        classification = self._classify_rows(rows)
        if classification != "all_absent":
            bundle = self._validated_bundle(rows, expected=prepared)
            return self._publication_from_bundle(prepared, bundle, created=False)
        operation = WriteOperation(
            id=prepared.operation_id,
            operation_role="primary",
            parent_operation_id=None,
            parent_terminal_payload_sha256=None,
            conversation_id=None,
            agent_run_id=None,
            tool_call_id=prepared.action_call_id,
            tool_name=prepared.action_name,
            adapter_kind="product_action",
            status="proposed",
            fingerprint_key_id=prepared.fingerprint_key_id,
            proposal_fingerprint=prepared.proposal_fingerprint,
            input_fingerprint=None,
            confirmation_token_fingerprint=prepared.confirmation_token_fingerprint,
            authorization_scope_fingerprint=prepared.authorization_scope_fingerprint,
            operation_request_fingerprint=None,
            delivery_status="pending",
            delivery_generation=0,
            created_at=prepared.created_at,
            updated_at=prepared.created_at,
        )
        session.add(operation)
        session.flush()
        session.add_all(
            (
                ProductActionProposal(
                    operation_id=prepared.operation_id,
                    action_call_id=prepared.action_call_id,
                    action_name=prepared.action_name,
                    request_origin=prepared.request_origin,
                    schema_version=prepared.schema_version,
                    source_kind=prepared.source_kind,
                    source_id=prepared.source_id,
                    source_revision=prepared.source_revision,
                    route_payload_json=prepared.route_payload_json,
                    route_payload_fingerprint=prepared.route_payload_fingerprint,
                    route_binding_fingerprint=prepared.route_binding_fingerprint,
                    request_idempotency_fingerprint=(
                        prepared.request_idempotency_fingerprint
                    ),
                    semantic_claim_fingerprint=prepared.semantic_claim_fingerprint,
                    historical_request_token_fingerprint=(
                        prepared.historical_request_token_fingerprint
                    ),
                    created_at=prepared.created_at,
                    terminalized_at=None,
                ),
                WriteOperationTransition(
                    id=prepared.transition_id,
                    operation_id=prepared.operation_id,
                    seq=1,
                    state="proposed",
                    created_at=prepared.created_at,
                ),
            )
        )
        session.flush()
        reverse_rows = self._load_rows(session, prepared.operation_id)
        bundle = self._validated_bundle(reverse_rows, expected=prepared)
        return self._publication_from_bundle(prepared, bundle, created=True)

    def reconcile_publication(
        self,
        prepared: PreparedProductActionProposalV1,
    ) -> ProductActionPublicationV1:
        validate_prepared_product_action(
            prepared,
            catalog=self._catalog,
            key_profiles=self._key_profiles,
            require_route_proof=False,
        )
        with self.session_factory() as session:
            try:
                rows = self._load_rows(session, prepared.operation_id)
            except DBAPIError:
                return ProductActionPublicationV1(
                    "unreadable",
                    prepared.operation_id,
                    prepared.action_call_id,
                    None,
                    False,
                )
            classification = self._classify_rows(rows)
            if classification == "all_absent":
                return ProductActionPublicationV1(
                    "all_absent",
                    prepared.operation_id,
                    prepared.action_call_id,
                    None,
                    False,
                )
            bundle = self._validated_bundle(rows, expected=prepared)
            return self._publication_from_bundle(prepared, bundle, created=False)

    def load_bundle(self, operation_id: str) -> ProductActionBundleV1:
        normalized = require_product_action_uuid(operation_id, "operation_id")
        with self.session_factory() as session:
            try:
                rows = self._load_rows(session, normalized)
            except DBAPIError as exc:
                raise ProductActionIntegrityError("product_action_bundle_unreadable") from exc
            if self._classify_rows(rows) == "all_absent":
                raise ProductActionIntegrityError("product_action_bundle_absent")
            return self._validated_bundle(rows, expected=None)

    def load_bundle_in_session(
        self,
        session: Session,
        publication_uow: ProductActionPublicationUoWV1,
        operation_id: str,
    ) -> ProductActionBundleV1:
        """Load and validate the exact bundle inside a caller-owned writer UoW."""

        normalized = require_product_action_uuid(operation_id, "operation_id")
        self._require_publication_uow(session, publication_uow)
        try:
            rows = self._load_rows(session, normalized)
        except DBAPIError as exc:
            raise ProductActionIntegrityError("product_action_bundle_unreadable") from exc
        if self._classify_rows(rows) == "all_absent":
            raise ProductActionIntegrityError("product_action_bundle_absent")
        return self._validated_bundle(rows, expected=None)

    def _load_rows(self, session: Session, operation_id: str) -> _RawBundleRows:
        # Explicit SQL is intentional: publication verification must not trust the
        # identity map that just flushed the three objects.
        operation = session.execute(
            text(
                "SELECT "
                + ",".join(_OPERATION_COLUMNS)
                + " FROM write_operations WHERE id=:operation_id"
            ),
            {"operation_id": operation_id},
        ).mappings().one_or_none()
        route = session.execute(
            text(
                "SELECT "
                + ",".join(_ROUTE_COLUMNS)
                + " FROM product_action_proposals WHERE operation_id=:operation_id"
            ),
            {"operation_id": operation_id},
        ).mappings().one_or_none()
        transitions = tuple(
            dict(row)
            for row in session.execute(
                text(
                    "SELECT id,operation_id,seq,state,created_at "
                    "FROM write_operation_transitions "
                    "WHERE operation_id=:operation_id ORDER BY seq,id"
                ),
                {"operation_id": operation_id},
            ).mappings()
        )
        return _RawBundleRows(
            dict(operation) if operation is not None else None,
            dict(route) if route is not None else None,
            transitions,
        )

    @staticmethod
    def _classify_rows(rows: _RawBundleRows) -> Literal["all_absent", "present"]:
        present = (rows.operation is not None, rows.route is not None, bool(rows.transitions))
        if present == (False, False, False):
            return "all_absent"
        if present != (True, True, True):
            raise ProductActionIntegrityError("partial_product_action_bundle")
        return "present"

    def _validated_bundle(
        self,
        rows: _RawBundleRows,
        *,
        expected: PreparedProductActionProposalV1 | None,
    ) -> ProductActionBundleV1:
        try:
            return self._validated_bundle_contract(rows, expected=expected)
        except ProductActionContractError as exc:
            raise ProductActionIntegrityError("product_action_row_contract") from exc

    def _validated_bundle_contract(
        self,
        rows: _RawBundleRows,
        *,
        expected: PreparedProductActionProposalV1 | None,
    ) -> ProductActionBundleV1:
        if self._classify_rows(rows) != "present":
            raise ProductActionIntegrityError("partial_product_action_bundle")
        operation_values = cast(dict[str, Any], rows.operation)
        route_values = cast(dict[str, Any], rows.route)
        try:
            operation = ProductActionOperationSnapshotV1(
                **{name: operation_values[name] for name in _OPERATION_COLUMNS}
            )
            route = ProductActionRouteSnapshotV1(
                **{name: route_values[name] for name in _ROUTE_COLUMNS}
            )
        except (KeyError, TypeError) as exc:
            raise ProductActionIntegrityError("product_action_row_shape") from exc
        if (
            operation.operation_role != "primary"
            or operation.parent_operation_id is not None
            or operation.parent_terminal_payload_sha256 is not None
            or operation.adapter_kind != "product_action"
            or operation.conversation_id is not None
            or operation.agent_run_id is not None
            or operation.tool_call_id is None
            or operation.tool_name not in PRODUCT_ACTION_NAMES
            or route.operation_id != operation.id
            or route.action_call_id != operation.tool_call_id
            or route.action_name != operation.tool_name
        ):
            raise ProductActionIntegrityError("product_action_parent_route_identity")
        require_product_action_uuid(operation.id, "operation_id")
        require_product_action_uuid(operation.tool_call_id, "action_call_id")
        require_product_action_uuid(operation.fingerprint_key_id, "fingerprint_key_id")
        for field_name in (
            "proposal_fingerprint",
            "confirmation_token_fingerprint",
            "authorization_scope_fingerprint",
        ):
            require_product_action_hmac(getattr(operation, field_name), field_name)
        if type(operation.delivery_generation) is not int:
            raise ProductActionIntegrityError("product_action_delivery_generation")
        if type(route.schema_version) is not int or route.schema_version != 1:
            raise ProductActionIntegrityError("product_action_schema_version")
        if type(route.source_id) is not int or type(route.source_revision) is not int:
            raise ProductActionIntegrityError("product_action_exact_integer")
        if route.source_id < 1 or route.source_revision < 1:
            raise ProductActionIntegrityError("product_action_source_identity")
        for field_name in (
            "route_payload_fingerprint",
            "route_binding_fingerprint",
            "request_idempotency_fingerprint",
        ):
            require_product_action_hmac(getattr(route, field_name), field_name)
        if route.semantic_claim_fingerprint is not None:
            require_product_action_hmac(
                route.semantic_claim_fingerprint,
                "semantic_claim_fingerprint",
            )
        if route.historical_request_token_fingerprint is not None:
            require_product_action_hmac(
                route.historical_request_token_fingerprint,
                "historical_request_token_fingerprint",
            )
        if (route.action_name == "save_review_readiness_signal") != (
            route.semantic_claim_fingerprint is not None
        ):
            raise ProductActionIntegrityError("product_action_semantic_claim_mapping")
        if (route.request_origin == "historical_story_bridge") != (
            route.historical_request_token_fingerprint is not None
        ):
            raise ProductActionIntegrityError("product_action_historical_request_mapping")
        for item in rows.transitions:
            if (
                item["operation_id"] != operation.id
                or type(item["state"]) is not str
                or item["created_at"] is None
            ):
                raise ProductActionIntegrityError("product_action_transition_identity")
            require_product_action_uuid(item["id"], "transition_id")
        prefix = tuple(
            (self._exact_transition_int(item["seq"]), cast(str, item["state"]))
            for item in rows.transitions
        )
        expected_prefix = EXPECTED_PREFIX.get(operation.status)
        if expected_prefix is None or prefix != expected_prefix:
            raise ProductActionIntegrityError("product_action_transition_prefix")
        if (
            route.created_at != operation.created_at
            or rows.transitions[0]["created_at"] != operation.created_at
        ):
            raise ProductActionIntegrityError("product_action_creation_identity")
        active = operation.status == "proposed"
        if active != (route.route_payload_json is not None and route.terminalized_at is None):
            raise ProductActionIntegrityError("product_action_route_lifecycle")
        if not active and not (
            route.route_payload_json is None and route.terminalized_at is not None
        ):
            raise ProductActionIntegrityError("product_action_route_lifecycle")
        if active:
            self._validate_proposed_parent_shape(operation, route, rows.transitions)
            self._validate_active_identity(operation, route)
        else:
            self._validate_terminal_parent_shape(operation)
            self._validate_terminal_identity(operation, route)
            try:
                payload_from_operation(SimpleNamespace(**operation_values))  # type: ignore[arg-type]
            except Exception as exc:
                raise ProductActionIntegrityError("product_action_terminal_digest") from exc
        if expected is not None:
            self._validate_expected(operation, route, expected)
        classification: Literal["exact_proposed", "exact_terminal"] = (
            "exact_proposed" if active else "exact_terminal"
        )
        return ProductActionBundleV1(classification, operation, route, prefix)

    @staticmethod
    def _validate_proposed_parent_shape(
        operation: ProductActionOperationSnapshotV1,
        route: ProductActionRouteSnapshotV1,
        transitions: tuple[dict[str, Any], ...],
    ) -> None:
        if (
            operation.input_fingerprint is not None
            or operation.operation_request_fingerprint is not None
            or operation.result_contract is not None
            or operation.result_json is not None
            or operation.visible_result is not None
            or operation.transport_json is not None
            or operation.undo_json is not None
            or operation.terminal_payload_sha256 is not None
            or operation.failure_category is not None
            or operation.failure_code is not None
            or operation.delivery_status != "pending"
            or operation.delivery_failure_code is not None
            or operation.delivery_generation != 0
            or operation.delivery_owner_token_fingerprint is not None
            or operation.delivery_lease_expires_at is not None
            or operation.delivery_outcome is not None
            or operation.delivery_message_count is not None
            or operation.delivery_manifest_sha256 is not None
            or operation.delivery_next_operation_id is not None
            or operation.delivered_at is not None
            or operation.approved_at is not None
            or operation.claimed_at is not None
            or operation.rejected_at is not None
            or operation.committed_at is not None
            or operation.failed_at is not None
            or operation.created_at != operation.updated_at
            or operation.created_at != route.created_at
            or transitions[0]["created_at"] != operation.created_at
        ):
            raise ProductActionIntegrityError("product_action_proposed_parent_shape")

    @staticmethod
    def _validate_terminal_parent_shape(operation: ProductActionOperationSnapshotV1) -> None:
        terminal_at = {
            "rejected": operation.rejected_at,
            "committed": operation.committed_at,
            "failed": operation.failed_at,
        }.get(operation.status)
        expected_result_contract = (
            "rejection_json_v1"
            if operation.status == "rejected"
            else "product_action_json_v1"
        )
        if (
            terminal_at is None
            or operation.delivered_at != terminal_at
            or operation.operation_request_fingerprint is None
            or operation.result_contract != expected_result_contract
            or operation.result_json is None
            or operation.visible_result is None
            or operation.transport_json is None
            or operation.terminal_payload_sha256 is None
            or operation.delivery_status != "not_applicable"
            or operation.delivery_failure_code is not None
            or operation.delivery_generation != 0
            or operation.delivery_owner_token_fingerprint is not None
            or operation.delivery_lease_expires_at is not None
            or operation.delivery_outcome != "none"
            or operation.delivery_message_count != 0
            or operation.delivery_manifest_sha256 is not None
            or operation.delivery_next_operation_id is not None
        ):
            raise ProductActionIntegrityError("product_action_terminal_parent_shape")
        require_product_action_hmac(
            operation.operation_request_fingerprint,
            "operation_request_fingerprint",
        )
        if operation.status == "rejected":
            if (
                operation.input_fingerprint is not None
                or operation.undo_json is not None
                or operation.failure_category is not None
                or operation.failure_code is not None
                or operation.approved_at is not None
                or operation.claimed_at is not None
                or operation.committed_at is not None
                or operation.failed_at is not None
            ):
                raise ProductActionIntegrityError("product_action_rejected_parent_shape")
            return
        require_product_action_hmac(operation.input_fingerprint, "input_fingerprint")
        if (
            operation.approved_at is None
            or operation.claimed_at is None
            or operation.rejected_at is not None
            or (operation.status == "committed")
            != (operation.committed_at is not None and operation.failed_at is None)
            or (operation.status == "failed")
            != (operation.failed_at is not None and operation.committed_at is None)
        ):
            raise ProductActionIntegrityError("product_action_terminal_timestamps")
        if operation.status == "committed":
            if (
                operation.undo_json is None
                or operation.failure_category is not None
                or operation.failure_code is not None
            ):
                raise ProductActionIntegrityError("product_action_committed_parent_shape")
        elif (
            operation.undo_json is not None
            or operation.failure_category is None
            or operation.failure_code is None
        ):
            raise ProductActionIntegrityError("product_action_failed_parent_shape")

    @staticmethod
    def _exact_transition_int(value: object) -> int:
        if type(value) is not int:
            raise ProductActionIntegrityError("product_action_transition_integer")
        return value

    def _validate_active_identity(
        self,
        operation: ProductActionOperationSnapshotV1,
        route: ProductActionRouteSnapshotV1,
    ) -> None:
        if route.route_payload_json is None:
            raise ProductActionIntegrityError("product_action_route_missing")
        decoded = decode_product_action_route_payload(
            route.route_payload_json.encode("utf-8"),
            action_name=route.action_name,
            request_origin=route.request_origin,
        )
        if decoded.source_identity != (
            route.source_kind,
            route.source_id,
            route.source_revision,
        ):
            raise ProductActionIntegrityError("product_action_source_identity")
        key = self._key_profiles.resolve(operation.fingerprint_key_id)
        derived = _derive(
            route=decoded,
            catalog=self._catalog,
            key=key,
            historical_request_token_fingerprint=(
                route.historical_request_token_fingerprint
            ),
        )
        comparisons = {
            "operation_id": operation.id,
            "action_call_id": operation.tool_call_id,
            "proposal_fingerprint": operation.proposal_fingerprint,
            "authorization_scope_fingerprint": (
                operation.authorization_scope_fingerprint
            ),
            "confirmation_token_fingerprint": (
                operation.confirmation_token_fingerprint
            ),
            "route_payload_fingerprint": route.route_payload_fingerprint,
            "semantic_claim_fingerprint": route.semantic_claim_fingerprint,
            "route_binding_fingerprint": route.route_binding_fingerprint,
            "request_idempotency_fingerprint": (
                route.request_idempotency_fingerprint
            ),
        }
        for name, persisted in comparisons.items():
            derived_name = "operation_id" if name == "operation_id" else name
            if derived[derived_name] != persisted:
                raise ProductActionIntegrityError(f"product_action_{name}")

    def _validate_terminal_identity(
        self,
        operation: ProductActionOperationSnapshotV1,
        route: ProductActionRouteSnapshotV1,
    ) -> None:
        """Verify the keyed identity chain that survives route payload clearing."""

        if (
            operation.tool_name not in PRODUCT_ACTION_NAMES
            or route.request_origin not in {"current", "historical_story_bridge"}
            or operation.tool_call_id is None
            or operation.proposal_fingerprint is None
            or operation.authorization_scope_fingerprint is None
            or operation.confirmation_token_fingerprint is None
        ):
            raise ProductActionIntegrityError("product_action_terminal_identity")
        key = self._key_profiles.resolve(operation.fingerprint_key_id)
        proposal_fingerprint, token_fingerprint = _derive_persisted_proposal_identity(
            key,
            catalog_fingerprint=self._catalog.fingerprint,
            action_name=cast(Any, operation.tool_name),
            request_origin=cast(Any, route.request_origin),
            operation_id=operation.id,
            action_call_id=operation.tool_call_id,
            route_payload_fingerprint=route.route_payload_fingerprint,
            route_binding_fingerprint=route.route_binding_fingerprint,
            authorization_scope_fingerprint=operation.authorization_scope_fingerprint,
            semantic_claim_fingerprint=route.semantic_claim_fingerprint,
            historical_request_token_fingerprint=(
                route.historical_request_token_fingerprint
            ),
        )
        if not hmac.compare_digest(
            proposal_fingerprint,
            operation.proposal_fingerprint,
        ):
            raise ProductActionIntegrityError("product_action_proposal_fingerprint")
        if not hmac.compare_digest(
            token_fingerprint,
            operation.confirmation_token_fingerprint,
        ):
            raise ProductActionIntegrityError("product_action_confirmation_token_fingerprint")

    @staticmethod
    def _validate_expected(
        operation: ProductActionOperationSnapshotV1,
        route: ProductActionRouteSnapshotV1,
        expected: PreparedProductActionProposalV1,
    ) -> None:
        comparisons = (
            (operation.id, expected.operation_id),
            (operation.tool_call_id, expected.action_call_id),
            (operation.tool_name, expected.action_name),
            (operation.fingerprint_key_id, expected.fingerprint_key_id),
            (operation.proposal_fingerprint, expected.proposal_fingerprint),
            (
                operation.confirmation_token_fingerprint,
                expected.confirmation_token_fingerprint,
            ),
            (
                operation.authorization_scope_fingerprint,
                expected.authorization_scope_fingerprint,
            ),
            (route.request_origin, expected.request_origin),
            (route.source_kind, expected.source_kind),
            (route.source_id, expected.source_id),
            (route.source_revision, expected.source_revision),
            (
                route.route_payload_fingerprint,
                expected.route_payload_fingerprint,
            ),
            (
                route.route_binding_fingerprint,
                expected.route_binding_fingerprint,
            ),
            (
                route.request_idempotency_fingerprint,
                expected.request_idempotency_fingerprint,
            ),
            (
                route.semantic_claim_fingerprint,
                expected.semantic_claim_fingerprint,
            ),
            (
                route.historical_request_token_fingerprint,
                expected.historical_request_token_fingerprint,
            ),
        )
        if any(left != right for left, right in comparisons):
            raise ProductActionIntegrityError("product_action_publication_identity")
        if route.route_payload_json is not None and not hmac.compare_digest(
            route.route_payload_json.encode("utf-8"),
            expected.route_payload_json.encode("utf-8"),
        ):
            raise ProductActionIntegrityError("product_action_route_payload")

    @staticmethod
    def _publication_from_bundle(
        prepared: PreparedProductActionProposalV1,
        bundle: ProductActionBundleV1,
        *,
        created: bool,
    ) -> ProductActionPublicationV1:
        return ProductActionPublicationV1(
            bundle.classification,
            prepared.operation_id,
            prepared.action_call_id,
            prepared.confirmation_token if bundle.classification == "exact_proposed" else None,
            created,
            bundle,
        )


__all__ = [
    "ProductActionBundleV1",
    "ProductActionOperationSnapshotV1",
    "ProductActionProposalRepository",
    "ProductActionPublicationV1",
    "ProductActionPublicationUoWV1",
    "ProductActionRouteSnapshotV1",
]
