"""Independent HITL coordinator for Provider-invisible Product Actions."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Literal, NoReturn, Protocol, TypeAlias, cast
from uuid import uuid5

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from offerpilot.ai.write_operations import (
    TerminalPayload,
    build_terminal_payload,
    ledger_fingerprint,
)
from offerpilot.models import (
    InterviewReadinessSignal,
    InterviewReadinessSignalVersion,
    ProductActionProposal,
    WriteOperation,
    WriteOperationTransition,
)
from offerpilot.product_actions.catalog import ProductActionCatalogV1
from offerpilot.product_actions.contracts import (
    JSONValue,
    ProductActionContractError,
    ProductActionExecutionAuthorization,
    ProductActionIntegrityError,
    ProductActionProofRegistryV1,
    RejectionOnlyRecoveryProof,
    SignalOwnerRecoveryProof,
    canonical_product_action_json,
    decode_product_action_route_payload,
    materialize_frozen_json,
    require_product_action_uuid,
)
from offerpilot.product_actions.issuer import (
    PRODUCT_ACTION_CALL_NAMESPACE,
    LedgerKeyProfileStoreV1,
    PreparedProductActionProposalV1,
    ReviewReadinessActionIssuer,
)
from offerpilot.product_actions.repository import (
    ProductActionBundleV1,
    ProductActionProposalRepository,
    ProductActionPublicationV1,
)
from offerpilot.review_readiness.candidates import project_readiness_candidates
from offerpilot.review_readiness.contracts import (
    CandidateProjectionV1,
    ReadinessCandidateV1,
    ReviewReadinessContractError,
    SignalProposalRequestV1,
    decode_signal_proposal_request,
    validate_user_note,
)
from offerpilot.review_readiness.repository import ReadinessSignalRepository


CapabilityCheck: TypeAlias = Callable[[str], bool]
CandidateProjector: TypeAlias = Callable[[int, int, Session], CandidateProjectionV1]


class ProductActionCoordinatorError(RuntimeError):
    """Stable, text-free failure returned by the Product Action boundary."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int = 409,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class ProductActionProposalResultV1:
    operation_id: str | None
    action_call_id: str | None
    action_name: str
    status: str
    created: bool
    confirmation_token: str | None = None
    result: Mapping[str, JSONValue] | None = None
    replayed: bool = False


@dataclass(frozen=True, slots=True, repr=False)
class ProductActionDecisionResultV1:
    operation_id: str
    action_name: str
    status: Literal["rejected", "committed", "failed"]
    result: Mapping[str, JSONValue]
    replayed: bool
    direct_commit: bool


@dataclass(frozen=True, slots=True, repr=False)
class ProductActionStateV1:
    operation_id: str
    action_name: str
    status: str
    result: Mapping[str, JSONValue] | None
    rejection_only: bool = False


@dataclass(frozen=True, slots=True, repr=False)
class ProductActionRecoveryV1:
    operation_id: str
    action_call_id: str
    action_name: str
    status: Literal["proposed"]
    confirmation_token: str
    allowed_decisions: tuple[str, ...]
    rejection_only: bool


@dataclass(frozen=True, slots=True, repr=False)
class _DecisionControlV1:
    confirmation_token: str
    decision: Literal["approve", "modify", "reject"]
    edited_payload: Mapping[str, JSONValue] | None


@dataclass(frozen=True, slots=True, repr=False)
class ProductActionPreflightV1:
    """Untrusted handler output that the Coordinator validates and seals."""

    action_name: str
    effective_payload: Mapping[str, JSONValue]
    effective_payload_sha256: str
    trusted_source: object


_TRUSTED_DECISION_SEAL = object()


class TrustedProductActionDecisionV1:
    """Single-use, handler-bound decision created only by the Coordinator."""

    __slots__ = (
        "_action_name",
        "_effective_payload_json",
        "_effective_payload_sha256",
        "_trusted_source",
        "_handler",
        "_stage",
        "_consumed",
    )
    _action_name: str
    _effective_payload_json: str
    _effective_payload_sha256: str
    _trusted_source: object
    _handler: ProductActionHandlerV1
    _stage: Literal["external", "locked"]
    _consumed: bool

    def __init__(
        self,
        seal: object,
        *,
        action_name: str,
        effective_payload: Mapping[str, JSONValue],
        effective_payload_sha256: str,
        trusted_source: object,
        handler: ProductActionHandlerV1,
        stage: Literal["external", "locked"],
    ) -> None:
        if seal is not _TRUSTED_DECISION_SEAL:
            raise TypeError("Trusted Product Action decision is sealed")
        object.__setattr__(self, "_action_name", action_name)
        object.__setattr__(
            self,
            "_effective_payload_json",
            canonical_product_action_json(dict(effective_payload)),
        )
        object.__setattr__(
            self,
            "_effective_payload_sha256",
            effective_payload_sha256,
        )
        object.__setattr__(self, "_trusted_source", trusted_source)
        object.__setattr__(self, "_handler", handler)
        object.__setattr__(self, "_stage", stage)
        object.__setattr__(self, "_consumed", False)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("Trusted Product Action decision is sealed")

    def __repr__(self) -> str:
        return "<TrustedProductActionDecisionV1>"

    def __copy__(self) -> NoReturn:
        raise TypeError("Trusted Product Action decision cannot be copied")

    def __deepcopy__(self, memo: object) -> NoReturn:
        del memo
        raise TypeError("Trusted Product Action decision cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("Trusted Product Action decision cannot be serialized")

    @property
    def action_name(self) -> str:
        return self._action_name

    @property
    def effective_payload(self) -> Mapping[str, JSONValue]:
        value = json.loads(self._effective_payload_json)
        if type(value) is not dict:
            raise ProductActionIntegrityError("trusted_decision_payload")
        return _json_mapping(cast(dict[str, JSONValue], value))

    @property
    def effective_payload_sha256(self) -> str:
        return self._effective_payload_sha256

    @property
    def trusted_source(self) -> object:
        return self._trusted_source

    def _consume(
        self,
        *,
        handler: ProductActionHandlerV1,
        stage: Literal["external", "locked"],
    ) -> None:
        if self._consumed or self._handler is not handler or self._stage != stage:
            raise ProductActionIntegrityError("trusted_decision_identity")
        object.__setattr__(self, "_consumed", True)


@dataclass(frozen=True, slots=True, repr=False)
class ProductActionHandlerResultV1:
    result: Mapping[str, JSONValue]
    visible_result: str
    undo: Mapping[str, JSONValue]


class ProductActionHandlerV1(Protocol):
    """Extension point used by Task 5 without coupling Story to Signal code."""

    action_name: str

    def external_preflight(
        self,
        route_payload: Mapping[str, JSONValue],
        decision: _DecisionControlV1,
    ) -> ProductActionPreflightV1: ...

    def locked_recheck(
        self,
        session: Session,
        route_payload: Mapping[str, JSONValue],
        trusted: TrustedProductActionDecisionV1,
    ) -> ProductActionPreflightV1: ...

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
    ) -> ProductActionHandlerResultV1: ...

    def terminal_replay_effective_payload_sha256(
        self,
        operation_id: str,
        decision: _DecisionControlV1,
    ) -> str: ...

    def persisted_terminal_effective_payload_sha256(
        self,
        operation_id: str,
    ) -> str: ...


def _sha256_json(value: JSONValue) -> str:
    canonical = canonical_product_action_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _json_mapping(value: Mapping[str, JSONValue]) -> Mapping[str, JSONValue]:
    return MappingProxyType(dict(value))


def _terminal_result(bundle: ProductActionBundleV1) -> Mapping[str, JSONValue]:
    raw = bundle.operation.result_json
    if raw is None:
        raise ProductActionIntegrityError("product_action_terminal_result")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ProductActionIntegrityError("product_action_terminal_result") from exc
    if type(value) is not dict:
        raise ProductActionIntegrityError("product_action_terminal_result")
    return _json_mapping(cast(dict[str, JSONValue], value))


def _transition_id(operation_id: str, seq: int) -> str:
    return str(uuid5(PRODUCT_ACTION_CALL_NAMESPACE, f"{operation_id}:transition:{seq}"))


def _append_transition(
    session: Session,
    operation_id: str,
    seq: int,
    state: str,
    created_at: datetime,
) -> None:
    session.add(
        WriteOperationTransition(
            id=_transition_id(operation_id, seq),
            operation_id=operation_id,
            seq=seq,
            state=state,
            created_at=created_at,
        )
    )


def _enforce_action_aggregate(payload: TerminalPayload, limit: int) -> None:
    total = sum(
        len(value.encode("utf-8"))
        for value in (
            payload.result_json,
            payload.visible_result,
            payload.transport_json,
            payload.undo_json or "",
        )
    )
    if total > limit:
        raise ProductActionCoordinatorError(
            "product_action_input_too_large",
            status_code=422,
        )


def _apply_terminal(
    operation: WriteOperation,
    payload: TerminalPayload,
    *,
    operation_request_fingerprint: str,
    input_fingerprint: str | None,
    timestamp: datetime,
) -> None:
    operation.status = payload.status
    operation.operation_request_fingerprint = operation_request_fingerprint
    operation.input_fingerprint = input_fingerprint
    operation.result_contract = payload.result_contract
    operation.result_json = payload.result_json
    operation.visible_result = payload.visible_result
    operation.transport_json = payload.transport_json
    operation.undo_json = payload.undo_json
    operation.terminal_payload_sha256 = payload.digest
    operation.failure_category = payload.failure_category
    operation.failure_code = payload.failure_code
    operation.delivery_status = "not_applicable"
    operation.delivery_generation = 0
    operation.delivery_outcome = "none"
    operation.delivery_message_count = 0
    operation.delivery_failure_code = None
    operation.delivery_owner_token_fingerprint = None
    operation.delivery_lease_expires_at = None
    operation.delivery_manifest_sha256 = None
    operation.delivery_next_operation_id = None
    operation.delivered_at = timestamp
    operation.updated_at = timestamp
    if payload.status == "rejected":
        operation.rejected_at = timestamp
    elif payload.status == "committed":
        operation.committed_at = timestamp
    else:
        operation.failed_at = timestamp


class _ReadinessSignalHandlerV1:
    action_name = "save_review_readiness_signal"

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        repository: ReadinessSignalRepository,
        candidate_projector: CandidateProjector,
    ) -> None:
        self._session_factory = session_factory
        self._repository = repository
        self._candidate_projector = candidate_projector

    @staticmethod
    def _candidate(
        projection: CandidateProjectionV1,
        route: Mapping[str, JSONValue],
    ) -> ReadinessCandidateV1:
        if projection.state != "ready":
            if projection.state in {"source_changed", "source_missing"}:
                code, status_code, retryable = (
                    "review_readiness_source_changed",
                    409,
                    False,
                )
            elif projection.state == "unavailable":
                code, status_code, retryable = (
                    "review_readiness_unavailable",
                    503,
                    True,
                )
            else:
                code, status_code, retryable = (
                    "review_readiness_invalid_candidate",
                    422,
                    False,
                )
            raise ProductActionCoordinatorError(
                code,
                status_code=status_code,
                retryable=retryable,
            )
        candidate = next(
            (
                item
                for item in projection.candidates
                if item.focus_id == route["focus_id"]
            ),
            None,
        )
        if candidate is None:
            raise ProductActionCoordinatorError(
                "review_readiness_invalid_candidate",
                status_code=422,
            )
        comparisons = (
            (candidate.application_id, route["application_id"]),
            (candidate.event_id, route["event_id"]),
            (candidate.note_id, route["note_id"]),
            (candidate.proposal_id, route["proposal_id"]),
            (candidate.source_note_revision, route["expected_note_revision"]),
            (candidate.source_note_fingerprint, route["expected_source_fingerprint"]),
            (candidate.source_proposal_hash, route["expected_proposal_hash"]),
            (candidate.candidate_fingerprint, route["expected_candidate_fingerprint"]),
        )
        if any(left != right for left, right in comparisons):
            raise ProductActionCoordinatorError(
                "review_readiness_source_changed",
                status_code=409,
            )
        return candidate

    def _preflight(
        self,
        session: Session,
        route_payload: Mapping[str, JSONValue],
        effective_payload: Mapping[str, JSONValue],
    ) -> ProductActionPreflightV1:
        note_id = route_payload["note_id"]
        proposal_id = route_payload["proposal_id"]
        if type(note_id) is not int or type(proposal_id) is not int:
            raise ProductActionIntegrityError("product_action_route_exact_integer")
        try:
            projection = self._candidate_projector(note_id, proposal_id, session)
        except DBAPIError as exc:
            raise ProductActionCoordinatorError(
                "review_readiness_unavailable",
                status_code=503,
                retryable=True,
            ) from exc
        candidate = self._candidate(projection, route_payload)
        return ProductActionPreflightV1(
            action_name=self.action_name,
            effective_payload=_json_mapping(effective_payload),
            effective_payload_sha256=_sha256_json(dict(effective_payload)),
            trusted_source=candidate,
        )

    def external_preflight(
        self,
        route_payload: Mapping[str, JSONValue],
        decision: _DecisionControlV1,
    ) -> ProductActionPreflightV1:
        if decision.decision == "approve":
            effective = {"user_note": route_payload["user_note"]}
        elif decision.decision == "modify":
            edited = decision.edited_payload
            if edited is None or set(edited) != {"user_note"}:
                raise ProductActionCoordinatorError(
                    "product_action_invalid_request",
                    status_code=422,
                )
            effective = {"user_note": validate_user_note(edited["user_note"])}
        else:
            raise ProductActionCoordinatorError(
                "product_action_invalid_request",
                status_code=422,
            )
        with self._session_factory() as session:
            return self._preflight(session, route_payload, effective)

    def locked_recheck(
        self,
        session: Session,
        route_payload: Mapping[str, JSONValue],
        trusted: TrustedProductActionDecisionV1,
    ) -> ProductActionPreflightV1:
        if trusted.action_name != self.action_name:
            raise ProductActionIntegrityError("product_action_handler_identity")
        return self._preflight(session, route_payload, trusted.effective_payload)

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
        candidate = trusted.trusted_source
        if type(candidate) is not ReadinessCandidateV1:
            raise ProductActionIntegrityError("readiness_candidate_identity")
        user_note = trusted.effective_payload.get("user_note")
        if type(user_note) is not str:
            raise ProductActionIntegrityError("readiness_user_note_identity")
        domain_idempotency_key = route_payload.get("domain_idempotency_key")
        if type(domain_idempotency_key) is not str:
            raise ProductActionIntegrityError("readiness_domain_idempotency_key")
        result = self._repository.create_signal_in_session(
            session,
            candidate=candidate,
            user_note=user_note,
            domain_idempotency_key=domain_idempotency_key,
            operation_id=operation_id,
            authorization=authorization,
            authorization_binding=authorization_binding,
        )
        # The domain key is route-owned and supplied by the Coordinator below.
        del operation_request_fingerprint
        safe_result: dict[str, JSONValue] = {
            "schema_version": 1,
            "action_name": self.action_name,
            "outcome": "created",
            "signal_id": result.signal_id,
            "signal_version_id": result.signal_version_id,
            "signal_revision": result.signal_revision,
            "source_status": "current",
        }
        undo: dict[str, JSONValue] = {
            "kind": "retract_review_readiness_signal_v1",
            "signal_id": result.signal_id,
            "created_version_id": result.signal_version_id,
            "expected_current_version_id": result.signal_version_id,
            "expected_signal_revision": result.signal_revision,
            "parent_operation_id": operation_id,
        }
        return ProductActionHandlerResultV1(
            _json_mapping(safe_result),
            "已保存为下次准备重点。",
            _json_mapping(undo),
        )

    def terminal_replay_effective_payload_sha256(
        self,
        operation_id: str,
        decision: _DecisionControlV1,
    ) -> str:
        aggregate = self._repository.load_by_operation(operation_id)
        if aggregate is None:
            raise ProductActionIntegrityError("readiness_signal_terminal_missing")
        if decision.decision == "modify":
            edited = decision.edited_payload
            if edited is None or set(edited) != {"user_note"}:
                raise ProductActionCoordinatorError(
                    "product_action_invalid_request",
                    status_code=422,
                )
            user_note = validate_user_note(edited["user_note"])
        elif decision.decision == "approve":
            user_note = aggregate.user_note
        else:
            raise ProductActionCoordinatorError(
                "product_action_invalid_request",
                status_code=422,
            )
        return _sha256_json({"user_note": user_note})

    def persisted_terminal_effective_payload_sha256(
        self,
        operation_id: str,
    ) -> str:
        aggregate = self._repository.load_by_operation(operation_id)
        if aggregate is None:
            raise ProductActionIntegrityError("readiness_signal_terminal_missing")
        return _sha256_json({"user_note": aggregate.user_note})


class ProductActionCoordinator:
    """Coordinator-owned UoW for proposal, HITL decision, and safe recovery."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        catalog: ProductActionCatalogV1,
        proposal_repository: ProductActionProposalRepository,
        review_issuer: ReviewReadinessActionIssuer,
        proof_registry: ProductActionProofRegistryV1,
        key_profiles: LedgerKeyProfileStoreV1,
        readiness_repository: ReadinessSignalRepository,
        capability_check: CapabilityCheck,
        candidate_projector: CandidateProjector = project_readiness_candidates,
        additional_handlers: tuple[ProductActionHandlerV1, ...] = (),
    ) -> None:
        if (
            not callable(session_factory)
            or type(catalog) is not ProductActionCatalogV1
            or type(proposal_repository) is not ProductActionProposalRepository
            or type(review_issuer) is not ReviewReadinessActionIssuer
            or type(proof_registry) is not ProductActionProofRegistryV1
            or type(key_profiles) is not LedgerKeyProfileStoreV1
            or type(readiness_repository) is not ReadinessSignalRepository
            or not callable(capability_check)
            or not callable(candidate_projector)
            or catalog._registry is not proof_registry
        ):
            raise TypeError("Product Action Coordinator composition is invalid")
        signal_handler = _ReadinessSignalHandlerV1(
            session_factory=session_factory,
            repository=readiness_repository,
            candidate_projector=candidate_projector,
        )
        handlers: dict[str, ProductActionHandlerV1] = {
            signal_handler.action_name: signal_handler
        }
        for handler in additional_handlers:
            if handler.action_name in handlers:
                raise TypeError("Product Action handler is duplicated")
            handlers[handler.action_name] = handler
        self._session_factory = session_factory
        self._catalog = catalog
        self._proposal_repository = proposal_repository
        self._review_issuer = review_issuer
        self._proof_registry = proof_registry
        self._key_profiles = key_profiles
        self._readiness_repository = readiness_repository
        self._capability_check = capability_check
        self._candidate_projector = candidate_projector
        self._handlers = MappingProxyType(handlers)

    def _require_capability(self, action_name: str) -> None:
        spec = next(
            (item for item in self._catalog.ordered_specs if item.action_name == action_name),
            None,
        )
        if spec is None or not self._capability_check(spec.capabilities[0]):
            raise ProductActionCoordinatorError(
                "review_readiness_not_found",
                status_code=404,
            )

    @staticmethod
    def _seal_preflight(
        handler: ProductActionHandlerV1,
        preflight: ProductActionPreflightV1,
        *,
        stage: Literal["external", "locked"],
    ) -> TrustedProductActionDecisionV1:
        if type(preflight) is not ProductActionPreflightV1:
            raise ProductActionIntegrityError("product_action_preflight_identity")
        frozen_payload = _json_mapping(preflight.effective_payload)
        if (
            preflight.action_name != handler.action_name
            or _sha256_json(dict(frozen_payload))
            != preflight.effective_payload_sha256
        ):
            raise ProductActionIntegrityError("product_action_preflight_identity")
        return TrustedProductActionDecisionV1(
            _TRUSTED_DECISION_SEAL,
            action_name=preflight.action_name,
            effective_payload=frozen_payload,
            effective_payload_sha256=preflight.effective_payload_sha256,
            trusted_source=preflight.trusted_source,
            handler=handler,
            stage=stage,
        )

    def _candidate_for_request(
        self,
        session: Session,
        note_id: int,
        request: SignalProposalRequestV1,
    ) -> ReadinessCandidateV1 | ProductActionProposalResultV1:
        projection = self._candidate_projector(note_id, request.proposal_id, session)
        if projection.state == "already_confirmed":
            row = session.execute(
                select(InterviewReadinessSignal, InterviewReadinessSignalVersion)
                .join(
                    InterviewReadinessSignalVersion,
                    InterviewReadinessSignalVersion.id
                    == InterviewReadinessSignal.current_version_id,
                )
                .where(
                    InterviewReadinessSignal.source_note_id == note_id,
                    InterviewReadinessSignal.source_proposal_id == request.proposal_id,
                    InterviewReadinessSignal.focus_id == request.focus_id,
                )
            ).one_or_none()
            if row is None:
                raise ProductActionCoordinatorError(
                    "review_readiness_invalid_candidate",
                    status_code=422,
                )
            signal, version = row
            if (
                version.source_note_revision != request.expected_note_revision
                or version.candidate_fingerprint
                != request.expected_candidate_fingerprint
            ):
                raise ProductActionCoordinatorError(
                    "review_readiness_source_changed",
                    status_code=409,
                )
            return ProductActionProposalResultV1(
                version.write_operation_id,
                None,
                "save_review_readiness_signal",
                "already_confirmed",
                False,
                result=_json_mapping(
                    {
                        "signal_id": signal.id,
                        "signal_version_id": version.id,
                        "signal_revision": signal.revision,
                    }
                ),
            )
        if projection.state != "ready":
            if projection.state in {"source_changed", "source_missing"}:
                code, status_code, retryable = (
                    "review_readiness_source_changed",
                    409,
                    False,
                )
            elif projection.state == "unavailable":
                code, status_code, retryable = (
                    "review_readiness_unavailable",
                    503,
                    True,
                )
            else:
                code, status_code, retryable = (
                    "review_readiness_invalid_candidate",
                    422,
                    False,
                )
            raise ProductActionCoordinatorError(
                code,
                status_code=status_code,
                retryable=retryable,
            )
        candidate = next(
            (item for item in projection.candidates if item.focus_id == request.focus_id),
            None,
        )
        if candidate is None:
            raise ProductActionCoordinatorError(
                "review_readiness_invalid_candidate",
                status_code=422,
            )
        if (
            candidate.source_note_revision != request.expected_note_revision
            or candidate.candidate_fingerprint
            != request.expected_candidate_fingerprint
        ):
            raise ProductActionCoordinatorError(
                "review_readiness_source_changed",
                status_code=409,
            )
        return candidate

    @staticmethod
    def _signal_route(
        candidate: ReadinessCandidateV1,
        request: SignalProposalRequestV1,
    ) -> bytes:
        route: dict[str, JSONValue] = {
            "application_id": candidate.application_id,
            "event_id": candidate.event_id,
            "note_id": candidate.note_id,
            "proposal_id": candidate.proposal_id,
            "proposal_schema_version": 2,
            "focus_id": candidate.focus_id,
            "expected_note_revision": candidate.source_note_revision,
            "expected_source_fingerprint": candidate.source_note_fingerprint,
            "expected_proposal_hash": candidate.source_proposal_hash,
            "expected_candidate_fingerprint": candidate.candidate_fingerprint,
            "user_note": request.user_note,
            "domain_idempotency_key": request.idempotency_key,
        }
        return canonical_product_action_json(route).encode("utf-8")

    def propose_readiness_signal(
        self,
        *,
        note_id: int,
        request: dict[str, JSONValue],
    ) -> ProductActionProposalResultV1:
        if type(note_id) is not int or note_id < 1:
            raise ProductActionCoordinatorError(
                "review_readiness_not_found",
                status_code=404,
            )
        try:
            decoded = decode_signal_proposal_request(request)
        except ReviewReadinessContractError as exc:
            raise ProductActionCoordinatorError(
                "product_action_invalid_request",
                status_code=422,
            ) from exc
        self._require_capability("save_review_readiness_signal")
        with self._session_factory() as session:
            try:
                candidate = self._candidate_for_request(session, note_id, decoded)
            except DBAPIError as exc:
                raise ProductActionCoordinatorError(
                    "review_readiness_unavailable",
                    status_code=503,
                    retryable=True,
                ) from exc
        if isinstance(candidate, ProductActionProposalResultV1):
            return candidate
        route_raw = self._signal_route(candidate, decoded)
        prepared = self._review_issuer.prepare(route_payload_raw=route_raw)
        return self._publish_signal(prepared, decoded, candidate, route_raw, allow_replay=True)

    def _publish_signal(
        self,
        prepared: PreparedProductActionProposalV1,
        request: SignalProposalRequestV1,
        candidate: ReadinessCandidateV1,
        route_raw: bytes,
        *,
        allow_replay: bool,
    ) -> ProductActionProposalResultV1:
        with self._session_factory() as session:
            try:
                uow = self._proposal_repository.begin_publication_uow(session)
                existing_bundle: ProductActionBundleV1 | None = None
                try:
                    existing_bundle = self._proposal_repository.load_bundle_in_session(
                        session,
                        uow,
                        prepared.operation_id,
                    )
                except ProductActionIntegrityError as exc:
                    if exc.code != "product_action_bundle_absent":
                        raise
                if existing_bundle is not None:
                    try:
                        publication = (
                            self._proposal_repository.publish_bundle_in_session(
                                session,
                                uow,
                                prepared,
                            )
                        )
                    except ProductActionIntegrityError as exc:
                        if exc.code == "product_action_publication_identity":
                            raise ProductActionCoordinatorError(
                                "product_action_idempotency_conflict",
                                status_code=409,
                            ) from exc
                        raise
                else:
                    domain_existing = session.scalar(
                        select(InterviewReadinessSignal).where(
                            InterviewReadinessSignal.source_proposal_id
                            == candidate.proposal_id,
                            InterviewReadinessSignal.focus_id == candidate.focus_id,
                        )
                    )
                    if domain_existing is not None:
                        current_version = session.get(
                            InterviewReadinessSignalVersion,
                            domain_existing.current_version_id,
                        )
                        if current_version is None:
                            raise ProductActionIntegrityError(
                                "readiness_signal_pointer"
                            )
                        self._revoke_prepared(prepared)
                        session.rollback()
                        return ProductActionProposalResultV1(
                            current_version.write_operation_id,
                            None,
                            "save_review_readiness_signal",
                            "already_confirmed",
                            False,
                            result=_json_mapping(
                                {
                                    "signal_id": domain_existing.id,
                                    "signal_version_id": cast(
                                        JSONValue,
                                        current_version.id,
                                    ),
                                    "signal_revision": domain_existing.revision,
                                }
                            ),
                        )
                    active_claim = session.scalar(
                        select(ProductActionProposal.operation_id).where(
                            ProductActionProposal.semantic_claim_fingerprint
                            == prepared.semantic_claim_fingerprint,
                            ProductActionProposal.terminalized_at.is_(None),
                        )
                    )
                    if active_claim is not None:
                        self._revoke_prepared(prepared)
                        session.rollback()
                        raise ProductActionCoordinatorError(
                            "review_readiness_action_in_progress",
                            status_code=409,
                        )
                    try:
                        locked_projection = self._candidate_projector(
                            candidate.note_id,
                            candidate.proposal_id,
                            session,
                        )
                    except DBAPIError as exc:
                        raise ProductActionCoordinatorError(
                            "review_readiness_unavailable",
                            status_code=503,
                            retryable=True,
                        ) from exc
                    locked_candidate = next(
                        (
                            item
                            for item in locked_projection.candidates
                            if item.focus_id == candidate.focus_id
                        ),
                        None,
                    )
                    if locked_projection.state != "ready" or locked_candidate != candidate:
                        self._revoke_prepared(prepared)
                        session.rollback()
                        raise ProductActionCoordinatorError(
                            "review_readiness_source_changed",
                            status_code=409,
                        )
                    publication = self._proposal_repository.publish_bundle_in_session(
                        session,
                        uow,
                        prepared,
                    )
                try:
                    session.commit()
                except DBAPIError:
                    try:
                        session.rollback()
                    except BaseException:
                        pass
                    return self._reconcile_signal_publication(
                        prepared,
                        request,
                        candidate,
                        route_raw,
                        allow_replay=allow_replay,
                    )
                return self._proposal_from_publication(publication)
            except BaseException:
                try:
                    session.rollback()
                except BaseException:
                    pass
                self._revoke_prepared(prepared)
                raise

    def _reconcile_signal_publication(
        self,
        prepared: PreparedProductActionProposalV1,
        request: SignalProposalRequestV1,
        candidate: ReadinessCandidateV1,
        route_raw: bytes,
        *,
        allow_replay: bool,
    ) -> ProductActionProposalResultV1:
        publication = self._proposal_repository.reconcile_publication(prepared)
        if publication.classification in {"exact_proposed", "exact_terminal"}:
            return self._proposal_from_publication(publication, replayed=True)
        if publication.classification == "unreadable":
            raise ProductActionCoordinatorError(
                "operation_result_unknown",
                status_code=503,
                retryable=True,
            )
        if not allow_replay:
            raise ProductActionIntegrityError("product_action_publication_absent")
        fresh = self._review_issuer.prepare(route_payload_raw=route_raw)
        return self._publish_signal(
            fresh,
            request,
            candidate,
            route_raw,
            allow_replay=False,
        )

    def _proposal_from_publication(
        self,
        publication: ProductActionPublicationV1,
        *,
        replayed: bool = False,
    ) -> ProductActionProposalResultV1:
        bundle = publication.bundle
        if bundle is None:
            raise ProductActionIntegrityError("product_action_publication_bundle")
        if bundle.classification == "exact_terminal":
            self._verify_persisted_terminal_input(bundle)
            result = _terminal_result(bundle)
        else:
            result = None
        return ProductActionProposalResultV1(
            publication.operation_id,
            publication.action_call_id,
            bundle.operation.tool_name,
            bundle.operation.status,
            publication.created,
            publication.confirmation_token,
            result,
            replayed,
        )

    def _revoke_prepared(self, prepared: PreparedProductActionProposalV1) -> None:
        try:
            self._proof_registry.revoke(prepared.route_proof)
        except ValueError:
            pass

    @staticmethod
    def _decode_decision(request: dict[str, JSONValue]) -> _DecisionControlV1:
        if type(request) is not dict:
            raise ProductActionCoordinatorError(
                "product_action_invalid_request",
                status_code=422,
            )
        decision = request.get("decision")
        expected = (
            {"confirmation_token", "decision", "edited_payload"}
            if decision == "modify"
            else {"confirmation_token", "decision"}
        )
        token = request.get("confirmation_token")
        if (
            decision not in {"approve", "modify", "reject"}
            or set(request) != expected
            or type(token) is not str
            or len(token) != 64
            or any(character not in "0123456789abcdef" for character in token)
        ):
            raise ProductActionCoordinatorError(
                "product_action_invalid_request",
                status_code=422,
            )
        edited = request.get("edited_payload") if decision == "modify" else None
        if decision == "modify" and type(edited) is not dict:
            raise ProductActionCoordinatorError(
                "product_action_invalid_request",
                status_code=422,
            )
        return _DecisionControlV1(
            token,
            cast(Literal["approve", "modify", "reject"], decision),
            _json_mapping(cast(dict[str, JSONValue], edited)) if edited is not None else None,
        )

    def _verify_token(
        self,
        bundle: ProductActionBundleV1,
        token: str,
    ) -> None:
        key = self._key_profiles.resolve(bundle.operation.fingerprint_key_id)
        token_fingerprint = ledger_fingerprint(
            key,
            "write-operation-confirmation-token-v1",
            token.encode("ascii"),
        )
        if not hmac.compare_digest(
            token_fingerprint,
            bundle.operation.confirmation_token_fingerprint or "",
        ):
            raise ProductActionCoordinatorError("product_action_request_conflict")

    def _request_fingerprint(
        self,
        bundle: ProductActionBundleV1,
        decision: _DecisionControlV1,
        effective_payload_sha256: str | None,
    ) -> str:
        key = self._key_profiles.resolve(bundle.operation.fingerprint_key_id)
        return ledger_fingerprint(
            key,
            "product-action-request-v1",
            {
                "operation_id": bundle.operation.id,
                "action_call_id": bundle.operation.tool_call_id,
                "action_name": bundle.operation.tool_name,
                "decision": decision.decision,
                "effective_payload_sha256": effective_payload_sha256,
                "confirmation_token_fingerprint": (
                    bundle.operation.confirmation_token_fingerprint
                ),
                "proposal_fingerprint": bundle.operation.proposal_fingerprint,
                "route_binding_fingerprint": bundle.route.route_binding_fingerprint,
            },
        )

    def _input_fingerprint(
        self,
        bundle: ProductActionBundleV1,
        operation_request_fingerprint: str,
        effective_payload_sha256: str,
    ) -> str:
        key = self._key_profiles.resolve(bundle.operation.fingerprint_key_id)
        return ledger_fingerprint(
            key,
            "product-action-input-v1",
            {
                "operation_request_fingerprint": operation_request_fingerprint,
                "authorization_scope_fingerprint": (
                    bundle.operation.authorization_scope_fingerprint
                ),
                "effective_payload_sha256": effective_payload_sha256,
            },
        )

    def _verify_terminal_input_fingerprint(
        self,
        bundle: ProductActionBundleV1,
        effective_payload_sha256: str | None,
    ) -> None:
        if bundle.operation.status == "rejected":
            if effective_payload_sha256 is not None:
                raise ProductActionIntegrityError("rejected_input_fingerprint")
            return
        request_fingerprint = bundle.operation.operation_request_fingerprint
        input_fingerprint = bundle.operation.input_fingerprint
        if (
            request_fingerprint is None
            or input_fingerprint is None
            or effective_payload_sha256 is None
        ):
            raise ProductActionIntegrityError("product_action_input_fingerprint")
        expected = self._input_fingerprint(
            bundle,
            request_fingerprint,
            effective_payload_sha256,
        )
        if not hmac.compare_digest(expected, input_fingerprint):
            raise ProductActionIntegrityError("product_action_input_fingerprint")

    def _verify_persisted_terminal_input(
        self,
        bundle: ProductActionBundleV1,
    ) -> None:
        if bundle.operation.status == "rejected":
            self._verify_terminal_input_fingerprint(bundle, None)
            return
        handler = self._handlers.get(bundle.operation.tool_name)
        if handler is None:
            raise ProductActionIntegrityError("product_action_handler_identity")
        effective = handler.persisted_terminal_effective_payload_sha256(
            bundle.operation.id
        )
        self._verify_terminal_input_fingerprint(bundle, effective)

    @staticmethod
    def _route_payload(bundle: ProductActionBundleV1) -> Mapping[str, JSONValue]:
        raw = bundle.route.route_payload_json
        if raw is None:
            raise ProductActionIntegrityError("product_action_route_missing")
        decoded = decode_product_action_route_payload(
            raw.encode("utf-8"),
            action_name=bundle.route.action_name,
            request_origin=bundle.route.request_origin,
        )
        return _json_mapping(
            cast(dict[str, JSONValue], materialize_frozen_json(decoded.payload))
        )

    def decide(
        self,
        *,
        operation_id: str,
        request: dict[str, JSONValue],
    ) -> ProductActionDecisionResultV1:
        try:
            normalized_operation_id = require_product_action_uuid(
                operation_id,
                "operation_id",
            )
        except ProductActionContractError as exc:
            raise ProductActionCoordinatorError(
                "product_action_invalid_request",
                status_code=422,
            ) from exc
        control = self._decode_decision(request)
        try:
            bundle = self._proposal_repository.load_bundle(normalized_operation_id)
        except ProductActionIntegrityError as exc:
            if exc.code == "product_action_bundle_absent":
                raise ProductActionCoordinatorError(
                    "review_readiness_not_found",
                    status_code=404,
                ) from exc
            if exc.code == "product_action_bundle_unreadable":
                raise ProductActionCoordinatorError(
                    "operation_result_unknown",
                    status_code=503,
                    retryable=True,
                ) from exc
            raise
        self._verify_token(bundle, control.confirmation_token)
        if control.decision == "reject":
            request_fingerprint = self._request_fingerprint(bundle, control, None)
            if bundle.classification == "exact_terminal":
                return self._terminal_replay(bundle, request_fingerprint)
            return self._reject(
                bundle,
                control,
                request_fingerprint,
                allow_retry=True,
            )
        if bundle.classification == "exact_terminal":
            handler = self._handlers.get(bundle.operation.tool_name)
            if handler is None:
                raise ProductActionCoordinatorError("product_action_stale")
            effective_hash = handler.terminal_replay_effective_payload_sha256(
                bundle.operation.id,
                control,
            )
            request_fingerprint = self._request_fingerprint(
                bundle,
                control,
                effective_hash,
            )
            return self._terminal_replay(
                bundle,
                request_fingerprint,
                effective_payload_sha256=effective_hash,
            )
        handler = self._handlers.get(bundle.operation.tool_name)
        if handler is None:
            raise ProductActionCoordinatorError("product_action_stale")
        self._require_capability(bundle.operation.tool_name)
        route_payload = self._route_payload(bundle)
        trusted = self._seal_preflight(
            handler,
            handler.external_preflight(route_payload, control),
            stage="external",
        )
        request_fingerprint = self._request_fingerprint(
            bundle,
            control,
            trusted.effective_payload_sha256,
        )
        return self._execute(
            bundle,
            control,
            trusted,
            request_fingerprint,
            allow_retry=True,
        )

    def _reject(
        self,
        initial: ProductActionBundleV1,
        control: _DecisionControlV1,
        request_fingerprint: str,
        *,
        allow_retry: bool,
    ) -> ProductActionDecisionResultV1:
        with self._session_factory() as session:
            try:
                uow = self._proposal_repository.begin_publication_uow(session)
                bundle = self._proposal_repository.load_bundle_in_session(
                    session,
                    uow,
                    initial.operation.id,
                )
                self._verify_token(bundle, control.confirmation_token)
                locked_fingerprint = self._request_fingerprint(bundle, control, None)
                if not hmac.compare_digest(locked_fingerprint, request_fingerprint):
                    raise ProductActionCoordinatorError("product_action_request_conflict")
                if bundle.classification == "exact_terminal":
                    result = self._terminal_replay(bundle, request_fingerprint)
                    session.rollback()
                    return result
                operation = session.get(WriteOperation, bundle.operation.id)
                if operation is None or operation.status != "proposed":
                    raise ProductActionIntegrityError("product_action_parent_missing")
                now = datetime.now(timezone.utc)
                result_payload: dict[str, JSONValue] = {
                    "schema_version": 1,
                    "action_name": operation.tool_name,
                    "decision": "rejected",
                }
                transport: dict[str, JSONValue] = {
                    "schema_version": 1,
                    "operation_id": operation.id,
                    "action_name": operation.tool_name,
                    "status": "rejected",
                    "result": {"decision": "rejected"},
                }
                visible = {
                    "save_review_readiness_signal": "已取消保存准备重点。",
                    "confirm_interview_story": "已取消保存经历素材。",
                }[operation.tool_name]
                terminal = build_terminal_payload(
                    status="rejected",
                    result_contract="rejection_json_v1",
                    result=result_payload,
                    visible_result=visible,
                    transport=transport,
                    undo=None,
                    failure_category=None,
                    failure_code=None,
                    budgets=(4_096, 1_024, 4_096, 0),
                )
                _enforce_action_aggregate(terminal, 12_288)
                _apply_terminal(
                    operation,
                    terminal,
                    operation_request_fingerprint=request_fingerprint,
                    input_fingerprint=None,
                    timestamp=now,
                )
                _append_transition(session, operation.id, 2, "rejected", now)
                session.flush()
                terminal_bundle = self._proposal_repository.load_bundle_in_session(
                    session,
                    uow,
                    operation.id,
                )
                try:
                    session.commit()
                except DBAPIError:
                    try:
                        session.rollback()
                    except BaseException:
                        pass
                    return self._reconcile_decision(
                        initial.operation.id,
                        control,
                        request_fingerprint,
                        allow_retry=allow_retry,
                    )
                return ProductActionDecisionResultV1(
                    operation.id,
                    operation.tool_name,
                    "rejected",
                    _terminal_result(terminal_bundle),
                    False,
                    True,
                )
            except BaseException:
                try:
                    session.rollback()
                except BaseException:
                    pass
                raise

    def _execute(
        self,
        initial: ProductActionBundleV1,
        control: _DecisionControlV1,
        trusted: TrustedProductActionDecisionV1,
        request_fingerprint: str,
        *,
        allow_retry: bool,
    ) -> ProductActionDecisionResultV1:
        with self._session_factory() as session:
            try:
                uow = self._proposal_repository.begin_publication_uow(session)
                bundle = self._proposal_repository.load_bundle_in_session(
                    session,
                    uow,
                    initial.operation.id,
                )
                self._verify_token(bundle, control.confirmation_token)
                if bundle.classification == "exact_terminal":
                    result = self._terminal_replay(
                        bundle,
                        request_fingerprint,
                        effective_payload_sha256=trusted.effective_payload_sha256,
                    )
                    session.rollback()
                    return result
                self._require_capability(bundle.operation.tool_name)
                route_payload = self._route_payload(bundle)
                handler = self._handlers.get(bundle.operation.tool_name)
                if handler is None:
                    raise ProductActionCoordinatorError("product_action_stale")
                trusted._consume(handler=handler, stage="external")
                locked = self._seal_preflight(
                    handler,
                    handler.locked_recheck(session, route_payload, trusted),
                    stage="locked",
                )
                locked_request = self._request_fingerprint(
                    bundle,
                    control,
                    locked.effective_payload_sha256,
                )
                if not hmac.compare_digest(locked_request, request_fingerprint):
                    raise ProductActionCoordinatorError("product_action_request_conflict")
                operation = session.get(WriteOperation, bundle.operation.id)
                if operation is None or operation.status != "proposed":
                    raise ProductActionIntegrityError("product_action_parent_missing")
                now = datetime.now(timezone.utc)
                _append_transition(session, operation.id, 2, "approved", now)
                _append_transition(session, operation.id, 3, "claimed", now)
                session.flush()
                authorization_binding = (
                    operation.id,
                    operation.tool_call_id,
                    request_fingerprint,
                    locked.effective_payload_sha256,
                    operation.authorization_scope_fingerprint,
                )
                authorization = cast(
                    ProductActionExecutionAuthorization,
                    self._proof_registry._issue(
                        ProductActionExecutionAuthorization,
                        action_name=operation.tool_name,
                        binding=authorization_binding,
                    ),
                )
                locked._consume(handler=handler, stage="locked")
                try:
                    handler_result = handler.execute_in_session(
                        session,
                        operation_id=operation.id,
                        operation_request_fingerprint=request_fingerprint,
                        route_payload=route_payload,
                        trusted=locked,
                        authorization=authorization,
                        authorization_binding=authorization_binding,
                    )
                except BaseException:
                    try:
                        self._proof_registry.revoke(authorization)
                    except ValueError:
                        pass
                    raise
                try:
                    self._proof_registry.revoke(authorization)
                except ValueError:
                    pass
                else:
                    raise ProductActionIntegrityError(
                        "execution_authorization_not_consumed"
                    )
                transport: dict[str, JSONValue] = {
                    "schema_version": 1,
                    "operation_id": operation.id,
                    "action_name": operation.tool_name,
                    "status": "committed",
                    "result": dict(handler_result.result),
                }
                terminal = build_terminal_payload(
                    status="committed",
                    result_contract="product_action_json_v1",
                    result=dict(handler_result.result),
                    visible_result=handler_result.visible_result,
                    transport=transport,
                    undo=dict(handler_result.undo),
                    failure_category=None,
                    failure_code=None,
                    budgets=(4_096, 1_024, 4_096, 4_096),
                )
                _enforce_action_aggregate(terminal, 16_384)
                input_fingerprint = self._input_fingerprint(
                    bundle,
                    request_fingerprint,
                    locked.effective_payload_sha256,
                )
                operation.approved_at = now
                operation.claimed_at = now
                _apply_terminal(
                    operation,
                    terminal,
                    operation_request_fingerprint=request_fingerprint,
                    input_fingerprint=input_fingerprint,
                    timestamp=now,
                )
                _append_transition(session, operation.id, 4, "committed", now)
                session.flush()
                terminal_bundle = self._proposal_repository.load_bundle_in_session(
                    session,
                    uow,
                    operation.id,
                )
                try:
                    session.commit()
                except DBAPIError:
                    try:
                        session.rollback()
                    except BaseException:
                        pass
                    return self._reconcile_decision(
                        operation.id,
                        control,
                        request_fingerprint,
                        allow_retry=allow_retry,
                    )
                return ProductActionDecisionResultV1(
                    operation.id,
                    operation.tool_name,
                    "committed",
                    _terminal_result(terminal_bundle),
                    False,
                    True,
                )
            except BaseException:
                try:
                    session.rollback()
                except BaseException:
                    pass
                raise

    def _reconcile_decision(
        self,
        operation_id: str,
        control: _DecisionControlV1,
        request_fingerprint: str,
        *,
        allow_retry: bool,
    ) -> ProductActionDecisionResultV1:
        try:
            bundle = self._proposal_repository.load_bundle(operation_id)
        except ProductActionIntegrityError as exc:
            if exc.code == "product_action_bundle_unreadable":
                raise ProductActionCoordinatorError(
                    "operation_result_unknown",
                    status_code=503,
                    retryable=True,
                ) from exc
            raise
        if bundle.classification == "exact_terminal":
            if control.decision == "reject":
                effective_payload_sha256 = None
            else:
                handler = self._handlers.get(bundle.operation.tool_name)
                if handler is None:
                    raise ProductActionIntegrityError(
                        "product_action_handler_identity"
                    )
                effective_payload_sha256 = (
                    handler.persisted_terminal_effective_payload_sha256(
                        bundle.operation.id
                    )
                )
            replay = self._terminal_replay(
                bundle,
                request_fingerprint,
                effective_payload_sha256=effective_payload_sha256,
            )
            return ProductActionDecisionResultV1(
                replay.operation_id,
                replay.action_name,
                replay.status,
                replay.result,
                True,
                False,
            )
        if allow_retry and control.decision == "reject":
            return self._reject(
                bundle,
                control,
                request_fingerprint,
                allow_retry=False,
            )
        if not allow_retry:
            raise ProductActionCoordinatorError(
                "operation_result_unknown",
                status_code=503,
                retryable=True,
            )
        handler = self._handlers.get(bundle.operation.tool_name)
        if handler is None:
            raise ProductActionCoordinatorError("product_action_stale")
        self._require_capability(bundle.operation.tool_name)
        route_payload = self._route_payload(bundle)
        trusted = self._seal_preflight(
            handler,
            handler.external_preflight(route_payload, control),
            stage="external",
        )
        retry_fingerprint = self._request_fingerprint(
            bundle,
            control,
            trusted.effective_payload_sha256,
        )
        if not hmac.compare_digest(retry_fingerprint, request_fingerprint):
            raise ProductActionCoordinatorError("product_action_request_conflict")
        return self._execute(
            bundle,
            control,
            trusted,
            request_fingerprint,
            allow_retry=False,
        )

    def _terminal_replay(
        self,
        bundle: ProductActionBundleV1,
        request_fingerprint: str,
        *,
        effective_payload_sha256: str | None = None,
    ) -> ProductActionDecisionResultV1:
        persisted = bundle.operation.operation_request_fingerprint
        if persisted is None or not hmac.compare_digest(persisted, request_fingerprint):
            raise ProductActionCoordinatorError("product_action_request_conflict")
        self._verify_terminal_input_fingerprint(
            bundle,
            effective_payload_sha256,
        )
        self._verify_persisted_terminal_input(bundle)
        return ProductActionDecisionResultV1(
            bundle.operation.id,
            bundle.operation.tool_name,
            cast(Literal["rejected", "committed", "failed"], bundle.operation.status),
            _terminal_result(bundle),
            True,
            False,
        )

    def get_state(self, operation_id: str) -> ProductActionStateV1:
        try:
            normalized = require_product_action_uuid(operation_id, "operation_id")
            bundle = self._proposal_repository.load_bundle(normalized)
        except ProductActionContractError as exc:
            raise ProductActionCoordinatorError(
                "review_readiness_not_found",
                status_code=404,
            ) from exc
        except ProductActionIntegrityError as exc:
            if exc.code == "product_action_bundle_absent":
                raise ProductActionCoordinatorError(
                    "review_readiness_not_found",
                    status_code=404,
                ) from exc
            if exc.code == "product_action_bundle_unreadable":
                raise ProductActionCoordinatorError(
                    "operation_result_unknown",
                    status_code=503,
                    retryable=True,
                ) from exc
            raise
        if bundle.classification == "exact_terminal":
            self._verify_persisted_terminal_input(bundle)
        return ProductActionStateV1(
            bundle.operation.id,
            bundle.operation.tool_name,
            bundle.operation.status,
            _terminal_result(bundle) if bundle.classification == "exact_terminal" else None,
        )

    def recover_signal_owner(
        self,
        *,
        note_id: int,
        operation_id: str,
    ) -> ProductActionRecoveryV1:
        self._require_capability("save_review_readiness_signal")
        try:
            normalized = require_product_action_uuid(operation_id, "operation_id")
        except ProductActionContractError as exc:
            raise ProductActionCoordinatorError(
                "review_readiness_not_found",
                status_code=404,
            ) from exc
        with self._session_factory() as session:
            try:
                uow = self._proposal_repository.begin_publication_uow(session)
                bundle = self._proposal_repository.load_bundle_in_session(
                    session,
                    uow,
                    normalized,
                )
                if bundle.classification != "exact_proposed":
                    raise ProductActionCoordinatorError(
                        "review_readiness_not_found",
                        status_code=404,
                    )
                route = self._route_payload(bundle)
                if (
                    bundle.operation.tool_name != "save_review_readiness_signal"
                    or route["note_id"] != note_id
                ):
                    raise ProductActionCoordinatorError(
                        "review_readiness_not_found",
                        status_code=404,
                    )
                try:
                    projection = self._candidate_projector(
                        cast(int, route["note_id"]),
                        cast(int, route["proposal_id"]),
                        session,
                    )
                except DBAPIError as exc:
                    raise ProductActionCoordinatorError(
                        "review_readiness_unavailable",
                        status_code=503,
                        retryable=True,
                    ) from exc
                _ReadinessSignalHandlerV1._candidate(projection, route)
                binding = (
                    "review_readiness_focus_owner",
                    route["application_id"],
                    route["event_id"],
                    route["note_id"],
                    route["proposal_id"],
                    route["expected_note_revision"],
                    bundle.route.semantic_claim_fingerprint,
                    bundle.operation.id,
                    bundle.operation.tool_call_id,
                    bundle.route.route_payload_fingerprint,
                    bundle.route.route_binding_fingerprint,
                    bundle.route.request_origin,
                    SignalOwnerRecoveryProof.allowed_decisions,
                )
                proof = cast(
                    SignalOwnerRecoveryProof,
                    self._proof_registry._issue(
                        SignalOwnerRecoveryProof,
                        action_name="save_review_readiness_signal",
                        binding=binding,
                    ),
                )
                with self._proof_registry.claim(
                    proof,
                    proof_type=SignalOwnerRecoveryProof,
                    action_name="save_review_readiness_signal",
                    expected_binding=binding,
                ):
                    token = self._review_issuer.recover_confirmation_token(
                        cast(Any, bundle)
                    )
                session.rollback()
                return ProductActionRecoveryV1(
                    bundle.operation.id,
                    cast(str, bundle.operation.tool_call_id),
                    bundle.operation.tool_name,
                    "proposed",
                    token,
                    SignalOwnerRecoveryProof.allowed_decisions,
                    False,
                )
            except BaseException:
                try:
                    session.rollback()
                except BaseException:
                    pass
                raise

    def recover_rejection_control(
        self,
        *,
        application_id: int,
        operation_id: str,
    ) -> ProductActionRecoveryV1:
        if type(application_id) is not int or application_id < 1:
            raise ProductActionCoordinatorError(
                "review_readiness_not_found",
                status_code=404,
            )
        try:
            normalized = require_product_action_uuid(operation_id, "operation_id")
        except ProductActionContractError as exc:
            raise ProductActionCoordinatorError(
                "review_readiness_not_found",
                status_code=404,
            ) from exc
        with self._session_factory() as session:
            try:
                uow = self._proposal_repository.begin_publication_uow(session)
                bundle = self._proposal_repository.load_bundle_in_session(
                    session,
                    uow,
                    normalized,
                )
                if bundle.classification != "exact_proposed":
                    raise ProductActionCoordinatorError(
                        "review_readiness_not_found",
                        status_code=404,
                    )
                route = self._route_payload(bundle)
                if (
                    bundle.operation.tool_name != "save_review_readiness_signal"
                    or route["application_id"] != application_id
                ):
                    raise ProductActionCoordinatorError(
                        "review_readiness_not_found",
                        status_code=404,
                    )
                binding = (
                    "application_interview_review_owner",
                    route["application_id"],
                    route["event_id"],
                    route["note_id"],
                    route["proposal_id"],
                    RejectionOnlyRecoveryProof.live_source_state,
                    bundle.route.semantic_claim_fingerprint,
                    bundle.operation.id,
                    bundle.operation.tool_call_id,
                    bundle.route.route_payload_fingerprint,
                    bundle.route.route_binding_fingerprint,
                    RejectionOnlyRecoveryProof.allowed_decisions,
                )
                proof = cast(
                    RejectionOnlyRecoveryProof,
                    self._proof_registry._issue(
                        RejectionOnlyRecoveryProof,
                        action_name="save_review_readiness_signal",
                        binding=binding,
                    ),
                )
                with self._proof_registry.claim(
                    proof,
                    proof_type=RejectionOnlyRecoveryProof,
                    action_name="save_review_readiness_signal",
                    expected_binding=binding,
                ):
                    token = self._review_issuer.recover_confirmation_token(
                        cast(Any, bundle)
                    )
                session.rollback()
                return ProductActionRecoveryV1(
                    bundle.operation.id,
                    cast(str, bundle.operation.tool_call_id),
                    bundle.operation.tool_name,
                    "proposed",
                    token,
                    RejectionOnlyRecoveryProof.allowed_decisions,
                    True,
                )
            except BaseException:
                try:
                    session.rollback()
                except BaseException:
                    pass
                raise


__all__ = [
    "ProductActionCoordinator",
    "ProductActionCoordinatorError",
    "ProductActionDecisionResultV1",
    "ProductActionHandlerResultV1",
    "ProductActionHandlerV1",
    "ProductActionPreflightV1",
    "ProductActionProposalResultV1",
    "ProductActionRecoveryV1",
    "ProductActionStateV1",
    "TrustedProductActionDecisionV1",
]
