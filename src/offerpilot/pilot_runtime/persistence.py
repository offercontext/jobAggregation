"""Typed orchestration over the existing ChatRepository persistence atoms.

The Pilot Runtime does not own a database session and deliberately does not
reimplement repository SQL.  This module only translates the loose message
values used by the current Agent/Route boundary into the existing atomic
repository calls and reports their finite outcomes to the Runtime.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, TypeAlias

from offerpilot.ai.agent import PendingAction
from offerpilot.ai.types import Message, ToolCall
from offerpilot.ai.write_operations import DeliveryOwnership
from offerpilot.repositories.chat import ChatRepository


if TYPE_CHECKING:
    class StrEnum(str, Enum):
        pass
else:
    try:
        from enum import StrEnum
    except ImportError:  # pragma: no cover - Python 3.10 compatibility
        class StrEnum(str, Enum):
            def __str__(self) -> str:
                return self.value


class PersistenceStatus(StrEnum):
    """Finite status set returned by every Chat persistence operation."""

    PERSISTED = "persisted"
    CLOSED = "closed"
    NOT_FOUND = "not_found"
    CAS_LOST = "cas_lost"
    DUPLICATE = "duplicate"


class DeliveryOutcome(StrEnum):
    """Ledger delivery outcomes accepted by the existing repository atom."""

    FINAL_RESPONSE = "final_response"
    CHAINED_PENDING = "chained_pending"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class PersistenceResult:
    """Closed, immutable result for a Chat persistence operation.

    ``generation`` is populated by confirmation continuation atoms.  The
    ``delivery_outcome`` value is populated only when an owned Ledger delivery
    was completed.  No ORM object, Session, exception, or mutable payload is
    returned across the Runtime boundary.
    """

    status: PersistenceStatus
    generation: datetime | None = None
    delivery_outcome: DeliveryOutcome | None = None
    message_count: int = 0
    operation_id: str | None = None
    persisted: bool = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, PersistenceStatus):
            raise TypeError("status must be a PersistenceStatus")
        if self.generation is not None and not isinstance(self.generation, datetime):
            raise TypeError("generation must be a datetime or None")
        if self.delivery_outcome is not None and not isinstance(
            self.delivery_outcome, DeliveryOutcome
        ):
            raise TypeError("delivery_outcome must be a DeliveryOutcome")
        if type(self.message_count) is not int or self.message_count < 0:
            raise ValueError("message_count must be a non-negative integer")
        if self.operation_id is not None and type(self.operation_id) is not str:
            raise TypeError("operation_id must be a string or None")
        object.__setattr__(self, "persisted", self.status is PersistenceStatus.PERSISTED)

    @property
    def closed(self) -> bool:
        return self.status is PersistenceStatus.CLOSED

    @property
    def cas_lost(self) -> bool:
        return self.status is PersistenceStatus.CAS_LOST

    @property
    def duplicate(self) -> bool:
        return self.status is PersistenceStatus.DUPLICATE


# These aliases keep the result vocabulary discoverable at the call sites
# without creating several structurally identical DTOs.
ChatPersistenceResult = PersistenceResult
MessagePersistenceResult = PersistenceResult
PendingPersistenceResult = PersistenceResult
DeliveryPersistenceResult = PersistenceResult
PersistedPendingResult = PersistenceResult
PersistedDeliveryResult = PersistenceResult
PersistenceState = PersistenceStatus


MessageInput: TypeAlias = Message | Mapping[str, object]


def _json_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


_USER_FACING_TOOL_NAMES = {
    "update_application_status": "更新投递状态",
    "create_application_event": "添加投递日程",
    "update_application_event": "更新投递日程",
    "delete_application_event": "删除投递日程",
    "add_application": "新建投递记录",
    "create_application": "新建投递记录",
    "add_note": "添加复盘记录",
    "update_note": "更新复盘记录",
    "delete_note": "删除复盘记录",
}


def _user_facing_assistant_content(content: str) -> str:
    """Copy the baseline Chat projection's assistant-content sanitization."""

    if not content:
        return content
    sanitized = content
    for internal_name, label in _USER_FACING_TOOL_NAMES.items():
        sanitized = sanitized.replace(f"`{internal_name}`", label)
        sanitized = sanitized.replace(internal_name, label)
    return sanitized


def _safe_tool_args(raw: str) -> dict[str, Any]:
    """Keep only the baseline's JSON-object tool-argument representation."""

    try:
        args = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    if not isinstance(args, dict):
        return {}
    return args


def _dump_tool_calls(tool_calls: list[ToolCall]) -> str:
    """Serialize tool calls with the exact baseline argument projection."""

    if not tool_calls:
        return ""
    return json.dumps(
        [
            {
                "id": tool_call.id,
                "name": tool_call.name,
                "args": _safe_tool_args(tool_call.args),
            }
            for tool_call in tool_calls
        ],
        ensure_ascii=False,
    )


def _dump_provider_blocks(provider_blocks: dict[str, Any]) -> str:
    """Persist only the provider block explicitly allowed by the baseline."""

    if not provider_blocks:
        return ""
    allowed = {
        key: value
        for key, value in provider_blocks.items()
        if key == "reasoning_content" and value is not None
    }
    if not allowed:
        return ""
    return json.dumps(allowed, ensure_ascii=False)


def _persistable_ai_messages(messages: list[Message]) -> list[dict[str, str]]:
    """Project Agent messages using the unchanged Chat persistence contract."""

    persisted: list[dict[str, str]] = []
    for message in messages:
        content = message.content
        if message.role == "assistant":
            content = _user_facing_assistant_content(content)
        persisted.append(
            {
                "role": message.role,
                "content": content,
                "tool_calls": _dump_tool_calls(message.tool_calls),
                "tool_call_id": message.tool_call_id,
                "provider_blocks": _dump_provider_blocks(message.provider_blocks),
            }
        )
    return persisted


def _raw_mapping_values(message: Mapping[str, object]) -> dict[str, str]:
    role = message.get("role", "")
    if not isinstance(role, str):
        raise TypeError("message role must be a string")
    return {
        "role": role,
        "content": _json_text(message.get("content", "")),
        "tool_calls": _json_text(message.get("tool_calls", "")),
        "tool_call_id": _json_text(message.get("tool_call_id", "")),
        "provider_blocks": _json_text(message.get("provider_blocks", "")),
    }


def _mapping_to_message(message: Mapping[str, object]) -> Message:
    values = _raw_mapping_values(message)
    raw_tool_calls = values["tool_calls"]
    parsed_tool_calls: list[ToolCall] = []
    if raw_tool_calls:
        try:
            decoded = json.loads(raw_tool_calls)
        except (TypeError, json.JSONDecodeError):
            decoded = []
        if isinstance(decoded, list):
            parsed_tool_calls = [
                ToolCall(
                    id=str(item.get("id", "")),
                    name=str(item.get("name", "")),
                    args=_mapping_tool_call_args(item),
                )
                for item in decoded
                if isinstance(item, Mapping)
            ]
    return Message(
        role=values["role"],
        content=values["content"],
        tool_calls=parsed_tool_calls,
        tool_call_id=values["tool_call_id"],
        provider_blocks=_decode_provider_blocks(values["provider_blocks"]),
    )


def _mapping_tool_call_args(item: Mapping[str, object]) -> str:
    raw_args = item.get("args", "")
    return raw_args if isinstance(raw_args, str) else _json_text(raw_args)


def _message_values(message: MessageInput) -> dict[str, str]:
    """Convert a raw Agent message through the baseline projection first."""

    candidate = message if isinstance(message, Message) else _mapping_to_message(message)
    return _persistable_ai_messages([candidate])[0]


def _as_message(message: MessageInput) -> Message:
    """Return a sanitized Message for repository atoms that accept a Message."""

    values = _message_values(message)
    raw_tool_calls = values["tool_calls"]
    parsed_tool_calls: list[ToolCall] = []
    if raw_tool_calls:
        try:
            decoded = json.loads(raw_tool_calls)
        except (TypeError, json.JSONDecodeError):
            decoded = []
        if isinstance(decoded, list):
            parsed_tool_calls = [
                ToolCall(
                    id=str(item.get("id", "")),
                    name=str(item.get("name", "")),
                    args=json.dumps(item.get("args", {}), ensure_ascii=False),
                )
                for item in decoded
                if isinstance(item, Mapping)
            ]
    return Message(
        role=values["role"],
        content=values["content"],
        tool_calls=parsed_tool_calls,
        tool_call_id=values["tool_call_id"],
        provider_blocks=_decode_provider_blocks(values["provider_blocks"]),
    )


def _decode_provider_blocks(value: str) -> dict[str, object]:
    """Decode optional provider metadata without changing tool-message identity.

    Route-shaped mappings historically carry this field as an opaque string,
    while ``Message`` carries a JSON object.  Confirmation atoms do not use
    provider blocks for the origin tool result, so malformed/legacy opaque
    values must not prevent the atomic delivery from being attempted.
    """

    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


class ChatPersistenceCoordinator:
    """Narrow Runtime-owned facade over :class:`ChatRepository` atoms.

    ``ChatRepository`` retains Session ownership.  The coordinator never
    opens a Session, executes SQL, or combines unrelated repository domains in
    a generic transaction.  Atomicity is supplied by the repository methods
    themselves (including their pending and delivery CAS checks).
    """

    def __init__(self, chat: ChatRepository) -> None:
        self.chat = chat

    def _failure_status(
        self,
        conversation_id: int,
        *,
        operation_id: str | None = None,
    ) -> PersistenceStatus:
        conversation = self.chat.get_conversation(conversation_id)
        if conversation is None:
            return PersistenceStatus.NOT_FOUND
        if conversation.archived_at is not None:
            return PersistenceStatus.CLOSED
        if operation_id:
            if any(
                message.operation_id == operation_id
                for message in self.chat.list_messages(conversation_id)
            ):
                return PersistenceStatus.DUPLICATE
        return PersistenceStatus.CAS_LOST

    def _writable_status(self, conversation_id: int) -> PersistenceStatus | None:
        conversation = self.chat.get_conversation(conversation_id)
        if conversation is None:
            return PersistenceStatus.NOT_FOUND
        if conversation.archived_at is not None:
            return PersistenceStatus.CLOSED
        return None

    def persist_message(
        self,
        conversation_id: int,
        role: str,
        content: str = "",
        *,
        tool_calls: str = "",
        tool_call_id: str = "",
        provider_blocks: str = "",
    ) -> PersistenceResult:
        """Persist one user/assistant/tool message through the current atom."""

        status = self._writable_status(conversation_id)
        if status is not None:
            return PersistenceResult(status)
        self.chat.append_message(
            conversation_id,
            role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            provider_blocks=provider_blocks,
        )
        return PersistenceResult(PersistenceStatus.PERSISTED, message_count=1)

    def persist_initial_user_message(
        self,
        conversation_id: int,
        content: str,
    ) -> PersistenceResult:
        return self.persist_message(conversation_id, "user", content)

    def persist_initial_assistant_message(
        self,
        conversation_id: int,
        content: str,
        *,
        tool_calls: str = "",
        tool_call_id: str = "",
        provider_blocks: str = "",
    ) -> PersistenceResult:
        return self.persist_message(
            conversation_id,
            "assistant",
            content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            provider_blocks=provider_blocks,
        )

    def persist_initial_messages(
        self,
        conversation_id: int,
        messages: Sequence[MessageInput],
    ) -> PersistenceResult:
        """Persist the baseline initial message sequence in order."""

        status = self._writable_status(conversation_id)
        if status is not None:
            return PersistenceResult(status)
        values = [_message_values(message) for message in messages]
        for value in values:
            self.chat.append_message(
                conversation_id,
                value["role"],
                content=value["content"],
                tool_calls=value["tool_calls"],
                tool_call_id=value["tool_call_id"],
                provider_blocks=value["provider_blocks"],
            )
        return PersistenceResult(PersistenceStatus.PERSISTED, message_count=len(values))

    def persist_initial_pending(
        self,
        conversation_id: int,
        messages: Sequence[MessageInput],
        pending: PendingAction,
    ) -> PersistenceResult:
        """Atomically persist the assistant/tool chain and a new Pending card."""

        status = self._writable_status(conversation_id)
        if status is not None:
            return PersistenceResult(status, operation_id=pending.operation_id or None)
        persisted = self.chat.persist_pending_action(
            conversation_id,
            pending,
            [_message_values(message) for message in messages],
        )
        if persisted:
            return PersistenceResult(
                PersistenceStatus.PERSISTED,
                message_count=len(messages),
                operation_id=pending.operation_id or None,
            )
        return PersistenceResult(
            self._failure_status(conversation_id, operation_id=pending.operation_id or None),
            operation_id=pending.operation_id or None,
        )

    def set_pending_clarification(
        self,
        conversation_id: int,
        pending: PendingAction,
        question: str,
    ) -> PersistenceResult:
        status = self._writable_status(conversation_id)
        if status is not None:
            return PersistenceResult(status)
        self.chat.set_pending_clarification(conversation_id, pending, question)
        stored = self.chat.get_pending_clarification(conversation_id)
        if stored is not None:
            stored_pending, stored_question = stored
            # ChatRepository's clarification atom intentionally stores only
            # the public draft fields; operation_id belongs to the Ledger and
            # has no clarification column.  Compare exactly what that atom
            # can persist instead of reporting a false CAS loss for a Ledger
            # backed draft.
            same_pending = (
                stored_pending.tool_call_id == pending.tool_call_id
                and stored_pending.tool_name == pending.tool_name
                and stored_pending.args == pending.args
                and stored_pending.human == pending.human
            )
            if same_pending and stored_question == question:
                return PersistenceResult(PersistenceStatus.PERSISTED)
        return PersistenceResult(self._failure_status(conversation_id))

    def clear_pending_clarification(self, conversation_id: int) -> PersistenceResult:
        status = self._writable_status(conversation_id)
        if status is not None:
            return PersistenceResult(status)
        self.chat.clear_pending_clarification(conversation_id)
        if self.chat.get_pending_clarification(conversation_id) is None:
            return PersistenceResult(PersistenceStatus.PERSISTED)
        return PersistenceResult(self._failure_status(conversation_id))

    def persist_timeout_assistant(
        self,
        conversation_id: int,
        content: str,
    ) -> PersistenceResult:
        """Persist the fixed timeout assistant message and clear clarification."""

        result = self.persist_assistant_message(conversation_id, content)
        if not result.persisted:
            return result
        clarification = self.clear_pending_clarification(conversation_id)
        if not clarification.persisted:
            return clarification
        return result

    def persist_assistant_message(
        self,
        conversation_id: int,
        content: str,
        *,
        tool_calls: str = "",
        tool_call_id: str = "",
        provider_blocks: str = "",
    ) -> PersistenceResult:
        return self.persist_initial_assistant_message(
            conversation_id,
            content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            provider_blocks=provider_blocks,
        )

    def persist_confirmation_delivery(
        self,
        conversation_id: int,
        ownership: DeliveryOwnership | None,
        origin_tool_message: MessageInput,
        continuation: Sequence[MessageInput] | None = None,
        chained_pending: PendingAction | None = None,
        *,
        messages: Sequence[MessageInput] | None = None,
        pending: PendingAction | None = None,
        clarification: tuple[PendingAction, str] | None = None,
        expected_generation: datetime | None = None,
        expected_pending: PendingAction | None = None,
        claim_id: str | None = None,
        undo: dict[str, Any] | None = None,
        delivery_failure_code: str | None = None,
    ) -> PersistenceResult:
        """Atomically deliver an origin tool result and continuation.

        When a Pending card is still live, ``expected_pending`` (or the card
        read from the repository) is used as the CAS identity.  The existing
        continuation atom then writes origin + continuation + optional chained
        card and closes the Ledger delivery in one transaction.
        """

        if continuation is not None and messages is not None:
            raise TypeError("pass continuation or messages, not both")
        if chained_pending is not None and pending is not None:
            raise TypeError("pass chained_pending or pending, not both")
        if clarification is not None:
            clarification_action, clarification_question = clarification
            if not isinstance(clarification_action, PendingAction):
                raise TypeError("clarification action must be a PendingAction")
            if not isinstance(clarification_question, str):
                raise TypeError("clarification question must be a string")
        if chained_pending is None:
            chained_pending = pending
        continuation_values = continuation if continuation is not None else messages or ()
        status = self._writable_status(conversation_id)
        if status is not None:
            operation_id = ownership.operation_id if ownership is not None else None
            return PersistenceResult(status, operation_id=operation_id)

        current = self.chat.get_conversation(conversation_id)
        if current is None:
            return PersistenceResult(PersistenceStatus.NOT_FOUND)
        active_pending = self.chat.get_pending_action(conversation_id)
        expected = expected_pending if expected_pending is not None else active_pending
        generation = expected_generation
        if generation is None and expected is not None:
            generation = current.updated_at
        operation_id = (
            ownership.operation_id
            if ownership is not None
            else expected.operation_id if expected is not None and expected.operation_id else None
        )
        if claim_id is None and expected is not None and expected.operation_id:
            claim_id = expected.operation_id

        if ownership is not None and operation_id:
            if any(
                message.operation_id == operation_id
                for message in self.chat.list_messages(conversation_id)
            ):
                return PersistenceResult(
                    PersistenceStatus.DUPLICATE,
                    delivery_outcome=self._delivery_outcome(
                        chained_pending, delivery_failure_code
                    ),
                    operation_id=operation_id,
                )

        persisted_generation: datetime | None
        origin_persisted = False
        values = [_message_values(message) for message in continuation_values]
        origin = _as_message(origin_tool_message)

        if ownership is not None and expected is not None:
            origin_persisted = True
            persisted_generation = self.chat.persist_confirmation_continuation(
                conversation_id,
                generation,
                values,
                pending=chained_pending,
                clarification=clarification,
                delivery_ownership=ownership,
                delivery_failure_code=delivery_failure_code,
                expected_pending=expected,
                claim_id=claim_id,
                origin_message=origin,
                undo=undo,
            )
        elif ownership is None and expected is not None:
            # The non-Ledger compatibility atom writes one terminal assistant
            # message together with the origin.  Existing callers only use a
            # single continuation in this mode; reject an unrepresentable
            # multi-message bundle rather than silently dropping identity.
            if len(values) > 1:
                return PersistenceResult(PersistenceStatus.CAS_LOST, operation_id=operation_id)
            terminal = values[0]["content"] if values else ""
            if chained_pending is not None:
                origin_persisted = True
                persisted_generation = self.chat.replace_pending_confirmation(
                    conversation_id,
                    expected,
                    chained_pending,
                    origin,
                    undo,
                    terminal_assistant_content=terminal,
                    claim_id=claim_id,
                )
            else:
                origin_persisted = True
                persisted_generation = self.chat.resolve_pending_confirmation(
                    conversation_id,
                    expected,
                    origin,
                    undo,
                    claim_id=claim_id,
                    terminal_assistant_content=terminal,
                )
        else:
            # This is the post-resolve continuation path: the origin result is
            # already durable and only the generation CAS plus continuation is
            # still owned by this call.
            persisted_generation = self.chat.persist_confirmation_continuation(
                conversation_id,
                generation,
                values,
                pending=chained_pending,
                clarification=clarification,
                delivery_ownership=ownership,
                delivery_failure_code=delivery_failure_code,
                expected_pending=None,
                origin_message=None,
                undo=undo,
            )

        if persisted_generation is None:
            return PersistenceResult(
                self._failure_status(conversation_id, operation_id=operation_id),
                operation_id=operation_id,
            )
        return PersistenceResult(
            PersistenceStatus.PERSISTED,
            generation=persisted_generation,
            delivery_outcome=self._delivery_outcome(chained_pending, delivery_failure_code)
            if ownership is not None
            else None,
            message_count=len(values) + (1 if origin_persisted else 0),
            operation_id=operation_id,
        )

    def persist_confirmation_fallback(
        self,
        conversation_id: int,
        ownership: DeliveryOwnership | None,
        origin_tool_message: MessageInput,
        message: str,
        *,
        expected_generation: datetime | None = None,
        expected_pending: PendingAction | None = None,
        claim_id: str | None = None,
        undo: dict[str, Any] | None = None,
        failure_code: str = "operation_delivery_failed",
    ) -> PersistenceResult:
        return self.persist_confirmation_delivery(
            conversation_id,
            ownership,
            origin_tool_message,
            [Message(role="assistant", content=message)],
            expected_generation=expected_generation,
            expected_pending=expected_pending,
            claim_id=claim_id,
            undo=undo,
            delivery_failure_code=failure_code,
        )

    def persist_replay_delivery(
        self,
        conversation_id: int,
        ownership: DeliveryOwnership | None,
        origin_tool_message: MessageInput,
        continuation: Sequence[MessageInput] | None = None,
        *,
        messages: Sequence[MessageInput] | None = None,
        expected_generation: datetime | None = None,
        expected_pending: PendingAction | None = None,
        chained_pending: PendingAction | None = None,
        failure_code: str | None = None,
        pending: PendingAction | None = None,
        claim_id: str | None = None,
        undo: dict[str, Any] | None = None,
        clarification: tuple[PendingAction, str] | None = None,
    ) -> PersistenceResult:
        """Typed replay delivery facade; replay never executes a provider/tool."""

        return self.persist_confirmation_delivery(
            conversation_id,
            ownership,
            origin_tool_message,
            continuation,
            chained_pending,
            messages=messages,
            pending=pending,
            clarification=clarification,
            expected_generation=expected_generation,
            expected_pending=expected_pending,
            claim_id=claim_id,
            undo=undo,
            delivery_failure_code=failure_code,
        )

    def persist_confirmation_continuation(
        self,
        conversation_id: int,
        expected_generation: datetime | None,
        messages: Sequence[MessageInput],
        *,
        pending: PendingAction | None = None,
        clarification: tuple[PendingAction, str] | None = None,
        delivery_ownership: DeliveryOwnership | None = None,
        delivery_failure_code: str | None = None,
        expected_pending: PendingAction | None = None,
        claim_id: str | None = None,
        origin_message: MessageInput | None = None,
        undo: dict[str, Any] | None = None,
    ) -> PersistenceResult:
        """Compatibility-shaped continuation facade for Runtime callers."""

        if origin_message is None:
            origin_message = Message(role="tool", content="", tool_call_id="")
        return self.persist_confirmation_delivery(
            conversation_id,
            delivery_ownership,
            origin_message,
            messages,
            pending,
            clarification=clarification,
            expected_generation=expected_generation,
            expected_pending=expected_pending,
            claim_id=claim_id,
            undo=undo,
            delivery_failure_code=delivery_failure_code,
        )

    # Names used by the Runtime state machine stay explicit while preserving
    # the repository vocabulary at this boundary.
    persist_clarification = set_pending_clarification
    clear_clarification = clear_pending_clarification
    persist_timeout_message = persist_timeout_assistant

    @staticmethod
    def _delivery_outcome(
        pending: PendingAction | None,
        failure_code: str | None,
    ) -> DeliveryOutcome:
        if failure_code is not None:
            return DeliveryOutcome.FALLBACK
        if pending is not None:
            return DeliveryOutcome.CHAINED_PENDING
        return DeliveryOutcome.FINAL_RESPONSE


# Friendly aliases for composition code that names the action rather than the
# implementation detail of the class.
ChatPersistence = ChatPersistenceCoordinator


__all__ = [
    "ChatPersistence",
    "ChatPersistenceCoordinator",
    "ChatPersistenceResult",
    "DeliveryOutcome",
    "DeliveryPersistenceResult",
    "MessageInput",
    "MessagePersistenceResult",
    "PendingPersistenceResult",
    "PersistedDeliveryResult",
    "PersistedPendingResult",
    "PersistenceResult",
    "PersistenceState",
    "PersistenceStatus",
]
