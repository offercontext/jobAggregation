"""Trusted, provider-free Pilot action orchestration.

The three writes exposed by the Pilot UI predate the general Agent tool
pipeline.  This module is the deliberately small compatibility boundary for
those writes.  It owns the allowlisted route names, pending-card construction,
confirmation token/edit rules, and the existing Legacy Ledger transaction;
the model catalog, provider resolver, and context projector are intentionally
not dependencies of this bridge.

The adapter is transport independent.  ``start_turn`` and ``confirm`` return a
typed outcome plus the small event prefix needed by a direct stream.  A stream
transport may therefore render a precomputed outcome without creating an
Agent worker or running the operation a second time.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from secrets import compare_digest
from typing import Any, Protocol, TypeVar, cast
from uuid import UUID, uuid4

from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.ai.deterministic_actions import (
    PilotAction,
    PilotActionDecision,
    PilotOutcomeAction,
    PilotSubmissionSnapshotAction,
    build_outcome_pending_action,
    build_pilot_pending_action,
    build_submission_snapshot_pending_action,
    decide_pilot_action,
    parse_pilot_action,
)
from offerpilot.ai.tool_runtime.contracts import JSONValue
from offerpilot.ai.tool_runtime.legacy import (
    LegacyInitialRouteIssuer,
    LegacyInitialRoutePort,
    LEGACY_DETERMINISTIC_NAMES,
    LegacyPendingPresentationV1,
    RuntimeRequestOwnerLeaseFactory,
)
from offerpilot.ai.tool_runtime.legacy_proof import (
    LegacyApprovedConfirmationInput,
    LegacyConfirmationLookupIdentity,
    LegacyPreparedInputPort,
    LegacyRouteIssuanceLease,
    PreparedLegacyCall,
    PreparedLegacyInputV1,
)
from offerpilot.ai.tool_runtime.metadata import OperationRouteIdentityV1, ToolOperationMetadataPort
from offerpilot.ai.types import Message, ToolCall
from offerpilot.ai.write_operations import (
    DeliveryOwnership,
    LegacyApprovedBoundRoute,
    LegacyApprovedRouteBinder,
    PendingPersistenceRouteHandle,
    PendingPersistenceRoutePort,
    PendingRouteIdentityV1,
    LedgerOperationPreheader,
    OperationCommitted,
    OperationFailed,
    OperationReplay,
    OperationUnknown,
    WriteOperationError,
    ledger_fingerprint,
    operation_request_fingerprint,
    pending_action_identity,
)
from sqlalchemy.orm import Session
from offerpilot.repositories.application_jd_versions import ApplicationJDService
from offerpilot.repositories.applications import ApplicationsRepository

from .contracts import (
    AssistantMessageEvent,
    ConfirmationRequiredEvent,
    ConfirmationRequiredOutcome,
    ConfirmationRequest,
    MetaEvent,
    MessageOutcome,
    OperationPendingOutcome,
    OperationReplayOutcome,
    PendingActionPayload,
    PreparationKind,
    RuntimeEvent,
    RuntimeFailureOutcome,
    RuntimeOutcome,
    RuntimeTransportContext,
    StartTurnRequest,
    StatusEvent,
    StreamExecutionMode,
    UserMessageSavedEvent,
    LegacyExecutionContext,
    LegacyReadContext,
    WriteStatus,
    freeze_json_mapping,
)
from .errors import RuntimeFailureCode
from .legacy_route import (
    LegacyConfirmationRouteComponents,
    LegacyPersistedPresentationPort,
)
from .persistence import PersistenceResult, PersistenceStatus


_DETERMINISTIC_ACTION_KINDS = frozenset(
    {
        "application_jd_save",
        "application_submission_snapshot",
        "application_outcome_record",
    }
)

_CANCELLED_TOOL_RESULT = json.dumps(
    {"status": "cancelled", "message": "用户取消了该操作，未执行。"},
    ensure_ascii=False,
)
_InitialRouteResult = TypeVar("_InitialRouteResult")


class _Persistence(Protocol):
    def get_pending_action(self, conversation_id: int) -> object | None: ...

    def get_pending_clarification(self, conversation_id: int) -> object | None: ...

    def persist_initial_user_message(self, conversation_id: int, content: str) -> object: ...

    def persist_initial_pending(
        self,
        conversation_id: int,
        messages: Sequence[object],
        pending: PendingAction,
        *,
        route_handle: object,
    ) -> object: ...

    def persist_clarification(
        self,
        conversation_id: int,
        messages: Sequence[object],
        pending: PendingAction,
        question: str,
        *,
        route_handle: object,
    ) -> object: ...

    def clear_pending_clarification(self, conversation_id: int) -> object: ...

    def persist_assistant_message(self, conversation_id: int, content: str) -> object: ...

    def persist_confirmation_delivery(
        self,
        conversation_id: int,
        ownership: object | None,
        origin_tool_message: Message,
        continuation: Sequence[Message],
        chained_pending: PendingAction | None,
        *,
        route_handle: PendingPersistenceRouteHandle | None,
        expected_pending: PendingAction,
        claim_id: str,
        undo: dict[str, Any] | None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class DeterministicDependencies:
    """Server-owned dependencies for :class:`DeterministicPilotAdapter`."""

    persistence: _Persistence
    applications: object
    application_jd_versions: object
    application_outcomes: object
    write_operations: object | None = None
    write_coordinator: object | None = None
    legacy_request_owner_lease_factory: RuntimeRequestOwnerLeaseFactory | None = field(
        default=None, repr=False, compare=False
    )
    legacy_initial_route_port: LegacyInitialRoutePort | None = field(
        default=None, repr=False, compare=False
    )
    legacy_jd_clarification_issuer: LegacyInitialRouteIssuer | None = field(
        default=None, repr=False, compare=False
    )
    legacy_jd_deterministic_action_issuer: LegacyInitialRouteIssuer | None = field(
        default=None, repr=False, compare=False
    )
    legacy_submission_snapshot_issuer: LegacyInitialRouteIssuer | None = field(
        default=None, repr=False, compare=False
    )
    legacy_outcome_recording_issuer: LegacyInitialRouteIssuer | None = field(
        default=None, repr=False, compare=False
    )
    legacy_confirmation_routes: LegacyConfirmationRouteComponents | None = field(
        default=None, repr=False, compare=False
    )
    operation_port: ToolOperationMetadataPort | None = field(
        default=None, repr=False, compare=False
    )
    pending_persistence_route_port: PendingPersistenceRoutePort | None = field(
        default=None, repr=False, compare=False
    )
    id_factory: Callable[[], str] = field(default=lambda: uuid4().hex, repr=False, compare=False)
    key_factory: Callable[[], str] = field(default=lambda: uuid4().hex, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class DeterministicExecution:
    """A precomputed deterministic result and its direct-stream event prefix."""

    outcome: RuntimeOutcome
    events: tuple[RuntimeEvent, ...] = ()
    preparation_kind: PreparationKind = PreparationKind.DETERMINISTIC_INITIAL
    execution_mode: StreamExecutionMode = StreamExecutionMode.DIRECT
    pending_replay: bool = False
    input_message_id: int | None = None
    journal_started: bool = False

    def __post_init__(self) -> None:
        if not isinstance(
            self.outcome,
            (
                MessageOutcome,
                ConfirmationRequiredOutcome,
                RuntimeFailureOutcome,
                OperationPendingOutcome,
                OperationReplayOutcome,
            ),
        ):
            raise TypeError("outcome must be a RuntimeOutcome")
        if type(self.events) is not tuple:
            raise TypeError("events must be a tuple")
        if self.execution_mode is not StreamExecutionMode.DIRECT:
            raise ValueError("deterministic execution is always direct")
        if self.input_message_id is not None and (
            type(self.input_message_id) is not int or self.input_message_id <= 0
        ):
            raise ValueError("input_message_id must be a positive integer or None")


@dataclass(frozen=True, slots=True)
class _DeterministicConfirmationPreflight:
    """One bounded Ledger decision for Legacy confirmation routing and replay."""

    preheader: LedgerOperationPreheader | None = field(repr=False, compare=False)
    matched: bool
    execution: DeterministicExecution | None = None
    route_identity: tuple[int, str, str, str] | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if type(self.matched) is not bool:
            raise TypeError("matched must be a bool")
        if self.preheader is not None and type(self.preheader) is not LedgerOperationPreheader:
            raise TypeError("preheader must be an exact LedgerOperationPreheader")
        if self.matched and self.preheader is None:
            raise ValueError("matched preflight requires a preheader")
        if self.matched and self.route_identity is None:
            raise ValueError("matched preflight requires a route identity")

    @property
    def operation(self) -> object | None:
        return self.preheader.operation if self.preheader is not None else None


class _ApprovedLegacyBoundRoute:
    __slots__ = (
        "_routes",
        "_session",
        "_lease",
        "_prepared",
        "_operation_port",
        "_pending_port",
        "_pending_identity",
        "_parent_route_handle",
        "_on_prepared",
        "_on_bound",
        "_projected",
        "_executed",
        "_jd_service",
        "_outcomes_repository",
    )

    def __init__(
        self,
        *,
        routes: LegacyConfirmationRouteComponents,
        session: Session,
        lease: LegacyRouteIssuanceLease,
        prepared: PreparedLegacyCall,
        operation_port: ToolOperationMetadataPort,
        pending_port: PendingPersistenceRoutePort,
        pending_identity: PendingRouteIdentityV1,
        on_prepared: Callable[[PreparedLegacyInputV1], object],
        on_bound: Callable[[object], object] | None,
        jd_service: object,
        outcomes_repository: object,
    ) -> None:
        self._routes = routes
        self._session = session
        self._lease = lease
        self._prepared = prepared
        self._operation_port = operation_port
        self._pending_port = pending_port
        self._pending_identity = pending_identity
        self._parent_route_handle: object | None = None
        self._on_prepared = on_prepared
        self._on_bound = on_bound
        self._projected = False
        self._executed = False
        self._jd_service = jd_service
        self._outcomes_repository = outcomes_repository

    def prepared_call(self) -> PreparedLegacyCall:
        return self._prepared

    def prepared_input_port(self) -> LegacyPreparedInputPort:
        return self._routes.prepared_input_port

    def accept_prepared_input(
        self,
        prepared: PreparedLegacyCall,
        projected: PreparedLegacyInputV1,
    ) -> None:
        if (
            self._projected
            or prepared is not self._prepared
            or type(projected) is not PreparedLegacyInputV1
        ):
            raise WriteOperationError("operation_not_committed", retryable=True)
        self._projected = True
        self._on_prepared(projected)

    def execute(self, prepared: PreparedLegacyCall) -> str:
        if self._executed or not self._projected:
            raise WriteOperationError("operation_not_committed", retryable=True)
        if prepared is not self._prepared:
            raise WriteOperationError("operation_not_committed", retryable=True)
        self._executed = True
        verifier = self._routes.pending_identity_verifier_port
        locked = verifier.locked_recheck(
            self._session,
            self._lease,
            self._prepared,
        )
        claim = verifier.bind_claim(
            self._session,
            self._lease,
            locked,
        )
        proof = self._routes.proof_issuer.issue_after_claim(
            self._session,
            self._lease,
            claim,
            self._prepared,
        )
        handle = self._routes.catalog.resolve_server_loaded(proof)
        identity = self._pending_identity
        operation_handle = self._operation_port.bind_legacy(
            handle,
            OperationRouteIdentityV1(
                operation_id=identity.operation_id,
                tool_call_id=identity.tool_call_id,
                revision=identity.pending_action_revision,
                arguments_digest=identity.arguments_digest,
            ),
        )
        try:
            self._parent_route_handle = self._pending_port.bind_primary_parent(
                operation_handle,
                identity,
            )
            context = LegacyExecutionContext(
                self._session,
                cast(Any, self._jd_service),
                cast(Any, self._outcomes_repository),
            )
            return self._routes.proof_consumer_port.execute(
                handle,
                context,
                before_execute=self._record_bound_journal,
            )
        finally:
            self._operation_port.revoke_legacy(operation_handle)

    def _record_bound_journal(self) -> None:
        if self._on_bound is not None:
            self._on_bound(self._session)

    def primary_parent_route_handle(self) -> object:
        handle = self._parent_route_handle
        if handle is None:
            raise WriteOperationError("operation_not_committed", retryable=True)
        self._parent_route_handle = None
        return handle

    def _revoke_unclaimed_parent(self) -> None:
        handle = self._parent_route_handle
        self._parent_route_handle = None
        if handle is not None:
            self._pending_port.revoke_pending(handle)


class _ApprovedLegacyRouteContext(AbstractContextManager[LegacyApprovedBoundRoute]):
    __slots__ = (
        "_routes",
        "_write_session",
        "_session_factory",
        "_lookup",
        "_confirmation_input",
        "_operation_port",
        "_pending_port",
        "_pending_identity",
        "_on_prepared",
        "_on_bound",
        "_jd_service",
        "_outcomes_repository",
        "_lease",
        "_bound_route",
    )

    def __init__(
        self,
        *,
        routes: LegacyConfirmationRouteComponents,
        write_session: Session,
        session_factory: Callable[[], AbstractContextManager[Session]],
        lookup: LegacyConfirmationLookupIdentity,
        confirmation_input: LegacyApprovedConfirmationInput,
        operation_port: ToolOperationMetadataPort,
        pending_port: PendingPersistenceRoutePort,
        pending_identity: PendingRouteIdentityV1,
        on_prepared: Callable[[PreparedLegacyInputV1], object],
        on_bound: Callable[[object], object] | None,
        jd_service: object,
        outcomes_repository: object,
    ) -> None:
        self._routes = routes
        self._write_session = write_session
        self._session_factory = session_factory
        self._lookup = lookup
        self._confirmation_input = confirmation_input
        self._operation_port = operation_port
        self._pending_port = pending_port
        self._pending_identity = pending_identity
        self._on_prepared = on_prepared
        self._on_bound = on_bound
        self._jd_service = jd_service
        self._outcomes_repository = outcomes_repository
        self._lease: LegacyRouteIssuanceLease | None = None
        self._bound_route: _ApprovedLegacyBoundRoute | None = None

    def __enter__(self) -> LegacyApprovedBoundRoute:
        with self._session_factory() as read_session:
            with read_session.begin():
                prepared = self._routes.proof_issuer.prepare_server_loaded(
                    read_session,
                    self._lookup,
                    self._confirmation_input,
                )
        lease = self._routes.pending_identity_verifier_port.open_issuance_lease(
            self._write_session,
            prepared,
        )
        try:
            bound_route = _ApprovedLegacyBoundRoute(
                routes=self._routes,
                session=self._write_session,
                lease=lease,
                prepared=prepared,
                operation_port=self._operation_port,
                pending_port=self._pending_port,
                pending_identity=self._pending_identity,
                on_prepared=self._on_prepared,
                on_bound=self._on_bound,
                jd_service=self._jd_service,
                outcomes_repository=self._outcomes_repository,
            )
        except BaseException:
            self._lease = None
            self._bound_route = None
            lease.close()
            raise
        self._lease = lease
        self._bound_route = bound_route
        return bound_route

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        bound_route = self._bound_route
        self._bound_route = None
        lease = self._lease
        self._lease = None
        try:
            if bound_route is not None:
                bound_route._revoke_unclaimed_parent()
        finally:
            if lease is not None:
                lease.close()


def _attribute(value: object, name: str, default: object = None) -> object:
    try:
        return getattr(value, name)
    except Exception:
        return default


def _callable(value: object | None, names: tuple[str, ...]) -> Callable[..., object] | None:
    if value is None:
        return None
    for name in names:
        try:
            function = getattr(value, name)
        except Exception:
            continue
        if callable(function):
            return cast(Callable[..., object], function)
    return None


def _invoke(
    function: Callable[..., object], named: Mapping[str, object], positional: tuple[object, ...]
) -> object:
    """Call an injected seam once after binding a supported argument shape.

    Binding is completed before entering the callable.  Consequently a
    ``TypeError`` (or any other exception) raised by the body is its own
    failure and is never mistaken for an argument-shape mismatch or retried.
    """

    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return function(*positional)

    parameters = tuple(signature.parameters.values())

    def composed_call() -> tuple[tuple[object, ...], dict[str, object]]:
        args: list[object] = []
        kwargs: dict[str, object] = {}
        fallback_index = 0
        consumed_named: set[str] = set()
        has_var_keyword = False
        for parameter in parameters:
            if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
                args.extend(positional[fallback_index:])
                fallback_index = len(positional)
                continue
            if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                has_var_keyword = True
                continue
            if parameter.name in named:
                value = named[parameter.name]
                consumed_named.add(parameter.name)
            elif fallback_index < len(positional):
                value = positional[fallback_index]
                fallback_index += 1
            elif parameter.default is inspect.Parameter.empty:
                continue
            else:
                continue
            if parameter.kind is inspect.Parameter.KEYWORD_ONLY:
                kwargs[parameter.name] = value
            else:
                args.append(value)
        if has_var_keyword:
            for name, value in named.items():
                if name not in consumed_named:
                    kwargs[name] = value
        return tuple(args), kwargs

    def named_call() -> tuple[tuple[object, ...], dict[str, object]]:
        args: list[object] = []
        kwargs: dict[str, object] = {}
        consumed_named: set[str] = set()
        has_var_keyword = False
        for parameter in parameters:
            if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
                if parameter.name in named:
                    args.append(named[parameter.name])
                    consumed_named.add(parameter.name)
                continue
            if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                has_var_keyword = True
                continue
            if (
                parameter.kind
                in {
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                }
                and parameter.name in named
            ):
                kwargs[parameter.name] = named[parameter.name]
                consumed_named.add(parameter.name)
        if has_var_keyword:
            for name, value in named.items():
                if name not in consumed_named:
                    kwargs[name] = value
        return tuple(args), kwargs

    candidates: tuple[tuple[tuple[object, ...], dict[str, object]], ...] = (
        composed_call(),
        named_call(),
        (tuple(positional), {}),
    )
    for args, kwargs in candidates:
        try:
            signature.bind(*args, **kwargs)
        except TypeError:
            continue
        return function(*args, **kwargs)
    raise TypeError("injected callable does not accept a supported argument shape")


def _safe_args(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _confirmation_token(pending: PendingAction) -> str:
    """Keep the opaque confirmation token identical to the legacy helper."""

    try:
        decoded = json.loads(pending.args)
        canonical = json.dumps(decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (json.JSONDecodeError, TypeError, ValueError):
        canonical = pending.args
    identity = json.dumps(
        [pending.tool_call_id, pending.tool_name, canonical],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return sha256(identity.encode("utf-8")).hexdigest()


def _pending(value: object | None) -> PendingAction | None:
    if value is None:
        return None
    if isinstance(value, PendingAction):
        return value
    action = _attribute(value, "pending", None)
    if action is not None and action is not value:
        return _pending(action)
    tool_call_id = _attribute(value, "tool_call_id", None)
    tool_name = _attribute(value, "tool_name", None)
    args = _attribute(value, "args", None)
    human = _attribute(value, "human", "")
    operation_id = _attribute(value, "operation_id", "")
    if not all(
        isinstance(item, str) for item in (tool_call_id, tool_name, args, human, operation_id)
    ):
        return None
    return PendingAction(
        cast(str, tool_call_id),
        cast(str, tool_name),
        cast(str, args),
        cast(str, human),
        cast(str, operation_id),
    )


def _result_ok(value: object) -> bool:
    if type(value) is bool:
        return value
    if isinstance(value, PersistenceResult):
        return value.persisted
    return bool(_attribute(value, "persisted", False))


def _result_status(value: object) -> object:
    return _attribute(value, "status", None)


def _result_message_id(value: object) -> int | None:
    ids = _attribute(value, "message_ids", ())
    if isinstance(ids, Sequence) and not isinstance(ids, (str, bytes)) and ids:
        first = ids[0]
        if type(first) is int and first > 0:
            return first
    candidate = _attribute(value, "message_id", None)
    return candidate if type(candidate) is int and candidate > 0 else None


def _error(
    code: RuntimeFailureCode,
    message: str,
    status: int,
    *,
    retryable: bool = False,
    pending_action: PendingActionPayload | None = None,
) -> RuntimeFailureOutcome:
    return RuntimeFailureOutcome(
        code,
        message,
        status,
        retryable=retryable,
        pending_action=pending_action,
    )


def _pending_messages(pending: PendingAction, user_message: str | None = None) -> list[Message]:
    messages: list[Message] = []
    if user_message is not None:
        messages.append(Message(role="user", content=user_message))
    messages.append(
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id=pending.tool_call_id,
                    name=pending.tool_name,
                    args=pending.args,
                )
            ],
        )
    )
    return messages


def _confirmation_payload(
    pending: PendingAction,
    *,
    presentation: LegacyPendingPresentationV1,
) -> PendingActionPayload:
    return PendingActionPayload(
        tool_name=pending.tool_name,
        operation_id=pending.operation_id or pending.tool_call_id,
        human=presentation.human,
        args=freeze_json_mapping(_safe_args(pending.args)),
        confirmation_token=_confirmation_token(pending),
        editable_fields=tuple(freeze_json_mapping(item) for item in presentation.editable_fields),
        details=freeze_json_mapping(presentation.details),
    )


class DeterministicPilotAdapter:
    """Bridge the trusted Pilot actions and Legacy deterministic writes.

    The adapter deliberately accepts only server-owned repositories and the
    Legacy catalog factory.  In particular there is no provider/model/catalog
    parameter: a model failure can never fall back into this class.
    """

    __slots__ = ("dependencies",)

    def __init__(
        self,
        dependencies: DeterministicDependencies | None = None,
        **kwargs: object,
    ) -> None:
        if dependencies is not None and kwargs:
            values = {
                name: getattr(dependencies, name)
                for name in DeterministicDependencies.__dataclass_fields__
            }
            values.update(kwargs)
            dependencies = DeterministicDependencies(**cast(Any, values))
        elif dependencies is None:
            # Keep construction pleasant for composition roots while retaining
            # an explicit closed field set (unknown values, including a model
            # catalog/provider/projector, are rejected).
            valid = set(DeterministicDependencies.__dataclass_fields__)
            unknown = sorted(name for name in kwargs if name not in valid)
            if unknown:
                raise TypeError("unknown deterministic dependency: " + ", ".join(unknown))
            dependencies = DeterministicDependencies(**cast(Any, kwargs))
        self.dependencies = dependencies

    # ---- trusted route and initial action ---------------------------------

    @staticmethod
    def _conversation_id(conversation: object) -> int:
        value = _attribute(conversation, "id")
        if type(value) is not int or value <= 0:
            raise ValueError("conversation id is invalid")
        return value

    @staticmethod
    def _action_from_request(
        request: StartTurnRequest,
    ) -> PilotAction | PilotSubmissionSnapshotAction | PilotOutcomeAction | None:
        descriptor = request.pilot_action
        if descriptor is None:
            return None
        kind = descriptor.kind
        if kind not in _DETERMINISTIC_ACTION_KINDS and kind not in {
            "application_jd_save",
            "application_submission_snapshot",
            "application_outcome_record",
        }:
            raise ValueError("unsupported pilot action")
        raw: object
        try:
            raw = json.loads(descriptor.value) if descriptor.value else {}
        except json.JSONDecodeError as exc:
            raise ValueError("pilot_action must be valid JSON") from exc
        if not isinstance(raw, dict):
            raise ValueError("pilot action must be an object")
        if "type" not in raw:
            type_value = {
                "application_jd_save": "application_jd_save",
                "application_submission_snapshot": "application_submission_snapshot",
                "application_outcome_record": "application_outcome_record",
            }[kind]
            raw = {"type": type_value, **raw}
        # The normalized DTO may carry API-style camelCase values.  The parser
        # remains the single validation authority and has no side effects.
        return parse_pilot_action(raw)

    def _run_initial_route(
        self,
        issuer: LegacyInitialRouteIssuer | None,
        *,
        conversation_id: int,
        pending: PendingAction,
        clarification: bool = False,
        body: Callable[
            [PendingPersistenceRouteHandle, LegacyPendingPresentationV1 | None],
            _InitialRouteResult,
        ],
    ) -> _InitialRouteResult:
        owner_factory = self.dependencies.legacy_request_owner_lease_factory
        port = self.dependencies.legacy_initial_route_port
        if type(owner_factory) is not RuntimeRequestOwnerLeaseFactory:
            raise TypeError("deterministic entry requires the exact Legacy request owner factory")
        if type(port) is not LegacyInitialRoutePort:
            raise TypeError("deterministic entry requires the exact Legacy initial route Port")
        if type(issuer) is not LegacyInitialRouteIssuer:
            raise TypeError("deterministic entry requires its exact source-bound Legacy issuer")
        with owner_factory.open() as owner:
            with issuer.open_request_lease(owner) as request_lease:
                token = issuer.issue(request_lease)
                handle = port.resolve_initial(token)
                binding = port.require_route(handle)
                if binding.name != pending.tool_name:
                    raise ValueError("Legacy initial route resolved the wrong Adapter")
                presentation = None
                if not clarification:
                    with self._legacy_read_context() as (_read_session, read_context):
                        presentation = port.project_pending(
                            handle,
                            encoded_args=pending.args,
                            context=read_context,
                        )
                operation_port = self.dependencies.operation_port
                pending_port = self.dependencies.pending_persistence_route_port
                if type(operation_port) is not ToolOperationMetadataPort:
                    raise TypeError("deterministic entry requires the exact operation Port")
                if type(pending_port) is not PendingPersistenceRoutePort:
                    raise TypeError("deterministic entry requires the exact Pending route Port")
                digest, revision = pending_action_identity(
                    pending.tool_call_id,
                    pending.tool_name,
                    pending.args,
                )
                pending_identity = PendingRouteIdentityV1(
                    conversation_id=conversation_id,
                    operation_id="" if clarification else pending.operation_id,
                    tool_call_id=pending.tool_call_id,
                    tool_name="" if clarification else pending.tool_name,
                    pending_action_revision=revision,
                    pending_confirmation_claim_id=("" if clarification else pending.operation_id),
                    arguments_digest=digest,
                )
                persistence_handle: PendingPersistenceRouteHandle | None = None
                operation_handle = None
                try:
                    if clarification:
                        persistence_handle = pending_port.bind_clarification_pending(
                            pending_identity
                        )
                    else:
                        operation_handle = operation_port.bind_legacy(
                            handle,
                            OperationRouteIdentityV1(
                                operation_id=pending.operation_id,
                                tool_call_id=pending.tool_call_id,
                                revision=revision,
                                arguments_digest=digest,
                            ),
                        )
                        persistence_handle = pending_port.bind_legacy_pending(
                            operation_handle,
                            pending_identity,
                        )
                    return body(persistence_handle, presentation)
                finally:
                    if persistence_handle is not None:
                        pending_port.revoke_pending(persistence_handle)
                    if operation_handle is not None:
                        operation_port.revoke_legacy(operation_handle)

    @contextmanager
    def _legacy_read_context(self) -> Iterator[tuple[Session, LegacyReadContext]]:
        coordinator = self.dependencies.write_coordinator
        repository = getattr(coordinator, "repository", None)
        session_factory = getattr(repository, "session_factory", None)
        if not callable(session_factory):
            raise TypeError("Legacy presentation requires the exact write Session factory")
        with session_factory() as session:
            with session.begin():
                yield (
                    session,
                    LegacyReadContext(
                        session,
                        ApplicationsRepository(session_factory),
                        ApplicationJDService(session_factory),
                    ),
                )

    def matches(self, request: StartTurnRequest, conversation: object) -> bool:
        if request.pilot_action is not None:
            self._action_from_request(request)
            return True
        clarification = self._clarification_for(conversation)
        if (
            clarification is not None
            and clarification[0].tool_name == "save_application_jd_version"
        ):
            return True
        application = self._application(conversation, missing_ok=True)
        if application is None:
            return (
                decide_pilot_action(
                    request.message,
                    has_current_jd=False,
                    collecting_jd=False,
                ).kind
                != "normal_agent"
            )
        current = self._current_jd(application)
        return (
            decide_pilot_action(
                request.message,
                has_current_jd=current is not None,
                collecting_jd=False,
            ).kind
            != "normal_agent"
        )

    def pending_action(self, conversation: object) -> PendingAction | None:
        """Return the detached trusted pending action for Journal orchestration."""

        return self._pending_for(conversation)

    def validate_new_request(self, request: StartTurnRequest) -> None:
        """Validate deterministic intent before a new Conversation is created."""

        action = self._action_from_request(request)
        if action is None:
            decision = decide_pilot_action(
                request.message,
                has_current_jd=False,
                collecting_jd=False,
            )
            if decision.kind == "normal_agent":
                return
        if request.context_type != "application":
            raise ValueError("application context is required for saving a JD")
        try:
            application_id = int(request.context_ref)
        except (TypeError, ValueError) as exc:
            raise ValueError("application context is invalid") from exc
        getter = _callable(self.dependencies.applications, ("get", "find"))
        application = (
            _invoke(
                getter, {"application_id": application_id, "id": application_id}, (application_id,)
            )
            if getter is not None
            else None
        )
        if application is None:
            raise LookupError("application not found")

    def validate_action(self, request: StartTurnRequest | ConfirmationRequest) -> None:
        """Validate a closed client action without reading domain state.

        Confirmation requests have no client Pilot action descriptor; their
        token/edit/CAS validation belongs to :meth:`confirm` after the trusted
        conversation is loaded.
        """

        if isinstance(request, ConfirmationRequest):
            if not request.approved and not request.edited_args.is_missing():
                raise ValueError("edited_args is only allowed when approved is true")
            if request.approved and request.rejection_feedback_present:
                raise ValueError("rejection_feedback is only allowed when approved is false")
            return
        self._action_from_request(request)

    def is_terminal_replay(self, request: ConfirmationRequest) -> bool:
        """Return whether a confirmation addresses an already-terminal Ledger row.

        This is intentionally Ledger-only.  The Runtime uses it to avoid
        reading a now-cleared Pending card before entering the replay path.
        """

        operations = self.dependencies.write_operations
        operation_id = request.operation_id
        if operations is None or not isinstance(operation_id, str) or not operation_id:
            return False
        operation = _callable(operations, ("get",))
        if operation is None:
            return False
        value = _invoke(
            operation, {"operation_id": operation_id, "id": operation_id}, (operation_id,)
        )
        status = _attribute(value, "status", None)
        return value is not None and status is not None and str(status) != "proposed"

    def preflight_confirmation(
        self,
        request: ConfirmationRequest,
        *,
        preheader: LedgerOperationPreheader | None = None,
        transport: RuntimeTransportContext | None = None,
    ) -> _DeterministicConfirmationPreflight:
        """Classify and replay one approved Legacy operation without Conversation state."""

        if not isinstance(request, ConfirmationRequest):
            raise TypeError("request must be a ConfirmationRequest")
        if not request.approved:
            return _DeterministicConfirmationPreflight(None, False)
        if type(preheader) is not LedgerOperationPreheader:
            raise TypeError("preheader must be an exact LedgerOperationPreheader")
        operation = _attribute(preheader, "operation", None)
        adapter_kind = str(_attribute(operation, "adapter_kind", "") or "")
        if adapter_kind != "legacy_deterministic":
            return _DeterministicConfirmationPreflight(preheader, False)
        tool_name = str(_attribute(operation, "tool_name", "") or "")
        route_identity = (
            request.conversation_id,
            str(_attribute(operation, "id", "") or ""),
            str(_attribute(operation, "tool_call_id", "") or ""),
            tool_name,
        )
        if tool_name not in LEGACY_DETERMINISTIC_NAMES:
            return _DeterministicConfirmationPreflight(
                preheader,
                True,
                DeterministicExecution(
                    self._write_error(WriteOperationError("operation_identity_conflict")),
                    preparation_kind=PreparationKind.REPLAY,
                ),
                route_identity,
            )
        pointer = _attribute(preheader, "pending_pointer", None)
        proposed = str(_attribute(operation, "status", "") or "") == "proposed"
        proposed_identity_matches = bool(
            not proposed
            or (
                pointer is not None
                and _attribute(pointer, "conversation_id", None) == request.conversation_id
                and str(_attribute(pointer, "operation_id", "") or "")
                == str(_attribute(operation, "id", "") or "")
                and str(_attribute(pointer, "tool_call_id", "") or "")
                == str(_attribute(operation, "tool_call_id", "") or "")
                and str(_attribute(pointer, "tool_name", "") or "") == tool_name
            )
        )
        if not proposed_identity_matches:
            return _DeterministicConfirmationPreflight(
                preheader,
                True,
                DeterministicExecution(
                    self._write_error(WriteOperationError("operation_integrity_error")),
                    preparation_kind=PreparationKind.REPLAY,
                ),
                route_identity,
            )
        execution = self._terminal_replay(
            request,
            request.conversation_id,
            transport=transport,
            operation=operation,
        )
        return _DeterministicConfirmationPreflight(
            preheader,
            True,
            execution,
            route_identity,
        )

    def start_turn(
        self,
        request: StartTurnRequest,
        conversation: object,
        *,
        transport: RuntimeTransportContext | None = None,
        event_sink: object | None = None,
        on_user_message_persisted: Callable[[int], object] | None = None,
    ) -> DeterministicExecution:
        del event_sink  # sync transport intentionally has no SSE prefix
        conversation_id = self._conversation_id(conversation)
        action = self._action_from_request(request)
        existing = self._pending_for(conversation)
        if existing is not None and existing.tool_name in LEGACY_DETERMINISTIC_NAMES:
            execution = self._confirmation_required(
                existing,
                conversation_id,
                presentation=self._persisted_legacy_presentation(
                    existing,
                    conversation_id,
                ),
                pending_replay=True,
            )
            return self._with_transport_initial(execution, transport)

        application = self._application(conversation)
        current_jd = self._current_jd(application)
        clarification_view = self._clarification_for(conversation)
        clarification_pending = clarification_view[0] if clarification_view is not None else None
        collecting = (
            clarification_pending is not None
            and clarification_pending.tool_name == "save_application_jd_version"
        )

        if isinstance(action, (PilotSubmissionSnapshotAction, PilotOutcomeAction)):
            if existing is not None:
                return self._confirmation_required(
                    existing,
                    conversation_id,
                    presentation=self._historical_pending_presentation(existing),
                    pending_replay=True,
                )
            pending = (
                build_submission_snapshot_pending_action(
                    application_id=self._application_id(application),
                    action=action,
                    id_factory=self.dependencies.id_factory,
                    key_factory=self.dependencies.key_factory,
                )
                if isinstance(action, PilotSubmissionSnapshotAction)
                else build_outcome_pending_action(
                    application_id=self._application_id(application),
                    action=action,
                    id_factory=self.dependencies.id_factory,
                    key_factory=self.dependencies.key_factory,
                )
            )
            issuer = (
                self.dependencies.legacy_submission_snapshot_issuer
                if isinstance(action, PilotSubmissionSnapshotAction)
                else self.dependencies.legacy_outcome_recording_issuer
            )
            return self._run_initial_route(
                issuer,
                conversation_id=conversation_id,
                pending=pending,
                body=lambda route_handle, presentation: self._persist_pending(
                    conversation_id,
                    request.message,
                    pending,
                    route_handle=route_handle,
                    presentation=presentation,
                    on_user_message_persisted=on_user_message_persisted,
                ),
            )

        if action is not None and action.jd_text is None:
            decision = PilotActionDecision(
                kind="collecting_jd",
                question="请粘贴完整岗位描述",
                source_url=action.source_url,
            )
        else:
            decision = decide_pilot_action(
                request.message,
                has_current_jd=current_jd is not None,
                collecting_jd=collecting,
            )
            if action is not None and action.jd_text is not None:
                decision = PilotActionDecision(
                    kind="pending_confirmation",
                    jd_text=action.jd_text,
                    source_url=action.source_url,
                )

        if existing is not None:
            if existing.tool_name == "save_application_jd_version":
                return self._confirmation_required(
                    existing,
                    conversation_id,
                    presentation=self._historical_pending_presentation(existing),
                    pending_replay=True,
                )
            return DeterministicExecution(
                _error(
                    RuntimeFailureCode.PENDING_CONFIRMATION_REQUIRED,
                    "请先处理当前待确认操作",
                    409,
                )
            )

        if decision.kind == "cancelled":
            return self._persist_cancelled(
                conversation_id,
                request.message,
                on_user_message_persisted=on_user_message_persisted,
            )
        if decision.kind == "collecting_jd":
            pending = build_pilot_pending_action(
                application_id=self._application_id(application),
                current_version_id=(
                    cast(int, _attribute(current_jd, "id"))
                    if current_jd is not None and type(_attribute(current_jd, "id")) is int
                    else None
                ),
                jd_text="",
                source_url=decision.source_url,
                id_factory=self.dependencies.id_factory,
                key_factory=self.dependencies.key_factory,
            )
            return self._run_initial_route(
                self.dependencies.legacy_jd_clarification_issuer,
                conversation_id=conversation_id,
                pending=pending,
                clarification=True,
                body=lambda route_handle, _presentation: self._persist_clarification(
                    conversation_id,
                    request.message,
                    pending,
                    decision.question,
                    route_handle=route_handle,
                    on_user_message_persisted=on_user_message_persisted,
                ),
            )
        if (
            decision.kind != "pending_confirmation"
            or not isinstance(decision.jd_text, str)
            or not decision.jd_text.strip()
        ):
            # A caller that selected deterministic for a normal message has an
            # invalid trusted route; it must not silently invoke the model.
            return DeterministicExecution(
                _error(RuntimeFailureCode.OPERATION_UNAVAILABLE, "unsupported runtime route", 400)
            )

        id_factory = self.dependencies.id_factory
        key_factory = self.dependencies.key_factory
        source_url = decision.source_url
        if clarification_pending is not None:
            previous_args = _safe_args(clarification_pending.args)
            previous_key = previous_args.get("idempotency_key")
            previous_url = previous_args.get("source_url")
            if isinstance(previous_key, str):

                def previous_key_factory(previous_key: str = previous_key) -> str:
                    return previous_key

                def previous_id_factory(call_id: str = clarification_pending.tool_call_id) -> str:
                    return call_id

                key_factory = previous_key_factory
                id_factory = previous_id_factory
            if source_url is None and isinstance(previous_url, str):
                source_url = previous_url
        pending = build_pilot_pending_action(
            application_id=self._application_id(application),
            current_version_id=(
                cast(int, _attribute(current_jd, "id"))
                if current_jd is not None and type(_attribute(current_jd, "id")) is int
                else None
            ),
            jd_text=decision.jd_text,
            source_url=source_url,
            id_factory=id_factory,
            key_factory=key_factory,
        )
        issuer = (
            self.dependencies.legacy_jd_clarification_issuer
            if clarification_pending is not None
            else self.dependencies.legacy_jd_deterministic_action_issuer
        )
        return self._run_initial_route(
            issuer,
            conversation_id=conversation_id,
            pending=pending,
            body=lambda route_handle, presentation: self._persist_pending(
                conversation_id,
                request.message,
                pending,
                route_handle=route_handle,
                presentation=presentation,
                on_user_message_persisted=on_user_message_persisted,
            ),
        )

    # Common spelling used by composition roots during the extraction.
    execute_initial = start_turn

    def prepare_stream(
        self,
        request: StartTurnRequest,
        conversation: object,
        *,
        transport: RuntimeTransportContext | None = None,
        on_user_message_persisted: Callable[[int], object] | None = None,
    ) -> DeterministicExecution:
        return self.start_turn(
            request,
            conversation,
            transport=transport,
            on_user_message_persisted=on_user_message_persisted,
        )

    # ---- deterministic confirmation --------------------------------------

    def confirm(
        self,
        request: ConfirmationRequest,
        conversation: object,
        *,
        transport: RuntimeTransportContext | None = None,
        on_confirmation_attempt: Callable[[PendingAction, bool], object] | None = None,
        on_confirmation_bound: Callable[[object], object] | None = None,
        on_tool_result: Callable[[PendingAction, str, bool], object] | None = None,
        preflight: _DeterministicConfirmationPreflight | None = None,
    ) -> DeterministicExecution:
        conversation_id = self._conversation_id(conversation)
        if not request.approved and not request.edited_args.is_missing():
            return DeterministicExecution(
                _error(
                    RuntimeFailureCode.INVALID_CONFIRMATION,
                    "edited_args is only allowed when approved is true",
                    422,
                ),
                preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
            )
        if request.approved and request.rejection_feedback_present:
            return DeterministicExecution(
                _error(
                    RuntimeFailureCode.INVALID_CONFIRMATION,
                    "rejection_feedback is only allowed when approved is false",
                    422,
                ),
                preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
            )
        if preflight is not None:
            if type(preflight) is not _DeterministicConfirmationPreflight:
                raise TypeError("preflight must be an exact deterministic confirmation preflight")
            if not preflight.matched:
                raise ValueError("preflight does not authorize the Legacy route")
            terminal = preflight.execution
            if terminal is None:
                terminal = self._fresh_terminal_replay(
                    request,
                    conversation_id,
                    preflight=preflight,
                    transport=transport,
                )
        else:
            terminal = self._terminal_replay(
                request,
                conversation_id,
                transport=transport,
            )
        if terminal is not None:
            return terminal
        pending = self._pending_for(conversation)
        if pending is None or pending.tool_name not in LEGACY_DETERMINISTIC_NAMES:
            return DeterministicExecution(
                _error(
                    RuntimeFailureCode.STALE_PENDING_ACTION, "待确认操作已过期，请刷新后重试。", 409
                ),
                preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
            )
        if preflight is not None:
            pending_route_identity = (
                conversation_id,
                pending.operation_id,
                pending.tool_call_id,
                pending.tool_name,
            )
            if pending_route_identity != preflight.route_identity:
                return DeterministicExecution(
                    self._write_error(WriteOperationError("operation_identity_conflict")),
                    preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
                )
        if request.operation_id is not None and request.operation_id != pending.operation_id:
            return DeterministicExecution(
                _error(
                    RuntimeFailureCode.OPERATION_IDENTITY_CONFLICT,
                    "operation identity conflict",
                    409,
                ),
                preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
            )
        expected_token = _confirmation_token(pending)
        token = request.confirmation_token or expected_token
        if not compare_digest(token, expected_token):
            return DeterministicExecution(
                _error(
                    RuntimeFailureCode.STALE_PENDING_ACTION,
                    "待确认操作已被更新，请刷新后重试。",
                    409,
                ),
                preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
            )
        if not request.confirmation_token and (
            not request.edited_args.is_missing() or request.rejection_feedback
        ):
            return DeterministicExecution(
                _error(
                    RuntimeFailureCode.INVALID_CONFIRMATION,
                    "confirmation_token is required when changing confirmation details",
                    422,
                ),
                preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
            )

        operations = self.dependencies.write_operations
        coordinator = self.dependencies.write_coordinator
        if operations is None or coordinator is None:
            return DeterministicExecution(
                _error(RuntimeFailureCode.OPERATION_UNAVAILABLE, "写入账本暂不可用。", 503),
                preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
            )
        try:
            fingerprint = self._request_fingerprint(
                pending,
                request,
                token,
            )
        except WriteOperationError as exc:
            return DeterministicExecution(
                self._write_error(exc),
                preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
            )

        if request.approved:
            projected_pending: list[PendingAction] = []

            def accept_prepared_input(projected: PreparedLegacyInputV1) -> None:
                if projected_pending:
                    raise WriteOperationError("operation_not_committed", retryable=True)
                effective_pending = PendingAction(
                    pending.tool_call_id,
                    pending.tool_name,
                    projected.encoded_args,
                    projected.confirmation_human,
                    pending.operation_id,
                )
                projected_pending.append(effective_pending)
                if on_confirmation_attempt is not None:
                    on_confirmation_attempt(effective_pending, True)

            execution = cast(Any, coordinator).execute_legacy(
                operation_id=pending.operation_id,
                conversation_id=conversation_id,
                tool_call_id=pending.tool_call_id,
                tool_name=pending.tool_name,
                request_fingerprint=fingerprint,
                route_binder=self._approved_route_binder(
                    conversation_id=conversation_id,
                    pending=pending,
                    request=request,
                    confirmation_token=token,
                    on_prepared=accept_prepared_input,
                    on_bound=on_confirmation_bound,
                ),
            )
            if isinstance(execution, OperationUnknown):
                return DeterministicExecution(
                    self._write_unknown(execution),
                    preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
                )
            if isinstance(execution, OperationReplay):
                return self._replay_execution(
                    conversation_id, execution, fingerprint, transport=transport
                )
            if not isinstance(execution, (OperationCommitted, OperationFailed)):
                return DeterministicExecution(
                    _error(
                        RuntimeFailureCode.OPERATION_RESULT_UNKNOWN,
                        "写入结果暂时无法确认，请保留确认卡后重试。",
                        503,
                        retryable=True,
                    ),
                    preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
                )
            if len(projected_pending) != 1:
                return DeterministicExecution(
                    _error(
                        RuntimeFailureCode.OPERATION_RESULT_UNKNOWN,
                        "写入结果暂时无法确认，请保留确认卡后重试。",
                        503,
                        retryable=True,
                    ),
                    preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
                )
            effective_pending = projected_pending[0]
            result = execution.payload.visible_result
            succeeded = execution.payload.status == "committed"
            if on_tool_result is not None:
                on_tool_result(effective_pending, result, succeeded)
            origin = Message(
                role="tool", content=result, tool_call_id=effective_pending.tool_call_id
            )
            if not succeeded:
                return self._persist_failure(
                    conversation_id,
                    pending,
                    origin,
                    execution,
                    effective_pending,
                    transport=transport,
                )
            response = self._deliver_terminal(
                conversation_id,
                pending,
                origin,
                execution,
                "岗位资料已保存。",
                "success",
            )
            return self._with_transport_confirmation(response, transport)

        if on_confirmation_attempt is not None:
            on_confirmation_attempt(pending, False)
        rejection = cast(Any, coordinator).reject_primary(
            operation_id=pending.operation_id,
            conversation_id=conversation_id,
            tool_call_id=pending.tool_call_id,
            tool_name=pending.tool_name,
            request_fingerprint=fingerprint,
            visible_result=_CANCELLED_TOOL_RESULT,
        )
        if isinstance(rejection, OperationUnknown):
            return DeterministicExecution(
                self._write_unknown(rejection),
                preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
            )
        if isinstance(rejection, OperationReplay):
            return self._replay_execution(
                conversation_id, rejection, fingerprint, transport=transport
            )
        if not isinstance(rejection, (OperationCommitted, OperationFailed)):
            return DeterministicExecution(
                _error(
                    RuntimeFailureCode.OPERATION_RESULT_UNKNOWN,
                    "写入结果暂时无法确认，请保留确认卡后重试。",
                    503,
                    retryable=True,
                ),
                preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
            )
        origin = Message(
            role="tool", content=_CANCELLED_TOOL_RESULT, tool_call_id=pending.tool_call_id
        )
        response = self._deliver_terminal(
            conversation_id,
            pending,
            origin,
            rejection,
            "已取消保存岗位资料。",
            "cancelled",
            undo=None,
        )
        return self._with_transport_confirmation(response, transport)

    execute_confirmation = confirm
    continue_confirmation = confirm

    def _fresh_terminal_replay(
        self,
        request: ConfirmationRequest,
        conversation_id: int,
        *,
        preflight: _DeterministicConfirmationPreflight,
        transport: RuntimeTransportContext | None,
    ) -> DeterministicExecution | None:
        """Close the proposed-to-terminal race with one authoritative fresh read."""

        operations = self.dependencies.write_operations
        getter = _callable(operations, ("get",))
        route_identity = preflight.route_identity
        operation_id = request.operation_id or (route_identity[1] if route_identity else "")
        if getter is None or not isinstance(operation_id, str) or not operation_id:
            return DeterministicExecution(
                self._write_error(WriteOperationError("operation_unavailable", retryable=True)),
                preparation_kind=PreparationKind.REPLAY,
            )
        operation = _invoke(
            getter,
            {"operation_id": operation_id, "id": operation_id},
            (operation_id,),
        )
        if operation is None or route_identity is None:
            return DeterministicExecution(
                self._write_error(WriteOperationError("operation_unavailable", retryable=True)),
                preparation_kind=PreparationKind.REPLAY,
            )
        current_identity = (
            _attribute(operation, "conversation_id", None),
            str(_attribute(operation, "id", "") or ""),
            str(_attribute(operation, "tool_call_id", "") or ""),
            str(_attribute(operation, "tool_name", "") or ""),
        )
        if (
            current_identity != route_identity
            or str(_attribute(operation, "adapter_kind", "") or "") != "legacy_deterministic"
            or current_identity[3] not in LEGACY_DETERMINISTIC_NAMES
        ):
            return DeterministicExecution(
                self._write_error(WriteOperationError("operation_identity_conflict")),
                preparation_kind=PreparationKind.REPLAY,
            )
        return self._terminal_replay(
            request,
            conversation_id,
            transport=transport,
            operation=operation,
        )

    def _terminal_replay(
        self,
        request: ConfirmationRequest,
        conversation_id: int,
        *,
        transport: RuntimeTransportContext | None,
        operation: object | None = None,
    ) -> DeterministicExecution | None:
        operations = self.dependencies.write_operations
        operation_id = request.operation_id or (
            str(_attribute(operation, "id", "") or "") if operation is not None else ""
        )
        if operations is None or not isinstance(operation_id, str) or not operation_id:
            return None
        if operation is None:
            getter = _callable(operations, ("get",))
            if getter is None:
                return None
            operation = _invoke(
                getter,
                {"operation_id": operation_id, "id": operation_id},
                (operation_id,),
            )
        status = _attribute(operation, "status", None)
        if operation is None or status is None or str(status) == "proposed":
            return None
        if (
            _attribute(operation, "conversation_id", None) != conversation_id
            or not request.confirmation_token
        ):
            return DeterministicExecution(
                _error(
                    RuntimeFailureCode.OPERATION_IDENTITY_CONFLICT,
                    "operation identity conflict",
                    409,
                ),
                preparation_kind=PreparationKind.REPLAY,
            )
        synthetic = PendingAction(
            str(_attribute(operation, "tool_call_id", "") or ""),
            str(_attribute(operation, "tool_name", "") or ""),
            "",
            str(_attribute(operation, "tool_name", "") or ""),
            str(_attribute(operation, "id", operation_id) or operation_id),
        )
        try:
            request_fingerprint = self._request_fingerprint_for_operation(
                operation,
                synthetic,
                request,
                request.confirmation_token,
            )
            replay = cast(Any, operations).replay(operation, request_fingerprint)
            if not isinstance(replay, OperationReplay):
                return DeterministicExecution(
                    _error(
                        RuntimeFailureCode.OPERATION_RESULT_UNKNOWN,
                        "写入结果暂时无法确认，请保留确认卡后重试。",
                        503,
                        retryable=True,
                    ),
                    preparation_kind=PreparationKind.REPLAY,
                )
            return self._replay_execution(
                conversation_id,
                replay,
                request_fingerprint,
                transport=transport,
            )
        except WriteOperationError as exc:
            return DeterministicExecution(
                self._write_error(exc),
                preparation_kind=PreparationKind.REPLAY,
            )

    # ---- persistence/read side -------------------------------------------

    def _pending_for(self, conversation: object) -> PendingAction | None:
        value = self.dependencies.persistence.get_pending_action(
            self._conversation_id(conversation)
        )
        return _pending(value)

    def _clarification_for(self, conversation: object) -> tuple[PendingAction, str] | None:
        value = self.dependencies.persistence.get_pending_clarification(
            self._conversation_id(conversation)
        )
        if value is None:
            return None
        if isinstance(value, tuple) and len(value) == 2:
            pending = _pending(value[0])
            return (pending, str(value[1])) if pending is not None else None
        pending = _pending(_attribute(value, "pending", value))
        question = _attribute(value, "question", "")
        return (pending, question) if pending is not None and isinstance(question, str) else None

    def _application(self, conversation: object, *, missing_ok: bool = False) -> object | None:
        if _attribute(conversation, "context_type", "") != "application":
            if missing_ok:
                return None
            raise ValueError("application context is required for saving a JD")
        try:
            application_id = int(str(_attribute(conversation, "context_ref", "")))
        except (TypeError, ValueError) as exc:
            if missing_ok:
                return None
            raise ValueError("application context is invalid") from exc
        getter = _callable(self.dependencies.applications, ("get", "find"))
        application = (
            _invoke(
                getter, {"application_id": application_id, "id": application_id}, (application_id,)
            )
            if getter is not None
            else None
        )
        if application is None and not missing_ok:
            raise LookupError("application not found")
        return application

    @staticmethod
    def _application_id(application: object) -> int:
        value = _attribute(application, "id")
        if type(value) is not int or value <= 0:
            raise ValueError("application id is invalid")
        return value

    def _current_jd(self, application: object) -> object | None:
        getter = _callable(self.dependencies.application_jd_versions, ("get_current", "current"))
        return (
            _invoke(
                getter,
                {
                    "application_id": self._application_id(application),
                    "id": self._application_id(application),
                },
                (self._application_id(application),),
            )
            if getter is not None
            else None
        )

    def _persist_pending(
        self,
        conversation_id: int,
        user_message: str,
        pending: PendingAction,
        *,
        route_handle: object,
        presentation: LegacyPendingPresentationV1 | None,
        on_user_message_persisted: Callable[[int], object] | None,
    ) -> DeterministicExecution:
        if type(presentation) is not LegacyPendingPresentationV1:
            raise TypeError("Legacy Pending persistence requires exact presentation")
        pending = PendingAction(
            pending.tool_call_id,
            pending.tool_name,
            pending.args,
            presentation.human,
            pending.operation_id,
        )
        user_result = self.dependencies.persistence.persist_initial_user_message(
            conversation_id,
            user_message,
        )
        if not _result_ok(user_result):
            return DeterministicExecution(self._persistence_error(user_result))
        message_id = _result_message_id(user_result)
        if on_user_message_persisted is not None and message_id is not None:
            on_user_message_persisted(message_id)
        result = self.dependencies.persistence.persist_initial_pending(
            conversation_id,
            _pending_messages(pending),
            pending,
            route_handle=route_handle,
        )
        if not _result_ok(result):
            return DeterministicExecution(self._persistence_error(result))
        return self._confirmation_required(
            pending,
            conversation_id,
            presentation=presentation,
        )

    def _persist_clarification(
        self,
        conversation_id: int,
        user_message: str,
        pending: PendingAction,
        question: str,
        *,
        route_handle: object,
        on_user_message_persisted: Callable[[int], object] | None,
    ) -> DeterministicExecution:
        result = self.dependencies.persistence.persist_clarification(
            conversation_id,
            [Message(role="user", content=user_message)],
            pending,
            question,
            route_handle=route_handle,
        )
        if not _result_ok(result):
            return DeterministicExecution(self._persistence_error(result))
        message_id = _result_message_id(result)
        if on_user_message_persisted is not None and message_id is not None:
            on_user_message_persisted(message_id)
        outcome = MessageOutcome(question, conversation_id=conversation_id)
        return DeterministicExecution(
            outcome,
            events=(
                MetaEvent(supports_delta=False, supports_tool_events=False),
                UserMessageSavedEvent(),
                StatusEvent(phase="collecting_jd", label="等待岗位描述"),
                AssistantMessageEvent(message=question),
            ),
        )

    def _persist_cancelled(
        self,
        conversation_id: int,
        user_message: str,
        *,
        on_user_message_persisted: Callable[[int], object] | None,
    ) -> DeterministicExecution:
        user_result = self.dependencies.persistence.persist_initial_user_message(
            conversation_id, user_message
        )
        if not _result_ok(user_result):
            return DeterministicExecution(self._persistence_error(user_result))
        message_id = _result_message_id(user_result)
        if on_user_message_persisted is not None and message_id is not None:
            on_user_message_persisted(message_id)
        cleared = self.dependencies.persistence.clear_pending_clarification(conversation_id)
        if not _result_ok(cleared):
            return DeterministicExecution(self._persistence_error(cleared))
        assistant = self.dependencies.persistence.persist_assistant_message(
            conversation_id, "已取消保存岗位资料。"
        )
        if not _result_ok(assistant):
            return DeterministicExecution(self._persistence_error(assistant))
        outcome = MessageOutcome("已取消保存岗位资料。", conversation_id=conversation_id)
        return DeterministicExecution(
            outcome,
            events=(
                MetaEvent(supports_delta=False, supports_tool_events=False),
                UserMessageSavedEvent(),
                StatusEvent(phase="collecting_jd", label="等待岗位描述"),
                AssistantMessageEvent(message=outcome.message),
            ),
        )

    def _persistence_error(self, value: object) -> RuntimeFailureOutcome:
        status = _result_status(value)
        if status is PersistenceStatus.CLOSED or str(status) in {"closed", "archived"}:
            return _error(RuntimeFailureCode.CONVERSATION_ARCHIVED, "conversation is archived", 409)
        if status is PersistenceStatus.NOT_FOUND or str(status) == "not_found":
            return _error(RuntimeFailureCode.APPLICATION_NOT_FOUND, "conversation not found", 404)
        return _error(
            RuntimeFailureCode.OPERATION_FAILED, "对话当前不可写入。", 503, retryable=True
        )

    def _confirmation_required(
        self,
        pending: PendingAction,
        conversation_id: int,
        *,
        presentation: LegacyPendingPresentationV1,
        operation_id: str | None = None,
        replayed: bool = False,
        pending_replay: bool | None = None,
    ) -> DeterministicExecution:
        payload = _confirmation_payload(pending, presentation=presentation)
        outcome = ConfirmationRequiredOutcome(
            confirmation_token=payload.confirmation_token,
            conversation_id=conversation_id,
            operation_id=operation_id,
            pending_action=payload,
            replayed=replayed,
        )
        return DeterministicExecution(
            outcome,
            events=(
                MetaEvent(supports_delta=False, supports_tool_events=False),
                UserMessageSavedEvent(),
                StatusEvent(phase="waiting_confirmation", label="需要确认"),
                ConfirmationRequiredEvent(
                    confirmation_token=payload.confirmation_token,
                    pending_action=payload,
                ),
            ),
            pending_replay=replayed if pending_replay is None else pending_replay,
        )

    @staticmethod
    def _historical_pending_presentation(
        pending: PendingAction,
    ) -> LegacyPendingPresentationV1:
        """Render immutable historical delivery data without live metadata lookup."""

        return LegacyPendingPresentationV1(
            human=pending.human,
            editable_fields=(),
            details={},
        )

    def _persisted_legacy_presentation(
        self,
        pending: PendingAction,
        conversation_id: int,
    ) -> LegacyPendingPresentationV1:
        routes = self.dependencies.legacy_confirmation_routes
        if type(routes) is not LegacyConfirmationRouteComponents:
            raise TypeError("Legacy Pending replay requires exact confirmation routes")
        port = routes.persisted_presentation_port
        if type(port) is not LegacyPersistedPresentationPort:
            raise TypeError("Legacy Pending replay requires exact presentation Port")
        lookup = LegacyConfirmationLookupIdentity(conversation_id=conversation_id)
        confirmation_input = LegacyApprovedConfirmationInput(
            decision="approved",
            operation_id=pending.operation_id,
            confirmation_token=_confirmation_token(pending),
            edited_args_present=False,
            edited_args=None,
            rejection_feedback_present=False,
            rejection_feedback="",
        )
        coordinator = self.dependencies.write_coordinator
        session_factory = getattr(
            getattr(coordinator, "repository", None),
            "session_factory",
            None,
        )
        if not callable(session_factory):
            raise TypeError("Legacy Pending replay requires the exact write Session factory")
        with session_factory() as identity_session:
            with identity_session.begin():
                with self._legacy_read_context() as (_read_session, read_context):
                    return port.project_pending(
                        identity_session,
                        lookup,
                        confirmation_input,
                        read_context,
                    )

    def _with_transport_initial(
        self,
        execution: DeterministicExecution,
        transport: RuntimeTransportContext | None,
    ) -> DeterministicExecution:
        del transport
        return execution

    def _with_transport_confirmation(
        self,
        execution: DeterministicExecution,
        transport: RuntimeTransportContext | None,
    ) -> DeterministicExecution:
        del transport
        if isinstance(execution.outcome, (RuntimeFailureOutcome, OperationPendingOutcome)):
            return execution
        return execution

    # ---- Legacy write/ledger ---------------------------------------------

    def _approved_route_binder(
        self,
        *,
        conversation_id: int,
        pending: PendingAction,
        request: ConfirmationRequest,
        confirmation_token: str,
        on_prepared: Callable[[PreparedLegacyInputV1], object],
        on_bound: Callable[[object], object] | None,
    ) -> LegacyApprovedRouteBinder:
        routes = self.dependencies.legacy_confirmation_routes
        coordinator = self.dependencies.write_coordinator
        if type(routes) is not LegacyConfirmationRouteComponents or coordinator is None:
            raise WriteOperationError("operation_unavailable")
        operation_port = self.dependencies.operation_port
        pending_port = self.dependencies.pending_persistence_route_port
        if (
            type(operation_port) is not ToolOperationMetadataPort
            or type(pending_port) is not PendingPersistenceRoutePort
        ):
            raise WriteOperationError("operation_unavailable")
        arguments_digest, revision = pending_action_identity(
            pending.tool_call_id,
            pending.tool_name,
            pending.args,
        )
        pending_identity = PendingRouteIdentityV1(
            conversation_id=conversation_id,
            operation_id=pending.operation_id,
            tool_call_id=pending.tool_call_id,
            tool_name=pending.tool_name,
            pending_action_revision=revision,
            pending_confirmation_claim_id=pending.operation_id,
            arguments_digest=arguments_digest,
        )
        lookup = LegacyConfirmationLookupIdentity(conversation_id=conversation_id)
        confirmation_input = LegacyApprovedConfirmationInput(
            decision="approved",
            operation_id=pending.operation_id,
            confirmation_token=confirmation_token,
            edited_args_present=not request.edited_args.is_missing(),
            edited_args=(
                None if request.edited_args.is_missing() else dict(request.edited_args.as_mapping)
            ),
            rejection_feedback_present=False,
            rejection_feedback="",
        )

        def bind(write_session: Session) -> AbstractContextManager[LegacyApprovedBoundRoute]:
            session_factory = getattr(
                getattr(coordinator, "repository", None),
                "session_factory",
                None,
            )
            if not callable(session_factory):
                raise WriteOperationError("operation_unavailable")
            return _ApprovedLegacyRouteContext(
                routes=routes,
                write_session=write_session,
                session_factory=cast(Any, session_factory),
                lookup=lookup,
                confirmation_input=confirmation_input,
                operation_port=operation_port,
                pending_port=pending_port,
                pending_identity=pending_identity,
                on_prepared=on_prepared,
                on_bound=on_bound,
                jd_service=self.dependencies.application_jd_versions,
                outcomes_repository=self.dependencies.application_outcomes,
            )

        return bind

    def _request_fingerprint(
        self, pending: PendingAction, request: ConfirmationRequest, token: str
    ) -> str:
        operations = self.dependencies.write_operations
        if operations is None:
            raise WriteOperationError("operation_unavailable")
        operation_id = request.operation_id or pending.operation_id
        try:
            operation_id = str(UUID(operation_id))
        except (TypeError, ValueError) as exc:
            raise WriteOperationError("operation_identity_conflict") from exc
        if operation_id != pending.operation_id:
            raise WriteOperationError("operation_identity_conflict")
        operation = cast(Any, operations).get(operation_id)
        if operation is None:
            raise WriteOperationError("operation_result_unknown", retryable=True)
        return self._request_fingerprint_for_operation(operation, pending, request, token)

    def _request_fingerprint_for_operation(
        self,
        operation: object,
        pending: PendingAction,
        request: ConfirmationRequest,
        token: str,
    ) -> str:
        operations = self.dependencies.write_operations
        if operations is None:
            raise WriteOperationError("operation_unavailable")
        operation_id = request.operation_id or pending.operation_id
        try:
            operation_id = str(UUID(operation_id))
        except (TypeError, ValueError) as exc:
            raise WriteOperationError("operation_identity_conflict") from exc
        if operation_id != pending.operation_id:
            raise WriteOperationError("operation_identity_conflict")
        token_fingerprint = ledger_fingerprint(
            cast(Any, operations).key,
            "write-operation-confirmation-token-v1",
            token.encode("ascii"),
        )
        stored = str(_attribute(operation, "confirmation_token_fingerprint", "") or "")
        if not compare_digest(token_fingerprint, stored):
            raise WriteOperationError("operation_input_conflict")
        edited = (
            None
            if request.edited_args.is_missing()
            else cast(Mapping[str, JSONValue], dict(request.edited_args.as_mapping))
        )
        return operation_request_fingerprint(
            cast(Any, operations).key,
            operation_id=operation_id,
            tool_call_id=pending.tool_call_id,
            approved=request.approved,
            edited_args_present=not request.edited_args.is_missing(),
            edited_args=edited,
            rejection_feedback_present=request.rejection_feedback_present,
            rejection_feedback=request.rejection_feedback,
            confirmation_token_fingerprint=token_fingerprint,
            proposal_fingerprint=str(_attribute(operation, "proposal_fingerprint", "") or ""),
        )

    @staticmethod
    def _write_error(exc: WriteOperationError) -> RuntimeFailureOutcome:
        code_map: dict[str, RuntimeFailureCode] = {
            "operation_delivery_unknown": RuntimeFailureCode.OPERATION_DELIVERY_UNKNOWN,
            "operation_integrity_error": RuntimeFailureCode.OPERATION_INTEGRITY_ERROR,
            "operation_delivery_pending": RuntimeFailureCode.OPERATION_DELIVERY_PENDING,
            "operation_result_unknown": RuntimeFailureCode.OPERATION_RESULT_UNKNOWN,
            "operation_input_conflict": RuntimeFailureCode.OPERATION_INPUT_CONFLICT,
            "operation_identity_conflict": RuntimeFailureCode.OPERATION_IDENTITY_CONFLICT,
            "operation_unavailable": RuntimeFailureCode.OPERATION_UNAVAILABLE,
            "invalid_confirmation": RuntimeFailureCode.INVALID_CONFIRMATION,
        }
        code = code_map.get(exc.code, RuntimeFailureCode.OPERATION_FAILED)
        if code is RuntimeFailureCode.INVALID_CONFIRMATION:
            status = 422
        elif code in {
            RuntimeFailureCode.OPERATION_DELIVERY_PENDING,
            RuntimeFailureCode.OPERATION_INPUT_CONFLICT,
            RuntimeFailureCode.OPERATION_IDENTITY_CONFLICT,
            RuntimeFailureCode.OPERATION_INTEGRITY_ERROR,
        }:
            status = 409
        else:
            status = 503
        message = (
            "对话结果暂时无法保存。"
            if code
            in {
                RuntimeFailureCode.OPERATION_DELIVERY_UNKNOWN,
                RuntimeFailureCode.OPERATION_INTEGRITY_ERROR,
            }
            else "无法确认写入结果，请保留原请求后重试。"
        )
        return _error(
            code,
            message,
            status,
            retryable=(
                True
                if code
                in {
                    RuntimeFailureCode.OPERATION_DELIVERY_UNKNOWN,
                    RuntimeFailureCode.OPERATION_INTEGRITY_ERROR,
                }
                else exc.retryable
            ),
        )

    @staticmethod
    def _write_unknown(execution: OperationUnknown) -> RuntimeFailureOutcome:
        return DeterministicPilotAdapter._write_error(
            WriteOperationError(execution.code, retryable=execution.retryable)
        )

    def _deliver_terminal(
        self,
        conversation_id: int,
        pending: PendingAction,
        origin: Message,
        execution: OperationCommitted | OperationFailed,
        message: str,
        write_status: WriteStatus,
        *,
        undo: dict[str, Any] | None = None,
    ) -> DeterministicExecution:
        result = self._resolve_pending(
            conversation_id,
            pending,
            origin,
            message,
            getattr(execution, "ownership", None),
            undo=undo,
        )
        if result is None:
            return DeterministicExecution(
                _error(
                    RuntimeFailureCode.STALE_PENDING_ACTION,
                    "待确认操作已被更新，请刷新后重试。",
                    409,
                ),
                preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
            )
        if result is False:
            return DeterministicExecution(
                _error(
                    RuntimeFailureCode.OPERATION_DELIVERY_FAILED,
                    "写入结果已提交，但暂时无法生成后续说明。",
                    503,
                    retryable=True,
                ),
                preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
            )
        outcome = MessageOutcome(
            message,
            conversation_id=conversation_id,
            write_status=write_status,
            operation_id=pending.operation_id,
            legacy_projection=True,
        )
        events = (
            MetaEvent(supports_delta=False, supports_tool_events=False),
            UserMessageSavedEvent(),
            StatusEvent(phase="completed", label="已完成"),
            AssistantMessageEvent(message=message),
        )
        return DeterministicExecution(
            outcome,
            events=events,
            preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
        )

    def _resolve_pending(
        self,
        conversation_id: int,
        pending: PendingAction,
        origin: Message,
        message: str,
        ownership: object | None,
        *,
        undo: dict[str, Any] | None = None,
    ) -> bool | None:
        try:
            return self._resolve_pending_delivery(
                conversation_id,
                pending,
                origin,
                message,
                ownership,
                undo=undo,
            )
        finally:
            if type(ownership) is DeliveryOwnership:
                ownership.revoke_parent_route()

    def _resolve_pending_delivery(
        self,
        conversation_id: int,
        pending: PendingAction,
        origin: Message,
        message: str,
        ownership: object | None,
        *,
        undo: dict[str, Any] | None,
    ) -> bool | None:
        value = self.dependencies.persistence.persist_confirmation_delivery(
            conversation_id,
            ownership,
            origin,
            (Message(role="assistant", content=message),),
            None,
            route_handle=None,
            expected_pending=pending,
            claim_id=pending.operation_id,
            undo=undo,
        )
        if _result_ok(value):
            return True
        status = _result_status(value)
        return None if str(status) in {"cas_lost", "not_found", "closed"} else False

    def _persist_failure(
        self,
        conversation_id: int,
        pending: PendingAction,
        origin: Message,
        execution: OperationCommitted | OperationFailed,
        effective: PendingAction,
        *,
        transport: RuntimeTransportContext | None,
    ) -> DeterministicExecution:
        code = str(execution.payload.failure_code or "")
        if code in {"application_jd_stale_current_version", "application_jd_idempotency_conflict"}:
            args = _safe_args(effective.args)
            application_id = args.get("application_id")
            jd_text = args.get("jd_text")
            if type(application_id) is int and isinstance(jd_text, str):
                current = self._current_jd_by_id(application_id)
                current_id = _attribute(current, "id", None) if current is not None else None
                try:
                    replacement = build_pilot_pending_action(
                        application_id=application_id,
                        current_version_id=current_id if type(current_id) is int else None,
                        jd_text=jd_text,
                        source_url=args.get("source_url")
                        if isinstance(args.get("source_url"), str)
                        else None,
                        id_factory=self.dependencies.id_factory,
                        key_factory=self.dependencies.key_factory,
                    )
                    replaced, replacement_presentation = self._replace_pending(
                        conversation_id,
                        pending,
                        replacement,
                        origin,
                        "当前岗位资料已变化，请重新确认保存。",
                        getattr(execution, "ownership", None),
                    )
                    if replaced is False:
                        return DeterministicExecution(
                            _error(
                                RuntimeFailureCode.OPERATION_DELIVERY_FAILED,
                                "写入结果已提交，但暂时无法生成后续说明。",
                                503,
                                retryable=True,
                            ),
                            preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
                        )
                    if replaced is True:
                        if type(replacement_presentation) is not LegacyPendingPresentationV1:
                            raise ValueError("replacement Pending presentation is unavailable")
                        return DeterministicExecution(
                            _error(
                                RuntimeFailureCode(code),
                                "当前岗位资料已变化，请重新确认保存。",
                                409,
                                pending_action=_confirmation_payload(
                                    replacement,
                                    presentation=replacement_presentation,
                                ),
                            ),
                            preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
                        )
                    if replaced is None:
                        return DeterministicExecution(
                            _error(
                                RuntimeFailureCode.STALE_PENDING_ACTION,
                                "待确认操作已被更新，请刷新对话后重试。",
                                409,
                            ),
                            preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
                        )
                except (ValueError, KeyError):
                    pass
        messages: dict[str, tuple[RuntimeFailureCode, int, str]] = {
            "application_jd_invalid_request": (
                RuntimeFailureCode.APPLICATION_JD_INVALID_REQUEST,
                422,
                "岗位资料参数无效，请修改后重试。",
            ),
            "application_archive_idempotency_conflict": (
                RuntimeFailureCode.APPLICATION_ARCHIVE_IDEMPOTENCY_CONFLICT,
                409,
                "投递事实已发生变化，请刷新后重新确认。",
            ),
            "application_archive_source_conflict": (
                RuntimeFailureCode.APPLICATION_ARCHIVE_SOURCE_CONFLICT,
                409,
                "投递事实已发生变化，请刷新后重新确认。",
            ),
            "application_outcome_idempotency_conflict": (
                RuntimeFailureCode.APPLICATION_OUTCOME_IDEMPOTENCY_CONFLICT,
                409,
                "投递事实已发生变化，请刷新后重新确认。",
            ),
            "application_outcome_source_conflict": (
                RuntimeFailureCode.APPLICATION_OUTCOME_SOURCE_CONFLICT,
                409,
                "投递事实已发生变化，请刷新后重新确认。",
            ),
            "application_archive_invalid_request": (
                RuntimeFailureCode.APPLICATION_ARCHIVE_INVALID_REQUEST,
                422,
                "投递事实参数无效，请修改后重试。",
            ),
            "application_outcome_invalid_request": (
                RuntimeFailureCode.APPLICATION_OUTCOME_INVALID_REQUEST,
                422,
                "投递事实参数无效，请修改后重试。",
            ),
        }
        failure_code, status, message = messages.get(
            code,
            (RuntimeFailureCode.OPERATION_FAILED, 502, "岗位资料保存失败，请检查后重试。"),
        )
        delivered = self._resolve_pending(
            conversation_id,
            pending,
            origin,
            message,
            getattr(execution, "ownership", None),
        )
        if delivered is None:
            return DeterministicExecution(
                _error(
                    RuntimeFailureCode.STALE_PENDING_ACTION,
                    "待确认操作已被更新，请刷新后重试。",
                    409,
                ),
                preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
            )
        if delivered is False:
            return DeterministicExecution(
                _error(
                    RuntimeFailureCode.OPERATION_DELIVERY_FAILED,
                    "写入结果已提交，但暂时无法生成后续说明。",
                    503,
                    retryable=True,
                ),
                preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
            )
        return DeterministicExecution(
            _error(failure_code, message, status),
            preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
        )

    def _replace_pending(
        self,
        conversation_id: int,
        pending: PendingAction,
        replacement: PendingAction,
        origin: Message,
        message: str,
        ownership: object | None,
    ) -> tuple[bool | None, LegacyPendingPresentationV1 | None]:
        persistence = self.dependencies.persistence
        issuer = self.dependencies.legacy_jd_deterministic_action_issuer
        projected: LegacyPendingPresentationV1 | None = None

        def persist(
            route_handle: PendingPersistenceRouteHandle,
            presentation: LegacyPendingPresentationV1 | None,
        ) -> object:
            nonlocal projected
            if type(presentation) is not LegacyPendingPresentationV1:
                raise TypeError("replacement Pending requires exact presentation")
            projected = presentation
            replacement_with_human = PendingAction(
                replacement.tool_call_id,
                replacement.tool_name,
                replacement.args,
                presentation.human,
                replacement.operation_id,
            )
            return persistence.persist_confirmation_delivery(
                conversation_id,
                ownership,
                origin,
                (Message(role="assistant", content=message),),
                replacement_with_human,
                route_handle=route_handle,
                expected_pending=pending,
                claim_id=pending.operation_id,
                undo={},
            )

        try:
            value = self._run_initial_route(
                issuer,
                conversation_id=conversation_id,
                pending=replacement,
                body=persist,
            )
        finally:
            if type(ownership) is DeliveryOwnership:
                ownership.revoke_parent_route()
        if _result_ok(value):
            return True, projected
        status = _result_status(value)
        if str(status) in {"cas_lost", "not_found", "closed"}:
            return None, None
        return False, None

    def _current_jd_by_id(self, application_id: int) -> object | None:
        getter = _callable(self.dependencies.application_jd_versions, ("get_current", "current"))
        if getter is None:
            return None
        return _invoke(
            getter, {"application_id": application_id, "id": application_id}, (application_id,)
        )

    def _replay_execution(
        self,
        conversation_id: int,
        execution: OperationReplay,
        request_fingerprint: str,
        *,
        transport: RuntimeTransportContext | None,
    ) -> DeterministicExecution:
        operations = self.dependencies.write_operations
        if operations is None:
            return DeterministicExecution(
                _error(RuntimeFailureCode.OPERATION_UNAVAILABLE, "写入账本暂不可用。", 503),
                preparation_kind=PreparationKind.REPLAY,
            )
        try:
            replay = execution
            if replay.delivery_status == "pending":
                repository = cast(Any, operations)
                converged = repository.converge_expired_delivery(replay.operation_id)
                if isinstance(converged, OperationUnknown):
                    return DeterministicExecution(
                        self._write_unknown(converged), preparation_kind=PreparationKind.REPLAY
                    )
                refreshed = repository.get(replay.operation_id)
                if refreshed is None:
                    return DeterministicExecution(
                        _error(
                            RuntimeFailureCode.OPERATION_RESULT_UNKNOWN,
                            "写入结果暂时无法确认，请保留确认卡后重试。",
                            503,
                            retryable=True,
                        ),
                        preparation_kind=PreparationKind.REPLAY,
                    )
                replay = repository.replay(refreshed, request_fingerprint)
            if replay.delivery_outcome == "chained_pending":
                verified = replay.chained_pending
                if (
                    verified is None
                    or verified.adapter_kind != "legacy_deterministic"
                    or verified.tool_name != "save_application_jd_version"
                    or verified.conversation_id != conversation_id
                ):
                    raise WriteOperationError("operation_delivery_unknown", retryable=True)
                pending = PendingAction(
                    verified.tool_call_id,
                    verified.tool_name,
                    verified.raw_args,
                    verified.human,
                    verified.operation_id,
                )
                token = _confirmation_token(pending)
                expected_token = ledger_fingerprint(
                    cast(Any, operations).key,
                    "write-operation-confirmation-token-v1",
                    token.encode("ascii"),
                )
                if not compare_digest(expected_token, verified.confirmation_token_fingerprint):
                    raise WriteOperationError("operation_integrity_error")
                confirmation = self._confirmation_required(
                    pending,
                    conversation_id,
                    presentation=self._historical_pending_presentation(pending),
                    operation_id=replay.operation_id,
                    replayed=True,
                )
                return DeterministicExecution(
                    confirmation.outcome,
                    events=confirmation.events,
                    preparation_kind=PreparationKind.REPLAY,
                    pending_replay=True,
                )
            status = replay.payload.status
            if status == "committed":
                message, write_status = replay.final_message or "操作已完成。", "success"
            elif status == "rejected":
                message, write_status = replay.final_message or "已取消本次操作。", "cancelled"
            else:
                message, write_status = (
                    replay.final_message or replay.payload.visible_result,
                    "failed",
                )
            undo: Mapping[str, JSONValue] | None = None
            if replay.payload.undo_json is not None:
                raw_undo = json.loads(replay.payload.undo_json)
                if isinstance(raw_undo, Mapping):
                    undo = {
                        **cast(dict[str, JSONValue], raw_undo),
                        "parent_operation_id": replay.operation_id,
                    }
            outcome = OperationReplayOutcome(
                operation_id=replay.operation_id,
                conversation_id=conversation_id,
                message=message,
                status=cast(Any, status),
                write_status=cast(Any, write_status),
                write_error=replay.payload.failure_code if status == "failed" else None,
                undo=freeze_json_mapping(undo) if undo is not None else None,
            )
            return DeterministicExecution(
                outcome,
                events=(
                    MetaEvent(supports_delta=False, supports_tool_events=False),
                    UserMessageSavedEvent(),
                    StatusEvent(phase="completed", label="已完成"),
                    AssistantMessageEvent(message=message),
                ),
                preparation_kind=PreparationKind.REPLAY,
            )
        except WriteOperationError as exc:
            return DeterministicExecution(
                self._write_error(exc), preparation_kind=PreparationKind.REPLAY
            )


__all__ = [
    "DeterministicDependencies",
    "DeterministicExecution",
    "DeterministicPilotAdapter",
    "LEGACY_DETERMINISTIC_NAMES",
]
