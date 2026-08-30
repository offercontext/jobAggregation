from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session, sessionmaker

from offerpilot.knowledge.interview_capture import (
    CAPTURE_SCHEMA_VERSION,
    CanonicalFragment,
    FragmentValidationError,
    canonicalize_fragments,
    fragments_json,
    note_fingerprint,
    serialize_capture_snapshot_with_ranges,
    source_fields_for_note,
)
from offerpilot.models import (
    Application,
    InterviewKnowledgeCaptureAttempt,
    KnowledgeCapturedSourceMetadata,
    KnowledgeEvidence,
    KnowledgeExtractionSnapshot,
    KnowledgeNote,
    KnowledgeNoteEvidence,
    KnowledgeNoteVersion,
    KnowledgeSource,
    InterviewNote,
)
from offerpilot.repositories.json_contract import canonical_json


class InterviewKnowledgeCaptureNotFound(LookupError):
    pass


class CaptureAttemptConflict(ValueError):
    pass


class CaptureAttemptExpired(ValueError):
    pass


class CaptureAttemptConfirmed(ValueError):
    pass


class InterviewKnowledgeSourceChanged(ValueError):
    pass


class InterviewKnowledgeValidationError(ValueError):
    pass


@dataclass(frozen=True)
class CaptureAttemptView:
    attempt_key: str
    note_fingerprint: str
    fragments: list[CanonicalFragment]
    preview_status: str
    preview: dict[str, Any]
    preview_error_code: str
    confirmed_note_version_id: int | None = None


@dataclass(frozen=True)
class AiPreviewClaim(CaptureAttemptView):
    should_call_provider: bool = False
    preview_revision: int = 0
    provider_call_token: str = ""


@dataclass(frozen=True)
class ConfirmedCapture:
    version_id: int
    note_id: int
    source_id: int
    content: dict[str, Any]
    evidence: list[dict[str, Any]]
    created: bool


def _visible_note(session: Session, note_id: int) -> InterviewNote | None:
    return session.scalar(
        select(InterviewNote)
        .outerjoin(Application, Application.id == InterviewNote.application_id)
        .where(InterviewNote.id == note_id)
        .where(
            (InterviewNote.application_id.is_(None))
            | (Application.deleted_at.is_(None))
        )
    )


def _json_preview(value: str) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}


def _fragments_from_json(value: str) -> list[CanonicalFragment]:
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise CaptureAttemptConflict("stored fragments are invalid")
    return [
        CanonicalFragment(
            fragment_id=str(item["fragment_id"]),
            path=str(item["path"]),
            start=int(item["start"]),
            end=int(item["end"]),
            text=str(item["text"]),
        )
        for item in parsed
        if isinstance(item, dict)
    ]


def _fragments_payload(fragments: Iterable[CanonicalFragment]) -> list[dict[str, Any]]:
    return [fragment.as_dict() for fragment in fragments]


def _view(attempt: InterviewKnowledgeCaptureAttempt) -> CaptureAttemptView:
    return CaptureAttemptView(
        attempt_key=attempt.attempt_key,
        note_fingerprint=attempt.note_fingerprint,
        fragments=_fragments_from_json(attempt.selected_fragments_json),
        preview_status=attempt.preview_status,
        preview=_json_preview(attempt.preview_json),
        preview_error_code=attempt.preview_error_code,
        confirmed_note_version_id=attempt.confirmed_note_version_id,
    )


def direct_preview(fragments: list[CanonicalFragment]) -> dict[str, Any]:
    return {
        "title": "",
        "blocks": [
            {
                "block_id": f"block_{index:03d}",
                "text": fragment.text,
                "evidence_refs": [
                    {"fragment_id": fragment.fragment_id, "excerpt": fragment.text}
                ],
            }
            for index, fragment in enumerate(fragments, start=1)
        ],
    }


class InterviewKnowledgeCaptureRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def prepare_preview(
        self,
        note_id: int,
        attempt_key: str,
        mode: str,
        raw_fragments: list[dict[str, Any]],
    ) -> CaptureAttemptView:
        if mode not in {"direct", "ai"}:
            raise FragmentValidationError("preview mode is invalid")
        with self._session_factory() as session:
            note = _visible_note(session, note_id)
            if note is None:
                raise InterviewKnowledgeCaptureNotFound()
            attempt = session.scalar(
                select(InterviewKnowledgeCaptureAttempt)
                .where(InterviewKnowledgeCaptureAttempt.note_id == note_id)
                .where(InterviewKnowledgeCaptureAttempt.attempt_key == attempt_key)
            )
            if attempt is not None and (
                attempt.preview_status == "confirmed" or attempt.confirmed_note_version_id is not None
            ):
                return _view(attempt)
            fragments = canonicalize_fragments(raw_fragments, source_fields_for_note(note))
            fingerprint = note_fingerprint(note)
            serialized_fragments = fragments_json(fragments)
            if attempt is not None:
                if (
                    attempt.note_fingerprint != fingerprint
                    or attempt.selected_fragments_json != serialized_fragments
                ):
                    raise CaptureAttemptConflict("capture attempt input changed")
                if attempt.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
                    raise CaptureAttemptExpired()
            else:
                attempt = InterviewKnowledgeCaptureAttempt(
                    note_id=note_id,
                    attempt_key=attempt_key,
                    note_fingerprint=fingerprint,
                    selected_fragments_json=serialized_fragments,
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
                )
                session.add(attempt)
                session.flush()
            if mode == "direct":
                attempt.last_preview_mode = "direct"
                attempt.preview_status = "direct_ready"
                attempt.preview_json = canonical_json(direct_preview(fragments))
                attempt.preview_error_code = ""
            session.commit()
            session.refresh(attempt)
            return _view(attempt)

    def claim_ai_preview(
        self,
        note_id: int,
        attempt_key: str,
        fragments: list[CanonicalFragment],
    ) -> AiPreviewClaim:
        with self._session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            note = _visible_note(session, note_id)
            if note is None:
                raise InterviewKnowledgeCaptureNotFound()
            attempt = session.scalar(
                select(InterviewKnowledgeCaptureAttempt)
                .where(InterviewKnowledgeCaptureAttempt.note_id == note_id)
                .where(InterviewKnowledgeCaptureAttempt.attempt_key == attempt_key)
            )
            if attempt is None:
                raise InterviewKnowledgeCaptureNotFound()
            if attempt.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
                raise CaptureAttemptExpired()
            if attempt.note_fingerprint != note_fingerprint(note):
                raise CaptureAttemptConflict("capture attempt source changed")
            if attempt.selected_fragments_json != fragments_json(fragments):
                raise CaptureAttemptConflict("capture attempt fragments changed")
            view = _view(attempt)
            if attempt.preview_status == "ai_generating":
                session.commit()
                return AiPreviewClaim(**view.__dict__, should_call_provider=False, preview_revision=attempt.preview_revision)
            if attempt.preview_status in {"ai_ready", "safe_empty", "confirmed"}:
                session.commit()
                return AiPreviewClaim(**view.__dict__, should_call_provider=False, preview_revision=attempt.preview_revision)
            attempt.last_preview_mode = "ai"
            attempt.preview_status = "ai_generating"
            attempt.preview_revision += 1
            attempt.provider_call_token = uuid4().hex
            attempt.preview_error_code = ""
            session.commit()
            return AiPreviewClaim(
                attempt_key=attempt.attempt_key,
                note_fingerprint=attempt.note_fingerprint,
                fragments=fragments,
                preview_status=attempt.preview_status,
                preview={},
                preview_error_code="",
                confirmed_note_version_id=attempt.confirmed_note_version_id,
                should_call_provider=True,
                preview_revision=attempt.preview_revision,
                provider_call_token=attempt.provider_call_token,
            )

    def complete_ai_preview(
        self,
        note_id: int,
        attempt_key: str,
        preview_revision: int,
        provider_call_token: str,
        preview: dict[str, Any],
        error_code: str = "",
    ) -> bool:
        status = "safe_empty" if not preview.get("blocks") else "ai_ready"
        with self._session_factory() as session:
            result = session.execute(
                update(InterviewKnowledgeCaptureAttempt)
                .where(InterviewKnowledgeCaptureAttempt.note_id == note_id)
                .where(InterviewKnowledgeCaptureAttempt.attempt_key == attempt_key)
                .where(InterviewKnowledgeCaptureAttempt.preview_revision == preview_revision)
                .where(InterviewKnowledgeCaptureAttempt.provider_call_token == provider_call_token)
                .where(InterviewKnowledgeCaptureAttempt.preview_status == "ai_generating")
                .values(
                    preview_status=status,
                    preview_json=canonical_json(preview),
                    preview_error_code=error_code,
                    provider_call_token="",
                )
            )
            session.commit()
            return int(getattr(result, "rowcount", 0) or 0) == 1

    def mark_provider_unknown(
        self, note_id: int, attempt_key: str, preview_revision: int, provider_call_token: str
    ) -> bool:
        with self._session_factory() as session:
            result = session.execute(
                update(InterviewKnowledgeCaptureAttempt)
                .where(InterviewKnowledgeCaptureAttempt.note_id == note_id)
                .where(InterviewKnowledgeCaptureAttempt.attempt_key == attempt_key)
                .where(InterviewKnowledgeCaptureAttempt.preview_revision == preview_revision)
                .where(InterviewKnowledgeCaptureAttempt.provider_call_token == provider_call_token)
                .where(InterviewKnowledgeCaptureAttempt.preview_status == "ai_generating")
                .values(preview_status="provider_unknown", provider_call_token="")
            )
            session.commit()
            return int(getattr(result, "rowcount", 0) or 0) == 1

    def get_attempt(self, note_id: int, attempt_key: str) -> CaptureAttemptView | None:
        with self._session_factory() as session:
            if _visible_note(session, note_id) is None:
                raise InterviewKnowledgeCaptureNotFound()
            attempt = session.scalar(
                select(InterviewKnowledgeCaptureAttempt)
                .where(InterviewKnowledgeCaptureAttempt.note_id == note_id)
                .where(InterviewKnowledgeCaptureAttempt.attempt_key == attempt_key)
            )
            return _view(attempt) if attempt is not None else None

    def discard_unconfirmed_attempt(self, note_id: int, attempt_key: str) -> bool:
        with self._session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            attempt = session.scalar(
                select(InterviewKnowledgeCaptureAttempt)
                .where(InterviewKnowledgeCaptureAttempt.note_id == note_id)
                .where(InterviewKnowledgeCaptureAttempt.attempt_key == attempt_key)
            )
            if attempt is None:
                session.commit()
                return False
            if attempt.preview_status == "confirmed" or attempt.confirmed_note_version_id is not None:
                session.rollback()
                raise CaptureAttemptConfirmed()
            session.delete(attempt)
            session.commit()
            return True

    def confirm(
        self,
        note_id: int,
        attempt_key: str,
        note_fingerprint_value: str,
        title: str,
        blocks: list[dict[str, Any]],
    ) -> ConfirmedCapture:
        from offerpilot.ai.interview_knowledge_capture import (
            InterviewKnowledgePreviewError,
            validate_interview_knowledge_preview,
        )
        with self._session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            attempt = session.scalar(
                select(InterviewKnowledgeCaptureAttempt)
                .where(InterviewKnowledgeCaptureAttempt.note_id == note_id)
                .where(InterviewKnowledgeCaptureAttempt.attempt_key == attempt_key)
            )
            if attempt is None:
                raise InterviewKnowledgeCaptureNotFound()
            if attempt.preview_status == "confirmed" and attempt.confirmed_note_version_id:
                version = session.get(KnowledgeNoteVersion, attempt.confirmed_note_version_id)
                if version is None:
                    raise InterviewKnowledgeCaptureNotFound()
                source = session.get(KnowledgeSource, version.source_id)
                evidence_rows = list(
                    session.scalars(
                        select(KnowledgeEvidence)
                        .join(KnowledgeNoteEvidence, KnowledgeNoteEvidence.evidence_id == KnowledgeEvidence.id)
                        .where(KnowledgeNoteEvidence.note_version_id == version.id)
                    )
                )
                return ConfirmedCapture(
                    version_id=version.id,
                    note_id=note_id,
                    source_id=source.id if source else 0,
                    content=json.loads(version.content_json),
                    evidence=[
                        {"id": row.id, "path": json.loads(row.heading_path_json)[0] if row.heading_path_json else "", "excerpt": row.canonical_excerpt}
                        for row in evidence_rows
                    ],
                    created=False,
                )
            if attempt.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
                raise CaptureAttemptExpired()
            if attempt.preview_status not in {"direct_ready", "ai_ready", "safe_empty"}:
                raise InterviewKnowledgeValidationError("preview_required")
            current_note = _visible_note(session, note_id)
            if current_note is None:
                raise InterviewKnowledgeCaptureNotFound()
            current_fingerprint = note_fingerprint(current_note)
            if (
                attempt.note_fingerprint != current_fingerprint
                or note_fingerprint_value != current_fingerprint
            ):
                raise InterviewKnowledgeSourceChanged()
            fragments = _fragments_from_json(attempt.selected_fragments_json)
            try:
                canonical_content = validate_interview_knowledge_preview(
                    {"title": title, "blocks": blocks}, fragments
                )
            except InterviewKnowledgePreviewError as exc:
                raise InterviewKnowledgeValidationError(exc.category) from exc
            snapshot_bytes, snapshot_ranges = serialize_capture_snapshot_with_ranges(fragments)
            snapshot_text = snapshot_bytes.decode("utf-8")
            snapshot_digest = hashlib.sha256(snapshot_bytes).hexdigest()
            source_hash = hashlib.sha256(
                f"{note_id}|{current_fingerprint}|{snapshot_digest}|{CAPTURE_SCHEMA_VERSION}".encode("utf-8")
            ).hexdigest()
            source = session.scalar(select(KnowledgeSource).where(KnowledgeSource.source_hash == source_hash))
            evidence_by_fragment: dict[str, KnowledgeEvidence] = {}
            if source is None:
                source = KnowledgeSource(
                    source_hash=source_hash,
                    source_kind="captured_interview_note",
                    title_hint=title,
                    main_filename="interview-note.txt",
                    main_media_type="text/plain",
                    main_relative_path=f"captured://interview-note/{note_id}",
                    manifest_json=json.dumps(
                        {"origin_note_id": note_id, "fragment_count": len(fragments)},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    total_bytes=len(snapshot_bytes),
                    token_count=0,
                    lifecycle="active",
                    extraction_status="extracted",
                    brief_status="not_started",
                )
                session.add(source)
                session.flush()
                snapshot = KnowledgeExtractionSnapshot(
                    source_id=source.id,
                    extractor_version="interview-note-capture-v1",
                    parser_version="interview-note-capture-v1",
                    normalization_version="none",
                    tokenizer_version="none",
                    encoding="utf-8",
                    detection_method="selected-fragments",
                    canonical_text=snapshot_text,
                    structure_manifest=json.dumps(
                        {"fragments": [item.as_dict() for item in fragments]},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    digest=snapshot_digest,
                    token_count=0,
                    char_count=len(snapshot_text),
                )
                session.add(snapshot)
                session.flush()
                source.active_snapshot_id = snapshot.id
                metadata = KnowledgeCapturedSourceMetadata(
                    source_id=source.id,
                    origin_note_id=note_id,
                    application_event_id=current_note.application_event_id,
                    note_fingerprint=current_fingerprint,
                    selected_fragments_json=attempt.selected_fragments_json,
                    capture_schema_version="interview-note-capture-v1",
                )
                session.add(metadata)
                for ordinal, fragment in enumerate(fragments, start=1):
                    evidence_id = "ev_capture_" + hashlib.sha256(
                        f"{source_hash}|{fragment.fragment_id}|{fragment.text}".encode("utf-8")
                    ).hexdigest()[:24]
                    evidence = KnowledgeEvidence(
                        id=evidence_id,
                        source_id=source.id,
                        snapshot_id=snapshot.id,
                        kind="interview_note_fragment",
                        block_kind="captured_fragment",
                        ordinal=ordinal,
                        heading_path_json=json.dumps([fragment.path], ensure_ascii=False),
                        char_start=snapshot_ranges[fragment.fragment_id].char_start,
                        char_end=snapshot_ranges[fragment.fragment_id].char_end,
                        line_start=snapshot_ranges[fragment.fragment_id].line_start,
                        line_end=snapshot_ranges[fragment.fragment_id].line_end,
                        canonical_excerpt=fragment.text,
                        search_text=fragment.text,
                        content_hash=hashlib.sha256(fragment.text.encode("utf-8")).hexdigest(),
                    )
                    session.add(evidence)
                    evidence_by_fragment[fragment.fragment_id] = evidence
                session.flush()
            else:
                evidence_rows = list(
                    session.scalars(
                        select(KnowledgeEvidence)
                        .where(KnowledgeEvidence.source_id == source.id)
                        .order_by(KnowledgeEvidence.ordinal.asc())
                    )
                )
                evidence_by_fragment = {
                    fragment.fragment_id: row
                    for fragment, row in zip(fragments, evidence_rows, strict=False)
                }
            knowledge_note = KnowledgeNote(
                title=canonical_content["title"],
                origin_kind="confirmed_interview_capture",
            )
            session.add(knowledge_note)
            session.flush()
            version_number = int(
                session.scalar(
                    select(KnowledgeNoteVersion.version_number)
                    .where(KnowledgeNoteVersion.note_id == knowledge_note.id)
                    .order_by(KnowledgeNoteVersion.version_number.desc())
                )
                or 0
            ) + 1
            submitted_preview_json = canonical_json(canonical_content)
            if submitted_preview_json != attempt.preview_json:
                origin = "user_edited_preview"
            else:
                origin = "direct_selected_text" if attempt.last_preview_mode == "direct" else "ai_preview"
            version = KnowledgeNoteVersion(
                note_id=knowledge_note.id,
                version_number=version_number,
                content_json=canonical_json(canonical_content),
                content_hash=hashlib.sha256(canonical_json(canonical_content).encode("utf-8")).hexdigest(),
                content_origin=origin,
                capture_attempt_key=attempt_key,
                source_id=source.id,
            )
            session.add(version)
            session.flush()
            evidence_payload: list[dict[str, Any]] = []
            for block in canonical_content["blocks"]:
                for ref in block["evidence_refs"]:
                    matched_evidence = evidence_by_fragment.get(ref["fragment_id"])
                    if matched_evidence is None:
                        raise InterviewKnowledgeValidationError("unknown_evidence_ref")
                    session.add(
                        KnowledgeNoteEvidence(
                            note_version_id=version.id,
                            block_id=block["block_id"],
                            evidence_id=matched_evidence.id,
                        )
                    )
                    evidence_payload.append(
                        {
                            "id": matched_evidence.id,
                            "path": json.loads(matched_evidence.heading_path_json)[0]
                            if matched_evidence.heading_path_json
                            else "",
                            "excerpt": matched_evidence.canonical_excerpt,
                        }
                    )
            knowledge_note.current_version_id = version.id
            knowledge_note.updated_at = datetime.now(timezone.utc)
            attempt.preview_status = "confirmed"
            attempt.confirmed_note_version_id = version.id
            attempt.provider_call_token = ""
            session.commit()
            return ConfirmedCapture(
                version_id=version.id,
                note_id=note_id,
                source_id=source.id,
                content=canonical_content,
                evidence=evidence_payload,
                created=True,
            )

    def list_knowledge_notes(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(KnowledgeNote)
                    .where(KnowledgeNote.origin_kind == "confirmed_interview_capture")
                    .order_by(KnowledgeNote.created_at.desc(), KnowledgeNote.id.desc())
                )
            )
            return [self._knowledge_note_payload(session, row) for row in rows]

    def get_knowledge_note(self, knowledge_note_id: int) -> dict[str, Any] | None:
        with self._session_factory() as session:
            row = session.get(KnowledgeNote, knowledge_note_id)
            if row is None or row.origin_kind != "confirmed_interview_capture":
                return None
            return self._knowledge_note_payload(session, row)

    @staticmethod
    def _knowledge_note_payload(session: Session, row: KnowledgeNote) -> dict[str, Any]:
        version = session.get(KnowledgeNoteVersion, row.current_version_id) if row.current_version_id else None
        if version is None:
            return {"id": row.id, "title": row.title, "origin_kind": row.origin_kind, "version": None}
        source = session.get(KnowledgeSource, version.source_id)
        metadata = session.scalar(
            select(KnowledgeCapturedSourceMetadata).where(
                KnowledgeCapturedSourceMetadata.source_id == version.source_id
            )
        )
        source_status = "frozen"
        if metadata is not None:
            current_note = session.get(InterviewNote, metadata.origin_note_id)
            if current_note is None or note_fingerprint(current_note) != metadata.note_fingerprint:
                source_status = "source_changed"
        evidence_rows = list(
            session.scalars(
                select(KnowledgeEvidence)
                .join(KnowledgeNoteEvidence, KnowledgeNoteEvidence.evidence_id == KnowledgeEvidence.id)
                .where(KnowledgeNoteEvidence.note_version_id == version.id)
                .order_by(KnowledgeEvidence.ordinal.asc())
            )
        )
        frozen_at = source.created_at.isoformat() if source else ""
        content = json.loads(version.content_json)
        evidence_by_id = {
            evidence.id: {
                "id": evidence.id,
                "path": json.loads(evidence.heading_path_json)[0]
                if evidence.heading_path_json
                else "",
                "excerpt": evidence.canonical_excerpt,
                "char_start": evidence.char_start,
                "char_end": evidence.char_end,
                "line_start": evidence.line_start,
                "line_end": evidence.line_end,
                "frozen_at": frozen_at,
            }
            for evidence in evidence_rows
        }
        links = list(
            session.execute(
                select(KnowledgeNoteEvidence.block_id, KnowledgeNoteEvidence.evidence_id)
                .where(KnowledgeNoteEvidence.note_version_id == version.id)
            )
        )
        evidence_by_block: dict[str, list[dict[str, Any]]] = {}
        for block_id, evidence_id in links:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is not None:
                evidence_by_block.setdefault(block_id, []).append(evidence)
        for block in content.get("blocks", []):
            block["evidence"] = evidence_by_block.get(block.get("block_id"), [])
        return {
            "id": row.id,
            "title": row.title,
            "origin_kind": row.origin_kind,
            "version_id": version.id,
            "version_number": version.version_number,
            "content": content,
            "source_id": version.source_id,
            "capture_metadata": (
                {
                    "source_id": metadata.source_id,
                    "origin_note_id": metadata.origin_note_id,
                    "application_event_id": metadata.application_event_id,
                    "note_fingerprint": metadata.note_fingerprint,
                    "capture_schema_version": metadata.capture_schema_version,
                    "captured_at": metadata.captured_at.isoformat(),
                }
                if metadata is not None
                else None
            ),
            "source_status": source_status,
            "captured_at": frozen_at,
            "evidence": list(evidence_by_id.values()),
        }
