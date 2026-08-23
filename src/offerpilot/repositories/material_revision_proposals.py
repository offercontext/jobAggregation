from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, List

from sqlalchemy import desc, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from offerpilot.ai.agent_contracts import ChatModel
from offerpilot.ai.material_proposals import (
    generate_material_proposal,
    validate_material_proposal,
)
from offerpilot.models import (
    Application,
    ApplicationEvidenceBundle,
    ApplicationEvent,
    ApplicationJDVersion,
    ApplicationMaterialKit,
    MaterialRevisionProposal,
    Resume,
)
from offerpilot.repositories.json_contract import (
    JsonContractError,
    canonical_json,
    parse_json_object,
    sha256_text,
)


class MaterialProposalNotFound(Exception):
    pass


class MaterialProposalValidationError(ValueError):
    pass


class MaterialProposalConflictError(ValueError):
    pass


class MaterialRevisionProposalsRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def create_generated(
        self,
        application_id: int,
        instructions: str,
        user_assertions: list[str],
        model: ChatModel,
        on_diagnostic: Callable[[dict[str, Any]], None] | None = None,
    ) -> MaterialRevisionProposal:
        with self._session_factory() as session:
            snapshot, fingerprint = build_source_snapshot(session, application_id, user_assertions)

        # Do not keep a database connection checked out while waiting for the AI
        # provider. Acceptance rechecks the fingerprint and returns 409 if this
        # frozen source changed during generation.
        validated = generate_material_proposal(
            model,
            snapshot,
            instructions,
            on_diagnostic=on_diagnostic,
        )
        proposal_json = canonical_json(validated.proposal)
        with self._session_factory() as session:
            # Serialize the visibility check and draft insert against concurrent
            # SQLite soft deletes. A competing writer waits until this transaction
            # commits, so it cannot make a just-created draft immediately invisible.
            session.execute(text("BEGIN IMMEDIATE"))
            if _visible_application(session, application_id) is None:
                raise MaterialProposalNotFound()
            try:
                _current_snapshot, current_fingerprint = build_source_snapshot(
                    session, application_id, user_assertions
                )
            except MaterialProposalConflictError:
                raise
            except MaterialProposalValidationError as exc:
                raise MaterialProposalConflictError("source material has changed, please generate a new proposal") from exc
            if current_fingerprint != fingerprint:
                raise MaterialProposalConflictError("source material has changed, please generate a new proposal")
            proposal = MaterialRevisionProposal(
                application_id=application_id,
                material_kit_id=int(snapshot["material_kit"]["id"]),
                jd_version_id=int(snapshot["material_kit"]["jd_version_id"]),
                source_resume_id=int(snapshot["resume"]["id"]),
                source_fingerprint_sha256=fingerprint,
                source_snapshot_json=canonical_json(snapshot),
                proposal_json=proposal_json,
                proposal_sha256=sha256_text(proposal_json),
                status="draft",
                accepted_change_ids_json="[]",
            )
            session.add(proposal)
            session.commit()
            session.refresh(proposal)
            _normalize_proposal_timestamps(proposal)
            return proposal

    def list(self, application_id: int) -> List[MaterialRevisionProposal]:
        statement = (
            select(MaterialRevisionProposal)
            .join(Application, Application.id == MaterialRevisionProposal.application_id)
            .where(MaterialRevisionProposal.application_id == application_id)
            .where(Application.deleted_at.is_(None))
            .order_by(MaterialRevisionProposal.created_at.desc(), MaterialRevisionProposal.id.desc())
        )
        with self._session_factory() as session:
            return [_normalize_proposal_timestamps(item) for item in session.scalars(statement)]

    def get(self, application_id: int, proposal_id: int) -> MaterialRevisionProposal | None:
        with self._session_factory() as session:
            proposal = _visible_proposal(session, application_id, proposal_id)
            return _normalize_proposal_timestamps(proposal) if proposal is not None else None

    def accept(
        self,
        application_id: int,
        proposal_id: int,
        expected_proposal_sha256: str,
        selected_change_ids: List[str],
    ) -> tuple[MaterialRevisionProposal, Resume, bool]:
        with self._session_factory() as session:
            # The source fingerprint is computed from several mutable rows. Acquire
            # SQLite's write lock before reading any of them so no source mutation
            # can slip between the drift check and the atomic proposal claim.
            session.execute(text("BEGIN IMMEDIATE"))
            proposal = _visible_proposal(session, application_id, proposal_id)
            if proposal is None:
                raise MaterialProposalNotFound()
            if proposal.status == "accepted":
                result = session.get(Resume, proposal.result_resume_id) if proposal.result_resume_id else None
                if result is None:
                    raise MaterialProposalConflictError("accepted proposal result is unavailable")
                return _normalize_proposal_timestamps(proposal), result, False
            if proposal.status == "rejected":
                raise MaterialProposalConflictError("rejected proposal cannot be accepted")
            if proposal.proposal_sha256 != expected_proposal_sha256:
                raise MaterialProposalConflictError("proposal has changed, please review it again")

            ids = _validate_selected_ids(selected_change_ids)
            stored_snapshot = _parse_stored_object(proposal.source_snapshot_json, "source snapshot")
            assertions = _assertion_texts(stored_snapshot)
            try:
                current_snapshot, current_fingerprint = build_source_snapshot(
                    session, application_id, assertions
                )
            except MaterialProposalValidationError as exc:
                raise MaterialProposalConflictError(
                    "source material has changed, please generate a new proposal"
                ) from exc
            if current_fingerprint != proposal.source_fingerprint_sha256:
                raise MaterialProposalConflictError("source material has changed, please generate a new proposal")

            proposal_data = _parse_stored_object(proposal.proposal_json, "proposal")
            selected_changes = [
                change for change in proposal_data.get("changes", []) if change.get("id") in ids
            ]
            if len(selected_changes) != len(ids):
                raise MaterialProposalValidationError("selected_change_ids must be a subset of proposal changes")
            validated = validate_material_proposal(
                {"summary": proposal_data.get("summary", ""), "changes": selected_changes},
                current_snapshot,
            )
            source_resume_id = int(current_snapshot["resume"]["id"])
            source_resume = session.get(Resume, source_resume_id)
            kit = session.get(ApplicationMaterialKit, int(current_snapshot["material_kit"]["id"]))
            application = session.get(Application, application_id)
            if source_resume is None or kit is None or application is None:
                raise MaterialProposalConflictError("source material is no longer available")

            claimed = session.execute(
                update(MaterialRevisionProposal)
                .where(MaterialRevisionProposal.id == proposal.id)
                .where(MaterialRevisionProposal.status == "draft")
                .where(MaterialRevisionProposal.proposal_sha256 == expected_proposal_sha256)
                .values(status="accepted")
            )
            if getattr(claimed, "rowcount", 0) != 1:
                session.refresh(proposal)
                if proposal.status == "accepted" and proposal.result_resume_id is not None:
                    result = session.get(Resume, proposal.result_resume_id)
                    if result is not None:
                        return _normalize_proposal_timestamps(proposal), result, False
                if proposal.status == "rejected":
                    raise MaterialProposalConflictError("rejected proposal cannot be accepted")
                raise MaterialProposalConflictError("proposal is already being accepted")
            proposal.status = "accepted"

            new_content = canonical_json(validated.content)
            parsed_data = (
                validated.content["raw_text"]
                if isinstance(validated.content.get("raw_text"), str)
                else source_resume.parsed_data
            )
            child = Resume(
                name=_child_resume_title(source_resume, application),
                title=_child_resume_title(source_resume, application),
                parsed_data=parsed_data,
                parse_status="text-ready" if isinstance(parsed_data, str) and parsed_data.strip() else source_resume.parse_status,
                is_master=False,
                parent_resume_id=source_resume.id,
                source="manual",
                source_file_path="",
                content_json=new_content,
            )
            session.add(child)
            session.flush()
            kit.resume_id = child.id
            proposal.accepted_change_ids_json = canonical_json(ids)
            proposal.result_resume_id = child.id
            proposal.accepted_at = datetime.now(timezone.utc)
            event = ApplicationEvent(
                application_id=application_id,
                event_type="custom",
                subtype="material_proposal_accepted",
                status="done",
                notes="User accepted an evidence-gated material proposal.",
            )
            event.tags = [
                "material_proposal",
                f"proposal:{proposal.id}",
                f"resume:{child.id}",
            ]
            session.add(event)
            session.commit()
            session.refresh(proposal)
            session.refresh(child)
            return _normalize_proposal_timestamps(proposal), child, True

    def reject(self, application_id: int, proposal_id: int) -> MaterialRevisionProposal:
        with self._session_factory() as session:
            proposal = _visible_proposal(session, application_id, proposal_id)
            if proposal is None:
                raise MaterialProposalNotFound()
            if proposal.status == "accepted":
                raise MaterialProposalConflictError("accepted proposal cannot be rejected")
            if proposal.status == "rejected":
                return _normalize_proposal_timestamps(proposal)
            rejected_at = datetime.now(timezone.utc)
            result = session.execute(
                update(MaterialRevisionProposal)
                .where(MaterialRevisionProposal.id == proposal.id)
                .where(MaterialRevisionProposal.status == "draft")
                .values(status="rejected", rejected_at=rejected_at)
            )
            if getattr(result, "rowcount", 0) != 1:
                session.refresh(proposal)
                if proposal.status == "accepted":
                    raise MaterialProposalConflictError("accepted proposal cannot be rejected")
                if proposal.status == "rejected":
                    return _normalize_proposal_timestamps(proposal)
                raise MaterialProposalConflictError("proposal state changed, please retry")
            proposal.status = "rejected"
            proposal.rejected_at = rejected_at
            session.commit()
            session.refresh(proposal)
            return _normalize_proposal_timestamps(proposal)


def build_source_snapshot(
    session: Session, application_id: int, user_assertions: list[str]
) -> tuple[dict[str, Any], str]:
    assertions = _normalize_assertions(user_assertions)
    application = _visible_application(session, application_id)
    if application is None:
        raise MaterialProposalNotFound()
    kits = list(session.scalars(select(ApplicationMaterialKit).where(ApplicationMaterialKit.application_id == application_id)))
    if len(kits) != 1:
        raise MaterialProposalValidationError("application must have exactly one material kit")
    kit = kits[0]
    if not kit.jd_snapshot.strip():
        raise MaterialProposalValidationError("material kit JD is required")
    if kit.jd_version_id is None:
        raise MaterialProposalValidationError("material kit JD version is required")
    current_jd_version_id = session.scalar(
        select(ApplicationJDVersion.id)
        .where(ApplicationJDVersion.application_id == application_id)
        .order_by(desc(ApplicationJDVersion.version_number))
        .limit(1)
    )
    if current_jd_version_id != kit.jd_version_id:
        raise MaterialProposalConflictError("material kit JD source is no longer current")
    if kit.resume_id is None:
        raise MaterialProposalValidationError("material kit must have a linked resume")
    resume = session.scalar(
        select(Resume).where(Resume.id == kit.resume_id).where(Resume.deleted_at.is_(None))
    )
    if resume is None:
        raise MaterialProposalValidationError("linked resume is unavailable")
    try:
        resume_content = parse_json_object("resume", resume.content_json)
        kit_content = parse_json_object("material kit", kit.content_json)
    except JsonContractError as exc:
        raise MaterialProposalValidationError(str(exc)) from exc
    latest = session.scalar(
        select(ApplicationEvidenceBundle)
        .where(ApplicationEvidenceBundle.application_id == application_id)
        .order_by(ApplicationEvidenceBundle.sequence.desc(), ApplicationEvidenceBundle.id.desc())
    )
    evidence: dict[str, Any] | None = None
    if latest is not None:
        evidence = {
            "id": latest.id,
            "bundle_sha256": latest.bundle_sha256,
            "snapshot": _parse_stored_object(latest.snapshot_json, "evidence bundle snapshot"),
        }
    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "application": {
            "id": application.id,
            "company_name": application.company_name,
            "position_name": application.position_name,
        },
        "material_kit": {
            "id": kit.id,
            "jd_version_id": kit.jd_version_id,
            "jd_snapshot": kit.jd_snapshot,
            "content_json": kit_content,
        },
        "resume": {
            "id": resume.id,
            "title": resume.title or resume.name,
            "parsed_data": resume.parsed_data,
            "content_json": resume_content,
        },
        "latest_evidence_bundle": evidence,
        "user_assertions": [
            {"id": f"assertion-{index + 1}", "text": text}
            for index, text in enumerate(assertions)
        ],
    }
    return snapshot, sha256_text(canonical_json(snapshot))


def _normalize_assertions(assertions: list[str]) -> list[str]:
    if not isinstance(assertions, list) or len(assertions) > 10:
        raise MaterialProposalValidationError("user_assertions must contain at most 10 items")
    normalized: list[str] = []
    for assertion in assertions:
        if not isinstance(assertion, str) or not assertion.strip() or len(assertion.strip()) > 500:
            raise MaterialProposalValidationError("each user assertion must be 1-500 characters")
        normalized.append(assertion.strip())
    return normalized


def _validate_selected_ids(selected: list[str]) -> list[str]:
    if not isinstance(selected, list) or not selected:
        raise MaterialProposalValidationError("selected_change_ids must not be empty")
    if any(not isinstance(item, str) or not item for item in selected):
        raise MaterialProposalValidationError("selected_change_ids must contain non-empty strings")
    if len(set(selected)) != len(selected):
        raise MaterialProposalValidationError("selected_change_ids must be unique")
    return list(selected)


def _assertion_texts(snapshot: dict[str, Any]) -> list[str]:
    value = snapshot.get("user_assertions")
    if not isinstance(value, list):
        return []
    return [item["text"] for item in value if isinstance(item, dict) and isinstance(item.get("text"), str)]


def _parse_stored_object(value: str, name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise MaterialProposalValidationError(f"{name} must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise MaterialProposalValidationError(f"{name} must be a JSON object")
    return parsed


def _visible_application(session: Session, application_id: int) -> Application | None:
    return session.scalar(
        select(Application).where(Application.id == application_id).where(Application.deleted_at.is_(None))
    )


def _visible_proposal(session: Session, application_id: int, proposal_id: int) -> MaterialRevisionProposal | None:
    return session.scalar(
        select(MaterialRevisionProposal)
        .join(Application, Application.id == MaterialRevisionProposal.application_id)
        .where(MaterialRevisionProposal.application_id == application_id)
        .where(MaterialRevisionProposal.id == proposal_id)
        .where(Application.deleted_at.is_(None))
    )


def _child_resume_title(source: Resume, application: Application) -> str:
    source_title = source.title or source.name or "Resume"
    return f"{source_title} · {application.company_name} {application.position_name}".strip()


def _normalize_proposal_timestamps(proposal: MaterialRevisionProposal) -> MaterialRevisionProposal:
    for attr in ("created_at", "accepted_at", "rejected_at"):
        value = getattr(proposal, attr)
        if value is None:
            continue
        if value.tzinfo is None or value.utcoffset() is None:
            setattr(proposal, attr, value.replace(tzinfo=timezone.utc))
        else:
            setattr(proposal, attr, value.astimezone(timezone.utc))
    return proposal
