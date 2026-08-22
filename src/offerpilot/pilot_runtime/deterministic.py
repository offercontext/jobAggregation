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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from secrets import compare_digest
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from offerpilot.ai.agent import PendingAction
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
    LegacyDeterministicAdapter,
    LegacyDeterministicCatalog,
    LEGACY_DETERMINISTIC_NAMES,
    prepare_legacy_arguments,
)
from offerpilot.ai.tool_specs.legacy import build_legacy_deterministic_catalog
from offerpilot.ai.types import Message, ToolCall
from offerpilot.ai.write_operations import (
    OperationCommitted,
    OperationFailed,
    OperationReplay,
    OperationUnknown,
    WriteOperationError,
    ledger_fingerprint,
    operation_request_fingerprint,
)

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
    WriteStatus,
    freeze_json_mapping,
)
from .errors import RuntimeFailureCode
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

# Task8 closed compatibility source: ``build_legacy_deterministic_catalog``
# currently keeps these schemas inside its builder and exposes no public,
# immutable editable-fields constant.  Keep this bridge-local projection
# closed and lock it against that legacy source in the focused test suite.
_LEGACY_EDITABLE_FIELDS: dict[str, tuple[dict[str, JSONValue], ...]] = {
    "save_application_jd_version": (
        {"field": "jd_text", "type": "long_text"},
        {
            "field": "source_url",
            "type": "string",
            "clearable": True,
            "clear_value": None,
        },
    ),
    "create_application_submission_snapshot": (
        {"field": "submitted_at", "type": "datetime"},
        {"field": "note", "type": "long_text"},
    ),
    "record_application_outcome": (
        {
            "field": "stage",
            "type": "enum",
            "options": ["applied", "closed", "interview", "offer", "screening", "written_test"],
        },
        {
            "field": "result",
            "type": "enum",
            "options": ["advanced", "no_response", "offer_received", "other", "rejected", "withdrawn"],
        },
        {"field": "feedback_text", "type": "long_text"},
        {"field": "reflection_text", "type": "long_text"},
        {"field": "next_action_text", "type": "long_text"},
        {"field": "occurred_at", "type": "datetime"},
    ),
}


class _Persistence(Protocol):
    def get_pending_action(self, conversation_id: int) -> object | None: ...

    def get_pending_clarification(self, conversation_id: int) -> object | None: ...

    def persist_initial_user_message(self, conversation_id: int, content: str) -> object: ...

    def persist_initial_pending(
        self, conversation_id: int, messages: Sequence[object], pending: PendingAction
    ) -> object: ...

    def persist_clarification(
        self,
        conversation_id: int,
        messages: Sequence[object],
        pending: PendingAction,
        question: str,
    ) -> object: ...

    def clear_pending_clarification(self, conversation_id: int) -> object: ...

    def persist_assistant_message(self, conversation_id: int, content: str) -> object: ...


@dataclass(frozen=True, slots=True)
class DeterministicDependencies:
    """Server-owned dependencies for :class:`DeterministicPilotAdapter`.

    ``legacy_catalog_factory`` is a factory rather than a general ToolCatalog:
    this keeps the three closed Legacy names out of the model Tool Surface.
    """

    persistence: _Persistence
    applications: object
    application_jd_versions: object
    application_outcomes: object
    write_operations: object | None = None
    write_coordinator: object | None = None
    chat: object | None = None
    legacy_catalog_factory: Callable[[object, object], LegacyDeterministicCatalog] | None = None
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
        if not isinstance(self.outcome, (MessageOutcome, ConfirmationRequiredOutcome,
                                         RuntimeFailureOutcome, OperationPendingOutcome,
                                         OperationReplayOutcome)):
            raise TypeError("outcome must be a RuntimeOutcome")
        if type(self.events) is not tuple:
            raise TypeError("events must be a tuple")
        if self.execution_mode is not StreamExecutionMode.DIRECT:
            raise ValueError("deterministic execution is always direct")
        if self.input_message_id is not None and (
            type(self.input_message_id) is not int or self.input_message_id <= 0
        ):
            raise ValueError("input_message_id must be a positive integer or None")


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


def _invoke(function: Callable[..., object], named: Mapping[str, object], positional: tuple[object, ...]) -> object:
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
            if parameter.kind in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            } and parameter.name in named:
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
    if not all(isinstance(item, str) for item in (tool_call_id, tool_name, args, human, operation_id)):
        return None
    return PendingAction(cast(str, tool_call_id), cast(str, tool_name), cast(str, args), cast(str, human), cast(str, operation_id))


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
    details: Mapping[str, object] | None = None,
) -> PendingActionPayload:
    return PendingActionPayload(
        tool_name=pending.tool_name,
        operation_id=pending.operation_id or pending.tool_call_id,
        human=pending.human,
        args=freeze_json_mapping(_safe_args(pending.args)),
        confirmation_token=_confirmation_token(pending),
        editable_fields=tuple(
            freeze_json_mapping(item)
            for item in _LEGACY_EDITABLE_FIELDS.get(pending.tool_name, ())
        ),
        details=freeze_json_mapping(details or {}),
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
            values = {name: getattr(dependencies, name) for name in DeterministicDependencies.__dataclass_fields__}
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
    def _action_from_request(request: StartTurnRequest) -> PilotAction | PilotSubmissionSnapshotAction | PilotOutcomeAction | None:
        descriptor = request.pilot_action
        if descriptor is None:
            return None
        kind = descriptor.kind
        if kind not in _DETERMINISTIC_ACTION_KINDS and kind not in {
            "application_jd_save", "application_submission_snapshot", "application_outcome_record",
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

    def matches(self, request: StartTurnRequest, conversation: object) -> bool:
        if request.pilot_action is not None:
            self._action_from_request(request)
            return True
        clarification = self._clarification_for(conversation)
        if clarification is not None and clarification[0].tool_name == "save_application_jd_version":
            return True
        application = self._application(conversation, missing_ok=True)
        if application is None:
            return decide_pilot_action(
                request.message,
                has_current_jd=False,
                collecting_jd=False,
            ).kind != "normal_agent"
        current = self._current_jd(application)
        return decide_pilot_action(
            request.message,
            has_current_jd=current is not None,
            collecting_jd=False,
        ).kind != "normal_agent"

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
            _invoke(getter, {"application_id": application_id, "id": application_id}, (application_id,))
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
        value = _invoke(operation, {"operation_id": operation_id, "id": operation_id}, (operation_id,))
        status = _attribute(value, "status", None)
        return value is not None and status is not None and str(status) != "proposed"

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
                pending_replay=True,
            )
            return self._with_transport_initial(execution, transport)

        application = self._application(conversation)
        current_jd = self._current_jd(application)
        clarification_view = self._clarification_for(conversation)
        clarification_pending = clarification_view[0] if clarification_view is not None else None
        collecting = clarification_pending is not None and clarification_pending.tool_name == "save_application_jd_version"

        if isinstance(action, (PilotSubmissionSnapshotAction, PilotOutcomeAction)):
            if existing is not None:
                return self._confirmation_required(
                    existing,
                    conversation_id,
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
            return self._persist_pending(
                conversation_id,
                request.message,
                pending,
                on_user_message_persisted=on_user_message_persisted,
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
            return self._persist_clarification(
                conversation_id,
                request.message,
                pending,
                decision.question,
                on_user_message_persisted=on_user_message_persisted,
            )
        if decision.kind != "pending_confirmation" or not isinstance(decision.jd_text, str) or not decision.jd_text.strip():
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
        return self._persist_pending(
            conversation_id,
            request.message,
            pending,
            on_user_message_persisted=on_user_message_persisted,
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
        on_tool_result: Callable[[PendingAction, str, bool], object] | None = None,
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
                _error(RuntimeFailureCode.STALE_PENDING_ACTION, "待确认操作已过期，请刷新后重试。", 409),
                preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
            )
        if request.operation_id is not None and request.operation_id != pending.operation_id:
            return DeterministicExecution(
                _error(RuntimeFailureCode.OPERATION_IDENTITY_CONFLICT, "operation identity conflict", 409),
                preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
            )
        expected_token = _confirmation_token(pending)
        token = request.confirmation_token or expected_token
        if not compare_digest(token, expected_token):
            return DeterministicExecution(
                _error(RuntimeFailureCode.STALE_PENDING_ACTION, "待确认操作已被更新，请刷新后重试。", 409),
                preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
            )
        if not request.confirmation_token and (
            not request.edited_args.is_missing() or request.rejection_feedback
        ):
            return DeterministicExecution(
                _error(RuntimeFailureCode.INVALID_CONFIRMATION, "confirmation_token is required when changing confirmation details", 422),
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

        adapter = self._legacy_adapter(pending)
        if adapter is None:
            return DeterministicExecution(
                _error(RuntimeFailureCode.OPERATION_IDENTITY_CONFLICT, "pending action is no longer available", 409),
                preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
            )
        edited = (
            None
            if request.edited_args.is_missing()
            else cast(Mapping[str, JSONValue], dict(request.edited_args.as_mapping))
        )
        if request.approved:
            try:
                effective_args, human = prepare_legacy_arguments(adapter, pending.args, edited)
            except ValueError as exc:
                return DeterministicExecution(
                    _error(RuntimeFailureCode.INVALID_CONFIRMATION, f"invalid confirmation edits: {exc}", 422),
                    preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
                )
            effective_pending = PendingAction(
                pending.tool_call_id,
                pending.tool_name,
                effective_args,
                human,
                pending.operation_id,
            )
            validation_error = adapter.validate(effective_args)
            if validation_error:
                return DeterministicExecution(
                    _error(RuntimeFailureCode.APPLICATION_JD_INVALID_REQUEST, validation_error, 422),
                    preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
                )
            if on_confirmation_attempt is not None:
                on_confirmation_attempt(effective_pending, True)
            input_fingerprint = ledger_fingerprint(
                cast(Any, operations).key,
                "write-operation-legacy-input-v1",
                cast(Any, json.loads(effective_args)),
            )
            execution = cast(Any, coordinator).execute_legacy(
                operation_id=pending.operation_id,
                conversation_id=conversation_id,
                tool_call_id=pending.tool_call_id,
                tool_name=pending.tool_name,
                input_fingerprint=input_fingerprint,
                request_fingerprint=fingerprint,
                executor=self._executor(adapter, effective_pending),
            )
            if isinstance(execution, OperationUnknown):
                return DeterministicExecution(
                    self._write_unknown(execution),
                    preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
                )
            if isinstance(execution, OperationReplay):
                return self._replay_execution(conversation_id, execution, fingerprint, transport=transport)
            if not isinstance(execution, (OperationCommitted, OperationFailed)):
                return DeterministicExecution(
                    _error(RuntimeFailureCode.OPERATION_RESULT_UNKNOWN, "写入结果暂时无法确认，请保留确认卡后重试。", 503, retryable=True),
                    preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
                )
            result = execution.payload.visible_result
            succeeded = execution.payload.status == "committed"
            if on_tool_result is not None:
                on_tool_result(effective_pending, result, succeeded)
            origin = Message(role="tool", content=result, tool_call_id=effective_pending.tool_call_id)
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
            return self._replay_execution(conversation_id, rejection, fingerprint, transport=transport)
        if not isinstance(rejection, (OperationCommitted, OperationFailed)):
            return DeterministicExecution(
                _error(RuntimeFailureCode.OPERATION_RESULT_UNKNOWN, "写入结果暂时无法确认，请保留确认卡后重试。", 503, retryable=True),
                preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
            )
        origin = Message(role="tool", content=_CANCELLED_TOOL_RESULT, tool_call_id=pending.tool_call_id)
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

    def _terminal_replay(
        self,
        request: ConfirmationRequest,
        conversation_id: int,
        *,
        transport: RuntimeTransportContext | None,
    ) -> DeterministicExecution | None:
        operations = self.dependencies.write_operations
        operation_id = request.operation_id
        if operations is None or not isinstance(operation_id, str) or not operation_id:
            return None
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
        if _attribute(operation, "conversation_id", None) != conversation_id or not request.confirmation_token:
            return DeterministicExecution(
                _error(RuntimeFailureCode.OPERATION_IDENTITY_CONFLICT, "operation identity conflict", 409),
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
                    _error(RuntimeFailureCode.OPERATION_RESULT_UNKNOWN, "写入结果暂时无法确认，请保留确认卡后重试。", 503, retryable=True),
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
        value = self.dependencies.persistence.get_pending_action(self._conversation_id(conversation))
        return _pending(value)

    def _clarification_for(self, conversation: object) -> tuple[PendingAction, str] | None:
        value = self.dependencies.persistence.get_pending_clarification(self._conversation_id(conversation))
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
        application = _invoke(getter, {"application_id": application_id, "id": application_id}, (application_id,)) if getter is not None else None
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
        return _invoke(getter, {"application_id": self._application_id(application), "id": self._application_id(application)}, (self._application_id(application),)) if getter is not None else None

    def _persist_pending(
        self,
        conversation_id: int,
        user_message: str,
        pending: PendingAction,
        *,
        on_user_message_persisted: Callable[[int], object] | None,
    ) -> DeterministicExecution:
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
        )
        if not _result_ok(result):
            return DeterministicExecution(self._persistence_error(result))
        return self._confirmation_required(pending, conversation_id)

    def _persist_clarification(
        self,
        conversation_id: int,
        user_message: str,
        pending: PendingAction,
        question: str,
        *,
        on_user_message_persisted: Callable[[int], object] | None,
    ) -> DeterministicExecution:
        result = self.dependencies.persistence.persist_clarification(
            conversation_id,
            [Message(role="user", content=user_message)],
            pending,
            question,
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
        user_result = self.dependencies.persistence.persist_initial_user_message(conversation_id, user_message)
        if not _result_ok(user_result):
            return DeterministicExecution(self._persistence_error(user_result))
        message_id = _result_message_id(user_result)
        if on_user_message_persisted is not None and message_id is not None:
            on_user_message_persisted(message_id)
        cleared = self.dependencies.persistence.clear_pending_clarification(conversation_id)
        if not _result_ok(cleared):
            return DeterministicExecution(self._persistence_error(cleared))
        assistant = self.dependencies.persistence.persist_assistant_message(conversation_id, "已取消保存岗位资料。")
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
        return _error(RuntimeFailureCode.OPERATION_FAILED, "对话当前不可写入。", 503, retryable=True)

    def _confirmation_required(
        self,
        pending: PendingAction,
        conversation_id: int,
        *,
        operation_id: str | None = None,
        replayed: bool = False,
        pending_replay: bool | None = None,
    ) -> DeterministicExecution:
        payload = _confirmation_payload(pending, details=self._pending_details(pending))
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

    def _pending_details(self, pending: PendingAction) -> dict[str, object]:
        """Build the small deterministic card metadata without model tools."""

        if pending.tool_name != "save_application_jd_version":
            return {}
        args = _safe_args(pending.args)
        application_id = args.get("application_id")
        if type(application_id) is not int:
            return {}
        getter = _callable(self.dependencies.applications, ("get", "find"))
        application = (
            _invoke(getter, {"application_id": application_id, "id": application_id}, (application_id,))
            if getter is not None
            else None
        )
        if application is None:
            return {}
        target = {
            "id": f"application-{application_id}",
            "kind": "application",
            "title": str(_attribute(application, "company_name", "")),
            "meta": str(_attribute(application, "position_name", "")),
            "source": "pending_action",
        }
        details: dict[str, object] = {"target": target, "evidence": [target]}
        expected = args.get("expected_current_version_id")
        version = None
        if type(expected) is int:
            getter = _callable(self.dependencies.application_jd_versions, ("get_version",))
            if getter is not None:
                version = _invoke(
                    getter,
                    {"application_id": application_id, "version_id": expected},
                    (application_id, expected),
                )
        raw_current_number = _attribute(version, "version_number", None) if version is not None else None
        current_number = raw_current_number if type(raw_current_number) is int else None
        details["application_jd"] = {
            "current_version_number": current_number,
            "proposed_version_number": (current_number or 0) + 1,
        }
        return details

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

    def _legacy_catalog(self) -> LegacyDeterministicCatalog:
        factory = self.dependencies.legacy_catalog_factory
        if factory is not None:
            return factory(self.dependencies.application_jd_versions, self.dependencies.application_outcomes)
        return cast(
            LegacyDeterministicCatalog,
            cast(Any, build_legacy_deterministic_catalog)(
                self.dependencies.application_jd_versions,
                self.dependencies.application_outcomes,
            ),
        )

    def _legacy_adapter(self, pending: PendingAction) -> LegacyDeterministicAdapter | None:
        if pending.tool_name not in LEGACY_DETERMINISTIC_NAMES:
            return None
        return self._legacy_catalog().resolve_server_loaded(pending)

    def _executor(
        self,
        adapter: LegacyDeterministicAdapter,
        pending: PendingAction,
    ) -> Callable[[object], str]:
        def execute(session: object) -> str:
            jd_service = self._bind(self.dependencies.application_jd_versions, session)
            outcome_repo = self._bind(self.dependencies.application_outcomes, session)
            factory = self.dependencies.legacy_catalog_factory
            catalog = (cast(Any, factory) if factory is not None else cast(Any, build_legacy_deterministic_catalog))(
                jd_service,
                outcome_repo,
            )
            loaded = catalog.resolve_server_loaded(pending)
            if loaded is None:
                raise ValueError("operation_identity_conflict")
            return cast(str, loaded.execute(pending.args))

        return execute

    @staticmethod
    def _bind(value: object, session: object) -> object:
        binder = _callable(value, ("bind",))
        return binder(session) if binder is not None else value

    def _request_fingerprint(self, pending: PendingAction, request: ConfirmationRequest, token: str) -> str:
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
        edited = None if request.edited_args.is_missing() else cast(Mapping[str, JSONValue], dict(request.edited_args.as_mapping))
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
            "operation_delivery_pending": RuntimeFailureCode.OPERATION_DELIVERY_PENDING,
            "operation_result_unknown": RuntimeFailureCode.OPERATION_RESULT_UNKNOWN,
            "operation_input_conflict": RuntimeFailureCode.OPERATION_INPUT_CONFLICT,
            "operation_identity_conflict": RuntimeFailureCode.OPERATION_IDENTITY_CONFLICT,
            "operation_unavailable": RuntimeFailureCode.OPERATION_UNAVAILABLE,
        }
        code = code_map.get(exc.code, RuntimeFailureCode.OPERATION_FAILED)
        status = 409 if code in {RuntimeFailureCode.OPERATION_DELIVERY_PENDING, RuntimeFailureCode.OPERATION_INPUT_CONFLICT, RuntimeFailureCode.OPERATION_IDENTITY_CONFLICT} else 503
        return _error(code, "无法确认写入结果，请保留原请求后重试。", status, retryable=exc.retryable)

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
        undo: object = {},
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
                _error(RuntimeFailureCode.STALE_PENDING_ACTION, "待确认操作已被更新，请刷新后重试。", 409),
                preparation_kind=PreparationKind.DETERMINISTIC_CONFIRMATION,
            )
        if result is False:
            return DeterministicExecution(
                _error(RuntimeFailureCode.OPERATION_DELIVERY_FAILED, "写入结果已提交，但暂时无法生成后续说明。", 503, retryable=True),
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
        undo: object = {},
    ) -> bool | None:
        target = self.dependencies.chat or self.dependencies.persistence
        function = _callable(target, ("resolve_pending_confirmation", "resolve_pending"))
        if function is not None:
            value = _invoke(
                function,
                {
                    "conversation_id": conversation_id,
                    "pending": pending,
                    "tool_message": origin,
                    "undo": undo,
                    "claim_id": pending.operation_id,
                    "terminal_assistant_content": message,
                    "delivery_ownership": ownership,
                },
                (conversation_id, pending, origin, {}),
            )
            return None if value is None else True
        # A Runtime persistence coordinator can expose the atomic delivery
        # facade without exposing a ChatRepository.
        function = _callable(target, ("persist_confirmation_delivery",))
        if function is None:
            return False
        value = _invoke(
            function,
            {
                "conversation_id": conversation_id,
                "ownership": ownership,
                "origin_tool_message": origin,
                "continuation": [Message(role="assistant", content=message)],
                "expected_pending": pending,
                "claim_id": pending.operation_id,
                "undo": undo,
            },
            (conversation_id, ownership, origin, [Message(role="assistant", content=message)]),
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
                        source_url=args.get("source_url") if isinstance(args.get("source_url"), str) else None,
                        id_factory=self.dependencies.id_factory,
                        key_factory=self.dependencies.key_factory,
                    )
                    replaced = self._replace_pending(
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
                        return DeterministicExecution(
                            _error(
                                RuntimeFailureCode(code),
                                "当前岗位资料已变化，请重新确认保存。",
                                409,
                                pending_action=_confirmation_payload(
                                    replacement,
                                    details=self._pending_details(replacement),
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
            "application_jd_invalid_request": (RuntimeFailureCode.APPLICATION_JD_INVALID_REQUEST, 422, "岗位资料参数无效，请修改后重试。"),
            "application_archive_idempotency_conflict": (RuntimeFailureCode.APPLICATION_ARCHIVE_IDEMPOTENCY_CONFLICT, 409, "投递事实已发生变化，请刷新后重新确认。"),
            "application_archive_source_conflict": (RuntimeFailureCode.APPLICATION_ARCHIVE_SOURCE_CONFLICT, 409, "投递事实已发生变化，请刷新后重新确认。"),
            "application_outcome_idempotency_conflict": (RuntimeFailureCode.APPLICATION_OUTCOME_IDEMPOTENCY_CONFLICT, 409, "投递事实已发生变化，请刷新后重新确认。"),
            "application_outcome_source_conflict": (RuntimeFailureCode.APPLICATION_OUTCOME_SOURCE_CONFLICT, 409, "投递事实已发生变化，请刷新后重新确认。"),
            "application_archive_invalid_request": (RuntimeFailureCode.APPLICATION_ARCHIVE_INVALID_REQUEST, 422, "投递事实参数无效，请修改后重试。"),
            "application_outcome_invalid_request": (RuntimeFailureCode.APPLICATION_OUTCOME_INVALID_REQUEST, 422, "投递事实参数无效，请修改后重试。"),
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
                _error(RuntimeFailureCode.STALE_PENDING_ACTION, "待确认操作已被更新，请刷新后重试。", 409),
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
    ) -> bool | None:
        target = self.dependencies.chat or self.dependencies.persistence
        function = _callable(target, ("replace_pending_confirmation", "replace_pending"))
        if function is not None:
            value = _invoke(
                function,
                {
                    "conversation_id": conversation_id,
                    "pending": pending,
                    "replacement": replacement,
                    "tool_message": origin,
                    "undo": {},
                    "terminal_assistant_content": message,
                    "claim_id": pending.operation_id,
                    "delivery_ownership": ownership,
                },
                (conversation_id, pending, replacement, origin, {}),
            )
            return None if value is None else True
        function = _callable(target, ("persist_confirmation_delivery",))
        if function is None:
            return False
        value = _invoke(
            function,
            {
                "conversation_id": conversation_id,
                "ownership": ownership,
                "origin_tool_message": origin,
                "continuation": [Message(role="assistant", content=message)],
                "chained_pending": replacement,
                "expected_pending": pending,
                "claim_id": pending.operation_id,
                "undo": {},
            },
            (conversation_id, ownership, origin, [Message(role="assistant", content=message)]),
        )
        if _result_ok(value):
            return True
        status = _result_status(value)
        return None if str(status) in {"cas_lost", "not_found", "closed"} else False

    def _current_jd_by_id(self, application_id: int) -> object | None:
        getter = _callable(self.dependencies.application_jd_versions, ("get_current", "current"))
        if getter is None:
            return None
        return _invoke(getter, {"application_id": application_id, "id": application_id}, (application_id,))

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
                    return DeterministicExecution(self._write_unknown(converged), preparation_kind=PreparationKind.REPLAY)
                refreshed = repository.get(replay.operation_id)
                if refreshed is None:
                    return DeterministicExecution(
                        _error(RuntimeFailureCode.OPERATION_RESULT_UNKNOWN, "写入结果暂时无法确认，请保留确认卡后重试。", 503, retryable=True),
                        preparation_kind=PreparationKind.REPLAY,
                    )
                replay = repository.replay(refreshed, request_fingerprint)
            if replay.delivery_outcome == "chained_pending":
                pending = _pending(
                    self.dependencies.persistence.get_pending_action(conversation_id)
                )
                if pending is not None:
                    confirmation = self._confirmation_required(
                        pending,
                        conversation_id,
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
                message, write_status = replay.final_message or replay.payload.visible_result, "failed"
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
            return DeterministicExecution(self._write_error(exc), preparation_kind=PreparationKind.REPLAY)


__all__ = [
    "DeterministicDependencies",
    "DeterministicExecution",
    "DeterministicPilotAdapter",
    "LEGACY_DETERMINISTIC_NAMES",
]
