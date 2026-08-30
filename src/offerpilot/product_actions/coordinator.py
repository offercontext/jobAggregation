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
    Application,
    ApplicationEvent,
    InterviewNote,
    InterviewReadinessSignal,
    InterviewReadinessSignalVersion,
    InterviewReviewProposal,
    InterviewStoryProposalAttempt,
    ProductActionProposal,
    WriteOperation,
    WriteOperationTransition,
)
from offerpilot.product_actions.catalog import ProductActionCatalogV1
from offerpilot.product_actions.contracts import (
    FrozenJSONValue,
    JSONValue,
    ProductActionContractError,
    ProductActionExecutionAuthorization,
    ProductActionIntegrityError,
    ProductActionProofRegistryV1,
    RejectionOnlyRecoveryProof,
    SignalOwnerRecoveryProof,
    StoryOwnerRecoveryProof,
    canonical_product_action_json,
    decode_product_action_route_payload,
    freeze_product_action_json,
    materialize_frozen_json,
    require_product_action_uuid,
)
from offerpilot.product_actions.issuer import (
    InterviewStoryActionIssuer,
    PRODUCT_ACTION_CALL_NAMESPACE,
    PRODUCT_ACTION_OPERATION_NAMESPACE,
    LedgerKeyProfileStoreV1,
    PreparedProductActionProposalV1,
    ReviewReadinessActionIssuer,
)
from offerpilot.product_actions.repository import (
    ProductActionBundleV1,
    ProductActionProposalRepository,
    ProductActionPublicationReplayV1,
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
ProductActionCompletionKind: TypeAlias = Literal[
    "direct_commit",
    "reconciliation",
    "replay",
]


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
    completion_kind: ProductActionCompletionKind
    transport: MappingProxyType[str, FrozenJSONValue]

    @property
    def replayed(self) -> bool:
        return self.completion_kind != "direct_commit"

    @property
    def direct_commit(self) -> bool:
        return self.completion_kind == "direct_commit"

    @property
    def legacy_projection(self) -> MappingProxyType[str, FrozenJSONValue] | None:
        key = (
            "legacy_direct_commit"
            if self.completion_kind == "direct_commit"
            else "legacy_reconciliation_or_replay"
        )
        value = self.transport.get(key)
        return value if type(value) is MappingProxyType else None


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


@dataclass(frozen=True, slots=True, repr=False)
class ProductActionTerminalBudgetsV1:
    result_bytes: int
    visible_bytes: int
    transport_bytes: int
    undo_bytes: int
    aggregate_bytes: int

    def __post_init__(self) -> None:
        values = (
            self.result_bytes,
            self.visible_bytes,
            self.transport_bytes,
            self.undo_bytes,
            self.aggregate_bytes,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise TypeError("Product Action terminal budgets are invalid")

    @property
    def field_budgets(self) -> tuple[int, int, int, int]:
        return (
            self.result_bytes,
            self.visible_bytes,
            self.transport_bytes,
            self.undo_bytes,
        )


class ProductActionStoryWriteConflict(RuntimeError):
    """The sole V1 executor-after-call failure declared by the Story action."""


@dataclass(frozen=True, slots=True, repr=False)
class ProductActionDeclaredExecutorFailureV1:
    exception_type: type[Exception]
    failure_category: str
    failure_code: str

    def __post_init__(self) -> None:
        if (
            type(self.exception_type) is not type
            or not issubclass(self.exception_type, Exception)
            or type(self.failure_category) is not str
            or not self.failure_category
            or type(self.failure_code) is not str
            or not self.failure_code
        ):
            raise TypeError("Product Action declared failure is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class ProductActionPreClaimDispositionV1:
    action_name: Literal["confirm_interview_story"]
    disposition: Literal["source_changed"]

    def __post_init__(self) -> None:
        if (
            self.action_name != "confirm_interview_story"
            or self.disposition != "source_changed"
        ):
            raise TypeError("Product Action pre-claim disposition is invalid")


@dataclass(frozen=True, slots=True, repr=False)
class ProductActionTerminalProjectionV1:
    result: Mapping[str, JSONValue]
    visible_result: str
    transport: Mapping[str, JSONValue]
    undo: Mapping[str, JSONValue] | None


_SIGNAL_TERMINAL_BUDGETS = ProductActionTerminalBudgetsV1(
    4_096,
    1_024,
    4_096,
    4_096,
    16_384,
)
_STORY_TERMINAL_BUDGETS = ProductActionTerminalBudgetsV1(
    4_096,
    1_024,
    4_096,
    32_768,
    49_152,
)
_FAILED_TERMINAL_BUDGETS = ProductActionTerminalBudgetsV1(
    4_096,
    1_024,
    4_096,
    0,
    12_288,
)
_STORY_WRITE_CONFLICT_FAILURE = ProductActionDeclaredExecutorFailureV1(
    ProductActionStoryWriteConflict,
    "conflict",
    "product_action_story_write_conflict",
)


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

    @property
    def action_name(self) -> str: ...

    @property
    def terminal_budgets(self) -> ProductActionTerminalBudgetsV1: ...

    @property
    def declared_executor_failures(
        self,
    ) -> tuple[ProductActionDeclaredExecutorFailureV1, ...]: ...

    @property
    def declared_preclaim_dispositions(self) -> tuple[str, ...]: ...

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
    ) -> ProductActionPreflightV1 | ProductActionPreClaimDispositionV1: ...

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

    def project_committed_terminal(
        self,
        operation_id: str,
        result: ProductActionHandlerResultV1,
    ) -> ProductActionTerminalProjectionV1: ...

    def project_failed_terminal(
        self,
        operation_id: str,
        failure: ProductActionDeclaredExecutorFailureV1,
    ) -> ProductActionTerminalProjectionV1: ...

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


def _frozen_json_mapping(
    value: Mapping[str, JSONValue],
) -> MappingProxyType[str, FrozenJSONValue]:
    frozen = freeze_product_action_json(dict(value))
    if type(frozen) is not MappingProxyType:
        raise ProductActionIntegrityError("product_action_terminal_transport")
    return frozen


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


def _terminal_json_object(raw: str | None, code: str) -> dict[str, JSONValue]:
    if raw is None:
        raise ProductActionIntegrityError(code)
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ProductActionIntegrityError(code) from exc
    if type(value) is not dict:
        raise ProductActionIntegrityError(code)
    return cast(dict[str, JSONValue], value)


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
    terminal_budgets = _SIGNAL_TERMINAL_BUDGETS
    declared_executor_failures: tuple[ProductActionDeclaredExecutorFailureV1, ...] = ()
    declared_preclaim_dispositions: tuple[str, ...] = ()

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
        }
        return ProductActionTerminalProjectionV1(
            result.result,
            result.visible_result,
            _json_mapping(transport),
            result.undo,
        )

    def project_failed_terminal(
        self,
        operation_id: str,
        failure: ProductActionDeclaredExecutorFailureV1,
    ) -> ProductActionTerminalProjectionV1:
        del operation_id, failure
        raise ProductActionIntegrityError("signal_declared_failure_forbidden")

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


def _require_closed_handler_contract(handler: ProductActionHandlerV1) -> None:
    expected = {
        "save_review_readiness_signal": (_SIGNAL_TERMINAL_BUDGETS, (), ()),
        "confirm_interview_story": (
            _STORY_TERMINAL_BUDGETS,
            (_STORY_WRITE_CONFLICT_FAILURE,),
            ("source_changed",),
        ),
    }.get(handler.action_name)
    if expected is None:
        raise TypeError("Product Action handler action is outside the closed Catalog")
    budgets = getattr(handler, "terminal_budgets", None)
    failures = getattr(handler, "declared_executor_failures", None)
    dispositions = getattr(handler, "declared_preclaim_dispositions", None)
    required_methods = (
        "external_preflight",
        "locked_recheck",
        "execute_in_session",
        "project_committed_terminal",
        "project_failed_terminal",
        "terminal_replay_effective_payload_sha256",
        "persisted_terminal_effective_payload_sha256",
    )
    if (
        type(budgets) is not ProductActionTerminalBudgetsV1
        or budgets != expected[0]
        or type(failures) is not tuple
        or failures != expected[1]
        or type(dispositions) is not tuple
        or dispositions != expected[2]
        or any(not callable(getattr(handler, method, None)) for method in required_methods)
    ):
        raise TypeError("Product Action handler contract is invalid")


_HANDLER_REGISTRATION_SEAL = object()


@dataclass(frozen=True, slots=True, repr=False, init=False)
class _SealedProductActionHandlerV1:
    action_name: str
    terminal_budgets: ProductActionTerminalBudgetsV1
    declared_executor_failures: tuple[ProductActionDeclaredExecutorFailureV1, ...]
    declared_preclaim_dispositions: tuple[str, ...]
    _external_preflight: Callable[..., ProductActionPreflightV1]
    _locked_recheck: Callable[
        ...,
        ProductActionPreflightV1 | ProductActionPreClaimDispositionV1,
    ]
    _execute_in_session: Callable[..., ProductActionHandlerResultV1]
    _project_committed_terminal: Callable[..., ProductActionTerminalProjectionV1]
    _project_failed_terminal: Callable[..., ProductActionTerminalProjectionV1]
    _terminal_replay_effective_payload_sha256: Callable[..., str]
    _persisted_terminal_effective_payload_sha256: Callable[..., str]
    _seal: object

    def __init__(
        self,
        seal: object,
        handler: ProductActionHandlerV1,
    ) -> None:
        if seal is not _HANDLER_REGISTRATION_SEAL:
            raise TypeError("Product Action handler registration is sealed")
        _require_closed_handler_contract(handler)
        if handler.action_name != "confirm_interview_story":
            raise TypeError("Only the Story handler is externally registerable")
        object.__setattr__(self, "action_name", handler.action_name)
        object.__setattr__(self, "terminal_budgets", handler.terminal_budgets)
        object.__setattr__(
            self,
            "declared_executor_failures",
            handler.declared_executor_failures,
        )
        object.__setattr__(
            self,
            "declared_preclaim_dispositions",
            handler.declared_preclaim_dispositions,
        )
        for field_name, method_name in (
            ("_external_preflight", "external_preflight"),
            ("_locked_recheck", "locked_recheck"),
            ("_execute_in_session", "execute_in_session"),
            ("_project_committed_terminal", "project_committed_terminal"),
            ("_project_failed_terminal", "project_failed_terminal"),
            (
                "_terminal_replay_effective_payload_sha256",
                "terminal_replay_effective_payload_sha256",
            ),
            (
                "_persisted_terminal_effective_payload_sha256",
                "persisted_terminal_effective_payload_sha256",
            ),
        ):
            object.__setattr__(self, field_name, getattr(handler, method_name))
        object.__setattr__(self, "_seal", seal)

    def _ensure_sealed(self) -> None:
        if self._seal is not _HANDLER_REGISTRATION_SEAL:
            raise ProductActionIntegrityError("product_action_handler_registration")

    def external_preflight(
        self,
        route_payload: Mapping[str, JSONValue],
        decision: _DecisionControlV1,
    ) -> ProductActionPreflightV1:
        self._ensure_sealed()
        return self._external_preflight(route_payload, decision)

    def locked_recheck(
        self,
        session: Session,
        route_payload: Mapping[str, JSONValue],
        trusted: TrustedProductActionDecisionV1,
    ) -> ProductActionPreflightV1 | ProductActionPreClaimDispositionV1:
        self._ensure_sealed()
        return self._locked_recheck(session, route_payload, trusted)

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
        self._ensure_sealed()
        return self._execute_in_session(
            session,
            operation_id=operation_id,
            operation_request_fingerprint=operation_request_fingerprint,
            route_payload=route_payload,
            trusted=trusted,
            authorization=authorization,
            authorization_binding=authorization_binding,
        )

    def project_committed_terminal(
        self,
        operation_id: str,
        result: ProductActionHandlerResultV1,
    ) -> ProductActionTerminalProjectionV1:
        self._ensure_sealed()
        return self._project_committed_terminal(operation_id, result)

    def project_failed_terminal(
        self,
        operation_id: str,
        failure: ProductActionDeclaredExecutorFailureV1,
    ) -> ProductActionTerminalProjectionV1:
        self._ensure_sealed()
        return self._project_failed_terminal(operation_id, failure)

    def terminal_replay_effective_payload_sha256(
        self,
        operation_id: str,
        decision: _DecisionControlV1,
    ) -> str:
        self._ensure_sealed()
        return self._terminal_replay_effective_payload_sha256(
            operation_id,
            decision,
        )

    def persisted_terminal_effective_payload_sha256(
        self,
        operation_id: str,
    ) -> str:
        self._ensure_sealed()
        return self._persisted_terminal_effective_payload_sha256(operation_id)

    def __copy__(self) -> NoReturn:
        raise TypeError("Product Action handler registration cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("Product Action handler registration cannot be copied")

    def __reduce_ex__(self, _protocol: Any) -> NoReturn:
        raise TypeError("Product Action handler registration cannot be serialized")

    def __getstate__(self) -> NoReturn:
        raise TypeError("Product Action handler registration cannot be serialized")


def seal_interview_story_product_action_handler(
    handler: ProductActionHandlerV1,
) -> ProductActionHandlerV1:
    """Snapshot a closed Story handler for trusted composition."""

    return _SealedProductActionHandlerV1(_HANDLER_REGISTRATION_SEAL, handler)


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
        additional_handlers: tuple[object, ...] = (),
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
        _require_closed_handler_contract(signal_handler)
        for registered_handler in additional_handlers:
            if type(registered_handler) is not _SealedProductActionHandlerV1:
                raise TypeError("Product Action handler registration is required")
            handler = registered_handler
            handler._ensure_sealed()
            _require_closed_handler_contract(handler)
            if handler.action_name in handlers:
                raise TypeError("Product Action handler is duplicated")
            handlers[handler.action_name] = handler
        self._session_factory = session_factory
        self._catalog = catalog
        self._proposal_repository = proposal_repository
        self._review_issuer = review_issuer
        self._story_issuer = InterviewStoryActionIssuer(
            catalog,
            proof_registry,
            key_profiles,
        )
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
                (
                    "interview_story_not_found"
                    if action_name == "confirm_interview_story"
                    else "review_readiness_not_found"
                ),
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

    @staticmethod
    def _signal_operation_id(idempotency_key: str) -> str:
        return str(
            uuid5(
                PRODUCT_ACTION_OPERATION_NAMESPACE,
                "save_review_readiness_signal:current:" + idempotency_key,
            )
        )

    @staticmethod
    def _require_signal_owner_visibility(
        session: Session,
        note_id: int,
        proposal_id: int,
    ) -> None:
        note = session.get(InterviewNote, note_id)
        proposal = session.get(InterviewReviewProposal, proposal_id)
        application = (
            session.get(Application, note.application_id)
            if note is not None and note.application_id is not None
            else None
        )
        event = (
            session.get(ApplicationEvent, note.application_event_id)
            if note is not None and note.application_event_id is not None
            else None
        )
        if (
            note is None
            or proposal is None
            or application is None
            or application.deleted_at is not None
            or event is None
            or proposal.note_id != note.id
            or proposal.application_event_id != note.application_event_id
            or event.application_id != note.application_id
        ):
            raise ProductActionCoordinatorError(
                "review_readiness_not_found",
                status_code=404,
            )

    def _require_current_signal_request(
        self,
        session: Session,
        bundle: ProductActionBundleV1,
        *,
        note_id: int,
        request: SignalProposalRequestV1,
    ) -> ReadinessCandidateV1:
        self._require_signal_owner_visibility(
            session,
            note_id,
            request.proposal_id,
        )
        try:
            projection = self._candidate_projector(
                note_id,
                request.proposal_id,
                session,
            )
        except DBAPIError as exc:
            raise ProductActionCoordinatorError(
                "review_readiness_unavailable",
                status_code=503,
                retryable=True,
            ) from exc
        if projection.state == "unavailable":
            raise ProductActionCoordinatorError(
                "review_readiness_unavailable",
                status_code=503,
                retryable=True,
            )
        if projection.state != "ready":
            raise ProductActionCoordinatorError(
                "review_readiness_source_changed",
                status_code=409,
            )
        candidate = next(
            (
                item
                for item in projection.candidates
                if item.focus_id == request.focus_id
            ),
            None,
        )
        if candidate is None:
            raise ProductActionCoordinatorError(
                "product_action_idempotency_conflict",
                status_code=409,
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
        if not self._review_issuer.matches_persisted_request(
            cast(Any, bundle),
            route_payload_raw=self._signal_route(candidate, request),
        ):
            raise ProductActionCoordinatorError(
                "product_action_idempotency_conflict",
                status_code=409,
            )
        return candidate

    @staticmethod
    def _request_matches_signal_route(
        route: Mapping[str, JSONValue],
        *,
        note_id: int,
        request: SignalProposalRequestV1,
    ) -> bool:
        return all(
            (
                route.get("note_id") == note_id,
                route.get("proposal_id") == request.proposal_id,
                route.get("focus_id") == request.focus_id,
                route.get("expected_note_revision") == request.expected_note_revision,
                route.get("expected_candidate_fingerprint")
                == request.expected_candidate_fingerprint,
                route.get("domain_idempotency_key") == request.idempotency_key,
                route.get("user_note") == request.user_note,
            )
        )

    def _existing_signal_request(
        self,
        *,
        note_id: int,
        request: SignalProposalRequestV1,
    ) -> ProductActionProposalResultV1 | None:
        operation_id = self._signal_operation_id(request.idempotency_key)
        try:
            bundle = self._proposal_repository.load_bundle(operation_id)
        except ProductActionIntegrityError as exc:
            # Python's SQLite legacy transaction mode can observe publication
            # between the three read-only SELECTs. A writer-UoW reload separates
            # that transient snapshot from persistent corruption; only the former
            # is allowed to converge on the exact committed bundle.
            if exc.code == "product_action_bundle_absent":
                return None
            if exc.code == "partial_product_action_bundle":
                with self._session_factory() as session:
                    try:
                        uow = self._proposal_repository.begin_publication_uow(session)
                        bundle = self._proposal_repository.load_bundle_in_session(
                            session,
                            uow,
                            operation_id,
                        )
                        session.rollback()
                    except ProductActionIntegrityError as locked_exc:
                        try:
                            session.rollback()
                        except BaseException:
                            pass
                        if locked_exc.code == "product_action_bundle_absent":
                            return None
                        raise
            elif exc.code == "product_action_bundle_unreadable":
                raise ProductActionCoordinatorError(
                    "operation_result_unknown",
                    status_code=503,
                    retryable=True,
                ) from exc
            else:
                raise
        if (
            bundle.operation.tool_name != "save_review_readiness_signal"
            or bundle.route.action_name != "save_review_readiness_signal"
            or bundle.route.source_id != request.proposal_id
        ):
            raise ProductActionCoordinatorError(
                "review_readiness_not_found",
                status_code=404,
            )
        if bundle.classification == "exact_proposed" or bundle.operation.status == "rejected":
            with self._session_factory() as session:
                try:
                    uow = self._proposal_repository.begin_publication_uow(session)
                    locked = self._proposal_repository.load_bundle_in_session(
                        session,
                        uow,
                        operation_id,
                    )
                    if (
                        locked.operation.tool_name != "save_review_readiness_signal"
                        or locked.route.action_name != "save_review_readiness_signal"
                        or locked.route.source_id != request.proposal_id
                    ):
                        raise ProductActionCoordinatorError(
                            "review_readiness_not_found",
                            status_code=404,
                        )
                    if locked.classification == "exact_proposed":
                        route = self._route_payload(locked)
                        if route.get("note_id") != note_id:
                            raise ProductActionCoordinatorError(
                                "review_readiness_not_found",
                                status_code=404,
                            )
                        if not self._request_matches_signal_route(
                            route,
                            note_id=note_id,
                            request=request,
                        ):
                            raise ProductActionCoordinatorError(
                                "product_action_idempotency_conflict",
                                status_code=409,
                            )
                        self._require_current_signal_request(
                            session,
                            locked,
                            note_id=note_id,
                            request=request,
                        )
                        token = self._review_issuer.recover_confirmation_token(
                            cast(Any, locked)
                        )
                        session.rollback()
                        return ProductActionProposalResultV1(
                            locked.operation.id,
                            locked.operation.tool_call_id,
                            locked.operation.tool_name,
                            locked.operation.status,
                            False,
                            token,
                            None,
                            True,
                        )
                    if locked.operation.status == "rejected":
                        self._require_current_signal_request(
                            session,
                            locked,
                            note_id=note_id,
                            request=request,
                        )
                        self._verify_persisted_terminal_input(locked)
                        session.rollback()
                        return ProductActionProposalResultV1(
                            locked.operation.id,
                            locked.operation.tool_call_id,
                            locked.operation.tool_name,
                            locked.operation.status,
                            False,
                            None,
                            _terminal_result(locked),
                            True,
                        )
                    session.rollback()
                    bundle = locked
                except BaseException:
                    try:
                        session.rollback()
                    except BaseException:
                        pass
                    raise
        self._verify_persisted_terminal_input(bundle)
        if bundle.operation.status == "committed":
            aggregate = self._readiness_repository.load_by_operation(operation_id)
            if aggregate is None:
                raise ProductActionIntegrityError("readiness_signal_terminal_missing")
            if aggregate.source_note_id != note_id:
                raise ProductActionCoordinatorError(
                    "review_readiness_not_found",
                    status_code=404,
                )
            if any(
                (
                    aggregate.source_proposal_id != request.proposal_id,
                    aggregate.focus_id != request.focus_id,
                    aggregate.source_note_revision != request.expected_note_revision,
                    aggregate.candidate_fingerprint
                    != request.expected_candidate_fingerprint,
                    aggregate.domain_idempotency_key != request.idempotency_key,
                )
            ):
                raise ProductActionCoordinatorError(
                    "product_action_idempotency_conflict",
                    status_code=409,
                )
            if (
                aggregate.source_event_id is None
                or aggregate.source_proposal_id is None
            ):
                raise ProductActionIntegrityError("readiness_signal_source_identity")
            candidate = ReadinessCandidateV1(
                application_id=aggregate.application_id,
                event_id=aggregate.source_event_id,
                note_id=aggregate.source_note_id,
                proposal_id=aggregate.source_proposal_id,
                proposal_schema_version=2,
                focus_id=aggregate.focus_id,
                statement_text=aggregate.statement_text,
                source_note_revision=aggregate.source_note_revision,
                source_note_fingerprint=aggregate.source_note_fingerprint,
                source_proposal_hash=aggregate.source_proposal_hash,
                candidate_fingerprint=aggregate.candidate_fingerprint,
                evidence=aggregate.evidence,
            )
            if not self._review_issuer.matches_persisted_request(
                cast(Any, bundle),
                route_payload_raw=self._signal_route(candidate, request),
            ):
                raise ProductActionCoordinatorError(
                    "product_action_idempotency_conflict",
                    status_code=409,
                )
        return ProductActionProposalResultV1(
            bundle.operation.id,
            bundle.operation.tool_call_id,
            bundle.operation.tool_name,
            bundle.operation.status,
            False,
            None,
            _terminal_result(bundle),
            True,
        )

    @staticmethod
    def _confirmed_signal_locator(
        session: Session,
        *,
        note_id: int,
        request: SignalProposalRequestV1,
    ) -> ProductActionProposalResultV1 | None:
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
            return None
        signal, version = row
        if (
            version.source_note_revision != request.expected_note_revision
            or version.candidate_fingerprint != request.expected_candidate_fingerprint
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

    def _candidate_for_request(
        self,
        session: Session,
        note_id: int,
        request: SignalProposalRequestV1,
    ) -> ReadinessCandidateV1 | ProductActionProposalResultV1:
        confirmed = self._confirmed_signal_locator(
            session,
            note_id=note_id,
            request=request,
        )
        if confirmed is not None:
            return confirmed
        projection = self._candidate_projector(note_id, request.proposal_id, session)
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
        existing = self._existing_signal_request(
            note_id=note_id,
            request=decoded,
        )
        if existing is not None:
            return existing
        with self._session_factory() as session:
            try:
                self._require_signal_owner_visibility(
                    session,
                    note_id,
                    decoded.proposal_id,
                )
            except DBAPIError as exc:
                raise ProductActionCoordinatorError(
                    "review_readiness_unavailable",
                    status_code=503,
                    retryable=True,
                ) from exc
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
                    if (
                        existing_bundle.classification == "exact_proposed"
                        and existing_bundle.route.route_payload_json
                        == route_raw.decode("utf-8")
                    ):
                        self._require_current_signal_request(
                            session,
                            existing_bundle,
                            note_id=candidate.note_id,
                            request=request,
                        )
                        token = self._review_issuer.recover_confirmation_token(
                            cast(Any, existing_bundle)
                        )
                        self._revoke_prepared(prepared)
                        session.rollback()
                        return ProductActionProposalResultV1(
                            existing_bundle.operation.id,
                            existing_bundle.operation.tool_call_id,
                            existing_bundle.operation.tool_name,
                            existing_bundle.operation.status,
                            False,
                            token,
                            None,
                            True,
                        )
                    self._revoke_prepared(prepared)
                    session.rollback()
                    existing = self._existing_signal_request(
                        note_id=candidate.note_id,
                        request=request,
                    )
                    if existing is not None:
                        return existing
                    raise ProductActionCoordinatorError(
                        "product_action_idempotency_conflict",
                        status_code=409,
                    )
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
                        replay_grant=publication.replay_grant,
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
        replay_grant: ProductActionPublicationReplayV1 | None,
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
        if not allow_replay or replay_grant is None:
            raise ProductActionIntegrityError("product_action_publication_absent")
        with self._session_factory() as session:
            try:
                uow = self._proposal_repository.begin_publication_uow(session)
                fresh = self._proposal_repository.refresh_all_absent_from_grant_in_session(
                    session,
                    uow,
                    replay_grant,
                )
                session.rollback()
            except BaseException:
                try:
                    session.rollback()
                except BaseException:
                    pass
                raise
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

    def _rejection_decision_credential(
        self,
        bundle: ProductActionBundleV1,
    ) -> str:
        key = self._key_profiles.resolve(bundle.operation.fingerprint_key_id)
        envelope: dict[str, JSONValue] = {
            "schema_version": 1,
            "action_name": bundle.operation.tool_name,
            "operation_id": bundle.operation.id,
            "action_call_id": bundle.operation.tool_call_id,
            "confirmation_token_fingerprint": (
                bundle.operation.confirmation_token_fingerprint
            ),
            "route_payload_fingerprint": bundle.route.route_payload_fingerprint,
            "route_binding_fingerprint": bundle.route.route_binding_fingerprint,
            "semantic_claim_fingerprint": bundle.route.semantic_claim_fingerprint,
            "allowed_decisions": ["reject"],
        }
        return hmac.new(
            key.secret,
            b"product-action-rejection-decision-v1\0"
            + canonical_product_action_json(envelope).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _verify_token(
        self,
        bundle: ProductActionBundleV1,
        token: str,
        decision: Literal["approve", "modify", "reject"],
    ) -> None:
        key = self._key_profiles.resolve(bundle.operation.fingerprint_key_id)
        token_fingerprint = ledger_fingerprint(
            key,
            "write-operation-confirmation-token-v1",
            token.encode("ascii"),
        )
        if hmac.compare_digest(
            token_fingerprint,
            bundle.operation.confirmation_token_fingerprint or "",
        ):
            return
        if hmac.compare_digest(
            token,
            self._rejection_decision_credential(bundle),
        ):
            if decision != "reject":
                raise ProductActionCoordinatorError("product_action_stale")
            return
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

    def _require_authorization_used(
        self,
        authorization: ProductActionExecutionAuthorization,
    ) -> None:
        try:
            self._proof_registry.revoke(authorization)
        except ValueError:
            return
        raise ProductActionIntegrityError("execution_authorization_not_consumed")

    @staticmethod
    def _declared_executor_failure(
        handler: ProductActionHandlerV1,
        exc: Exception,
    ) -> ProductActionDeclaredExecutorFailureV1 | None:
        return next(
            (
                failure
                for failure in handler.declared_executor_failures
                if type(exc) is failure.exception_type
            ),
            None,
        )

    @staticmethod
    def _require_terminal_projection(
        projection: ProductActionTerminalProjectionV1,
        *,
        operation_id: str,
        action_name: str,
        status: Literal["committed", "failed"],
    ) -> ProductActionTerminalProjectionV1:
        if type(projection) is not ProductActionTerminalProjectionV1:
            raise ProductActionIntegrityError("product_action_terminal_projector")
        result = dict(projection.result)
        transport = dict(projection.transport)
        if (
            type(projection.visible_result) is not str
            or type(result.get("schema_version")) is not int
            or result.get("schema_version") != 1
            or result.get("action_name") != action_name
            or type(transport.get("schema_version")) is not int
            or transport.get("schema_version") != 1
            or transport.get("operation_id") != operation_id
            or transport.get("action_name") != action_name
            or transport.get("status") != status
        ):
            raise ProductActionIntegrityError("product_action_terminal_projector")
        if status == "committed" and transport.get("result") != result:
            raise ProductActionIntegrityError("product_action_terminal_projector")
        if action_name == "save_review_readiness_signal" and status == "committed":
            signal_id = result.get("signal_id")
            version_id = result.get("signal_version_id")
            revision = result.get("signal_revision")
            undo = dict(projection.undo) if projection.undo is not None else None
            if (
                set(result)
                != {
                    "schema_version",
                    "action_name",
                    "outcome",
                    "signal_id",
                    "signal_version_id",
                    "signal_revision",
                    "source_status",
                }
                or result.get("outcome") != "created"
                or result.get("source_status") != "current"
                or type(signal_id) is not int
                or signal_id < 1
                or type(version_id) is not int
                or version_id < 1
                or type(revision) is not int
                or revision < 1
                or projection.visible_result != "已保存为下次准备重点。"
                or set(transport)
                != {
                    "schema_version",
                    "operation_id",
                    "action_name",
                    "status",
                    "result",
                }
                or undo is None
                or set(undo)
                != {
                    "kind",
                    "signal_id",
                    "created_version_id",
                    "expected_current_version_id",
                    "expected_signal_revision",
                    "parent_operation_id",
                }
                or undo.get("kind") != "retract_review_readiness_signal_v1"
                or type(undo.get("signal_id")) is not int
                or undo.get("signal_id") != signal_id
                or type(undo.get("created_version_id")) is not int
                or undo.get("created_version_id") != version_id
                or type(undo.get("expected_current_version_id")) is not int
                or undo.get("expected_current_version_id") != version_id
                or type(undo.get("expected_signal_revision")) is not int
                or undo.get("expected_signal_revision") != revision
                or undo.get("parent_operation_id") != operation_id
            ):
                raise ProductActionIntegrityError("product_action_terminal_projector")
        if action_name == "confirm_interview_story" and status == "committed":
            direct = transport.get("legacy_direct_commit")
            replay = transport.get("legacy_reconciliation_or_replay")
            story_id = result.get("story_id")
            version_id = result.get("story_version_id")
            revision = result.get("story_revision")
            if (
                set(result)
                != {
                    "schema_version",
                    "action_name",
                    "outcome",
                    "story_id",
                    "story_version_id",
                    "story_revision",
                }
                or result.get("outcome") not in {"created", "version_appended"}
                or type(story_id) is not int
                or story_id < 1
                or type(version_id) is not int
                or version_id < 1
                or type(revision) is not int
                or revision < 1
                or projection.visible_result != "已保存到经历素材。"
                or set(transport)
                != {
                    "schema_version",
                    "operation_id",
                    "action_name",
                    "status",
                    "result",
                    "legacy_direct_commit",
                    "legacy_reconciliation_or_replay",
                }
                or type(direct) is not dict
                or type(replay) is not dict
                or set(direct) != {"status_code", "body"}
                or set(replay) != {"status_code", "body"}
                or type(direct.get("status_code")) is not int
                or direct.get("status_code") != 201
                or type(replay.get("status_code")) is not int
                or replay.get("status_code") != 200
            ):
                raise ProductActionIntegrityError("product_action_terminal_projector")
            direct_body = direct.get("body")
            replay_body = replay.get("body")
            if (
                type(direct_body) is not dict
                or type(replay_body) is not dict
                or set(direct_body) != {"story_id", "version_id", "created"}
                or set(replay_body) != {"story_id", "version_id", "created"}
                or type(direct_body.get("story_id")) is not int
                or direct_body.get("story_id") != story_id
                or type(direct_body.get("version_id")) is not int
                or direct_body.get("version_id") != version_id
                or direct_body.get("created") is not True
                or type(replay_body.get("story_id")) is not int
                or replay_body.get("story_id") != story_id
                or type(replay_body.get("version_id")) is not int
                or replay_body.get("version_id") != version_id
                or replay_body.get("created") is not False
            ):
                raise ProductActionIntegrityError("product_action_terminal_projector")
            undo = dict(projection.undo) if projection.undo is not None else None
            if undo is None:
                raise ProductActionIntegrityError("product_action_terminal_projector")
            if result.get("outcome") == "created":
                if (
                    set(undo)
                    != {
                        "kind",
                        "story_id",
                        "created_version_id",
                        "expected_current_version_id",
                        "expected_story_revision",
                        "expected_status",
                    }
                    or undo.get("kind") != "archive_created_story_v1"
                    or type(undo.get("story_id")) is not int
                    or undo.get("story_id") != story_id
                    or type(undo.get("created_version_id")) is not int
                    or undo.get("created_version_id") != version_id
                    or type(undo.get("expected_current_version_id")) is not int
                    or undo.get("expected_current_version_id") != version_id
                    or type(undo.get("expected_story_revision")) is not int
                    or undo.get("expected_story_revision") != revision
                    or undo.get("expected_status") != "active"
                ):
                    raise ProductActionIntegrityError(
                        "product_action_terminal_projector"
                    )
            elif (
                set(undo)
                != {
                    "kind",
                    "story_id",
                    "created_version_id",
                    "previous_current_version_id",
                    "previous_title",
                    "expected_post_revision",
                }
                or undo.get("kind") != "restore_story_pointer_v1"
                or type(undo.get("story_id")) is not int
                or undo.get("story_id") != story_id
                or type(undo.get("created_version_id")) is not int
                or undo.get("created_version_id") != version_id
                or type(undo.get("previous_current_version_id")) is not int
                or cast(int, undo.get("previous_current_version_id")) < 1
                or type(undo.get("previous_title")) is not str
                or not cast(str, undo.get("previous_title")).strip()
                or len(cast(str, undo.get("previous_title"))) > 200
                or type(undo.get("expected_post_revision")) is not int
                or undo.get("expected_post_revision") != revision
            ):
                raise ProductActionIntegrityError("product_action_terminal_projector")
        if status == "failed" and (
            action_name != "confirm_interview_story"
            or set(result)
            != {"schema_version", "action_name", "outcome", "code"}
            or result.get("outcome") != "failed"
            or result.get("code") != "product_action_story_write_conflict"
            or projection.visible_result
            != "经历素材写入发生冲突，请刷新后重试。"
            or set(transport)
            != {"schema_version", "operation_id", "action_name", "status", "code"}
            or transport.get("code") != result.get("code")
            or projection.undo is not None
        ):
            raise ProductActionIntegrityError("product_action_terminal_projector")
        return ProductActionTerminalProjectionV1(
            _json_mapping(result),
            projection.visible_result,
            _json_mapping(transport),
            (
                _json_mapping(projection.undo)
                if projection.undo is not None
                else None
            ),
        )

    @classmethod
    def _verified_decision_transport(
        cls,
        bundle: ProductActionBundleV1,
    ) -> MappingProxyType[str, FrozenJSONValue]:
        result = dict(_terminal_result(bundle))
        transport = _terminal_json_object(
            bundle.operation.transport_json,
            "product_action_terminal_transport",
        )
        status = bundle.operation.status
        if status == "rejected":
            expected_visible = {
                "save_review_readiness_signal": "已取消保存准备重点。",
                "confirm_interview_story": "已取消保存经历素材。",
            }.get(bundle.operation.tool_name)
            if (
                set(result) != {"schema_version", "action_name", "decision"}
                or type(result.get("schema_version")) is not int
                or result.get("schema_version") != 1
                or result.get("action_name") != bundle.operation.tool_name
                or result.get("decision") != "rejected"
                or set(transport)
                != {
                    "schema_version",
                    "operation_id",
                    "action_name",
                    "status",
                    "result",
                }
                or type(transport.get("schema_version")) is not int
                or transport.get("schema_version") != 1
                or transport.get("operation_id") != bundle.operation.id
                or transport.get("action_name") != bundle.operation.tool_name
                or transport.get("status") != "rejected"
                or transport.get("result") != {"decision": "rejected"}
                or bundle.operation.visible_result != expected_visible
                or bundle.operation.undo_json is not None
            ):
                raise ProductActionIntegrityError("product_action_terminal_transport")
            return _frozen_json_mapping(transport)
        if status not in {"committed", "failed"}:
            raise ProductActionIntegrityError("product_action_terminal_transport")
        undo = (
            _terminal_json_object(
                bundle.operation.undo_json,
                "product_action_terminal_undo",
            )
            if bundle.operation.undo_json is not None
            else None
        )
        projection = cls._require_terminal_projection(
            ProductActionTerminalProjectionV1(
                result,
                bundle.operation.visible_result or "",
                transport,
                undo,
            ),
            operation_id=bundle.operation.id,
            action_name=bundle.operation.tool_name,
            status=cast(Literal["committed", "failed"], status),
        )
        return _frozen_json_mapping(projection.transport)

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
        self._verify_token(bundle, control.confirmation_token, control.decision)
        if control.decision == "reject":
            request_fingerprint = self._request_fingerprint(bundle, control, None)
            if bundle.classification == "exact_terminal":
                return self._terminal_replay(bundle, request_fingerprint)
            return self._reject(
                bundle,
                control,
                request_fingerprint,
                allow_retry=True,
                completion_kind="direct_commit",
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
            completion_kind="direct_commit",
        )

    def _reject(
        self,
        initial: ProductActionBundleV1,
        control: _DecisionControlV1,
        request_fingerprint: str,
        *,
        allow_retry: bool,
        completion_kind: ProductActionCompletionKind,
    ) -> ProductActionDecisionResultV1:
        with self._session_factory() as session:
            try:
                uow = self._proposal_repository.begin_publication_uow(session)
                bundle = self._proposal_repository.load_bundle_in_session(
                    session,
                    uow,
                    initial.operation.id,
                )
                self._verify_token(bundle, control.confirmation_token, control.decision)
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
                    completion_kind,
                    self._verified_decision_transport(terminal_bundle),
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
        completion_kind: ProductActionCompletionKind,
    ) -> ProductActionDecisionResultV1:
        with self._session_factory() as session:
            try:
                uow = self._proposal_repository.begin_publication_uow(session)
                bundle = self._proposal_repository.load_bundle_in_session(
                    session,
                    uow,
                    initial.operation.id,
                )
                self._verify_token(bundle, control.confirmation_token, control.decision)
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
                preclaim_savepoint = session.begin_nested()
                try:
                    locked_outcome = handler.locked_recheck(
                        session,
                        route_payload,
                        trusted,
                    )
                except BaseException:
                    try:
                        preclaim_savepoint.rollback()
                    except BaseException:
                        pass
                    raise
                preclaim_savepoint.rollback()
                session.expire_all()
                if type(locked_outcome) is ProductActionPreClaimDispositionV1:
                    disposition = locked_outcome
                    if (
                        disposition.action_name != handler.action_name
                        or disposition.disposition
                        not in handler.declared_preclaim_dispositions
                    ):
                        raise ProductActionIntegrityError(
                            "product_action_preclaim_disposition"
                        )
                    attempt_id = route_payload.get("attempt_id")
                    if type(attempt_id) is not int:
                        raise ProductActionIntegrityError(
                            "product_action_preclaim_disposition"
                        )
                    story_attempt = session.get(
                        InterviewStoryProposalAttempt,
                        attempt_id,
                    )
                    if (
                        story_attempt is None
                        or story_attempt.attempt_status != "ready"
                        or story_attempt.failure_category != ""
                        or story_attempt.product_action_operation_id
                        != bundle.operation.id
                        or story_attempt.generation_revision
                        != route_payload.get("generation_revision")
                        or story_attempt.product_action_generation
                        != route_payload.get("product_action_generation")
                        or story_attempt.proposal_hash
                        != route_payload.get("proposal_hash")
                        or story_attempt.source_fingerprint
                        != route_payload.get("source_fingerprint")
                        or story_attempt.target_story_id
                        != route_payload.get("target_story_id")
                        or story_attempt.confirmed_story_id is not None
                        or story_attempt.confirmed_story_version_id is not None
                        or story_attempt.confirmed_at is not None
                    ):
                        raise ProductActionIntegrityError(
                            "product_action_preclaim_disposition"
                        )
                    story_attempt.attempt_status = "invalidated"
                    story_attempt.failure_category = "source_changed"
                    session.flush()
                    unchanged = self._proposal_repository.load_bundle_in_session(
                        session,
                        uow,
                        bundle.operation.id,
                    )
                    if unchanged.classification != "exact_proposed":
                        raise ProductActionIntegrityError(
                            "product_action_preclaim_disposition"
                        )
                    try:
                        session.commit()
                    except DBAPIError as exc:
                        try:
                            session.rollback()
                        except BaseException:
                            pass
                        raise ProductActionCoordinatorError(
                            "operation_result_unknown",
                            status_code=503,
                            retryable=True,
                        ) from exc
                    raise ProductActionCoordinatorError("product_action_stale")
                locked = self._seal_preflight(
                    handler,
                    cast(ProductActionPreflightV1, locked_outcome),
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
                savepoint = session.begin_nested()
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
                except BaseException as exc:
                    try:
                        savepoint.rollback()
                    finally:
                        if not isinstance(exc, Exception):
                            try:
                                self._proof_registry.revoke(authorization)
                            except ValueError:
                                pass
                            raise
                    self._require_authorization_used(authorization)
                    failure = self._declared_executor_failure(handler, exc)
                    if failure is None:
                        raise
                    projection = self._require_terminal_projection(
                        handler.project_failed_terminal(operation.id, failure),
                        operation_id=operation.id,
                        action_name=operation.tool_name,
                        status="failed",
                    )
                    if projection.result.get("code") != failure.failure_code:
                        raise ProductActionIntegrityError(
                            "product_action_terminal_projector"
                        )
                    terminal_status: Literal["committed", "failed"] = "failed"
                    terminal_budgets = _FAILED_TERMINAL_BUDGETS
                    failure_category: str | None = failure.failure_category
                    failure_code: str | None = failure.failure_code
                else:
                    savepoint.commit()
                    self._require_authorization_used(authorization)
                    projection = self._require_terminal_projection(
                        handler.project_committed_terminal(
                            operation.id,
                            handler_result,
                        ),
                        operation_id=operation.id,
                        action_name=operation.tool_name,
                        status="committed",
                    )
                    terminal_status = "committed"
                    terminal_budgets = handler.terminal_budgets
                    failure_category = None
                    failure_code = None
                terminal = build_terminal_payload(
                    status=terminal_status,
                    result_contract="product_action_json_v1",
                    result=dict(projection.result),
                    visible_result=projection.visible_result,
                    transport=dict(projection.transport),
                    undo=(dict(projection.undo) if projection.undo is not None else None),
                    failure_category=failure_category,
                    failure_code=failure_code,
                    budgets=terminal_budgets.field_budgets,
                )
                _enforce_action_aggregate(terminal, terminal_budgets.aggregate_bytes)
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
                _append_transition(session, operation.id, 4, terminal_status, now)
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
                    terminal_status,
                    _terminal_result(terminal_bundle),
                    completion_kind,
                    self._verified_decision_transport(terminal_bundle),
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
                completion_kind="reconciliation",
            )
            return replay
        if allow_retry and control.decision == "reject":
            return self._reject(
                bundle,
                control,
                request_fingerprint,
                allow_retry=False,
                completion_kind="reconciliation",
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
            completion_kind="reconciliation",
        )

    def _terminal_replay(
        self,
        bundle: ProductActionBundleV1,
        request_fingerprint: str,
        *,
        effective_payload_sha256: str | None = None,
        completion_kind: ProductActionCompletionKind = "replay",
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
            completion_kind,
            self._verified_decision_transport(bundle),
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
            except ProductActionIntegrityError as exc:
                try:
                    session.rollback()
                except BaseException:
                    pass
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
            except BaseException:
                try:
                    session.rollback()
                except BaseException:
                    pass
                raise

    def recover_story_owner(
        self,
        *,
        attempt_id: int,
        operation_id: str,
    ) -> ProductActionRecoveryV1:
        if type(attempt_id) is not int or attempt_id < 1:
            raise ProductActionCoordinatorError(
                "interview_story_not_found",
                status_code=404,
            )
        self._require_capability("confirm_interview_story")
        try:
            normalized = require_product_action_uuid(operation_id, "operation_id")
        except ProductActionContractError as exc:
            raise ProductActionCoordinatorError(
                "interview_story_not_found",
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
                        "interview_story_not_found",
                        status_code=404,
                    )
                route = self._route_payload(bundle)
                if (
                    bundle.operation.tool_name != "confirm_interview_story"
                    or route.get("attempt_id") != attempt_id
                ):
                    raise ProductActionCoordinatorError(
                        "interview_story_not_found",
                        status_code=404,
                    )
                attempt = session.get(InterviewStoryProposalAttempt, attempt_id)
                source_current = (
                    attempt is not None
                    and attempt.attempt_status == "ready"
                    and type(attempt.generation_revision) is int
                    and attempt.generation_revision == route["generation_revision"]
                    and type(attempt.product_action_generation) is int
                    and attempt.product_action_generation
                    == route["product_action_generation"]
                    and attempt.product_action_operation_id == bundle.operation.id
                    and attempt.proposal_hash == route["proposal_hash"]
                    and attempt.source_fingerprint == route["source_fingerprint"]
                    and attempt.target_story_id == route["target_story_id"]
                    and attempt.failure_category == ""
                    and attempt.confirmation_token_hash == ""
                    and attempt.confirmation_payload_hash == ""
                    and attempt.confirmed_story_id is None
                    and attempt.confirmed_story_version_id is None
                    and attempt.confirmed_at is None
                )
                if not source_current:
                    session.rollback()
                    return self.recover_story_rejection_control(
                        attempt_id=attempt_id,
                        operation_id=normalized,
                    )
                binding = (
                    "interview_story_owner",
                    route["attempt_id"],
                    route["generation_revision"],
                    route["proposal_hash"],
                    bundle.operation.id,
                    bundle.operation.tool_call_id,
                    bundle.route.route_payload_fingerprint,
                    bundle.route.route_binding_fingerprint,
                    bundle.route.request_origin,
                    StoryOwnerRecoveryProof.allowed_decisions,
                    route["product_action_generation"],
                )
                proof = cast(
                    StoryOwnerRecoveryProof,
                    self._proof_registry._issue(
                        StoryOwnerRecoveryProof,
                        action_name="confirm_interview_story",
                        binding=binding,
                    ),
                )
                with self._proof_registry.claim(
                    proof,
                    proof_type=StoryOwnerRecoveryProof,
                    action_name="confirm_interview_story",
                    expected_binding=binding,
                ):
                    token = self._story_issuer.recover_confirmation_token(
                        cast(Any, bundle)
                    )
                session.rollback()
                return ProductActionRecoveryV1(
                    bundle.operation.id,
                    cast(str, bundle.operation.tool_call_id),
                    bundle.operation.tool_name,
                    "proposed",
                    token,
                    StoryOwnerRecoveryProof.allowed_decisions,
                    False,
                )
            except ProductActionIntegrityError as exc:
                try:
                    session.rollback()
                except BaseException:
                    pass
                if exc.code == "product_action_bundle_absent":
                    raise ProductActionCoordinatorError(
                        "interview_story_not_found",
                        status_code=404,
                    ) from exc
                if exc.code == "product_action_bundle_unreadable":
                    raise ProductActionCoordinatorError(
                        "operation_result_unknown",
                        status_code=503,
                        retryable=True,
                    ) from exc
                raise
            except BaseException:
                try:
                    session.rollback()
                except BaseException:
                    pass
                raise

    def recover_story_rejection_control(
        self,
        *,
        attempt_id: int,
        operation_id: str,
    ) -> ProductActionRecoveryV1:
        if type(attempt_id) is not int or attempt_id < 1:
            raise ProductActionCoordinatorError(
                "interview_story_not_found",
                status_code=404,
            )
        try:
            normalized = require_product_action_uuid(operation_id, "operation_id")
        except ProductActionContractError as exc:
            raise ProductActionCoordinatorError(
                "interview_story_not_found",
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
                        "interview_story_not_found",
                        status_code=404,
                    )
                route = self._route_payload(bundle)
                if (
                    bundle.operation.tool_name != "confirm_interview_story"
                    or route.get("attempt_id") != attempt_id
                ):
                    raise ProductActionCoordinatorError(
                        "interview_story_not_found",
                        status_code=404,
                    )
                binding = (
                    "interview_story_owner",
                    route["attempt_id"],
                    RejectionOnlyRecoveryProof.live_source_state,
                    route["generation_revision"],
                    route["product_action_generation"],
                    route["proposal_hash"],
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
                        action_name="confirm_interview_story",
                        binding=binding,
                    ),
                )
                with self._proof_registry.claim(
                    proof,
                    proof_type=RejectionOnlyRecoveryProof,
                    action_name="confirm_interview_story",
                    expected_binding=binding,
                ):
                    token = self._rejection_decision_credential(bundle)
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
            except ProductActionIntegrityError as exc:
                try:
                    session.rollback()
                except BaseException:
                    pass
                if exc.code == "product_action_bundle_absent":
                    raise ProductActionCoordinatorError(
                        "interview_story_not_found",
                        status_code=404,
                    ) from exc
                if exc.code == "product_action_bundle_unreadable":
                    raise ProductActionCoordinatorError(
                        "operation_result_unknown",
                        status_code=503,
                        retryable=True,
                    ) from exc
                raise
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
                    token = self._rejection_decision_credential(bundle)
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
            except ProductActionIntegrityError as exc:
                try:
                    session.rollback()
                except BaseException:
                    pass
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
            except BaseException:
                try:
                    session.rollback()
                except BaseException:
                    pass
                raise


__all__ = [
    "ProductActionCompletionKind",
    "ProductActionCoordinator",
    "ProductActionCoordinatorError",
    "ProductActionDeclaredExecutorFailureV1",
    "ProductActionDecisionResultV1",
    "ProductActionHandlerResultV1",
    "ProductActionHandlerV1",
    "ProductActionPreClaimDispositionV1",
    "ProductActionPreflightV1",
    "ProductActionProposalResultV1",
    "ProductActionRecoveryV1",
    "ProductActionStateV1",
    "ProductActionStoryWriteConflict",
    "ProductActionTerminalBudgetsV1",
    "ProductActionTerminalProjectionV1",
    "TrustedProductActionDecisionV1",
    "seal_interview_story_product_action_handler",
]
