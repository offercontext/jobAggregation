from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, Thread, current_thread
from typing import Any, Callable, List
from uuid import uuid4

from sqlalchemy import select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from offerpilot.ai.agent_contracts import ChatModel
from offerpilot.ai.interview_preparation_proposals import (
    InterviewPreparationModelError,
    generate_interview_preparation_proposal,
)
from offerpilot.knowledge.interview_capture import note_fingerprint
from offerpilot.models import (
    Application,
    ApplicationEvent,
    ApplicationJDVersion,
    InterviewPreparationProposal,
    KnowledgeEvidence,
    KnowledgeCapturedSourceMetadata,
    KnowledgeNote,
    KnowledgeNoteEvidence,
    KnowledgeNoteVersion,
    KnowledgeSource,
    InterviewNote,
    Resume,
)
from offerpilot.repositories.json_contract import canonical_json, parse_json_object, sha256_text


LEASE_SECONDS = 30
HEARTBEAT_INTERVAL_SECONDS = 10
HEARTBEAT_RETRY_ATTEMPTS = 2
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class InterviewPreparationNotFound(Exception):
    def __init__(self, code: str = "interview_preparation_application_not_found") -> None:
        super().__init__(code)
        self.code = code


class InterviewPreparationValidationError(ValueError):
    def __init__(self, message: str, code: str = "interview_preparation_invalid_request") -> None:
        super().__init__(message)
        self.code = code


class InterviewPreparationConflictError(ValueError):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class InterviewPreparationProviderError(RuntimeError):
    code = "interview_preparation_provider_error"


@dataclass
class InterviewPreparationGenerationResult:
    proposal: InterviewPreparationProposal | None
    created: bool
    pending: bool
    attempt_status: str


@dataclass
class _InterviewPreparationOwnedGeneration:
    attempt_id: int
    owner_revision: int
    owner_token: str


def _attempt_invalidated() -> InterviewPreparationConflictError:
    return InterviewPreparationConflictError(
        "interview preparation attempt was invalidated",
        "interview_preparation_attempt_invalidated",
    )


def _result_for_existing(
    row: InterviewPreparationProposal,
) -> InterviewPreparationGenerationResult:
    if row.attempt_status == "ready":
        return InterviewPreparationGenerationResult(row, False, False, "ready")
    if row.attempt_status in {"generating", "provider_unknown"}:
        return InterviewPreparationGenerationResult(row, False, True, row.attempt_status)
    if row.attempt_status == "invalidated":
        raise _attempt_invalidated()
    raise InterviewPreparationConflictError(
        "interview preparation attempt has an unsupported state",
        "interview_preparation_idempotency_conflict",
    )


class _InterviewPreparationLeaseHeartbeat:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        attempt_id: int,
        owner_revision: int,
        owner_token: str,
        lease_seconds: int,
        now_factory: Callable[[], datetime],
        heartbeat_interval_seconds: int = HEARTBEAT_INTERVAL_SECONDS,
        waiter: Callable[[float], bool] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._attempt_id = attempt_id
        self._owner_revision = owner_revision
        self._owner_token = owner_token
        self._lease_seconds = lease_seconds
        self._now_factory = now_factory
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._stop_event = Event()
        self._waiter = waiter or self._stop_event.wait
        self._thread: Thread | None = None
        self.heartbeat_count = 0
        self.confirmed_ownership_lost = False
        self.heartbeat_uncertain = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = Thread(target=self._run, name="interview-preparation-lease", daemon=True)
        self._thread.start()

    def stop_and_join(self) -> None:
        self._stop_event.set()
        wake = getattr(self._waiter, "wake", None)
        if callable(wake):
            wake()
        if self._thread is not None and self._thread is not current_thread():
            self._thread.join()

    def renew_once(self) -> bool:
        for attempt in range(HEARTBEAT_RETRY_ATTEMPTS):
            try:
                return self._renew_once()
            except SQLAlchemyError:
                if attempt + 1 == HEARTBEAT_RETRY_ATTEMPTS:
                    self.heartbeat_uncertain = True
                    return False

        self.heartbeat_uncertain = True
        return False

    def _renew_once(self) -> bool:
        with self._session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            result = session.execute(
                update(InterviewPreparationProposal)
                .where(InterviewPreparationProposal.id == self._attempt_id)
                .where(InterviewPreparationProposal.attempt_status == "generating")
                .where(
                    InterviewPreparationProposal.generation_revision
                    == self._owner_revision
                )
                .where(
                    InterviewPreparationProposal.provider_call_token == self._owner_token
                )
                .values(
                    provider_lease_until=_to_db_naive(
                        self._now_factory() + timedelta(seconds=self._lease_seconds)
                    )
                )
            )
            if getattr(result, "rowcount", 0) == 1:
                session.commit()
                self.heartbeat_count += 1
                return True
            row = session.get(InterviewPreparationProposal, self._attempt_id)
            session.rollback()
            if (
                row is None
                or row.attempt_status != "generating"
                or row.generation_revision != self._owner_revision
                or row.provider_call_token != self._owner_token
            ):
                self.confirmed_ownership_lost = True
            else:
                self.heartbeat_uncertain = True
            return False

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                self._waiter(self._heartbeat_interval_seconds)
                if self._stop_event.is_set():
                    return
                self.renew_once()
                if self.confirmed_ownership_lost or self.heartbeat_uncertain:
                    return
        except Exception:
            self.heartbeat_uncertain = True


class InterviewPreparationProposalsRepository:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        lease_seconds: int = LEASE_SECONDS,
        heartbeat_interval_seconds: int = HEARTBEAT_INTERVAL_SECONDS,
        now_factory: Callable[[], datetime] | None = None,
        waiter: Callable[[float], bool] | None = None,
    ):
        self._session_factory = session_factory
        self._lease_seconds = lease_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self._waiter = waiter

    def create_generated(
        self,
        *,
        application_id: int,
        event_id: int,
        resume_id: int,
        jd_text: str,
        knowledge_selections: list[dict[str, Any]],
        user_assertions: list[str],
        idempotency_key: str,
        model: ChatModel | None,
        on_diagnostic: Any | None = None,
        jd_version_id: int | None = None,
    ) -> InterviewPreparationGenerationResult:
        if not isinstance(idempotency_key, str) or not IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
            raise InterviewPreparationValidationError(
                "idempotency_key must be 16-128 ASCII characters",
                "interview_preparation_invalid_request",
            )
        owner_attempt_id: int
        owner_revision: int
        owner_token: str
        with self._session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            if jd_version_id is not None:
                current_version_id = session.scalar(
                    select(ApplicationJDVersion.id)
                    .where(ApplicationJDVersion.application_id == application_id)
                    .order_by(ApplicationJDVersion.version_number.desc())
                    .limit(1)
                )
                if current_version_id != jd_version_id:
                    raise InterviewPreparationConflictError(
                        "interview preparation source changed",
                        "interview_preparation_source_conflict",
                    )
            snapshot = _build_snapshot(
                session,
                application_id=application_id,
                event_id=event_id,
                resume_id=resume_id,
                jd_text=jd_text,
                knowledge_selections=knowledge_selections,
                user_assertions=user_assertions,
                jd_version_id=jd_version_id,
            )
            fingerprint = sha256_text(canonical_json(snapshot))
            existing = _find_by_key(session, application_id, event_id, idempotency_key)
            if existing is not None:
                result = self._handle_existing(
                    session,
                    existing,
                    fingerprint=fingerprint,
                    snapshot=snapshot,
                    model=model,
                    application_id=application_id,
                    event_id=event_id,
                    resume_id=resume_id,
                    jd_text=jd_text,
                    knowledge_selections=knowledge_selections,
                    user_assertions=user_assertions,
                    jd_version_id=jd_version_id,
                    idempotency_key=idempotency_key,
                    on_diagnostic=on_diagnostic,
                )
                if result is None:
                    raise InterviewPreparationProviderError()
                if isinstance(result, InterviewPreparationGenerationResult):
                    return result
                owner_attempt_id = result.attempt_id
                owner_revision = result.owner_revision
                owner_token = result.owner_token
            else:
                token = uuid4().hex
                row = InterviewPreparationProposal(
                    application_id=application_id,
                    application_event_id=event_id,
                    resume_id=resume_id,
                    jd_version_id=jd_version_id,
                    idempotency_key=idempotency_key,
                    attempt_status="generating",
                    generation_revision=1,
                    provider_call_token=token,
                    provider_lease_until=self._lease_until(),
                    input_snapshot_json=canonical_json(snapshot),
                    source_fingerprint=fingerprint,
                )
                session.add(row)
                session.commit()
                owner_attempt_id = row.id
                owner_revision = 1
                owner_token = token

        return self._call_and_store(
            model=model,
            attempt_id=owner_attempt_id,
            owner_revision=owner_revision,
            owner_token=owner_token,
            application_id=application_id,
            event_id=event_id,
            resume_id=resume_id,
            jd_text=jd_text,
            jd_version_id=jd_version_id,
            knowledge_selections=knowledge_selections,
            user_assertions=user_assertions,
            idempotency_key=idempotency_key,
            source_fingerprint=fingerprint,
            snapshot=snapshot,
            on_diagnostic=on_diagnostic,
        )

    def preflight(
        self,
        *,
        application_id: int,
        event_id: int,
        resume_id: int,
        jd_text: str,
        knowledge_selections: List[dict[str, Any]],
        user_assertions: List[str],
        idempotency_key: str,
        jd_version_id: int | None = None,
    ) -> InterviewPreparationGenerationResult | None:
        """Return a replayable attempt without resolving the AI provider.

        A ready result and an unexpired lease are safe to return before provider
        configuration is loaded.  ``None`` means a new row or an expired lease
        needs a provider call, so the caller must resolve the provider before
        invoking ``create_generated``.
        """
        if not isinstance(idempotency_key, str) or not IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
            raise InterviewPreparationValidationError(
                "idempotency_key must be 16-128 ASCII characters",
                "interview_preparation_invalid_request",
            )
        with self._session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            snapshot = _build_snapshot(
                session,
                application_id=application_id,
                event_id=event_id,
                resume_id=resume_id,
                jd_text=jd_text,
                knowledge_selections=knowledge_selections,
                user_assertions=user_assertions,
                jd_version_id=jd_version_id,
            )
            fingerprint = sha256_text(canonical_json(snapshot))
            existing = _find_by_key(session, application_id, event_id, idempotency_key)
            if existing is None:
                session.commit()
                return None
            result = self._handle_existing(
                session,
                existing,
                fingerprint=fingerprint,
                snapshot=snapshot,
                model=None,
                application_id=application_id,
                event_id=event_id,
                resume_id=resume_id,
                jd_text=jd_text,
                knowledge_selections=knowledge_selections,
                user_assertions=user_assertions,
                jd_version_id=jd_version_id,
                idempotency_key=idempotency_key,
                on_diagnostic=None,
            )
            if isinstance(result, _InterviewPreparationOwnedGeneration):
                raise AssertionError("preflight cannot claim an expired attempt")
            return result

    def list(self, application_id: int) -> list[InterviewPreparationProposal]:
        with self._session_factory() as session:
            _require_visible_application(session, application_id)
            rows = list(
                session.scalars(
                    select(InterviewPreparationProposal)
                    .where(InterviewPreparationProposal.application_id == application_id)
                    .where(InterviewPreparationProposal.attempt_status == "ready")
                    .order_by(
                        InterviewPreparationProposal.created_at.desc(),
                        InterviewPreparationProposal.id.desc(),
                    )
                )
            )
            for row in rows:
                _set_source_status(session, row)
            return rows

    def get(self, application_id: int, proposal_id: int) -> InterviewPreparationProposal | None:
        with self._session_factory() as session:
            _require_visible_application(session, application_id)
            row = session.scalar(
                select(InterviewPreparationProposal)
                .where(InterviewPreparationProposal.application_id == application_id)
                .where(InterviewPreparationProposal.id == proposal_id)
                .where(InterviewPreparationProposal.attempt_status == "ready")
            )
            if row is not None:
                _set_source_status(session, row)
            return row

    def _handle_existing(
        self,
        session: Session,
        row: InterviewPreparationProposal,
        *,
        fingerprint: str,
        snapshot: dict[str, Any],
        model: ChatModel | None,
        application_id: int,
        event_id: int,
        resume_id: int,
        jd_text: str,
        knowledge_selections: List[dict[str, Any]],
        user_assertions: List[str],
        idempotency_key: str,
        on_diagnostic: Any | None,
        jd_version_id: int | None = None,
    ) -> InterviewPreparationGenerationResult | _InterviewPreparationOwnedGeneration | None:
        if row.attempt_status == "invalidated":
            raise _attempt_invalidated()
        if row.source_fingerprint != fingerprint:
            if row.attempt_status in {"generating", "provider_unknown"}:
                session.execute(
                    update(InterviewPreparationProposal)
                    .where(InterviewPreparationProposal.id == row.id)
                    .where(
                        InterviewPreparationProposal.attempt_status.in_(
                            ["generating", "provider_unknown"]
                        )
                    )
                    .where(
                        InterviewPreparationProposal.generation_revision
                        == row.generation_revision
                    )
                    .where(
                        InterviewPreparationProposal.provider_call_token
                        == row.provider_call_token
                    )
                    .values(
                        attempt_status="invalidated",
                        invalidation_reason="idempotency_conflict",
                        provider_call_token="",
                        provider_lease_until=None,
                    )
                )
                session.commit()
            raise InterviewPreparationConflictError(
                "interview preparation idempotency key has a different snapshot",
                "interview_preparation_idempotency_conflict",
            )
        if row.attempt_status == "ready":
            _set_source_status(session, row)
            return InterviewPreparationGenerationResult(row, False, False, "ready")

        lease_until = _as_aware(row.provider_lease_until)
        if lease_until is not None and lease_until > self._now_factory():
            return InterviewPreparationGenerationResult(row, False, True, row.attempt_status)

        if model is None:
            session.commit()
            return None

        old_revision = row.generation_revision
        old_token = row.provider_call_token
        new_token = uuid4().hex
        result = session.execute(
            update(InterviewPreparationProposal)
            .where(InterviewPreparationProposal.id == row.id)
            .where(InterviewPreparationProposal.attempt_status.in_(["generating", "provider_unknown"]))
            .where(InterviewPreparationProposal.generation_revision == old_revision)
            .where(InterviewPreparationProposal.provider_call_token == old_token)
            .where(
                InterviewPreparationProposal.provider_lease_until
                <= self._db_now()
            )
            .values(
                attempt_status="generating",
                generation_revision=old_revision + 1,
                provider_call_token=new_token,
                provider_lease_until=self._lease_until(),
                invalidation_reason="",
            )
        )
        if getattr(result, "rowcount", 0) != 1:
            session.commit()
            refreshed = session.get(InterviewPreparationProposal, row.id)
            if refreshed is None:
                raise InterviewPreparationConflictError(
                    "interview preparation attempt disappeared",
                    "interview_preparation_idempotency_conflict",
                )
            return _result_for_existing(refreshed)
        session.commit()
        return _InterviewPreparationOwnedGeneration(
            attempt_id=row.id,
            owner_revision=old_revision + 1,
            owner_token=new_token,
        )

    def _lease_until(self) -> datetime:
        return _to_db_naive(self._now_factory() + timedelta(seconds=self._lease_seconds))

    def _db_now(self) -> datetime:
        return _to_db_naive(self._now_factory())

    def _call_and_store(
        self,
        *,
        model: ChatModel | None,
        attempt_id: int | None = None,
        owner_revision: int,
        owner_token: str,
        application_id: int,
        event_id: int,
        resume_id: int,
        jd_text: str,
        knowledge_selections: List[dict[str, Any]],
        user_assertions: List[str],
        idempotency_key: str,
        source_fingerprint: str,
        snapshot: dict[str, Any],
        on_diagnostic: Any | None,
        jd_version_id: int | None = None,
    ) -> InterviewPreparationGenerationResult:
        if model is None:
            raise InterviewPreparationProviderError()
        heartbeat = (
            _InterviewPreparationLeaseHeartbeat(
                session_factory=self._session_factory,
                attempt_id=attempt_id,
                owner_revision=owner_revision,
                owner_token=owner_token,
                lease_seconds=self._lease_seconds,
                heartbeat_interval_seconds=self._heartbeat_interval_seconds,
                now_factory=self._now_factory,
                waiter=self._waiter,
            )
            if attempt_id is not None
            else None
        )
        if heartbeat is not None:
            heartbeat.start()
        try:
            try:
                proposal = generate_interview_preparation_proposal(
                    model,
                    snapshot,
                    on_diagnostic=on_diagnostic,
                )
            except InterviewPreparationModelError as exc:
                if exc.failure_category != "provider_error":
                    raise
                self._mark_provider_unknown(
                    application_id, event_id, idempotency_key, owner_revision, owner_token
                )
                raise InterviewPreparationProviderError() from exc
        finally:
            if heartbeat is not None:
                heartbeat.stop_and_join()

        if heartbeat is not None and heartbeat.confirmed_ownership_lost:
            with self._session_factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                current = _find_by_key(session, application_id, event_id, idempotency_key)
                if current is None:
                    session.rollback()
                    raise InterviewPreparationConflictError(
                        "interview preparation attempt disappeared",
                        "interview_preparation_idempotency_conflict",
                    )
                session.commit()
                return _result_for_existing(current)

        with self._session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = _find_by_key(session, application_id, event_id, idempotency_key)
            if row is None:
                session.rollback()
                raise InterviewPreparationConflictError(
                    "interview preparation attempt disappeared",
                    "interview_preparation_idempotency_conflict",
                )
            try:
                current_snapshot = _build_snapshot(
                    session,
                    application_id=application_id,
                    event_id=event_id,
                    resume_id=resume_id,
                    jd_text=jd_text,
                    jd_version_id=jd_version_id,
                    knowledge_selections=knowledge_selections,
                    user_assertions=user_assertions,
                )
            except InterviewPreparationNotFound as exc:
                if exc.code == "interview_preparation_application_not_found":
                    session.rollback()
                    raise
                current_snapshot = None
            except InterviewPreparationValidationError:
                current_snapshot = None
            if current_snapshot is None:
                invalidated = session.execute(
                    update(InterviewPreparationProposal)
                    .where(InterviewPreparationProposal.id == row.id)
                    .where(InterviewPreparationProposal.attempt_status == "generating")
                    .where(InterviewPreparationProposal.generation_revision == owner_revision)
                    .where(InterviewPreparationProposal.provider_call_token == owner_token)
                    .values(
                        attempt_status="invalidated",
                        invalidation_reason="source_conflict",
                        provider_call_token="",
                        provider_lease_until=None,
                    )
                )
                if getattr(invalidated, "rowcount", 0) == 1:
                    session.commit()
                    raise InterviewPreparationConflictError(
                        "interview preparation source changed",
                        "interview_preparation_source_conflict",
                    )
                session.commit()
                return _result_for_existing(row)
            if jd_version_id is not None:
                current_version_id = session.scalar(
                    select(ApplicationJDVersion.id)
                    .where(ApplicationJDVersion.application_id == application_id)
                    .order_by(ApplicationJDVersion.version_number.desc())
                    .limit(1)
                )
                if current_version_id != jd_version_id:
                    invalidated = session.execute(
                        update(InterviewPreparationProposal)
                        .where(InterviewPreparationProposal.id == row.id)
                        .where(InterviewPreparationProposal.attempt_status == "generating")
                        .where(InterviewPreparationProposal.generation_revision == owner_revision)
                        .where(InterviewPreparationProposal.provider_call_token == owner_token)
                        .values(
                            attempt_status="invalidated",
                            invalidation_reason="source_conflict",
                            provider_call_token="",
                            provider_lease_until=None,
                        )
                    )
                    if getattr(invalidated, "rowcount", 0) == 1:
                        session.commit()
                        raise InterviewPreparationConflictError(
                            "interview preparation source changed",
                            "interview_preparation_source_conflict",
                        )
                    session.commit()
                    return _result_for_existing(row)
            current_fingerprint = sha256_text(canonical_json(current_snapshot))
            if current_fingerprint != source_fingerprint:
                invalidated = session.execute(
                    update(InterviewPreparationProposal)
                    .where(InterviewPreparationProposal.id == row.id)
                    .where(InterviewPreparationProposal.attempt_status == "generating")
                    .where(InterviewPreparationProposal.generation_revision == owner_revision)
                    .where(InterviewPreparationProposal.provider_call_token == owner_token)
                    .values(
                        attempt_status="invalidated",
                        invalidation_reason="source_conflict",
                        provider_call_token="",
                        provider_lease_until=None,
                    )
                )
                if getattr(invalidated, "rowcount", 0) == 1:
                    session.commit()
                    raise InterviewPreparationConflictError(
                        "interview preparation source changed",
                        "interview_preparation_source_conflict",
                    )
                session.commit()
                return _result_for_existing(row)
            if (
                row.attempt_status != "generating"
                or row.generation_revision != owner_revision
                or row.provider_call_token != owner_token
            ):
                session.commit()
                return _result_for_existing(row)
            proposal_json = canonical_json(proposal)
            row.proposal_json = proposal_json
            row.proposal_hash = sha256_text(proposal_json)
            row.proposal_status = (
                "safe_empty"
                if all(not proposal[field] for field in ("preparation_directions", "story_prompts", "review_points", "interviewer_questions", "items_to_clarify"))
                else "normal"
            )
            row.attempt_status = "ready"
            row.provider_call_token = ""
            row.provider_lease_until = None
            session.commit()
            _set_source_status(session, row)
            return InterviewPreparationGenerationResult(row, True, False, "ready")

    def _mark_provider_unknown(
        self,
        application_id: int,
        event_id: int,
        idempotency_key: str,
        revision: int,
        token: str,
    ) -> None:
        with self._session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            result = session.execute(
                update(InterviewPreparationProposal)
                .where(InterviewPreparationProposal.application_id == application_id)
                .where(InterviewPreparationProposal.application_event_id == event_id)
                .where(InterviewPreparationProposal.idempotency_key == idempotency_key)
                .where(InterviewPreparationProposal.attempt_status == "generating")
                .where(InterviewPreparationProposal.generation_revision == revision)
                .where(InterviewPreparationProposal.provider_call_token == token)
                .values(attempt_status="provider_unknown")
            )
            session.commit()
            if getattr(result, "rowcount", 0) != 1:
                return


def _build_snapshot(
    session: Session,
    *,
    application_id: int,
        event_id: int,
        resume_id: int,
        jd_text: str,
        knowledge_selections: list[dict[str, Any]],
        user_assertions: list[str],
    jd_version_id: int | None = None,
) -> dict[str, Any]:
    _require_visible_application(session, application_id)
    event = session.scalar(
        select(ApplicationEvent)
        .where(ApplicationEvent.id == event_id)
        .where(ApplicationEvent.application_id == application_id)
    )
    if event is None or event.event_type != "interview":
        raise InterviewPreparationValidationError(
            "interview event is invalid", "interview_preparation_event_invalid"
        )
    resume = session.get(Resume, resume_id)
    if resume is None or resume.deleted_at is not None:
        raise InterviewPreparationNotFound("interview_preparation_resume_not_found")
    if not isinstance(jd_text, str) or not jd_text.strip():
        raise InterviewPreparationValidationError(
            "JD is required", "interview_preparation_jd_required"
        )
    if len(jd_text.encode("utf-8")) > 60_000:
        raise InterviewPreparationValidationError(
            "JD is too large", "interview_preparation_input_too_large"
        )
    if not isinstance(knowledge_selections, list):
        raise InterviewPreparationValidationError(
            "Knowledge selections must be an array", "interview_preparation_invalid_request"
        )
    if not isinstance(user_assertions, list) or any(not isinstance(item, str) for item in user_assertions):
        raise InterviewPreparationValidationError(
            "user_assertions must be an array of strings", "interview_preparation_invalid_request"
        )
    normalized_assertions = [item for item in user_assertions if item.strip()]
    if len(normalized_assertions) > 10 or any(len(item) > 500 for item in normalized_assertions):
        raise InterviewPreparationValidationError(
            "user_assertions is too large", "interview_preparation_input_too_large"
        )
    try:
        content_json = parse_json_object("resume", resume.content_json)
    except Exception as exc:
        raise InterviewPreparationValidationError(
            "Resume content is invalid", "interview_preparation_resume_not_found"
        ) from exc
    if len(canonical_json(content_json).encode("utf-8")) > 200_000:
        raise InterviewPreparationValidationError(
            "Resume content is too large", "interview_preparation_input_too_large"
        )
    canonical_knowledge = _validate_knowledge_selections(session, knowledge_selections)
    jd_snapshot: dict[str, Any] = {"text": jd_text}
    if jd_version_id is not None:
        version = session.get(ApplicationJDVersion, jd_version_id)
        if version is None or version.application_id != application_id:
            raise InterviewPreparationConflictError(
                "interview preparation JD version is unavailable",
                "interview_preparation_source_conflict",
            )
        jd_snapshot.update(
            {
                "version_id": version.id,
                "version_number": version.version_number,
                "content_sha256": version.content_sha256,
            }
        )
    snapshot: dict[str, Any] = {
        "event": {
            "id": event.id,
            "application_id": event.application_id,
            "event_type": event.event_type,
            "subtype": event.subtype,
            "round": event.round,
            "scheduled_at": event.scheduled_at.isoformat() if event.scheduled_at else None,
            "duration_minutes": event.duration_minutes,
            "status": event.status,
        },
        "jd": jd_snapshot,
        "resume": {"id": resume.id, "content_json": content_json},
        "knowledge_evidence": canonical_knowledge,
        "user_assertions": normalized_assertions,
    }
    if jd_version_id is not None:
        snapshot["jd_version_id"] = jd_version_id
    return snapshot


def _validate_knowledge_selections(
    session: Session, selections: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if len(selections) > 5:
        raise InterviewPreparationValidationError(
            "Too many Knowledge Note selections", "interview_preparation_input_too_large"
        )
    result: list[dict[str, Any]] = []
    pending: list[tuple[int, str, KnowledgeEvidence]] = []
    seen: set[str] = set()
    total = 0
    total_bytes = 0
    for selection in selections:
        if set(selection) != {"note_version_id", "evidence_ids"}:
            raise InterviewPreparationValidationError(
                "Knowledge selection shape is invalid",
                "interview_preparation_knowledge_selection_invalid",
            )
        version_id = selection.get("note_version_id")
        evidence_ids = selection.get("evidence_ids")
        if not isinstance(version_id, int) or isinstance(version_id, bool) or not isinstance(evidence_ids, list):
            raise InterviewPreparationValidationError(
                "Knowledge selection shape is invalid",
                "interview_preparation_knowledge_selection_invalid",
            )
        version = session.scalar(
            select(KnowledgeNoteVersion)
            .join(KnowledgeNote, KnowledgeNote.id == KnowledgeNoteVersion.note_id)
            .join(KnowledgeSource, KnowledgeSource.id == KnowledgeNoteVersion.source_id)
            .where(KnowledgeNoteVersion.id == version_id)
            .where(KnowledgeNote.current_version_id == version_id)
            .where(KnowledgeNote.origin_kind == "confirmed_interview_capture")
            .where(KnowledgeNote.archived_at.is_(None))
            .where(KnowledgeSource.deleted_at.is_(None))
            .where(KnowledgeSource.archived_at.is_(None))
            .where(KnowledgeSource.lifecycle != "deleting")
        )
        if version is None:
            raise InterviewPreparationValidationError(
                "Knowledge Note Version is unavailable",
                "interview_preparation_knowledge_selection_invalid",
            )
        metadata = session.scalar(
            select(KnowledgeCapturedSourceMetadata).where(
                KnowledgeCapturedSourceMetadata.source_id == version.source_id
            )
        )
        if metadata is None:
            raise InterviewPreparationValidationError(
                "Knowledge source is not a confirmed interview capture",
                "interview_preparation_knowledge_selection_invalid",
            )
        origin_note = session.get(InterviewNote, metadata.origin_note_id)
        if origin_note is None or note_fingerprint(origin_note) != metadata.note_fingerprint:
            raise InterviewPreparationValidationError(
                "Knowledge source has changed",
                "interview_preparation_knowledge_selection_invalid",
            )
        if len(evidence_ids) > 20 or any(
            not isinstance(evidence_id, str) or not evidence_id or evidence_id in seen
            for evidence_id in evidence_ids
        ):
            raise InterviewPreparationValidationError(
                "Knowledge Evidence selection is invalid",
                "interview_preparation_knowledge_selection_invalid",
            )
        total += len(evidence_ids)
        if total > 20:
            raise InterviewPreparationValidationError(
                "Too many Knowledge Evidence selections",
                "interview_preparation_input_too_large",
            )
        evidence_rows = list(
            session.scalars(
                select(KnowledgeEvidence)
                .join(KnowledgeNoteEvidence, KnowledgeNoteEvidence.evidence_id == KnowledgeEvidence.id)
                .where(KnowledgeNoteEvidence.note_version_id == version_id)
                .where(KnowledgeEvidence.id.in_(evidence_ids))
            )
        )
        by_id = {row.id: row for row in evidence_rows}
        if len(by_id) != len(evidence_ids):
            raise InterviewPreparationValidationError(
                "Knowledge Evidence is not part of the selected version",
                "interview_preparation_knowledge_selection_invalid",
            )
        for evidence_id in evidence_ids:
            seen.add(evidence_id)
            row = by_id[evidence_id]
            total_bytes += len(row.canonical_excerpt.encode("utf-8"))
            if total_bytes > 64_000:
                raise InterviewPreparationValidationError(
                    "Knowledge Evidence is too large",
                    "interview_preparation_input_too_large",
                )
            pending.append((version_id, evidence_id, row))

    for version_id, evidence_id, row in sorted(pending, key=lambda item: (item[0], item[1])):
        provider_path = f"/knowledge_evidence/{len(result) + 1:03d}"
        result.append(
            {
                "id": row.id,
                "note_version_id": version_id,
                "path": f"/{row.id}",
                "provider_path": provider_path,
                "excerpt": row.canonical_excerpt,
                "content_hash": row.content_hash,
                "source_hash": session.scalar(
                    select(KnowledgeSource.source_hash).where(KnowledgeSource.id == row.source_id)
                ),
            }
        )
    return result


def _find_by_key(
    session: Session, application_id: int, event_id: int, idempotency_key: str
) -> InterviewPreparationProposal | None:
    return session.scalar(
        select(InterviewPreparationProposal)
        .where(InterviewPreparationProposal.application_id == application_id)
        .where(InterviewPreparationProposal.application_event_id == event_id)
        .where(InterviewPreparationProposal.idempotency_key == idempotency_key)
    )


def _require_visible_application(session: Session, application_id: int) -> Application:
    application = session.scalar(
        select(Application)
        .where(Application.id == application_id)
        .where(Application.deleted_at.is_(None))
    )
    if application is None:
        raise InterviewPreparationNotFound()
    return application


def _set_source_status(session: Session, row: InterviewPreparationProposal) -> None:
    states = {
        "event": "source_changed",
        "resume": "source_changed",
        "jd": "not_checked",
        "knowledge": "current",
    }
    try:
        snapshot = json.loads(row.input_snapshot_json)
        event_id = snapshot["event"]["id"]
        resume_id = snapshot["resume"]["id"]
        event = session.scalar(
            select(ApplicationEvent)
            .where(ApplicationEvent.id == event_id)
            .where(ApplicationEvent.application_id == row.application_id)
        )
        event_snapshot = snapshot["event"]
        if (
            event is not None
            and event.event_type == event_snapshot.get("event_type")
            and event.subtype == event_snapshot.get("subtype")
            and event.round == event_snapshot.get("round")
            and (event.scheduled_at.isoformat() if event.scheduled_at else None)
            == event_snapshot.get("scheduled_at")
            and event.duration_minutes == event_snapshot.get("duration_minutes")
            and event.status == event_snapshot.get("status")
        ):
            states["event"] = "current"
        resume = session.scalar(
            select(Resume)
            .where(Resume.id == resume_id)
            .where(Resume.deleted_at.is_(None))
        )
        if resume is not None:
            try:
                current_content = parse_json_object("resume", resume.content_json)
                if canonical_json(current_content) == canonical_json(snapshot["resume"]["content_json"]):
                    states["resume"] = "current"
            except Exception:
                pass
        recorded_jd_version_id = snapshot.get("jd_version_id")
        current_jd_version = session.scalar(
            select(ApplicationJDVersion)
            .where(ApplicationJDVersion.application_id == row.application_id)
            .order_by(ApplicationJDVersion.version_number.desc())
            .limit(1)
        )
        recorded_jd = snapshot.get("jd") if isinstance(snapshot.get("jd"), dict) else {}
        if type(recorded_jd_version_id) is int and recorded_jd_version_id > 0:
            states["jd"] = (
                "current"
                if current_jd_version is not None
                and current_jd_version.id == recorded_jd_version_id
                and current_jd_version.version_number == recorded_jd.get("version_number")
                and current_jd_version.content_sha256 == recorded_jd.get("content_sha256")
                else "source_changed"
            )
        elif current_jd_version is not None:
            states["jd"] = "source_changed"
        knowledge_changed = False
        for evidence in snapshot.get("knowledge_evidence", []):
            evidence_id = evidence.get("id") if isinstance(evidence, dict) else None
            version_id = evidence.get("note_version_id") if isinstance(evidence, dict) else None
            current = session.scalar(
                select(KnowledgeEvidence)
                .join(KnowledgeNoteEvidence, KnowledgeNoteEvidence.evidence_id == KnowledgeEvidence.id)
                .join(KnowledgeNoteVersion, KnowledgeNoteVersion.id == KnowledgeNoteEvidence.note_version_id)
                .join(KnowledgeNote, KnowledgeNote.id == KnowledgeNoteVersion.note_id)
                .where(KnowledgeEvidence.id == evidence_id)
                .where(KnowledgeNoteEvidence.note_version_id == version_id)
                .where(KnowledgeNote.current_version_id == version_id)
                .where(KnowledgeNote.origin_kind == "confirmed_interview_capture")
                .where(KnowledgeNote.archived_at.is_(None))
            )
            source = session.get(KnowledgeSource, current.source_id) if current is not None else None
            metadata = (
                session.scalar(
                    select(KnowledgeCapturedSourceMetadata).where(
                        KnowledgeCapturedSourceMetadata.source_id == current.source_id
                    )
                )
                if current is not None
                else None
            )
            origin_note = (
                session.get(InterviewNote, metadata.origin_note_id)
                if metadata is not None
                else None
            )
            if (
                current is None
                or source is None
                or metadata is None
                or origin_note is None
                or note_fingerprint(origin_note) != metadata.note_fingerprint
                or source.deleted_at is not None
                or source.archived_at is not None
                or source.lifecycle == "deleting"
                or current.canonical_excerpt != evidence.get("excerpt")
                or current.content_hash != evidence.get("content_hash")
                or source.source_hash != evidence.get("source_hash")
            ):
                knowledge_changed = True
        if knowledge_changed:
            states["knowledge"] = "source_changed"
        source_status = "source_changed" if "source_changed" in states.values() else "not_checked"
    except (TypeError, ValueError, KeyError):
        source_status = "source_changed"
    setattr(row, "source_status", source_status)
    setattr(row, "source_states", states)


def source_is_current_for_mock_interview(
    session: Session, row: InterviewPreparationProposal
) -> bool:
    """Return the same source decision used by the preparation history API."""
    _set_source_status(session, row)
    return getattr(row, "source_status", "source_changed") != "source_changed"


def _lease_until() -> datetime:
    return _db_now() + timedelta(seconds=LEASE_SECONDS)


def _db_now() -> datetime:
    """Return the naive UTC value SQLite returns for DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_db_naive(value: datetime) -> datetime:
    aware = _as_aware(value)
    if aware is None:
        raise TypeError("datetime value is required")
    return aware.replace(tzinfo=None)


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
