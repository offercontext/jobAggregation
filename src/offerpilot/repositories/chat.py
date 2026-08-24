from __future__ import annotations

import json
import re
from contextlib import contextmanager
from hashlib import sha256
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping, cast

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.ai.write_operations import (
    DeliveryOwnership,
    LEGACY_WRITE_OPERATION_NAMES,
    WriteOperationRepository,
    ledger_fingerprint,
)
from offerpilot.ai.types import Message
from offerpilot.models import ChatMessage, Conversation, WriteOperation


_CONFIRMATION_CLAIM_LEASE = timedelta(minutes=15)
_MAX_SCOPE_REVISION = 9223372036854775807
_SCOPE_CONTEXT_TYPES = frozenset({"workspace", "global", "mode", "application"})
_ASCII_POSITIVE_INTEGER = re.compile(r"[1-9][0-9]*\Z")
_SCOPE_KEYS = frozenset({"mode", "context_type", "context_ref", "scope_revision"})
_SCOPE_ARGUMENT_MISSING = object()


class ConversationScopeError(ValueError):
    """Invalid or unavailable scope input which must fail before persistence."""


class ConversationScopeUnavailable(ConversationScopeError):
    """The requested Application scope does not resolve to an active row."""


class ConversationScopeVisibilityFailure(ConversationScopeError):
    """The active-Application visibility query failed internally."""


@dataclass(frozen=True, slots=True)
class ConversationScopeMutationSnapshot:
    """Canonical, validated scope values used by the atomic repository ports.

    The constructor intentionally accepts the JSON-level Application id forms
    (exact Python ``int`` or canonical decimal ``str``), but stores only the
    canonical string representation.  It never strips or case-folds user input.
    """

    context_type: str | None = "workspace"
    context_ref: object = ""
    mode: str | None = "general"

    def __post_init__(self) -> None:
        context_type = _canonical_context_type(self.context_type)
        context_ref = _canonical_context_ref(context_type, self.context_ref)
        mode = _canonical_scope_mode(self.mode)
        object.__setattr__(self, "context_type", context_type)
        object.__setattr__(self, "context_ref", context_ref)
        object.__setattr__(self, "mode", mode)


@dataclass(frozen=True)
class ConversationArchiveUpdate:
    status: Literal["updated", "not_found", "pending"]
    conversation: Conversation | None = None


class ChatRepository:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        write_operations: WriteOperationRepository | None = None,
        session: Session | None = None,
    ):
        self._session_factory = session_factory
        self._write_operations = write_operations
        self._session = session

    def bind(self, session: Session) -> "ChatRepository":
        return ChatRepository(self._session_factory, self._write_operations, session)

    @contextmanager
    def _operation_session(self) -> Any:
        if self._session is not None:
            yield self._session, False
            return
        with self._session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            yield session, True

    def create_conversation(
        self,
        title: str,
        mode: object = _SCOPE_ARGUMENT_MISSING,
        context_type: object = _SCOPE_ARGUMENT_MISSING,
        context_ref: object = _SCOPE_ARGUMENT_MISSING,
        title_source: str = "fallback",
    ) -> Conversation:
        if (
            mode is not _SCOPE_ARGUMENT_MISSING
            or context_type is not _SCOPE_ARGUMENT_MISSING
            or context_ref is not _SCOPE_ARGUMENT_MISSING
        ):
            raise ValueError("scope fields require create_conversation_with_scope")
        conversation = Conversation(
            title=title,
            title_source=title_source,
            mode="general",
            context_type="workspace",
            context_ref="",
            scope_revision=0,
        )
        with self._session_factory() as session:
            session.add(conversation)
            session.commit()
            session.refresh(conversation)
            return conversation

    def create_conversation_with_scope(
        self,
        title: str,
        mutation: ConversationScopeMutationSnapshot,
        *,
        title_source: str = "fallback",
    ) -> Conversation:
        """Create a Conversation and its scope as one locked, revision-zero write."""

        if not isinstance(mutation, ConversationScopeMutationSnapshot):
            raise TypeError("mutation must be ConversationScopeMutationSnapshot")
        with self._session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            _require_active_application(session, mutation)
            conversation = Conversation(
                title=title,
                title_source=title_source,
                mode=mutation.mode,
                context_type=mutation.context_type,
                context_ref=mutation.context_ref,
                scope_revision=0,
            )
            session.add(conversation)
            session.commit()
            session.refresh(conversation)
            return conversation

    def get_conversation(self, conversation_id: int) -> Conversation | None:
        with self._session_factory() as session:
            return session.get(Conversation, conversation_id)

    def list_conversations(self, include_archived: bool = False) -> list[Conversation]:
        statement = select(Conversation)
        if not include_archived:
            statement = statement.where(Conversation.archived_at.is_(None))
        statement = statement.order_by(
            Conversation.pinned_at.is_(None).asc(),
            Conversation.pinned_at.desc(),
            Conversation.updated_at.desc(),
            Conversation.id.desc(),
        )
        with self._session_factory() as session:
            return list(session.scalars(statement))

    def update_conversation(
        self, conversation_id: int, values: dict[str, Any]
    ) -> Conversation | None:
        if _SCOPE_KEYS.intersection(values):
            raise ValueError("scope fields require patch_conversation_with_scope")
        if not values:
            return self.get_conversation(conversation_id)
        with self._session_factory() as session:
            conversation = session.get(Conversation, conversation_id)
            if conversation is None:
                return None
            for key, value in values.items():
                setattr(conversation, key, value)
            conversation.updated_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(conversation)
            return conversation

    def patch_conversation_with_scope(
        self,
        conversation_id: int,
        values: Mapping[str, object],
        mutation: ConversationScopeMutationSnapshot | None,
        *,
        expected_scope_revision: int,
    ) -> Conversation | None:
        """Atomically patch scope and ordinary Conversation fields with a CAS.

        The caller supplies an already validated mutation snapshot.  The row is
        reloaded while holding ``BEGIN IMMEDIATE`` before visibility and scope
        comparison, so a scope change and a title/pin/archive change either
        commit together or none of them does.
        """

        if _SCOPE_KEYS.intersection(values):
            raise ValueError("scope fields belong in mutation")
        if type(expected_scope_revision) is not int or not (
            0 <= expected_scope_revision <= _MAX_SCOPE_REVISION
        ):
            raise ValueError("expected_scope_revision must be a valid integer")
        if mutation is not None and not isinstance(
            mutation, ConversationScopeMutationSnapshot
        ):
            raise TypeError("mutation must be ConversationScopeMutationSnapshot or None")

        with self._session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            conversation = session.get(Conversation, conversation_id)
            if conversation is None or conversation.scope_revision != expected_scope_revision:
                session.rollback()
                return None
            if values.get("archived_at") is not None and conversation.pending_tool_name:
                session.rollback()
                return None

            update_values = dict(values)
            scope_changed = False
            if mutation is not None:
                _require_active_application(session, mutation)
                scope_changed = (
                    conversation.context_type != mutation.context_type
                    or conversation.context_ref != mutation.context_ref
                    or conversation.mode != mutation.mode
                )
                if scope_changed:
                    if conversation.scope_revision >= _MAX_SCOPE_REVISION:
                        session.rollback()
                        raise ConversationScopeError("conversation scope revision overflow")
                    update_values.update(
                        {
                            "context_type": mutation.context_type,
                            "context_ref": mutation.context_ref,
                            "mode": mutation.mode,
                            "scope_revision": conversation.scope_revision + 1,
                        }
                    )

            if update_values:
                update_values.setdefault("updated_at", datetime.now(timezone.utc))
                for key, value in update_values.items():
                    if key == "updated_at":
                        continue
                    if not hasattr(Conversation, key):
                        raise ValueError(f"unknown conversation field: {key}")
                    setattr(conversation, key, value)
                conversation.updated_at = cast(datetime, update_values["updated_at"])
                session.flush()
            session.commit()
            session.refresh(conversation)
            return conversation

    def apply_generated_title(self, conversation_id: int, title: str) -> bool:
        with self._session_factory() as session:
            result = session.execute(
                update(Conversation)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.title_source == "fallback",
                )
                .values(
                    title=title,
                    title_source="generated",
                    updated_at=Conversation.updated_at,
                )
            )
            session.commit()
            return bool(getattr(result, "rowcount", 0))

    def update_conversation_for_archive(
        self, conversation_id: int, values: dict[str, Any]
    ) -> ConversationArchiveUpdate:
        if _SCOPE_KEYS.intersection(values):
            raise ValueError("scope fields require patch_conversation_with_scope")
        now = datetime.now(timezone.utc)
        with self._session_factory() as session:
            result = session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .where(Conversation.pending_tool_name == "")
                .values(**values, updated_at=now)
            )
            if getattr(result, "rowcount", 0) == 1:
                session.commit()
                conversation = session.get(Conversation, conversation_id)
                return ConversationArchiveUpdate("updated", conversation)
            conversation = session.get(Conversation, conversation_id)
            if conversation is None:
                return ConversationArchiveUpdate("not_found")
            return ConversationArchiveUpdate("pending")

    def append_message(
        self,
        conversation_id: int,
        role: str,
        content: str = "",
        tool_calls: str = "",
        tool_call_id: str = "",
        provider_blocks: str = "",
    ) -> ChatMessage:
        message = ChatMessage(
            conversation_id=conversation_id,
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            provider_blocks=provider_blocks,
        )
        with self._session_factory() as session:
            now = _next_conversation_timestamp(session, conversation_id)
            session.add(message)
            session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(updated_at=now)
            )
            session.commit()
            session.refresh(message)
            return message

    def list_messages(self, conversation_id: int) -> list[ChatMessage]:
        statement = (
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.id.asc())
        )
        with self._session_factory() as session:
            return list(session.scalars(statement))

    def has_user_message(self) -> bool:
        statement = select(ChatMessage.id).where(ChatMessage.role == "user").limit(1)
        with self._session_factory() as session:
            return session.scalar(statement) is not None

    def get_pending_action(self, conversation_id: int) -> PendingAction | None:
        with self._session_factory() as session:
            conversation = session.get(Conversation, conversation_id)
            if conversation is None or not conversation.pending_tool_name:
                return None
            return PendingAction(
                tool_call_id=conversation.pending_tool_call_id,
                tool_name=conversation.pending_tool_name,
                args=conversation.pending_args,
                human=conversation.pending_human or conversation.pending_tool_name,
                operation_id=conversation.pending_operation_id,
            )

    def set_pending_action(self, conversation_id: int, pending: PendingAction) -> bool:
        if pending.operation_id and self._write_operations is None:
            return False
        with self._session_factory() as session:
            if pending.operation_id and self._write_operations is not None:
                self._create_operation_for_pending(session, conversation_id, pending)
            result = session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .where(Conversation.archived_at.is_(None))
                .where(Conversation.pending_confirmation_claim_id == "")
                .values(
                    pending_tool_call_id=pending.tool_call_id,
                    pending_operation_id=pending.operation_id,
                    pending_confirmation_claim_id="",
                    pending_confirmation_claimed_at=None,
                    pending_tool_name=pending.tool_name,
                    pending_args=pending.args,
                    pending_human=pending.human,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()
            return getattr(result, "rowcount", 0) == 1

    def persist_pending_action(
        self,
        conversation_id: int,
        pending: PendingAction,
        messages: list[dict[str, str]],
    ) -> bool:
        """Atomically persist a write proposal and make it the pending action."""
        if pending.operation_id and self._write_operations is None:
            return False
        with self._operation_session() as (session, owned):
            result = session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .where(Conversation.archived_at.is_(None))
                .where(Conversation.pending_confirmation_claim_id == "")
                .where(Conversation.pending_tool_call_id == "")
                .where(Conversation.pending_operation_id == "")
                .where(Conversation.pending_tool_name == "")
                .values(
                    pending_tool_call_id=pending.tool_call_id,
                    pending_operation_id=pending.operation_id,
                    pending_confirmation_claim_id="",
                    pending_confirmation_claimed_at=None,
                    pending_tool_name=pending.tool_name,
                    pending_args=pending.args,
                    pending_human=pending.human,
                    clarification_tool_call_id="",
                    clarification_tool_name="",
                    clarification_args="",
                    clarification_human="",
                    clarification_question="",
                    updated_at=datetime.now(timezone.utc),
                )
            )
            if getattr(result, "rowcount", 0) != 1:
                if owned:
                    session.rollback()
                return False
            if pending.operation_id and self._write_operations is not None:
                self._create_operation_for_pending(session, conversation_id, pending)
            for message in messages:
                session.add(
                    ChatMessage(
                        conversation_id=conversation_id,
                        role=message.get("role", ""),
                        content=message.get("content", ""),
                        tool_calls=message.get("tool_calls", ""),
                        tool_call_id=message.get("tool_call_id", ""),
                        provider_blocks=message.get("provider_blocks", ""),
                    )
                )
            if owned:
                session.commit()
            return True

    def clear_pending_action(self, conversation_id: int) -> None:
        with self._session_factory() as session:
            session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .where(Conversation.pending_confirmation_claim_id == "")
                .values(
                    pending_tool_call_id="",
                    pending_operation_id="",
                    pending_confirmation_claim_id="",
                    pending_confirmation_claimed_at=None,
                    pending_tool_name="",
                    pending_args="",
                    pending_human="",
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

    def claim_pending_confirmation(
        self,
        conversation_id: int,
        expected: PendingAction,
        claim_id: str,
    ) -> bool:
        """Atomically claim one Pending Action while preserving its public representation."""

        if not claim_id:
            raise ValueError("confirmation claim id must be non-empty")
        now = datetime.now(timezone.utc)
        stale_before = now - _CONFIRMATION_CLAIM_LEASE
        with self._session_factory() as session:
            result = session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .where(Conversation.archived_at.is_(None))
                .where(Conversation.pending_tool_call_id == expected.tool_call_id)
                .where(Conversation.pending_operation_id == expected.operation_id)
                .where(Conversation.pending_tool_name == expected.tool_name)
                .where(Conversation.pending_args == expected.args)
                .where(
                    or_(
                        Conversation.pending_confirmation_claim_id == "",
                        Conversation.pending_confirmation_claimed_at.is_(None),
                        Conversation.pending_confirmation_claimed_at <= stale_before,
                    )
                )
                .values(
                    pending_confirmation_claim_id=claim_id,
                    pending_confirmation_claimed_at=now,
                    updated_at=now,
                )
            )
            session.commit()
            return getattr(result, "rowcount", 0) == 1

    def resolve_pending_confirmation(
        self,
        conversation_id: int,
        expected: PendingAction,
        tool_message: Message,
        undo: dict[str, Any] | None,
        *,
        claim_id: str | None = None,
        terminal_assistant_content: str = "",
        delivery_ownership: DeliveryOwnership | None = None,
    ) -> datetime | None:
        """Persist a result with tri-state undo: None preserves, empty clears, non-empty replaces."""
        if claim_id == "":
            raise ValueError("confirmation claim id must be non-empty when provided")
        values: dict[str, Any] = {
            "pending_tool_call_id": "",
            "pending_operation_id": "",
            "pending_confirmation_claim_id": "",
            "pending_confirmation_claimed_at": None,
            "pending_tool_name": "",
            "pending_args": "",
            "pending_human": "",
            "clarification_tool_call_id": "",
            "clarification_tool_name": "",
            "clarification_args": "",
            "clarification_human": "",
            "clarification_question": "",
        }
        if undo is not None:
            values["last_write_undo_json"] = json.dumps(undo, ensure_ascii=False) if undo else ""
            values["last_write_operation_id"] = expected.operation_id if undo else ""
        with self._operation_session() as (session, owned):
            now = _next_conversation_timestamp(session, conversation_id)
            values["updated_at"] = now
            statement = (
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .where(Conversation.pending_tool_call_id == expected.tool_call_id)
                .where(Conversation.pending_operation_id == expected.operation_id)
                .where(Conversation.pending_tool_name == expected.tool_name)
                .where(Conversation.pending_args == expected.args)
            )
            if claim_id is None:
                statement = statement.where(
                    Conversation.pending_confirmation_claim_id == "",
                    Conversation.pending_confirmation_claimed_at.is_(None),
                )
            else:
                statement = statement.where(Conversation.pending_confirmation_claim_id == claim_id)
            result = session.execute(statement.values(**values))
            if getattr(result, "rowcount", 0) != 1:
                if owned:
                    session.rollback()
                return None
            session.add(
                ChatMessage(
                    conversation_id=conversation_id,
                    role=tool_message.role,
                    content=tool_message.content,
                    tool_call_id=tool_message.tool_call_id,
                    operation_id=expected.operation_id or None,
                    delivery_kind=("origin_tool_result" if expected.operation_id else None),
                    delivery_ordinal=(0 if expected.operation_id else None),
                )
            )
            if terminal_assistant_content:
                session.add(
                    ChatMessage(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=terminal_assistant_content,
                        operation_id=(
                            delivery_ownership.operation_id
                            if delivery_ownership is not None
                            else None
                        ),
                        delivery_kind=(
                            "continuation_message" if delivery_ownership is not None else None
                        ),
                        delivery_ordinal=(1 if delivery_ownership is not None else None),
                    )
                )
            if delivery_ownership is not None:
                if self._write_operations is None:
                    if owned:
                        session.rollback()
                    return None
                session.flush()
                if not self._write_operations.complete_delivery(
                    session,
                    delivery_ownership,
                    outcome="final_response",
                ):
                    if owned:
                        session.rollback()
                    return None
            if owned:
                session.commit()
            return now

    def replace_pending_confirmation(
        self,
        conversation_id: int,
        expected: PendingAction,
        replacement: PendingAction,
        tool_message: Message,
        undo: dict[str, Any] | None,
        *,
        terminal_assistant_content: str = "",
        claim_id: str | None = None,
        delivery_ownership: DeliveryOwnership | None = None,
    ) -> datetime | None:
        """Atomically replace a stale pending card only when the original still owns it."""
        values: dict[str, Any] = {
            "pending_tool_call_id": replacement.tool_call_id,
            "pending_operation_id": replacement.operation_id,
            "pending_confirmation_claim_id": "",
            "pending_confirmation_claimed_at": None,
            "pending_tool_name": replacement.tool_name,
            "pending_args": replacement.args,
            "pending_human": replacement.human,
            "clarification_tool_call_id": "",
            "clarification_tool_name": "",
            "clarification_args": "",
            "clarification_human": "",
            "clarification_question": "",
        }
        if undo is not None:
            values["last_write_undo_json"] = json.dumps(undo, ensure_ascii=False) if undo else ""
            values["last_write_operation_id"] = expected.operation_id if undo else ""
        with self._operation_session() as (session, owned):
            now = _next_conversation_timestamp(session, conversation_id)
            values["updated_at"] = now
            statement = (
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .where(Conversation.archived_at.is_(None))
                .where(Conversation.pending_tool_call_id == expected.tool_call_id)
                .where(Conversation.pending_operation_id == expected.operation_id)
                .where(Conversation.pending_tool_name == expected.tool_name)
                .where(Conversation.pending_args == expected.args)
            )
            if claim_id is None:
                statement = statement.where(Conversation.pending_confirmation_claim_id == "")
            else:
                statement = statement.where(Conversation.pending_confirmation_claim_id == claim_id)
            result = session.execute(statement.values(**values))
            if getattr(result, "rowcount", 0) != 1:
                if owned:
                    session.rollback()
                return None
            if replacement.operation_id and self._write_operations is not None:
                self._create_operation_for_pending(session, conversation_id, replacement)
            session.add(
                ChatMessage(
                    conversation_id=conversation_id,
                    role=tool_message.role,
                    content=tool_message.content,
                    tool_call_id=tool_message.tool_call_id,
                    operation_id=(delivery_ownership.operation_id if delivery_ownership else None),
                    delivery_kind=("origin_tool_result" if delivery_ownership else None),
                    delivery_ordinal=(0 if delivery_ownership else None),
                )
            )
            if terminal_assistant_content:
                session.add(
                    ChatMessage(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=terminal_assistant_content,
                        operation_id=(
                            delivery_ownership.operation_id if delivery_ownership else None
                        ),
                        delivery_kind=("continuation_message" if delivery_ownership else None),
                        delivery_ordinal=(1 if delivery_ownership else None),
                    )
                )
            if delivery_ownership is not None:
                if self._write_operations is None:
                    if owned:
                        session.rollback()
                    return None
                session.flush()
                if not self._write_operations.complete_delivery(
                    session,
                    delivery_ownership,
                    outcome="chained_pending",
                    next_operation_id=replacement.operation_id,
                ):
                    if owned:
                        session.rollback()
                    return None
            if owned:
                session.commit()
            return now

    def persist_confirmation_continuation(
        self,
        conversation_id: int,
        expected_generation: datetime | None,
        messages: list[dict[str, str]],
        *,
        pending: PendingAction | None = None,
        clarification: tuple[PendingAction, str] | None = None,
        delivery_ownership: DeliveryOwnership | None = None,
        delivery_failure_code: str | None = None,
        expected_pending: PendingAction | None = None,
        claim_id: str | None = None,
        origin_message: Message | None = None,
        undo: dict[str, Any] | None = None,
    ) -> datetime | None:
        if expected_generation is None:
            return None
        values: dict[str, Any] = {}
        if expected_pending is not None:
            values.update(
                {
                    "pending_tool_call_id": "",
                    "pending_operation_id": "",
                    "pending_confirmation_claim_id": "",
                    "pending_confirmation_claimed_at": None,
                    "pending_tool_name": "",
                    "pending_args": "",
                    "pending_human": "",
                    "clarification_tool_call_id": "",
                    "clarification_tool_name": "",
                    "clarification_args": "",
                    "clarification_human": "",
                    "clarification_question": "",
                }
            )
        if pending is not None:
            values.update(
                {
                    "pending_tool_call_id": pending.tool_call_id,
                    "pending_operation_id": pending.operation_id,
                    "pending_confirmation_claim_id": "",
                    "pending_confirmation_claimed_at": None,
                    "pending_tool_name": pending.tool_name,
                    "pending_args": pending.args,
                    "pending_human": pending.human,
                    "clarification_tool_call_id": "",
                    "clarification_tool_name": "",
                    "clarification_args": "",
                    "clarification_human": "",
                    "clarification_question": "",
                }
            )
        elif clarification is not None:
            action, question = clarification
            values.update(
                {
                    "pending_tool_call_id": "",
                    "pending_operation_id": "",
                    "pending_confirmation_claim_id": "",
                    "pending_confirmation_claimed_at": None,
                    "pending_tool_name": "",
                    "pending_args": "",
                    "pending_human": "",
                    "clarification_tool_call_id": action.tool_call_id,
                    "clarification_tool_name": action.tool_name,
                    "clarification_args": action.args,
                    "clarification_human": action.human,
                    "clarification_question": question,
                }
            )
        if expected_pending is not None and undo is not None:
            values["last_write_undo_json"] = json.dumps(undo, ensure_ascii=False) if undo else ""
            values["last_write_operation_id"] = expected_pending.operation_id if undo else ""
        with self._operation_session() as (session, owned):
            now = _next_conversation_timestamp(session, conversation_id, expected_generation)
            values["updated_at"] = now
            statement = update(Conversation).where(Conversation.id == conversation_id)
            if expected_pending is not None:
                statement = (
                    statement.where(
                        Conversation.pending_tool_call_id == expected_pending.tool_call_id
                    )
                    .where(Conversation.pending_operation_id == expected_pending.operation_id)
                    .where(Conversation.pending_tool_name == expected_pending.tool_name)
                    .where(Conversation.pending_args == expected_pending.args)
                )
                if claim_id is None:
                    statement = statement.where(Conversation.pending_confirmation_claim_id == "")
                else:
                    statement = statement.where(
                        Conversation.pending_confirmation_claim_id == claim_id
                    )
            else:
                statement = statement.where(Conversation.updated_at == expected_generation)
            if pending is not None:
                statement = statement.where(Conversation.archived_at.is_(None))
            result = session.execute(statement.values(**values))
            if getattr(result, "rowcount", 0) != 1:
                if owned:
                    session.rollback()
                return None
            if pending is not None and pending.operation_id and self._write_operations is not None:
                self._create_operation_for_pending(session, conversation_id, pending)
            if delivery_ownership is not None:
                if origin_message is None or expected_pending is None:
                    if owned:
                        session.rollback()
                    return None
                session.add(
                    ChatMessage(
                        conversation_id=conversation_id,
                        role=origin_message.role,
                        content=origin_message.content,
                        tool_call_id=origin_message.tool_call_id,
                        operation_id=delivery_ownership.operation_id,
                        delivery_kind="origin_tool_result",
                        delivery_ordinal=0,
                    )
                )
            for index, message in enumerate(messages, start=1):
                session.add(
                    ChatMessage(
                        conversation_id=conversation_id,
                        role=message.get("role", ""),
                        content=message.get("content", ""),
                        tool_calls=message.get("tool_calls", ""),
                        tool_call_id=message.get("tool_call_id", ""),
                        provider_blocks=message.get("provider_blocks", ""),
                        operation_id=(
                            delivery_ownership.operation_id
                            if delivery_ownership is not None
                            else None
                        ),
                        delivery_kind=(
                            "continuation_message" if delivery_ownership is not None else None
                        ),
                        delivery_ordinal=(index if delivery_ownership is not None else None),
                    )
                )
            if delivery_ownership is not None:
                if self._write_operations is None:
                    if owned:
                        session.rollback()
                    return None
                session.flush()
                chained_id = pending.operation_id if pending is not None else None
                delivery_outcome = (
                    "fallback"
                    if delivery_failure_code is not None
                    else "chained_pending"
                    if pending is not None
                    else "final_response"
                )
                if not self._write_operations.complete_delivery(
                    session,
                    delivery_ownership,
                    outcome=cast(Any, delivery_outcome),
                    next_operation_id=chained_id,
                    failure_code=delivery_failure_code,
                ):
                    if owned:
                        session.rollback()
                    return None
            if owned:
                session.commit()
            return now

    def get_pending_clarification(self, conversation_id: int) -> tuple[PendingAction, str] | None:
        with self._session_factory() as session:
            conversation = session.get(Conversation, conversation_id)
            if conversation is None or not conversation.clarification_tool_name:
                return None
            return (
                PendingAction(
                    tool_call_id=conversation.clarification_tool_call_id,
                    tool_name=conversation.clarification_tool_name,
                    args=conversation.clarification_args,
                    human=conversation.clarification_human or conversation.clarification_tool_name,
                ),
                conversation.clarification_question,
            )

    def set_pending_clarification(
        self,
        conversation_id: int,
        pending: PendingAction,
        question: str,
    ) -> None:
        with self._session_factory() as session:
            session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(
                    clarification_tool_call_id=pending.tool_call_id,
                    clarification_tool_name=pending.tool_name,
                    clarification_args=pending.args,
                    clarification_human=pending.human,
                    clarification_question=question,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

    def clear_pending_clarification(self, conversation_id: int) -> None:
        with self._session_factory() as session:
            session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(
                    clarification_tool_call_id="",
                    clarification_tool_name="",
                    clarification_args="",
                    clarification_human="",
                    clarification_question="",
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

    def get_last_write_undo(self, conversation_id: int) -> dict[str, Any] | None:
        with self._session_factory() as session:
            conversation = session.get(Conversation, conversation_id)
            if conversation is None or not conversation.last_write_undo_json:
                return None
            try:
                payload = json.loads(conversation.last_write_undo_json)
            except json.JSONDecodeError:
                return None
            return payload if isinstance(payload, dict) else None

    def set_last_write_undo(self, conversation_id: int, undo: dict[str, Any]) -> None:
        parent_operation_id = undo.get("parent_operation_id")
        with self._session_factory() as session:
            session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(
                    last_write_undo_json=json.dumps(undo, ensure_ascii=False),
                    last_write_operation_id=(
                        parent_operation_id if isinstance(parent_operation_id, str) else ""
                    ),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

    def clear_last_write_undo(self, conversation_id: int) -> None:
        with self._session_factory() as session:
            session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(
                    last_write_undo_json="",
                    last_write_operation_id="",
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

    def clear_last_write_undo_if_matches(
        self,
        conversation_id: int,
        expected: dict[str, Any],
    ) -> bool:
        with self._session_factory() as session:
            result = session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .where(
                    Conversation.last_write_undo_json == json.dumps(expected, ensure_ascii=False)
                )
                .values(
                    last_write_undo_json="",
                    last_write_operation_id="",
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()
            return getattr(result, "rowcount", 0) == 1

    def get_last_write_operation_id(self, conversation_id: int) -> str:
        with self._session_factory() as session:
            value = session.scalar(
                select(Conversation.last_write_operation_id).where(
                    Conversation.id == conversation_id
                )
            )
            return str(value or "")

    def _create_operation_for_pending(
        self,
        session: Session,
        conversation_id: int,
        pending: PendingAction,
    ) -> None:
        if self._write_operations is None or not pending.operation_id:
            return
        if session.get(WriteOperation, pending.operation_id) is not None:
            return
        try:
            parsed = json.loads(pending.args)
        except json.JSONDecodeError:
            parsed = pending.args
        proposal = ledger_fingerprint(
            self._write_operations.key,
            "write-operation-proposal-v1",
            parsed,
        )
        token = _pending_confirmation_token(pending)
        token_fingerprint = ledger_fingerprint(
            self._write_operations.key,
            "write-operation-confirmation-token-v1",
            token.encode("ascii"),
        )
        self._write_operations.create_primary(
            session,
            operation_id=pending.operation_id,
            conversation_id=conversation_id,
            tool_call_id=pending.tool_call_id,
            tool_name=pending.tool_name,
            adapter_kind=(
                "legacy_deterministic"
                if pending.tool_name in LEGACY_WRITE_OPERATION_NAMES
                else "typed"
            ),
            proposal_fingerprint=proposal,
            confirmation_token_fingerprint=token_fingerprint,
        )

    def delete_conversation(self, conversation_id: int) -> None:
        with self._session_factory() as session:
            session.execute(
                delete(ChatMessage).where(ChatMessage.conversation_id == conversation_id)
            )
            conversation = session.get(Conversation, conversation_id)
            if conversation is not None:
                session.delete(conversation)
            session.commit()


def _canonical_context_type(value: object) -> str:
    if value is None or value == "":
        return "workspace"
    if type(value) is not str:
        raise ConversationScopeError("context_type must be a string")
    if value not in _SCOPE_CONTEXT_TYPES:
        raise ConversationScopeError("context_type is invalid")
    return value


def _canonical_context_ref(context_type: str, value: object) -> str:
    if context_type != "application":
        if value is None or value == "":
            return ""
        if type(value) is not str:
            raise ConversationScopeError("context_ref must be a string")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ConversationScopeError("context_ref must not contain surrogate characters") from exc
        if len(encoded) > 256:
            raise ConversationScopeError("context_ref must be at most 256 bytes")
        if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value):
            raise ConversationScopeError("context_ref must not contain control characters")
        return ""
    if value is None or value == "":
        raise ConversationScopeError("application context_ref is required")
    if type(value) is int:
        if not 0 < value <= _MAX_SCOPE_REVISION:
            raise ConversationScopeError("application context_ref must be a positive integer")
        return str(value)
    if type(value) is str and _ASCII_POSITIVE_INTEGER.fullmatch(value):
        try:
            integer = int(value)
        except ValueError as exc:  # pragma: no cover - regex already bounds syntax
            raise ConversationScopeError("application context_ref is invalid") from exc
        if integer <= 0 or integer > _MAX_SCOPE_REVISION:
            raise ConversationScopeError("application context_ref must be a positive integer")
        return value
    raise ConversationScopeError("application context_ref must be a canonical positive integer")


def _canonical_scope_mode(value: object) -> str:
    if value is None or value == "":
        return "general"
    if type(value) is not str:
        raise ConversationScopeError("mode must be a string")
    if value[0].isspace() or value[-1].isspace():
        raise ConversationScopeError("mode must not have edge whitespace")
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ConversationScopeError("mode must not contain surrogate characters") from exc
    if len(value) > 64 or encoded_length > 256:
        raise ConversationScopeError("mode must be at most 64 characters and 256 bytes")
    if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value):
        raise ConversationScopeError("mode must not contain control characters")
    return value


def _require_active_application(
    session: Session,
    mutation: ConversationScopeMutationSnapshot,
) -> None:
    if mutation.context_type != "application":
        return
    application_id = int(cast(str, mutation.context_ref))
    from offerpilot.ai.tool_authority.visibility import (
        AuthorityApplicationVisibilityError,
        AuthorityApplicationVisibilityQuery,
    )

    try:
        visible = AuthorityApplicationVisibilityQuery().execute_on_session(
            session,
            application_id,
        )
    except AuthorityApplicationVisibilityError as exc:
        raise ConversationScopeVisibilityFailure(
            "application context visibility could not be checked"
        ) from exc
    if visible is None:
        raise ConversationScopeUnavailable("application context is unavailable")


def _next_conversation_timestamp(
    session: Session,
    conversation_id: int,
    floor: datetime | None = None,
) -> datetime:
    current = session.scalar(
        select(Conversation.updated_at).where(Conversation.id == conversation_id)
    )
    bounds = [value for value in (current, floor) if value is not None]
    normalized_bounds = [
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None or value.utcoffset() is None
        else value.astimezone(timezone.utc)
        for value in bounds
    ]
    now = datetime.now(timezone.utc)
    if not normalized_bounds:
        return now
    lower_bound = max(normalized_bounds)
    return max(now, lower_bound + timedelta(microseconds=1))


def _pending_confirmation_token(pending: PendingAction) -> str:
    try:
        parsed = json.loads(pending.args)
        encoded = json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        encoded = pending.args
    identity = json.dumps(
        [pending.tool_call_id, pending.tool_name, encoded],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return sha256(identity.encode("utf-8")).hexdigest()
