from __future__ import annotations

import json
import hmac
import re
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping, cast

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from offerpilot.ai.agent_contracts import PendingAction
from offerpilot.ai.pending_replay import (
    PendingReplayArgsDecoderV1,
    PendingReplayIntegrityError,
)
from offerpilot.ai.tool_authority import (
    AuthorityFactory,
    AuthorityPhaseError,
    PendingAuthorityClaim,
    SegmentExecutionAuthority,
    TrustedContextScope,
)
from offerpilot.ai.tool_authority.fingerprint import authorization_scope_fingerprint
from offerpilot.ai.tool_runtime.contracts import JSONValue
from offerpilot.ai.write_operations import (
    ClarificationPendingRouteHandle,
    DeliveryOwnership,
    PendingPersistenceRouteHandle,
    _PendingRouteResolution,
    WriteOperationRepository,
    abandon_pending_persistence_route,
    ledger_fingerprint,
    pending_action_identity,
    pending_route_claim_for_cleanup,
    require_pending_persistence_route,
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


class _PendingClaimCASLost(RuntimeError):
    """Internal control flow which revokes a one-shot Pending claim."""


@dataclass(slots=True)
class _PendingClaimLease:
    """One nested call-chain's ownership of a one-shot Pending claim."""

    claim: PendingAuthorityClaim
    factory: AuthorityFactory
    transaction_owner: object | None = None
    session: Session | None = None
    committed: bool = False

    def bind_transaction(self, owner: object, session: Session) -> None:
        if self.committed:
            raise AuthorityPhaseError("Typed Pending claim is already committed")
        if self.transaction_owner is not None or self.session is not None:
            raise AuthorityPhaseError("Typed Pending claim already owns a transaction")
        self.transaction_owner = owner
        self.session = session

    def require_transaction(self, owner: object, session: Session) -> None:
        if self.transaction_owner is not owner or self.session is not session:
            raise AuthorityPhaseError(
                "Typed Pending persistence requires its exact active transaction"
            )

    def commit_transaction(self, owner: object, session: Session) -> None:
        self.require_transaction(owner, session)
        self.factory.consume(self.claim)
        self.committed = True

    def release_transaction(self, owner: object, session: Session) -> None:
        self.require_transaction(owner, session)
        self.transaction_owner = None
        self.session = None


_ACTIVE_PENDING_CLAIM_LEASE: ContextVar[_PendingClaimLease | None] = ContextVar(
    "offerpilot_active_pending_claim_lease",
    default=None,
)


@contextmanager
def _pending_claim_lifecycle(
    claim: PendingAuthorityClaim | None,
) -> Any:
    """Own one claim across nested coordinator/repository calls.

    The outermost entry marks the claim in flight.  Only the exact repository
    transaction can mark the lease committed; every other normal return and
    every exception (including cancellation) revokes it.
    """

    if claim is None:
        yield None
        return
    if type(claim) is not PendingAuthorityClaim:
        raise AuthorityPhaseError("Typed Pending claim has an invalid type")
    active = _ACTIVE_PENDING_CLAIM_LEASE.get()
    if active is not None and active.claim is claim:
        yield active
        return

    factory = _pending_claim_factory(claim)
    try:
        factory.mark_in_flight(claim)
    except BaseException:
        factory._revoke_lifecycle_after_entry_failure(claim, owns_in_flight=False)
        raise
    lease = _PendingClaimLease(claim, factory)
    token = _ACTIVE_PENDING_CLAIM_LEASE.set(lease)
    try:
        yield lease
    finally:
        _ACTIVE_PENDING_CLAIM_LEASE.reset(token)
        if not lease.committed:
            factory._revoke_lifecycle_after_entry_failure(claim, owns_in_flight=True)


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

    @contextmanager
    def _typed_pending_transaction(
        self,
        claim: PendingAuthorityClaim,
    ) -> Any:
        """Own a Pending claim from before lock acquisition through commit."""

        with _pending_claim_lifecycle(claim) as lease:
            if lease is None:  # pragma: no cover - exact claim required by the signature
                raise AuthorityPhaseError("Typed Pending claim lifecycle is unavailable")
            if self._session is not None:
                raise AuthorityPhaseError(
                    "Typed Pending persistence requires the transaction-owning repository"
                )
            with self._operation_session() as (session, owned):
                if not owned:  # pragma: no cover - guarded above
                    raise AuthorityPhaseError("Typed Pending transaction ownership was lost")
                lease.bind_transaction(self, session)
                try:
                    yield session
                    session.commit()
                    lease.commit_transaction(self, session)
                except BaseException:
                    session.rollback()
                    raise
                finally:
                    lease.release_transaction(self, session)

    @contextmanager
    def _pending_route_transaction(
        self,
        pending: PendingAction,
        route_handle: PendingPersistenceRouteHandle,
        conversation_id: int,
    ) -> Any:
        try:
            route = self._require_pending_route(pending, route_handle, conversation_id)
        except BaseException:
            claim = pending_route_claim_for_cleanup(route_handle)
            if type(claim) is PendingAuthorityClaim:
                _pending_claim_factory(claim)._revoke_lifecycle_after_entry_failure(
                    claim,
                    owns_in_flight=False,
                )
            raise
        if route.adapter_kind == "typed":
            if type(route.claim) is not PendingAuthorityClaim:
                raise AuthorityPhaseError("Typed Pending requires its exact authority claim")
            typed_claim = route.claim
            with self._typed_pending_transaction(typed_claim) as session:
                yield session, False, route, typed_claim
            return
        with self._operation_session() as (session, owned):
            yield session, owned, route, None

    @contextmanager
    def _optional_pending_route_transaction(
        self,
        pending: PendingAction | None,
        route_handle: PendingPersistenceRouteHandle | None,
        conversation_id: int,
    ) -> Any:
        if pending is not None:
            if route_handle is None:
                raise TypeError("Pending persistence requires an exact route handle")
            try:
                with self._pending_route_transaction(
                    pending,
                    route_handle,
                    conversation_id,
                ) as route:
                    yield route
            except _PendingClaimCASLost:
                return
            return
        if route_handle is not None:
            raise TypeError("Terminal persistence cannot consume a Pending route")
        with self._operation_session() as (session, owned):
            yield session, owned, None, None

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
        if mutation is not None and not isinstance(mutation, ConversationScopeMutationSnapshot):
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

    def set_pending_action(
        self,
        conversation_id: int,
        pending: PendingAction,
        *,
        route_handle: PendingPersistenceRouteHandle,
    ) -> bool:
        try:
            with self._pending_route_transaction(pending, route_handle, conversation_id) as (
                session,
                owned,
                route,
                typed_claim,
            ):
                if typed_claim is not None:
                    self._validate_typed_pending(session, conversation_id, pending, typed_claim)
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
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                if getattr(result, "rowcount", 0) != 1:
                    if typed_claim is not None:
                        raise _PendingClaimCASLost
                    if owned:
                        session.rollback()
                    return False
                self._persist_routed_pending(session, conversation_id, pending, route_handle, route)
                if owned:
                    session.commit()
                return True
        except _PendingClaimCASLost:
            return False

    def persist_pending_action(
        self,
        conversation_id: int,
        pending: PendingAction,
        messages: list[dict[str, str]],
        *,
        route_handle: PendingPersistenceRouteHandle,
    ) -> bool:
        """Atomically persist a write proposal and make it the pending action."""
        try:
            with self._pending_route_transaction(pending, route_handle, conversation_id) as (
                session,
                owned,
                route,
                typed_claim,
            ):
                if typed_claim is not None:
                    self._validate_typed_pending(session, conversation_id, pending, typed_claim)
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
                    if typed_claim is not None:
                        raise _PendingClaimCASLost
                    if owned:
                        session.rollback()
                    return False
                self._persist_routed_pending(session, conversation_id, pending, route_handle, route)
                self._add_pending_messages(session, conversation_id, messages)
                if owned:
                    session.commit()
                return True
        except _PendingClaimCASLost:
            return False

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
        route_handle: PendingPersistenceRouteHandle,
    ) -> datetime | None:
        """Atomically replace a stale pending card only when the original still owns it."""
        if delivery_ownership is None:
            abandon_pending_persistence_route(route_handle)
            raise TypeError("chained Pending replacement requires exact parent ownership")
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
        try:
            with self._pending_route_transaction(replacement, route_handle, conversation_id) as (
                session,
                owned,
                route,
                typed_claim,
            ):
                delivery_ownership.bind_chained_transition(route_handle)
                if typed_claim is not None:
                    self._validate_typed_pending(session, conversation_id, replacement, typed_claim)
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
                    statement = statement.where(
                        Conversation.pending_confirmation_claim_id == claim_id
                    )
                result = session.execute(statement.values(**values))
                if getattr(result, "rowcount", 0) != 1:
                    if typed_claim is not None:
                        raise _PendingClaimCASLost
                    if owned:
                        session.rollback()
                    return None
                self._persist_routed_pending(
                    session,
                    conversation_id,
                    replacement,
                    route_handle,
                    route,
                )
                session.add(
                    ChatMessage(
                        conversation_id=conversation_id,
                        role=tool_message.role,
                        content=tool_message.content,
                        tool_call_id=tool_message.tool_call_id,
                        operation_id=(
                            delivery_ownership.operation_id if delivery_ownership else None
                        ),
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
                        if typed_claim is not None:
                            raise _PendingClaimCASLost
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
                        if typed_claim is not None:
                            raise _PendingClaimCASLost
                        if owned:
                            session.rollback()
                        return None
                if owned:
                    session.commit()
                return now
        except _PendingClaimCASLost:
            return None

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
        route_handle: PendingPersistenceRouteHandle | None,
    ) -> datetime | None:
        if pending is not None and delivery_ownership is None:
            if route_handle is not None:
                abandon_pending_persistence_route(route_handle)
            raise TypeError("chained Pending continuation requires exact parent ownership")
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
        routed_pending = (
            pending if pending is not None else clarification[0] if clarification else None
        )
        with self._optional_pending_route_transaction(
            routed_pending, route_handle, conversation_id
        ) as (session, owned, route, typed_claim):
            if expected_generation is None:
                if typed_claim is not None:
                    raise _PendingClaimCASLost
                if owned:
                    session.rollback()
                return None
            if pending is not None:
                assert route_handle is not None
                assert delivery_ownership is not None
                delivery_ownership.bind_chained_transition(route_handle)
            if typed_claim is not None and pending is not None:
                self._validate_typed_pending(session, conversation_id, pending, typed_claim)
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
                if typed_claim is not None:
                    raise _PendingClaimCASLost
                if owned:
                    session.rollback()
                return None
            if pending is not None:
                assert route_handle is not None and route is not None
                self._persist_routed_pending(
                    session,
                    conversation_id,
                    pending,
                    route_handle,
                    route,
                )
            if delivery_ownership is not None:
                if origin_message is None or expected_pending is None:
                    if typed_claim is not None:
                        raise _PendingClaimCASLost
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
                    if typed_claim is not None:
                        raise _PendingClaimCASLost
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
                    if typed_claim is not None:
                        raise _PendingClaimCASLost
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
        *,
        route_handle: ClarificationPendingRouteHandle,
    ) -> None:
        route = self._require_pending_route(
            pending,
            route_handle,
            conversation_id,
            clarification=True,
        )
        if route.adapter_kind != "clarification":
            raise TypeError("Clarification persistence requires a clarification route")
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

    def _require_pending_route(
        self,
        pending: PendingAction,
        route_handle: PendingPersistenceRouteHandle,
        conversation_id: int,
        *,
        clarification: bool = False,
    ) -> _PendingRouteResolution:
        arguments_digest, revision = pending_action_identity(
            pending.tool_call_id,
            pending.tool_name,
            pending.args,
        )
        if not clarification:
            arguments_digest = pending.arguments_digest or arguments_digest
            revision = pending.pending_action_revision or revision
        operationless = clarification or type(route_handle) is ClarificationPendingRouteHandle
        operation_id = "" if operationless else pending.operation_id
        claim_id = (
            ""
            if operationless
            else str(pending.pending_confirmation_claim_id or pending.operation_id)
        )
        route_tool_name = "" if operationless else pending.tool_name
        return require_pending_persistence_route(
            route_handle,
            conversation_id=conversation_id,
            operation_id=operation_id,
            tool_call_id=pending.tool_call_id,
            tool_name=route_tool_name,
            pending_action_revision=revision,
            pending_confirmation_claim_id=claim_id,
            arguments_digest=arguments_digest,
        )

    @staticmethod
    def _add_pending_messages(
        session: Session,
        conversation_id: int,
        messages: list[dict[str, str]],
    ) -> None:
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

    def _persist_routed_pending(
        self,
        session: Session,
        conversation_id: int,
        pending: PendingAction,
        route_handle: PendingPersistenceRouteHandle,
        route: _PendingRouteResolution,
    ) -> None:
        if route.adapter_kind == "clarification":
            return
        if self._write_operations is None:
            raise AuthorityPhaseError("Pending operation repository is unavailable")
        if session.get(WriteOperation, pending.operation_id) is not None:
            raise AuthorityPhaseError("Pending operation identity already exists")
        scope_fingerprint: str | None = None
        if route.adapter_kind == "typed":
            pending_authority_claim = route.claim
            if type(pending_authority_claim) is not PendingAuthorityClaim:
                raise AuthorityPhaseError("Typed Pending claim has an invalid type")
            lease = _ACTIVE_PENDING_CLAIM_LEASE.get()
            if lease is None:  # pragma: no cover - exact claim required by the signature
                raise AuthorityPhaseError("Typed Pending claim lifecycle is unavailable")
            lease.require_transaction(self, session)
            scope_fingerprint, arguments = self._validate_typed_pending(
                session,
                conversation_id,
                pending,
                pending_authority_claim,
            )
            proposal_value: JSONValue = cast(JSONValue, dict(arguments))
        else:
            try:
                proposal_value = cast(JSONValue, json.loads(pending.args))
            except json.JSONDecodeError:
                proposal_value = pending.args
        proposal = ledger_fingerprint(
            self._write_operations.key,
            "write-operation-proposal-v1",
            proposal_value,
        )
        token_fingerprint = ledger_fingerprint(
            self._write_operations.key,
            "write-operation-confirmation-token-v1",
            _pending_confirmation_token(pending).encode("ascii"),
        )
        self._write_operations.create_primary(
            session,
            route_handle=route_handle,
            operation_id=pending.operation_id,
            conversation_id=conversation_id,
            tool_call_id=pending.tool_call_id,
            tool_name=pending.tool_name,
            pending_action_revision=route.identity.pending_action_revision,
            pending_confirmation_claim_id=route.identity.pending_confirmation_claim_id,
            arguments_digest=route.identity.arguments_digest,
            proposal_fingerprint=proposal,
            confirmation_token_fingerprint=token_fingerprint,
            authorization_scope_fingerprint=scope_fingerprint,
        )

    def _validate_typed_pending(
        self,
        session: Session,
        conversation_id: int,
        pending: PendingAction,
        claim: PendingAuthorityClaim,
    ) -> tuple[str, Mapping[str, Any]]:
        if type(claim) is not PendingAuthorityClaim:
            raise AuthorityPhaseError("Typed Pending claim has an invalid type")
        factory = _pending_claim_factory(claim)
        lifecycle = factory._claim_lifecycle(claim)
        if lifecycle.state != "in_flight":
            raise AuthorityPhaseError("Typed Pending claim is not in flight")
        if lifecycle.pending is not pending:
            raise AuthorityPhaseError("Typed Pending object identity does not match claim")
        if type(lifecycle.authority) is not SegmentExecutionAuthority:
            raise AuthorityPhaseError("Typed Pending claim requires Segment authority")
        authority = lifecycle.authority
        prepared_record = factory._prepared_record(lifecycle.prepared)
        if (
            claim.authority_instance_token is not authority.authority_instance_token
            or claim.pending_identity is not factory.pending_token(pending)
            or claim.prepared_instance_token is not prepared_record[1]
        ):
            raise AuthorityPhaseError("Typed Pending source identity does not match claim")
        if (
            type(conversation_id) is not int
            or claim.conversation_id != conversation_id
            or authority.conversation_id != conversation_id
            or claim.segment_id != authority.segment_id
            or claim.operation_id != pending.operation_id
            or claim.tool_call_id != pending.tool_call_id
            or claim.tool_name != pending.tool_name
            or claim.pending_confirmation_claim_id
            != getattr(pending, "pending_confirmation_claim_id", None)
        ):
            raise AuthorityPhaseError("Typed Pending semantic identity does not match claim")
        arguments = _canonical_pending_arguments(pending.args)
        arguments_digest = _pending_arguments_digest(arguments)
        if not hmac.compare_digest(arguments_digest, claim.arguments_digest):
            raise AuthorityPhaseError("Typed Pending arguments changed after prepare")

        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            raise AuthorityPhaseError("Typed Pending conversation is unavailable")
        mutation = ConversationScopeMutationSnapshot(
            context_type=conversation.context_type,
            context_ref=conversation.context_ref,
            mode=conversation.mode,
        )
        _require_active_application(session, mutation)
        trusted_scope = claim.trusted_scope
        if type(trusted_scope) is not TrustedContextScope:
            raise AuthorityPhaseError("Typed Pending trusted scope has an invalid type")
        trusted_context_ref = (
            trusted_scope.context_ref if trusted_scope.context_type == "application" else None
        )
        locked_context_ref = (
            int(cast(str, mutation.context_ref)) if mutation.context_type == "application" else None
        )
        if (
            conversation.scope_revision != claim.conversation_scope_revision
            or authority.conversation_scope_revision != claim.conversation_scope_revision
            or mutation.context_type != trusted_scope.context_type
            or locked_context_ref != trusted_context_ref
            or mutation.mode != trusted_scope.mode
        ):
            raise AuthorityPhaseError("Typed Pending authorization scope changed")
        if self._write_operations is None:
            raise AuthorityPhaseError("Typed Pending operation repository is unavailable")
        return (
            authorization_scope_fingerprint(
                self._write_operations.key,
                conversation_id=conversation_id,
                conversation_scope_revision=conversation.scope_revision,
                context_type=cast(str, mutation.context_type),
                context_ref=locked_context_ref,
                mode=mutation.mode,
                capability_profile_id=claim.capability_profile_id,
                capability_policy_version=claim.capability_policy_version,
                binding_policy_version=claim.binding_policy_version,
                capability_profile_fingerprint=claim.capability_profile_fingerprint,
                binding_policy_fingerprint=claim.binding_policy_fingerprint,
            ),
            arguments,
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
            raise ConversationScopeError(
                "context_ref must not contain surrogate characters"
            ) from exc
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


def _canonical_pending_arguments(raw: str) -> Mapping[str, Any]:
    try:
        value = PendingReplayArgsDecoderV1().decode(raw)
    except PendingReplayIntegrityError as exc:
        raise AuthorityPhaseError("Typed Pending arguments are not canonical JSON") from exc
    return cast(Mapping[str, Any], value)


def _pending_arguments_digest(arguments: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _pending_claim_factory(claim: PendingAuthorityClaim) -> AuthorityFactory:
    """Resolve only the exact live factory which registered this claim object."""

    if type(claim) is not PendingAuthorityClaim:
        raise AuthorityPhaseError("Typed Pending claim has an invalid type")
    from offerpilot.ai.tool_authority import composition

    with composition._ACTIVE_AUTHORITIES_LOCK:
        found = composition._ACTIVE_OBJECTS.get(id(claim))
    if found is None or found[0] is not claim or type(found[1]) is not AuthorityFactory:
        raise AuthorityPhaseError("Typed Pending claim is not active")
    return found[1]
