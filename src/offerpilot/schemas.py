from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ApplicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company_name: str
    position_name: str
    job_url: str
    status: str
    source: str
    notes: str
    applied_at: datetime
    first_pending_at: datetime | None = None
    first_applied_at: datetime | None = None
    first_written_test_at: datetime | None = None
    first_interview_at: datetime | None = None
    first_offer_at: datetime | None = None
    closed_reason: str = ""
    closed_at: datetime | None = None
    deleted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ApplicationSubmissionSnapshotOut(BaseModel):
    id: int
    application_id: int
    resume_id: int
    resume_title: str
    jd_version_id: int
    jd_version_number: int
    material_kit_id: int | None
    resume_snapshot: dict[str, Any]
    jd_snapshot: str
    material_snapshot: dict[str, Any] | None
    note: str
    source_kind: Literal["ui", "pilot"]
    source_states: dict[str, Literal["current", "changed", "missing"]]
    submitted_at: datetime
    created_at: datetime


class ApplicationOutcomeOut(BaseModel):
    id: int
    application_id: int
    submission_snapshot_id: int
    application_event_id: int | None
    stage: Literal["applied", "screening", "written_test", "interview", "offer", "closed"]
    result: Literal["advanced", "rejected", "withdrawn", "no_response", "offer_received", "other"]
    feedback_text: str
    reflection_text: str
    next_action_text: str
    feedback_tags: list[str]
    source_kind: Literal["ui", "pilot"]
    occurred_at: datetime
    created_at: datetime


class ApplicationOutcomeSummaryOut(BaseModel):
    total: int
    stage_counts: dict[str, int]
    result_counts: dict[str, int]
    feedback_tag_counts: dict[str, int]
    next_actions_pending: int


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    title_source: str = "manual"
    mode: str = "general"
    context_type: str = "workspace"
    context_ref: str = ""
    pinned_at: datetime | None = None
    archived_at: datetime | None = None
    pending_action: dict[str, object] | None = None
    pending_clarification: dict[str, object] | None = None
    last_write_undo: dict[str, object] | None = None
    created_at: datetime
    updated_at: datetime


class ChatMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    role: str
    content: str
    tool_calls: str = ""
    tool_call_id: str = ""
    created_at: datetime


class ApplicationEventOut(BaseModel):
    id: int
    application_id: int
    event_type: str
    subtype: str = ""
    tags: list[str] = []
    round: int
    scheduled_at: str
    duration_minutes: int
    location: str
    notes: str
    remind_at: str | None = None
    status: str = "todo"
    created_at: datetime
    company_name: str | None = None
    position_name: str | None = None


class InterviewNoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    application_id: int | None = None
    application_event_id: int | None = None
    company: str
    position: str
    round: str
    date: str
    questions: str
    self_reflection: str
    difficulty_points: str
    mood: str
    created_at: datetime


class InterviewNoteRestOut(InterviewNoteOut):
    content_revision: int
    updated_at: datetime


class OfferOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    application_id: int | None = None
    company_name: str
    position_name: str
    status: str
    base_monthly: int
    months_per_year: int
    signing_bonus: int
    equity: str
    perks: str
    deadline: str
    notes: str
    assessment: str
    total_cash: int
    created_at: datetime
    updated_at: datetime


class OfferComparisonDimensionOut(BaseModel):
    id: int
    label: str
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class OfferComparisonValueOut(BaseModel):
    id: int
    offer_id: int
    dimension_id: int
    value_text: str | None = None
    created_at: datetime
    updated_at: datetime


class OfferComparisonValueCellOut(BaseModel):
    offer_id: int
    value_text: str | None = None


class OfferComparisonDimensionReadOut(BaseModel):
    id: int
    label: str
    values: list[OfferComparisonValueCellOut]


class OfferComparisonMissingOut(BaseModel):
    offer_id: int
    path: str
    label: str


class OfferComparisonReadOut(BaseModel):
    offers: list[OfferOut]
    dimensions: list[OfferComparisonDimensionReadOut]
    missing: list[OfferComparisonMissingOut]


class ResumeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str = ""
    file_path: str = ""
    parsed_data: str = ""
    parse_status: str = "pending"
    title: str = ""
    is_master: bool = False
    parent_resume_id: int | None = None
    source: str = "manual"
    source_file_path: str = ""
    content_json: dict[str, Any] = Field(default_factory=dict)
    deleted_at: datetime | None = None
    created_at: datetime

    @field_validator("content_json", mode="before")
    @classmethod
    def _parse_content_json(cls, value: Any) -> dict[str, Any]:
        return normalize_resume_content(value)


class ResumeMatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    resume_id: int
    application_id: int | None = None
    jd_version_id: int | None = None
    jd_text: str
    result: str
    created_at: datetime


class JDAnalysisOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    application_id: int | None = None
    jd_version_id: int | None = None
    jd_source: str
    jd_text: str
    result: str
    created_at: datetime


class QuestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    application_id: int | None = None
    topic: str = ""
    category: str
    difficulty: str
    question: str
    reference_answer: str
    tags: list[str]
    source_type: str
    status: str
    practice_count: int
    last_practiced_at: datetime | None = None
    next_review_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class QuestionReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    question_id: int
    rating: int
    note: str
    created_at: datetime


class MaterialKitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    application_id: int
    resume_id: int | None = None
    jd_analysis_id: int | None = None
    jd_version_id: int | None = None
    jd_snapshot: str
    status: str
    content_json: str
    created_at: datetime
    updated_at: datetime


class ApplicationEvidenceBundleSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    application_id: int
    sequence: int
    submitted_at: datetime
    confirmed_at: datetime
    confirmation_kind: str
    bundle_sha256: str
    created_at: datetime


class ApplicationEvidenceBundleOut(ApplicationEvidenceBundleSummaryOut):
    snapshot: dict[str, Any]


class MaterialRevisionProposalSummaryOut(BaseModel):
    id: int
    application_id: int
    material_kit_id: int
    jd_version_id: int | None = None
    source_resume_id: int | None
    status: Literal["draft", "accepted", "rejected"]
    summary: str
    proposal_sha256: str
    result_resume_id: int | None
    created_at: datetime


class MaterialRevisionProposalOut(MaterialRevisionProposalSummaryOut):
    changes: list[dict[str, Any]]
    source: dict[str, Any]
    accepted_change_ids: list[str]
    accepted_at: datetime | None
    rejected_at: datetime | None


class OpportunityFitEvidenceRefOut(BaseModel):
    source: Literal["jd", "resume", "user_assertion"]
    path: str
    excerpt: str


class OpportunityFitSummaryOut(BaseModel):
    text: str
    evidence_refs: list[OpportunityFitEvidenceRefOut]


class OpportunityFitReviewSummaryOut(BaseModel):
    id: int
    schema_version: Literal[1] = 1
    application_id: int
    resume_id: int | None
    status: Literal["triage_complete", "deep_reviewed"]
    summary: OpportunityFitSummaryOut
    recommendation: Literal["advance", "hold", "decline"]
    source_fingerprint_sha256: str
    triage_sha256: str
    deep_review_sha256: str | None
    created_at: datetime
    deep_reviewed_at: datetime | None


class InterviewReviewProposalOut(BaseModel):
    id: int
    note_id: int | None
    application_event_id: int | None = None
    proposal_schema_version: Literal[1, 2]
    source_note_revision: int | None
    source_fingerprint: str
    source_status: Literal["current", "source_changed"]
    proposal: dict[str, Any]
    proposal_hash: str
    created_at: datetime | str


class ProductActionProposalOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    operation_id: str | None
    action_call_id: str | None
    action_name: Literal["save_review_readiness_signal", "confirm_interview_story"]
    status: Literal[
        "proposed",
        "rejected",
        "committed",
        "failed",
        "already_confirmed",
    ]
    created: bool
    replayed: bool
    confirmation_token: str | None = None
    result: dict[str, Any] | None = None


class ProductActionDecisionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    operation_id: str
    action_name: Literal["save_review_readiness_signal", "confirm_interview_story"]
    status: Literal["rejected", "committed", "failed"]
    result: dict[str, Any]
    replayed: bool
    direct_commit: bool


class ProductActionStateOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    operation_id: str
    action_name: Literal["save_review_readiness_signal", "confirm_interview_story"]
    status: Literal["proposed", "rejected", "committed", "failed"]
    result: dict[str, Any] | None = None


class ProductActionRecoveryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    operation_id: str
    action_call_id: str
    action_name: Literal["save_review_readiness_signal", "confirm_interview_story"]
    status: Literal["proposed"]
    confirmation_token: str
    allowed_decisions: list[Literal["approve", "modify", "reject"]]
    rejection_only: bool
    live_source_state: Literal["current", "not_observed"]


class ProductActionErrorOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error_code: str
    retryable: bool = False


class AdaptivePracticeStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: int
    focus_id: str
    expected_source_fingerprint: str
    idempotency_key: str


class AdaptivePracticeCompleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int
    response_text: str
    reflection_text: str = ""
    self_assessment: Literal["needs_work", "clearer", "confident"]
    idempotency_key: str


class AdaptivePracticePlanOut(BaseModel):
    id: int
    application_id: int
    application_event_id: int
    interview_note_id: int
    proposal_id: int
    focus_id: str
    company_name: str
    position_name: str
    drill_kind: str
    title: str
    observation: str
    reason: str
    prompt: str
    source_path: str
    source_excerpt: str
    source_fingerprint: str
    source_status: Literal["current", "changed", "missing"]
    status: Literal["in_progress", "completed"]
    revision: int
    response_text: str
    reflection_text: str
    self_assessment: str
    created_at: str
    completed_at: str | None


class VoiceCoachingFillerOccurrenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str = Field(min_length=1, max_length=20)
    count: int = Field(ge=1, le=100)
    transcript_offsets: list[int] = Field(max_length=100)


class VoiceCoachingSnapshotCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    idempotency_key: str = Field(min_length=16, max_length=128)
    total_duration_ms: int = Field(ge=1, le=299_000)
    voiced_duration_ms: int = Field(ge=0, le=299_000)
    pause_count: int = Field(ge=0, le=300)
    longest_pause_ms: int = Field(ge=0, le=299_000)
    speech_rate_cpm: int | None = Field(default=None, ge=1, le=1_000)
    filler_occurrences: list[VoiceCoachingFillerOccurrenceIn] = Field(max_length=20)
    reflection_text: str = Field(max_length=1_000)
    focus_kind: Literal[
        "long_pause_control", "filler_reduction", "pace_consistency"
    ] | None = None
    origin_snapshot_id: int | None = Field(default=None, ge=1)


class VoiceCoachingSnapshotOut(BaseModel):
    id: int
    attempt_id: int
    turn_id: int
    application_id: int
    event_id: int
    question_text: str
    confirmed_answer_text: str
    answer_sha256: str
    measurement_source: Literal["local_browser_measurement"]
    total_duration_ms: int
    voiced_duration_ms: int
    pause_count: int
    longest_pause_ms: int
    speech_rate_cpm: int | None
    filler_occurrences: list[dict[str, Any]]
    reflection_text: str
    focus_kind: str | None
    origin_snapshot_id: int | None
    created_at: datetime | str
    source_available: bool
    company_name: str
    position_name: str


class VoiceCoachingSnapshotListOut(BaseModel):
    items: list[VoiceCoachingSnapshotOut]


class VoiceCoachingTrendOut(BaseModel):
    snapshot_count: int
    window_size: int
    metrics: dict[str, dict[str, Any]]
    recommendation: dict[str, Any] | None


class InterviewPreparationProposalOut(BaseModel):
    id: int
    application_id: int
    event_id: int
    resume_id: int
    attempt_status: Literal["ready"]
    proposal_status: Literal["normal", "safe_empty"]
    source_fingerprint: str
    source_status: Literal["current", "source_changed", "not_checked"]
    source_states: dict[str, str]
    proposal: dict[str, Any]
    proposal_hash: str
    input_snapshot: dict[str, Any]
    created_at: datetime | str


class InterviewPreparationPendingOut(BaseModel):
    attempt_status: Literal["generating", "provider_unknown"]
    application_id: int
    event_id: int
    idempotency_key: str
    generation_revision: int
    retry_after_ms: int


class InterviewStoryEvidenceLinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_kind: Literal["title", "block", "capability_label", "applicable_question"]
    target_id: str
    source_kind: Literal["resume_version", "interview_note", "mock_turn", "user_assertion"]
    source_stable_id: str
    source_version_or_snapshot: str
    source_path: str
    excerpt: str
    text_location: str = ""


class InterviewStoryOut(BaseModel):
    id: int
    title: str
    status: Literal["active", "archived"]
    current_version_id: int | None
    story_revision: int
    version_number: int | None
    source_states: list[dict[str, Any]]
    version: dict[str, Any] | None = None


class InterviewStoryAttemptPendingOut(BaseModel):
    id: int
    attempt_status: Literal["generating", "provider_unknown"]
    generation_revision: int
    source_fingerprint: str
    retry_after_ms: int = 1000


class OpportunityFitReviewOut(OpportunityFitReviewSummaryOut):
    source: dict[str, Any]
    triage: dict[str, Any]
    deep_review: dict[str, Any] | None


class EvidenceBundlePreviewOut(BaseModel):
    application_id: int
    ready: bool
    issues: list[str]
    bundle_sha256: str | None = None
    sources: dict[str, Any]


class KnowledgeSourceOut(BaseModel):
    id: int
    source_kind: str
    display_title: str
    title_hint: str
    main_filename: str
    main_media_type: str
    total_bytes: int
    token_count: int
    lifecycle: str
    extraction_status: str
    extraction_error_code: str
    extraction_error_message: str
    brief_status: str
    brief_block_reason: str
    brief_error_code: str
    brief_error_message: str
    active_snapshot_id: int | None = None
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class KnowledgeOriginOut(BaseModel):
    id: int
    source_id: int
    import_method: str
    original_filename: str
    origin_url: str
    imported_at: datetime


class KnowledgeJobOut(BaseModel):
    id: int
    kind: str
    queue: str
    source_id: int | None = None
    snapshot_id: int | None = None
    stage: str
    status: str
    progress: int
    retry_count: int
    error_code: str
    error_message: str
    canceled: bool
    created_at: datetime
    updated_at: datetime


class KnowledgeEvidenceOut(BaseModel):
    id: str
    source_id: int
    snapshot_id: int
    kind: str
    block_kind: str
    ordinal: int
    heading_path: list[str]
    char_start: int
    char_end: int
    line_start: int
    line_end: int
    canonical_excerpt: str
    search_text: str
    content_hash: str
    asset_id: int | None = None
    previous_evidence_id: str | None = None
    next_evidence_id: str | None = None


class KnowledgeEvidenceSearchHitOut(BaseModel):
    evidence_id: str
    source_id: int
    snapshot_id: int
    block_kind: str
    heading_path: list[str]
    char_start: int
    char_end: int
    line_start: int
    line_end: int
    canonical_excerpt: str
    snippet: str
    score: float


class KnowledgeIngestResponse(BaseModel):
    deduplicated: bool
    source: KnowledgeSourceOut
    job: KnowledgeJobOut
    extraction_error_code: str = ""
    extraction_error_message: str = ""


RESUME_COMPLETION_SECTIONS = (
    "career_intent",
    "contact",
    "education",
    "experience",
    "projects",
    "skills",
)


def normalize_resume_content(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def resume_completion(content: dict[str, Any]) -> tuple[int, list[str], bool]:
    missing = [
        section
        for section in RESUME_COMPLETION_SECTIONS
        if not _resume_section_present(section, content.get(section))
    ]
    present_count = len(RESUME_COMPLETION_SECTIONS) - len(missing)
    completion_percent = round(present_count / len(RESUME_COMPLETION_SECTIONS) * 100)
    return completion_percent, missing, not missing


def resume_payload(resume: Any) -> dict[str, Any]:
    payload = ResumeOut.model_validate(resume).model_dump(mode="json")
    title = payload.get("title") or payload.get("name") or ""
    source_file_path = payload.get("source_file_path") or payload.get("file_path") or ""
    payload["title"] = title
    payload["name"] = payload.get("name") or title
    payload["source_file_path"] = source_file_path
    payload["file_path"] = payload.get("file_path") or source_file_path
    content = normalize_resume_content(payload.get("content_json"))
    if not content and payload.get("parsed_data"):
        content = {"raw_text": payload["parsed_data"]}
    payload["content_json"] = content
    completion_percent, missing_sections, is_complete = resume_completion(content)
    payload["completion_percent"] = completion_percent
    payload["missing_sections"] = missing_sections
    payload["is_complete"] = is_complete
    return payload


def _resume_section_present(section: str, value: Any) -> bool:
    if section == "career_intent":
        if not isinstance(value, dict):
            return False
        roles = value.get("target_roles")
        return isinstance(roles, list) and any(str(role).strip() for role in roles)
    if isinstance(value, list):
        return any(_non_empty(item) for item in value)
    return _non_empty(value)


def _non_empty(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_non_empty(item) for item in value.values())
    if isinstance(value, list):
        return any(_non_empty(item) for item in value)
    return bool(str(value or "").strip())
