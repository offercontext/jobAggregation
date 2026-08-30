"""Session-bound selection of confirmed readiness feedback for Preparation V2.

This module owns no transaction boundary.  The Interview Preparation repository
constructs the loader inside the same read unit-of-work that freezes Event, JD,
Resume, Knowledge, and readiness feedback.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy.orm import Session

from offerpilot.event_lifecycle import classify_event_lifecycle_v1
from offerpilot.models import Application, ApplicationEvent, Resume
from offerpilot.review_readiness.projection import (
    load_canonical_readiness_signal,
    project_practice_target,
)


MAX_SELECTED_READINESS_VERSIONS = 8
MAX_READINESS_EVIDENCE_PER_SIGNAL = 5
MAX_READINESS_STATEMENT_BYTES = 16 * 1024
MAX_READINESS_USER_NOTE_BYTES = 8 * 1024
MAX_READINESS_EXCERPT_BYTES = 32 * 1024
MAX_READINESS_FEEDBACK_ENVELOPE_BYTES = 64 * 1024


class PreparationReadinessSelectionError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True, repr=False)
class PreparationReadinessEvidenceV2:
    path: str
    excerpt: str
    excerpt_sha256: str

    def to_json(self) -> dict[str, str]:
        return {
            "path": self.path,
            "excerpt": self.excerpt,
            "excerpt_sha256": self.excerpt_sha256,
        }


@dataclass(frozen=True, slots=True, repr=False)
class PreparationReadinessSourceEventV2:
    round: int
    subtype: str

    def to_json(self) -> dict[str, Any]:
        return {"round": self.round, "subtype": self.subtype}


@dataclass(frozen=True, slots=True, repr=False)
class PreparationReadinessFeedbackV2:
    statement: str
    user_note: str
    source_event: PreparationReadinessSourceEventV2
    practice_state: str
    evidence: tuple[PreparationReadinessEvidenceV2, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "statement": self.statement,
            "user_note": self.user_note,
            "source_event": self.source_event.to_json(),
            "practice_state": self.practice_state,
            "evidence": [item.to_json() for item in self.evidence],
        }


@dataclass(frozen=True, slots=True, repr=False)
class PreparationReadinessSelectionV2:
    application_id: int
    target_event_id: int
    resume_id: int
    ordered_version_ids: tuple[int, ...]
    selection_fingerprint: str
    readiness_feedback: tuple[PreparationReadinessFeedbackV2, ...]
    canonical_readiness_feedback_json: str


def _exact_positive_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1 or value > 2**63 - 1:
        raise PreparationReadinessSelectionError(f"preparation_readiness_{field}_invalid")
    return value


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise PreparationReadinessSelectionError(
            "preparation_readiness_input_invalid"
        ) from exc


def canonical_readiness_feedback_bytes(
    readiness_feedback: Sequence[dict[str, Any]],
) -> bytes:
    """Canonicalize the exact final Provider feedback wrapper and enforce 64 KiB."""

    try:
        encoded = _canonical_json(
            {"readiness_feedback": list(readiness_feedback)}
        ).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PreparationReadinessSelectionError(
            "preparation_readiness_input_invalid"
        ) from exc
    if len(encoded) > MAX_READINESS_FEEDBACK_ENVELOPE_BYTES:
        raise PreparationReadinessSelectionError(
            "preparation_readiness_feedback_too_large"
        )
    return encoded


def _sha256_canonical(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_ordered_version_ids(value: object) -> tuple[int, ...]:
    if type(value) is not tuple or len(value) > MAX_SELECTED_READINESS_VERSIONS:
        raise PreparationReadinessSelectionError(
            "preparation_readiness_selection_invalid"
        )
    ordered = tuple(
        _exact_positive_int(item, "signal_version_id") for item in value
    )
    if len(set(ordered)) != len(ordered):
        raise PreparationReadinessSelectionError(
            "preparation_readiness_selection_invalid"
        )
    return ordered


class PreparationReadinessSelectionLoader:
    """Load one explicit ordered selection without starting a transaction."""

    def __init__(self, session: Session) -> None:
        if not isinstance(session, Session):
            raise TypeError("session must be a SQLAlchemy Session")
        self._session = session

    def load(
        self,
        *,
        application_id: int,
        target_event_id: int,
        resume_id: int,
        ordered_version_ids: tuple[int, ...],
    ) -> PreparationReadinessSelectionV2:
        application_id = _exact_positive_int(application_id, "application_id")
        target_event_id = _exact_positive_int(target_event_id, "target_event_id")
        resume_id = _exact_positive_int(resume_id, "resume_id")
        ordered_version_ids = _require_ordered_version_ids(ordered_version_ids)

        application = self._session.get(Application, application_id)
        if application is None or application.deleted_at is not None:
            raise PreparationReadinessSelectionError(
                "preparation_readiness_application_not_found"
            )
        resume = self._session.get(Resume, resume_id)
        if resume is None or resume.deleted_at is not None:
            raise PreparationReadinessSelectionError(
                "preparation_readiness_resume_not_found"
            )
        target_projection = project_practice_target(
            self._session,
            application_id=application_id,
            target_event_id=target_event_id,
        )
        if target_projection.state != "ready" or target_projection.target is None:
            raise PreparationReadinessSelectionError(
                "preparation_readiness_target_not_eligible"
            )
        target = target_projection.target

        # Explicit empty means exactly empty.  Do not scan or aggregate any Signal.
        if not ordered_version_ids:
            readiness_feedback: tuple[PreparationReadinessFeedbackV2, ...] = ()
            canonical = canonical_readiness_feedback_bytes(()).decode(
                "utf-8"
            )
            fingerprint = _sha256_canonical(
                {
                    "contract": "preparation_readiness_selection_v1",
                    "application_id": application_id,
                    "target_application_event_id": target_event_id,
                    "resume_id": resume_id,
                    "ordered_version_ids": [],
                    "target_fingerprint": target.practice_target_fingerprint,
                    "sources": [],
                }
            )
            return PreparationReadinessSelectionV2(
                application_id,
                target_event_id,
                resume_id,
                ordered_version_ids,
                fingerprint,
                readiness_feedback,
                canonical,
            )

        feedback_items: list[PreparationReadinessFeedbackV2] = []
        fingerprint_sources: list[dict[str, Any]] = []
        canonical_feedback_bytes: bytes | None = None
        statement_bytes = 0
        user_note_bytes = 0
        excerpt_bytes = 0
        for version_id in ordered_version_ids:
            projection = load_canonical_readiness_signal(
                self._session,
                signal_version_id=version_id,
            )
            aggregate = projection.aggregate
            if projection.state != "current" or aggregate is None:
                raise PreparationReadinessSelectionError(
                    "preparation_readiness_signal_not_current"
                )
            if aggregate.application_id != application_id:
                raise PreparationReadinessSelectionError(
                    "preparation_readiness_signal_not_current"
                )
            if aggregate.source_event_id == target_event_id:
                raise PreparationReadinessSelectionError(
                    "preparation_readiness_source_target_same"
                )
            if type(aggregate.source_event_id) is not int:
                raise PreparationReadinessSelectionError(
                    "preparation_readiness_signal_not_current"
                )
            source_event = self._session.get(
                ApplicationEvent,
                aggregate.source_event_id,
            )
            if (
                source_event is None
                or source_event.application_id != application_id
                or source_event.event_type != "interview"
                or classify_event_lifecycle_v1(source_event.status) != "completed"
            ):
                raise PreparationReadinessSelectionError(
                    "preparation_readiness_source_not_eligible"
                )
            if type(source_event.round) is not int or source_event.round < 0:
                raise PreparationReadinessSelectionError(
                    "preparation_readiness_source_not_eligible"
                )
            if type(source_event.subtype) is not str:
                raise PreparationReadinessSelectionError(
                    "preparation_readiness_source_not_eligible"
                )
            evidence = tuple(
                PreparationReadinessEvidenceV2(
                    item.source_path,
                    item.excerpt,
                    item.excerpt_sha256,
                )
                for item in aggregate.evidence
            )
            if not 1 <= len(evidence) <= MAX_READINESS_EVIDENCE_PER_SIGNAL:
                raise PreparationReadinessSelectionError(
                    "preparation_readiness_signal_not_current"
                )
            statement_bytes += len(aggregate.statement_text.encode("utf-8"))
            user_note_bytes += len(aggregate.user_note.encode("utf-8"))
            excerpt_bytes += sum(
                len(item.excerpt.encode("utf-8")) for item in evidence
            )
            if statement_bytes > MAX_READINESS_STATEMENT_BYTES:
                raise PreparationReadinessSelectionError(
                    "preparation_readiness_statement_too_large"
                )
            if user_note_bytes > MAX_READINESS_USER_NOTE_BYTES:
                raise PreparationReadinessSelectionError(
                    "preparation_readiness_user_note_too_large"
                )
            if excerpt_bytes > MAX_READINESS_EXCERPT_BYTES:
                raise PreparationReadinessSelectionError(
                    "preparation_readiness_excerpt_too_large"
                )
            feedback_items.append(
                PreparationReadinessFeedbackV2(
                    statement=aggregate.statement_text,
                    user_note=aggregate.user_note,
                    source_event=PreparationReadinessSourceEventV2(
                        source_event.round,
                        source_event.subtype,
                    ),
                    practice_state="completed",
                    evidence=evidence,
                )
            )
            # Enforce the exact final Provider wrapper at every complete Signal
            # boundary.  This keeps the loader fail-closed without accumulating an
            # over-budget prefix through the remaining selected aggregates.
            canonical_feedback_bytes = canonical_readiness_feedback_bytes(
                [item.to_json() for item in feedback_items]
            )
            fingerprint_sources.append(
                {
                    "version_id": aggregate.version_id,
                    "practice_source_fingerprint": aggregate.practice_source_fingerprint,
                    "source_event_id": aggregate.source_event_id,
                    "evidence_sha256": [item.excerpt_sha256 for item in evidence],
                }
            )
        readiness_feedback = tuple(feedback_items)
        if canonical_feedback_bytes is None:
            raise PreparationReadinessSelectionError(
                "preparation_readiness_selection_invalid"
            )
        canonical = canonical_feedback_bytes.decode("utf-8")
        fingerprint = _sha256_canonical(
            {
                "contract": "preparation_readiness_selection_v1",
                "application_id": application_id,
                "target_application_event_id": target_event_id,
                "resume_id": resume_id,
                "ordered_version_ids": list(ordered_version_ids),
                "target_fingerprint": target.practice_target_fingerprint,
                "sources": fingerprint_sources,
            }
        )
        return PreparationReadinessSelectionV2(
            application_id,
            target_event_id,
            resume_id,
            ordered_version_ids,
            fingerprint,
            readiness_feedback,
            canonical,
        )


__all__ = [
    "MAX_READINESS_FEEDBACK_ENVELOPE_BYTES",
    "PreparationReadinessSelectionError",
    "PreparationReadinessEvidenceV2",
    "PreparationReadinessFeedbackV2",
    "PreparationReadinessSelectionLoader",
    "PreparationReadinessSelectionV2",
    "PreparationReadinessSourceEventV2",
    "canonical_readiness_feedback_bytes",
]
