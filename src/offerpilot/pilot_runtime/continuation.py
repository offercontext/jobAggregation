"""Ledger-first confirmation continuation orchestration.

The HTTP routes used to carry this state in a collection of local closures and
``dict[str, Any]`` values.  This module is the transport-independent owner of
that state.  It deliberately delegates transactions to the existing write and
chat coordinators: it never opens a Session, executes SQL, or calls a provider.

There are two important boundaries in this file:

* ``terminal_replay`` is Ledger-first.  A terminal operation is validated and
  replayed without looking at Pending, the model, the projector, or a tool.
* ``ConfirmationSession`` is the only object that can claim a live Pending,
  record a tool result, and submit a fenced delivery bundle.  Its callbacks are
  intentionally narrow so the Agent Driver remains the only execution entry.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from secrets import compare_digest
from threading import RLock
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import select

from offerpilot.ai.agent_contracts import PendingAction, _ASDICT_GUARD
from offerpilot.ai.agent_loop import ApprovedContinuationSegment
from offerpilot.ai.confirmation import prepare_pending_action
from offerpilot.ai.tool_authority import (
    ApprovalExecutionAuthority,
    AuthorityFactory,
    PendingAuthorityClaim,
    TrustedContextScope,
)
from offerpilot.ai.tool_authority.visibility import (
    AuthorityApplicationVisibilityError,
    AuthorityApplicationVisibilityQuery,
)
from offerpilot.ai.tool_runtime.context import ToolExecutionContext
from offerpilot.ai.tool_runtime.contracts import (
    JSONValue,
    PreparedToolCall,
    ToolExecutionRecord,
    ToolFailure,
    ToolSuccess,
    TransientToolRuntimeValue,
)
from offerpilot.ai.types import Message, ToolCall
from offerpilot.ai.write_operations import (
    DeliveryHeartbeat,
    DeliveryOwnership,
    LedgerOperationPreheader,
    LedgerPendingPointer,
    OperationCommitted,
    OperationExecution,
    OperationFailed,
    OperationReplay,
    OperationUnknown,
    WriteOperationError,
    ledger_fingerprint,
    operation_request_fingerprint,
    pending_action_identity,
)
from offerpilot.models import Conversation, WriteOperation

from .contracts import (
    ConfirmationRequiredOutcome,
    ConfirmationRequest,
    EditedArgs,
    ImmutablePayload,
    OperationReplayOutcome,
    PendingActionPayload,
    freeze_json_mapping,
)
from .persistence import PersistenceResult, PersistenceStatus


ConfirmationAttempt = Callable[
    [PendingAction, PreparedToolCall[Any, Any] | None],
    ToolFailure | None,
]
ConfirmationResult = Callable[
    [PendingAction, bool, Message, ToolExecutionRecord[Any, Any] | None],
    object,
]
LedgerExecutor = Callable[
    [PreparedToolCall[Any, Any], object, object],
    ToolExecutionRecord[Any, Any],
]
class ConfirmationPendingReader(Protocol):
    """The only persistence read needed before a confirmation claim."""

    def get_pending_action(self, conversation_id: int) -> object | None: ...


class ConfirmationPersistenceAdapter(ConfirmationPendingReader, Protocol):
    """Typed read/delivery boundary; implementations own the SQL atom."""

    def list_messages(self, conversation_id: int) -> Sequence[object]: ...

    def persist_confirmation_delivery(self, **kwargs: object) -> PersistenceResult: ...


class ConfirmationOperationRepository(Protocol):
    """Typed Ledger read/replay/lease surface used by this state machine."""

    key: object

    def get(self, operation_id: str) -> object | None: ...

    def operation_preheader(
        self, *, conversation_id: int, operation_id: str | None
    ) -> LedgerOperationPreheader: ...

    def replay(self, operation: object, request_fingerprint: str) -> OperationReplay: ...

    def converge_expired_delivery(self, operation_id: str) -> OperationReplay | OperationUnknown: ...

    def heartbeat(self, ownership: DeliveryOwnership) -> bool: ...


class ConfirmationWriteCoordinator(Protocol):
    """Existing transaction atoms; SQL remains outside this module."""

    def reject_primary(self, **kwargs: object) -> OperationExecution: ...

    def execute_primary(self, **kwargs: object) -> tuple[OperationExecution, ToolExecutionRecord[Any, Any] | None]: ...


_STALE_APPROVAL_CODES = frozenset(
    {
        "authorization_scope_changed",
        "authorization_scope_unbound",
        "authorization_scope_unavailable",
        "scope_access_denied",
    }
)


def _public_confirmation_error_code(code: str) -> str:
    return "stale_pending_action" if code in _STALE_APPROVAL_CODES else code


class ApprovalAuthorityResolver:
    """Minimal, provider-free resolver for one proposed approval attempt.

    The resolver deliberately projects only Ledger identity, the owning
    Conversation scope/revision, and active Application visibility.  Pending
    arguments are supplied only as an already-computed digest/revision and are
    never loaded or decoded here.
    """

    __slots__ = (
        "repository",
        "factory",
        "capabilities",
        "capability_profile_id",
        "capability_policy_version",
        "binding_policy_version",
        "capability_profile_fingerprint",
        "binding_policy_fingerprint",
    )

    def __init__(
        self,
        repository: object,
        factory: AuthorityFactory,
        *,
        capabilities: frozenset[object] = frozenset(),
        capability_profile_id: str = "agent_typed_v1",
        capability_policy_version: str = "capability-policy-v1",
        binding_policy_version: str = "binding-policy-v1",
        capability_profile_fingerprint: str = "sha256:" + "0" * 64,
        binding_policy_fingerprint: str = "sha256:" + "0" * 64,
    ) -> None:
        self.repository = repository
        self.factory = factory
        self.capabilities = capabilities
        self.capability_profile_id = capability_profile_id
        self.capability_policy_version = capability_policy_version
        self.binding_policy_version = binding_policy_version
        self.capability_profile_fingerprint = capability_profile_fingerprint
        self.binding_policy_fingerprint = binding_policy_fingerprint

    def resolve(
        self,
        *,
        operation: object,
        pending: object,
        conversation_id: int,
        pending_action_revision: int,
        effective_args_digest: str,
    ) -> ApprovalExecutionAuthority:
        session_factory = _attribute(self.repository, "session_factory")
        if not callable(session_factory):
            raise WriteOperationError("operation_unavailable")
        operation_id = str(_attribute(operation, "id", "") or "")
        with session_factory() as session:
            ledger_row = session.execute(
                select(
                    WriteOperation.id,
                    WriteOperation.conversation_id,
                    WriteOperation.status,
                    WriteOperation.adapter_kind,
                    WriteOperation.tool_call_id,
                    WriteOperation.tool_name,
                    WriteOperation.proposal_fingerprint,
                    WriteOperation.confirmation_token_fingerprint,
                    WriteOperation.authorization_scope_fingerprint,
                ).where(WriteOperation.id == operation_id)
            ).one_or_none()
            if ledger_row is None:
                raise WriteOperationError("operation_result_unknown", retryable=True)
            if ledger_row.conversation_id is None:
                raise WriteOperationError("operation_unavailable")
            if ledger_row.status != "proposed":
                raise WriteOperationError("operation_identity_conflict")
            if ledger_row.authorization_scope_fingerprint is None:
                raise WriteOperationError("authorization_scope_unbound")
            scope_row = session.execute(
                select(
                    Conversation.id,
                    Conversation.context_type,
                    Conversation.context_ref,
                    Conversation.mode,
                    Conversation.scope_revision,
                    Conversation.pending_operation_id,
                    Conversation.pending_tool_call_id,
                    Conversation.pending_tool_name,
                    Conversation.pending_confirmation_claim_id,
                ).where(Conversation.id == conversation_id)
            ).one_or_none()
            if scope_row is None:
                raise WriteOperationError("operation_unavailable")
            if (
                ledger_row.conversation_id != conversation_id
                or scope_row.pending_operation_id != operation_id
                or scope_row.pending_tool_call_id != ledger_row.tool_call_id
                or scope_row.pending_tool_name != ledger_row.tool_name
                or scope_row.pending_confirmation_claim_id != ""
            ):
                raise WriteOperationError("operation_identity_conflict")
            context_ref: int | None = None
            if scope_row.context_type == "application":
                try:
                    context_ref = int(scope_row.context_ref)
                except (TypeError, ValueError) as exc:
                    raise WriteOperationError("authorization_scope_unavailable") from exc
                try:
                    active = AuthorityApplicationVisibilityQuery().execute_on_session(
                        session, context_ref
                    )
                except AuthorityApplicationVisibilityError as exc:
                    raise WriteOperationError(
                        "operation_not_committed", retryable=True
                    ) from exc
                if active is None:
                    raise WriteOperationError("authorization_scope_unavailable")
        self._bind_pending_identity(
            pending,
            conversation_id=conversation_id,
            operation_id=operation_id,
            pending_action_revision=pending_action_revision,
            effective_args_digest=effective_args_digest,
        )
        self.factory.register_pending(
            pending,
            conversation_id=conversation_id,
            operation_id=operation_id,
            tool_call_id=str(ledger_row.tool_call_id),
            tool_name=str(ledger_row.tool_name),
            pending_action_revision=pending_action_revision,
            effective_args_digest=effective_args_digest,
            arguments_digest=effective_args_digest,
        )
        return self.factory.create_approval_authority(
            operation_id=operation_id,
            conversation_id=conversation_id,
            conversation_scope_revision=int(scope_row.scope_revision),
            trusted_scope=TrustedContextScope(
                cast(Any, scope_row.context_type),
                context_ref,
                str(scope_row.mode),
            ),
            pending_identity=pending,
            pending_action_revision=pending_action_revision,
            tool_call_id=str(ledger_row.tool_call_id),
            tool_name=str(ledger_row.tool_name),
            effective_args_digest=effective_args_digest,
            capability_profile_id=self.capability_profile_id,
            capabilities=self.capabilities,
            capability_policy_version=self.capability_policy_version,
            binding_policy_version=self.binding_policy_version,
            capability_profile_fingerprint=self.capability_profile_fingerprint,
            binding_policy_fingerprint=self.binding_policy_fingerprint,
        )

    @staticmethod
    def _bind_pending_identity(
        pending: object,
        *,
        conversation_id: int,
        operation_id: str,
        pending_action_revision: int,
        effective_args_digest: str,
    ) -> None:
        expected = {
            "conversation_id": conversation_id,
            "operation_id": operation_id,
            "pending_action_revision": pending_action_revision,
            "arguments_digest": effective_args_digest,
            "effective_args_digest": effective_args_digest,
        }
        current = {name: getattr(pending, name, None) for name in expected}
        if all(value is None for name, value in current.items() if name != "operation_id"):
            binder = getattr(pending, "bind_typed_proposal_identity", None)
            if not callable(binder):
                raise WriteOperationError("operation_identity_conflict")
            try:
                binder(
                    conversation_id=conversation_id,
                    pending_action_revision=pending_action_revision,
                    pending_confirmation_claim_id=operation_id,
                    arguments_digest=effective_args_digest,
                )
            except (TypeError, ValueError) as exc:
                raise WriteOperationError("operation_identity_conflict") from exc
            return
        for name, expected_value in expected.items():
            if getattr(pending, name, None) != expected_value:
                raise WriteOperationError("operation_identity_conflict")
        claim_id = getattr(pending, "pending_confirmation_claim_id", None)
        if claim_id is not None and claim_id != operation_id:
            raise WriteOperationError("operation_identity_conflict")


def _attribute(value: object | None, name: str, default: object = None) -> object:
    if value is None:
        return default
    if isinstance(value, Mapping):
        return value.get(name, default)
    try:
        return getattr(value, name)
    except AttributeError:
        return default


def _callable(value: object | None, names: tuple[str, ...]) -> Callable[..., object] | None:
    if value is None:
        return None
    if callable(value):
        return cast(Callable[..., object], value)
    for name in names:
        candidate = _attribute(value, name)
        if callable(candidate):
            return cast(Callable[..., object], candidate)
    return None


def _invoke(
    function: Callable[..., object],
    values: Mapping[str, object],
    positional: tuple[object, ...] = (),
    *,
    var_keyword_values: Mapping[str, object] | None = None,
) -> object:
    """Enter an injected callable exactly once after signature binding.

    A ``TypeError`` raised by a callable body is never mistaken for a binding
    error and therefore never causes a second executor/provider attempt.
    """

    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return function(*positional)

    parameters = tuple(signature.parameters.values())
    kwargs: dict[str, object] = {}
    args: list[object] = []
    fallback_index = 0
    has_var_keyword = False
    for parameter in parameters:
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            args.extend(positional[fallback_index:])
            fallback_index = len(positional)
            continue
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            has_var_keyword = True
            continue
        if parameter.name in values:
            selected = values[parameter.name]
        elif fallback_index < len(positional):
            selected = positional[fallback_index]
            fallback_index += 1
        elif parameter.default is inspect.Parameter.empty:
            continue
        else:
            continue
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY:
            kwargs[parameter.name] = selected
        else:
            args.append(selected)
    if has_var_keyword:
        keyword_values = dict(values)
        keyword_values.update(var_keyword_values or {})
        for name, value in keyword_values.items():
            if name not in signature.parameters and name not in kwargs:
                kwargs[name] = value
    try:
        signature.bind(*args, **kwargs)
    except TypeError:
        named_kwargs: dict[str, object] = {}
        named_args: list[object] = []
        for parameter in parameters:
            if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
                if parameter.name in values:
                    named_args.append(values[parameter.name])
            elif parameter.kind in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            } and parameter.name in values:
                named_kwargs[parameter.name] = values[parameter.name]
        if has_var_keyword:
            keyword_values = dict(values)
            keyword_values.update(var_keyword_values or {})
            for name, value in keyword_values.items():
                if name not in signature.parameters:
                    named_kwargs[name] = value
        signature.bind(*named_args, **named_kwargs)
        return function(*named_args, **named_kwargs)
    return function(*args, **kwargs)


def _pending(value: object | None) -> PendingAction | None:
    if value is None:
        return None
    if isinstance(value, PendingAction):
        return value
    operation_id = str(_attribute(value, "operation_id", "") or "")
    return PendingAction(
        tool_call_id=str(_attribute(value, "tool_call_id", "") or ""),
        tool_name=str(_attribute(value, "tool_name", "") or ""),
        args=str(_attribute(value, "args", "") or ""),
        human=str(_attribute(value, "human", "") or ""),
        operation_id=operation_id,
    )


def _message(value: object) -> Message:
    if isinstance(value, Message):
        return value
    if isinstance(value, Mapping):
        raw_calls = value.get("tool_calls") or ()
        calls = tuple(
            ToolCall(
                str(_attribute(call, "id", "") or ""),
                str(_attribute(call, "name", "") or ""),
                str(_attribute(call, "args", "") or ""),
            )
            for call in raw_calls
            if isinstance(raw_calls, Sequence) and not isinstance(raw_calls, (str, bytes))
        )
        blocks = value.get("provider_blocks")
        return Message(
            role=str(value.get("role") or "assistant"),
            content=str(value.get("content") or ""),
            tool_calls=list(calls),
            tool_call_id=str(value.get("tool_call_id") or ""),
            provider_blocks=dict(blocks) if isinstance(blocks, Mapping) else {},
            surface_contributor=str(value.get("surface_contributor") or ""),
            surface_signal=str(value.get("surface_signal") or ""),
            surface_revision=str(value.get("surface_revision") or ""),
            surface_page_kind=str(value.get("surface_page_kind") or ""),
            surface_attachment_kinds=str(value.get("surface_attachment_kinds") or ""),
        )
    raw_calls = _attribute(value, "tool_calls", ())
    call_values = (
        cast(Sequence[object], raw_calls)
        if isinstance(raw_calls, Sequence) and not isinstance(raw_calls, (str, bytes))
        else ()
    )
    calls = tuple(
        ToolCall(
            str(_attribute(call, "id", "") or ""),
            str(_attribute(call, "name", "") or ""),
            str(_attribute(call, "args", "") or ""),
        )
        for call in call_values
    )
    blocks = _attribute(value, "provider_blocks", {})
    return Message(
        role=str(_attribute(value, "role", "assistant") or "assistant"),
        content=str(_attribute(value, "content", "") or ""),
        tool_calls=list(calls),
        tool_call_id=str(_attribute(value, "tool_call_id", "") or ""),
        provider_blocks=dict(blocks) if isinstance(blocks, Mapping) else {},
        surface_contributor=str(_attribute(value, "surface_contributor", "") or ""),
        surface_signal=str(_attribute(value, "surface_signal", "") or ""),
        surface_revision=str(_attribute(value, "surface_revision", "") or ""),
        surface_page_kind=str(_attribute(value, "surface_page_kind", "") or ""),
        surface_attachment_kinds=str(_attribute(value, "surface_attachment_kinds", "") or ""),
    )


def _confirmation_token(pending: PendingAction) -> str:
    """Produce the public confirmation token without exposing its input."""

    try:
        parsed = json.loads(pending.args)
        canonical_args = json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        canonical_args = pending.args
    identity = json.dumps(
        [pending.tool_call_id, pending.tool_name, canonical_args],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _status(value: object | None) -> str:
    raw = _attribute(value, "status", "")
    return str(getattr(raw, "value", raw or ""))


def _terminal(execution: object) -> bool:
    return isinstance(execution, (OperationCommitted, OperationFailed))


def _record_succeeded(record: object | None) -> bool:
    if record is None:
        return False
    if _attribute(record, "terminal_persisted", False) is True:
        return _status(_attribute(record, "outcome")) == "success" or isinstance(
            _attribute(record, "outcome"), ToolSuccess
        )
    return isinstance(_attribute(record, "outcome"), ToolSuccess)


@dataclass(frozen=True, slots=True)
class ConfirmationDependencies:
    """Runtime-owned seams used by :class:`ConfirmationCoordinator`.

    The dependencies are intentionally opaque.  The concrete repository and
    provider implementations stay in the composition root and are only
    reached through existing coordinator/driver methods.
    """

    persistence: ConfirmationPersistenceAdapter | None = None
    write_operations: ConfirmationOperationRepository | None = None
    write_coordinator: ConfirmationWriteCoordinator | None = None
    catalog: object | None = None
    approval_context_resolver: Callable[..., ToolExecutionContext] | None = field(
        default=None, repr=False, compare=False
    )
    transactional_delivery: object | None = field(default=None, repr=False, compare=False)
    clock: Callable[[], datetime] = field(
        default=lambda: datetime.now(timezone.utc), repr=False, compare=False
    )


@dataclass(frozen=True, slots=True, repr=False)
class ConfirmationIdentity:
    conversation_id: int
    operation_id: str
    tool_call_id: str
    tool_name: str
    request_fingerprint: str = field(repr=False)
    confirmation_token: str = field(repr=False)
    proposal_fingerprint: str = field(repr=False)


@dataclass(slots=True, repr=False)
class ConfirmationState:
    """Sealed mutable state for one confirmation invocation.

    Sensitive feedback, public tokens, HMAC fingerprints, owner raw tokens,
    and generation data are excluded from repr.  The state is intentionally
    not a mapping so accidental persistence/logging of internal control fields
    is structurally harder.
    """

    identity: ConfirmationIdentity
    pending: PendingAction
    effective_pending: PendingAction
    approved: bool
    edited_args: EditedArgs = field(repr=False)
    rejection_feedback: str = field(default="", repr=False)
    undo_seed: Mapping[str, Any] = field(default_factory=dict, repr=False)
    claim_id: str | None = field(default=None, repr=False)
    confirmation_attempted: bool = False
    approval_decided_callback: Callable[[object | None], None] | None = field(
        default=None, repr=False, compare=False
    )
    approval_resume_callback: Callable[[], None] | None = field(
        default=None, repr=False, compare=False
    )
    approval_decided_recorded: bool = field(default=False, repr=False, compare=False)
    terminal_execution: OperationExecution | None = field(default=None, repr=False)
    origin_tool_message: Message | None = field(default=None, repr=False)
    execution_record: ToolExecutionRecord[Any, Any] | None = field(default=None, repr=False)
    succeeded: bool = False
    replayed: bool = False
    undo: Mapping[str, Any] = field(default_factory=dict, repr=False)
    undo_update: Mapping[str, Any] | None = field(default=None, repr=False)
    undo_operation_id: str = field(default="", repr=False)
    prepared_call: object | None = field(default=None, repr=False, compare=False)
    approval_context: ToolExecutionContext | None = field(
        default=None, repr=False, compare=False
    )
    delivery_ownership: DeliveryOwnership | None = field(default=None, repr=False)
    delivery_heartbeat: DeliveryHeartbeat | None = field(default=None, repr=False)
    continuation_generation: datetime | None = field(default=None, repr=False)
    fallback_persisted: bool = False
    cas_lost: bool = False
    timed_out: bool = False
    cancelled: bool = False
    delivered: bool = False
    delivery_in_progress: bool = field(default=False, repr=False, compare=False)
    delivery_result: PersistenceResult | None = field(default=None, repr=False, compare=False)
    transactional_delivery_persisted: bool = field(default=False, repr=False, compare=False)
    continuation_segment_activated: bool = field(default=False, repr=False, compare=False)
    continuation_segment_close: Callable[[], object] | None = field(
        default=None, repr=False, compare=False
    )
    active: bool = True
    lock: RLock = field(default_factory=RLock, repr=False, compare=False)


@dataclass(frozen=True, slots=True, repr=False)
class DeliveryBundle:
    """Detached continuation result awaiting one fenced delivery commit."""

    messages: tuple[Message, ...]
    pending: PendingAction | None = None
    clarification: tuple[PendingAction, str] | None = None
    pending_authority_claim: PendingAuthorityClaim | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if type(self.messages) is not tuple:
            raise TypeError("messages must be a tuple")
        if any(not isinstance(item, Message) for item in self.messages):
            raise TypeError("messages must contain Message values")
        if self.pending is not None and self.clarification is not None:
            raise ValueError("pending and clarification cannot both be delivered")
        if self.pending_authority_claim is not None:
            if type(self.pending_authority_claim) is not PendingAuthorityClaim:
                raise TypeError("pending_authority_claim must be an exact PendingAuthorityClaim")
            if self.pending is None:
                raise ValueError("pending_authority_claim requires a chained Pending")


@dataclass(slots=True, repr=False)
class ConfirmationSession:
    """Ledger-backed state and callbacks for an approved continuation."""

    state: ConfirmationState
    on_confirmation_attempt: ConfirmationAttempt
    on_confirmation_result: ConfirmationResult
    execute_operation: LedgerExecutor
    delivery_fence: Callable[[], bool]
    continuation_segment_builder: Callable[[], object] | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def pending(self) -> PendingAction:
        return self.state.effective_pending

    @property
    def request_fingerprint(self) -> str:
        return self.state.identity.request_fingerprint

    @property
    def approval_context(self) -> ToolExecutionContext:
        context = self.state.approval_context
        if not isinstance(context, ToolExecutionContext):
            raise WriteOperationError("operation_unavailable")
        return context

    def approval_execution_context(self, recorder: object) -> ToolExecutionContext:
        """Return the origin Approval context with its Ledger executor bound."""

        context = self.approval_context
        return context.with_runtime_dependencies(
            run_recorder=cast(Any, recorder),
            operation_executor=self.execute_operation,
        )

    def activate_continuation_segment(self) -> ApprovedContinuationSegment:
        """Close approval and activate one fresh post-terminal Segment."""

        with self.state.lock:
            if self.state.continuation_segment_activated:
                raise WriteOperationError("operation_delivery_unknown", retryable=True)
            if self.state.cancelled or self.state.timed_out or not self.state.active:
                raise WriteOperationError("confirmation_claim_lost")
            if _attribute(self.state, "approved", True) is not True:
                raise WriteOperationError("confirmation_claim_lost")
            terminal = self.state.terminal_execution
            if not isinstance(terminal, (OperationCommitted, OperationFailed)):
                raise WriteOperationError("operation_delivery_unknown", retryable=True)
            ownership = self.state.delivery_ownership
            terminal_ownership = _attribute(terminal, "ownership")
            identity = _attribute(self.state, "identity")
            expected_operation_id = _attribute(identity, "operation_id", terminal.operation_id)
            if (
                type(ownership) is not DeliveryOwnership
                or type(terminal_ownership) is not DeliveryOwnership
                or ownership is not terminal_ownership
                or ownership.operation_id != expected_operation_id
                or terminal.operation_id != expected_operation_id
            ):
                raise WriteOperationError("operation_delivery_unknown", retryable=True)
            if not self.delivery_fence():
                raise WriteOperationError("operation_delivery_unknown", retryable=True)
            builder = self.continuation_segment_builder
            self.state.continuation_segment_activated = True

        # Approval authority is valid only for the origin transaction.  Close
        # it before the canonical reload so the fresh Segment cannot retain it.
        ConfirmationCoordinator._close_approval_context(self.state)
        if builder is None:
            raise WriteOperationError("operation_delivery_unknown", retryable=True)
        value = builder()
        if type(value) is not ApprovedContinuationSegment:
            raise WriteOperationError("operation_delivery_unknown", retryable=True)
        return value

    def close_continuation_segment(self) -> None:
        with self.state.lock:
            close = self.state.continuation_segment_close
            self.state.continuation_segment_close = None
        if close is not None:
            close()


@dataclass(frozen=True, slots=True, repr=False)
class ConfirmationApprovedWritePort(TransientToolRuntimeValue):
    """Expose one session through the Agent Loop's narrow approved port."""

    session: ConfirmationSession
    _serialization_guard: object = field(
        default=_ASDICT_GUARD,
        init=False,
        repr=False,
        compare=False,
    )

    def __repr__(self) -> str:
        return "<ConfirmationApprovedWritePort transient>"

    @property
    def pending(self) -> PendingAction:
        return self.session.pending

    def claim(
        self,
        pending: PendingAction,
        prepared: PreparedToolCall[Any, Any],
    ) -> ToolFailure | None:
        claimed = self.session.on_confirmation_attempt(pending, prepared)
        if claimed is not None and not isinstance(claimed, ToolFailure):
            raise WriteOperationError("confirmation_claim_lost")
        return claimed

    def record_result(
        self,
        pending: PendingAction,
        tool_message: Message,
        record: ToolExecutionRecord[Any, Any],
    ) -> None:
        self.session.on_confirmation_result(pending, True, tool_message, record)

    def delivery_fence(self) -> bool:
        return self.session.delivery_fence()

    def activate_continuation_segment(self) -> ApprovedContinuationSegment:
        value = self.session.activate_continuation_segment()
        if type(value) is not ApprovedContinuationSegment:
            raise TypeError("continuation activation must return ApprovedContinuationSegment")
        return value


class ConfirmationReplayError(RuntimeError):
    """Raised by the Ledger callback when the operation was already terminal."""

    def __init__(self, replay: OperationReplay) -> None:
        super().__init__("operation replay")
        self.replay = replay


def _replayed_pending_payload(replay: OperationReplay) -> PendingActionPayload:
    pending = replay.chained_pending
    if (
        pending is None
        or pending.adapter_kind != "typed"
        or pending.decoded_args is None
    ):
        raise WriteOperationError("operation_delivery_unknown", retryable=True)
    canonical_args = json.dumps(
        pending.decoded_args,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    identity = json.dumps(
        [pending.tool_call_id, pending.tool_name, canonical_args],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    token = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return PendingActionPayload(
        tool_name=pending.tool_name,
        operation_id=pending.operation_id,
        human=pending.human,
        args=freeze_json_mapping(cast(Mapping[str, object], pending.decoded_args)),
        confirmation_token=token,
    )


def _runtime_replay(
    replay: OperationReplay,
    conversation_id: int,
    *,
    operation: object | None = None,
) -> OperationReplayOutcome | ConfirmationRequiredOutcome:
    payload = replay.payload
    if payload.status not in {"committed", "rejected", "failed"}:
        raise WriteOperationError("operation_integrity_error")
    if replay.delivery_status not in {"pending", "completed", "failed"}:
        raise WriteOperationError("operation_integrity_error")
    if replay.delivery_outcome == "chained_pending":
        pending_payload = _replayed_pending_payload(replay)
        return ConfirmationRequiredOutcome(
            confirmation_token=pending_payload.confirmation_token,
            conversation_id=conversation_id,
            operation_id=replay.operation_id,
            pending_action=pending_payload,
            replayed=True,
        )
    if replay.delivery_outcome not in {None, "final_response", "fallback"}:
        raise WriteOperationError("operation_integrity_error")

    try:
        transport = json.loads(payload.transport_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WriteOperationError("operation_integrity_error") from exc
    if not isinstance(transport, Mapping):
        raise WriteOperationError("operation_integrity_error")
    tool_call_id = str(
        transport.get("tool_call_id")
        or _attribute(operation, "tool_call_id", "")
        or ""
    )
    tool_name = str(
        transport.get("tool_name")
        or _attribute(operation, "tool_name", "")
        or ""
    )
    if not tool_call_id or not tool_name:
        raise WriteOperationError("operation_integrity_error")

    def payload_tuple(name: str) -> tuple[ImmutablePayload, ...]:
        value = transport.get(name, ())
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            raise WriteOperationError("operation_integrity_error")
        items: list[ImmutablePayload] = []
        for item in value:
            if not isinstance(item, Mapping):
                raise WriteOperationError("operation_integrity_error")
            try:
                items.append(freeze_json_mapping(cast(Mapping[str, object], item)))
            except (TypeError, ValueError) as exc:
                raise WriteOperationError("operation_integrity_error") from exc
        return tuple(items)

    summary = str(transport.get("summary", payload.visible_result) or "")
    evidence = payload_tuple("evidence")
    affected_resources = payload_tuple("affected_resources")
    changed_entities = payload_tuple("changed_entities")
    if payload.status == "committed":
        write_status = "success"
        message = replay.final_message or "操作已完成。"
    elif payload.status == "rejected":
        write_status = "cancelled"
        message = replay.final_message or "已取消本次操作。"
    else:
        write_status = "failed"
        message = replay.final_message or payload.visible_result
    undo: Mapping[str, JSONValue] | None = None
    if payload.undo_json:
        try:
            decoded = json.loads(payload.undo_json)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WriteOperationError("operation_integrity_error") from exc
        if not isinstance(decoded, Mapping):
            raise WriteOperationError("operation_integrity_error")
        undo = cast(Mapping[str, JSONValue], dict(decoded))
    return OperationReplayOutcome(
        operation_id=replay.operation_id,
        conversation_id=conversation_id,
        message=message,
        status=cast(Any, payload.status),
        write_status=cast(Any, write_status),
        write_error=payload.failure_code,
        undo=freeze_json_mapping(undo) if undo is not None else None,
        replayed=True,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        visible_result=payload.visible_result,
        summary=summary,
        evidence=evidence,
        affected_resources=affected_resources,
        changed_entities=changed_entities,
    )


class ConfirmationCoordinator:
    """Own the complete live/terminal confirmation state machine."""

    __slots__ = ("dependencies",)

    def __init__(
        self,
        dependencies: ConfirmationDependencies | None = None,
        **kwargs: object,
    ) -> None:
        if dependencies is not None and kwargs:
            values = {
                name: getattr(dependencies, name)
                for name in ConfirmationDependencies.__dataclass_fields__
            }
            values.update(kwargs)
            dependencies = ConfirmationDependencies(**cast(Any, values))
        elif dependencies is None:
            allowed = set(ConfirmationDependencies.__dataclass_fields__)
            unknown = sorted(name for name in kwargs if name not in allowed)
            if unknown:
                raise TypeError("unknown confirmation dependency: " + ", ".join(unknown))
            dependencies = ConfirmationDependencies(**cast(Any, kwargs))
        self.dependencies = dependencies

    # ---- Ledger-first request identity ---------------------------------

    def _operation(self, operation_id: str) -> object | None:
        repository = self.dependencies.write_operations
        getter = _callable(repository, ("get",))
        if getter is None:
            raise WriteOperationError("operation_unavailable")
        return _invoke(getter, {"operation_id": operation_id, "id": operation_id}, (operation_id,))

    def _preheader(self, request: ConfirmationRequest) -> LedgerOperationPreheader:
        repository = self.dependencies.write_operations
        loader = _callable(repository, ("operation_preheader", "load_operation_preheader"))
        if loader is not None:
            value = _invoke(
                loader,
                {
                    "conversation_id": request.conversation_id,
                    "operation_id": request.operation_id,
                },
                (),
            )
            if not isinstance(value, LedgerOperationPreheader):
                operation = _attribute(value, "operation")
                pointer = _attribute(value, "pending_pointer")
                if operation is None or not isinstance(pointer, LedgerPendingPointer):
                    raise WriteOperationError("operation_unavailable")
                value = LedgerOperationPreheader(cast(Any, operation), pointer)
            return value
        # Compatibility for narrow test doubles with an explicit Ledger id.
        # Production repositories always expose the bounded projection above;
        # an omitted id never falls back to reading a full Pending object.
        if not request.operation_id:
            raise WriteOperationError("operation_unavailable")
        operation = self._operation(request.operation_id)
        if operation is None:
            raise WriteOperationError("operation_result_unknown", retryable=True)
        operation_conversation_id = _attribute(operation, "conversation_id")
        if operation_conversation_id is None:
            raise WriteOperationError("operation_unavailable")
        if operation_conversation_id != request.conversation_id:
            raise WriteOperationError("operation_identity_conflict")
        return LedgerOperationPreheader(
            cast(Any, operation),
            LedgerPendingPointer(
                conversation_id=request.conversation_id,
                operation_id=str(_attribute(operation, "id", "") or ""),
                tool_call_id=str(_attribute(operation, "tool_call_id", "") or ""),
                tool_name=str(_attribute(operation, "tool_name", "") or ""),
                pending_confirmation_claim_id="",
            ),
        )

    def operation_preheader(self, request: ConfirmationRequest) -> LedgerOperationPreheader:
        """Load the bounded Ledger route identity exactly once for Runtime dispatch."""

        if not isinstance(request, ConfirmationRequest):
            raise TypeError("request must be a ConfirmationRequest")
        preheader = self._preheader(request)
        if type(preheader) is not LedgerOperationPreheader:
            raise WriteOperationError("operation_unavailable")
        return preheader

    def _rejection_fingerprint(
        self,
        request: ConfirmationRequest,
        preheader: LedgerOperationPreheader,
    ) -> str:
        operation = preheader.operation
        repository = self.dependencies.write_operations
        key = _attribute(repository, "key")
        if key is None:
            raise WriteOperationError("operation_unavailable")
        stored = str(_attribute(operation, "confirmation_token_fingerprint", "") or "")
        if request.confirmation_token:
            try:
                token_bytes = request.confirmation_token.encode("ascii")
            except UnicodeEncodeError as exc:
                raise WriteOperationError("invalid_confirmation") from exc
            supplied = ledger_fingerprint(
                cast(Any, key),
                "write-operation-confirmation-token-v1",
                token_bytes,
            )
            if not compare_digest(supplied, stored):
                raise WriteOperationError("operation_input_conflict")
        elif request.rejection_feedback_present:
            raise WriteOperationError("invalid_confirmation")
        return operation_request_fingerprint(
            cast(Any, key),
            operation_id=str(_attribute(operation, "id", "") or ""),
            tool_call_id=str(_attribute(operation, "tool_call_id", "") or ""),
            approved=False,
            edited_args_present=False,
            edited_args=None,
            rejection_feedback_present=request.rejection_feedback_present,
            rejection_feedback=request.rejection_feedback,
            confirmation_token_fingerprint=stored,
            proposal_fingerprint=str(_attribute(operation, "proposal_fingerprint", "") or ""),
        )

    def _fingerprint(
        self,
        pending: PendingAction,
        request: ConfirmationRequest,
        token: str,
        operation: object,
    ) -> str:
        repository = self.dependencies.write_operations
        key = _attribute(repository, "key")
        if key is None:
            raise WriteOperationError("operation_unavailable")
        requested_operation_id = request.operation_id or pending.operation_id
        if not isinstance(requested_operation_id, str) or not requested_operation_id:
            raise WriteOperationError("operation_identity_conflict")
        try:
            normalized_id = str(UUID(requested_operation_id))
        except (TypeError, ValueError) as exc:
            raise WriteOperationError("operation_identity_conflict") from exc
        if normalized_id != pending.operation_id:
            raise WriteOperationError("operation_identity_conflict")
        try:
            token_bytes = token.encode("ascii")
        except UnicodeEncodeError as exc:
            raise WriteOperationError("invalid_confirmation") from exc
        token_fingerprint = ledger_fingerprint(
            cast(Any, key),
            "write-operation-confirmation-token-v1",
            token_bytes,
        )
        stored_token_fingerprint = str(
            _attribute(operation, "confirmation_token_fingerprint", "") or ""
        )
        if not compare_digest(token_fingerprint, stored_token_fingerprint):
            raise WriteOperationError("operation_input_conflict")
        return operation_request_fingerprint(
            cast(Any, key),
            operation_id=normalized_id,
            tool_call_id=pending.tool_call_id,
            approved=request.approved,
            edited_args_present=not request.edited_args.is_missing(),
            edited_args=cast(Any, request.edited_args.as_mapping),
            rejection_feedback_present=request.rejection_feedback_present,
            rejection_feedback=request.rejection_feedback,
            confirmation_token_fingerprint=token_fingerprint,
            proposal_fingerprint=str(_attribute(operation, "proposal_fingerprint", "") or ""),
        )

    def _token(self, pending: PendingAction, request: ConfirmationRequest) -> str:
        token = request.confirmation_token
        if token:
            return token
        if not request.edited_args.is_missing() or request.rejection_feedback_present:
            # The public route treats an omitted token together with edited
            # arguments/feedback as request validation, not as a stale
            # Ledger identity.  A supplied-but-wrong token remains the
            # 409/input-conflict path below.
            raise WriteOperationError("invalid_confirmation")
        return _confirmation_token(pending)

    def _pending_for(self, conversation_id: int, supplied: PendingAction | None) -> PendingAction:
        if supplied is not None:
            return supplied
        getter = _callable(self.dependencies.persistence, ("get_pending_action",))
        if getter is None:
            raise WriteOperationError("operation_unavailable")
        pending = _pending(
            _invoke(getter, {"conversation_id": conversation_id}, (conversation_id,))
        )
        if pending is None:
            raise WriteOperationError("stale_pending_action")
        return pending

    def _validate_live_identity(
        self, conversation_id: int, pending: PendingAction, operation: object
    ) -> None:
        if _attribute(operation, "conversation_id") != conversation_id:
            raise WriteOperationError("operation_identity_conflict")
        for field_name in ("tool_call_id", "tool_name"):
            if str(_attribute(operation, field_name, "") or "") != str(
                getattr(pending, field_name)
            ):
                raise WriteOperationError("operation_identity_conflict")
        if (
            not pending.operation_id
            or str(_attribute(operation, "id", "") or "") != pending.operation_id
        ):
            raise WriteOperationError("operation_identity_conflict")

    def terminal_replay(
        self,
        request: ConfirmationRequest,
        *,
        operation: object | None = None,
    ) -> OperationReplay | None:
        """Replay a terminal Ledger row before reading Pending or any model state."""

        if not isinstance(request, ConfirmationRequest):
            raise TypeError("request must be a ConfirmationRequest")
        if not request.approved and not request.edited_args.is_missing():
            raise WriteOperationError("operation_input_conflict")
        if request.approved and request.rejection_feedback_present:
            raise WriteOperationError("operation_input_conflict")
        operation_id = request.operation_id
        if operation is None:
            preheader = self._preheader(request)
            operation = preheader.operation
            if not operation_id:
                operation_id = str(_attribute(operation, "id", "") or "")
        elif not operation_id:
            operation_id = str(_attribute(operation, "id", "") or "")
        if not operation_id:
            raise WriteOperationError("operation_result_unknown", retryable=True)
        try:
            normalized_operation_id = str(UUID(operation_id))
        except (TypeError, ValueError) as exc:
            # An explicitly supplied operation id is part of the Ledger
            # identity.  Reject malformed ids before the repository lookup so
            # a bad terminal-replay request cannot be reported as an infra
            # miss.
            raise WriteOperationError("operation_identity_conflict") from exc
        # Ledger ids are canonical UUID strings.  Accept an equivalent UUID
        # spelling from the transport, then use the canonical value for the
        # repository lookup and every downstream fingerprint.
        operation_id = normalized_operation_id
        assert operation is not None
        if _attribute(operation, "conversation_id") is None:
            raise WriteOperationError("operation_unavailable")
        status = _status(operation)
        if status not in {"proposed", "committed", "rejected", "failed"}:
            raise WriteOperationError("operation_integrity_error")
        if status == "proposed":
            return None
        if not request.confirmation_token:
            # A terminal operation has no Pending row from which to derive a
            # token.  Keep the old route's identity-conflict response instead
            # of treating a replay without proof as a live request.
            raise WriteOperationError("operation_identity_conflict")
        if str(_attribute(operation, "id", "") or "") != normalized_operation_id:
            raise WriteOperationError("operation_identity_conflict")
        if _attribute(operation, "conversation_id") != request.conversation_id:
            raise WriteOperationError("operation_identity_conflict")
        pending = PendingAction(
            str(_attribute(operation, "tool_call_id", "") or ""),
            str(_attribute(operation, "tool_name", "") or ""),
            "",
            str(_attribute(operation, "tool_name", "") or ""),
            operation_id,
        )
        token = self._token(pending, request)
        fingerprint = self._fingerprint(pending, request, token, operation)
        repository = self.dependencies.write_operations
        replay_function = _callable(repository, ("replay",))
        if replay_function is None:
            raise WriteOperationError("operation_unavailable")
        replay = _invoke(
            replay_function,
            {"operation": operation, "request_fingerprint": fingerprint},
            (operation, fingerprint),
        )
        if not isinstance(replay, OperationReplay):
            raise WriteOperationError("operation_result_unknown", retryable=True)
        if replay.delivery_status != "pending":
            return replay
        converge = _callable(repository, ("converge_expired_delivery",))
        if converge is None:
            raise WriteOperationError("operation_delivery_unknown")
        converged = _invoke(
            converge, {"operation_id": operation_id, "id": operation_id}, (operation_id,)
        )
        if isinstance(converged, OperationUnknown):
            raise WriteOperationError(converged.code, retryable=converged.retryable)
        fresh = self._operation(operation_id)
        if fresh is None:
            raise WriteOperationError("operation_result_unknown", retryable=True)
        replayed = _invoke(
            replay_function,
            {"operation": fresh, "request_fingerprint": fingerprint},
            (fresh, fingerprint),
        )
        if not isinstance(replayed, OperationReplay):
            raise WriteOperationError("operation_result_unknown", retryable=True)
        return replayed

    # Friendly alias used by Runtime and tests.
    replay_terminal = terminal_replay

    def replay_outcome(
        self,
        request: ConfirmationRequest,
        *,
        preheader: LedgerOperationPreheader | None = None,
    ) -> OperationReplayOutcome | ConfirmationRequiredOutcome | None:
        operation: object | None = None
        if preheader is not None:
            if type(preheader) is not LedgerOperationPreheader:
                raise TypeError("preheader must be an exact LedgerOperationPreheader")
            operation = preheader.operation
            if _attribute(operation, "conversation_id") != request.conversation_id:
                raise WriteOperationError("operation_identity_conflict")
            if request.operation_id:
                try:
                    requested_id = str(UUID(request.operation_id))
                    operation_id = str(UUID(str(_attribute(operation, "id", "") or "")))
                except (TypeError, ValueError) as exc:
                    raise WriteOperationError("operation_identity_conflict") from exc
                if requested_id != operation_id:
                    raise WriteOperationError("operation_identity_conflict")
        replay = self.terminal_replay(request, operation=operation)
        if replay is None:
            return None
        metadata_operation = operation
        try:
            transport = json.loads(replay.payload.transport_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            transport = None
        if (
            not isinstance(transport, Mapping)
            or not transport.get("tool_call_id")
            or not transport.get("tool_name")
        ):
            metadata_operation = self._operation(replay.operation_id)
        return _runtime_replay(
            replay,
            request.conversation_id,
            operation=metadata_operation,
        )

    def preflight_live(
        self,
        request: ConfirmationRequest,
        *,
        catalog: object | None = None,
    ) -> PendingAction:
        """Validate a live approval before any Conversation/model work.

        The returned Pending is only a detached identity snapshot.  The
        session constructor repeats the Ledger read immediately before the
        claim, so this probe never becomes an authority for execution.
        """

        if not isinstance(request, ConfirmationRequest) or not request.approved:
            raise ValueError("live preflight requires an approved confirmation")
        replay = self.terminal_replay(request)
        if replay is not None:
            raise ConfirmationReplayError(replay)
        live = self._pending_for(request.conversation_id, None)
        operation_id = request.operation_id or live.operation_id
        try:
            operation_id = str(UUID(operation_id))
        except (TypeError, ValueError) as exc:
            raise WriteOperationError("operation_identity_conflict") from exc
        if operation_id != live.operation_id:
            raise WriteOperationError("operation_identity_conflict")
        operation = self._operation(operation_id)
        if operation is None:
            if request.operation_id and request.operation_id != live.operation_id:
                raise WriteOperationError("operation_identity_conflict")
            raise WriteOperationError("operation_result_unknown", retryable=True)
        if _attribute(operation, "conversation_id") is None:
            raise WriteOperationError("operation_unavailable")
        self._validate_live_identity(request.conversation_id, live, operation)
        if _status(operation) != "proposed":
            changed = self.terminal_replay(request, operation=operation)
            if changed is not None:
                raise ConfirmationReplayError(changed)
            raise WriteOperationError("operation_result_unknown", retryable=True)
        if (
            _attribute(operation, "adapter_kind") == "typed"
            and _attribute(operation, "authorization_scope_fingerprint") is None
        ):
            raise WriteOperationError("authorization_scope_unbound")
        token = self._token(live, request)
        if request.confirmation_token and not compare_digest(token, request.confirmation_token):
            raise WriteOperationError("operation_input_conflict")
        self._fingerprint(live, request, token, operation)
        if catalog is not None:
            edited = None if request.edited_args.is_missing() else dict(request.edited_args.as_mapping)
            try:
                prepare_pending_action(live, cast(Any, catalog), cast(Any, edited))
            except ValueError as exc:
                raise WriteOperationError("invalid_confirmation") from exc
        return live

    # ---- Live Pending/session construction ------------------------------

    def _new_session(
        self,
        request: ConfirmationRequest,
        *,
        pending: PendingAction | None,
        approved: bool,
        conversation: object | None = None,
        undo_seed: Mapping[str, Any] | None = None,
        catalog: object | None = None,
    ) -> ConfirmationSession:
        replay = self.terminal_replay(request)
        if replay is not None:
            raise ConfirmationReplayError(replay)
        live = self._pending_for(request.conversation_id, pending)
        operation_id = request.operation_id or live.operation_id
        try:
            operation_id = str(UUID(operation_id))
        except (TypeError, ValueError) as exc:
            raise WriteOperationError("operation_identity_conflict") from exc
        if operation_id != live.operation_id:
            raise WriteOperationError("operation_identity_conflict")
        operation = self._operation(operation_id)
        if operation is None:
            if request.operation_id and request.operation_id != live.operation_id:
                raise WriteOperationError("operation_identity_conflict")
            raise WriteOperationError("operation_result_unknown", retryable=True)
        self._validate_live_identity(request.conversation_id, live, operation)
        if _status(operation) != "proposed":
            # The row changed after the first Ledger-first read.  Re-run the
            # terminal branch with a fresh row; never inspect stale Pending.
            raise ConfirmationReplayError(cast(OperationReplay, self.terminal_replay(request, operation=operation)))
        if (
            approved
            and _attribute(operation, "adapter_kind") == "typed"
            and _attribute(operation, "authorization_scope_fingerprint") is None
        ):
            raise WriteOperationError("authorization_scope_unbound")
        token = self._token(live, request)
        if request.confirmation_token and not compare_digest(token, request.confirmation_token):
            raise WriteOperationError("operation_input_conflict")
        fingerprint = self._fingerprint(live, request, token, operation)
        edited = request.edited_args
        effective = live
        # The Agent Loop's approved continuation is the single
        # schema/decode/capability/binding/preflight boundary.  Running
        # ``prepare_call`` here as well would duplicate provider-side
        # preparation and, more importantly, would let a direct confirmation
        # route execute a different context than the one used by the Ledger
        # coordinator.  ``prepare_pending_action`` above only validates the
        # public editable-field projection.
        preflight_result: object | None = None
        if approved:
            edited_mapping = (
                None if edited.is_missing() else cast(dict[str, JSONValue], dict(edited.as_mapping))
            )
            selected_catalog = catalog if catalog is not None else self.dependencies.catalog
            if selected_catalog is not None:
                # This is the schema/edit boundary.  Reject never enters it.
                try:
                    effective = prepare_pending_action(
                        live, cast(Any, selected_catalog), edited_mapping
                    )
                except ValueError as exc:
                    raise WriteOperationError("invalid_confirmation") from exc
        approval_context: ToolExecutionContext | None = None
        context_resolver = self.dependencies.approval_context_resolver
        if approved and context_resolver is not None:
            effective_digest, effective_revision = pending_action_identity(
                effective.tool_call_id,
                effective.tool_name,
                effective.args,
            )
            try:
                resolved_context = _invoke(
                    context_resolver,
                    {
                        "operation": operation,
                        "pending": effective,
                        "conversation_id": request.conversation_id,
                        "pending_action_revision": effective_revision,
                        "effective_args_digest": effective_digest,
                    },
                    (),
                )
            except WriteOperationError:
                raise
            except Exception as exc:
                raise WriteOperationError(
                    "operation_not_committed", retryable=True
                ) from exc
            if not isinstance(resolved_context, ToolExecutionContext):
                raise WriteOperationError("operation_not_committed", retryable=True)
            approval_context = resolved_context
        identity = ConfirmationIdentity(
            conversation_id=request.conversation_id,
            operation_id=operation_id,
            tool_call_id=live.tool_call_id,
            tool_name=live.tool_name,
            request_fingerprint=fingerprint,
            confirmation_token=token,
            proposal_fingerprint=str(_attribute(operation, "proposal_fingerprint", "") or ""),
        )
        # Rejection is intentionally a Ledger/Pending-only path.  When the
        # caller did not already provide a trusted Conversation snapshot,
        # leave the generation unset and let the atomic delivery adapter read
        # it under its own transaction.  Loading a Conversation here would
        # violate the preheader rejection boundary and could turn a valid
        # Ledger CAS into a model/conversation failure.
        if approved:
            # Approval is Ledger/authority-only.  The Conversation generation
            # used by delivery CAS is established from the canonical reload
            # after terminal execution and ownership, when the fresh Segment
            # is activated; never load the pre-approval Conversation here.
            generation = None
        else:
            candidate_generation = _attribute(conversation, "updated_at")
            generation = candidate_generation if isinstance(candidate_generation, datetime) else None
        state = ConfirmationState(
            identity=identity,
            pending=live,
            effective_pending=effective,
            approved=approved,
            edited_args=edited,
            rejection_feedback=request.rejection_feedback,
            undo_seed=dict(undo_seed or {}),
            prepared_call=preflight_result,
            approval_context=approval_context,
            continuation_generation=generation,
        )
        live_session: ConfirmationSession | None = None

        def attempt(
            action: PendingAction,
            prepared: PreparedToolCall[Any, Any] | None,
        ) -> ToolFailure | None:
            with state.lock:
                if not state.active or state.cancelled or state.timed_out:
                    return ToolFailure("stale_state", "confirmation_claim_lost")
                # A Ledger confirmation must acquire a real delivery lease
                # before it can claim Pending or reach the executor.  The
                # legacy deterministic adapters have their own explicit
                # bridge; this model confirmation path never falls back to an
                # unfenced ownership=None delivery.
                if not callable(_attribute(self.dependencies.write_operations, "heartbeat")):
                    raise WriteOperationError("operation_unavailable")
                if prepared is not None and (
                    _attribute(prepared, "pending_identity") is None
                    or _attribute(prepared, "pending_action_revision") is None
                ):
                    return ToolFailure("conflict", "confirmation_claim_failed")
                current = self._pending_for(request.conversation_id, None)
                if (
                    current.operation_id != state.pending.operation_id
                    or current.tool_call_id != state.pending.tool_call_id
                    or current.tool_name != state.pending.tool_name
                    # Reject is deliberately a token/Pending/Ledger identity
                    # path.  Compare the persisted argument bytes directly so
                    # a malformed proposal cannot enter schema/JSON decoding.
                    or not compare_digest(
                        current.args.encode("utf-8"), state.pending.args.encode("utf-8")
                    )
                ):
                    state.cas_lost = True
                    return ToolFailure("stale_state", "confirmation_claim_lost")
                state.claim_id = state.identity.operation_id
                state.confirmation_attempted = True
                if prepared is None:
                    visible = self._rejection_result(state.rejection_feedback)
                    execution = self._reject(state, visible)
                    state.terminal_execution = execution
                    if isinstance(execution, OperationReplay):
                        # The rejection CAS can race a worker that terminalizes
                        # the same operation after the Ledger-first probe.  A
                        # replay has no delivery ownership to lease; leave it
                        # for the Runtime replay branch instead of treating
                        # the missing owner as an infrastructure failure.
                        state.replayed = True
                        return None
                    if state.approval_decided_callback is not None:
                        state.approval_decided_callback(None)
                    if state.approval_resume_callback is not None:
                        state.approval_resume_callback()
                    self._set_ownership(state, execution)
                    return None
                return None

        def result(
            action: PendingAction,
            approved_result: bool,
            tool_message: Message,
            execution_record: ToolExecutionRecord[Any, Any] | None,
        ) -> object:
            value = self.record_result(
                state, action, approved_result, tool_message, execution_record
            )
            # A timeout can return while the provider/transaction worker is
            # still inside ``execute_primary``.  Once that late worker has a
            # terminal payload, converge the fenced fallback here before the
            # unchanged Agent can load history or start a follow-up model
            # turn.  A timeout that already delivered its fallback is
            # inactive and therefore remains idempotent.
            if live_session is not None:
                with state.lock:
                    late_terminal = (
                        state.timed_out
                        and state.active
                        and state.origin_tool_message is not None
                        and state.delivery_ownership is not None
                    )
                if late_terminal:
                    try:
                        late_delivery = self.fallback(live_session)
                        if _status(late_delivery) in {
                            PersistenceStatus.PERSISTED.value,
                            PersistenceStatus.DUPLICATE.value,
                        }:
                            with state.lock:
                                state.fallback_persisted = True
                    except Exception:
                        # The Ledger terminal is authoritative even when this
                        # late delivery attempt cannot acquire its fence.  A
                        # subsequent replay/takeover converges the pending
                        # delivery without rerunning the provider.
                        pass
            return value

        def execute(
            prepared: PreparedToolCall[Any, Any],
            tool_context: object,
            prepare_identity: object,
        ) -> ToolExecutionRecord[Any, Any]:
            return self.execute_operation(state, prepared, tool_context, prepare_identity)

        live_session = ConfirmationSession(
            state=state,
            on_confirmation_attempt=attempt,
            on_confirmation_result=result,
            execute_operation=execute,
            delivery_fence=lambda: self.delivery_fence(state),
        )
        return live_session

    def approve_modify(
        self,
        request: ConfirmationRequest,
        *,
        pending: PendingAction | None = None,
        conversation: object | None = None,
        undo_seed: Mapping[str, Any] | None = None,
        catalog: object | None = None,
    ) -> ConfirmationSession:
        if not request.approved:
            raise ValueError("approve_modify requires approved=true")
        return self._new_session(
            request,
            pending=pending,
            approved=True,
            conversation=conversation,
            undo_seed=undo_seed,
            catalog=catalog,
        )

    def reject(
        self,
        request: ConfirmationRequest,
        *,
        pending: PendingAction | None = None,
        conversation: object | None = None,
    ) -> ConfirmationSession:
        if request.approved:
            raise ValueError("reject requires approved=false")
        del pending, conversation
        if not request.edited_args.is_missing():
            raise WriteOperationError("operation_input_conflict")
        if not request.confirmation_token and request.rejection_feedback_present:
            raise WriteOperationError("invalid_confirmation")
        preheader = self._preheader(request)
        operation = preheader.operation
        if _status(operation) != "proposed":
            replay = self.terminal_replay(request, operation=operation)
            if replay is not None:
                raise ConfirmationReplayError(replay)
            raise WriteOperationError("operation_result_unknown", retryable=True)
        pointer = preheader.pending_pointer
        if pointer is None:
            raise WriteOperationError("operation_identity_conflict")
        operation_id = str(_attribute(operation, "id", "") or "")
        if (
            _attribute(operation, "conversation_id") != request.conversation_id
            or pointer.conversation_id != request.conversation_id
            or pointer.operation_id != operation_id
            or pointer.tool_call_id != str(_attribute(operation, "tool_call_id", "") or "")
            or pointer.tool_name != str(_attribute(operation, "tool_name", "") or "")
            or pointer.pending_confirmation_claim_id != ""
        ):
            raise WriteOperationError("operation_identity_conflict")
        fingerprint = self._rejection_fingerprint(request, preheader)
        rejected_pending = PendingAction(
            pointer.tool_call_id,
            pointer.tool_name,
            "",
            pointer.tool_name,
            pointer.operation_id,
        )
        state = ConfirmationState(
            identity=ConfirmationIdentity(
                conversation_id=request.conversation_id,
                operation_id=operation_id,
                tool_call_id=pointer.tool_call_id,
                tool_name=pointer.tool_name,
                request_fingerprint=fingerprint,
                confirmation_token=request.confirmation_token or "",
                proposal_fingerprint=str(
                    _attribute(operation, "proposal_fingerprint", "") or ""
                ),
            ),
            pending=rejected_pending,
            effective_pending=rejected_pending,
            approved=False,
            edited_args=request.edited_args,
            rejection_feedback=request.rejection_feedback,
        )

        def attempt(
            action: PendingAction,
            prepared: PreparedToolCall[Any, Any] | None,
        ) -> ToolFailure | None:
            del action
            if prepared is not None:
                return ToolFailure("conflict", "confirmation_claim_failed")
            with state.lock:
                if not state.active or state.cancelled or state.timed_out:
                    return ToolFailure("stale_state", "confirmation_claim_lost")
                state.confirmation_attempted = True
                state.claim_id = operation_id
            execution = self._reject(state, self._rejection_result(state.rejection_feedback))
            with state.lock:
                state.terminal_execution = execution
                if isinstance(execution, OperationReplay):
                    state.replayed = True
                elif _attribute(execution, "ownership") is None:
                    # Parameter-free rejection atom already persisted the
                    # deterministic origin/assistant pair and closed delivery.
                    state.delivered = True
                    state.active = False
                else:
                    self._set_ownership(state, execution)
            if not isinstance(execution, OperationReplay):
                if state.approval_decided_callback is not None:
                    state.approval_decided_callback(None)
                if state.approval_resume_callback is not None:
                    state.approval_resume_callback()
            return None

        def result(
            action: PendingAction,
            approved_result: bool,
            tool_message: Message,
            execution_record: ToolExecutionRecord[Any, Any] | None,
        ) -> object:
            with state.lock:
                atomically_delivered = state.delivered
            if atomically_delivered:
                return None
            return self.record_result(
                state,
                action,
                approved_result,
                tool_message,
                execution_record,
            )

        return ConfirmationSession(
            state=state,
            on_confirmation_attempt=attempt,
            on_confirmation_result=result,
            execute_operation=lambda *_args: (_ for _ in ()).throw(
                WriteOperationError("operation_unavailable")
            ),
            delivery_fence=lambda: False,
        )

    # ---- Ledger execution/result atoms ----------------------------------

    def _reject(self, state: ConfirmationState, visible_result: str) -> OperationExecution:
        coordinator = _callable(self.dependencies.write_coordinator, ("reject_primary",))
        if coordinator is None:
            raise WriteOperationError("operation_unavailable")
        execution = _invoke(
            coordinator,
            {
                "operation_id": state.identity.operation_id,
                "conversation_id": state.identity.conversation_id,
                "tool_call_id": state.identity.tool_call_id,
                "tool_name": state.identity.tool_name,
                "request_fingerprint": state.identity.request_fingerprint,
                "visible_result": visible_result,
                "confirmation_token": state.identity.confirmation_token or None,
            },
            (),
        )
        if not isinstance(execution, (OperationCommitted, OperationFailed, OperationReplay, OperationUnknown)) and not (
            _attribute(execution, "operation_id") is not None
            and _attribute(execution, "payload") is not None
        ):
            raise WriteOperationError("operation_result_unknown", retryable=True)
        if isinstance(execution, OperationUnknown):
            raise WriteOperationError(
                _public_confirmation_error_code(execution.code),
                retryable=execution.retryable,
            )
        return cast(OperationExecution, execution)

    def execute_operation(
        self,
        state: ConfirmationState,
        prepared: PreparedToolCall[Any, Any],
        tool_context: object,
        prepare_identity: object,
    ) -> ToolExecutionRecord[Any, Any]:
        with state.lock:
            if state.cancelled or state.timed_out or not state.active:
                raise WriteOperationError("confirmation_claim_lost")
        coordinator = _callable(self.dependencies.write_coordinator, ("execute_primary",))
        if coordinator is None:
            raise WriteOperationError("operation_unavailable")
        transactional_delivery = self.dependencies.transactional_delivery
        register_delivery = _callable(transactional_delivery, ("register",))
        unregister_delivery = _callable(transactional_delivery, ("unregister",))
        registered_delivery = False
        registered_delivery_handle: object | None = None
        if register_delivery is not None:
            registered_delivery_handle = _invoke(register_delivery, {"state": state}, (state,))
            registered_delivery = True
        values: dict[str, object] = {
            "operation_id": state.identity.operation_id,
            "conversation_id": state.identity.conversation_id,
            "prepared": prepared,
            "context": tool_context,
            "prepare_identity": prepare_identity,
            "request_fingerprint": state.identity.request_fingerprint,
            "edited_args_present": not state.edited_args.is_missing(),
            "edited_args": (
                None if state.edited_args.is_missing() else state.edited_args.as_mapping
            ),
            "approval_decided_callback": state.approval_decided_callback,
        }
        try:
            execution, record = cast(
                tuple[OperationExecution, ToolExecutionRecord[Any, Any] | None],
                _invoke(
                    coordinator,
                    values,
                    (),
                ),
            )
        finally:
            if registered_delivery and unregister_delivery is not None:
                _invoke(
                    unregister_delivery,
                    {
                        "state": state,
                        "handle": registered_delivery_handle,
                        "registration": registered_delivery_handle,
                    },
                    (state, registered_delivery_handle),
                )
        if _terminal(execution):
            state.terminal_execution = execution
            self._set_ownership(state, execution)
            if state.approval_resume_callback is not None:
                state.approval_resume_callback()
        if record is not None:
            return record
        if isinstance(execution, OperationReplay):
            state.replayed = True
            raise ConfirmationReplayError(execution)
        if isinstance(execution, OperationUnknown):
            raise WriteOperationError(
                _public_confirmation_error_code(execution.code),
                retryable=execution.retryable,
            )
        raise WriteOperationError("operation_result_unknown", retryable=True)

    def record_result(
        self,
        state: ConfirmationState,
        action: PendingAction,
        approved: bool,
        tool_message: Message,
        execution_record: ToolExecutionRecord[Any, Any] | None,
    ) -> object:
        with state.lock:
            if not state.active or state.cancelled:
                return None
            if state.claim_id is None:
                state.cas_lost = True
                raise WriteOperationError("confirmation_claim_lost")
            state.origin_tool_message = tool_message
            state.execution_record = execution_record
            state.approved = approved
            terminal_status = str(
                _attribute(_attribute(state.terminal_execution, "payload"), "status", "")
                or ""
            )
            state.succeeded = approved and (
                terminal_status == "committed" or _record_succeeded(execution_record)
            )
            state.replayed = bool(_attribute(execution_record, "replayed", False))
            state.undo_update = dict(state.undo) if state.succeeded and state.undo else None
            if state.terminal_execution is not None and _attribute(
                _attribute(state.terminal_execution, "payload"), "undo_json"
            ):
                raw_undo = _attribute(_attribute(state.terminal_execution, "payload"), "undo_json")
                try:
                    decoded = json.loads(cast(str, raw_undo))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise WriteOperationError("operation_integrity_error") from exc
                if not isinstance(decoded, Mapping):
                    raise WriteOperationError("operation_integrity_error")
                state.undo = dict(decoded)
                state.undo_update = dict(decoded)
            if not approved:
                state.undo_update = None
            elif not state.succeeded:
                # An approved write that failed is terminal and must clear any
                # previously exposed undo atom rather than leaving stale
                # recovery data attached to the conversation.
                state.undo_update = {}
            return None

    # ---- Ownership, timeout, cancellation, delivery ---------------------

    def _set_ownership(self, state: ConfirmationState, execution: object) -> None:
        ownership = _attribute(execution, "ownership")
        if not isinstance(ownership, DeliveryOwnership):
            raise WriteOperationError("operation_delivery_unknown", retryable=True)
        heartbeat = _attribute(self.dependencies.write_operations, "heartbeat")
        if not callable(heartbeat):
            # A terminal Ledger row without a lease heartbeat is not safe to
            # deliver: a second worker could take over while this worker is
            # still publishing messages.  Production composition must expose
            # the existing repository atom; test adapters must implement it
            # explicitly rather than receiving an un-fenced fallback.
            raise WriteOperationError("operation_unavailable")
        with state.lock:
            if (
                state.cancelled
                or state.delivery_ownership is not None
            ):
                return
            state.delivery_ownership = ownership
            # Existing DeliveryHeartbeat owns its own retry/stop loop. It is
            # installed and started while the state lock is held, so a
            # concurrent timeout/cancel cannot miss it between ownership
            # assignment and thread creation.
            state.delivery_heartbeat = DeliveryHeartbeat(
                cast(Any, self.dependencies.write_operations), ownership
            ).start()

    def delivery_fence(self, state: ConfirmationState) -> bool:
        with state.lock:
            if state.cancelled or state.cas_lost or not state.active:
                return False
            heartbeat = state.delivery_heartbeat
        result = heartbeat is not None and heartbeat.fence()
        return result

    def stop_heartbeat(self, state: ConfirmationState | ConfirmationSession) -> None:
        if isinstance(state, ConfirmationSession):
            state = state.state
        with state.lock:
            heartbeat = state.delivery_heartbeat
            state.delivery_heartbeat = None
        if heartbeat is not None:
            heartbeat.stop()

    def timeout_convergence(self, session: ConfirmationSession) -> PersistenceResult | None:
        """Converge a timed-out owning request without starting new work."""

        state = session.state
        with state.lock:
            state.timed_out = True
            if state.cancelled or state.fallback_persisted:
                return None
            self._hydrate_terminal_undo(state)
            if state.origin_tool_message is None and state.terminal_execution is not None:
                terminal_payload = _attribute(state.terminal_execution, "payload")
                visible = str(_attribute(terminal_payload, "visible_result", "") or "")
                state.origin_tool_message = Message(
                    role="tool",
                    content=visible,
                    tool_call_id=state.identity.tool_call_id,
                )
            attempted = state.confirmation_attempted
            ready = attempted and state.origin_tool_message is not None
        if not ready:
            # No claim means there cannot be a late executor callback.  Close
            # this session now; a subsequent confirmation request will read
            # the still-proposed Ledger row as a fresh attempt.  Once a claim
            # exists, keep the session active so a worker that is already in
            # the transaction can publish its late terminal/fallback bundle.
            if not attempted:
                with state.lock:
                    state.active = False
                self._close_approval_context(state)
            return None
        result = self.final_delivery(
            session,
            DeliveryBundle((Message(role="assistant", content=self._fallback_message(state)),)),
            failure_code="operation_delivery_failed",
        )
        if _status(result) == PersistenceStatus.PERSISTED.value:
            state.fallback_persisted = True
        return cast(PersistenceResult | None, result)

    timeout = timeout_convergence
    converge_timeout = timeout_convergence

    def cancel_cleanup(self, session: ConfirmationSession) -> None:
        state = session.state
        with state.lock:
            if state.active:
                state.cancelled = True
                state.active = False
        self._close_approval_context(state)
        session.close_continuation_segment()
        self.stop_heartbeat(state)

    @staticmethod
    def _close_approval_context(state: ConfirmationState) -> None:
        with state.lock:
            context = state.approval_context
            state.approval_context = None
        if context is not None:
            context.authority_factory.close()

    @staticmethod
    def _hydrate_terminal_undo(state: ConfirmationState) -> None:
        terminal = state.terminal_execution
        terminal_payload = _attribute(terminal, "payload")
        terminal_status = str(_attribute(terminal_payload, "status", "") or "")
        if terminal_status in {"committed", "failed", "rejected"}:
            state.succeeded = terminal_status == "committed"
        raw_undo = _attribute(_attribute(terminal, "payload"), "undo_json")
        if not raw_undo:
            return
        try:
            decoded = json.loads(cast(str, raw_undo))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WriteOperationError("operation_integrity_error") from exc
        if not isinstance(decoded, Mapping):
            raise WriteOperationError("operation_integrity_error")
        state.undo = dict(decoded)
        if state.succeeded or terminal_status == "committed":
            state.undo_update = dict(decoded)

    cancel = cancel_cleanup
    cleanup_cancel = cancel_cleanup

    def final_delivery(
        self,
        session: ConfirmationSession,
        bundle: DeliveryBundle | Sequence[Message] = (),
        *,
        pending: PendingAction | None = None,
        clarification: tuple[PendingAction, str] | None = None,
        failure_code: str | None = None,
        pending_authority_claim: PendingAuthorityClaim | None = None,
    ) -> PersistenceResult | object | None:
        """Persist exactly one origin+continuation bundle under the owner fence."""

        state = session.state

        def close_segment_on_failure() -> None:
            # A post-terminal Segment is one-shot.  Any delivery CAS/transport
            # failure must release it before the caller can retry/replay; the
            # successful atom closes it below.
            session.close_continuation_segment()

        # Final delivery starts only after the approved origin has returned a
        # terminal result.  Revoke its one-shot authority before any durable
        # continuation branch, including every delivery short-circuit.
        self._close_approval_context(state)
        if isinstance(bundle, DeliveryBundle):
            delivery = bundle
        else:
            delivery = DeliveryBundle(
                tuple(_message(item) for item in bundle),
                pending,
                clarification,
                pending_authority_claim,
            )
        fence_lost = False
        ownership_missing = False
        with state.lock:
            if (
                state.delivered
                or state.cancelled
                or state.cas_lost
                or state.delivery_in_progress
            ):
                close_segment_on_failure()
                return None
            if state.origin_tool_message is None:
                # A terminal replay has no continuation owner and must not
                # fabricate operation-bound messages.
                self.stop_heartbeat(state)
                return None
            if not isinstance(state.delivery_ownership, DeliveryOwnership):
                # Model confirmations are never allowed to use the old
                # ownership=None persistence atom.  The deterministic legacy
                # bridge is explicit and does not enter this coordinator.
                ownership_missing = True
            elif not self.delivery_fence(state):
                state.cas_lost = True
                fence_lost = True
            if ownership_missing or fence_lost:
                generation = None
                ownership = None
                expected_pending = None
                claim_id = None
                undo = None
            else:
                generation = state.continuation_generation
                ownership = state.delivery_ownership
                expected_pending = state.pending
                claim_id = state.claim_id
                undo = dict(state.undo_update) if state.undo_update is not None else None
                state.delivery_in_progress = True
        if ownership_missing:
            self.stop_heartbeat(state)
            close_segment_on_failure()
            raise WriteOperationError("operation_unavailable")
        if fence_lost:
            self.stop_heartbeat(state)
            close_segment_on_failure()
            return None
        persistence_object = self.dependencies.persistence
        if persistence_object is None:
            with state.lock:
                state.delivery_in_progress = False
            self.stop_heartbeat(state)
            close_segment_on_failure()
            raise WriteOperationError("operation_unavailable")
        values = tuple(delivery.messages)
        chained_pending = pending if pending is not None else delivery.pending
        chained_pending_claim = (
            pending_authority_claim
            if pending is not None
            else delivery.pending_authority_claim
        )
        clarification_value = clarification if clarification is not None else delivery.clarification
        try:
            persistence = _attribute(persistence_object, "persist_confirmation_delivery")
            if callable(persistence):
                kwargs: dict[str, object] = {
                    "conversation_id": state.identity.conversation_id,
                    "ownership": ownership,
                    "origin_tool_message": state.origin_tool_message,
                    "continuation": values,
                    "chained_pending": chained_pending,
                    "clarification": clarification_value,
                    "expected_generation": generation,
                    "expected_pending": expected_pending,
                    "claim_id": claim_id,
                    "undo": undo,
                    "delivery_failure_code": failure_code,
                    "pending_authority_claim": chained_pending_claim,
                }
            else:
                persistence = _attribute(persistence_object, "persist_confirmation_continuation")
                if not callable(persistence):
                    with state.lock:
                        state.delivery_in_progress = False
                    self.stop_heartbeat(state)
                    close_segment_on_failure()
                    raise WriteOperationError("operation_unavailable")
                kwargs = {
                    "conversation_id": state.identity.conversation_id,
                    "expected_generation": generation,
                    "messages": values,
                    "pending": chained_pending,
                    "clarification": clarification_value,
                    "delivery_ownership": ownership,
                    "delivery_failure_code": failure_code,
                    "expected_pending": expected_pending,
                    "claim_id": claim_id,
                    "origin_message": state.origin_tool_message,
                    "undo": undo,
                    "pending_authority_claim": chained_pending_claim,
                }
        except BaseException:
            with state.lock:
                state.delivery_in_progress = False
            self.stop_heartbeat(state)
            close_segment_on_failure()
            raise
        try:
            raw = _invoke(cast(Callable[..., object], persistence), kwargs, ())
        except Exception:
            with state.lock:
                state.delivery_in_progress = False
            self.stop_heartbeat(state)
            close_segment_on_failure()
            raise
        except BaseException:
            with state.lock:
                state.delivery_in_progress = False
            self.stop_heartbeat(state)
            close_segment_on_failure()
            raise
        try:
            status = _attribute(raw, "status")
            status_value = str(getattr(status, "value", status or ""))
        except BaseException:
            with state.lock:
                state.delivery_in_progress = False
            self.stop_heartbeat(state)
            close_segment_on_failure()
            raise
        if status_value == "":
            with state.lock:
                state.delivery_in_progress = False
            self.stop_heartbeat(state)
            close_segment_on_failure()
            raise WriteOperationError("operation_delivery_unknown", retryable=True)
        if status_value in {PersistenceStatus.CAS_LOST.value, "cas_lost"}:
            with state.lock:
                state.cas_lost = True
                state.delivery_in_progress = False
            self.stop_heartbeat(state)
            close_segment_on_failure()
            return raw
        if status_value in {
            PersistenceStatus.CLOSED.value,
            PersistenceStatus.NOT_FOUND.value,
        }:
            with state.lock:
                state.delivery_in_progress = False
            self.stop_heartbeat(state)
            close_segment_on_failure()
            return raw
        if status_value not in {
            PersistenceStatus.PERSISTED.value,
            PersistenceStatus.DUPLICATE.value,
        }:
            with state.lock:
                state.delivery_in_progress = False
            self.stop_heartbeat(state)
            close_segment_on_failure()
            raise WriteOperationError("operation_delivery_unknown", retryable=True)
        try:
            next_generation = _attribute(raw, "generation")
        except BaseException:
            with state.lock:
                state.delivery_in_progress = False
            self.stop_heartbeat(state)
            close_segment_on_failure()
            raise
        with state.lock:
            if isinstance(next_generation, datetime):
                state.continuation_generation = next_generation
            state.delivered = True
            state.delivery_in_progress = False
            state.delivery_result = raw if isinstance(raw, PersistenceResult) else None
            state.active = False
        session.close_continuation_segment()
        self.stop_heartbeat(state)
        return raw

    deliver = final_delivery
    persist_delivery = final_delivery

    def fallback(
        self,
        session: ConfirmationSession,
        *,
        message: str | None = None,
    ) -> PersistenceResult | object | None:
        state = session.state
        return self.final_delivery(
            session,
            DeliveryBundle((Message(role="assistant", content=message or self._fallback_message(state)),)),
            failure_code="operation_delivery_failed",
        )

    # ---- detached source/replay helpers ----------------------------------

    @staticmethod
    def _rejection_result(feedback: str) -> str:
        if feedback.strip():
            return "已取消这次操作，并会按你的反馈保持不变。"
        return "已取消这次操作。你可以告诉我下一步想怎么做。"

    @staticmethod
    def _fallback_message(state: ConfirmationState) -> str:
        if state.succeeded:
            return "写入已完成，但暂时无法生成后续说明。你可以刷新数据查看结果。"
        if state.approved:
            return "写入未完成，错误结果已记录。请检查输入后重试。"
        return ConfirmationCoordinator._rejection_result(state.rejection_feedback)


__all__ = [
    "ConfirmationAttempt",
    "ConfirmationCoordinator",
    "ConfirmationDependencies",
    "ConfirmationIdentity",
    "ConfirmationOperationRepository",
    "ConfirmationPendingReader",
    "ConfirmationPersistenceAdapter",
    "ConfirmationReplayError",
    "ConfirmationResult",
    "ConfirmationSession",
    "ConfirmationState",
    "ConfirmationWriteCoordinator",
    "DeliveryBundle",
]
