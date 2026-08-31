from __future__ import annotations

import json
import hashlib
import math
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, cast
from uuid import UUID, uuid4

from sqlalchemy import case, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from offerpilot.models import (
    InterviewNote,
    InterviewStory,
    InterviewStoryProposalAttempt,
    InterviewStoryUserAssertion,
    InterviewStoryVersion,
    InterviewStoryVersionEvidenceLink,
    MockInterviewAttempt,
    MockInterviewTurn,
    Resume,
    WriteOperation,
    WriteOperationTransition,
)
from offerpilot.product_actions.compensation import (
    ProductActionCompensationStale,
    _CompensationExecutionUowClaimV1,
    _CompensationExecutionUowV1,
)
from offerpilot.product_actions.contracts import (
    JSONValue,
    ProductActionContractError,
    ProductActionExecutionAuthorization,
    ProductActionIntegrityError,
    ProductActionProofRegistryV1,
    canonical_product_action_json,
)
from offerpilot.product_actions.coordinator import (
    ProductActionCoordinator,
    ProductActionCoordinatorError,
    ProductActionDeclaredExecutorFailureV1,
    ProductActionHandlerResultV1,
    ProductActionPreClaimDispositionV1,
    ProductActionPreflightV1,
    ProductActionStoryWriteConflict,
    ProductActionTerminalBudgetsV1,
    ProductActionTerminalProjectionV1,
    TrustedProductActionDecisionV1,
)
from offerpilot.product_actions.issuer import (
    InterviewStoryActionIssuer,
    PreparedProductActionProposalV1,
)
from offerpilot.product_actions.repository import (
    ProductActionBundleV1,
    ProductActionProposalRepository,
    ProductActionPublicationV1,
    ProductActionPublicationReplayV1,
    ProductActionPublicationUoWV1,
)
from offerpilot.repositories.json_contract import canonical_json, sha256_text


class StoryValidationError(ValueError):
    """Raised when Story content or selected candidate evidence is invalid."""


class StoryNotFoundError(StoryValidationError):
    """Raised when a requested Story is not available."""


class StoryConflictError(StoryValidationError):
    """Raised when an immutable Story pointer or lifecycle CAS is stale."""


class StoryIdempotencyConflictError(StoryConflictError):
    """Raised when a replay key is reused with a different frozen input."""


class StoryCasConflictError(StoryConflictError):
    """Raised when a Story revision/current-version compare-and-swap is stale."""


class StorySourceConflictError(StoryConflictError):
    """Raised when a frozen source changed or is no longer materializable."""


@dataclass(frozen=True)
class StorySourceSnapshot:
    sources: list[dict[str, str]]
    source_fingerprint: str


@dataclass(frozen=True)
class CanonicalStoryLink:
    target_kind: str
    target_id: str
    source_kind: str
    source_stable_id: str
    source_version_or_snapshot: str
    source_path: str
    text_location: str
    excerpt: str
    source_fingerprint: str
    link_hash: str

    def as_dict(self) -> dict[str, str]:
        return {
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "source_kind": self.source_kind,
            "source_stable_id": self.source_stable_id,
            "source_version_or_snapshot": self.source_version_or_snapshot,
            "source_path": self.source_path,
            "text_location": self.text_location,
            "excerpt": self.excerpt,
            "source_fingerprint": self.source_fingerprint,
            "link_hash": self.link_hash,
        }


_NOTE_FIELDS = {
    "/questions": "questions",
    "/self_reflection": "self_reflection",
    "/difficulty_points": "difficulty_points",
    "/mood": "mood",
}
_ALLOWED_SOURCE_KINDS = {"resume_version", "interview_note", "mock_turn"}
_FACT_GAP_CODES = {"missing_result"}
_BLOCK_KINDS = {"situation", "task", "action", "result", "reflection"}
_TARGET_KINDS = {"title", "block", "capability_label", "applicable_question"}
_MAX_EVIDENCE_EXCERPT_CHARS = 800
_MAX_EVIDENCE_LINKS_PER_TARGET = 5
_MAX_TITLE_CHARS = 200
_MAX_BLOCKS = 12
_MAX_BLOCK_TEXT_CHARS = 4_000
_MAX_SHORT_ITEMS = 12
_MAX_SHORT_TEXT_CHARS = 300
_MAX_FACT_GAPS = 1
_MAX_ASSERTION_CHARS = 4_000
_STORY_VERSION_SCHEMA = "interview-story-v1"
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_STORY_LIFECYCLE_LEGACY_IDEMPOTENCY_PREFIX = "story_lifecycle_"
_STORY_LIFECYCLE_INTERNAL_IDEMPOTENCY_PREFIX = "story:lifecycle:"
_STORY_LEASE_SECONDS = 30
_STORY_HEARTBEAT_SECONDS = 10
_STORY_RETRY_SAFETY_MARGIN_MS = 250


def _is_optional_positive_int(value: object) -> bool:
    return value is None or (type(value) is int and value > 0)


def _require_bounded_repair_count(value: object) -> None:
    if type(value) is not int or value < 0 or value > 1:
        raise StoryValidationError("story repair count is invalid")


def canonical_story_content(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize user content and allocate version-local stable target IDs."""

    title = raw.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > _MAX_TITLE_CHARS:
        raise StoryValidationError("title is invalid")
    blocks_raw = raw.get("blocks", [])
    labels_raw = raw.get("capability_labels", [])
    questions_raw = raw.get("applicable_questions", [])
    gaps_raw = raw.get("fact_gap_codes", [])
    if not all(isinstance(value, list) for value in (blocks_raw, labels_raw, questions_raw, gaps_raw)):
        raise StoryValidationError("story collections must be arrays")

    if len(blocks_raw) > _MAX_BLOCKS or len(labels_raw) > _MAX_SHORT_ITEMS or len(questions_raw) > _MAX_SHORT_ITEMS or len(gaps_raw) > _MAX_FACT_GAPS:
        raise StoryValidationError("story content exceeds limits")

    block_counts: dict[str, int] = {}
    blocks: list[dict[str, str]] = []
    for item in blocks_raw:
        if not isinstance(item, Mapping):
            raise StoryValidationError("block must be an object")
        kind = item.get("kind")
        value = item.get("text")
        fact_mode = item.get("fact_mode")
        if (
            kind not in _BLOCK_KINDS
            or not isinstance(value, str)
            or not value.strip()
            or len(value) > _MAX_BLOCK_TEXT_CHARS
            or not isinstance(fact_mode, str)
        ):
            raise StoryValidationError("block is invalid")
        if kind == "reflection":
            if fact_mode != "user_view":
                raise StoryValidationError("reflection must be user_view")
        elif fact_mode != "evidence_backed":
            raise StoryValidationError("STAR block must be evidence_backed")
        block_counts[kind] = block_counts.get(kind, 0) + 1
        blocks.append(
            {
                "id": f"{kind}_{block_counts[kind]:03d}",
                "kind": kind,
                "text": value,
                "fact_mode": fact_mode,
            }
        )

    def _items(values: list[Any], prefix: str) -> list[dict[str, str]]:
        if not all(isinstance(value, str) and value.strip() and len(value) <= _MAX_SHORT_TEXT_CHARS for value in values):
            raise StoryValidationError(f"{prefix} items must be strings")
        return [{"id": f"{prefix}_{index:03d}", "text": value} for index, value in enumerate(values, 1)]

    if not all(isinstance(code, str) and code in _FACT_GAP_CODES for code in gaps_raw):
        raise StoryValidationError("fact gap code is invalid")
    if len(set(gaps_raw)) != len(gaps_raw):
        raise StoryValidationError("fact gap code is duplicated")
    has_result = any(block["kind"] == "result" for block in blocks)
    if has_result != (gaps_raw == []):
        raise StoryValidationError("result and fact gap are inconsistent")
    return {
        "title": {"id": "title", "text": title},
        "blocks": blocks,
        "capability_labels": _items(labels_raw, "capability"),
        "applicable_questions": _items(questions_raw, "question"),
        "fact_gap_codes": list(gaps_raw),
    }


def validate_story_evidence_links(
    content: Mapping[str, Any],
    links: list[dict[str, Any]],
    snapshot: StorySourceSnapshot,
) -> list[CanonicalStoryLink]:
    """Bind every non-empty Story target to exact, frozen candidate evidence.

    Callers may only reference a source leaf emitted by
    :func:`materialize_selected_sources`; clients cannot substitute a source,
    path, snapshot fingerprint, or fabricated excerpt.
    """

    expected_targets = _evidence_targets(content)
    catalog = {
        _source_identity(item): item
        for item in snapshot.sources
    }
    canonical: list[CanonicalStoryLink] = []
    link_identities: set[tuple[str, str, str, str, str, str, str, str]] = set()
    linked_targets: set[tuple[str, str]] = set()
    requested_counts: dict[tuple[str, str], int] = {}
    for raw in links:
        if not isinstance(raw, Mapping):
            continue
        target = (raw.get("target_kind"), raw.get("target_id"))
        if target not in expected_targets:
            continue
        requested_counts[target] = requested_counts.get(target, 0) + 1
        if requested_counts[target] > _MAX_EVIDENCE_LINKS_PER_TARGET:
            raise StoryValidationError("evidence link count exceeds limit")
    for raw in links:
        if not isinstance(raw, Mapping):
            raise StoryValidationError("evidence link must be an object")
        full_allowed = {
            "target_kind",
            "target_id",
            "source_kind",
            "source_stable_id",
            "source_version_or_snapshot",
            "source_path",
            "excerpt",
            "text_location",
        }
        client_allowed = {
            "target_kind",
            "target_id",
            "source_kind",
            "source_id",
            "source_path",
            "excerpt",
            "text_location",
        }
        fields = set(raw)
        if fields - full_allowed and fields - client_allowed:
            raise StoryValidationError("evidence link has extra fields")
        target_kind = raw.get("target_kind")
        target_id = raw.get("target_id")
        if (
            target_kind not in _TARGET_KINDS
            or not isinstance(target_id, str)
            or (target_kind, target_id) not in expected_targets
        ):
            raise StoryValidationError("evidence link target is invalid")
        source_kind = raw.get("source_kind")
        source_stable_id = raw.get("source_stable_id", raw.get("source_id"))
        source_version_or_snapshot = raw.get("source_version_or_snapshot")
        source_path = raw.get("source_path")
        excerpt = raw.get("excerpt")
        text_location = raw.get("text_location", "")
        if not isinstance(source_kind, str):
            raise StoryValidationError("evidence link shape is invalid")
        if isinstance(source_stable_id, int) and not isinstance(source_stable_id, bool):
            source_stable_id = str(source_stable_id)
        if not isinstance(source_stable_id, str):
            raise StoryValidationError("evidence link shape is invalid")
        if not isinstance(source_path, str):
            raise StoryValidationError("evidence link shape is invalid")
        if not isinstance(excerpt, str):
            raise StoryValidationError("evidence link shape is invalid")
        if not isinstance(text_location, str):
            raise StoryValidationError("evidence link shape is invalid")
        if excerpt == "":
            raise StoryValidationError("evidence link shape is invalid")
        if not excerpt.strip():
            raise StoryValidationError("evidence excerpt is invalid")
        if len(excerpt) > _MAX_EVIDENCE_EXCERPT_CHARS:
            raise StoryValidationError("evidence excerpt exceeds limit")
        if source_version_or_snapshot is None:
            source = next(
                (
                    entry
                    for entry in snapshot.sources
                    if entry["source_kind"] == source_kind
                    and entry["source_stable_id"] == source_stable_id
                    and entry["path"] == source_path
                ),
                None,
            )
            source_version_or_snapshot = source["source_version_or_snapshot"] if source else None
        if not isinstance(source_version_or_snapshot, str):
            raise StoryValidationError("evidence link shape is invalid")
        source = catalog.get((source_kind, source_stable_id, source_version_or_snapshot, source_path))
        if source is None:
            raise StoryValidationError("evidence source is invalid")
        if excerpt not in source["excerpt"]:
            raise StoryValidationError("evidence excerpt is invalid")
        payload = {
            "target_kind": target_kind,
            "target_id": target_id,
            "source_kind": source_kind,
            "source_stable_id": source_stable_id,
            "source_version_or_snapshot": source_version_or_snapshot,
            "source_path": source_path,
            "text_location": text_location,
            "excerpt": excerpt,
            "source_fingerprint": source["source_fingerprint"],
        }
        identity = (
            payload["target_kind"],
            payload["target_id"],
            payload["source_kind"],
            payload["source_stable_id"],
            payload["source_version_or_snapshot"],
            payload["source_path"],
            payload["text_location"],
            payload["excerpt"],
        )
        if identity in link_identities:
            raise StoryValidationError("evidence link is duplicated")
        link_identities.add(identity)
        canonical.append(CanonicalStoryLink(**payload, link_hash=sha256_text(canonical_json(payload))))
        linked_targets.add((target_kind, target_id))

    missing = expected_targets - linked_targets
    if missing:
        raise StoryValidationError("story targets require evidence")
    canonical.sort(
        key=lambda item: (
            item.target_kind,
            item.target_id,
            item.source_kind,
            item.source_stable_id,
            item.source_path,
            item.excerpt,
        )
    )
    return canonical


def derive_story_source_states(
    session: Session,
    version: InterviewStoryVersion,
) -> list[dict[str, str]]:
    """Derive (but never persist) current/changed/missing source state."""

    links = list(
        session.scalars(
            select(InterviewStoryVersionEvidenceLink)
            .where(InterviewStoryVersionEvidenceLink.story_version_id == version.id)
            .order_by(InterviewStoryVersionEvidenceLink.id.asc())
        )
    )
    states: list[dict[str, str]] = []
    for link in links:
        base = {
            "target_kind": link.target_kind,
            "target_id": link.target_id,
            "source_kind": link.source_kind,
            "source_stable_id": link.source_stable_id,
            "source_version_or_snapshot": link.source_version_or_snapshot,
            "source_path": link.source_path,
            "excerpt": link.excerpt,
        }
        if link.source_kind == "user_assertion":
            states.append({**base, "state": "frozen_user_assertion"})
            continue
        try:
            source = _revalidate_persisted_source(session, link)
        except StoryValidationError as exc:
            state = "missing" if "missing" in str(exc) else "changed"
        else:
            state = (
                "current"
                if (
                    source["source_fingerprint"] == link.source_fingerprint
                    and source["excerpt"] == link.excerpt
                    and source["source_version_or_snapshot"] == link.source_version_or_snapshot
                )
                else "changed"
            )
        states.append({**base, "state": state})
    return states


def story_request_fingerprint(
    *,
    target_story_id: int | None,
    expected_current_version_id: int | None,
    expected_story_revision: int | None,
    selections: list[dict[str, Any]],
    assertions: list[str],
) -> str:
    """Hash canonical user-selected input, never client-supplied snapshots."""

    for field, value in (
        ("target story id", target_story_id),
        ("expected current version id", expected_current_version_id),
        ("expected story revision", expected_story_revision),
    ):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            raise StoryValidationError(f"{field} is invalid")
    normalized_selections: list[dict[str, Any]] = []
    for item in selections:
        if not isinstance(item, Mapping):
            raise StoryValidationError("source selection must be an object")
        if set(item) != {"source_kind", "source_id", "path"}:
            raise StoryValidationError("source selection shape is invalid")
        normalized_selections.append(dict(item))
    if not all(isinstance(statement, str) for statement in assertions):
        raise StoryValidationError("assertion is invalid")
    payload = {
        "schema": _STORY_VERSION_SCHEMA,
        "target_story_id": target_story_id,
        "expected_current_version_id": expected_current_version_id,
        "expected_story_revision": expected_story_revision,
        "selections": sorted(
            normalized_selections,
            key=lambda item: (str(item["source_kind"]), str(item["source_id"]), str(item["path"])),
        ),
        "assertions": list(assertions),
    }
    return sha256_text(canonical_json(payload))


def _manual_request_fingerprint(
    *,
    target_story_id: int | None,
    content: Mapping[str, Any],
    evidence_links: list[dict[str, Any]],
    selections: list[dict[str, Any]],
    assertions: list[str],
    expected_current_version_id: int | None,
    expected_story_revision: int | None,
) -> str:
    """Bind an explicit manual save to the exact user-confirmed payload."""

    if not all(isinstance(item, Mapping) for item in evidence_links):
        raise StoryValidationError("evidence link must be an object")
    payload = {
        "operation": "manual_save",
        "target_story_id": target_story_id,
        "expected_current_version_id": expected_current_version_id,
        "expected_story_revision": expected_story_revision,
        "content": dict(content),
        "evidence_links": sorted(
            [dict(item) for item in evidence_links],
            key=canonical_json,
        ),
        "selections": _canonical_selections(selections),
        "assertions": list(assertions),
    }
    return sha256_text(canonical_json(payload))


def _story_datetime_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return aware.astimezone(timezone.utc).isoformat()


def _story_lifecycle_request_fingerprint(payload: Mapping[str, Any]) -> str:
    """Bind one owner-local lifecycle transition to its exact raw CAS and states."""

    return sha256_text(canonical_json(dict(payload)))


def _story_lifecycle_idempotency_key(
    *,
    story_id: int,
    expected_story_revision: int,
    desired_status: str,
) -> str:
    material = canonical_json(
        {
            "operation": "story_lifecycle_v1",
            "target_story_id": story_id,
            "expected_story_revision": expected_story_revision,
            "desired_status": desired_status,
        }
    )
    # The colon is deliberately outside the public idempotency-key alphabet.
    # This keeps internal audit identities disjoint from all client writers.
    return _STORY_LIFECYCLE_INTERNAL_IDEMPOTENCY_PREFIX + sha256_text(material)


def _story_lifecycle_legacy_idempotency_key(
    *,
    story_id: int,
    expected_story_revision: int,
    desired_status: str,
) -> str:
    """Return the pre-isolation identity for validating persisted audit rows."""

    material = canonical_json(
        {
            "operation": "story_lifecycle_v1",
            "target_story_id": story_id,
            "expected_story_revision": expected_story_revision,
            "desired_status": desired_status,
        }
    )
    return _STORY_LIFECYCLE_LEGACY_IDEMPOTENCY_PREFIX + sha256_text(material)


def _uses_reserved_story_idempotency_namespace(idempotency_key: str) -> bool:
    return idempotency_key.startswith(_STORY_LIFECYCLE_LEGACY_IDEMPOTENCY_PREFIX)


@dataclass(frozen=True)
class StoryProposalClaim:
    attempt_id: int
    input_snapshot: dict[str, Any]
    source_snapshot: StorySourceSnapshot
    source_fingerprint: str
    should_call_provider: bool
    pending: bool
    generation_revision: int
    provider_call_token: str
    attempt_status: str


@dataclass(frozen=True)
class StoryConfirmation:
    story_id: int
    version_id: int
    created: bool


@dataclass(frozen=True, slots=True)
class StoryConfirmationAdapterPlan:
    operation_id: str | None
    decision: Mapping[str, JSONValue] | None
    replay: StoryConfirmation | None
    force_replay_projection: bool = False
    terminal_failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class StoryWriteResult:
    story_id: int
    version_id: int
    story_revision: int
    outcome: str
    undo: Mapping[str, JSONValue]


@dataclass(frozen=True, slots=True)
class StoryUndoResultV1:
    story_id: int
    current_version_id: int
    story_revision: int
    status: str


@dataclass(frozen=True, slots=True)
class StoryProductActionProposalResult:
    operation_id: str
    action_call_id: str
    product_action_generation: int
    status: str
    proposal_created: bool
    confirmation_token: str | None
    terminal_result: Mapping[str, JSONValue] | None


_TRUSTED_STORY_DECISION_SEAL = object()


@dataclass(frozen=True, slots=True, init=False, repr=False)
class TrustedStoryDecision:
    attempt_id: int
    operation_id: str
    content: Mapping[str, Any]
    evidence_links: tuple[Mapping[str, Any], ...]
    expected_current_version_id: int | None
    expected_story_revision: int | None
    snapshot: StorySourceSnapshot
    assertions: tuple[str, ...]
    effective_payload_sha256: str

    def __init__(
        self,
        seal: object,
        *,
        attempt_id: int,
        operation_id: str,
        content: Mapping[str, Any],
        evidence_links: tuple[Mapping[str, Any], ...],
        expected_current_version_id: int | None,
        expected_story_revision: int | None,
        snapshot: StorySourceSnapshot,
        assertions: tuple[str, ...],
        effective_payload_sha256: str,
    ) -> None:
        if seal is not _TRUSTED_STORY_DECISION_SEAL:
            raise TypeError("Trusted Story decision is sealed")
        object.__setattr__(self, "attempt_id", attempt_id)
        object.__setattr__(self, "operation_id", operation_id)
        object.__setattr__(self, "content", dict(content))
        object.__setattr__(self, "evidence_links", tuple(dict(item) for item in evidence_links))
        object.__setattr__(self, "expected_current_version_id", expected_current_version_id)
        object.__setattr__(self, "expected_story_revision", expected_story_revision)
        object.__setattr__(self, "snapshot", snapshot)
        object.__setattr__(self, "assertions", assertions)
        object.__setattr__(self, "effective_payload_sha256", effective_payload_sha256)


def materialize_selected_sources(
    session: Session,
    selections: list[dict[str, Any]],
    assertions: list[str],
) -> StorySourceSnapshot:
    """Resolve only explicit phase-one source selections into frozen leaf evidence."""

    sources: list[dict[str, str]] = []
    selection_identities: set[tuple[str, int, str]] = set()
    for selection in selections:
        if not isinstance(selection, Mapping):
            raise StoryValidationError("source selection must be an object")
        kind = selection.get("source_kind")
        source_id = selection.get("source_id")
        path = selection.get("path")
        if kind not in _ALLOWED_SOURCE_KINDS:
            raise StoryValidationError("source kind is invalid")
        if isinstance(source_id, bool) or not isinstance(source_id, int) or source_id <= 0:
            raise StoryValidationError("source id is invalid")
        if not isinstance(path, str):
            raise StoryValidationError("source path is invalid")
        selection_identity = (kind, source_id, path)
        if selection_identity in selection_identities:
            raise StoryValidationError("source selection is duplicated")
        selection_identities.add(selection_identity)
        if kind == "resume_version":
            sources.append(_materialize_resume(session, source_id, path))
        elif kind == "interview_note":
            sources.append(_materialize_note(session, source_id, path))
        else:
            sources.append(_materialize_mock_turn(session, source_id, path))

    assertion_hashes: set[str] = set()
    for index, statement in enumerate(assertions, 1):
        if not isinstance(statement, str) or not statement.strip() or len(statement) > _MAX_ASSERTION_CHARS:
            raise StoryValidationError("assertion is invalid")
        statement_hash = sha256_text(statement)
        if statement_hash in assertion_hashes:
            raise StoryValidationError("assertion is duplicated")
        assertion_hashes.add(statement_hash)
        sources.append(
            {
                "source_kind": "user_assertion",
                "source_stable_id": f"assertion_{index:03d}",
                "source_version_or_snapshot": "pending_confirmation",
                "path": "/statement",
                "excerpt": statement,
                "source_fingerprint": sha256_text(statement),
            }
        )

    sources.sort(key=lambda item: (item["source_kind"], item["source_stable_id"], item["path"]))
    return StorySourceSnapshot(
        sources=sources,
        source_fingerprint=sha256_text(canonical_json(sources)),
    )


def _materialize_resume(session: Session, resume_id: int, path: str) -> dict[str, str]:
    pointer = _resume_content_pointer(path)
    resume = session.get(Resume, resume_id)
    if resume is None or resume.deleted_at is not None:
        raise StoryValidationError("resume source is missing")
    try:
        payload = json.loads(resume.content_json)
    except (TypeError, ValueError) as exc:
        raise StoryValidationError("resume source is invalid") from exc
    value = _resolve_json_pointer(payload, pointer)
    if not isinstance(value, str) or not value.strip():
        raise StoryValidationError("resume source path is invalid")
    return {
        "source_kind": "resume_version",
        "source_stable_id": str(resume.id),
        "source_version_or_snapshot": sha256_text(canonical_json(payload)),
        "path": path,
        "excerpt": value,
        "source_fingerprint": sha256_text(value),
    }


def _materialize_note(session: Session, note_id: int, path: str) -> dict[str, str]:
    field = _NOTE_FIELDS.get(path)
    note = session.get(InterviewNote, note_id)
    if field is None:
        raise StoryValidationError("interview note path is invalid")
    if note is None:
        raise StoryValidationError("interview note source is missing")
    value = getattr(note, field)
    if not isinstance(value, str) or not value.strip():
        raise StoryValidationError("interview note path is invalid")
    return {
        "source_kind": "interview_note",
        "source_stable_id": str(note.id),
        "source_version_or_snapshot": sha256_text(canonical_json({field: value})),
        "path": path,
        "excerpt": value,
        "source_fingerprint": sha256_text(value),
    }


def _materialize_mock_turn(session: Session, attempt_id: int, path: str) -> dict[str, str]:
    parts = path.split("/")
    if (
        len(parts) != 4
        or parts[:2] != ["", "turns"]
        or len(parts[2]) != 3
        or not _is_three_ascii_digits(parts[2])
    ):
        raise StoryValidationError("mock turn path is invalid")
    field = {"question": "question_text", "answer": "answer_text"}.get(parts[3])
    if field is None:
        raise StoryValidationError("mock turn path is invalid")
    attempt = session.get(MockInterviewAttempt, attempt_id)
    if (
        attempt is None
        or attempt.cancelled_at is not None
        or attempt.completed_at is None
        or attempt.attempt_status not in {"feedback_ready", "confirmed"}
    ):
        raise StoryValidationError("mock turn source is invalid")
    turn = session.scalar(
        select(MockInterviewTurn)
        .where(MockInterviewTurn.attempt_id == attempt_id)
        .where(MockInterviewTurn.turn_no == int(parts[2]))
    )
    if turn is None or turn.turn_status != "answered":
        raise StoryValidationError("mock turn source is invalid")
    value = getattr(turn, field)
    if not isinstance(value, str) or not value.strip():
        raise StoryValidationError("mock turn source is invalid")
    return {
        "source_kind": "mock_turn",
        "source_stable_id": f"{attempt.id}:{turn.turn_no:03d}",
        "source_version_or_snapshot": attempt.transcript_fingerprint,
        "path": path,
        "excerpt": value,
        "source_fingerprint": sha256_text(value),
    }


def _resolve_json_pointer(value: Any, pointer: str) -> Any:
    if not pointer.startswith("/"):
        raise StoryValidationError("resume source path is invalid")
    current = value
    for token in pointer[1:].split("/"):
        token = _decode_json_pointer_token(token)
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif (
            isinstance(current, list)
            and _is_canonical_array_index(token)
            and int(token) < len(current)
        ):
            current = current[int(token)]
        else:
            raise StoryValidationError("resume source path is invalid")
    return current


def _resume_content_pointer(path: str) -> str:
    prefix = "/content_json"
    if not path.startswith(f"{prefix}/"):
        raise StoryValidationError("resume source path is invalid")
    pointer = path[len(prefix) :]
    for token in pointer[1:].split("/"):
        _decode_json_pointer_token(token)
    return pointer


def _decode_json_pointer_token(token: str) -> str:
    decoded: list[str] = []
    index = 0
    while index < len(token):
        char = token[index]
        if char != "~":
            decoded.append(char)
            index += 1
            continue
        if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
            raise StoryValidationError("resume source path is invalid")
        decoded.append("~" if token[index + 1] == "0" else "/")
        index += 2
    return "".join(decoded)


def _is_canonical_array_index(value: str) -> bool:
    # Keep the lexical RFC 6901 rule narrow *and* bounded before callers use
    # int(value). Python rejects unbounded decimal conversions on modern
    # runtimes; a path must fail as invalid instead of leaking ValueError.
    return value == "0" or (
        bool(value)
        and len(value) <= 18
        and value[0] in "123456789"
        and all(char in "0123456789" for char in value[1:])
    )


def _is_three_ascii_digits(value: str) -> bool:
    return len(value) == 3 and all(char in "0123456789" for char in value)


def _escape_json_pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _resume_string_leaves(value: Any, pointer: str = "/content_json") -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(pointer, value)] if value.strip() else []
    if isinstance(value, dict):
        leaves: list[tuple[str, str]] = []
        for key in sorted(value):
            if isinstance(key, str):
                leaves.extend(_resume_string_leaves(value[key], f"{pointer}/{_escape_json_pointer_token(key)}"))
        return leaves
    if isinstance(value, list):
        leaves = []
        for index, item in enumerate(value):
            leaves.extend(_resume_string_leaves(item, f"{pointer}/{index}"))
        return leaves
    return []


def _source_preview(value: str) -> str:
    # Prefix only, preserving original code points and never adding an ellipsis:
    # it remains a valid contiguous excerpt if the user chooses it manually.
    return value[:240]


def _source_identity(source: Mapping[str, str]) -> tuple[str, str, str, str]:
    return (
        source["source_kind"],
        source["source_stable_id"],
        source["source_version_or_snapshot"],
        source["path"],
    )


def _evidence_targets(content: Mapping[str, Any]) -> set[tuple[str, str]]:
    title = content.get("title")
    if not isinstance(title, Mapping) or not isinstance(title.get("id"), str) or not isinstance(title.get("text"), str):
        raise StoryValidationError("canonical story title is invalid")
    targets: set[tuple[str, str]] = set()
    if title["text"].strip():
        targets.add(("title", title["id"]))
    collections = (
        ("blocks", "block"),
        ("capability_labels", "capability_label"),
        ("applicable_questions", "applicable_question"),
    )
    for field, target_kind in collections:
        values = content.get(field)
        if not isinstance(values, list):
            raise StoryValidationError("canonical story content is invalid")
        for item in values:
            if not isinstance(item, Mapping) or not isinstance(item.get("id"), str) or not isinstance(item.get("text"), str):
                raise StoryValidationError("canonical story content is invalid")
            if item["text"].strip():
                targets.add((target_kind, item["id"]))
    if not targets:
        raise StoryValidationError("story must contain an evidence target")
    return targets


def _target_fact_modes(content: Mapping[str, Any]) -> dict[tuple[str, str], str]:
    """Return the fact mode for every stable evidence target."""

    modes = {target: "evidence_backed" for target in _evidence_targets(content)}
    blocks = content.get("blocks")
    if isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, Mapping) and isinstance(block.get("id"), str):
                modes[("block", block["id"])] = str(block.get("fact_mode", "evidence_backed"))
    return modes


def _revalidate_persisted_source(
    session: Session,
    link: InterviewStoryVersionEvidenceLink,
) -> dict[str, str]:
    if link.source_kind == "resume_version":
        try:
            resume_id = int(link.source_stable_id)
        except ValueError as exc:
            raise StoryValidationError("resume source is missing") from exc
        return _materialize_resume(session, resume_id, link.source_path)
    if link.source_kind == "interview_note":
        try:
            note_id = int(link.source_stable_id)
        except ValueError as exc:
            raise StoryValidationError("interview note source is missing") from exc
        return _materialize_note(session, note_id, link.source_path)
    if link.source_kind == "mock_turn":
        attempt_id, separator, _turn_no = link.source_stable_id.partition(":")
        if not separator:
            raise StoryValidationError("mock turn source is missing")
        try:
            return _materialize_mock_turn(session, int(attempt_id), link.source_path)
        except ValueError as exc:
            raise StoryValidationError("mock turn source is missing") from exc
    raise StoryValidationError("story source is missing")


class InterviewStoriesRepository:
    """Own immutable Story Versions and their explicitly selected evidence."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        action_issuer: InterviewStoryActionIssuer | None = None,
        proposal_repository: ProductActionProposalRepository | None = None,
        proof_registry: ProductActionProofRegistryV1 | None = None,
    ):
        configured = (
            action_issuer is not None,
            proposal_repository is not None,
            proof_registry is not None,
        )
        if any(configured) and not all(configured):
            raise TypeError("Story Product Action composition is incomplete")
        self._session_factory = session_factory
        self._action_issuer = action_issuer
        self._proposal_repository = proposal_repository
        self._proof_registry = proof_registry

    def list_stories(self, *, status: str = "active", query: str = "") -> list[dict[str, Any]]:
        if status not in {"active", "archived", "all"}:
            raise StoryValidationError("story status is invalid")
        with self._session_factory() as session:
            statement = select(InterviewStory).order_by(InterviewStory.updated_at.desc(), InterviewStory.id.desc())
            if status != "all":
                statement = statement.where(InterviewStory.status == status)
            rows = list(session.scalars(statement))
            normalized_query = query.strip().casefold()
            if normalized_query:
                rows = [row for row in rows if normalized_query in row.title.casefold()]
            return [self._story_summary(session, row) for row in rows]

    def list_source_candidates(self, *, review_note_id: int | None = None) -> dict[str, Any]:
        """Return bounded, read-only candidates for the explicit Story picker.

        The response is deliberately a selection aid, not a provider snapshot:
        it contains only canonical identities plus a bounded literal prefix.  The
        selected leaves are always materialized again in the short claim
        transaction before they can reach a model or a Version.
        """

        if review_note_id is not None:
            _require_positive_int("review note id", review_note_id)
        with self._session_factory() as session:
            notes_statement = select(InterviewNote).order_by(InterviewNote.id.desc())
            if review_note_id is not None:
                notes_statement = notes_statement.where(InterviewNote.id == review_note_id)
            notes = [
                {
                    "id": note.id,
                    "label": " · ".join(part for part in (note.company, note.position) if part),
                    "leaves": [
                        {"path": path, "preview": _source_preview(value)}
                        for path, field in _NOTE_FIELDS.items()
                        if isinstance((value := getattr(note, field)), str) and value.strip()
                    ],
                }
                for note in session.scalars(notes_statement)
            ]
            # A saved-review handoff is intentionally narrow: it must not turn
            # into a picker for unrelated candidate sources.
            if review_note_id is not None:
                return {"resumes": [], "interview_notes": notes, "mock_turns": []}

            resumes = []
            for resume in session.scalars(
                select(Resume).where(Resume.deleted_at.is_(None)).order_by(Resume.id.desc())
            ):
                try:
                    payload = json.loads(resume.content_json)
                except (TypeError, ValueError):
                    continue
                leaves = [
                    {"path": path, "preview": _source_preview(value)}
                    for path, value in _resume_string_leaves(payload)
                    if path.startswith("/content_json/")
                ]
                if leaves:
                    resumes.append({"id": resume.id, "label": resume.title or resume.name or f"Resume {resume.id}", "leaves": leaves})

            mock_turns = []
            attempts = session.scalars(
                select(MockInterviewAttempt)
                .where(MockInterviewAttempt.cancelled_at.is_(None))
                .where(MockInterviewAttempt.completed_at.is_not(None))
                .where(MockInterviewAttempt.attempt_status.in_({"feedback_ready", "confirmed"}))
                .order_by(MockInterviewAttempt.id.desc())
            )
            for attempt in attempts:
                for turn in session.scalars(
                    select(MockInterviewTurn)
                    .where(MockInterviewTurn.attempt_id == attempt.id)
                    .where(MockInterviewTurn.turn_status == "answered")
                    .order_by(MockInterviewTurn.turn_no.asc())
                ):
                    leaves = []
                    for name, value in (("question", turn.question_text), ("answer", turn.answer_text)):
                        if isinstance(value, str) and value.strip():
                            leaves.append({"path": f"/turns/{turn.turn_no:03d}/{name}", "preview": _source_preview(value)})
                    if leaves:
                        mock_turns.append({
                            "attempt_id": attempt.id,
                            "turn_no": turn.turn_no,
                            "label": f"模拟面试 #{attempt.id} · 第 {turn.turn_no} 题",
                            "leaves": leaves,
                        })
            return {"resumes": resumes, "interview_notes": notes, "mock_turns": mock_turns}

    def get_story(self, story_id: int) -> dict[str, Any] | None:
        with self._session_factory() as session:
            story = session.get(InterviewStory, story_id)
            return self._story_payload(session, story) if story is not None else None

    def get_version(self, story_id: int, version_id: int) -> dict[str, Any] | None:
        with self._session_factory() as session:
            version = session.get(InterviewStoryVersion, version_id)
            if version is None or version.story_id != story_id:
                return None
            return self._version_payload(session, version)

    def list_versions(self, story_id: int) -> list[dict[str, Any]] | None:
        with self._session_factory() as session:
            if session.get(InterviewStory, story_id) is None:
                return None
            versions = list(
                session.scalars(
                    select(InterviewStoryVersion)
                    .where(InterviewStoryVersion.story_id == story_id)
                    .order_by(InterviewStoryVersion.version_number.desc())
                )
            )
            return [
                {
                    "id": version.id,
                    "version_number": version.version_number,
                    "origin_kind": version.origin_kind,
                    "confirmed_at": version.confirmed_at.isoformat() if version.confirmed_at else None,
                    "source_fingerprint": version.source_fingerprint,
                }
                for version in versions
            ]

    def create_manual_story(
        self,
        *,
        content: Mapping[str, Any],
        evidence_links: list[dict[str, Any]],
        selections: list[dict[str, Any]],
        assertions: list[str],
        expected_current_version_id: int | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        _require_exact_null("new story current version id", expected_current_version_id)
        request_fingerprint = _manual_request_fingerprint(
            target_story_id=None,
            content=content,
            evidence_links=evidence_links,
            selections=selections,
            assertions=assertions,
            expected_current_version_id=expected_current_version_id,
            expected_story_revision=None,
        )
        with self._session_factory() as session:
            try:
                _begin_immediate(session)
                replay = self._replay_manual_save(
                    session,
                    idempotency_key,
                    request_fingerprint,
                    raw_target_story_id=None,
                    raw_expected_current_version_id=None,
                    raw_expected_story_revision=None,
                )
                if replay is not None:
                    session.commit()
                    return replay
                canonical = canonical_story_content(content)
                snapshot = materialize_selected_sources(session, selections, assertions)
                canonical_links = validate_story_evidence_links(canonical, evidence_links, snapshot)
                story = InterviewStory(title=canonical["title"]["text"], status="active", story_revision=1)
                session.add(story)
                session.flush()
                version = self._insert_version(
                    session,
                    story=story,
                    version_number=1,
                    canonical=canonical,
                    snapshot=snapshot,
                    canonical_links=canonical_links,
                    assertions=assertions,
                    origin_kind="manual",
                )
                story.current_version_id = version.id
                self._record_manual_save(
                    session,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    raw_target_story_id=None,
                    raw_expected_current_version_id=None,
                    raw_expected_story_revision=None,
                    story=story,
                    version=version,
                    snapshot=snapshot,
                )
                session.commit()
                return self._story_payload(session, story)
            except Exception:
                session.rollback()
                raise

    def create_manual_version(
        self,
        *,
        story_id: int,
        content: Mapping[str, Any],
        evidence_links: list[dict[str, Any]],
        selections: list[dict[str, Any]],
        assertions: list[str],
        expected_current_version_id: int | None,
        expected_story_revision: int | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        _require_positive_int("expected current version id", expected_current_version_id)
        _require_positive_int("expected story revision", expected_story_revision)
        request_fingerprint = _manual_request_fingerprint(
            target_story_id=story_id,
            content=content,
            evidence_links=evidence_links,
            selections=selections,
            assertions=assertions,
            expected_current_version_id=expected_current_version_id,
            expected_story_revision=expected_story_revision,
        )
        with self._session_factory() as session:
            try:
                _begin_immediate(session)
                replay = self._replay_manual_save(
                    session,
                    idempotency_key,
                    request_fingerprint,
                    raw_target_story_id=story_id,
                    raw_expected_current_version_id=expected_current_version_id,
                    raw_expected_story_revision=expected_story_revision,
                )
                if replay is not None:
                    session.commit()
                    return replay
                story = self._require_active_story(session, story_id)
                self._check_story_cas(story, expected_current_version_id, expected_story_revision)
                canonical = canonical_story_content(content)
                snapshot = materialize_selected_sources(session, selections, assertions)
                canonical_links = validate_story_evidence_links(canonical, evidence_links, snapshot)
                version_number = int(
                    session.scalar(
                        select(InterviewStoryVersion.version_number)
                        .where(InterviewStoryVersion.story_id == story.id)
                        .order_by(InterviewStoryVersion.version_number.desc())
                    )
                    or 0
                ) + 1
                version = self._insert_version(
                    session,
                    story=story,
                    version_number=version_number,
                    canonical=canonical,
                    snapshot=snapshot,
                    canonical_links=canonical_links,
                    assertions=assertions,
                    origin_kind="manual",
                )
                story.current_version_id = version.id
                story.story_revision += 1
                story.title = canonical["title"]["text"]
                story.updated_at = datetime.now(timezone.utc)
                self._invalidate_active_target_attempts(session, story.id)
                self._record_manual_save(
                    session,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    raw_target_story_id=story_id,
                    raw_expected_current_version_id=expected_current_version_id,
                    raw_expected_story_revision=expected_story_revision,
                    story=story,
                    version=version,
                    snapshot=snapshot,
                )
                session.commit()
                return self._story_payload(session, story)
            except Exception:
                session.rollback()
                raise

    def archive(self, *, story_id: int, expected_story_revision: int | None) -> dict[str, Any]:
        return self._change_lifecycle(
            story_id=story_id,
            expected_story_revision=expected_story_revision,
            desired_status="archived",
        )

    def restore(self, *, story_id: int, expected_story_revision: int | None) -> dict[str, Any]:
        return self._change_lifecycle(
            story_id=story_id,
            expected_story_revision=expected_story_revision,
            desired_status="active",
        )

    def claim_proposal(
        self,
        *,
        target_story_id: int | None,
        expected_current_version_id: int | None,
        expected_story_revision: int | None,
        selections: list[dict[str, Any]],
        assertions: list[str],
        idempotency_key: str,
        entrypoint: str,
        entry_context: dict[str, Any] | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> StoryProposalClaim:
        """Claim one fenced Provider attempt from explicitly selected source leaves."""

        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise StoryValidationError("idempotency key is invalid")
        if entrypoint not in {"ui", "pilot"}:
            raise StoryValidationError("story entrypoint is invalid")
        now = _now(now_factory)
        request_fingerprint = story_request_fingerprint(
            target_story_id=target_story_id,
            expected_current_version_id=expected_current_version_id,
            expected_story_revision=expected_story_revision,
            selections=selections,
            assertions=assertions,
        )
        with self._session_factory() as session:
            try:
                _begin_immediate(session)
                existing = session.scalar(
                    select(InterviewStoryProposalAttempt).where(
                        InterviewStoryProposalAttempt.idempotency_key == idempotency_key
                    )
                )
                if existing is not None:
                    payload = _attempt_input_payload(existing)
                    if payload.get("request_fingerprint") != request_fingerprint:
                        raise StoryIdempotencyConflictError("story idempotency input changed")
                    return self._replay_or_takeover_attempt(
                        session=session,
                        attempt=existing,
                        payload=payload,
                        now=now,
                    )
                if _uses_reserved_story_idempotency_namespace(idempotency_key):
                    raise StoryValidationError("idempotency key is reserved")
                self._validate_target_story_for_claim(
                    session,
                    target_story_id=target_story_id,
                    expected_current_version_id=expected_current_version_id,
                    expected_story_revision=expected_story_revision,
                )
                snapshot = materialize_selected_sources(session, selections, assertions)
                token = uuid4().hex
                input_snapshot = {
                    "schema": _STORY_VERSION_SCHEMA,
                    "request_fingerprint": request_fingerprint,
                    "target_story_id": target_story_id,
                    "expected_current_version_id": expected_current_version_id,
                    "expected_story_revision": expected_story_revision,
                    "selections": _canonical_selections(selections),
                    "assertions": list(assertions),
                    "sources": snapshot.sources,
                }
                attempt = InterviewStoryProposalAttempt(
                    target_story_id=target_story_id,
                    idempotency_key=idempotency_key,
                    entrypoint=entrypoint,
                    entry_context_json=canonical_json(entry_context or {}),
                    attempt_status="generating",
                    generation_revision=1,
                    provider_call_token=token,
                    provider_lease_until=_as_naive_utc(now + timedelta(seconds=_STORY_LEASE_SECONDS)),
                    input_snapshot_json=canonical_json(input_snapshot),
                    source_fingerprint=snapshot.source_fingerprint,
                )
                session.add(attempt)
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    existing = session.scalar(
                        select(InterviewStoryProposalAttempt).where(
                            InterviewStoryProposalAttempt.idempotency_key == idempotency_key
                        )
                    )
                    if existing is None:
                        raise
                    payload = _attempt_input_payload(existing)
                    if payload.get("request_fingerprint") != request_fingerprint:
                        raise StoryIdempotencyConflictError("story idempotency input changed")
                    return self._replay_or_takeover_attempt(
                        session=session,
                        attempt=existing,
                        payload=payload,
                        now=now,
                    )
                return StoryProposalClaim(
                    attempt_id=attempt.id,
                    input_snapshot=input_snapshot,
                    source_snapshot=snapshot,
                    source_fingerprint=snapshot.source_fingerprint,
                    should_call_provider=True,
                    pending=False,
                    generation_revision=attempt.generation_revision,
                    provider_call_token=token,
                    attempt_status=attempt.attempt_status,
                )
            except Exception:
                session.rollback()
                raise

    def complete_proposal(
        self,
        *,
        attempt_id: int,
        generation_revision: int,
        provider_call_token: str,
        proposal: dict[str, Any],
        repair_count: int = 0,
    ) -> bool:
        """Final fencing CAS: token/revision own the result, not lease freshness."""

        _require_bounded_repair_count(repair_count)
        if self._action_issuer is not None:
            return self._complete_proposal_with_product_action(
                attempt_id=attempt_id,
                generation_revision=generation_revision,
                provider_call_token=provider_call_token,
                proposal=proposal,
                repair_count=repair_count,
            )
        with self._session_factory() as session:
            try:
                _begin_immediate(session)
                attempt = session.get(InterviewStoryProposalAttempt, attempt_id)
                if not _owned_attempt(attempt, generation_revision, provider_call_token):
                    session.commit()
                    return False
                if attempt is None:
                    session.commit()
                    return False
                payload = _attempt_input_payload(attempt)
                snapshot = _snapshot_from_attempt(payload, attempt.source_fingerprint)
                selections = payload.get("selections")
                assertions = payload.get("assertions")
                if not isinstance(selections, list) or not isinstance(assertions, list):
                    self._invalidate_attempt(attempt, category="source_changed")
                    session.commit()
                    return False
                try:
                    self._validate_target_story_for_claim(
                        session,
                        target_story_id=attempt.target_story_id,
                        expected_current_version_id=payload.get("expected_current_version_id"),
                        expected_story_revision=payload.get("expected_story_revision"),
                    )
                except (StoryConflictError, StoryNotFoundError) as exc:
                    self._invalidate_attempt(attempt, category="source_changed")
                    session.commit()
                    raise StorySourceConflictError("story source changed") from exc
                try:
                    current = materialize_selected_sources(session, selections, assertions)
                except StoryValidationError as exc:
                    self._invalidate_attempt(attempt, category="source_changed")
                    session.commit()
                    raise StorySourceConflictError("story source changed") from exc
                if current.source_fingerprint != attempt.source_fingerprint:
                    self._invalidate_attempt(attempt, category="source_changed")
                    session.commit()
                    return False
                # Re-run strict validation server side.  The only alternate form
                # is the already-normalized server result returned by the local
                # generator; clients never reach this repository method directly.
                if proposal.get("proposal_status") == "normal":
                    raw_content = proposal.get("content")
                    raw_links = proposal.get("evidence_links")
                    if not isinstance(raw_content, dict) or not isinstance(raw_links, list):
                        raise StoryValidationError("story proposal is invalid")
                    content = canonical_story_content(_manual_content_from_canonical(raw_content))
                    checked = {
                        "proposal_status": "normal",
                        "content": content,
                        "evidence_links": [
                            item.as_dict()
                            for item in validate_story_evidence_links(
                                content,
                                [_client_link_fields(item) for item in raw_links],
                                snapshot,
                            )
                        ],
                    }
                elif proposal.get("proposal_status") == "safe_empty":
                    from offerpilot.ai.interview_stories import safe_empty_interview_story_proposal

                    if proposal != safe_empty_interview_story_proposal():
                        raise StoryValidationError("story proposal is invalid")
                    checked = proposal
                else:
                    from offerpilot.ai.interview_stories import validate_interview_story_proposal

                    checked = validate_interview_story_proposal(proposal, snapshot)
                status = "safe_empty" if checked["proposal_status"] == "safe_empty" else "ready"
                attempt.attempt_status = status
                attempt.proposal_json = canonical_json(checked)
                attempt.proposal_hash = sha256_text(attempt.proposal_json)
                attempt.repair_count = max(attempt.repair_count, repair_count)
                attempt.failure_category = ""
                attempt.provider_call_token = ""
                attempt.provider_lease_until = None
                session.commit()
                return True
            except StoryValidationError:
                session.rollback()
                raise

    @staticmethod
    def _checked_provider_proposal(
        proposal: dict[str, Any],
        snapshot: StorySourceSnapshot,
    ) -> dict[str, Any]:
        if proposal.get("proposal_status") == "normal":
            raw_content = proposal.get("content")
            raw_links = proposal.get("evidence_links")
            if not isinstance(raw_content, dict) or not isinstance(raw_links, list):
                raise StoryValidationError("story proposal is invalid")
            content = canonical_story_content(_manual_content_from_canonical(raw_content))
            return {
                "proposal_status": "normal",
                "content": content,
                "evidence_links": [
                    item.as_dict()
                    for item in validate_story_evidence_links(
                        content,
                        [_client_link_fields(item) for item in raw_links],
                        snapshot,
                    )
                ],
            }
        if proposal.get("proposal_status") == "safe_empty":
            from offerpilot.ai.interview_stories import safe_empty_interview_story_proposal

            if proposal != safe_empty_interview_story_proposal():
                raise StoryValidationError("story proposal is invalid")
            return proposal
        from offerpilot.ai.interview_stories import validate_interview_story_proposal

        return validate_interview_story_proposal(proposal, snapshot)

    @staticmethod
    def _story_route_payload(
        attempt: InterviewStoryProposalAttempt,
        *,
        proposal_hash: str,
        product_action_generation: int,
    ) -> bytes:
        payload: dict[str, JSONValue] = {
            "attempt_id": attempt.id,
            "generation_revision": attempt.generation_revision,
            "proposal_hash": (
                proposal_hash
                if proposal_hash.startswith("sha256:")
                else "sha256:" + proposal_hash
            ),
            "source_fingerprint": (
                attempt.source_fingerprint
                if attempt.source_fingerprint.startswith("sha256:")
                else "sha256:" + attempt.source_fingerprint
            ),
            "target_story_id": attempt.target_story_id,
            "expected_current_version_id": None,
            "expected_story_revision": None,
            "product_action_generation": product_action_generation,
        }
        input_payload = _attempt_input_payload(attempt)
        payload["expected_current_version_id"] = input_payload.get(
            "expected_current_version_id"
        )
        payload["expected_story_revision"] = input_payload.get(
            "expected_story_revision"
        )
        return canonical_product_action_json(payload).encode("utf-8")

    def _complete_proposal_with_product_action(
        self,
        *,
        attempt_id: int,
        generation_revision: int,
        provider_call_token: str,
        proposal: dict[str, Any],
        repair_count: int,
    ) -> bool:
        issuer = self._action_issuer
        product_actions = self._proposal_repository
        registry = self._proof_registry
        if issuer is None or product_actions is None or registry is None:
            raise ProductActionIntegrityError("story_product_action_composition")
        with self._session_factory() as read_session:
            attempt = read_session.get(InterviewStoryProposalAttempt, attempt_id)
            if not _owned_attempt(attempt, generation_revision, provider_call_token):
                read_session.rollback()
                return False
            exact_attempt = cast(InterviewStoryProposalAttempt, attempt)
            payload = _attempt_input_payload(exact_attempt)
            frozen_snapshot = _snapshot_from_attempt(
                payload,
                exact_attempt.source_fingerprint,
            )
            checked = self._checked_provider_proposal(proposal, frozen_snapshot)
            proposal_json = canonical_json(checked)
            proposal_hash = sha256_text(proposal_json)
            if checked["proposal_status"] == "safe_empty":
                read_session.rollback()
                return self._complete_safe_empty(
                    attempt_id=attempt_id,
                    generation_revision=generation_revision,
                    provider_call_token=provider_call_token,
                    proposal_json=proposal_json,
                    proposal_hash=proposal_hash,
                    repair_count=repair_count,
                )
            route_raw = self._story_route_payload(
                exact_attempt,
                proposal_hash=proposal_hash,
                product_action_generation=1,
            )
            read_session.rollback()
        prepared = issuer.prepare(route_payload_raw=route_raw)
        try:
            return self._publish_ready_attempt(
                prepared=prepared,
                attempt_id=attempt_id,
                generation_revision=generation_revision,
                provider_call_token=provider_call_token,
                proposal_json=proposal_json,
                proposal_hash=proposal_hash,
                repair_count=repair_count,
                allow_all_absent_replay=True,
            )
        except BaseException:
            try:
                registry.revoke(prepared.route_proof)
            except ValueError:
                pass
            raise

    def _publish_ready_attempt(
        self,
        *,
        prepared: PreparedProductActionProposalV1,
        attempt_id: int,
        generation_revision: int,
        provider_call_token: str,
        proposal_json: str,
        proposal_hash: str,
        repair_count: int,
        allow_all_absent_replay: bool,
    ) -> bool:
        product_actions = self._proposal_repository
        if product_actions is None:
            raise ProductActionIntegrityError("story_product_action_composition")
        publication: ProductActionPublicationV1 | None = None
        with self._session_factory() as session:
            try:
                uow = product_actions.begin_publication_uow(session)
                attempt = session.get(InterviewStoryProposalAttempt, attempt_id)
                if not _owned_attempt(attempt, generation_revision, provider_call_token):
                    session.rollback()
                    return False
                exact_attempt = cast(InterviewStoryProposalAttempt, attempt)
                if (
                    exact_attempt.product_action_generation != 0
                    or exact_attempt.product_action_operation_id is not None
                ):
                    raise ProductActionIntegrityError("story_ready_publication_pointer")
                payload = _attempt_input_payload(exact_attempt)
                selections = payload.get("selections")
                assertions = payload.get("assertions")
                if not isinstance(selections, list) or not isinstance(assertions, list):
                    raise StorySourceConflictError("story source changed")
                try:
                    self._validate_target_story_for_claim(
                        session,
                        target_story_id=exact_attempt.target_story_id,
                        expected_current_version_id=cast(
                            int | None, payload.get("expected_current_version_id")
                        ),
                        expected_story_revision=cast(
                            int | None, payload.get("expected_story_revision")
                        ),
                    )
                    current = materialize_selected_sources(session, selections, assertions)
                except StoryValidationError as exc:
                    self._invalidate_attempt(exact_attempt, category="source_changed")
                    session.commit()
                    raise StorySourceConflictError("story source changed") from exc
                if current.source_fingerprint != exact_attempt.source_fingerprint:
                    self._invalidate_attempt(exact_attempt, category="source_changed")
                    session.commit()
                    raise StorySourceConflictError("story source changed")
                exact_attempt.attempt_status = "ready"
                exact_attempt.proposal_json = proposal_json
                exact_attempt.proposal_hash = proposal_hash
                exact_attempt.repair_count = max(exact_attempt.repair_count, repair_count)
                exact_attempt.failure_category = ""
                exact_attempt.provider_call_token = ""
                exact_attempt.provider_lease_until = None
                publication = product_actions.publish_bundle_in_session(
                    session,
                    uow,
                    prepared,
                )
                if publication.classification != "exact_proposed" or not publication.created:
                    raise ProductActionIntegrityError("story_ready_publication_bundle")
                exact_attempt.product_action_operation_id = publication.operation_id
                exact_attempt.product_action_generation = 1
                session.flush()
                self._require_ready_publication_attempt(
                    session,
                    attempt_id=attempt_id,
                    operation_id=publication.operation_id,
                    proposal_hash=proposal_hash,
                    product_action_generation=1,
                    allow_terminal=False,
                    bundle=publication.bundle,
                )
                try:
                    session.commit()
                except DBAPIError:
                    try:
                        session.rollback()
                    except BaseException:
                        pass
                    return self._reconcile_ready_publication(
                        prepared=prepared,
                        attempt_id=attempt_id,
                        generation_revision=generation_revision,
                        provider_call_token=provider_call_token,
                        proposal_json=proposal_json,
                        proposal_hash=proposal_hash,
                        repair_count=repair_count,
                        replay_grant=publication.replay_grant,
                        allow_all_absent_replay=allow_all_absent_replay,
                    )
                return True
            except BaseException:
                try:
                    session.rollback()
                except BaseException:
                    pass
                raise

    def _reconcile_ready_publication(
        self,
        *,
        prepared: PreparedProductActionProposalV1,
        attempt_id: int,
        generation_revision: int,
        provider_call_token: str,
        proposal_json: str,
        proposal_hash: str,
        repair_count: int,
        replay_grant: ProductActionPublicationReplayV1 | None,
        allow_all_absent_replay: bool,
    ) -> bool:
        product_actions = self._proposal_repository
        registry = self._proof_registry
        if product_actions is None or registry is None:
            raise ProductActionIntegrityError("story_product_action_composition")
        with self._session_factory() as session:
            try:
                uow = product_actions.begin_publication_uow(session)
                publication = product_actions.reconcile_publication_in_session(
                    session,
                    uow,
                    prepared,
                )
                if publication.classification in {
                    "exact_proposed",
                    "exact_terminal",
                }:
                    self._require_ready_publication_attempt(
                        session,
                        attempt_id=attempt_id,
                        operation_id=publication.operation_id,
                        proposal_hash=proposal_hash,
                        product_action_generation=1,
                        allow_terminal=True,
                        bundle=publication.bundle,
                    )
                session.rollback()
            except (DBAPIError, ProductActionIntegrityError) as exc:
                try:
                    session.rollback()
                except BaseException:
                    pass
                self._fence_ready_publication_unknown(
                    attempt_id=attempt_id,
                    generation_revision=generation_revision,
                    provider_call_token=provider_call_token,
                )
                if (
                    isinstance(exc, ProductActionIntegrityError)
                    and exc.code != "product_action_bundle_unreadable"
                ):
                    raise
                raise ProductActionCoordinatorError(
                    "operation_result_unknown",
                    status_code=503,
                    retryable=True,
                ) from exc
        if publication.classification in {"exact_proposed", "exact_terminal"}:
            return True
        if publication.classification == "unreadable":
            self._fence_ready_publication_unknown(
                attempt_id=attempt_id,
                generation_revision=generation_revision,
                provider_call_token=provider_call_token,
            )
            raise ProductActionCoordinatorError(
                "operation_result_unknown",
                status_code=503,
                retryable=True,
            )
        if not allow_all_absent_replay or replay_grant is None:
            self._fence_ready_publication_unknown(
                attempt_id=attempt_id,
                generation_revision=generation_revision,
                provider_call_token=provider_call_token,
            )
            raise ProductActionCoordinatorError(
                "operation_result_unknown",
                status_code=503,
                retryable=True,
            )
        with self._session_factory() as session:
            try:
                uow = product_actions.begin_publication_uow(session)
                fresh = product_actions.refresh_all_absent_from_grant_in_session(
                    session,
                    uow,
                    replay_grant,
                )
                session.rollback()
            except ProductActionContractError as exc:
                try:
                    session.rollback()
                except BaseException:
                    pass
                if exc.code == "publication_replay_not_all_absent":
                    return self._reconcile_ready_publication(
                        prepared=prepared,
                        attempt_id=attempt_id,
                        generation_revision=generation_revision,
                        provider_call_token=provider_call_token,
                        proposal_json=proposal_json,
                        proposal_hash=proposal_hash,
                        repair_count=repair_count,
                        replay_grant=None,
                        allow_all_absent_replay=False,
                    )
                raise
            except BaseException:
                try:
                    session.rollback()
                except BaseException:
                    pass
                raise
        try:
            return self._publish_ready_attempt(
                prepared=fresh,
                attempt_id=attempt_id,
                generation_revision=generation_revision,
                provider_call_token=provider_call_token,
                proposal_json=proposal_json,
                proposal_hash=proposal_hash,
                repair_count=repair_count,
                allow_all_absent_replay=False,
            )
        finally:
            try:
                registry.revoke(fresh.route_proof)
            except ValueError:
                pass

    def _fence_ready_publication_unknown(
        self,
        *,
        attempt_id: int,
        generation_revision: int,
        provider_call_token: str,
    ) -> None:
        """Persist a restart-stable fence after an unresolvable publication result."""

        with self._session_factory() as session:
            try:
                result = session.execute(
                    update(InterviewStoryProposalAttempt)
                    .where(InterviewStoryProposalAttempt.id == attempt_id)
                    .where(
                        InterviewStoryProposalAttempt.generation_revision
                        == generation_revision
                    )
                    .where(
                        InterviewStoryProposalAttempt.attempt_status
                        == "generating"
                    )
                    .where(
                        InterviewStoryProposalAttempt.provider_call_token
                        == provider_call_token
                    )
                    .where(
                        InterviewStoryProposalAttempt.product_action_generation == 0
                    )
                    .where(
                        InterviewStoryProposalAttempt.product_action_operation_id.is_(
                            None
                        )
                    )
                    .values(
                        attempt_status="provider_unknown",
                        provider_call_token="",
                        provider_lease_until=None,
                        failure_category="product_action_publication_unknown",
                    )
                )
                session.commit()
                if int(getattr(result, "rowcount", 0) or 0) not in {0, 1}:
                    raise ProductActionIntegrityError(
                        "story_ready_publication_fence"
                    )
            except BaseException:
                try:
                    session.rollback()
                except BaseException:
                    pass
                raise

    @staticmethod
    def _require_ready_publication_attempt(
        session: Session,
        *,
        attempt_id: int,
        operation_id: str,
        proposal_hash: str,
        product_action_generation: int,
        allow_terminal: bool,
        bundle: ProductActionBundleV1 | None,
    ) -> InterviewStoryProposalAttempt:
        attempt = session.get(InterviewStoryProposalAttempt, attempt_id)
        if (
            attempt is None
            or attempt.product_action_operation_id != operation_id
            or attempt.product_action_generation != product_action_generation
            or attempt.proposal_hash != proposal_hash
            or bundle is None
            or bundle.operation.id != operation_id
            or bundle.operation.tool_name != "confirm_interview_story"
        ):
            raise ProductActionIntegrityError("story_ready_publication_attempt")
        status = bundle.operation.status
        terminal_projection = (
            ProductActionCoordinator.verify_terminal_projection(bundle)
            if allow_terminal and bundle.classification == "exact_terminal"
            else None
        )
        if status == "proposed":
            exact = (
                bundle.classification == "exact_proposed"
                and (
                    attempt.attempt_status == "ready"
                    or (
                        attempt.attempt_status == "invalidated"
                        and attempt.failure_category == "source_changed"
                    )
                )
            )
        elif not allow_terminal or bundle.classification != "exact_terminal":
            exact = False
        elif status == "rejected":
            exact = attempt.attempt_status == "ready" or (
                attempt.attempt_status == "invalidated"
                and attempt.failure_category == "source_changed"
            )
        elif status == "failed":
            exact = (
                attempt.attempt_status == "ready"
                and bundle.operation.failure_code
                == "product_action_story_write_conflict"
            )
        elif status == "committed":
            result = terminal_projection.result if terminal_projection else {}
            exact = (
                attempt.attempt_status == "confirmed"
                and attempt.confirmed_story_id == result.get("story_id")
                and attempt.confirmed_story_version_id
                == result.get("story_version_id")
                and attempt.confirmation_payload_hash.startswith("sha256:")
            )
        else:
            exact = False
        if not exact:
            raise ProductActionIntegrityError("story_ready_publication_attempt")
        return attempt

    def _complete_safe_empty(
        self,
        *,
        attempt_id: int,
        generation_revision: int,
        provider_call_token: str,
        proposal_json: str,
        proposal_hash: str,
        repair_count: int,
    ) -> bool:
        with self._session_factory() as session:
            try:
                _begin_immediate(session)
                attempt = session.get(InterviewStoryProposalAttempt, attempt_id)
                if not _owned_attempt(attempt, generation_revision, provider_call_token):
                    session.commit()
                    return False
                exact = cast(InterviewStoryProposalAttempt, attempt)
                exact.attempt_status = "safe_empty"
                exact.proposal_json = proposal_json
                exact.proposal_hash = proposal_hash
                exact.repair_count = max(exact.repair_count, repair_count)
                exact.failure_category = ""
                exact.provider_call_token = ""
                exact.provider_lease_until = None
                session.commit()
                return True
            except BaseException:
                session.rollback()
                raise
            except Exception:
                session.rollback()
                raise

    def mark_provider_unknown(
        self,
        *,
        attempt_id: int,
        generation_revision: int,
        provider_call_token: str,
        category: str,
        repair_count: int = 0,
    ) -> bool:
        _require_bounded_repair_count(repair_count)
        with self._session_factory() as session:
            result = session.execute(
                update(InterviewStoryProposalAttempt)
                .where(InterviewStoryProposalAttempt.id == attempt_id)
                .where(InterviewStoryProposalAttempt.attempt_status == "generating")
                .where(InterviewStoryProposalAttempt.generation_revision == generation_revision)
                .where(InterviewStoryProposalAttempt.provider_call_token == provider_call_token)
                .values(
                    attempt_status="provider_unknown",
                    provider_call_token="",
                    repair_count=case(
                        (InterviewStoryProposalAttempt.repair_count < repair_count, repair_count),
                        else_=InterviewStoryProposalAttempt.repair_count,
                    ),
                    failure_category=category,
                )
            )
            session.commit()
            return int(getattr(result, "rowcount", 0) or 0) == 1

    def mark_contract_failed(
        self,
        *,
        attempt_id: int,
        generation_revision: int,
        provider_call_token: str,
        category: str,
        repair_count: int = 0,
    ) -> bool:
        _require_bounded_repair_count(repair_count)
        with self._session_factory() as session:
            result = session.execute(
                update(InterviewStoryProposalAttempt)
                .where(InterviewStoryProposalAttempt.id == attempt_id)
                .where(InterviewStoryProposalAttempt.attempt_status == "generating")
                .where(InterviewStoryProposalAttempt.generation_revision == generation_revision)
                .where(InterviewStoryProposalAttempt.provider_call_token == provider_call_token)
                .values(
                    attempt_status="contract_failed",
                    provider_call_token="",
                    provider_lease_until=None,
                    repair_count=case(
                        (InterviewStoryProposalAttempt.repair_count < repair_count, repair_count),
                        else_=InterviewStoryProposalAttempt.repair_count,
                    ),
                    failure_category=category,
                )
            )
            session.commit()
            return int(getattr(result, "rowcount", 0) or 0) == 1

    def get_attempt(self, attempt_id: int) -> dict[str, Any] | None:
        with self._session_factory() as session:
            attempt = session.get(InterviewStoryProposalAttempt, attempt_id)
            if attempt is None or _is_story_lifecycle_attempt(attempt):
                return None
            return _attempt_payload(attempt)

    def product_action_source_is_current(
        self,
        *,
        attempt_id: int,
        operation_id: str,
    ) -> bool:
        """Check the live Story owner before choosing full or rejection-only recovery."""

        with self._session_factory() as session:
            attempt = session.get(InterviewStoryProposalAttempt, attempt_id)
            if (
                attempt is None
                or attempt.attempt_status != "ready"
                or attempt.product_action_operation_id != operation_id
                or attempt.product_action_generation < 1
                or attempt.failure_category != ""
            ):
                return False
            payload = _attempt_input_payload(attempt)
            selections = payload.get("selections")
            assertions = payload.get("assertions")
            if not isinstance(selections, list) or not isinstance(assertions, list):
                return False
            try:
                self._validate_target_story_for_claim(
                    session,
                    target_story_id=attempt.target_story_id,
                    expected_current_version_id=cast(
                        int | None,
                        payload.get("expected_current_version_id"),
                    ),
                    expected_story_revision=cast(
                        int | None,
                        payload.get("expected_story_revision"),
                    ),
                )
                current = materialize_selected_sources(session, selections, assertions)
            except StoryValidationError:
                return False
            return current.source_fingerprint == attempt.source_fingerprint

    def prepare_confirmation_decision(
        self,
        *,
        attempt_id: int,
        confirmation_token: str,
        content: Mapping[str, Any],
        evidence_links: list[dict[str, Any]],
        expected_current_version_id: int | None,
        expected_story_revision: int | None,
    ) -> StoryConfirmationAdapterPlan:
        """Map the compatibility request to the closed Product Action decision union."""

        if (
            type(confirmation_token) is not str
            or not _is_optional_positive_int(expected_current_version_id)
            or not _is_optional_positive_int(expected_story_revision)
            or type(content) is not dict
            or type(evidence_links) is not list
            or not all(type(item) is dict for item in evidence_links)
        ):
            raise StoryValidationError("confirmation request is invalid")
        with self._session_factory() as session:
            attempt = session.get(InterviewStoryProposalAttempt, attempt_id)
            if attempt is None or _is_story_lifecycle_attempt(attempt):
                raise StoryNotFoundError("story proposal is missing")
            historical_baseline = (
                attempt.product_action_generation == 0
                and attempt.product_action_operation_id is None
            )
            historical_bridge_request = historical_baseline
            if (
                not historical_baseline
                and _IDEMPOTENCY_KEY.fullmatch(confirmation_token) is not None
                and attempt.product_action_operation_id is not None
                and self._proposal_repository is not None
            ):
                persisted = self._proposal_repository.load_bundle(
                    attempt.product_action_operation_id
                )
                historical_bridge_request = (
                    persisted.route.request_origin == "historical_story_bridge"
                    and persisted.route.source_kind == "story_proposal"
                    and persisted.route.source_id == attempt.id
                )
                if (
                    historical_bridge_request
                    and persisted.classification == "exact_proposed"
                    and self._action_issuer is not None
                    and self._action_issuer.recover_confirmation_token(
                        cast(Any, persisted)
                    )
                    == confirmation_token
                ):
                    historical_bridge_request = False
            if historical_bridge_request:
                if _IDEMPOTENCY_KEY.fullmatch(confirmation_token) is None:
                    raise StoryValidationError("confirmation request is invalid")
                if historical_baseline and attempt.attempt_status == "confirmed":
                    confirmation_hash = sha256_text(confirmation_token)
                    payload_hash = sha256_text(
                        canonical_json(
                            {
                                "content": dict(content),
                                "evidence_links": evidence_links,
                            }
                        )
                    )
                    if (
                        attempt.confirmation_token_hash != confirmation_hash
                        or attempt.confirmation_payload_hash != payload_hash
                        or not attempt.confirmed_story_id
                        or not attempt.confirmed_story_version_id
                    ):
                        raise StoryIdempotencyConflictError(
                            "confirmation input changed"
                        )
                    return StoryConfirmationAdapterPlan(
                        None,
                        None,
                        StoryConfirmation(
                            attempt.confirmed_story_id,
                            attempt.confirmed_story_version_id,
                            False,
                        ),
                        True,
                    )
                if historical_baseline and attempt.attempt_status != "ready":
                    raise StoryCasConflictError(
                        "story proposal cannot be confirmed"
                    )
            elif (
                attempt.product_action_generation < 1
                or not attempt.product_action_operation_id
                or len(confirmation_token) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in confirmation_token
                )
            ):
                raise StoryValidationError("confirmation request is invalid")
            input_payload = _attempt_input_payload(attempt)
            if (
                input_payload.get("expected_current_version_id")
                != expected_current_version_id
                or input_payload.get("expected_story_revision") != expected_story_revision
            ):
                raise StoryCasConflictError("story proposal confirmation CAS changed")
            try:
                proposal = json.loads(attempt.proposal_json)
            except (TypeError, ValueError) as exc:
                raise StoryConflictError("story proposal is invalid") from exc
            if (
                type(proposal) is not dict
                or proposal.get("proposal_status") != "normal"
                or type(proposal.get("content")) is not dict
                or type(proposal.get("evidence_links")) is not list
            ):
                raise StoryCasConflictError("story proposal cannot be confirmed")
            normalized_content = _manual_content_from_canonical(
                canonical_story_content(content)
            )
            normalized_links = [_client_link_fields(item) for item in evidence_links]
            effective: dict[str, JSONValue] = {
                "content": cast(JSONValue, normalized_content),
                "evidence_links": cast(JSONValue, normalized_links),
                "expected_current_version_id": expected_current_version_id,
                "expected_story_revision": expected_story_revision,
            }
            original: dict[str, JSONValue] = {
                "content": cast(
                    JSONValue,
                    _manual_content_from_canonical(
                        cast(dict[str, Any], proposal["content"])
                    ),
                ),
                "evidence_links": cast(
                    JSONValue,
                    [
                        _client_link_fields(item)
                        for item in cast(list[Any], proposal["evidence_links"])
                    ],
                ),
                "expected_current_version_id": expected_current_version_id,
                "expected_story_revision": expected_story_revision,
            }
            request: dict[str, JSONValue] = {
                "confirmation_token": confirmation_token,
                "decision": (
                    "approve"
                    if canonical_product_action_json(effective)
                    == canonical_product_action_json(original)
                    else "modify"
                ),
            }
            if request["decision"] == "modify":
                request["edited_payload"] = effective
            if not historical_bridge_request:
                return StoryConfirmationAdapterPlan(
                    cast(str, attempt.product_action_operation_id),
                    request,
                    None,
                )
            route_raw = self._story_route_payload(
                attempt,
                proposal_hash=attempt.proposal_hash,
                product_action_generation=1,
            )
            session.rollback()
        publication = self._publish_historical_story_bridge(
            attempt_id=attempt_id,
            route_payload_raw=route_raw,
            legacy_confirmation_token=confirmation_token,
        )
        bundle = publication.bundle
        if bundle is None:
            raise ProductActionIntegrityError("historical_story_bridge_bundle")
        if bundle.classification == "exact_terminal":
            ProductActionCoordinator.verify_terminal_projection(bundle)
            expected_hash = "sha256:" + hashlib.sha256(
                canonical_product_action_json(effective).encode("utf-8")
            ).hexdigest()
            if bundle.operation.status == "rejected":
                raise StoryIdempotencyConflictError(
                    "confirmation input changed"
                )
            if bundle.operation.status == "failed":
                failure_code = self._historical_story_failed_terminal_replay(
                    bundle,
                    expected_effective_payload_hash=expected_hash,
                )
                return StoryConfirmationAdapterPlan(
                    None,
                    None,
                    None,
                    True,
                    failure_code,
                )
            if bundle.operation.status != "committed":
                raise ProductActionIntegrityError(
                    "story_product_action_terminal"
                )
            replay = self._historical_story_terminal_replay(
                bundle,
                expected_effective_payload_hash=expected_hash,
            )
            return StoryConfirmationAdapterPlan(None, None, replay, True)
        token = publication.confirmation_token
        if token is None:
            raise ProductActionIntegrityError("historical_story_bridge_token")
        bridged_request = dict(request)
        bridged_request["confirmation_token"] = token
        return StoryConfirmationAdapterPlan(
            publication.operation_id,
            bridged_request,
            None,
            not publication.created,
        )

    def _publish_historical_story_bridge(
        self,
        *,
        attempt_id: int,
        route_payload_raw: bytes,
        legacy_confirmation_token: str,
    ) -> ProductActionPublicationV1:
        issuer = self._action_issuer
        product_actions = self._proposal_repository
        if issuer is None or product_actions is None:
            raise ProductActionIntegrityError("story_product_action_composition")
        publication: ProductActionPublicationV1 | None = None
        with self._session_factory() as session:
            try:
                uow = product_actions.begin_publication_uow(session)
                publication = product_actions.publish_historical_story_bridge_in_session(
                    session,
                    uow,
                    issuer=issuer,
                    route_payload_raw=route_payload_raw,
                    legacy_confirmation_token=legacy_confirmation_token,
                )
                try:
                    session.commit()
                except DBAPIError:
                    try:
                        session.rollback()
                    except BaseException:
                        pass
                    return self._reconcile_historical_story_bridge(
                        attempt_id=attempt_id,
                        publication=publication,
                    )
                return publication
            except BaseException:
                try:
                    session.rollback()
                except BaseException:
                    pass
                raise

    def _reconcile_historical_story_bridge(
        self,
        *,
        attempt_id: int,
        publication: ProductActionPublicationV1,
    ) -> ProductActionPublicationV1:
        product_actions = self._proposal_repository
        issuer = self._action_issuer
        if product_actions is None or issuer is None:
            raise ProductActionIntegrityError("story_product_action_composition")
        try:
            bundle = product_actions.load_bundle(publication.operation_id)
        except ProductActionIntegrityError as exc:
            if exc.code == "product_action_bundle_unreadable":
                raise ProductActionCoordinatorError(
                    "operation_result_unknown",
                    status_code=503,
                    retryable=True,
                ) from exc
            if exc.code != "product_action_bundle_absent":
                raise
        else:
            return self._publication_from_existing_story_bundle(bundle)
        replay_grant = publication.replay_grant
        if replay_grant is None:
            raise ProductActionCoordinatorError(
                "operation_result_unknown",
                status_code=503,
                retryable=True,
            )
        with self._session_factory() as session:
            try:
                uow = product_actions.begin_publication_uow(session)
                try:
                    replayed = (
                        product_actions.replay_all_absent_historical_story_bridge_in_session(
                            session,
                            uow,
                            replay_grant,
                        )
                    )
                except ProductActionContractError as exc:
                    try:
                        session.rollback()
                    except BaseException:
                        pass
                    if exc.code != "publication_replay_not_all_absent":
                        raise
                    try:
                        bundle = product_actions.load_bundle(publication.operation_id)
                    except ProductActionIntegrityError as load_exc:
                        if load_exc.code in {
                            "product_action_bundle_absent",
                            "product_action_bundle_unreadable",
                        }:
                            raise ProductActionCoordinatorError(
                                "operation_result_unknown",
                                status_code=503,
                                retryable=True,
                            ) from load_exc
                        raise
                    return self._publication_from_existing_story_bundle(bundle)
                try:
                    session.commit()
                except DBAPIError:
                    try:
                        session.rollback()
                    except BaseException:
                        pass
                    try:
                        bundle = product_actions.load_bundle(publication.operation_id)
                    except ProductActionIntegrityError as exc:
                        if exc.code in {
                            "product_action_bundle_absent",
                            "product_action_bundle_unreadable",
                        }:
                            raise ProductActionCoordinatorError(
                                "operation_result_unknown",
                                status_code=503,
                                retryable=True,
                            ) from exc
                        raise
                    return self._publication_from_existing_story_bundle(bundle)
                return ProductActionPublicationV1(
                    replayed.classification,
                    replayed.operation_id,
                    replayed.action_call_id,
                    replayed.confirmation_token,
                    False,
                    replayed.bundle,
                    None,
                )
            except BaseException:
                try:
                    session.rollback()
                except BaseException:
                    pass
                raise

    def _publication_from_existing_story_bundle(
        self,
        bundle: ProductActionBundleV1,
    ) -> ProductActionPublicationV1:
        issuer = self._action_issuer
        if issuer is None:
            raise ProductActionIntegrityError("story_product_action_composition")
        token = (
            issuer.recover_confirmation_token(cast(Any, bundle))
            if bundle.classification == "exact_proposed"
            else None
        )
        return ProductActionPublicationV1(
            bundle.classification,
            bundle.operation.id,
            cast(str, bundle.operation.tool_call_id),
            token,
            False,
            bundle,
            None,
        )

    def _historical_story_terminal_replay(
        self,
        bundle: ProductActionBundleV1,
        *,
        expected_effective_payload_hash: str,
    ) -> StoryConfirmation:
        projection = ProductActionCoordinator.verify_terminal_projection(bundle)
        result = projection.result
        with self._session_factory() as session:
            attempt = session.scalar(
                select(InterviewStoryProposalAttempt).where(
                    InterviewStoryProposalAttempt.product_action_operation_id
                    == bundle.operation.id
                )
            )
            if (
                attempt is None
                or attempt.attempt_status != "confirmed"
                or attempt.confirmation_payload_hash
                != expected_effective_payload_hash
                or not attempt.confirmed_story_id
                or not attempt.confirmed_story_version_id
                or attempt.confirmed_story_id != result.get("story_id")
                or attempt.confirmed_story_version_id
                != result.get("story_version_id")
            ):
                raise StoryIdempotencyConflictError("confirmation input changed")
        return StoryConfirmation(
            attempt.confirmed_story_id,
            attempt.confirmed_story_version_id,
            False,
        )

    def _historical_story_failed_terminal_replay(
        self,
        bundle: ProductActionBundleV1,
        *,
        expected_effective_payload_hash: str,
    ) -> str:
        failure_code = "product_action_story_write_conflict"
        projection = ProductActionCoordinator.verify_terminal_projection(bundle)
        with self._session_factory() as session:
            attempt = session.scalar(
                select(InterviewStoryProposalAttempt).where(
                    InterviewStoryProposalAttempt.product_action_operation_id
                    == bundle.operation.id
                )
            )
            if (
                attempt is None
                or attempt.attempt_status != "ready"
                or attempt.confirmation_payload_hash
                != expected_effective_payload_hash
                or bundle.operation.failure_code != failure_code
                or projection.result.get("code") != failure_code
            ):
                raise StoryIdempotencyConflictError(
                    "confirmation input changed"
                )
        return failure_code

    @staticmethod
    def _story_terminal_result(bundle: ProductActionBundleV1) -> Mapping[str, JSONValue]:
        return ProductActionCoordinator.verify_terminal_projection(bundle).result

    def _story_proposal_result(
        self,
        bundle: ProductActionBundleV1,
        *,
        generation: int,
        created: bool,
    ) -> StoryProductActionProposalResult:
        issuer = self._action_issuer
        if issuer is None:
            raise ProductActionIntegrityError("story_product_action_composition")
        token = (
            issuer.recover_confirmation_token(cast(Any, bundle))
            if bundle.classification == "exact_proposed"
            else None
        )
        result = (
            self._story_terminal_result(bundle)
            if bundle.classification == "exact_terminal"
            else None
        )
        return StoryProductActionProposalResult(
            bundle.operation.id,
            cast(str, bundle.operation.tool_call_id),
            generation,
            bundle.operation.status,
            created,
            token,
            result,
        )

    def create_next_product_action(
        self,
        *,
        attempt_id: int,
        expected_generation_revision: int,
        expected_product_action_generation: int,
    ) -> StoryProductActionProposalResult:
        if (
            type(attempt_id) is not int
            or attempt_id < 1
            or type(expected_generation_revision) is not int
            or expected_generation_revision < 1
            or type(expected_product_action_generation) is not int
            or expected_product_action_generation < 1
        ):
            raise StoryValidationError("story Product Action generation is invalid")
        issuer = self._action_issuer
        product_actions = self._proposal_repository
        registry = self._proof_registry
        if issuer is None or product_actions is None or registry is None:
            raise ProductActionIntegrityError("story_product_action_composition")
        next_generation = expected_product_action_generation + 1
        replay_operation_id: str | None = None
        current_generation = 0
        with self._session_factory() as session:
            attempt = session.get(InterviewStoryProposalAttempt, attempt_id)
            if attempt is None or _is_story_lifecycle_attempt(attempt):
                raise StoryNotFoundError("story proposal is missing")
            if (
                attempt.generation_revision != expected_generation_revision
                or not attempt.product_action_operation_id
                or (
                    attempt.product_action_generation
                    == expected_product_action_generation
                    and attempt.attempt_status != "ready"
                )
                or (
                    attempt.product_action_generation >= next_generation
                    and attempt.attempt_status not in {"ready", "confirmed"}
                )
                or attempt.product_action_generation
                < expected_product_action_generation
            ):
                raise StoryCasConflictError("story Product Action generation is stale")
            current_generation = attempt.product_action_generation
            route_raw = self._story_route_payload(
                attempt,
                proposal_hash=attempt.proposal_hash,
                product_action_generation=next_generation,
            )
            if current_generation == next_generation:
                replay_operation_id = attempt.product_action_operation_id
            session.rollback()
        if replay_operation_id is not None:
            existing = self._load_and_validate_replayed_story_generation(
                operation_id=replay_operation_id,
                route_payload_raw=route_raw,
                attempt_id=attempt_id,
                product_action_generation=next_generation,
                expected_generation_revision=expected_generation_revision,
            )
            if existing is None:
                raise ProductActionIntegrityError("story_next_generation_pointer")
            return self._story_proposal_result(
                existing,
                generation=next_generation,
                created=False,
            )
        prepared = issuer.prepare(route_payload_raw=route_raw)
        try:
            try:
                existing = self._load_and_validate_replayed_story_generation(
                    operation_id=prepared.operation_id,
                    route_payload_raw=route_raw,
                    attempt_id=attempt_id,
                    product_action_generation=next_generation,
                )
            except ProductActionIntegrityError as exc:
                if exc.code != "product_action_bundle_absent":
                    raise
            else:
                if existing is None:
                    raise ProductActionIntegrityError(
                        "story_next_generation_pointer"
                    )
                registry.revoke(prepared.route_proof)
                return self._story_proposal_result(
                    existing,
                    generation=next_generation,
                    created=False,
                )
            return self._publish_next_story_generation(
                prepared=prepared,
                route_payload_raw=route_raw,
                attempt_id=attempt_id,
                expected_generation_revision=expected_generation_revision,
                expected_product_action_generation=expected_product_action_generation,
                allow_all_absent_replay=True,
            )
        except BaseException:
            try:
                registry.revoke(prepared.route_proof)
            except ValueError:
                pass
            raise

    def _publish_next_story_generation(
        self,
        *,
        prepared: PreparedProductActionProposalV1,
        route_payload_raw: bytes,
        attempt_id: int,
        expected_generation_revision: int,
        expected_product_action_generation: int,
        allow_all_absent_replay: bool,
    ) -> StoryProductActionProposalResult:
        product_actions = self._proposal_repository
        if product_actions is None:
            raise ProductActionIntegrityError("story_product_action_composition")
        next_generation = expected_product_action_generation + 1
        with self._session_factory() as session:
            try:
                uow = product_actions.begin_publication_uow(session)
                attempt = session.get(InterviewStoryProposalAttempt, attempt_id)
                if (
                    attempt is not None
                    and attempt.attempt_status in {"ready", "confirmed"}
                    and attempt.generation_revision == expected_generation_revision
                    and attempt.product_action_generation == next_generation
                    and attempt.product_action_operation_id == prepared.operation_id
                ):
                    existing = product_actions.load_bundle_in_session(
                        session,
                        uow,
                        prepared.operation_id,
                    )
                    if not self._action_issuer or not self._action_issuer.matches_persisted_request(
                        cast(Any, existing),
                        route_payload_raw=route_payload_raw,
                    ):
                        raise ProductActionIntegrityError(
                            "story_next_generation_identity"
                        )
                    session.rollback()
                    return self._story_proposal_result(
                        existing,
                        generation=next_generation,
                        created=False,
                    )
                if (
                    attempt is None
                    or attempt.attempt_status != "ready"
                    or attempt.generation_revision != expected_generation_revision
                    or attempt.product_action_generation
                    != expected_product_action_generation
                    or not attempt.product_action_operation_id
                ):
                    raise StoryCasConflictError(
                        "story Product Action generation is stale"
                    )
                current = product_actions.load_bundle_in_session(
                    session,
                    uow,
                    attempt.product_action_operation_id,
                )
                current_route_raw = self._story_route_payload(
                    attempt,
                    proposal_hash=attempt.proposal_hash,
                    product_action_generation=expected_product_action_generation,
                )
                if (
                    self._action_issuer is None
                    or not self._action_issuer.matches_persisted_request(
                        cast(Any, current),
                        route_payload_raw=current_route_raw,
                    )
                ):
                    raise ProductActionIntegrityError(
                        "story_next_generation_pointer"
                    )
                if (
                    current.classification != "exact_terminal"
                    or current.operation.status != "rejected"
                ):
                    raise StoryCasConflictError(
                        "only a rejected Story action can advance"
                    )
                if not self._story_bundle_status_matches_attempt(current, attempt):
                    raise ProductActionIntegrityError(
                        "story_next_generation_pointer"
                    )
                payload = _attempt_input_payload(attempt)
                selections = payload.get("selections")
                assertions = payload.get("assertions")
                if not isinstance(selections, list) or not isinstance(assertions, list):
                    raise StorySourceConflictError("story source changed")
                try:
                    self._validate_target_story_for_claim(
                        session,
                        target_story_id=attempt.target_story_id,
                        expected_current_version_id=cast(
                            int | None,
                            payload.get("expected_current_version_id"),
                        ),
                        expected_story_revision=cast(
                            int | None,
                            payload.get("expected_story_revision"),
                        ),
                    )
                except StoryValidationError as exc:
                    raise ProductActionCoordinatorError(
                        "product_action_story_write_conflict"
                    ) from exc
                try:
                    current_snapshot = materialize_selected_sources(
                        session,
                        selections,
                        assertions,
                    )
                except StoryValidationError as exc:
                    raise StorySourceConflictError("story source changed") from exc
                if current_snapshot.source_fingerprint != attempt.source_fingerprint:
                    raise StorySourceConflictError("story source changed")
                publication = product_actions.publish_bundle_in_session(
                    session,
                    uow,
                    prepared,
                )
                if publication.classification != "exact_proposed" or not publication.created:
                    raise ProductActionIntegrityError(
                        "story_next_generation_publication"
                    )
                attempt.product_action_operation_id = publication.operation_id
                attempt.product_action_generation = next_generation
                session.flush()
                bundle = product_actions.load_bundle_in_session(
                    session,
                    uow,
                    publication.operation_id,
                )
                try:
                    session.commit()
                except DBAPIError:
                    try:
                        session.rollback()
                    except BaseException:
                        pass
                    return self._reconcile_next_story_generation(
                        prepared=prepared,
                        route_payload_raw=route_payload_raw,
                        attempt_id=attempt_id,
                        expected_generation_revision=expected_generation_revision,
                        expected_product_action_generation=(
                            expected_product_action_generation
                        ),
                        replay_grant=publication.replay_grant,
                        allow_all_absent_replay=allow_all_absent_replay,
                    )
                return self._story_proposal_result(
                    bundle,
                    generation=next_generation,
                    created=True,
                )
            except BaseException:
                try:
                    session.rollback()
                except BaseException:
                    pass
                raise

    def _reconcile_next_story_generation(
        self,
        *,
        prepared: PreparedProductActionProposalV1,
        route_payload_raw: bytes,
        attempt_id: int,
        expected_generation_revision: int,
        expected_product_action_generation: int,
        replay_grant: ProductActionPublicationReplayV1 | None,
        allow_all_absent_replay: bool,
    ) -> StoryProductActionProposalResult:
        issuer = self._action_issuer
        product_actions = self._proposal_repository
        registry = self._proof_registry
        if issuer is None or product_actions is None or registry is None:
            raise ProductActionIntegrityError("story_product_action_composition")
        next_generation = expected_product_action_generation + 1
        bundle: ProductActionBundleV1 | None = None
        try:
            bundle = self._load_and_validate_replayed_story_generation(
                operation_id=prepared.operation_id,
                route_payload_raw=route_payload_raw,
                attempt_id=attempt_id,
                product_action_generation=next_generation,
                expected_generation_revision=expected_generation_revision,
                allow_absent_for_previous_generation=(
                    expected_product_action_generation
                ),
            )
        except ProductActionIntegrityError as exc:
            if exc.code == "product_action_bundle_unreadable":
                raise ProductActionCoordinatorError(
                    "operation_result_unknown",
                    status_code=503,
                    retryable=True,
                ) from exc
            if exc.code != "product_action_bundle_absent":
                raise
        if bundle is not None:
            return self._story_proposal_result(
                bundle,
                generation=next_generation,
                created=False,
            )
        if not allow_all_absent_replay or replay_grant is None:
            raise ProductActionCoordinatorError(
                "operation_result_unknown",
                status_code=503,
                retryable=True,
            )
        with self._session_factory() as session:
            try:
                uow = product_actions.begin_publication_uow(session)
                fresh = product_actions.refresh_all_absent_from_grant_in_session(
                    session,
                    uow,
                    replay_grant,
                )
                session.rollback()
            except ProductActionContractError as exc:
                try:
                    session.rollback()
                except BaseException:
                    pass
                if exc.code == "publication_replay_not_all_absent":
                    return self._reconcile_next_story_generation(
                        prepared=prepared,
                        route_payload_raw=route_payload_raw,
                        attempt_id=attempt_id,
                        expected_generation_revision=expected_generation_revision,
                        expected_product_action_generation=(
                            expected_product_action_generation
                        ),
                        replay_grant=None,
                        allow_all_absent_replay=False,
                    )
                raise
            except BaseException:
                try:
                    session.rollback()
                except BaseException:
                    pass
                raise
        try:
            result = self._publish_next_story_generation(
                prepared=fresh,
                route_payload_raw=route_payload_raw,
                attempt_id=attempt_id,
                expected_generation_revision=expected_generation_revision,
                expected_product_action_generation=(
                    expected_product_action_generation
                ),
                allow_all_absent_replay=False,
            )
            return StoryProductActionProposalResult(
                result.operation_id,
                result.action_call_id,
                result.product_action_generation,
                result.status,
                False,
                result.confirmation_token,
                result.terminal_result,
            )
        finally:
            try:
                registry.revoke(fresh.route_proof)
            except ValueError:
                pass

    def _load_and_validate_replayed_story_generation(
        self,
        *,
        operation_id: str,
        route_payload_raw: bytes,
        attempt_id: int,
        product_action_generation: int,
        expected_generation_revision: int | None = None,
        allow_absent_for_previous_generation: int | None = None,
    ) -> ProductActionBundleV1 | None:
        product_actions = self._proposal_repository
        issuer = self._action_issuer
        if product_actions is None or issuer is None:
            raise ProductActionIntegrityError("story_product_action_composition")
        with self._session_factory() as session:
            try:
                uow = product_actions.begin_publication_uow(session)
                try:
                    bundle = product_actions.load_bundle_in_session(
                        session,
                        uow,
                        operation_id,
                    )
                except ProductActionIntegrityError as exc:
                    if (
                        exc.code != "product_action_bundle_absent"
                        or allow_absent_for_previous_generation is None
                    ):
                        raise
                    self._require_absent_next_generation_state_in_session(
                        session,
                        uow,
                        attempt_id=attempt_id,
                        expected_generation_revision=expected_generation_revision,
                        previous_generation=allow_absent_for_previous_generation,
                    )
                    session.rollback()
                    return None
                if not issuer.matches_persisted_request(
                    cast(Any, bundle),
                    route_payload_raw=route_payload_raw,
                ):
                    raise ProductActionIntegrityError(
                        "story_next_generation_identity"
                    )
                self._validate_replayed_story_generation_in_session(
                    session,
                    uow,
                    bundle,
                    attempt_id=attempt_id,
                    product_action_generation=product_action_generation,
                    expected_generation_revision=expected_generation_revision,
                )
                session.rollback()
                return bundle
            except BaseException:
                try:
                    session.rollback()
                except BaseException:
                    pass
                raise

    def _require_absent_next_generation_state_in_session(
        self,
        session: Session,
        uow: ProductActionPublicationUoWV1,
        *,
        attempt_id: int,
        expected_generation_revision: int | None,
        previous_generation: int,
    ) -> None:
        product_actions = self._proposal_repository
        issuer = self._action_issuer
        if product_actions is None or issuer is None:
            raise ProductActionIntegrityError("story_product_action_composition")
        attempt = session.get(InterviewStoryProposalAttempt, attempt_id)
        if (
            attempt is None
            or expected_generation_revision is None
            or attempt.generation_revision != expected_generation_revision
            or attempt.attempt_status != "ready"
            or attempt.product_action_generation != previous_generation
            or attempt.product_action_operation_id is None
        ):
            raise ProductActionIntegrityError("story_next_generation_pointer")
        current_route_raw = self._story_route_payload(
            attempt,
            proposal_hash=attempt.proposal_hash,
            product_action_generation=previous_generation,
        )
        current = product_actions.load_bundle_in_session(
            session,
            uow,
            attempt.product_action_operation_id,
        )
        if (
            not issuer.matches_persisted_request(
                cast(Any, current),
                route_payload_raw=current_route_raw,
            )
            or current.classification != "exact_terminal"
            or current.operation.status != "rejected"
            or not self._story_bundle_status_matches_attempt(current, attempt)
        ):
            raise ProductActionIntegrityError("story_next_generation_pointer")

    def _validate_replayed_story_generation_in_session(
        self,
        session: Session,
        uow: ProductActionPublicationUoWV1,
        bundle: ProductActionBundleV1,
        *,
        attempt_id: int,
        product_action_generation: int,
        expected_generation_revision: int | None = None,
    ) -> None:
        product_actions = self._proposal_repository
        issuer = self._action_issuer
        if product_actions is None or issuer is None:
            raise ProductActionIntegrityError("story_product_action_composition")
        if (
            bundle.route.source_kind != "story_proposal"
            or bundle.route.source_id != attempt_id
        ):
            raise ProductActionIntegrityError("story_next_generation_identity")
        attempt = session.get(InterviewStoryProposalAttempt, attempt_id)
        if (
            attempt is None
            or (
                expected_generation_revision is not None
                and attempt.generation_revision != expected_generation_revision
            )
            or attempt.product_action_generation < product_action_generation
        ):
            raise ProductActionIntegrityError("story_next_generation_pointer")
        current_generation = attempt.product_action_generation
        current_operation_id = attempt.product_action_operation_id
        if current_generation == product_action_generation:
            if (
                current_operation_id != bundle.operation.id
                or not self._story_bundle_status_matches_attempt(bundle, attempt)
            ):
                raise ProductActionIntegrityError("story_next_generation_pointer")
            return
        if (
            bundle.classification != "exact_terminal"
            or bundle.operation.status != "rejected"
            or current_operation_id is None
            or attempt.attempt_status not in {"ready", "confirmed"}
        ):
            raise ProductActionIntegrityError("story_next_generation_pointer")
        ProductActionCoordinator.verify_terminal_projection(bundle)
        current_route_raw = self._story_route_payload(
            attempt,
            proposal_hash=attempt.proposal_hash,
            product_action_generation=current_generation,
        )
        current = product_actions.load_bundle_in_session(
            session,
            uow,
            current_operation_id,
        )
        if not issuer.matches_persisted_request(
            cast(Any, current),
            route_payload_raw=current_route_raw,
        ):
            raise ProductActionIntegrityError("story_next_generation_pointer")
        if (
            attempt.product_action_operation_id != current.operation.id
            or attempt.product_action_generation != current_generation
            or not self._story_bundle_status_matches_attempt(current, attempt)
        ):
            raise ProductActionIntegrityError("story_next_generation_pointer")

    @staticmethod
    def _story_bundle_status_matches_attempt(
        bundle: ProductActionBundleV1,
        attempt: InterviewStoryProposalAttempt,
    ) -> bool:
        status = bundle.operation.status
        if status == "proposed":
            return (
                bundle.classification == "exact_proposed"
                and attempt.attempt_status == "ready"
            )
        if bundle.classification != "exact_terminal":
            return False
        ProductActionCoordinator.verify_terminal_projection(bundle)
        if status == "rejected":
            return attempt.attempt_status == "ready"
        if status == "failed":
            return (
                attempt.attempt_status == "ready"
                and bundle.operation.failure_code
                == "product_action_story_write_conflict"
            )
        if status != "committed" or attempt.attempt_status != "confirmed":
            return False
        result = ProductActionCoordinator.verify_terminal_projection(bundle).result
        return (
            attempt.confirmed_story_id == result.get("story_id")
            and attempt.confirmed_story_version_id
            == result.get("story_version_id")
        )

    def get_attempt_retry_after_ms(
        self,
        attempt_id: int,
        *,
        now_factory: Callable[[], datetime] | None = None,
    ) -> int:
        """Return a safe, non-secret delay before a fenced Attempt can be reclaimed."""

        now = _now(now_factory)
        with self._session_factory() as session:
            attempt = session.get(InterviewStoryProposalAttempt, attempt_id)
            if attempt is None or attempt.attempt_status not in {"generating", "provider_unknown"}:
                return 0
            lease_until = _as_aware_utc(attempt.provider_lease_until)
            if lease_until is None or lease_until <= now:
                return 0
            remaining_ms = math.ceil((lease_until - now).total_seconds() * 1000)
            return remaining_ms + _STORY_RETRY_SAFETY_MARGIN_MS

    def confirm_attempt_bound(
        self,
        session: Session,
        trusted_request: TrustedStoryDecision,
        authorization: ProductActionExecutionAuthorization,
        *,
        authorization_binding: tuple[object, ...],
    ) -> StoryWriteResult:
        """Flush a Story aggregate and Attempt confirmation; never own the transaction."""

        registry = self._proof_registry
        if registry is None or type(trusted_request) is not TrustedStoryDecision:
            raise ProductActionIntegrityError("story_bound_confirmation_identity")
        if not session.in_transaction():
            raise ProductActionIntegrityError("story_bound_confirmation_transaction")
        with registry.claim(
            authorization,
            proof_type=ProductActionExecutionAuthorization,
            action_name="confirm_interview_story",
            expected_binding=authorization_binding,
        ):
            attempt = session.get(
                InterviewStoryProposalAttempt,
                trusted_request.attempt_id,
            )
            if (
                attempt is None
                or attempt.attempt_status != "ready"
                or attempt.product_action_operation_id != trusted_request.operation_id
            ):
                raise ProductActionStoryWriteConflict()
            canonical = canonical_story_content(trusted_request.content)
            links = validate_story_evidence_links(
                canonical,
                [dict(item) for item in trusted_request.evidence_links],
                trusted_request.snapshot,
            )
            target_story_id = attempt.target_story_id
            previous_current_version_id: int | None = None
            previous_title: str | None = None
            if target_story_id is None:
                if (
                    trusted_request.expected_current_version_id is not None
                    or trusted_request.expected_story_revision is not None
                ):
                    raise ProductActionStoryWriteConflict()
                story = InterviewStory(
                    title=canonical["title"]["text"],
                    status="active",
                    story_revision=1,
                )
                session.add(story)
                session.flush()
                version_number = 1
                outcome = "created"
            else:
                try:
                    story = self._require_active_story(session, target_story_id)
                    self._check_story_cas(
                        story,
                        trusted_request.expected_current_version_id,
                        trusted_request.expected_story_revision,
                    )
                except StoryValidationError as exc:
                    raise ProductActionStoryWriteConflict() from exc
                previous_current_version_id = story.current_version_id
                previous_title = story.title
                version_number = int(
                    session.scalar(
                        select(InterviewStoryVersion.version_number)
                        .where(InterviewStoryVersion.story_id == story.id)
                        .order_by(InterviewStoryVersion.version_number.desc())
                    )
                    or 0
                ) + 1
                outcome = "version_appended"
            version = self._insert_version(
                session,
                story=story,
                version_number=version_number,
                canonical=canonical,
                snapshot=trusted_request.snapshot,
                canonical_links=links,
                assertions=list(trusted_request.assertions),
                origin_kind="proposal",
            )
            story.current_version_id = version.id
            story.title = canonical["title"]["text"]
            if target_story_id is not None:
                story.story_revision += 1
                self._invalidate_active_target_attempts(session, story.id)
            attempt.attempt_status = "confirmed"
            attempt.confirmation_token_hash = ""
            attempt.confirmation_payload_hash = trusted_request.effective_payload_sha256
            attempt.confirmed_story_id = story.id
            attempt.confirmed_story_version_id = version.id
            attempt.confirmed_at = datetime.now(timezone.utc)
            attempt.provider_lease_until = None
            session.flush()
            if outcome == "created":
                undo: dict[str, JSONValue] = {
                    "kind": "archive_created_story_v1",
                    "story_id": story.id,
                    "created_version_id": version.id,
                    "expected_current_version_id": version.id,
                    "expected_story_revision": story.story_revision,
                    "expected_status": "active",
                }
            else:
                if previous_current_version_id is None or previous_title is None:
                    raise ProductActionIntegrityError("story_undo_identity")
                undo = {
                    "kind": "restore_story_pointer_v1",
                    "story_id": story.id,
                    "created_version_id": version.id,
                    "previous_current_version_id": previous_current_version_id,
                    "previous_title": previous_title,
                    "expected_post_revision": story.story_revision,
                }
            return StoryWriteResult(
                story.id,
                version.id,
                story.story_revision,
                outcome,
                undo,
            )

    def undo_product_action_in_session(
        self,
        session: Session,
        *,
        story_id: int,
        source_attempt_id: int,
        parent_operation_id: str,
        compensation_operation_id: str,
        validated_undo_json: dict[str, JSONValue],
        authorization: ProductActionExecutionAuthorization,
        authorization_binding: tuple[object, ...],
        execution_uow: _CompensationExecutionUowV1 | None = None,
    ) -> dict[str, JSONValue]:
        """Apply one exact Product Action Story Undo inside the caller's UoW."""

        registry = self._proof_registry
        if registry is None:
            raise ProductActionIntegrityError(
                "interview_story_compensation_registry"
            )
        if (
            type(authorization_binding) is not tuple
            or len(authorization_binding) != 7
            or authorization_binding[0]
            != "product_action_compensation_execution_v1"
            or authorization_binding[1] != compensation_operation_id
            or authorization_binding[2] != parent_operation_id
            or authorization_binding[4] != "undo:confirm_interview_story"
            or type(authorization_binding[5]) is not str
            or type(authorization_binding[6]) is not str
            or len(authorization_binding[6]) != 76
            or not authorization_binding[6].startswith("hmac-sha256:")
        ):
            try:
                registry.revoke(authorization)
            except (TypeError, ValueError):
                pass
            raise ProductActionIntegrityError(
                "interview_story_compensation_authorization_binding"
            )
        if type(execution_uow) is not _CompensationExecutionUowV1:
            raise ProductActionIntegrityError(
                "interview_story_compensation_execution_uow"
            )
        try:
            execution_claim_context = execution_uow._claim_story(
                session,
                operation_id=compensation_operation_id,
                parent_operation_id=parent_operation_id,
                authorization_binding=authorization_binding,
            )
        except AttributeError as exc:
            raise ProductActionIntegrityError(
                "interview_story_compensation_execution_uow"
            ) from exc
        with execution_claim_context as execution_claim:
            with registry.claim(
                authorization,
                proof_type=ProductActionExecutionAuthorization,
                action_name="confirm_interview_story",
                expected_binding=authorization_binding,
            ):
                return self._undo_product_action_authorized_in_session(
                    session,
                    story_id=story_id,
                    source_attempt_id=source_attempt_id,
                    parent_operation_id=parent_operation_id,
                    compensation_operation_id=compensation_operation_id,
                    validated_undo_json=validated_undo_json,
                    authorization_binding=authorization_binding,
                    execution_claim=execution_claim,
                )

    def _undo_product_action_authorized_in_session(
        self,
        session: Session,
        *,
        story_id: int,
        source_attempt_id: int,
        parent_operation_id: str,
        compensation_operation_id: str,
        validated_undo_json: dict[str, JSONValue],
        authorization_binding: tuple[object, ...],
        execution_claim: _CompensationExecutionUowClaimV1,
    ) -> dict[str, JSONValue]:
        if (
            type(story_id) is not int
            or story_id < 1
            or type(source_attempt_id) is not int
            or source_attempt_id < 1
            or type(validated_undo_json) is not dict
            or type(execution_claim) is not _CompensationExecutionUowClaimV1
        ):
            raise ProductActionIntegrityError("interview_story_compensation_identity")
        try:
            parent_operation_id = str(UUID(parent_operation_id))
            compensation_operation_id = str(UUID(compensation_operation_id))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ProductActionIntegrityError(
                "interview_story_compensation_identity"
            ) from exc
        compensation = session.get(WriteOperation, compensation_operation_id)
        transitions = tuple(
            session.scalars(
                select(WriteOperationTransition)
                .where(
                    WriteOperationTransition.operation_id
                    == compensation_operation_id
                )
                .order_by(WriteOperationTransition.seq)
            )
        )
        if (
            compensation is None
            or compensation.operation_role != "compensation"
            or compensation.adapter_kind != "compensation"
            or compensation.tool_name != "undo:confirm_interview_story"
            or compensation.parent_operation_id != parent_operation_id
            or compensation.status != "proposed"
            or tuple((row.seq, row.state) for row in transitions)
            != ((1, "proposed"), (2, "approved"), (3, "claimed"))
        ):
            raise ProductActionIntegrityError(
                "interview_story_compensation_operation"
            )
        expected_undo_json = canonical_product_action_json(validated_undo_json)
        parent = session.get(WriteOperation, parent_operation_id)
        if (
            authorization_binding[3]
            != compensation.parent_terminal_payload_sha256
            or authorization_binding[5] != expected_undo_json
            or parent is None
            or parent.operation_role != "primary"
            or parent.adapter_kind != "product_action"
            or parent.tool_name != "confirm_interview_story"
            or parent.status != "committed"
            or parent.undo_json != expected_undo_json
            or parent.terminal_payload_sha256
            != compensation.parent_terminal_payload_sha256
        ):
            raise ProductActionIntegrityError(
                "interview_story_compensation_parent_binding"
            )
        attempt = session.get(InterviewStoryProposalAttempt, source_attempt_id)
        created_version_id = validated_undo_json.get("created_version_id")
        if (
            attempt is None
            or attempt.attempt_status != "confirmed"
            or attempt.product_action_operation_id != parent_operation_id
            or attempt.confirmed_story_id != story_id
            or attempt.confirmed_story_version_id != created_version_id
            or type(created_version_id) is not int
        ):
            raise ProductActionIntegrityError(
                "interview_story_compensation_attempt_lineage"
            )
        story = session.get(InterviewStory, story_id)
        created = session.get(InterviewStoryVersion, created_version_id)
        if (
            story is None
            or created is None
            or created.story_id != story.id
            or created.origin_kind != "proposal"
        ):
            raise ProductActionIntegrityError(
                "interview_story_compensation_domain"
            )
        kind = validated_undo_json.get("kind")
        now = datetime.now(timezone.utc)
        if kind == "archive_created_story_v1":
            if set(validated_undo_json) != {
                "kind",
                "story_id",
                "created_version_id",
                "expected_current_version_id",
                "expected_story_revision",
                "expected_status",
            }:
                raise ProductActionIntegrityError(
                    "interview_story_compensation_undo"
                )
            if (
                validated_undo_json.get("story_id") != story.id
                or validated_undo_json.get("expected_status") != "active"
                or story.status != "active"
                or story.current_version_id
                != validated_undo_json.get("expected_current_version_id")
                or story.story_revision
                != validated_undo_json.get("expected_story_revision")
            ):
                raise ProductActionCompensationStale(
                    "interview_story_undo_stale"
                )
            story.status = "archived"
            story.archived_at = now
        elif kind == "restore_story_pointer_v1":
            if set(validated_undo_json) != {
                "kind",
                "story_id",
                "created_version_id",
                "previous_current_version_id",
                "previous_title",
                "expected_post_revision",
            }:
                raise ProductActionIntegrityError(
                    "interview_story_compensation_undo"
                )
            previous_id = validated_undo_json.get("previous_current_version_id")
            previous_title = validated_undo_json.get("previous_title")
            previous = (
                session.get(InterviewStoryVersion, previous_id)
                if type(previous_id) is int
                else None
            )
            if (
                validated_undo_json.get("story_id") != story.id
                or previous is None
                or previous.story_id != story.id
                or type(previous_title) is not str
                or not previous_title.strip()
                or len(previous_title) > 200
            ):
                raise ProductActionIntegrityError(
                    "interview_story_compensation_undo"
                )
            if (
                story.status != "active"
                or story.current_version_id != created.id
                or story.story_revision
                != validated_undo_json.get("expected_post_revision")
            ):
                raise ProductActionCompensationStale(
                    "interview_story_undo_stale"
                )
            story.current_version_id = previous.id
            story.title = previous_title
            story.archived_at = None
        else:
            raise ProductActionIntegrityError("interview_story_compensation_undo")
        story.story_revision += 1
        story.updated_at = now
        self._invalidate_active_target_attempts(session, story.id)
        session.flush()
        if story.current_version_id is None:
            raise ProductActionIntegrityError(
                "interview_story_compensation_domain"
            )
        domain_result = StoryUndoResultV1(
            story.id,
            story.current_version_id,
            story.story_revision,
            story.status,
        )
        try:
            return execution_claim.terminalize_story(session, domain_result)
        except BaseException:
            session.rollback()
            raise

    def start_heartbeat(
        self,
        *,
        attempt_id: int,
        generation_revision: int,
        provider_call_token: str,
        now_factory: Callable[[], datetime] | None = None,
        waiter: threading.Event | None = None,
    ) -> "_StoryLeaseHeartbeat":
        heartbeat = _StoryLeaseHeartbeat(
            self._session_factory,
            attempt_id=attempt_id,
            generation_revision=generation_revision,
            provider_call_token=provider_call_token,
            now_factory=now_factory,
            waiter=waiter,
        )
        heartbeat.start()
        return heartbeat

    def _replay_or_takeover_attempt(
        self,
        *,
        session: Session,
        attempt: InterviewStoryProposalAttempt,
        payload: dict[str, Any],
        now: datetime,
    ) -> StoryProposalClaim:
        snapshot = _snapshot_from_attempt(payload, attempt.source_fingerprint)
        if (
            attempt.attempt_status == "provider_unknown"
            and attempt.failure_category == "product_action_publication_unknown"
        ):
            session.commit()
            return StoryProposalClaim(
                attempt.id,
                payload,
                snapshot,
                attempt.source_fingerprint,
                False,
                True,
                attempt.generation_revision,
                attempt.provider_call_token,
                attempt.attempt_status,
            )
        if attempt.attempt_status in {"generating", "provider_unknown"} and _lease_is_live(
            attempt.provider_lease_until, now
        ):
            session.commit()
            return StoryProposalClaim(
                attempt.id, payload, snapshot, attempt.source_fingerprint, False, True,
                attempt.generation_revision, attempt.provider_call_token, attempt.attempt_status,
            )
        if attempt.attempt_status in {"generating", "provider_unknown"}:
            token = uuid4().hex
            attempt.attempt_status = "generating"
            attempt.generation_revision += 1
            attempt.provider_call_token = token
            attempt.provider_lease_until = _as_naive_utc(now + timedelta(seconds=_STORY_LEASE_SECONDS))
            attempt.failure_category = ""
            session.commit()
            return StoryProposalClaim(
                attempt.id, payload, snapshot, attempt.source_fingerprint, True, False,
                attempt.generation_revision, token, attempt.attempt_status,
            )
        session.commit()
        return StoryProposalClaim(
            attempt.id, payload, snapshot, attempt.source_fingerprint, False, False,
            attempt.generation_revision, attempt.provider_call_token, attempt.attempt_status,
        )

    @staticmethod
    def _validate_target_story_for_claim(
        session: Session,
        *,
        target_story_id: int | None,
        expected_current_version_id: int | None,
        expected_story_revision: int | None,
    ) -> None:
        if target_story_id is None:
            _require_exact_null("new story current version id", expected_current_version_id)
            _require_exact_null("new story revision", expected_story_revision)
            return
        _require_positive_int("expected current version id", expected_current_version_id)
        _require_positive_int("expected story revision", expected_story_revision)
        story = InterviewStoriesRepository._require_active_story(session, target_story_id)
        InterviewStoriesRepository._check_story_cas(
            story, expected_current_version_id, expected_story_revision
        )

    def _change_lifecycle(
        self, *, story_id: int, expected_story_revision: int | None, desired_status: str
    ) -> dict[str, Any]:
        _require_positive_int("expected story revision", expected_story_revision)
        with self._session_factory() as session:
            try:
                _begin_immediate(session)
                story = session.get(InterviewStory, story_id)
                if story is None:
                    raise StoryNotFoundError("story is missing")
                if story.story_revision != expected_story_revision:
                    raise StoryCasConflictError("story revision is stale")
                if story.status == desired_status:
                    session.commit()
                    return self._story_payload(session, story)
                transitioned_at = datetime.now(timezone.utc)
                before = {
                    "status": story.status,
                    "story_revision": story.story_revision,
                    "archived_at": _story_datetime_iso(story.archived_at),
                    "current_version_id": story.current_version_id,
                    "title": story.title,
                }
                after = {
                    "status": desired_status,
                    "story_revision": story.story_revision + 1,
                    "archived_at": (
                        _story_datetime_iso(transitioned_at)
                        if desired_status == "archived"
                        else None
                    ),
                    "current_version_id": story.current_version_id,
                    "title": story.title,
                }
                story.status = desired_status
                story.archived_at = transitioned_at if desired_status == "archived" else None
                story.story_revision += 1
                story.updated_at = transitioned_at
                self._invalidate_active_target_attempts(session, story.id)
                self._record_lifecycle_transition(
                    session,
                    story=story,
                    expected_story_revision=expected_story_revision,
                    desired_status=desired_status,
                    transitioned_at=transitioned_at,
                    before=before,
                    after=after,
                )
                session.commit()
                return self._story_payload(session, story)
            except Exception:
                session.rollback()
                raise

    @staticmethod
    def _record_lifecycle_transition(
        session: Session,
        *,
        story: InterviewStory,
        expected_story_revision: int,
        desired_status: str,
        transitioned_at: datetime,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> None:
        fingerprint_fields = {
            "operation": "story_lifecycle_v1",
            "target_story_id": story.id,
            "expected_story_revision": expected_story_revision,
            "desired_status": desired_status,
            "transitioned_at": _story_datetime_iso(transitioned_at),
            "before": before,
            "after": after,
        }
        request_fingerprint = _story_lifecycle_request_fingerprint(
            fingerprint_fields
        )
        payload = fingerprint_fields | {
            "request_fingerprint": request_fingerprint,
        }
        payload_json = canonical_json(payload)
        idempotency_key = _story_lifecycle_idempotency_key(
            story_id=story.id,
            expected_story_revision=expected_story_revision,
            desired_status=desired_status,
        )
        marker = canonical_json({"proposal_status": "story_lifecycle"})
        session.add(
            InterviewStoryProposalAttempt(
                target_story_id=story.id,
                idempotency_key=idempotency_key,
                entrypoint="internal",
                entry_context_json=canonical_json(
                    {"operation": "story_lifecycle_v1"}
                ),
                attempt_status="confirmed",
                generation_revision=1,
                provider_call_token="",
                provider_lease_until=None,
                input_snapshot_json=payload_json,
                source_fingerprint=request_fingerprint,
                proposal_json=marker,
                proposal_hash=sha256_text(marker),
                failure_category="",
                confirmation_token_hash=sha256_text(idempotency_key),
                confirmation_payload_hash=sha256_text(payload_json),
                confirmed_story_id=story.id,
                confirmed_story_version_id=None,
                product_action_operation_id=None,
                product_action_generation=0,
                confirmed_at=transitioned_at,
            )
        )

    def _replay_manual_save(
        self,
        session: Session,
        idempotency_key: str,
        request_fingerprint: str,
        *,
        raw_target_story_id: int | None,
        raw_expected_current_version_id: int | None,
        raw_expected_story_revision: int | None,
    ) -> dict[str, Any] | None:
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise StoryValidationError("idempotency key is invalid")
        existing = session.scalar(
            select(InterviewStoryProposalAttempt).where(
                InterviewStoryProposalAttempt.idempotency_key == idempotency_key
            )
        )
        if existing is None:
            if _uses_reserved_story_idempotency_namespace(idempotency_key):
                raise StoryValidationError("idempotency key is reserved")
            return None
        payload = _attempt_input_payload(existing)
        legacy_shape = set(payload) == {"operation", "request_fingerprint"}
        raw_shape = set(payload) == {
            "operation",
            "request_fingerprint",
            "target_story_id",
            "expected_current_version_id",
            "expected_story_revision",
        }
        if (
            (not legacy_shape and not raw_shape)
            or payload.get("operation") != "manual_save"
            or payload.get("request_fingerprint") != request_fingerprint
            or (
                raw_shape
                and (
                    payload.get("target_story_id") != raw_target_story_id
                    or payload.get("expected_current_version_id")
                    != raw_expected_current_version_id
                    or payload.get("expected_story_revision")
                    != raw_expected_story_revision
                )
            )
            or existing.attempt_status != "confirmed"
            or existing.confirmed_story_id is None
            or existing.confirmed_story_version_id is None
        ):
            raise StoryIdempotencyConflictError("story idempotency input changed")
        story = session.get(InterviewStory, existing.confirmed_story_id)
        version = session.get(InterviewStoryVersion, existing.confirmed_story_version_id)
        if story is None or version is None or version.story_id != story.id:
            raise StoryIdempotencyConflictError("manual story replay is unavailable")
        return self._story_payload(session, story, version=version)

    @staticmethod
    def _record_manual_save(
        session: Session,
        *,
        idempotency_key: str,
        request_fingerprint: str,
        raw_target_story_id: int | None,
        raw_expected_current_version_id: int | None,
        raw_expected_story_revision: int | None,
        story: InterviewStory,
        version: InterviewStoryVersion,
        snapshot: StorySourceSnapshot,
    ) -> None:
        payload = {
            "operation": "manual_save",
            "request_fingerprint": request_fingerprint,
            "target_story_id": raw_target_story_id,
            "expected_current_version_id": raw_expected_current_version_id,
            "expected_story_revision": raw_expected_story_revision,
        }
        payload_json = canonical_json(payload)
        session.add(
            InterviewStoryProposalAttempt(
                target_story_id=story.id,
                idempotency_key=idempotency_key,
                entrypoint="ui",
                entry_context_json=canonical_json({"operation": "manual_save"}),
                attempt_status="confirmed",
                generation_revision=1,
                provider_call_token="",
                provider_lease_until=None,
                input_snapshot_json=payload_json,
                source_fingerprint=snapshot.source_fingerprint,
                proposal_json=canonical_json({"proposal_status": "manual"}),
                proposal_hash=sha256_text(canonical_json({"proposal_status": "manual"})),
                failure_category="",
                confirmation_token_hash=sha256_text(idempotency_key),
                confirmation_payload_hash=sha256_text(payload_json),
                confirmed_story_id=story.id,
                confirmed_story_version_id=version.id,
                confirmed_at=datetime.now(timezone.utc),
            )
        )

    @staticmethod
    def _invalidate_attempt(attempt: InterviewStoryProposalAttempt, *, category: str) -> None:
        attempt.attempt_status = "invalidated"
        attempt.provider_call_token = ""
        attempt.provider_lease_until = None
        attempt.failure_category = category

    @staticmethod
    def _invalidate_active_target_attempts(session: Session, story_id: int) -> None:
        """Release in-flight target Story claims as soon as its revision changes."""
        session.execute(
            update(InterviewStoryProposalAttempt)
            .where(InterviewStoryProposalAttempt.target_story_id == story_id)
            .where(InterviewStoryProposalAttempt.attempt_status.in_(("generating", "provider_unknown")))
            .values(
                attempt_status="invalidated",
                provider_call_token="",
                provider_lease_until=None,
                failure_category="source_changed",
            )
        )

    @staticmethod
    def _require_active_story(session: Session, story_id: int) -> InterviewStory:
        story = session.get(InterviewStory, story_id)
        if story is None:
            raise StoryNotFoundError("story is missing")
        if story.status != "active":
            raise StoryCasConflictError("story is archived")
        return story

    @staticmethod
    def _check_story_cas(
        story: InterviewStory,
        expected_current_version_id: int | None,
        expected_story_revision: int | None,
    ) -> None:
        if (
            story.current_version_id != expected_current_version_id
            or story.story_revision != expected_story_revision
        ):
            raise StoryCasConflictError("story version is stale")

    @staticmethod
    def _insert_version(
        session: Session,
        *,
        story: InterviewStory,
        version_number: int,
        canonical: dict[str, Any],
        snapshot: StorySourceSnapshot,
        canonical_links: list[CanonicalStoryLink],
        assertions: list[str],
        origin_kind: str,
    ) -> InterviewStoryVersion:
        content_json = canonical_json(canonical)
        version = InterviewStoryVersion(
            story_id=story.id,
            version_number=version_number,
            content_json=content_json,
            content_hash=sha256_text(content_json),
            source_fingerprint=snapshot.source_fingerprint,
            origin_kind=origin_kind,
        )
        session.add(version)
        session.flush()
        assertion_ids: dict[str, str] = {}
        for index, statement in enumerate(assertions, 1):
            assertion = InterviewStoryUserAssertion(
                story_version_id=version.id,
                statement_text=statement,
                statement_hash=sha256_text(statement),
            )
            session.add(assertion)
            session.flush()
            assertion_ids[f"assertion_{index:03d}"] = str(assertion.id)
        for link in canonical_links:
            source_stable_id = assertion_ids.get(link.source_stable_id, link.source_stable_id)
            payload = link.as_dict() | {"source_stable_id": source_stable_id}
            # The link's persisted identity, including an assertion's real ID, is
            # what auditors later hash; never retain the temporary request ID.
            payload["link_hash"] = sha256_text(canonical_json({key: value for key, value in payload.items() if key != "link_hash"}))
            session.add(
                InterviewStoryVersionEvidenceLink(
                    story_version_id=version.id,
                    **payload,
                )
            )
        session.flush()
        return version

    def _story_summary(self, session: Session, story: InterviewStory) -> dict[str, Any]:
        version = session.get(InterviewStoryVersion, story.current_version_id) if story.current_version_id else None
        return {
            "id": story.id,
            "title": story.title,
            "status": story.status,
            "current_version_id": story.current_version_id,
            "story_revision": story.story_revision,
            "version_number": version.version_number if version else None,
            "source_states": derive_story_source_states(session, version) if version else [],
        }

    def _story_payload(
        self,
        session: Session,
        story: InterviewStory,
        *,
        version: InterviewStoryVersion | None = None,
    ) -> dict[str, Any]:
        payload = self._story_summary(session, story)
        if version is None:
            version = session.get(InterviewStoryVersion, story.current_version_id) if story.current_version_id else None
        payload["version"] = self._version_payload(session, version) if version else None
        return payload

    @staticmethod
    def _version_payload(session: Session, version: InterviewStoryVersion) -> dict[str, Any]:
        links = list(
            session.scalars(
                select(InterviewStoryVersionEvidenceLink)
                .where(InterviewStoryVersionEvidenceLink.story_version_id == version.id)
                .order_by(InterviewStoryVersionEvidenceLink.id.asc())
            )
        )
        assertions = list(
            session.scalars(
                select(InterviewStoryUserAssertion)
                .where(InterviewStoryUserAssertion.story_version_id == version.id)
                .order_by(InterviewStoryUserAssertion.id.asc())
            )
        )
        return {
            "id": version.id,
            "story_id": version.story_id,
            "version_number": version.version_number,
            "content": json.loads(version.content_json),
            "content_hash": version.content_hash,
            "source_fingerprint": version.source_fingerprint,
            "origin_kind": version.origin_kind,
            "evidence_links": [
                {
                    "target_kind": link.target_kind,
                    "target_id": link.target_id,
                    "source_kind": link.source_kind,
                    "source_stable_id": link.source_stable_id,
                    "source_version_or_snapshot": link.source_version_or_snapshot,
                    "source_path": link.source_path,
                    "text_location": link.text_location,
                    "excerpt": link.excerpt,
                    "source_fingerprint": link.source_fingerprint,
                    "link_hash": link.link_hash,
                }
                for link in links
            ],
            "assertions": [
                {"id": assertion.id, "statement": assertion.statement_text, "frozen": True}
                for assertion in assertions
            ],
            "source_states": derive_story_source_states(session, version),
        }


class InterviewStoryProductActionHandler:
    """Sealed Coordinator handler for the Story Product Action."""

    action_name = "confirm_interview_story"
    terminal_budgets = ProductActionTerminalBudgetsV1(
        4_096,
        1_024,
        4_096,
        32_768,
        49_152,
    )
    declared_executor_failures = (
        ProductActionDeclaredExecutorFailureV1(
            ProductActionStoryWriteConflict,
            "conflict",
            "product_action_story_write_conflict",
        ),
    )
    declared_preclaim_dispositions = ("source_changed",)

    def __init__(self, repository: InterviewStoriesRepository) -> None:
        if type(repository) is not InterviewStoriesRepository:
            raise TypeError("Story Product Action handler composition is invalid")
        self._repository = repository
        self._session_factory = repository._session_factory

    @staticmethod
    def _hash_payload(payload: Mapping[str, JSONValue]) -> str:
        raw = canonical_product_action_json(dict(payload)).encode("utf-8")
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _original_effective_payload(
        attempt: InterviewStoryProposalAttempt,
        route_payload: Mapping[str, JSONValue],
    ) -> dict[str, JSONValue]:
        try:
            proposal = json.loads(attempt.proposal_json)
        except (TypeError, ValueError) as exc:
            raise ProductActionIntegrityError("story_proposal_payload") from exc
        if (
            type(proposal) is not dict
            or proposal.get("proposal_status") != "normal"
            or type(proposal.get("content")) is not dict
            or type(proposal.get("evidence_links")) is not list
        ):
            raise ProductActionIntegrityError("story_proposal_payload")
        content = _manual_content_from_canonical(cast(dict[str, Any], proposal["content"]))
        evidence_links = [
            _client_link_fields(item)
            for item in cast(list[Any], proposal["evidence_links"])
        ]
        return {
            "content": cast(JSONValue, content),
            "evidence_links": cast(JSONValue, evidence_links),
            "expected_current_version_id": route_payload["expected_current_version_id"],
            "expected_story_revision": route_payload["expected_story_revision"],
        }

    def _preflight(
        self,
        session: Session,
        route_payload: Mapping[str, JSONValue],
        effective_payload: Mapping[str, JSONValue],
        *,
        locked: bool,
    ) -> ProductActionPreflightV1 | ProductActionPreClaimDispositionV1:
        attempt_id = route_payload.get("attempt_id")
        if type(attempt_id) is not int:
            raise ProductActionIntegrityError("story_route_attempt")
        attempt = session.get(InterviewStoryProposalAttempt, attempt_id)
        if (
            attempt is None
            or attempt.attempt_status != "ready"
            or attempt.generation_revision != route_payload.get("generation_revision")
            or attempt.product_action_generation
            != route_payload.get("product_action_generation")
            or attempt.product_action_operation_id is None
            or (
                attempt.proposal_hash
                if attempt.proposal_hash.startswith("sha256:")
                else "sha256:" + attempt.proposal_hash
            )
            != route_payload.get("proposal_hash")
            or (
                attempt.source_fingerprint
                if attempt.source_fingerprint.startswith("sha256:")
                else "sha256:" + attempt.source_fingerprint
            )
            != route_payload.get("source_fingerprint")
            or attempt.target_story_id != route_payload.get("target_story_id")
            or attempt.failure_category != ""
        ):
            if locked:
                return ProductActionPreClaimDispositionV1(
                    "confirm_interview_story",
                    "source_changed",
                )
            raise ProductActionCoordinatorError("story_source_conflict")
        input_payload = _attempt_input_payload(attempt)
        selections = input_payload.get("selections")
        assertions = input_payload.get("assertions")
        if not isinstance(selections, list) or not isinstance(assertions, list):
            if locked:
                return ProductActionPreClaimDispositionV1(
                    "confirm_interview_story",
                    "source_changed",
                )
            raise ProductActionCoordinatorError("story_source_conflict")
        try:
            snapshot = materialize_selected_sources(session, selections, assertions)
        except StoryValidationError as exc:
            if locked:
                return ProductActionPreClaimDispositionV1(
                    "confirm_interview_story",
                    "source_changed",
                )
            raise ProductActionCoordinatorError("story_source_conflict") from exc
        if snapshot.source_fingerprint != attempt.source_fingerprint:
            if locked:
                return ProductActionPreClaimDispositionV1(
                    "confirm_interview_story",
                    "source_changed",
                )
            raise ProductActionCoordinatorError("story_source_conflict")
        if set(effective_payload) != {
            "content",
            "evidence_links",
            "expected_current_version_id",
            "expected_story_revision",
        }:
            raise ProductActionCoordinatorError(
                "product_action_invalid_request",
                status_code=422,
            )
        current = effective_payload["expected_current_version_id"]
        revision = effective_payload["expected_story_revision"]
        if (
            current != route_payload["expected_current_version_id"]
            or revision != route_payload["expected_story_revision"]
            or not _is_optional_positive_int(current)
            or not _is_optional_positive_int(revision)
            or type(effective_payload["content"]) is not dict
            or type(effective_payload["evidence_links"]) is not list
        ):
            raise ProductActionCoordinatorError(
                "product_action_revision_conflict",
            )
        content = cast(dict[str, Any], effective_payload["content"])
        links = cast(list[Any], effective_payload["evidence_links"])
        if not all(type(item) is dict for item in links):
            raise ProductActionCoordinatorError(
                "product_action_invalid_request",
                status_code=422,
            )
        try:
            canonical = canonical_story_content(content)
            canonical_links = validate_story_evidence_links(
                canonical,
                cast(list[dict[str, Any]], links),
                snapshot,
            )
        except StoryValidationError as exc:
            raise ProductActionCoordinatorError(
                "product_action_invalid_request",
                status_code=422,
            ) from exc
        normalized: dict[str, JSONValue] = {
            "content": cast(JSONValue, _manual_content_from_canonical(canonical)),
            "evidence_links": cast(
                JSONValue,
                [_client_link_fields(item.as_dict()) for item in canonical_links],
            ),
            "expected_current_version_id": current,
            "expected_story_revision": revision,
        }
        effective_hash = self._hash_payload(normalized)
        trusted = TrustedStoryDecision(
            _TRUSTED_STORY_DECISION_SEAL,
            attempt_id=attempt.id,
            operation_id=attempt.product_action_operation_id,
            content=cast(Mapping[str, Any], normalized["content"]),
            evidence_links=tuple(
                cast(list[Mapping[str, Any]], normalized["evidence_links"])
            ),
            expected_current_version_id=cast(int | None, current),
            expected_story_revision=cast(int | None, revision),
            snapshot=snapshot,
            assertions=tuple(cast(list[str], assertions)),
            effective_payload_sha256=effective_hash,
        )
        return ProductActionPreflightV1(
            self.action_name,
            normalized,
            effective_hash,
            trusted,
        )

    def external_preflight(
        self,
        route_payload: Mapping[str, JSONValue],
        decision: Any,
    ) -> ProductActionPreflightV1:
        with self._session_factory() as session:
            if decision.decision == "approve":
                attempt_id = route_payload.get("attempt_id")
                if type(attempt_id) is not int:
                    raise ProductActionIntegrityError("story_route_attempt")
                attempt = session.get(InterviewStoryProposalAttempt, attempt_id)
                if attempt is None:
                    raise ProductActionCoordinatorError("story_source_conflict")
                effective = self._original_effective_payload(attempt, route_payload)
            elif decision.decision == "modify":
                edited = decision.edited_payload
                if edited is None:
                    raise ProductActionCoordinatorError(
                        "product_action_invalid_request",
                        status_code=422,
                    )
                effective = dict(edited)
            else:
                raise ProductActionCoordinatorError(
                    "product_action_invalid_request",
                    status_code=422,
                )
            result = self._preflight(
                session,
                route_payload,
                cast(Mapping[str, JSONValue], effective),
                locked=False,
            )
            if type(result) is not ProductActionPreflightV1:
                raise ProductActionIntegrityError("story_preflight_identity")
            return result

    def locked_recheck(
        self,
        session: Session,
        route_payload: Mapping[str, JSONValue],
        trusted: TrustedProductActionDecisionV1,
    ) -> ProductActionPreflightV1 | ProductActionPreClaimDispositionV1:
        return self._preflight(
            session,
            route_payload,
            trusted.effective_payload,
            locked=True,
        )

    def execute_in_session(
        self,
        session: Session,
        *,
        operation_id: str,
        operation_request_fingerprint: str,
        route_payload: Mapping[str, JSONValue],
        trusted: TrustedProductActionDecisionV1,
        authorization: ProductActionExecutionAuthorization,
        authorization_binding: tuple[object, ...],
    ) -> ProductActionHandlerResultV1:
        del operation_request_fingerprint, route_payload
        source = trusted.trusted_source
        if type(source) is not TrustedStoryDecision or source.operation_id != operation_id:
            raise ProductActionIntegrityError("story_trusted_decision_identity")
        result = self._repository.confirm_attempt_bound(
            session,
            source,
            authorization,
            authorization_binding=authorization_binding,
        )
        safe_result: dict[str, JSONValue] = {
            "schema_version": 1,
            "action_name": self.action_name,
            "outcome": result.outcome,
            "story_id": result.story_id,
            "story_version_id": result.version_id,
            "story_revision": result.story_revision,
        }
        return ProductActionHandlerResultV1(
            safe_result,
            "已保存到经历素材。",
            result.undo,
        )

    def stage_terminal_input_in_session(
        self,
        session: Session,
        *,
        operation_id: str,
        effective_payload_sha256: str,
    ) -> None:
        attempt = session.scalar(
            select(InterviewStoryProposalAttempt).where(
                InterviewStoryProposalAttempt.product_action_operation_id
                == operation_id
            )
        )
        if (
            attempt is None
            or attempt.attempt_status != "ready"
            or attempt.confirmation_payload_hash
            or not effective_payload_sha256.startswith("sha256:")
        ):
            raise ProductActionIntegrityError("story_terminal_attempt")
        attempt.confirmation_payload_hash = effective_payload_sha256
        session.flush()

    def project_committed_terminal(
        self,
        operation_id: str,
        result: ProductActionHandlerResultV1,
    ) -> ProductActionTerminalProjectionV1:
        story_id = cast(int, result.result["story_id"])
        version_id = cast(int, result.result["story_version_id"])
        transport: dict[str, JSONValue] = {
            "schema_version": 1,
            "operation_id": operation_id,
            "action_name": self.action_name,
            "status": "committed",
            "result": cast(JSONValue, dict(result.result)),
            "legacy_direct_commit": {
                "status_code": 201,
                "body": {"story_id": story_id, "version_id": version_id, "created": True},
            },
            "legacy_reconciliation_or_replay": {
                "status_code": 200,
                "body": {"story_id": story_id, "version_id": version_id, "created": False},
            },
        }
        return ProductActionTerminalProjectionV1(
            result.result,
            result.visible_result,
            transport,
            result.undo,
        )

    def project_failed_terminal(
        self,
        operation_id: str,
        failure: ProductActionDeclaredExecutorFailureV1,
    ) -> ProductActionTerminalProjectionV1:
        result: dict[str, JSONValue] = {
            "schema_version": 1,
            "action_name": self.action_name,
            "outcome": "failed",
            "code": failure.failure_code,
        }
        return ProductActionTerminalProjectionV1(
            result,
            "经历素材写入发生冲突，请刷新后重试。",
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "action_name": self.action_name,
                "status": "failed",
                "code": failure.failure_code,
            },
            None,
        )

    def terminal_replay_effective_payload_sha256(
        self,
        operation_id: str,
        decision: Any,
    ) -> str:
        if decision.decision == "approve":
            return self.persisted_terminal_effective_payload_sha256(operation_id)
        if decision.decision != "modify" or decision.edited_payload is None:
            raise ProductActionCoordinatorError(
                "product_action_invalid_request",
                status_code=422,
            )
        return self._hash_payload(decision.edited_payload)

    def persisted_terminal_effective_payload_sha256(self, operation_id: str) -> str:
        with self._session_factory() as session:
            attempt = session.scalar(
                select(InterviewStoryProposalAttempt).where(
                    InterviewStoryProposalAttempt.product_action_operation_id == operation_id
                )
            )
            if (
                attempt is None
                or attempt.attempt_status not in {"ready", "confirmed"}
                or not attempt.confirmation_payload_hash.startswith("sha256:")
            ):
                raise ProductActionIntegrityError("story_terminal_attempt")
            return attempt.confirmation_payload_hash


def _begin_immediate(session: Session) -> None:
    session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def _require_positive_int(name: str, value: int | None) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StoryValidationError(f"{name} is invalid")


def _require_exact_null(name: str, value: object) -> None:
    if value is not None:
        raise StoryValidationError(f"{name} must be null")


def _canonical_selections(selections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [dict(item) for item in selections],
        key=lambda item: (str(item.get("source_kind")), str(item.get("source_id")), str(item.get("path"))),
    )


def _manual_content_from_canonical(content: dict[str, Any]) -> dict[str, Any]:
    title = content.get("title")
    blocks = content.get("blocks")
    labels = content.get("capability_labels")
    questions = content.get("applicable_questions")
    gaps = content.get("fact_gap_codes")
    if not isinstance(title, dict) or not isinstance(blocks, list) or not isinstance(labels, list) or not isinstance(questions, list) or not isinstance(gaps, list):
        raise StoryValidationError("story proposal is invalid")
    return {
        "title": title.get("text"),
        "blocks": [
            {key: block.get(key) for key in ("kind", "text", "fact_mode")}
            for block in blocks
            if isinstance(block, dict)
        ],
        "capability_labels": [item.get("text") for item in labels if isinstance(item, dict)],
        "applicable_questions": [item.get("text") for item in questions if isinstance(item, dict)],
        "fact_gap_codes": gaps,
    }


def _client_link_fields(link: Any) -> dict[str, Any]:
    if not isinstance(link, dict):
        raise StoryValidationError("story evidence link is invalid")
    allowed = {
        "target_kind",
        "target_id",
        "source_kind",
        "source_stable_id",
        "source_version_or_snapshot",
        "source_path",
        "excerpt",
        "text_location",
    }
    normalized = {key: value for key, value in link.items() if key in allowed}
    if "source_stable_id" not in normalized and "source_id" in link:
        normalized["source_stable_id"] = link["source_id"]
    return normalized


def _attempt_input_payload(attempt: InterviewStoryProposalAttempt) -> dict[str, Any]:
    try:
        parsed = json.loads(attempt.input_snapshot_json)
    except (TypeError, ValueError) as exc:
        raise StoryConflictError("story proposal snapshot is invalid") from exc
    if not isinstance(parsed, dict):
        raise StoryConflictError("story proposal snapshot is invalid")
    return parsed


def _is_story_lifecycle_attempt(attempt: InterviewStoryProposalAttempt) -> bool:
    """Keep internal lifecycle audit rows out of every Proposal surface."""

    if attempt.entrypoint == "internal":
        return True
    try:
        return _attempt_input_payload(attempt).get("operation") == "story_lifecycle_v1"
    except StoryConflictError:
        return False


def _snapshot_from_attempt(payload: dict[str, Any], fingerprint: str) -> StorySourceSnapshot:
    sources = payload.get("sources")
    if not isinstance(sources, list) or not all(isinstance(source, dict) for source in sources):
        raise StoryConflictError("story proposal snapshot is invalid")
    normalized = [dict(source) for source in sources]
    if not all(
        set(source) == {
            "source_kind",
            "source_stable_id",
            "source_version_or_snapshot",
            "path",
            "excerpt",
            "source_fingerprint",
        }
        and all(isinstance(value, str) for value in source.values())
        for source in normalized
    ):
        raise StoryConflictError("story proposal snapshot is invalid")
    return StorySourceSnapshot(sources=normalized, source_fingerprint=fingerprint)


def _attempt_payload(attempt: InterviewStoryProposalAttempt) -> dict[str, Any]:
    return {
        "id": attempt.id,
        "target_story_id": attempt.target_story_id,
        "entrypoint": attempt.entrypoint,
        "attempt_status": attempt.attempt_status,
        "generation_revision": attempt.generation_revision,
        "source_fingerprint": attempt.source_fingerprint,
        "proposal": json.loads(attempt.proposal_json) if attempt.proposal_json else None,
        "failure_category": attempt.failure_category or None,
        "confirmed_story_id": attempt.confirmed_story_id,
        "confirmed_story_version_id": attempt.confirmed_story_version_id,
        "product_action_operation_id": attempt.product_action_operation_id,
        "product_action_generation": attempt.product_action_generation,
    }


def _now(now_factory: Any) -> datetime:
    value = now_factory() if now_factory is not None else datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        raise StoryValidationError("clock is invalid")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _as_naive_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _as_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _lease_is_live(value: datetime | None, now: datetime) -> bool:
    aware = _as_aware_utc(value)
    return aware is not None and aware > now


def _owned_attempt(
    attempt: InterviewStoryProposalAttempt | None,
    generation_revision: int,
    provider_call_token: str,
) -> bool:
    return bool(
        attempt is not None
        and attempt.attempt_status == "generating"
        and attempt.generation_revision == generation_revision
        and attempt.provider_call_token == provider_call_token
    )


class _StoryLeaseHeartbeat:
    """Best-effort lease renewal using a new short Session for each tick."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        attempt_id: int,
        generation_revision: int,
        provider_call_token: str,
        now_factory: Callable[[], datetime] | None,
        waiter: threading.Event | None,
    ) -> None:
        self._session_factory = session_factory
        self._attempt_id = attempt_id
        self._generation_revision = generation_revision
        self._provider_call_token = provider_call_token
        self._now_factory = now_factory
        self._stop = waiter or threading.Event()
        self._thread = threading.Thread(target=self._run, name="interview-story-lease", daemon=True)
        self.heartbeat_count = 0
        self.confirmed_ownership_lost = False
        self.heartbeat_uncertain = False

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()
        if self._thread.is_alive():
            raise RuntimeError("interview story lease heartbeat did not stop")

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def tick(self) -> bool:
        """Renew once; lock errors remain uncertain rather than proving takeover."""

        for _attempt in range(2):
            try:
                with self._session_factory() as session:
                    now = _now(self._now_factory)
                    result = session.execute(
                        update(InterviewStoryProposalAttempt)
                        .where(InterviewStoryProposalAttempt.id == self._attempt_id)
                        .where(InterviewStoryProposalAttempt.attempt_status == "generating")
                        .where(InterviewStoryProposalAttempt.generation_revision == self._generation_revision)
                        .where(InterviewStoryProposalAttempt.provider_call_token == self._provider_call_token)
                        .values(provider_lease_until=_as_naive_utc(now + timedelta(seconds=_STORY_LEASE_SECONDS)))
                    )
                    session.commit()
                    if int(getattr(result, "rowcount", 0) or 0) == 0:
                        self.confirmed_ownership_lost = True
                        return False
                    self.heartbeat_count += 1
                    return True
            except SQLAlchemyError:
                continue
        self.heartbeat_uncertain = True
        return False

    def _run(self) -> None:
        while not self._stop.wait(_STORY_HEARTBEAT_SECONDS):
            if not self.tick():
                return
