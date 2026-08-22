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
  intentionally narrow so ``resume_after_confirm`` remains behind the Agent
  Driver boundary.
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
from typing import Any, cast
from uuid import UUID

from offerpilot.ai.agent import PendingAction, prepare_pending_action
from offerpilot.ai.tool_runtime.contracts import (
    ExecutionAuthorization,
    JSONValue,
    PreparedToolCall,
    ToolExecutionRecord,
    ToolFailure,
    ToolSuccess,
)
from offerpilot.ai.types import Message, ToolCall
from offerpilot.ai.write_operations import (
    DeliveryHeartbeat,
    DeliveryOwnership,
    OperationCommitted,
    OperationExecution,
    OperationFailed,
    OperationReplay,
    OperationUnknown,
    WriteOperationError,
    ledger_fingerprint,
    operation_request_fingerprint,
)

from .contracts import (
    ConfirmationRequest,
    EditedArgs,
    OperationReplayOutcome,
    freeze_json_mapping,
)
from .persistence import PersistenceResult, PersistenceStatus


ConfirmationAttempt = Callable[
    [PendingAction, PreparedToolCall[Any, Any] | None],
    ExecutionAuthorization | ToolFailure | None,
]
ConfirmationResult = Callable[
    [PendingAction, bool, Message, ToolExecutionRecord[Any, Any] | None],
    object,
]
LedgerExecutor = Callable[
    [PreparedToolCall[Any, Any], object, ExecutionAuthorization],
    ToolExecutionRecord[Any, Any],
]
ContinuationLoader = Callable[[], Sequence[Message]]


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

    persistence: object | None = None
    write_operations: object | None = None
    write_coordinator: object | None = None
    conversations: object | None = None
    catalog: object | None = None
    prepare_call: Callable[..., object] | None = field(default=None, repr=False, compare=False)
    undo_seed_builder: Callable[..., Mapping[str, Any]] | None = field(
        default=None, repr=False, compare=False
    )
    undo_builder: Callable[..., Mapping[str, Any] | None] | None = field(
        default=None, repr=False, compare=False
    )
    source_loader: object | None = None
    context_assembler: object | None = None
    journal: object | None = None
    applications: object | None = None
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
    terminal_execution: OperationExecution | None = field(default=None, repr=False)
    origin_tool_message: Message | None = field(default=None, repr=False)
    execution_record: ToolExecutionRecord[Any, Any] | None = field(default=None, repr=False)
    succeeded: bool = False
    replayed: bool = False
    undo: Mapping[str, Any] = field(default_factory=dict, repr=False)
    undo_update: Mapping[str, Any] | None = field(default=None, repr=False)
    undo_operation_id: str = field(default="", repr=False)
    prepared_call: object | None = field(default=None, repr=False, compare=False)
    delivery_ownership: DeliveryOwnership | None = field(default=None, repr=False)
    delivery_heartbeat: DeliveryHeartbeat | None = field(default=None, repr=False)
    continuation_generation: datetime | None = field(default=None, repr=False)
    fallback_persisted: bool = False
    cas_lost: bool = False
    timed_out: bool = False
    cancelled: bool = False
    delivered: bool = False
    active: bool = True
    lock: RLock = field(default_factory=RLock, repr=False, compare=False)


@dataclass(frozen=True, slots=True, repr=False)
class DeliveryBundle:
    """Detached continuation result awaiting one fenced delivery commit."""

    messages: tuple[Message, ...]
    pending: PendingAction | None = None
    clarification: tuple[PendingAction, str] | None = None

    def __post_init__(self) -> None:
        if type(self.messages) is not tuple:
            raise TypeError("messages must be a tuple")
        if any(not isinstance(item, Message) for item in self.messages):
            raise TypeError("messages must contain Message values")
        if self.pending is not None and self.clarification is not None:
            raise ValueError("pending and clarification cannot both be delivered")


@dataclass(slots=True, repr=False)
class ConfirmationSession:
    """Callbacks passed to the unchanged Agent ``resume_after_confirm`` path."""

    state: ConfirmationState
    on_confirmation_attempt: ConfirmationAttempt
    on_confirmation_result: ConfirmationResult
    execute_operation: LedgerExecutor
    continuation_message_loader: ContinuationLoader
    delivery_fence: Callable[[], bool]

    @property
    def pending(self) -> PendingAction:
        return self.state.effective_pending

    @property
    def request_fingerprint(self) -> str:
        return self.state.identity.request_fingerprint


class ConfirmationReplayError(RuntimeError):
    """Raised by the Ledger callback when the operation was already terminal."""

    def __init__(self, replay: OperationReplay) -> None:
        super().__init__("operation replay")
        self.replay = replay


def _runtime_replay(replay: OperationReplay, conversation_id: int) -> OperationReplayOutcome:
    payload = replay.payload
    if payload.status == "committed":
        write_status = "success"
        message = replay.final_message or payload.visible_result
    elif payload.status == "rejected":
        write_status = "cancelled"
        message = replay.final_message or payload.visible_result
    else:
        write_status = "failed"
        message = replay.final_message or payload.visible_result
    undo: Mapping[str, JSONValue] | None = None
    if payload.undo_json:
        decoded = json.loads(payload.undo_json)
        if isinstance(decoded, Mapping):
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
        getter = _callable(self.dependencies.write_operations, ("get",))
        if getter is None:
            return None
        return _invoke(getter, {"operation_id": operation_id, "id": operation_id}, (operation_id,))

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
        if requested_operation_id != pending.operation_id:
            raise WriteOperationError("operation_identity_conflict")
        try:
            normalized_id = str(UUID(requested_operation_id))
        except (TypeError, ValueError) as exc:
            raise WriteOperationError("operation_identity_conflict") from exc
        if normalized_id != requested_operation_id:
            raise WriteOperationError("operation_identity_conflict")
        token_fingerprint = ledger_fingerprint(
            cast(Any, key),
            "write-operation-confirmation-token-v1",
            token.encode("ascii"),
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
            raise WriteOperationError("operation_input_conflict")
        return _confirmation_token(pending)

    def _pending_for(self, conversation_id: int, supplied: PendingAction | None) -> PendingAction:
        if supplied is not None:
            return supplied
        getter = _callable(self.dependencies.persistence, ("get_pending_action",))
        if getter is None:
            raise WriteOperationError("operation_unavailable")
        pending = _pending(_invoke(getter, {"conversation_id": conversation_id}, (conversation_id,)))
        if pending is None:
            raise WriteOperationError("stale_pending_action")
        return pending

    def _validate_live_identity(self, conversation_id: int, pending: PendingAction, operation: object) -> None:
        if _attribute(operation, "conversation_id") != conversation_id:
            raise WriteOperationError("operation_identity_conflict")
        for field_name in ("tool_call_id", "tool_name"):
            if str(_attribute(operation, field_name, "") or "") != str(
                getattr(pending, field_name)
            ):
                raise WriteOperationError("operation_identity_conflict")
        if not pending.operation_id or str(_attribute(operation, "id", "") or "") != pending.operation_id:
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
        if not operation_id:
            return None
        operation = operation if operation is not None else self._operation(operation_id)
        if operation is None:
            raise WriteOperationError("operation_result_unknown", retryable=True)
        status = _status(operation)
        if status == "proposed":
            return None
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
        replay = _invoke(replay_function, {"operation": operation, "request_fingerprint": fingerprint}, (operation, fingerprint))
        if not isinstance(replay, OperationReplay):
            raise WriteOperationError("operation_result_unknown", retryable=True)
        if replay.delivery_status != "pending":
            return replay
        converge = _callable(repository, ("converge_expired_delivery",))
        if converge is None:
            raise WriteOperationError("operation_delivery_unknown")
        converged = _invoke(converge, {"operation_id": operation_id, "id": operation_id}, (operation_id,))
        if isinstance(converged, OperationUnknown):
            raise WriteOperationError(converged.code, retryable=converged.retryable)
        fresh = self._operation(operation_id)
        if fresh is None:
            raise WriteOperationError("operation_result_unknown", retryable=True)
        replayed = _invoke(replay_function, {"operation": fresh, "request_fingerprint": fingerprint}, (fresh, fingerprint))
        if not isinstance(replayed, OperationReplay):
            raise WriteOperationError("operation_result_unknown", retryable=True)
        return replayed

    # Friendly alias used by Runtime and tests.
    replay_terminal = terminal_replay

    def replay_outcome(self, request: ConfirmationRequest) -> OperationReplayOutcome | None:
        replay = self.terminal_replay(request)
        return None if replay is None else _runtime_replay(replay, request.conversation_id)

    # ---- Live Pending/session construction ------------------------------

    def _new_session(
        self,
        request: ConfirmationRequest,
        *,
        pending: PendingAction | None,
        approved: bool,
        conversation: object | None = None,
        undo_seed: Mapping[str, Any] | None = None,
        source_loader: ContinuationLoader | None = None,
        catalog: object | None = None,
    ) -> ConfirmationSession:
        replay = self.terminal_replay(request)
        if replay is not None:
            raise ConfirmationReplayError(replay)
        live = self._pending_for(request.conversation_id, pending)
        operation_id = request.operation_id or live.operation_id
        operation = self._operation(operation_id)
        if operation is None:
            raise WriteOperationError("operation_result_unknown", retryable=True)
        self._validate_live_identity(request.conversation_id, live, operation)
        if _status(operation) != "proposed":
            # The row changed after the first Ledger-first read.  Re-run the
            # terminal branch with a fresh row; never inspect stale Pending.
            raise ConfirmationReplayError(cast(OperationReplay, self.terminal_replay(request, operation=operation)))
        token = self._token(live, request)
        if request.confirmation_token and not compare_digest(token, request.confirmation_token):
            raise WriteOperationError("operation_input_conflict")
        fingerprint = self._fingerprint(live, request, token, operation)
        edited = request.edited_args
        effective = live
        preflight_result: object | None = None
        if approved:
            edited_mapping = (
                None if edited.is_missing() else cast(dict[str, JSONValue], dict(edited.as_mapping))
            )
            selected_catalog = catalog if catalog is not None else self.dependencies.catalog
            if selected_catalog is not None:
                # This is the schema/edit boundary.  Reject never enters it.
                effective = prepare_pending_action(
                    live, cast(Any, selected_catalog), edited_mapping
                )
            prepare_function = self.dependencies.prepare_call
            if prepare_function is not None:
                # The coordinator may be used without the Agent driver in
                # tests and in direct control routes.  When a composition
                # root supplies the existing ``prepare_call`` atom, run it
                # once at the approved boundary; rejection never enters this
                # hook.  The result is retained for diagnostics, while the
                # unchanged Agent resume path may supply its own prepared
                # call to the claim callback.
                prepared_tool_call = ToolCall(
                    live.tool_call_id,
                    live.tool_name,
                    effective.args,
                )
                context = _attribute(self.dependencies.applications, "tool_context")
                preflight_result = _invoke(
                    prepare_function,
                    {
                        "catalog": selected_catalog,
                        "context": context,
                        "tool_context": context,
                        "call": prepared_tool_call,
                        "tool_call": prepared_tool_call,
                        "pending": effective,
                        "pending_identity": f"{live.tool_call_id}:{live.tool_name}",
                        "record_proposal": False,
                    },
                    (selected_catalog, context, prepared_tool_call),
                )
                rejected = _attribute(preflight_result, "failure")
                if rejected is None and _status(preflight_result) in {"rejected", "failure"}:
                    rejected = preflight_result
                if rejected is not None:
                    code = str(_attribute(rejected, "code", "operation_input_conflict") or "operation_input_conflict")
                    raise WriteOperationError(code)
        identity = ConfirmationIdentity(
            conversation_id=request.conversation_id,
            operation_id=operation_id,
            tool_call_id=live.tool_call_id,
            tool_name=live.tool_name,
            request_fingerprint=fingerprint,
            confirmation_token=token,
            proposal_fingerprint=str(_attribute(operation, "proposal_fingerprint", "") or ""),
        )
        generation = self._conversation_generation(conversation, request.conversation_id)
        state = ConfirmationState(
            identity=identity,
            pending=live,
            effective_pending=effective,
            approved=approved,
            edited_args=edited,
            rejection_feedback=request.rejection_feedback,
            undo_seed=dict(undo_seed or {}),
            prepared_call=preflight_result,
            continuation_generation=generation,
        )

        def attempt(
            action: PendingAction,
            prepared: PreparedToolCall[Any, Any] | None,
        ) -> ExecutionAuthorization | ToolFailure | None:
            with state.lock:
                if not state.active or state.cancelled or state.timed_out:
                    return ToolFailure("stale_state", "confirmation_claim_lost")
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
                    or not compare_digest(current.args, state.pending.args)
                ):
                    state.cas_lost = True
                    return ToolFailure("stale_state", "confirmation_claim_lost")
                state.claim_id = state.identity.operation_id
                state.confirmation_attempted = True
                if prepared is None:
                    visible = self._rejection_result(state.rejection_feedback)
                    execution = self._reject(state, visible)
                    state.terminal_execution = execution
                    self._set_ownership(state, execution)
                    return None
                return ExecutionAuthorization(
                    pending_identity=cast(str, _attribute(prepared, "pending_identity")),
                    pending_action_revision=cast(int, _attribute(prepared, "pending_action_revision")),
                    tool_call_id=str(_attribute(prepared, "tool_call_id", action.tool_call_id) or action.tool_call_id),
                    tool_name=str(_attribute(_attribute(prepared, "spec"), "name", action.tool_name) or action.tool_name),
                    arguments_digest=str(_attribute(prepared, "arguments_digest", "") or ""),
                    operation_id=state.identity.operation_id,
                )

        def result(
            action: PendingAction,
            approved_result: bool,
            tool_message: Message,
            execution_record: ToolExecutionRecord[Any, Any] | None,
        ) -> object:
            return self.record_result(state, action, approved_result, tool_message, execution_record)

        def execute(
            prepared: PreparedToolCall[Any, Any],
            tool_context: object,
            authorization: ExecutionAuthorization,
        ) -> ToolExecutionRecord[Any, Any]:
            return self.execute_operation(state, prepared, tool_context, authorization)

        loader = source_loader or self._source_loader(
            conversation, request.conversation_id, request
        )
        return ConfirmationSession(
            state=state,
            on_confirmation_attempt=attempt,
            on_confirmation_result=result,
            execute_operation=execute,
            continuation_message_loader=loader,
            delivery_fence=lambda: self.delivery_fence(state),
        )

    def approve_modify(
        self,
        request: ConfirmationRequest,
        *,
        pending: PendingAction | None = None,
        conversation: object | None = None,
        undo_seed: Mapping[str, Any] | None = None,
        source_loader: ContinuationLoader | None = None,
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
            source_loader=source_loader,
            catalog=catalog,
        )

    approve = approve_modify
    modify = approve_modify

    def reject(
        self,
        request: ConfirmationRequest,
        *,
        pending: PendingAction | None = None,
        conversation: object | None = None,
        source_loader: ContinuationLoader | None = None,
    ) -> ConfirmationSession:
        if request.approved:
            raise ValueError("reject requires approved=false")
        return self._new_session(
            request,
            pending=pending,
            approved=False,
            conversation=conversation,
            source_loader=source_loader,
        )

    def begin(
        self,
        request: ConfirmationRequest,
        *,
        pending: PendingAction | None = None,
        conversation: object | None = None,
        undo_seed: Mapping[str, Any] | None = None,
        source_loader: ContinuationLoader | None = None,
        catalog: object | None = None,
    ) -> ConfirmationSession | OperationReplay:
        replay = self.terminal_replay(request)
        if replay is not None:
            return replay
        if request.approved:
            return self.approve_modify(
                request,
                pending=pending,
                conversation=conversation,
                undo_seed=undo_seed,
                source_loader=source_loader,
                catalog=catalog,
            )
        return self.reject(
            request,
            pending=pending,
            conversation=conversation,
            source_loader=source_loader,
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
            },
            (),
        )
        if not isinstance(execution, (OperationCommitted, OperationFailed, OperationReplay, OperationUnknown)) and not (
            _attribute(execution, "operation_id") is not None
            and _attribute(execution, "payload") is not None
        ):
            raise WriteOperationError("operation_result_unknown", retryable=True)
        if isinstance(execution, OperationUnknown):
            raise WriteOperationError(execution.code, retryable=execution.retryable)
        return cast(OperationExecution, execution)

    def execute_operation(
        self,
        state: ConfirmationState,
        prepared: PreparedToolCall[Any, Any],
        tool_context: object,
        authorization: ExecutionAuthorization,
    ) -> ToolExecutionRecord[Any, Any]:
        with state.lock:
            if state.cancelled or state.timed_out or not state.active:
                raise WriteOperationError("confirmation_claim_lost")
        coordinator = _callable(self.dependencies.write_coordinator, ("execute_primary",))
        if coordinator is None:
            raise WriteOperationError("operation_unavailable")
        undo_seed_builder = self.dependencies.undo_seed_builder
        if undo_seed_builder is None:
            def undo_seed_builder(_prepared: object, _context: object) -> Mapping[str, Any]:
                return dict(state.undo_seed)
        else:
            supplied_seed_builder = undo_seed_builder

            def undo_seed_builder(prepared_call: object, context: object) -> Mapping[str, Any]:
                value = _invoke(
                    supplied_seed_builder,
                    {
                        "prepared": prepared_call,
                        "context": context,
                        "state": state,
                    },
                    (prepared_call, context),
                )
                return dict(value) if isinstance(value, Mapping) else {}

        undo_builder = self.dependencies.undo_builder
        if undo_builder is not None:
            supplied_undo_builder = undo_builder

            def undo_builder(
                prepared_call: object,
                record: object,
                seed: object,
            ) -> Mapping[str, Any] | None:
                value = _invoke(
                    supplied_undo_builder,
                    {
                        "prepared": prepared_call,
                        "record": record,
                        "seed": seed,
                        "state": state,
                    },
                    (prepared_call, record, seed),
                )
                return dict(value) if isinstance(value, Mapping) else None

        values: dict[str, object] = {
            "operation_id": state.identity.operation_id,
            "conversation_id": state.identity.conversation_id,
            "prepared": prepared,
            "context": tool_context,
            "authorization": authorization,
            "request_fingerprint": state.identity.request_fingerprint,
            "undo_seed_builder": undo_seed_builder,
        }
        if undo_builder is not None:
            values["undo_builder"] = undo_builder
        execution, record = cast(
            tuple[OperationExecution, ToolExecutionRecord[Any, Any] | None],
            _invoke(
                coordinator,
                values,
                (),
            ),
        )
        if _terminal(execution):
            state.terminal_execution = execution
            self._set_ownership(state, execution)
        if record is not None:
            return record
        if isinstance(execution, OperationReplay):
            state.replayed = True
            raise ConfirmationReplayError(execution)
        if isinstance(execution, OperationUnknown):
            raise WriteOperationError(execution.code, retryable=execution.retryable)
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
            if not state.active or state.cancelled or state.timed_out:
                return None
            if state.claim_id is None:
                state.cas_lost = True
                raise WriteOperationError("confirmation_claim_lost")
            state.origin_tool_message = tool_message
            state.execution_record = execution_record
            state.approved = approved
            state.succeeded = approved and _record_succeeded(execution_record)
            state.replayed = bool(_attribute(execution_record, "replayed", False))
            state.undo_update = dict(state.undo) if state.succeeded and state.undo else None
            if state.terminal_execution is not None and _attribute(
                _attribute(state.terminal_execution, "payload"), "undo_json"
            ):
                raw_undo = _attribute(_attribute(state.terminal_execution, "payload"), "undo_json")
                try:
                    decoded = json.loads(cast(str, raw_undo))
                except (TypeError, ValueError, json.JSONDecodeError):
                    decoded = {}
                if isinstance(decoded, Mapping):
                    state.undo = dict(decoded)
                    state.undo_update = dict(decoded)
            if not approved:
                state.undo_update = None
            if state.continuation_generation is None:
                state.continuation_generation = self._conversation_generation(None, state.identity.conversation_id)
            return None

    # ---- Ownership, timeout, cancellation, delivery ---------------------

    def _set_ownership(self, state: ConfirmationState, execution: object) -> None:
        ownership = _attribute(execution, "ownership")
        if not isinstance(ownership, DeliveryOwnership):
            return
        state.delivery_ownership = ownership
        heartbeat = _attribute(self.dependencies.write_operations, "heartbeat")
        if callable(heartbeat):
            # Existing DeliveryHeartbeat owns its own retry/stop loop.  It is
            # started before source loading by the owning Runtime request.
            state.delivery_heartbeat = DeliveryHeartbeat(
                cast(Any, self.dependencies.write_operations), ownership
            ).start()

    def delivery_fence(self, state: ConfirmationState) -> bool:
        with state.lock:
            if state.cancelled or state.cas_lost or not state.active:
                return False
            heartbeat = state.delivery_heartbeat
        return heartbeat is None or heartbeat.fence()

    def stop_heartbeat(self, state: ConfirmationState) -> None:
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
            if state.origin_tool_message is None and state.terminal_execution is not None:
                terminal_payload = _attribute(state.terminal_execution, "payload")
                visible = str(_attribute(terminal_payload, "visible_result", "") or "")
                state.origin_tool_message = Message(
                    role="tool",
                    content=visible,
                    tool_call_id=state.identity.tool_call_id,
                )
            ready = state.confirmation_attempted and state.origin_tool_message is not None
        if not ready:
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
            if not state.active:
                return
            state.cancelled = True
            state.active = False
        self.stop_heartbeat(state)

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
    ) -> PersistenceResult | object | None:
        """Persist exactly one origin+continuation bundle under the owner fence."""

        state = session.state
        if isinstance(bundle, DeliveryBundle):
            delivery = bundle
        else:
            delivery = DeliveryBundle(tuple(_message(item) for item in bundle), pending, clarification)
        with state.lock:
            if state.delivered or state.cancelled or state.cas_lost:
                return None
            if state.origin_tool_message is None:
                # A terminal replay has no continuation owner and must not
                # fabricate operation-bound messages.
                return None
            if state.delivery_ownership is not None and not self.delivery_fence(state):
                state.cas_lost = True
                self.stop_heartbeat(state)
                return None
            generation = state.continuation_generation
            ownership = state.delivery_ownership
            expected_pending = state.pending
            claim_id = state.claim_id
            undo = dict(state.undo_update) if state.undo_update is not None else None
        persistence_object = self.dependencies.persistence
        if persistence_object is None:
            self.stop_heartbeat(state)
            raise WriteOperationError("operation_unavailable")
        values = tuple(delivery.messages)
        chained_pending = pending if pending is not None else delivery.pending
        clarification_value = clarification if clarification is not None else delivery.clarification
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
            }
        else:
            persistence = _attribute(persistence_object, "persist_confirmation_continuation")
            if not callable(persistence):
                self.stop_heartbeat(state)
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
            }
        try:
            raw = _invoke(cast(Callable[..., object], persistence), kwargs, ())
        except Exception:
            self.stop_heartbeat(state)
            raise
        status = _attribute(raw, "status")
        status_value = str(getattr(status, "value", status or ""))
        if status_value in {PersistenceStatus.CAS_LOST.value, "cas_lost"}:
            state.cas_lost = True
            self.stop_heartbeat(state)
            return raw
        if status_value in {PersistenceStatus.CLOSED.value, PersistenceStatus.NOT_FOUND.value}:
            self.stop_heartbeat(state)
            return raw
        next_generation = _attribute(raw, "generation")
        if isinstance(next_generation, datetime):
            state.continuation_generation = next_generation
        with state.lock:
            state.delivered = True
            state.active = False
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

    def _conversation_generation(self, conversation: object | None, conversation_id: int) -> datetime | None:
        candidate = conversation
        if candidate is None:
            getter = _callable(self.dependencies.conversations, ("load", "get_conversation", "get"))
            if getter is not None:
                candidate = _invoke(getter, {"conversation_id": conversation_id, "id": conversation_id}, (conversation_id,))
        value = _attribute(candidate, "updated_at")
        return value if isinstance(value, datetime) else None

    def _source_loader(
        self,
        conversation: object | None,
        conversation_id: int,
        request: ConfirmationRequest | None = None,
    ) -> ContinuationLoader:
        loader = _callable(self.dependencies.source_loader, ("load", "load_messages", "messages"))
        assembler = _callable(self.dependencies.context_assembler, ("assemble", "assemble_context", "build_messages"))

        def load() -> tuple[Message, ...]:
            if loader is None:
                return ()
            source = _invoke(
                loader,
                {
                    "conversation": conversation,
                    "conversation_id": conversation_id,
                    "request": request,
                },
                (conversation, request or conversation_id),
            )
            if assembler is not None:
                assembled = _invoke(
                    assembler,
                    {
                        "source": source,
                        "sources": source,
                        "conversation": conversation,
                        "conversation_id": conversation_id,
                        "request": request,
                    },
                    (source, conversation),
                )
            else:
                assembled = source
            values = _attribute(assembled, "messages", assembled)
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                return ()
            return tuple(_message(item) for item in values)

        return load

    @staticmethod
    def _rejection_result(feedback: str) -> str:
        if feedback.strip():
            return "已取消这次操作，并会按你的反馈保持不变。"
        return "已取消这次操作。你可以告诉我下一步想怎么做。"

    @staticmethod
    def _fallback_message(state: ConfirmationState) -> str:
        if state.succeeded:
            return "操作已提交，但后续说明生成失败。"
        if state.approved:
            return "操作未完成，请查看工具结果后重试。"
        return ConfirmationCoordinator._rejection_result(state.rejection_feedback)


__all__ = [
    "ConfirmationAttempt",
    "ConfirmationCoordinator",
    "ConfirmationDependencies",
    "ConfirmationIdentity",
    "ConfirmationReplayError",
    "ConfirmationResult",
    "ConfirmationSession",
    "ConfirmationState",
    "ContinuationLoader",
    "DeliveryBundle",
]
