"""Canonical, Session-bound Review-to-Readiness read projection.

The functions in this module never start, commit, or roll back a transaction.  They
fail closed over persisted aggregate corruption and are the single source of the
fingerprints later consumed by Practice and Preparation.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, TypeAlias, cast
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from offerpilot.event_lifecycle import EventLifecycleV1, classify_event_lifecycle_v1
from offerpilot.models import (
    AdaptivePracticePlan,
    Application,
    ApplicationEvent,
    InterviewReadinessSignal,
    InterviewReadinessSignalEvidence,
    InterviewReadinessSignalVersion,
)
from offerpilot.product_actions.contracts import JSONValue
from offerpilot.review_readiness.candidates import resolve_readiness_candidates
from offerpilot.review_readiness.contracts import ReadinessCandidateV1, ReadinessEvidenceV1


ReadinessSourceStateV1: TypeAlias = Literal[
    "current",
    "changed",
    "missing",
    "unavailable",
    "retracted",
]
PracticeFocusStateV1: TypeAlias = Literal[
    "ready",
    "in_progress",
    "completed",
    "source_changed",
    "source_missing",
    "target_changed",
    "target_missing",
    "retracted",
    "not_eligible",
    "unavailable",
]
PracticeTargetStateV1: TypeAlias = Literal[
    "ready",
    "missing",
    "not_eligible",
    "unavailable",
]

_SHA256_PREFIX = "sha256:"
_ABSENT: dict[str, JSONValue] = {"state": "absent", "value": None}
_EVIDENCE_PATHS = frozenset(
    {"/questions", "/self_reflection", "/difficulty_points", "/mood"}
)


@dataclass(frozen=True, slots=True, repr=False)
class CanonicalReadinessSignalV1:
    signal_id: int
    application_id: int
    source_event_id: int | None
    source_note_id: int | None
    source_proposal_id: int | None
    focus_id: str
    signal_revision: int
    version_id: int
    version_number: int
    disposition: Literal["active", "retracted"]
    statement_text: str
    user_note: str
    source_note_revision: int
    source_note_fingerprint: str
    source_proposal_hash: str
    candidate_fingerprint: str
    evidence: tuple[ReadinessEvidenceV1, ...]
    practice_source_fingerprint: str


@dataclass(frozen=True, slots=True, repr=False)
class ReadinessSignalProjectionV1:
    state: ReadinessSourceStateV1
    aggregate: CanonicalReadinessSignalV1 | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class PracticeTargetV1:
    event_id: int
    application_id: int
    lifecycle: EventLifecycleV1
    practice_target_fingerprint: str


@dataclass(frozen=True, slots=True, repr=False)
class PracticeTargetProjectionV1:
    state: PracticeTargetStateV1
    target: PracticeTargetV1 | None = None


@dataclass(frozen=True, slots=True, repr=False)
class PracticeFocusProjectionV1:
    state: PracticeFocusStateV1
    source: CanonicalReadinessSignalV1 | None = None
    target: PracticeTargetV1 | None = None


class _ProjectionIntegrityError(RuntimeError):
    pass


def _exact_int(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value > 2**63 - 1:
        raise _ProjectionIntegrityError(f"{field}_invalid")
    return value


def _bounded_text(
    value: object,
    field: str,
    *,
    max_codepoints: int,
    max_bytes: int,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str or (not allow_empty and not value.strip()):
        raise _ProjectionIntegrityError(f"{field}_invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _ProjectionIntegrityError(f"{field}_invalid") from exc
    if len(value) > max_codepoints or len(encoded) > max_bytes:
        raise _ProjectionIntegrityError(f"{field}_too_large")
    return value


def _sha256_text(value: str) -> str:
    return _SHA256_PREFIX + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_sha256(value: object, field: str) -> str:
    if (
        type(value) is not str
        or len(value) != 71
        or not value.startswith(_SHA256_PREFIX)
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise _ProjectionIntegrityError(f"{field}_invalid")
    return value


def _canonical_uuid(value: object, field: str) -> str:
    if type(value) is not str:
        raise _ProjectionIntegrityError(f"{field}_invalid")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise _ProjectionIntegrityError(f"{field}_invalid") from exc
    if str(parsed) != value:
        raise _ProjectionIntegrityError(f"{field}_invalid")
    return value


def _bounded_identifier(value: object, field: str) -> str:
    text = _bounded_text(
        value,
        field,
        max_codepoints=128,
        max_bytes=128,
    )
    if any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
        for character in text
    ):
        raise _ProjectionIntegrityError(f"{field}_invalid")
    return text


def _require_fingerprint_json(value: object) -> None:
    """Enforce the stricter fingerprint JSON scalar contract (notably no bool)."""

    if value is None or type(value) in {int, str}:
        if type(value) is int and not -(2**63) <= value <= 2**63 - 1:
            raise _ProjectionIntegrityError("fingerprint_integer_invalid")
        if type(value) is str:
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise _ProjectionIntegrityError("fingerprint_unicode_invalid") from exc
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise _ProjectionIntegrityError("fingerprint_number_invalid")
        return
    if type(value) is list:
        for item in cast(list[object], value):
            _require_fingerprint_json(item)
        return
    if type(value) is dict:
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise _ProjectionIntegrityError("fingerprint_key_invalid")
            _require_fingerprint_json(item)
        return
    raise _ProjectionIntegrityError("fingerprint_value_invalid")


def _canonical_fingerprint(value: dict[str, JSONValue]) -> str:
    _require_fingerprint_json(value)
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _ProjectionIntegrityError("fingerprint_json_invalid") from exc
    return _SHA256_PREFIX + hashlib.sha256(raw).hexdigest()


def _evidence_rows(
    session: Session,
    version_id: int,
) -> tuple[ReadinessEvidenceV1, ...]:
    rows = list(
        session.scalars(
            select(InterviewReadinessSignalEvidence)
            .where(InterviewReadinessSignalEvidence.signal_version_id == version_id)
            .order_by(InterviewReadinessSignalEvidence.ordinal)
        )
    )
    # ORDER BY is authoritative; the defensive sort also protects callers backed by
    # adapters whose scalar materialization does not preserve the DB cursor order.
    rows.sort(key=lambda row: row.ordinal if type(row.ordinal) is int else -1)
    if not 1 <= len(rows) <= 5:
        raise _ProjectionIntegrityError("readiness_signal_evidence_count")
    if tuple(row.ordinal for row in rows) != tuple(range(len(rows))):
        raise _ProjectionIntegrityError("readiness_signal_evidence_prefix")
    evidence: list[ReadinessEvidenceV1] = []
    total_excerpt_bytes = 0
    for row in rows:
        _exact_int(row.ordinal, "evidence_ordinal")
        if row.source_path not in _EVIDENCE_PATHS:
            raise _ProjectionIntegrityError("evidence_source_path_invalid")
        excerpt = _bounded_text(
            row.excerpt,
            "evidence_excerpt",
            max_codepoints=2_000,
            max_bytes=8_192,
        )
        total_excerpt_bytes += len(excerpt.encode("utf-8"))
        if total_excerpt_bytes > 16_384:
            raise _ProjectionIntegrityError("evidence_excerpt_total_too_large")
        excerpt_sha256 = _require_sha256(row.excerpt_sha256, "evidence_excerpt_sha256")
        if excerpt_sha256 != _sha256_text(excerpt):
            raise _ProjectionIntegrityError("evidence_excerpt_hash_mismatch")
        evidence.append(
            ReadinessEvidenceV1(
                ordinal=row.ordinal,
                source_path=row.source_path,
                excerpt=excerpt,
                excerpt_sha256=excerpt_sha256,
                source_field_sha256=_require_sha256(
                    row.source_field_sha256,
                    "evidence_source_field_sha256",
                ),
            )
        )
    return tuple(evidence)


def compute_practice_source_fingerprint_v1(
    *,
    signal: InterviewReadinessSignal,
    version: InterviewReadinessSignalVersion,
    evidence: tuple[ReadinessEvidenceV1, ...],
) -> str:
    """Compute the pinned source fingerprint from a validated aggregate."""

    envelope: dict[str, JSONValue] = {
        "contract": "practice_source_fingerprint_v1",
        "signal_id": _exact_int(signal.id, "signal_id", minimum=1),
        "signal_revision": _exact_int(signal.revision, "signal_revision", minimum=1),
        "version_id": _exact_int(version.id, "version_id", minimum=1),
        "version_number": _exact_int(
            version.version_number,
            "version_number",
            minimum=1,
        ),
        "disposition": version.disposition,
        "statement_sha256": _sha256_text(version.statement_text),
        "user_note_sha256": _sha256_text(version.user_note),
        "source_note_revision": _exact_int(
            version.source_note_revision,
            "source_note_revision",
            minimum=1,
        ),
        "source_note_fingerprint": _require_sha256(
            version.source_note_fingerprint,
            "source_note_fingerprint",
        ),
        "source_proposal_hash": _require_sha256(
            version.source_proposal_hash,
            "source_proposal_hash",
        ),
        "candidate_fingerprint": _require_sha256(
            version.candidate_fingerprint,
            "candidate_fingerprint",
        ),
        "evidence": [
            {
                "ordinal": item.ordinal,
                "source_path": item.source_path,
                "excerpt_sha256": item.excerpt_sha256,
                "source_field_sha256": item.source_field_sha256,
            }
            for item in evidence
        ],
    }
    return _canonical_fingerprint(envelope)


def _validated_aggregate(
    session: Session,
    signal: InterviewReadinessSignal,
    version: InterviewReadinessSignalVersion,
) -> CanonicalReadinessSignalV1:
    if signal.current_version_id != version.id or version.signal_id != signal.id:
        raise _ProjectionIntegrityError("readiness_signal_current_pointer")
    if version.disposition not in {"active", "retracted"}:
        raise _ProjectionIntegrityError("readiness_signal_disposition")
    if version.schema_version != "readiness-signal-v1":
        raise _ProjectionIntegrityError("readiness_signal_schema")
    if signal.revision != version.version_number:
        raise _ProjectionIntegrityError("readiness_signal_revision_version")
    focus_id = _bounded_text(
        signal.focus_id,
        "focus_id",
        max_codepoints=128,
        max_bytes=128,
    )
    statement = _bounded_text(
        version.statement_text,
        "statement_text",
        max_codepoints=1_000,
        max_bytes=4_096,
    )
    user_note = _bounded_text(
        version.user_note,
        "user_note",
        max_codepoints=500,
        max_bytes=2_048,
        allow_empty=True,
    )
    evidence = _evidence_rows(session, version.id)
    if version.disposition == "active":
        if version.version_number != 1 or version.parent_version_id is not None:
            raise _ProjectionIntegrityError("readiness_signal_active_lineage")
    else:
        if type(version.parent_version_id) is not int:
            raise _ProjectionIntegrityError("readiness_signal_retraction_parent")
        parent = session.get(
            InterviewReadinessSignalVersion,
            version.parent_version_id,
        )
        if (
            parent is None
            or parent.signal_id != signal.id
            or parent.disposition != "active"
            or parent.version_number != 1
            or parent.parent_version_id is not None
            or version.version_number != 2
            or parent.schema_version != version.schema_version
            or parent.statement_text != version.statement_text
            or parent.user_note != version.user_note
            or parent.source_note_revision != version.source_note_revision
            or parent.source_note_fingerprint != version.source_note_fingerprint
            or parent.source_proposal_hash != version.source_proposal_hash
            or parent.candidate_fingerprint != version.candidate_fingerprint
            or _evidence_rows(session, parent.id) != evidence
        ):
            raise _ProjectionIntegrityError("readiness_signal_retraction_lineage")
    fingerprint = compute_practice_source_fingerprint_v1(
        signal=signal,
        version=version,
        evidence=evidence,
    )
    return CanonicalReadinessSignalV1(
        signal_id=_exact_int(signal.id, "signal_id", minimum=1),
        application_id=_exact_int(signal.application_id, "application_id", minimum=1),
        source_event_id=signal.source_event_id,
        source_note_id=signal.source_note_id,
        source_proposal_id=signal.source_proposal_id,
        focus_id=focus_id,
        signal_revision=_exact_int(signal.revision, "signal_revision", minimum=1),
        version_id=_exact_int(version.id, "version_id", minimum=1),
        version_number=_exact_int(version.version_number, "version_number", minimum=1),
        disposition=cast(Literal["active", "retracted"], version.disposition),
        statement_text=statement,
        user_note=user_note,
        source_note_revision=_exact_int(
            version.source_note_revision,
            "source_note_revision",
            minimum=1,
        ),
        source_note_fingerprint=_require_sha256(
            version.source_note_fingerprint,
            "source_note_fingerprint",
        ),
        source_proposal_hash=_require_sha256(
            version.source_proposal_hash,
            "source_proposal_hash",
        ),
        candidate_fingerprint=_require_sha256(
            version.candidate_fingerprint,
            "candidate_fingerprint",
        ),
        evidence=evidence,
        practice_source_fingerprint=fingerprint,
    )


def _candidate_identity_matches(
    aggregate: CanonicalReadinessSignalV1,
    candidate: ReadinessCandidateV1,
) -> bool:
    return (
        aggregate.application_id == candidate.application_id
        and aggregate.source_event_id == candidate.event_id
        and aggregate.source_note_id == candidate.note_id
        and aggregate.source_proposal_id == candidate.proposal_id
        and aggregate.focus_id == candidate.focus_id
        and aggregate.source_note_revision == candidate.source_note_revision
        and aggregate.source_note_fingerprint == candidate.source_note_fingerprint
        and aggregate.source_proposal_hash == candidate.source_proposal_hash
        and aggregate.candidate_fingerprint == candidate.candidate_fingerprint
    )


def _candidate_details_match(
    aggregate: CanonicalReadinessSignalV1,
    candidate: ReadinessCandidateV1,
) -> bool:
    return (
        aggregate.statement_text == candidate.statement_text
        and aggregate.evidence == candidate.evidence
    )


def load_canonical_readiness_signal(
    session: Session,
    *,
    signal_id: int | None = None,
    signal_version_id: int | None = None,
) -> ReadinessSignalProjectionV1:
    """Load and classify one current Signal aggregate in the caller's read UoW."""

    if (signal_id is None) == (signal_version_id is None):
        return ReadinessSignalProjectionV1("unavailable", error_code="locator_invalid")
    locator = signal_id if signal_id is not None else signal_version_id
    if type(locator) is not int or locator < 1:
        return ReadinessSignalProjectionV1("missing")
    try:
        if signal_id is not None:
            signal = session.get(InterviewReadinessSignal, signal_id)
            if signal is None:
                return ReadinessSignalProjectionV1("missing")
            if signal.current_version_id is None:
                return ReadinessSignalProjectionV1(
                    "unavailable",
                    error_code="current_version_missing",
                )
            version = session.get(
                InterviewReadinessSignalVersion,
                signal.current_version_id,
            )
        else:
            located_version = session.get(
                InterviewReadinessSignalVersion,
                signal_version_id,
            )
            if located_version is None:
                return ReadinessSignalProjectionV1("missing")
            signal = session.get(InterviewReadinessSignal, located_version.signal_id)
            if signal is None:
                return ReadinessSignalProjectionV1(
                    "unavailable",
                    error_code="locator_dangling",
                )
            if signal.current_version_id is None:
                return ReadinessSignalProjectionV1(
                    "unavailable",
                    error_code="current_version_missing",
                )
            version = session.get(
                InterviewReadinessSignalVersion,
                signal.current_version_id,
            )
            if version is not None and located_version.id != version.id:
                if not (
                    version.signal_id == signal.id
                    and version.schema_version == "readiness-signal-v1"
                    and version.version_number == 2
                    and version.disposition == "retracted"
                    and version.parent_version_id == located_version.id
                    and located_version.signal_id == signal.id
                    and located_version.schema_version == "readiness-signal-v1"
                    and located_version.version_number == 1
                    and located_version.disposition == "active"
                    and located_version.parent_version_id is None
                ):
                    return ReadinessSignalProjectionV1(
                        "unavailable",
                        error_code="locator_not_current_lineage",
                    )
        if signal is None or version is None:
            return ReadinessSignalProjectionV1(
                "unavailable",
                error_code="aggregate_partial",
            )
        aggregate = _validated_aggregate(session, signal, version)
        application = session.get(Application, aggregate.application_id)
        if application is None or application.deleted_at is not None:
            return ReadinessSignalProjectionV1(
                "unavailable",
                aggregate,
                "application_unavailable",
            )
        if aggregate.disposition == "retracted":
            return ReadinessSignalProjectionV1("retracted", aggregate)
        if (
            aggregate.source_event_id is None
            or aggregate.source_note_id is None
            or aggregate.source_proposal_id is None
        ):
            return ReadinessSignalProjectionV1("missing", aggregate)
        resolved = resolve_readiness_candidates(
            aggregate.source_note_id,
            aggregate.source_proposal_id,
            session,
        )
        if resolved.state == "unavailable":
            return ReadinessSignalProjectionV1(
                "unavailable",
                error_code="source_unavailable",
            )
        if resolved.state == "source_missing":
            return ReadinessSignalProjectionV1("missing", aggregate)
        if resolved.state != "ready":
            return ReadinessSignalProjectionV1("changed", aggregate)
        candidate = next(
            (item for item in resolved.candidates if item.focus_id == aggregate.focus_id),
            None,
        )
        if candidate is None:
            return ReadinessSignalProjectionV1("changed", aggregate)
        if not _candidate_identity_matches(aggregate, candidate):
            return ReadinessSignalProjectionV1("changed", aggregate)
        if not _candidate_details_match(aggregate, candidate):
            return ReadinessSignalProjectionV1(
                "unavailable",
                error_code="aggregate_source_mismatch",
            )
        return ReadinessSignalProjectionV1("current", aggregate)
    except SQLAlchemyError:
        return ReadinessSignalProjectionV1("unavailable", error_code="database_unavailable")
    except (_ProjectionIntegrityError, UnicodeEncodeError, ValueError, TypeError):
        return ReadinessSignalProjectionV1("unavailable", error_code="aggregate_unreadable")


def _rfc3339_or_absent(value: datetime | None) -> JSONValue:
    if value is None:
        return dict(_ABSENT)
    if not isinstance(value, datetime):
        raise _ProjectionIntegrityError("scheduled_at_invalid")
    normalized = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z")


def _ordered_tags(event: ApplicationEvent) -> list[JSONValue]:
    try:
        value = json.loads(event._tags)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
        raise _ProjectionIntegrityError("event_tags_invalid") from exc
    if type(value) is not list or any(type(item) is not str for item in value):
        raise _ProjectionIntegrityError("event_tags_invalid")
    for item in cast(list[str], value):
        try:
            item.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise _ProjectionIntegrityError("event_tags_invalid") from exc
    return cast(list[JSONValue], value)


def compute_practice_target_fingerprint_v1(event: ApplicationEvent) -> str:
    """Compute the target fingerprint from the exact authoritative Event row."""

    lifecycle = classify_event_lifecycle_v1(event.status)
    envelope: dict[str, JSONValue] = {
        "contract": "practice_target_fingerprint_v1",
        "event_id": _exact_int(event.id, "event_id", minimum=1),
        "application_id": _exact_int(event.application_id, "application_id", minimum=1),
        "event_type": _bounded_text(
            event.event_type,
            "event_type",
            max_codepoints=128,
            max_bytes=512,
        ),
        "subtype": _bounded_text(
            event.subtype,
            "event_subtype",
            max_codepoints=128,
            max_bytes=512,
            allow_empty=True,
        ),
        "tags": _ordered_tags(event),
        "round": _exact_int(event.round, "event_round"),
        "scheduled_at": _rfc3339_or_absent(event.scheduled_at),
        "duration_minutes": _exact_int(event.duration_minutes, "event_duration_minutes"),
        "lifecycle_status": lifecycle,
    }
    return _canonical_fingerprint(envelope)


def project_practice_target(
    session: Session,
    *,
    application_id: int,
    target_event_id: int,
    source_event_id: int | None = None,
) -> PracticeTargetProjectionV1:
    """Resolve one target with owner scope decided before target content decode."""

    if (
        type(application_id) is not int
        or application_id < 1
        or type(target_event_id) is not int
        or target_event_id < 1
    ):
        return PracticeTargetProjectionV1("missing")
    if source_event_id is not None and target_event_id == source_event_id:
        return PracticeTargetProjectionV1("not_eligible")
    try:
        owner_id = session.scalar(
            select(ApplicationEvent.application_id).where(
                ApplicationEvent.id == target_event_id
            )
        )
        if owner_id is None:
            return PracticeTargetProjectionV1("missing")
        if type(owner_id) is not int or owner_id != application_id:
            return PracticeTargetProjectionV1("not_eligible")
        event = session.get(ApplicationEvent, target_event_id)
        if event is None:
            return PracticeTargetProjectionV1("unavailable")
        _bounded_text(
            event.status,
            "event_status",
            max_codepoints=128,
            max_bytes=512,
        )
        fingerprint = compute_practice_target_fingerprint_v1(event)
        lifecycle = classify_event_lifecycle_v1(event.status)
        target = PracticeTargetV1(
            _exact_int(event.id, "event_id", minimum=1),
            _exact_int(event.application_id, "application_id", minimum=1),
            lifecycle,
            fingerprint,
        )
        if event.event_type != "interview" or lifecycle not in {
            "scheduled",
            "in_progress",
        }:
            return PracticeTargetProjectionV1("not_eligible", target)
        return PracticeTargetProjectionV1("ready", target)
    except SQLAlchemyError:
        return PracticeTargetProjectionV1("unavailable")
    except (_ProjectionIntegrityError, UnicodeEncodeError, ValueError, TypeError):
        return PracticeTargetProjectionV1("unavailable")


def _plan_source_identity_matches(
    plan: AdaptivePracticePlan,
    aggregate: CanonicalReadinessSignalV1,
) -> bool:
    return (
        type(plan.application_id) is int
        and plan.application_id == aggregate.application_id
        and type(plan.application_event_id) is int
        and plan.application_event_id == aggregate.source_event_id
        and type(plan.interview_note_id) is int
        and plan.interview_note_id == aggregate.source_note_id
        and type(plan.interview_review_proposal_id) is int
        and plan.interview_review_proposal_id == aggregate.source_proposal_id
        and type(plan.focus_id) is str
        and plan.focus_id == aggregate.focus_id
    )


def _exact_v2_plan_for_pair(
    session: Session,
    *,
    aggregate: CanonicalReadinessSignalV1,
    target: PracticeTargetV1,
) -> AdaptivePracticePlan | None:
    """Find the exact pair while detecting damaged locators tied to its owner."""

    owner_match = (
        (AdaptivePracticePlan.application_id == aggregate.application_id)
        & (AdaptivePracticePlan.application_event_id == aggregate.source_event_id)
        & (AdaptivePracticePlan.interview_note_id == aggregate.source_note_id)
        & (
            AdaptivePracticePlan.interview_review_proposal_id
            == aggregate.source_proposal_id
        )
        & (AdaptivePracticePlan.focus_id == aggregate.focus_id)
    )
    plans = list(
        session.scalars(
            select(AdaptivePracticePlan)
            .where(
                AdaptivePracticePlan.origin_contract
                == "confirmed_readiness_signal_v1",
                or_(
                    owner_match,
                    AdaptivePracticePlan.readiness_signal_version_id
                    == aggregate.version_id,
                    AdaptivePracticePlan.target_application_event_id
                    == target.event_id,
                ),
            )
            .order_by(AdaptivePracticePlan.id)
        )
    )
    exact: list[AdaptivePracticePlan] = []
    for plan in plans:
        belongs_to_source = _plan_source_identity_matches(plan, aggregate)
        if belongs_to_source:
            if plan.readiness_signal_version_id != aggregate.version_id:
                raise _ProjectionIntegrityError("practice_plan_source_locator_invalid")
            if plan.target_application_event_id is None:
                # ON DELETE SET NULL preserves an unrelated historical pair.
                continue
            if type(plan.target_application_event_id) is not int:
                raise _ProjectionIntegrityError("practice_plan_target_locator_invalid")
            if plan.target_application_event_id == aggregate.source_event_id:
                raise _ProjectionIntegrityError("practice_plan_target_locator_invalid")
            if plan.target_application_event_id == target.event_id:
                exact.append(plan)
                continue
            other_target_owner = session.scalar(
                select(ApplicationEvent.application_id).where(
                    ApplicationEvent.id == plan.target_application_event_id
                )
            )
            if other_target_owner != aggregate.application_id:
                raise _ProjectionIntegrityError("practice_plan_target_locator_invalid")
        elif plan.readiness_signal_version_id == aggregate.version_id:
            if not belongs_to_source:
                raise _ProjectionIntegrityError("practice_plan_source_owner_invalid")
    if len(exact) > 1:
        raise _ProjectionIntegrityError("practice_plan_pair_ambiguous")
    return exact[0] if exact else None


def _validate_v2_plan(
    plan: AdaptivePracticePlan,
    *,
    aggregate: CanonicalReadinessSignalV1,
    target: PracticeTargetV1,
) -> None:
    """Validate the complete non-private frozen V2 plan envelope."""

    _exact_int(plan.id, "practice_plan_id", minimum=1)
    if not _plan_source_identity_matches(plan, aggregate):
        raise _ProjectionIntegrityError("practice_plan_source_owner_invalid")
    if (
        plan.origin_contract != "confirmed_readiness_signal_v1"
        or type(plan.readiness_signal_version_id) is not int
        or plan.readiness_signal_version_id != aggregate.version_id
        or type(plan.target_application_event_id) is not int
        or plan.target_application_event_id != target.event_id
        or plan.application_event_id == plan.target_application_event_id
    ):
        raise _ProjectionIntegrityError("practice_plan_locator_invalid")

    source_fingerprint = _require_sha256(
        plan.source_fingerprint,
        "practice_plan_source_fingerprint",
    )
    target_fingerprint = _require_sha256(
        plan.target_fingerprint,
        "practice_plan_target_fingerprint",
    )
    primary = aggregate.evidence[0]
    if (
        plan.source_path != primary.source_path
        or plan.source_excerpt != primary.excerpt
        or plan.source_hash != primary.source_field_sha256
    ):
        raise _ProjectionIntegrityError("practice_plan_source_snapshot_invalid")
    _require_sha256(plan.source_hash, "practice_plan_source_hash")

    _bounded_identifier(plan.drill_kind, "practice_plan_drill_kind")
    _bounded_text(
        plan.title,
        "practice_plan_title",
        max_codepoints=200,
        max_bytes=800,
    )
    _bounded_text(
        plan.observation,
        "practice_plan_observation",
        max_codepoints=1_000,
        max_bytes=4_096,
    )
    _bounded_text(
        plan.reason,
        "practice_plan_reason",
        max_codepoints=1_000,
        max_bytes=4_096,
    )
    _bounded_text(
        plan.prompt,
        "practice_plan_prompt",
        max_codepoints=2_000,
        max_bytes=8_192,
    )

    start_key = _canonical_uuid(
        plan.start_idempotency_key,
        "practice_plan_start_key",
    )
    start_fingerprint = _require_sha256(
        plan.start_input_fingerprint,
        "practice_plan_start_input_fingerprint",
    )
    expected_start_fingerprint = _canonical_fingerprint(
        {
            "idempotency_key": start_key,
            "readiness_signal_version_id": aggregate.version_id,
            "expected_source_fingerprint": source_fingerprint,
            "target_application_event_id": target.event_id,
            "expected_target_fingerprint": target_fingerprint,
        }
    )
    if start_fingerprint != expected_start_fingerprint:
        raise _ProjectionIntegrityError("practice_plan_start_input_mismatch")

    if plan.status == "in_progress":
        if (
            type(plan.revision) is not int
            or plan.revision != 1
            or plan.response_text != ""
            or plan.reflection_text != ""
            or plan.completion_idempotency_key is not None
            or plan.completion_fingerprint != ""
            or plan.completed_at is not None
        ):
            raise _ProjectionIntegrityError("practice_plan_in_progress_shape_invalid")
        return
    if plan.status != "completed" or type(plan.revision) is not int or plan.revision != 2:
        raise _ProjectionIntegrityError("practice_plan_status_invalid")
    _bounded_text(
        plan.response_text,
        "practice_plan_response",
        max_codepoints=8_000,
        max_bytes=32_768,
    )
    _bounded_text(
        plan.reflection_text,
        "practice_plan_reflection",
        max_codepoints=4_000,
        max_bytes=16_384,
        allow_empty=True,
    )
    _canonical_uuid(
        plan.completion_idempotency_key,
        "practice_plan_completion_key",
    )
    _require_sha256(
        plan.completion_fingerprint,
        "practice_plan_completion_fingerprint",
    )
    if not isinstance(plan.completed_at, datetime):
        raise _ProjectionIntegrityError("practice_plan_completed_at_invalid")


def project_practice_focus(
    session: Session,
    *,
    signal_version_id: int,
    target_event_id: int,
) -> PracticeFocusProjectionV1:
    """Project one exact Signal Version / target Event pair without date inference."""

    source = load_canonical_readiness_signal(
        session,
        signal_version_id=signal_version_id,
    )
    source_state: dict[ReadinessSourceStateV1, PracticeFocusStateV1] = {
        "current": "ready",
        "changed": "source_changed",
        "missing": "source_missing",
        "unavailable": "unavailable",
        "retracted": "retracted",
    }
    if source.state == "unavailable" or source.aggregate is None:
        return PracticeFocusProjectionV1(
            source_state[source.state],
            None,
        )
    aggregate = source.aggregate
    if type(target_event_id) is not int or target_event_id < 1:
        return PracticeFocusProjectionV1("target_missing", aggregate)
    try:
        target_projection = project_practice_target(
            session,
            application_id=aggregate.application_id,
            target_event_id=target_event_id,
            source_event_id=aggregate.source_event_id,
        )
        if target_projection.state == "missing":
            return PracticeFocusProjectionV1("target_missing", aggregate)
        if target_projection.state == "unavailable":
            return PracticeFocusProjectionV1("unavailable", aggregate)
        if target_projection.target is None:
            return PracticeFocusProjectionV1("not_eligible", aggregate)
        target = target_projection.target
        if target_projection.state == "not_eligible":
            return PracticeFocusProjectionV1("not_eligible", aggregate, target)
        if source.state != "current":
            return PracticeFocusProjectionV1(
                source_state[source.state],
                aggregate,
                target,
            )
        plan = _exact_v2_plan_for_pair(
            session,
            aggregate=aggregate,
            target=target,
        )
        if plan is not None:
            _validate_v2_plan(plan, aggregate=aggregate, target=target)
            if plan.source_fingerprint != aggregate.practice_source_fingerprint:
                return PracticeFocusProjectionV1("source_changed", aggregate, target)
            if plan.target_fingerprint != target.practice_target_fingerprint:
                return PracticeFocusProjectionV1("target_changed", aggregate, target)
        if plan is None:
            state: PracticeFocusStateV1 = "ready"
        elif plan.status == "completed":
            state = "completed"
        elif plan.status == "in_progress":
            state = "in_progress"
        else:
            return PracticeFocusProjectionV1("unavailable", aggregate, target)
        return PracticeFocusProjectionV1(state, aggregate, target)
    except SQLAlchemyError:
        return PracticeFocusProjectionV1("unavailable", aggregate)
    except (_ProjectionIntegrityError, UnicodeEncodeError, ValueError, TypeError):
        return PracticeFocusProjectionV1("unavailable", aggregate)


__all__ = [
    "CanonicalReadinessSignalV1",
    "PracticeFocusProjectionV1",
    "PracticeFocusStateV1",
    "PracticeTargetProjectionV1",
    "PracticeTargetStateV1",
    "PracticeTargetV1",
    "ReadinessSignalProjectionV1",
    "ReadinessSourceStateV1",
    "compute_practice_source_fingerprint_v1",
    "compute_practice_target_fingerprint_v1",
    "load_canonical_readiness_signal",
    "project_practice_focus",
    "project_practice_target",
]
