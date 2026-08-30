"""Read-only exact candidate projection for confirmed readiness Signals."""

from __future__ import annotations

import hashlib
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from offerpilot.ai.interview_review_proposals import (
    InterviewReviewModelError,
    build_interview_review_snapshot,
    validate_interview_review,
)
from offerpilot.event_lifecycle import classify_event_lifecycle_v1
from offerpilot.models import (
    Application,
    ApplicationEvent,
    InterviewNote,
    InterviewReadinessSignal,
    InterviewReviewProposal,
)
from offerpilot.product_actions.contracts import (
    JSONValue,
    canonical_product_action_json,
    decode_product_action_request_v1,
)
from offerpilot.repositories.json_contract import canonical_json, sha256_text
from offerpilot.review_readiness.contracts import (
    CandidateProjectionV1,
    ReadinessCandidateV1,
    ReadinessEvidenceV1,
    ReviewReadinessContractError,
)


_MAX_PERSISTED_JSON_BYTES = 256 * 1024
_EVIDENCE_PATHS = frozenset(
    {"/questions", "/self_reflection", "/difficulty_points", "/mood"}
)


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _prefixed_stored_hash(value: object, field: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ReviewReadinessContractError(f"{field}_invalid")
    return "sha256:" + value


def _valid_focus_id(value: object) -> str:
    if type(value) is not str:
        raise ReviewReadinessContractError("focus_id_invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ReviewReadinessContractError("focus_id_invalid") from exc
    if (
        not 1 <= len(encoded) <= 128
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ReviewReadinessContractError("focus_id_invalid")
    return value


def _bounded_text(
    value: object,
    *,
    field: str,
    max_codepoints: int,
    max_bytes: int,
) -> str:
    if type(value) is not str or not value.strip():
        raise ReviewReadinessContractError(f"{field}_invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ReviewReadinessContractError(f"{field}_invalid") from exc
    if len(value) > max_codepoints or len(encoded) > max_bytes:
        raise ReviewReadinessContractError(f"{field}_too_large")
    return value


def _candidate_from_focus(
    *,
    application_id: int,
    event_id: int,
    note: InterviewNote,
    proposal: InterviewReviewProposal,
    source_fingerprint: str,
    proposal_hash: str,
    focus: dict[str, Any],
) -> ReadinessCandidateV1:
    focus_id = _valid_focus_id(focus.get("id"))
    statement = _bounded_text(
        focus.get("text"),
        field="focus_text",
        max_codepoints=1_000,
        max_bytes=4_096,
    )
    raw_evidence = focus.get("evidence_refs")
    if type(raw_evidence) is not list or not 1 <= len(raw_evidence) <= 5:
        raise ReviewReadinessContractError("evidence_count_invalid")
    evidence: list[ReadinessEvidenceV1] = []
    excerpt_bytes = 0
    envelope_evidence: list[JSONValue] = []
    for ordinal, raw in enumerate(raw_evidence):
        if type(raw) is not dict or set(raw) != {"source", "path", "excerpt"}:
            raise ReviewReadinessContractError("evidence_shape_invalid")
        if raw.get("source") != "interview_note" or raw.get("path") not in _EVIDENCE_PATHS:
            raise ReviewReadinessContractError("evidence_source_invalid")
        path = cast(str, raw["path"])
        excerpt = _bounded_text(
            raw.get("excerpt"),
            field="evidence_excerpt",
            max_codepoints=2_000,
            max_bytes=8_192,
        )
        source_field = getattr(note, path[1:], None)
        if type(source_field) is not str or excerpt not in source_field:
            raise ReviewReadinessContractError("evidence_not_verbatim")
        encoded = excerpt.encode("utf-8")
        excerpt_bytes += len(encoded)
        if excerpt_bytes > 16_384:
            raise ReviewReadinessContractError("evidence_total_too_large")
        excerpt_hash = _sha256_bytes(encoded)
        evidence.append(
            ReadinessEvidenceV1(
                ordinal=ordinal,
                source_path=path,
                excerpt=excerpt,
                excerpt_sha256=excerpt_hash,
                source_field_sha256=_sha256_bytes(source_field.encode("utf-8")),
            )
        )
        envelope_evidence.append(
            {"path": path, "excerpt_sha256": excerpt_hash}
        )
    envelope: dict[str, JSONValue] = {
        "proposal_schema_version": 2,
        "source_note_revision": note.content_revision,
        "source_fingerprint": source_fingerprint,
        "proposal_hash": proposal_hash,
        "focus_id": focus_id,
        "focus_text": statement,
        "evidence": envelope_evidence,
    }
    canonical = canonical_product_action_json(envelope).encode("utf-8")
    if len(canonical) > 32_768:
        raise ReviewReadinessContractError("candidate_envelope_too_large")
    return ReadinessCandidateV1(
        application_id=application_id,
        event_id=event_id,
        note_id=note.id,
        proposal_id=proposal.id,
        proposal_schema_version=2,
        focus_id=focus_id,
        statement_text=statement,
        source_note_revision=note.content_revision,
        source_note_fingerprint=source_fingerprint,
        source_proposal_hash=proposal_hash,
        candidate_fingerprint=_sha256_bytes(canonical),
        evidence=tuple(evidence),
    )


def project_readiness_candidates(
    note_id: int,
    proposal_id: int,
    session: Session,
) -> CandidateProjectionV1:
    """Project at most eight safe candidates without writing or calling a Provider."""

    if type(note_id) is not int or note_id < 1 or type(proposal_id) is not int or proposal_id < 1:
        return CandidateProjectionV1("unavailable", note_id, proposal_id)
    note = session.get(InterviewNote, note_id)
    proposal = session.get(InterviewReviewProposal, proposal_id)
    if note is None or proposal is None:
        return CandidateProjectionV1("source_missing", note_id, proposal_id)
    if note.application_id is None or note.application_event_id is None:
        return CandidateProjectionV1("source_missing", note_id, proposal_id)
    application = session.get(Application, note.application_id)
    if application is None or application.deleted_at is not None:
        return CandidateProjectionV1("unavailable", note_id, proposal_id)
    event = session.get(ApplicationEvent, note.application_event_id)
    if event is None:
        return CandidateProjectionV1("source_missing", note_id, proposal_id)
    if (
        event.application_id != application.id
        or event.event_type != "interview"
        or classify_event_lifecycle_v1(event.status) != "completed"
    ):
        return CandidateProjectionV1("not_eligible", note_id, proposal_id)
    if proposal.proposal_schema_version != 2 or proposal.source_note_revision is None:
        return CandidateProjectionV1(
            "legacy_requires_regeneration",
            note_id,
            proposal_id,
        )
    if (
        proposal.note_id != note.id
        or proposal.application_event_id != event.id
        or type(proposal.source_note_revision) is not int
        or proposal.source_note_revision != note.content_revision
    ):
        return CandidateProjectionV1("source_changed", note_id, proposal_id)
    try:
        snapshot = build_interview_review_snapshot(note, event)
        snapshot_json = canonical_json(snapshot)
        if (
            proposal.input_snapshot_json != snapshot_json
            or proposal.source_fingerprint != sha256_text(snapshot_json)
        ):
            return CandidateProjectionV1("source_changed", note_id, proposal_id)
        parsed = decode_product_action_request_v1(
            proposal.proposal_json.encode("utf-8"),
            max_bytes=_MAX_PERSISTED_JSON_BYTES,
        )
        normalized = validate_interview_review(cast(dict[str, Any], parsed), snapshot)
        proposal_json = canonical_json(normalized)
        if (
            proposal.proposal_json != proposal_json
            or proposal.proposal_hash != sha256_text(proposal_json)
        ):
            return CandidateProjectionV1("source_changed", note_id, proposal_id)
        source_fingerprint = _prefixed_stored_hash(
            proposal.source_fingerprint,
            "source_fingerprint",
        )
        proposal_hash = _prefixed_stored_hash(proposal.proposal_hash, "proposal_hash")
        candidates = tuple(
            _candidate_from_focus(
                application_id=application.id,
                event_id=event.id,
                note=note,
                proposal=proposal,
                source_fingerprint=source_fingerprint,
                proposal_hash=proposal_hash,
                focus=cast(dict[str, Any], focus),
            )
            for focus in normalized["practice_focuses"]
        )
    except (
        InterviewReviewModelError,
        ReviewReadinessContractError,
        TypeError,
        ValueError,
        UnicodeEncodeError,
    ):
        return CandidateProjectionV1("not_eligible", note_id, proposal_id)
    if not candidates:
        return CandidateProjectionV1("not_eligible", note_id, proposal_id)
    confirmed_focuses = set(
        session.scalars(
            select(InterviewReadinessSignal.focus_id).where(
                InterviewReadinessSignal.source_proposal_id == proposal.id
            )
        )
    )
    available = tuple(
        candidate for candidate in candidates if candidate.focus_id not in confirmed_focuses
    )[:8]
    if not available:
        return CandidateProjectionV1(
            "already_confirmed",
            note_id,
            proposal_id,
            candidates,
        )
    return CandidateProjectionV1("ready", note_id, proposal_id, available)


__all__ = ["project_readiness_candidates"]
