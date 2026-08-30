from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _sqlite_uuid_check(column: str) -> str:
    return (
        f"length({column}) = 36 AND lower({column}) = {column} "
        f"AND substr({column}, 9, 1) = '-' AND substr({column}, 14, 1) = '-' "
        f"AND substr({column}, 19, 1) = '-' AND substr({column}, 24, 1) = '-' "
        f"AND length(replace({column}, '-', '')) = 32 "
        f"AND {column} NOT GLOB '*[^0-9a-f-]*'"
    )


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (Index("idx_applications_status", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    company_name: Mapped[str] = mapped_column(String, nullable=False)
    position_name: Mapped[str] = mapped_column(String, nullable=False)
    job_url: Mapped[str] = mapped_column(String, default="", server_default="")
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="applied", server_default="applied"
    )
    source: Mapped[str] = mapped_column(String, nullable=False, default="cli", server_default="cli")
    notes: Mapped[str] = mapped_column(String, default="", server_default="")
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )
    first_pending_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_written_test_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_interview_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_offer_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_reason: Mapped[str] = mapped_column(String, default="", server_default="")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class ApplicationJDVersion(Base):
    __tablename__ = "application_jd_versions"
    __table_args__ = (
        UniqueConstraint("application_id", "version_number"),
        UniqueConstraint("application_id", "idempotency_key"),
        Index("idx_application_jd_versions_app_version", "application_id", "version_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    jd_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String, nullable=False)
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    source_kind: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    request_fingerprint_sha256: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class ApplicationSubmissionSnapshot(Base):
    __tablename__ = "application_submission_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "application_id",
            "idempotency_key",
            name="uq_application_submission_snapshots_application_key",
        ),
        Index("idx_application_submission_snapshots_app", "application_id", "submitted_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    resume_id: Mapped[int] = mapped_column(
        ForeignKey("resumes.id", ondelete="RESTRICT"), nullable=False
    )
    jd_version_id: Mapped[int] = mapped_column(
        ForeignKey("application_jd_versions.id", ondelete="RESTRICT"), nullable=False
    )
    material_kit_id: Mapped[int | None] = mapped_column(
        ForeignKey("application_material_kits.id", ondelete="RESTRICT"), nullable=True
    )
    resume_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    resume_snapshot_hash: Mapped[str] = mapped_column(String, nullable=False)
    jd_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    jd_snapshot_hash: Mapped[str] = mapped_column(String, nullable=False)
    material_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    material_snapshot_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    source_kind: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    request_fingerprint_sha256: Mapped[str] = mapped_column(String, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class ApplicationOutcome(Base):
    __tablename__ = "application_outcomes"
    __table_args__ = (
        UniqueConstraint(
            "application_id",
            "idempotency_key",
            name="uq_application_outcomes_application_key",
        ),
        Index("idx_application_outcomes_app_occurred", "application_id", "occurred_at"),
        Index("idx_application_outcomes_snapshot", "submission_snapshot_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    submission_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("application_submission_snapshots.id", ondelete="RESTRICT"), nullable=False
    )
    application_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("application_events.id", ondelete="SET NULL"), nullable=True
    )
    stage: Mapped[str] = mapped_column(String, nullable=False)
    result: Mapped[str] = mapped_column(String, nullable=False)
    feedback_text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    reflection_text: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    next_action_text: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    feedback_tags_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]"
    )
    source_kind: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    request_fingerprint_sha256: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class ApplicationEvent(Base):
    __tablename__ = "application_events"
    __table_args__ = (
        Index("idx_application_events_app", "application_id"),
        Index("idx_application_events_type", "event_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    subtype: Mapped[str] = mapped_column(String, default="", server_default="")
    _tags: Mapped[str] = mapped_column("tags", String, default="[]", server_default="[]")
    round: Mapped[int] = mapped_column(default=0, server_default="0")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_minutes: Mapped[int] = mapped_column(default=0, server_default="0")
    location: Mapped[str] = mapped_column(String, default="", server_default="")
    notes: Mapped[str] = mapped_column(String, default="", server_default="")
    remind_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String, default="todo", server_default="todo")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )

    @property
    def tags(self) -> list[str]:
        if not self._tags:
            return []
        value = json.loads(self._tags)
        return value if isinstance(value, list) else []

    @tags.setter
    def tags(self, value: list[str]) -> None:
        self._tags = json.dumps(value or [], ensure_ascii=False)


class InterviewNote(Base):
    __tablename__ = "interview_notes"
    __table_args__ = (
        CheckConstraint(
            "typeof(content_revision) = 'integer' AND content_revision >= 1",
            name="ck_interview_notes_content_revision",
        ),
        Index("idx_notes_app", "application_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"),
        nullable=True,
    )
    application_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("application_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    company: Mapped[str] = mapped_column(String, nullable=False)
    position: Mapped[str] = mapped_column(String, nullable=False)
    round: Mapped[str] = mapped_column(String, default="", server_default="")
    date: Mapped[str] = mapped_column(String, default="", server_default="")
    questions: Mapped[str] = mapped_column(String, default="", server_default="")
    self_reflection: Mapped[str] = mapped_column(String, default="", server_default="")
    difficulty_points: Mapped[str] = mapped_column(String, default="", server_default="")
    mood: Mapped[str] = mapped_column(String, default="", server_default="")
    content_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class Offer(Base):
    __tablename__ = "offers"
    __table_args__ = (
        Index("idx_offers_app", "application_id"),
        Index("idx_offers_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"),
        nullable=True,
    )
    company_name: Mapped[str] = mapped_column(String, nullable=False)
    position_name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending", server_default="pending")
    base_monthly: Mapped[int] = mapped_column(default=0, server_default="0")
    months_per_year: Mapped[int] = mapped_column(default=12, server_default="12")
    signing_bonus: Mapped[int] = mapped_column(default=0, server_default="0")
    equity: Mapped[str] = mapped_column(String, default="", server_default="")
    perks: Mapped[str] = mapped_column(String, default="", server_default="")
    deadline: Mapped[str] = mapped_column(String, default="", server_default="")
    notes: Mapped[str] = mapped_column(String, default="", server_default="")
    assessment: Mapped[str] = mapped_column(String, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    @property
    def total_cash(self) -> int:
        return self.base_monthly * self.months_per_year + self.signing_bonus


class OfferComparisonDimension(Base):
    __tablename__ = "offer_comparison_dimensions"
    __table_args__ = (Index("idx_offer_comparison_dimensions_active", "archived_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class OfferComparisonValue(Base):
    __tablename__ = "offer_comparison_values"
    __table_args__ = (
        UniqueConstraint(
            "offer_id", "dimension_id", name="uq_offer_comparison_values_offer_dimension"
        ),
        Index("idx_offer_comparison_values_offer", "offer_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    offer_id: Mapped[int] = mapped_column(
        ForeignKey("offers.id", ondelete="CASCADE"), nullable=False
    )
    dimension_id: Mapped[int] = mapped_column(
        ForeignKey("offer_comparison_dimensions.id", ondelete="CASCADE"), nullable=False
    )
    value_text: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class OfferNegotiationProposal(Base):
    __tablename__ = "offer_negotiation_proposals"
    __table_args__ = (
        UniqueConstraint(
            "offer_id", "idempotency_key", name="uq_offer_negotiation_proposals_offer_key"
        ),
        Index("idx_offer_negotiation_proposals_offer", "offer_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    offer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    application_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    attempt_status: Mapped[str] = mapped_column(
        String, nullable=False, default="generating", server_default="generating"
    )
    source_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    input_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    proposal_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposal_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    source_states_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )
    provider_call_token: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    invalidation_reason: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OfferNegotiationBrief(Base):
    __tablename__ = "offer_negotiation_briefs"
    __table_args__ = (
        UniqueConstraint("proposal_id", name="uq_offer_negotiation_briefs_proposal"),
        Index("idx_offer_negotiation_briefs_offer", "offer_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    proposal_id: Mapped[int] = mapped_column(Integer, nullable=False)
    offer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    origin_application_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmation_key: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    selected_blocks_json: Mapped[str] = mapped_column(Text, nullable=False)
    edited_content_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False, default="", server_default="")
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class Resume(Base):
    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, default="", server_default="")
    file_path: Mapped[str] = mapped_column(String, default="", server_default="")
    parsed_data: Mapped[str] = mapped_column(String, default="", server_default="")
    parse_status: Mapped[str] = mapped_column(String, default="pending", server_default="pending")
    title: Mapped[str] = mapped_column(String, default="", server_default="")
    is_master: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    parent_resume_id: Mapped[int | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL"),
        nullable=True,
    )
    source: Mapped[str] = mapped_column(String, default="manual", server_default="manual")
    source_file_path: Mapped[str] = mapped_column(String, default="", server_default="")
    content_json: Mapped[str] = mapped_column(String, default="{}", server_default="{}")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class ResumeMatch(Base):
    __tablename__ = "resume_matches"
    __table_args__ = (Index("idx_matches_resume", "resume_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    resume_id: Mapped[int] = mapped_column(
        ForeignKey("resumes.id", ondelete="CASCADE"),
        nullable=False,
    )
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"),
        nullable=True,
    )
    jd_text: Mapped[str] = mapped_column(String, nullable=False)
    result: Mapped[str] = mapped_column(String, nullable=False)
    jd_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class JDAnalysis(Base):
    __tablename__ = "jd_analyses"
    __table_args__ = (Index("idx_jd_app", "application_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"),
        nullable=True,
    )
    jd_source: Mapped[str] = mapped_column(String, default="text", server_default="text")
    jd_text: Mapped[str] = mapped_column(String, nullable=False)
    result: Mapped[str] = mapped_column(String, nullable=False)
    jd_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class ApplicationMaterialKit(Base):
    __tablename__ = "application_material_kits"
    __table_args__ = (
        Index("idx_material_kits_app", "application_id"),
        Index("idx_material_kits_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    resume_id: Mapped[int | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL"),
        nullable=True,
    )
    jd_analysis_id: Mapped[int | None] = mapped_column(
        ForeignKey("jd_analyses.id", ondelete="SET NULL"),
        nullable=True,
    )
    jd_snapshot: Mapped[str] = mapped_column(String, default="", server_default="")
    jd_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, default="draft", server_default="draft")
    content_json: Mapped[str] = mapped_column(String, default="{}", server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class ApplicationEvidenceBundle(Base):
    __tablename__ = "application_evidence_bundles"
    __table_args__ = (
        UniqueConstraint("application_id", "sequence", name="uq_evidence_bundle_sequence"),
        UniqueConstraint(
            "application_id", "idempotency_key", name="uq_evidence_bundle_idempotency"
        ),
        Index("idx_evidence_bundles_application", "application_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmation_kind: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="user_asserted",
        server_default="user_asserted",
    )
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(String, nullable=False)
    bundle_sha256: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class MaterialRevisionProposal(Base):
    __tablename__ = "material_revision_proposals"
    __table_args__ = (
        Index(
            "idx_material_revision_proposals_application_created", "application_id", "created_at"
        ),
        UniqueConstraint("result_resume_id", name="uq_material_revision_proposals_result_resume"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    material_kit_id: Mapped[int] = mapped_column(
        ForeignKey("application_material_kits.id", ondelete="CASCADE"), nullable=False
    )
    source_resume_id: Mapped[int | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True
    )
    source_fingerprint_sha256: Mapped[str] = mapped_column(String, nullable=False)
    source_snapshot_json: Mapped[str] = mapped_column(String, nullable=False)
    jd_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    proposal_json: Mapped[str] = mapped_column(String, nullable=False)
    proposal_sha256: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="draft", server_default="draft"
    )
    accepted_change_ids_json: Mapped[str] = mapped_column(
        String, nullable=False, default="[]", server_default="[]"
    )
    result_resume_id: Mapped[int | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class OpportunityFitReview(Base):
    __tablename__ = "opportunity_fit_reviews"
    __table_args__ = (
        UniqueConstraint(
            "application_id",
            "idempotency_key",
            name="uq_opportunity_fit_reviews_application_idempotency",
        ),
        Index("idx_opportunity_fit_reviews_application_created", "application_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    resume_id: Mapped[int | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    proposal_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    source_fingerprint_sha256: Mapped[str] = mapped_column(String, nullable=False)
    source_snapshot_json: Mapped[str] = mapped_column(String, nullable=False)
    triage_json: Mapped[str] = mapped_column(String, nullable=False)
    triage_sha256: Mapped[str] = mapped_column(String, nullable=False)
    deep_review_json: Mapped[str | None] = mapped_column(String, nullable=True)
    deep_review_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    deep_reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class OpportunityFitReviewSession(Base):
    __tablename__ = "opportunity_fit_review_sessions"
    __table_args__ = (
        UniqueConstraint(
            "application_id",
            "triage_idempotency_key",
            name="uq_opportunity_fit_sessions_application_triage_key",
        ),
        Index("idx_opportunity_fit_sessions_application_created", "application_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    triage_idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    proposal_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="active", server_default="active"
    )
    jd_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class OpportunityFitReviewStage(Base):
    __tablename__ = "opportunity_fit_review_stages"
    __table_args__ = (
        UniqueConstraint(
            "application_id",
            "stage",
            "idempotency_key",
            name="uq_opportunity_fit_stages_application_stage_key",
        ),
        Index("idx_opportunity_fit_stages_review_created", "review_id", "created_at"),
        Index("idx_opportunity_fit_stages_parent", "parent_triage_stage_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    review_id: Mapped[int] = mapped_column(
        ForeignKey("opportunity_fit_review_sessions.id", ondelete="CASCADE"), nullable=False
    )
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    resume_id: Mapped[int | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL"), nullable=True
    )
    parent_triage_stage_id: Mapped[int | None] = mapped_column(
        ForeignKey("opportunity_fit_review_stages.id", ondelete="RESTRICT"), nullable=True
    )
    stage: Mapped[str] = mapped_column(String, nullable=False)
    proposal_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    source_snapshot_json: Mapped[str] = mapped_column(String, nullable=False)
    source_fingerprint_sha256: Mapped[str] = mapped_column(String, nullable=False)
    proposal_json: Mapped[str] = mapped_column(String, nullable=False)
    jd_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    proposal_sha256: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="ready", server_default="ready"
    )
    stage_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    provider_call_token: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmation_token_hash: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    confirmation_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class InterviewReviewProposal(Base):
    __tablename__ = "interview_review_proposals"
    __table_args__ = (
        UniqueConstraint(
            "note_id",
            "idempotency_key",
            name="uq_interview_review_proposals_note_key",
        ),
        CheckConstraint(
            "(typeof(proposal_schema_version) = 'integer' "
            "AND proposal_schema_version = 1 AND source_note_revision IS NULL) OR "
            "(typeof(proposal_schema_version) = 'integer' "
            "AND proposal_schema_version = 2 "
            "AND typeof(source_note_revision) = 'integer' "
            "AND source_note_revision >= 1)",
            name="ck_interview_review_proposal_source_revision",
        ),
        Index("idx_interview_review_proposals_note", "note_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    note_id: Mapped[int | None] = mapped_column(
        ForeignKey("interview_notes.id", ondelete="SET NULL"),
        nullable=True,
    )
    application_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("application_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    input_snapshot_json: Mapped[str] = mapped_column(String, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    proposal_json: Mapped[str] = mapped_column(String, nullable=False)
    proposal_hash: Mapped[str] = mapped_column(String, nullable=False)
    proposal_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    source_note_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class AdaptivePracticePlan(Base):
    __tablename__ = "adaptive_practice_plans"
    __table_args__ = (
        UniqueConstraint(
            "start_idempotency_key",
            name="uq_adaptive_practice_start_key",
        ),
        UniqueConstraint(
            "completion_idempotency_key",
            name="uq_adaptive_practice_completion_key",
        ),
        Index("idx_adaptive_practice_application", "application_id", "created_at"),
        Index("idx_adaptive_practice_status", "status", "created_at"),
        Index(
            "uq_adaptive_practice_legacy_proposal_focus",
            "interview_review_proposal_id",
            "focus_id",
            unique=True,
            sqlite_where=text("origin_contract = 'legacy_review_focus_v1'"),
        ),
        Index(
            "uq_adaptive_practice_signal_target",
            "readiness_signal_version_id",
            "target_application_event_id",
            unique=True,
            sqlite_where=text("origin_contract = 'confirmed_readiness_signal_v1'"),
        ),
        CheckConstraint(
            "(origin_contract = 'legacy_review_focus_v1' "
            "AND readiness_signal_version_id IS NULL "
            "AND target_application_event_id IS NULL "
            "AND target_fingerprint IS NULL) OR "
            "(origin_contract = 'confirmed_readiness_signal_v1' "
            "AND target_fingerprint IS NOT NULL "
            "AND length(source_fingerprint) = 71 "
            "AND substr(source_fingerprint,1,7) = 'sha256:' "
            "AND substr(source_fingerprint,8) NOT GLOB '*[^0-9a-f]*' "
            "AND length(target_fingerprint) = 71 "
            "AND substr(target_fingerprint,1,7) = 'sha256:' "
            "AND substr(target_fingerprint,8) NOT GLOB '*[^0-9a-f]*')",
            name="ck_adaptive_practice_origin_contract",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(Integer, nullable=False)
    application_event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    interview_note_id: Mapped[int] = mapped_column(Integer, nullable=False)
    interview_review_proposal_id: Mapped[int] = mapped_column(Integer, nullable=False)
    focus_id: Mapped[str] = mapped_column(String, nullable=False)
    start_idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    start_input_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    source_path: Mapped[str] = mapped_column(String, nullable=False)
    source_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    source_hash: Mapped[str] = mapped_column(String, nullable=False)
    drill_kind: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    observation: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="in_progress", server_default="in_progress"
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    response_text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    reflection_text: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    self_assessment: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    completion_idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True)
    completion_fingerprint: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    origin_contract: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="legacy_review_focus_v1",
        server_default="legacy_review_focus_v1",
    )
    readiness_signal_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("interview_readiness_signal_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    target_application_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("application_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    target_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class InterviewPreparationProposal(Base):
    __tablename__ = "interview_preparation_proposals"
    __table_args__ = (
        UniqueConstraint(
            "application_id",
            "application_event_id",
            "idempotency_key",
            name="uq_interview_preparation_application_event_key",
        ),
        Index("idx_interview_preparation_application", "application_id"),
        Index("idx_interview_preparation_event", "application_event_id"),
        Index("idx_interview_preparation_resume", "resume_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # These IDs are immutable snapshot identities, not foreign keys.  Event
    # and Resume deletion must not block deletion or cascade away the audit row.
    application_id: Mapped[int] = mapped_column(Integer, nullable=False)
    application_event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    resume_id: Mapped[int] = mapped_column(Integer, nullable=False)
    jd_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    attempt_status: Mapped[str] = mapped_column(
        String, default="generating", server_default="generating", nullable=False
    )
    proposal_status: Mapped[str] = mapped_column(
        String, default="", server_default="", nullable=False
    )
    generation_revision: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    provider_call_token: Mapped[str] = mapped_column(
        String, default="", server_default="", nullable=False
    )
    provider_lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    invalidation_reason: Mapped[str] = mapped_column(
        String, default="", server_default="", nullable=False
    )
    input_snapshot_json: Mapped[str] = mapped_column(
        Text, default="", server_default="", nullable=False
    )
    source_fingerprint: Mapped[str] = mapped_column(
        String, default="", server_default="", nullable=False
    )
    proposal_json: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    proposal_hash: Mapped[str] = mapped_column(
        String, default="", server_default="", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class InterviewKnowledgeCaptureAttempt(Base):
    __tablename__ = "interview_knowledge_capture_attempts"
    __table_args__ = (
        UniqueConstraint("note_id", "attempt_key", name="uq_interview_capture_note_key"),
        Index("idx_interview_capture_attempt_note", "note_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    note_id: Mapped[int] = mapped_column(
        ForeignKey("interview_notes.id", ondelete="CASCADE"), nullable=False
    )
    attempt_key: Mapped[str] = mapped_column(String, nullable=False)
    note_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    selected_fragments_json: Mapped[str] = mapped_column(Text, nullable=False)
    last_preview_mode: Mapped[str] = mapped_column(
        String, default="direct", server_default="direct"
    )
    preview_status: Mapped[str] = mapped_column(
        String, default="not_requested", server_default="not_requested"
    )
    preview_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    provider_call_token: Mapped[str] = mapped_column(String, default="", server_default="")
    preview_json: Mapped[str] = mapped_column(Text, default="", server_default="")
    preview_error_code: Mapped[str] = mapped_column(String, default="", server_default="")
    confirmed_note_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_note_versions.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class KnowledgeNote(Base):
    __tablename__ = "knowledge_notes"
    __table_args__ = (Index("idx_knowledge_notes_origin", "origin_kind"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String, default="", server_default="")
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_note_versions.id", ondelete="SET NULL"), nullable=True
    )
    origin_kind: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KnowledgeNoteVersion(Base):
    __tablename__ = "knowledge_note_versions"
    __table_args__ = (
        UniqueConstraint("note_id", "version_number", name="uq_knowledge_note_version_number"),
        Index("idx_knowledge_note_versions_note", "note_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    note_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_notes.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    content_origin: Mapped[str] = mapped_column(String, nullable=False)
    capture_attempt_key: Mapped[str] = mapped_column(String, nullable=False)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class KnowledgeNoteEvidence(Base):
    __tablename__ = "knowledge_note_evidence"
    __table_args__ = (
        UniqueConstraint(
            "note_version_id", "block_id", "evidence_id", name="uq_knowledge_note_block_evidence"
        ),
        Index("idx_knowledge_note_evidence_version", "note_version_id"),
    )

    note_version_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_note_versions.id", ondelete="RESTRICT"), primary_key=True
    )
    block_id: Mapped[str] = mapped_column(String, primary_key=True)
    evidence_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_evidence.id", ondelete="RESTRICT"), primary_key=True
    )


class KnowledgeCapturedSourceMetadata(Base):
    __tablename__ = "knowledge_captured_source_metadata"

    source_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"), primary_key=True
    )
    origin_note_id: Mapped[int] = mapped_column(Integer, nullable=False)
    application_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    selected_fragments_json: Mapped[str] = mapped_column(Text, nullable=False)
    capture_schema_version: Mapped[str] = mapped_column(String, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class Question(Base):
    __tablename__ = "questions"
    __table_args__ = (
        Index("idx_questions_topic", "topic"),
        Index("idx_questions_status", "status"),
        Index("idx_questions_next_review", "next_review_at"),
        Index("idx_questions_hash", "question_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    application_id: Mapped[int | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"),
        nullable=True,
    )
    topic: Mapped[str] = mapped_column(String, default="", server_default="")
    category: Mapped[str] = mapped_column(String, default="", server_default="")
    difficulty: Mapped[str] = mapped_column(String, default="medium", server_default="medium")
    question: Mapped[str] = mapped_column(String, nullable=False)
    reference_answer: Mapped[str] = mapped_column(String, default="", server_default="")
    _tags: Mapped[str] = mapped_column("tags", String, default="[]", server_default="[]")
    source_type: Mapped[str] = mapped_column(String, default="manual", server_default="manual")
    status: Mapped[str] = mapped_column(String, default="new", server_default="new")
    practice_count: Mapped[int] = mapped_column(default=0, server_default="0")
    last_practiced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    question_hash: Mapped[str] = mapped_column(String, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    @property
    def tags(self) -> list[str]:
        if not self._tags:
            return []
        value = json.loads(self._tags)
        return value if isinstance(value, list) else []

    @tags.setter
    def tags(self, value: list[str]) -> None:
        self._tags = json.dumps(value or [], ensure_ascii=False)


class QuestionReview(Base):
    __tablename__ = "question_reviews"
    __table_args__ = (Index("idx_question_reviews_question", "question_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"),
        nullable=False,
    )
    rating: Mapped[int] = mapped_column(nullable=False)
    note: Mapped[str] = mapped_column(String, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class InterviewPracticeCase(Base):
    __tablename__ = "interview_practice_cases"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_interview_practice_cases_key"),
        Index("idx_interview_practice_cases_status", "status", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    request_fingerprint_sha256: Mapped[str] = mapped_column(String, nullable=False)
    position_name_snapshot: Mapped[str] = mapped_column(String, nullable=False)
    jd_text_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    jd_fingerprint_sha256: Mapped[str] = mapped_column(String, nullable=False)
    resume_id: Mapped[int] = mapped_column(
        ForeignKey("resumes.id", ondelete="RESTRICT"), nullable=False
    )
    resume_content_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    resume_fingerprint_sha256: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="active", server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MockInterviewAttempt(Base):
    __tablename__ = "mock_interview_attempts"
    __table_args__ = (
        UniqueConstraint(
            "context_kind",
            "application_id",
            "event_id",
            "practice_case_id",
            "idempotency_key",
            name="uq_mock_interview_attempts_context_key",
        ),
        CheckConstraint(
            "(context_kind = 'application_event' AND application_id IS NOT NULL AND event_id IS NOT NULL AND practice_case_id IS NULL) "
            "OR (context_kind = 'quick_practice' AND application_id IS NULL AND event_id IS NULL AND practice_case_id IS NOT NULL)",
            name="ck_mock_interview_attempt_context",
        ),
        Index("idx_mock_interview_attempts_event", "application_id", "event_id"),
        Index("idx_mock_interview_attempts_context", "context_kind", "practice_case_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    context_kind: Mapped[str] = mapped_column(
        String, nullable=False, default="application_event", server_default="application_event"
    )
    application_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    practice_case_id: Mapped[int | None] = mapped_column(
        ForeignKey("interview_practice_cases.id", ondelete="RESTRICT"), nullable=True
    )
    resume_id: Mapped[int] = mapped_column(Integer, nullable=False)
    jd_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    input_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    attempt_status: Mapped[str] = mapped_column(String, nullable=False)
    generation_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    provider_call_token: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    provider_lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_turn_no: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    transcript_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    failure_category: Mapped[str] = mapped_column(String, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MockInterviewTurn(Base):
    __tablename__ = "mock_interview_turns"
    __table_args__ = (
        UniqueConstraint("attempt_id", "turn_no"),
        UniqueConstraint("attempt_id", "turn_no", "turn_idempotency_key"),
        UniqueConstraint("attempt_id", "turn_no", "question_idempotency_key"),
        Index("idx_mock_interview_turns_attempt", "attempt_id", "turn_no"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    attempt_id: Mapped[int] = mapped_column(Integer, nullable=False)
    turn_no: Mapped[int] = mapped_column(Integer, nullable=False)
    question_idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    turn_idempotency_key: Mapped[str] = mapped_column(String, default="", server_default="")
    question_text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    answer_text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    question_source_snapshot_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )
    answer_sha256: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    turn_status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class VoiceCoachingSnapshot(Base):
    __tablename__ = "voice_coaching_snapshots"
    __table_args__ = (
        UniqueConstraint("turn_id", name="uq_voice_coaching_snapshots_turn"),
        UniqueConstraint("idempotency_key", name="uq_voice_coaching_snapshots_key"),
        Index("idx_voice_coaching_snapshots_created", "created_at", "id"),
        Index("idx_voice_coaching_snapshots_application_event", "application_id", "event_id"),
        Index("idx_voice_coaching_snapshots_attempt", "attempt_id"),
        Index("idx_voice_coaching_snapshots_context", "context_kind", "practice_case_id"),
        CheckConstraint(
            "(context_kind = 'application_event' AND application_id IS NOT NULL AND event_id IS NOT NULL AND practice_case_id IS NULL) "
            "OR (context_kind = 'quick_practice' AND application_id IS NULL AND event_id IS NULL AND practice_case_id IS NOT NULL)",
            name="ck_voice_coaching_snapshot_context",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("mock_interview_attempts.id", ondelete="CASCADE"), nullable=False
    )
    turn_id: Mapped[int] = mapped_column(
        ForeignKey("mock_interview_turns.id", ondelete="CASCADE"), nullable=False
    )
    context_kind: Mapped[str] = mapped_column(
        String, nullable=False, default="application_event", server_default="application_event"
    )
    application_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    practice_case_id: Mapped[int | None] = mapped_column(
        ForeignKey("interview_practice_cases.id", ondelete="RESTRICT"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    request_fingerprint_sha256: Mapped[str] = mapped_column(String, nullable=False)
    question_text_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    confirmed_answer_text_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    answer_sha256: Mapped[str] = mapped_column(String, nullable=False)
    measurement_source: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="local_browser_measurement",
        server_default="local_browser_measurement",
    )
    total_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    voiced_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    pause_count: Mapped[int] = mapped_column(Integer, nullable=False)
    longest_pause_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    speech_rate_cpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    filler_occurrences_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]"
    )
    reflection_text: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    focus_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    origin_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("voice_coaching_snapshots.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class MockInterviewFeedbackProposal(Base):
    __tablename__ = "mock_interview_feedback_proposals"
    __table_args__ = (
        UniqueConstraint("attempt_id", "idempotency_key"),
        Index("idx_mock_interview_feedback_attempt", "attempt_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    attempt_id: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    input_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    transcript_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    proposal_json: Mapped[str] = mapped_column(Text, nullable=False)
    proposal_hash: Mapped[str] = mapped_column(String, nullable=False)
    proposal_status: Mapped[str] = mapped_column(String, nullable=False)
    failure_category: Mapped[str] = mapped_column(String, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class MockInterviewReviewDraft(Base):
    __tablename__ = "mock_interview_review_drafts"
    __table_args__ = (
        UniqueConstraint("proposal_id"),
        Index("idx_mock_interview_review_drafts_attempt", "attempt_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    attempt_id: Mapped[int] = mapped_column(Integer, nullable=False)
    proposal_id: Mapped[int] = mapped_column(Integer, nullable=False)
    confirmation_idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    application_id: Mapped[int] = mapped_column(Integer, nullable=False)
    event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_blocks_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="confirmed", server_default="confirmed"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class InterviewStory(Base):
    __tablename__ = "interview_stories"
    __table_args__ = (Index("idx_interview_stories_status", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="active", server_default="active"
    )
    # The current pointer intentionally is not an FK: Story/Version writes validate
    # ownership in the repository and Story has no physical-delete operation.
    current_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    story_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InterviewStoryVersion(Base):
    __tablename__ = "interview_story_versions"
    __table_args__ = (
        UniqueConstraint(
            "story_id", "version_number", name="uq_interview_story_versions_story_number"
        ),
        Index("idx_interview_story_versions_story", "story_id", "version_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    story_id: Mapped[int] = mapped_column(
        ForeignKey("interview_stories.id", ondelete="RESTRICT"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    origin_kind: Mapped[str] = mapped_column(String, nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class InterviewStoryVersionEvidenceLink(Base):
    __tablename__ = "interview_story_version_evidence_links"
    __table_args__ = (
        UniqueConstraint(
            "story_version_id",
            "target_kind",
            "target_id",
            "source_kind",
            "source_stable_id",
            "source_version_or_snapshot",
            "source_path",
            "text_location",
            "excerpt",
            name="uq_interview_story_evidence_link_identity",
        ),
        Index("idx_interview_story_evidence_version", "story_version_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    story_version_id: Mapped[int] = mapped_column(
        ForeignKey("interview_story_versions.id", ondelete="RESTRICT"), nullable=False
    )
    target_kind: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[str] = mapped_column(String, nullable=False)
    source_kind: Mapped[str] = mapped_column(String, nullable=False)
    source_stable_id: Mapped[str] = mapped_column(String, nullable=False)
    source_version_or_snapshot: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    source_path: Mapped[str] = mapped_column(String, nullable=False)
    text_location: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    link_hash: Mapped[str] = mapped_column(String, nullable=False)


class InterviewStoryUserAssertion(Base):
    __tablename__ = "interview_story_user_assertions"
    __table_args__ = (
        UniqueConstraint(
            "story_version_id", "statement_hash", name="uq_interview_story_assertion_hash"
        ),
        Index("idx_interview_story_assertions_version", "story_version_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    story_version_id: Mapped[int] = mapped_column(
        ForeignKey("interview_story_versions.id", ondelete="RESTRICT"), nullable=False
    )
    statement_text: Mapped[str] = mapped_column(Text, nullable=False)
    statement_hash: Mapped[str] = mapped_column(String, nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class InterviewStoryProposalAttempt(Base):
    __tablename__ = "interview_story_proposal_attempts"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_interview_story_attempt_key"),
        CheckConstraint(
            "typeof(product_action_generation) = 'integer' "
            "AND product_action_generation >= 0 "
            "AND ((product_action_generation = 0 AND product_action_operation_id IS NULL) "
            "OR (product_action_generation >= 1 AND product_action_operation_id IS NOT NULL))",
            name="ck_interview_story_product_action_generation",
        ),
        Index(
            "uq_interview_story_attempt_product_action_operation",
            "product_action_operation_id",
            unique=True,
            sqlite_where=text("product_action_operation_id IS NOT NULL"),
        ),
        Index("idx_interview_story_attempt_target", "target_story_id"),
        Index("idx_interview_story_attempt_status", "attempt_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # Target/confirmed identities remain plain values. Story has no physical delete
    # in phase one and historical Attempts must remain readable if that changes later.
    target_story_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    entrypoint: Mapped[str] = mapped_column(String, nullable=False)
    entry_context_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )
    attempt_status: Mapped[str] = mapped_column(String, nullable=False)
    generation_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    provider_call_token: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    provider_lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    input_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    proposal_json: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    proposal_hash: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    # Bounded internal JSON-format repair count.  This is execution metadata,
    # not model text; it lets the browser acceptance audit attribute a second
    # Provider connection to the one allowed repair instead of inferring it.
    repair_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    failure_category: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    confirmation_token_hash: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    confirmation_payload_hash: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    confirmed_story_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmed_story_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    product_action_operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("write_operations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    product_action_generation: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class ProductActionProposal(Base):
    __tablename__ = "product_action_proposals"
    __table_args__ = (
        CheckConstraint(_sqlite_uuid_check("operation_id"), name="ck_product_action_operation_uuid"),
        CheckConstraint(_sqlite_uuid_check("action_call_id"), name="ck_product_action_call_uuid"),
        CheckConstraint(
            "action_name IN ('confirm_interview_story','save_review_readiness_signal')",
            name="ck_product_action_name",
        ),
        CheckConstraint(
            "request_origin IN ('current','historical_story_bridge')",
            name="ck_product_action_origin",
        ),
        CheckConstraint(
            "typeof(schema_version) = 'integer' AND schema_version = 1",
            name="ck_product_action_schema_version",
        ),
        CheckConstraint(
            "typeof(source_id) = 'integer' AND source_id > 0 "
            "AND typeof(source_revision) = 'integer' AND source_revision > 0",
            name="ck_product_action_source_revision",
        ),
        CheckConstraint(
            "(action_name = 'confirm_interview_story' AND source_kind = 'story_proposal') OR "
            "(action_name = 'save_review_readiness_signal' AND source_kind = 'review_focus')",
            name="ck_product_action_source_mapping",
        ),
        CheckConstraint(
            "request_origin = 'current' OR action_name = 'confirm_interview_story'",
            name="ck_product_action_historical_origin",
        ),
        CheckConstraint(
            "(action_name = 'save_review_readiness_signal' "
            "AND semantic_claim_fingerprint IS NOT NULL) OR "
            "(action_name = 'confirm_interview_story' "
            "AND semantic_claim_fingerprint IS NULL)",
            name="ck_product_action_semantic_claim",
        ),
        CheckConstraint(
            "(request_origin = 'historical_story_bridge' "
            "AND historical_request_token_fingerprint IS NOT NULL) OR "
            "(request_origin = 'current' "
            "AND historical_request_token_fingerprint IS NULL)",
            name="ck_product_action_historical_request",
        ),
        CheckConstraint(
            "(route_payload_json IS NOT NULL AND terminalized_at IS NULL "
            "AND json_valid(route_payload_json) = 1 "
            "AND json_type(route_payload_json) = 'object' "
            "AND length(CAST(route_payload_json AS BLOB)) <= 16384) OR "
            "(route_payload_json IS NULL AND terminalized_at IS NOT NULL)",
            name="ck_product_action_route_lifecycle",
        ),
        CheckConstraint(
            "length(route_payload_fingerprint) = 76 "
            "AND substr(route_payload_fingerprint,1,12) = 'hmac-sha256:' "
            "AND substr(route_payload_fingerprint,13) NOT GLOB '*[^0-9a-f]*' "
            "AND length(route_binding_fingerprint) = 76 "
            "AND substr(route_binding_fingerprint,1,12) = 'hmac-sha256:' "
            "AND substr(route_binding_fingerprint,13) NOT GLOB '*[^0-9a-f]*' "
            "AND length(request_idempotency_fingerprint) = 76 "
            "AND substr(request_idempotency_fingerprint,1,12) = 'hmac-sha256:' "
            "AND substr(request_idempotency_fingerprint,13) NOT GLOB '*[^0-9a-f]*' "
            "AND (semantic_claim_fingerprint IS NULL OR "
            "(length(semantic_claim_fingerprint) = 76 "
            "AND substr(semantic_claim_fingerprint,1,12) = 'hmac-sha256:' "
            "AND substr(semantic_claim_fingerprint,13) NOT GLOB '*[^0-9a-f]*')) "
            "AND (historical_request_token_fingerprint IS NULL OR "
            "(length(historical_request_token_fingerprint) = 76 "
            "AND substr(historical_request_token_fingerprint,1,12) = 'hmac-sha256:' "
            "AND substr(historical_request_token_fingerprint,13) NOT GLOB '*[^0-9a-f]*'))",
            name="ck_product_action_fingerprints",
        ),
        Index(
            "uq_product_action_active_semantic_claim",
            "semantic_claim_fingerprint",
            unique=True,
            sqlite_where=text(
                "action_name = 'save_review_readiness_signal' AND terminalized_at IS NULL"
            ),
        ),
    )

    operation_id: Mapped[str] = mapped_column(
        ForeignKey("write_operations.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    action_call_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    action_name: Mapped[str] = mapped_column(String, nullable=False)
    request_origin: Mapped[str] = mapped_column(String, nullable=False)
    schema_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    source_kind: Mapped[str] = mapped_column(String, nullable=False)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    route_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    route_payload_fingerprint: Mapped[str] = mapped_column(String(76), nullable=False)
    route_binding_fingerprint: Mapped[str] = mapped_column(String(76), nullable=False)
    request_idempotency_fingerprint: Mapped[str] = mapped_column(
        String(76),
        nullable=False,
        unique=True,
    )
    semantic_claim_fingerprint: Mapped[str | None] = mapped_column(String(76), nullable=True)
    historical_request_token_fingerprint: Mapped[str | None] = mapped_column(
        String(76),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )
    terminalized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class InterviewReadinessSignal(Base):
    __tablename__ = "interview_readiness_signals"
    __table_args__ = (
        CheckConstraint(
            "typeof(revision) = 'integer' AND revision >= 1",
            name="ck_interview_readiness_signal_revision",
        ),
        Index(
            "uq_interview_readiness_signal_source_focus",
            "source_proposal_id",
            "focus_id",
            unique=True,
            sqlite_where=text("source_proposal_id IS NOT NULL"),
        ),
        Index("idx_interview_readiness_signal_application", "application_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("application_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_note_id: Mapped[int | None] = mapped_column(
        ForeignKey("interview_notes.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_proposal_id: Mapped[int | None] = mapped_column(
        ForeignKey("interview_review_proposals.id", ondelete="SET NULL"),
        nullable=True,
    )
    focus_id: Mapped[str] = mapped_column(String, nullable=False)
    current_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class InterviewReadinessSignalVersion(Base):
    __tablename__ = "interview_readiness_signal_versions"
    __table_args__ = (
        CheckConstraint(
            "typeof(version_number) = 'integer' AND version_number >= 1",
            name="ck_interview_readiness_signal_version_number",
        ),
        CheckConstraint(
            "disposition IN ('active','retracted')",
            name="ck_interview_readiness_signal_disposition",
        ),
        CheckConstraint(
            "schema_version = 'readiness-signal-v1'",
            name="ck_interview_readiness_signal_schema",
        ),
        CheckConstraint(
            "typeof(source_note_revision) = 'integer' AND source_note_revision >= 1",
            name="ck_interview_readiness_signal_note_revision",
        ),
        CheckConstraint(
            "length(source_note_fingerprint) = 71 "
            "AND substr(source_note_fingerprint,1,7) = 'sha256:' "
            "AND substr(source_note_fingerprint,8) NOT GLOB '*[^0-9a-f]*' "
            "AND length(source_proposal_hash) = 71 "
            "AND substr(source_proposal_hash,1,7) = 'sha256:' "
            "AND substr(source_proposal_hash,8) NOT GLOB '*[^0-9a-f]*' "
            "AND length(candidate_fingerprint) = 71 "
            "AND substr(candidate_fingerprint,1,7) = 'sha256:' "
            "AND substr(candidate_fingerprint,8) NOT GLOB '*[^0-9a-f]*'",
            name="ck_interview_readiness_signal_hashes",
        ),
        CheckConstraint(
            _sqlite_uuid_check("domain_idempotency_key"),
            name="ck_interview_readiness_signal_domain_key",
        ),
        UniqueConstraint(
            "signal_id",
            "version_number",
            name="uq_interview_readiness_signal_version_number",
        ),
        UniqueConstraint("id", "signal_id", name="uq_interview_readiness_signal_version_owner"),
        ForeignKeyConstraint(
            ["parent_version_id", "signal_id"],
            [
                "interview_readiness_signal_versions.id",
                "interview_readiness_signal_versions.signal_id",
            ],
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
            name="fk_interview_readiness_signal_version_parent",
        ),
        Index("idx_interview_readiness_signal_version_signal", "signal_id", "version_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(
        ForeignKey("interview_readiness_signals.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    disposition: Mapped[str] = mapped_column(String, nullable=False)
    schema_version: Mapped[str] = mapped_column(String, nullable=False)
    statement_text: Mapped[str] = mapped_column(Text, nullable=False)
    user_note: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    source_note_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_note_fingerprint: Mapped[str] = mapped_column(String(71), nullable=False)
    source_proposal_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    candidate_fingerprint: Mapped[str] = mapped_column(String(71), nullable=False)
    domain_idempotency_key: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    write_operation_id: Mapped[str] = mapped_column(
        ForeignKey("write_operations.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class InterviewReadinessSignalEvidence(Base):
    __tablename__ = "interview_readiness_signal_evidence"
    __table_args__ = (
        CheckConstraint(
            "typeof(ordinal) = 'integer' AND ordinal BETWEEN 0 AND 4",
            name="ck_interview_readiness_signal_evidence_ordinal",
        ),
        CheckConstraint(
            "source_path IN ('/questions','/self_reflection','/difficulty_points','/mood')",
            name="ck_interview_readiness_signal_evidence_path",
        ),
        CheckConstraint(
            "length(excerpt_sha256) = 71 "
            "AND substr(excerpt_sha256,1,7) = 'sha256:' "
            "AND substr(excerpt_sha256,8) NOT GLOB '*[^0-9a-f]*' "
            "AND length(source_field_sha256) = 71 "
            "AND substr(source_field_sha256,1,7) = 'sha256:' "
            "AND substr(source_field_sha256,8) NOT GLOB '*[^0-9a-f]*'",
            name="ck_interview_readiness_signal_evidence_hashes",
        ),
        UniqueConstraint(
            "signal_version_id",
            "ordinal",
            name="uq_interview_readiness_signal_evidence_ordinal",
        ),
        Index("idx_interview_readiness_signal_evidence_version", "signal_version_id", "ordinal"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    signal_version_id: Mapped[int] = mapped_column(
        ForeignKey("interview_readiness_signal_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    source_path: Mapped[str] = mapped_column(String, nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    excerpt_sha256: Mapped[str] = mapped_column(String(71), nullable=False)
    source_field_sha256: Mapped[str] = mapped_column(String(71), nullable=False)


# Every model with a direct foreign key to applications.id. Conditional application
# deletion iterates this explicit inventory; a metadata-backed test keeps it exhaustive.
APPLICATION_FOREIGN_KEY_MODELS = (
    ApplicationEvent,
    ApplicationJDVersion,
    ApplicationSubmissionSnapshot,
    ApplicationOutcome,
    InterviewNote,
    InterviewReadinessSignal,
    Offer,
    ResumeMatch,
    JDAnalysis,
    ApplicationMaterialKit,
    ApplicationEvidenceBundle,
    MaterialRevisionProposal,
    OpportunityFitReview,
    OpportunityFitReviewSession,
    OpportunityFitReviewStage,
    Question,
)


class Wakeup(Base):
    __tablename__ = "wakeups"
    __table_args__ = (
        Index("idx_wakeups_status_due", "status", "due_at"),
        Index("idx_wakeups_kind", "kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload_json: Mapped[str] = mapped_column(String, default="{}", server_default="{}")
    status: Mapped[str] = mapped_column(String, default="pending", server_default="pending")
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(
            "typeof(scope_revision) = 'integer' "
            "AND scope_revision BETWEEN 0 AND 9223372036854775807",
            name="ck_conversations_scope_revision",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(
        String, nullable=False, default="新对话", server_default="新对话"
    )
    title_source: Mapped[str] = mapped_column(
        String, nullable=False, default="fallback", server_default="fallback"
    )
    mode: Mapped[str] = mapped_column(String, default="general", server_default="general")
    context_type: Mapped[str] = mapped_column(
        String, default="workspace", server_default="workspace"
    )
    context_ref: Mapped[str] = mapped_column(String, default="", server_default="")
    scope_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pending_tool_call_id: Mapped[str] = mapped_column(String, default="", server_default="")
    pending_operation_id: Mapped[str] = mapped_column(String, default="", server_default="")
    pending_confirmation_claim_id: Mapped[str] = mapped_column(
        String,
        default="",
        server_default="",
    )
    pending_confirmation_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    pending_tool_name: Mapped[str] = mapped_column(String, default="", server_default="")
    pending_args: Mapped[str] = mapped_column(String, default="", server_default="")
    pending_human: Mapped[str] = mapped_column(String, default="", server_default="")
    clarification_tool_call_id: Mapped[str] = mapped_column(String, default="", server_default="")
    clarification_tool_name: Mapped[str] = mapped_column(String, default="", server_default="")
    clarification_args: Mapped[str] = mapped_column(String, default="", server_default="")
    clarification_human: Mapped[str] = mapped_column(String, default="", server_default="")
    clarification_question: Mapped[str] = mapped_column(String, default="", server_default="")
    last_write_undo_json: Mapped[str] = mapped_column(String, default="", server_default="")
    last_write_operation_id: Mapped[str] = mapped_column(String, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    @property
    def pending_action(self) -> dict[str, object] | None:
        if not self.pending_tool_name:
            return None
        try:
            args = json.loads(self.pending_args) if self.pending_args else {}
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        return {
            "tool_name": self.pending_tool_name,
            "human": self.pending_human or self.pending_tool_name,
            "args": args,
        }

    @property
    def pending_clarification(self) -> dict[str, object] | None:
        if not self.clarification_tool_name:
            return None
        try:
            args = json.loads(self.clarification_args) if self.clarification_args else {}
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        return {
            "tool_name": self.clarification_tool_name,
            "human": self.clarification_human or self.clarification_tool_name,
            "args": args,
            "question": self.clarification_question,
        }

    @property
    def last_write_undo(self) -> dict[str, object] | None:
        if not self.last_write_undo_json:
            return None
        try:
            payload = json.loads(self.last_write_undo_json)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint(
            "(operation_id IS NULL AND delivery_kind IS NULL AND delivery_ordinal IS NULL) OR "
            "(operation_id IS NOT NULL AND delivery_kind IS NOT NULL AND delivery_ordinal IS NOT NULL)",
            name="ck_chat_messages_delivery_group",
        ),
        CheckConstraint(
            "operation_id IS NULL OR "
            "(delivery_kind = 'origin_tool_result' AND delivery_ordinal = 0 "
            "AND role = 'tool' AND tool_call_id <> '') OR "
            "(delivery_kind = 'continuation_message' AND delivery_ordinal >= 1 "
            "AND ((role = 'tool' AND tool_call_id <> '') OR "
            "(role = 'assistant' AND tool_call_id = '')))",
            name="ck_chat_messages_delivery_shape",
        ),
        Index("idx_chat_messages_conv", "conversation_id"),
        Index(
            "uq_chat_messages_operation_ordinal",
            "operation_id",
            "delivery_ordinal",
            unique=True,
            sqlite_where=text("operation_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(String, default="", server_default="")
    tool_calls: Mapped[str] = mapped_column(String, default="", server_default="")
    tool_call_id: Mapped[str] = mapped_column(String, default="", server_default="")
    provider_blocks: Mapped[str] = mapped_column(String, default="", server_default="")
    operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("write_operations.id", ondelete="RESTRICT"), nullable=True
    )
    delivery_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    delivery_ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class WriteOperation(Base):
    __tablename__ = "write_operations"
    __table_args__ = (
        CheckConstraint(_sqlite_uuid_check("id"), name="ck_write_operations_id_uuid"),
        CheckConstraint(
            "operation_role IN ('primary','compensation')", name="ck_write_operations_role"
        ),
        CheckConstraint(
            "adapter_kind IN ('typed','legacy_deterministic','compensation','product_action')",
            name="ck_write_operations_adapter",
        ),
        CheckConstraint(
            "status IN ('proposed','rejected','committed','failed')",
            name="ck_write_operations_status",
        ),
        CheckConstraint(
            "delivery_status IN ('pending','completed','failed','not_applicable')",
            name="ck_write_operations_delivery_status",
        ),
        CheckConstraint(
            "(operation_role = 'primary' AND parent_operation_id IS NULL AND parent_terminal_payload_sha256 IS NULL AND tool_call_id IS NOT NULL AND tool_call_id <> '' "
            "AND proposal_fingerprint IS NOT NULL AND confirmation_token_fingerprint IS NOT NULL) OR "
            "(operation_role = 'compensation' AND parent_operation_id IS NOT NULL AND parent_terminal_payload_sha256 IS NOT NULL AND tool_call_id IS NULL "
            "AND proposal_fingerprint IS NULL AND confirmation_token_fingerprint IS NULL)",
            name="ck_write_operations_role_identity",
        ),
        CheckConstraint(
            "(operation_role = 'primary' AND adapter_kind = 'typed' AND tool_name IN "
            "('create_application','update_application_status','create_application_event',"
            "'update_application_event','delete_application_event','add_note','update_note',"
            "'delete_note','update_offer','save_offer_assessment','resume_update_career_intent',"
            "'resume_rewrite_highlight')) OR "
            "(operation_role = 'primary' AND adapter_kind = 'legacy_deterministic' AND tool_name IN "
            "('save_application_jd_version','create_application_submission_snapshot',"
            "'record_application_outcome')) OR "
            "(operation_role = 'primary' AND adapter_kind = 'product_action' AND tool_name IN "
            "('confirm_interview_story','save_review_readiness_signal')) OR "
            "(operation_role = 'compensation' AND adapter_kind = 'compensation' AND tool_name IN "
            "('undo:update_application_status','undo:create_application',"
            "'undo:create_application_event','undo:add_note','undo:confirm_interview_story',"
            "'undo:save_review_readiness_signal'))",
            name="ck_write_operations_manifest",
        ),
        CheckConstraint(
            "adapter_kind <> 'product_action' OR "
            "(operation_role = 'primary' AND conversation_id IS NULL AND agent_run_id IS NULL "
            "AND tool_call_id IS NOT NULL AND proposal_fingerprint IS NOT NULL "
            "AND confirmation_token_fingerprint IS NOT NULL "
            "AND authorization_scope_fingerprint IS NOT NULL)",
            name="ck_write_operations_product_action_primary_shape",
        ),
        CheckConstraint(
            "NOT (operation_role = 'compensation' AND tool_name IN "
            "('undo:confirm_interview_story','undo:save_review_readiness_signal')) OR "
            "(adapter_kind = 'compensation' AND conversation_id IS NULL AND agent_run_id IS NULL "
            "AND tool_call_id IS NULL AND proposal_fingerprint IS NULL "
            "AND confirmation_token_fingerprint IS NULL "
            "AND authorization_scope_fingerprint IS NULL)",
            name="ck_write_operations_product_compensation_shape",
        ),
        CheckConstraint(
            "result_json IS NULL OR length(CAST(result_json AS BLOB)) <= 524288",
            name="ck_write_operations_result_bytes",
        ),
        CheckConstraint(
            "visible_result IS NULL OR length(CAST(visible_result AS BLOB)) <= 262144",
            name="ck_write_operations_visible_bytes",
        ),
        CheckConstraint(
            "transport_json IS NULL OR length(CAST(transport_json AS BLOB)) <= 131072",
            name="ck_write_operations_transport_bytes",
        ),
        CheckConstraint(
            "undo_json IS NULL OR length(CAST(undo_json AS BLOB)) <= 65536",
            name="ck_write_operations_undo_bytes",
        ),
        CheckConstraint(
            "coalesce(length(CAST(result_json AS BLOB)),0) + coalesce(length(CAST(visible_result AS BLOB)),0) + "
            "coalesce(length(CAST(transport_json AS BLOB)),0) + coalesce(length(CAST(undo_json AS BLOB)),0) <= 1048576",
            name="ck_write_operations_terminal_bytes",
        ),
        CheckConstraint(
            "failure_category IS NULL OR failure_category IN ('validation_error','permission_denied',"
            "'confirmation_rejected','stale_state','conflict','not_found','provider_error','internal_error')",
            name="ck_write_operations_failure_category",
        ),
        CheckConstraint(
            "failure_code IS NULL OR (length(CAST(failure_code AS BLOB)) BETWEEN 1 AND 128 "
            "AND failure_code NOT GLOB '*[^ -~]*')",
            name="ck_write_operations_failure_code",
        ),
        CheckConstraint(
            "length(fingerprint_key_id) = 36 AND lower(fingerprint_key_id) = fingerprint_key_id "
            "AND substr(fingerprint_key_id,9,1) = '-' AND substr(fingerprint_key_id,14,1) = '-' "
            "AND substr(fingerprint_key_id,19,1) = '-' AND substr(fingerprint_key_id,24,1) = '-' "
            "AND fingerprint_key_id NOT GLOB '*[^0-9a-f-]*'",
            name="ck_write_operations_key_id",
        ),
        CheckConstraint(
            "(proposal_fingerprint IS NULL OR (length(proposal_fingerprint) = 76 "
            "AND substr(proposal_fingerprint,1,12) = 'hmac-sha256:' "
            "AND substr(proposal_fingerprint,13) NOT GLOB '*[^0-9a-f]*')) "
            "AND (input_fingerprint IS NULL OR (length(input_fingerprint) = 76 "
            "AND substr(input_fingerprint,1,12) = 'hmac-sha256:' "
            "AND substr(input_fingerprint,13) NOT GLOB '*[^0-9a-f]*')) "
            "AND (confirmation_token_fingerprint IS NULL OR (length(confirmation_token_fingerprint) = 76 "
            "AND substr(confirmation_token_fingerprint,1,12) = 'hmac-sha256:' "
            "AND substr(confirmation_token_fingerprint,13) NOT GLOB '*[^0-9a-f]*')) "
            "AND (operation_request_fingerprint IS NULL OR (length(operation_request_fingerprint) = 76 "
            "AND substr(operation_request_fingerprint,1,12) = 'hmac-sha256:' "
            "AND substr(operation_request_fingerprint,13) NOT GLOB '*[^0-9a-f]*')) "
            "AND (delivery_owner_token_fingerprint IS NULL OR (length(delivery_owner_token_fingerprint) = 76 "
            "AND substr(delivery_owner_token_fingerprint,1,12) = 'hmac-sha256:' "
            "AND substr(delivery_owner_token_fingerprint,13) NOT GLOB '*[^0-9a-f]*'))",
            name="ck_write_operations_hmac_fingerprints",
        ),
        CheckConstraint(
            "authorization_scope_fingerprint IS NULL OR "
            "(length(authorization_scope_fingerprint) = 76 "
            "AND substr(authorization_scope_fingerprint,1,12) = 'hmac-sha256:' "
            "AND substr(authorization_scope_fingerprint,13) NOT GLOB '*[^0-9a-f]*')",
            name="ck_write_operations_authorization_scope_fingerprint",
        ),
        CheckConstraint(
            "NOT (operation_role = 'primary' AND adapter_kind = 'product_action' "
            "AND status = 'proposed' AND authorization_scope_fingerprint IS NULL)",
            name="ck_write_operations_product_action_scope_bound",
        ),
        CheckConstraint(
            "(parent_terminal_payload_sha256 IS NULL OR (length(parent_terminal_payload_sha256) = 71 "
            "AND substr(parent_terminal_payload_sha256,1,7) = 'sha256:' "
            "AND substr(parent_terminal_payload_sha256,8) NOT GLOB '*[^0-9a-f]*')) "
            "AND (terminal_payload_sha256 IS NULL OR (length(terminal_payload_sha256) = 71 "
            "AND substr(terminal_payload_sha256,1,7) = 'sha256:' "
            "AND substr(terminal_payload_sha256,8) NOT GLOB '*[^0-9a-f]*')) "
            "AND (delivery_manifest_sha256 IS NULL OR (length(delivery_manifest_sha256) = 71 "
            "AND substr(delivery_manifest_sha256,1,7) = 'sha256:' "
            "AND substr(delivery_manifest_sha256,8) NOT GLOB '*[^0-9a-f]*'))",
            name="ck_write_operations_sha256_digests",
        ),
        CheckConstraint(
            "(status = 'proposed' AND input_fingerprint IS NULL "
            "AND ((operation_role = 'primary' AND operation_request_fingerprint IS NULL) "
            "OR (operation_role = 'compensation' AND operation_request_fingerprint IS NOT NULL)) "
            "AND result_contract IS NULL AND result_json IS NULL AND visible_result IS NULL "
            "AND transport_json IS NULL AND undo_json IS NULL AND terminal_payload_sha256 IS NULL "
            "AND failure_category IS NULL AND failure_code IS NULL AND approved_at IS NULL "
            "AND claimed_at IS NULL AND rejected_at IS NULL AND committed_at IS NULL AND failed_at IS NULL) OR "
            "(status = 'rejected' AND operation_role = 'primary' AND result_contract = 'rejection_json_v1' "
            "AND operation_request_fingerprint IS NOT NULL AND input_fingerprint IS NULL "
            "AND result_json IS NOT NULL AND visible_result IS NOT NULL AND transport_json IS NOT NULL "
            "AND undo_json IS NULL AND terminal_payload_sha256 IS NOT NULL "
            "AND failure_category IS NULL AND failure_code IS NULL "
            "AND approved_at IS NULL AND claimed_at IS NULL AND rejected_at IS NOT NULL "
            "AND committed_at IS NULL AND failed_at IS NULL) OR "
            "(status = 'committed' AND operation_request_fingerprint IS NOT NULL "
            "AND input_fingerprint IS NOT NULL AND result_json IS NOT NULL AND visible_result IS NOT NULL "
            "AND transport_json IS NOT NULL AND terminal_payload_sha256 IS NOT NULL "
            "AND failure_category IS NULL AND failure_code IS NULL "
            "AND approved_at IS NOT NULL AND claimed_at IS NOT NULL AND rejected_at IS NULL "
            "AND committed_at IS NOT NULL AND failed_at IS NULL) OR "
            "(status = 'failed' AND operation_request_fingerprint IS NOT NULL "
            "AND input_fingerprint IS NOT NULL AND result_json IS NOT NULL AND visible_result IS NOT NULL "
            "AND transport_json IS NOT NULL AND undo_json IS NULL AND terminal_payload_sha256 IS NOT NULL "
            "AND failure_category IS NOT NULL AND failure_code IS NOT NULL "
            "AND approved_at IS NOT NULL AND claimed_at IS NOT NULL AND rejected_at IS NULL "
            "AND committed_at IS NULL AND failed_at IS NOT NULL)",
            name="ck_write_operations_terminal_shape",
        ),
        CheckConstraint(
            "status NOT IN ('committed','failed') OR "
            "(adapter_kind = 'typed' AND result_contract = 'typed_json_v1') OR "
            "(adapter_kind = 'legacy_deterministic' AND result_contract = 'legacy_string_v1') OR "
            "(adapter_kind = 'product_action' AND result_contract = 'product_action_json_v1') OR "
            "(adapter_kind = 'compensation' AND result_contract = 'compensation_json_v1')",
            name="ck_write_operations_result_contract",
        ),
        CheckConstraint(
            "status <> 'committed' OR "
            "(operation_role = 'primary' AND tool_name IN "
            "('create_application','update_application_status','create_application_event','add_note',"
            "'confirm_interview_story','save_review_readiness_signal') "
            "AND undo_json IS NOT NULL) OR "
            "((operation_role = 'compensation' OR tool_name NOT IN "
            "('create_application','update_application_status','create_application_event','add_note',"
            "'confirm_interview_story','save_review_readiness_signal')) "
            "AND undo_json IS NULL)",
            name="ck_write_operations_undo_policy",
        ),
        CheckConstraint(
            "(status = 'proposed' AND delivery_status = 'pending' AND delivery_generation = 0 "
            "AND delivery_owner_token_fingerprint IS NULL AND delivery_lease_expires_at IS NULL "
            "AND delivery_outcome IS NULL AND delivery_message_count IS NULL "
            "AND delivery_manifest_sha256 IS NULL AND delivery_next_operation_id IS NULL "
            "AND delivered_at IS NULL AND delivery_failure_code IS NULL) OR "
            "(status <> 'proposed' AND operation_role = 'primary' AND delivery_status = 'pending' AND delivery_generation >= 1 "
            "AND delivery_owner_token_fingerprint IS NOT NULL AND delivery_lease_expires_at IS NOT NULL "
            "AND delivery_outcome IS NULL AND delivery_message_count IS NULL "
            "AND delivery_manifest_sha256 IS NULL AND delivery_next_operation_id IS NULL "
            "AND delivered_at IS NULL AND delivery_failure_code IS NULL) OR "
            "(status <> 'proposed' AND operation_role = 'primary' AND delivery_status = 'completed' "
            "AND delivery_generation >= 1 AND delivery_owner_token_fingerprint IS NULL AND delivery_lease_expires_at IS NULL "
            "AND delivery_outcome IN ('final_response','chained_pending') "
            "AND delivery_message_count >= 2 AND delivery_manifest_sha256 IS NOT NULL "
            "AND delivered_at IS NOT NULL AND delivery_failure_code IS NULL "
            "AND ((delivery_outcome = 'chained_pending' AND delivery_next_operation_id IS NOT NULL) "
            "OR (delivery_outcome = 'final_response' AND delivery_next_operation_id IS NULL))) OR "
            "(status <> 'proposed' AND operation_role = 'primary' AND delivery_status = 'failed' "
            "AND delivery_generation >= 1 AND delivery_owner_token_fingerprint IS NULL "
            "AND delivery_lease_expires_at IS NULL AND delivery_outcome = 'fallback' "
            "AND delivery_message_count = 2 AND delivery_manifest_sha256 IS NOT NULL "
            "AND delivery_next_operation_id IS NULL AND delivered_at IS NOT NULL "
            "AND delivery_failure_code IS NOT NULL) OR "
            "(status <> 'proposed' AND operation_role = 'compensation' AND delivery_status = 'not_applicable' "
            "AND delivery_generation = 0 AND delivery_outcome = 'none' AND delivery_message_count = 0 "
            "AND delivery_owner_token_fingerprint IS NULL AND delivery_lease_expires_at IS NULL "
            "AND delivery_manifest_sha256 IS NULL AND delivery_next_operation_id IS NULL "
            "AND delivery_failure_code IS NULL AND delivered_at IS NOT NULL "
            "AND ((status = 'committed' AND delivered_at = committed_at) "
            "OR (status = 'failed' AND delivered_at = failed_at))) OR "
            "(status <> 'proposed' AND operation_role = 'primary' "
            "AND adapter_kind = 'product_action' "
            "AND delivery_status = 'not_applicable' AND delivery_generation = 0 "
            "AND delivery_outcome = 'none' AND delivery_message_count = 0 "
            "AND delivery_owner_token_fingerprint IS NULL "
            "AND delivery_lease_expires_at IS NULL "
            "AND delivery_manifest_sha256 IS NULL "
            "AND delivery_next_operation_id IS NULL "
            "AND delivery_failure_code IS NULL AND delivered_at IS NOT NULL "
            "AND ((status = 'rejected' AND delivered_at = rejected_at) "
            "OR (status = 'committed' AND delivered_at = committed_at) "
            "OR (status = 'failed' AND delivered_at = failed_at)))",
            name="ck_write_operations_delivery_shape",
        ),
        Index(
            "uq_write_operations_primary_call",
            "conversation_id",
            "tool_call_id",
            unique=True,
            sqlite_where=text("operation_role = 'primary' AND conversation_id IS NOT NULL"),
        ),
        Index(
            "uq_write_operations_compensation_parent",
            "parent_operation_id",
            unique=True,
            sqlite_where=text("operation_role = 'compensation'"),
        ),
        Index(
            "uq_write_operations_product_action_call",
            "tool_call_id",
            unique=True,
            sqlite_where=text("operation_role = 'primary' AND adapter_kind = 'product_action'"),
        ),
        Index("idx_write_operations_status", "status", "delivery_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    operation_role: Mapped[str] = mapped_column(String, nullable=False)
    parent_operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("write_operations.id", ondelete="RESTRICT"), nullable=True
    )
    parent_terminal_payload_sha256: Mapped[str | None] = mapped_column(String(71), nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    agent_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tool_name: Mapped[str] = mapped_column(String, nullable=False)
    adapter_kind: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="proposed")
    fingerprint_key_id: Mapped[str] = mapped_column(String(36), nullable=False)
    proposal_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    input_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmation_token_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    authorization_scope_fingerprint: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    operation_request_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    result_contract: Mapped[str | None] = mapped_column(String, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    visible_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    transport_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    undo_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    terminal_payload_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_category: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String, nullable=True)
    delivery_status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    delivery_failure_code: Mapped[str | None] = mapped_column(String, nullable=True)
    delivery_outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    delivery_message_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delivery_manifest_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    delivery_next_operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("write_operations.id", ondelete="RESTRICT"), nullable=True
    )
    delivery_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delivery_owner_token_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    delivery_lease_expires_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )


class WriteOperationTransition(Base):
    __tablename__ = "write_operation_transitions"
    __table_args__ = (
        CheckConstraint(_sqlite_uuid_check("id"), name="ck_write_operation_transitions_id_uuid"),
        CheckConstraint(
            "state IN ('proposed','approved','rejected','claimed','committed','failed')",
            name="ck_write_operation_transitions_state",
        ),
        CheckConstraint("seq >= 1", name="ck_write_operation_transitions_seq"),
        UniqueConstraint("operation_id", "seq", name="uq_write_operation_transitions_seq"),
        Index("idx_write_operation_transitions_operation", "operation_id", "seq"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    operation_id: Mapped[str] = mapped_column(
        ForeignKey("write_operations.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(_sqlite_uuid_check("id"), name="ck_agent_runs_id_uuid"),
        CheckConstraint(
            _sqlite_uuid_check("fingerprint_key_id"),
            name="ck_agent_runs_key_uuid",
        ),
        CheckConstraint("last_seq >= 0", name="ck_agent_runs_last_seq"),
        CheckConstraint("recording_error_count >= 0", name="ck_agent_runs_recording_error_count"),
        CheckConstraint(
            "status IN ('running','waiting_confirmation','completed','failed','cancelled','timed_out')",
            name="ck_agent_runs_status",
        ),
        CheckConstraint(
            "recording_status IN ('healthy','degraded')",
            name="ck_agent_runs_recording_status",
        ),
        Index("idx_agent_runs_conversation_waiting", "conversation_id", "waiting_tool_call_id"),
        Index(
            "uq_agent_runs_waiting_tool_call",
            "conversation_id",
            "waiting_tool_call_id",
            unique=True,
            sqlite_where=text("waiting_tool_call_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    input_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )
    origin_kind: Mapped[str] = mapped_column(String, nullable=False)
    initial_context_type: Mapped[str] = mapped_column(String, nullable=False)
    initial_context_entity_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    initial_context_ref_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fingerprint_key_id: Mapped[str] = mapped_column(String(36), nullable=False)
    initial_transport_mode: Mapped[str] = mapped_column(String, nullable=False)
    initial_route_kind: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="running")
    waiting_tool_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    recording_status: Mapped[str] = mapped_column(
        String, nullable=False, default="healthy", server_default="healthy"
    )
    recording_error_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    failure_code: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentEvent(Base):
    __tablename__ = "agent_events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_agent_events_run_seq"),
        UniqueConstraint("run_id", "dedupe_key", name="uq_agent_events_run_dedupe"),
        CheckConstraint(_sqlite_uuid_check("id"), name="ck_agent_events_id_uuid"),
        CheckConstraint(
            _sqlite_uuid_check("execution_segment_id"),
            name="ck_agent_events_segment_uuid",
        ),
        CheckConstraint(
            f"model_call_id IS NULL OR ({_sqlite_uuid_check('model_call_id')})",
            name="ck_agent_events_model_call_uuid",
        ),
        CheckConstraint("seq > 0", name="ck_agent_events_seq"),
        CheckConstraint(
            f"fingerprint_key_id IS NULL OR ({_sqlite_uuid_check('fingerprint_key_id')})",
            name="ck_agent_events_key_uuid",
        ),
        CheckConstraint(
            "length(CAST(payload_json AS BLOB)) <= 4096", name="ck_agent_events_payload_size"
        ),
        Index("idx_agent_events_type", "run_id", "event_type", "seq"),
        Index("idx_agent_events_segment", "run_id", "execution_segment_id", "seq"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    execution_segment_id: Mapped[str] = mapped_column(String(36), nullable=False)
    model_step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_call_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_ref_type: Mapped[str | None] = mapped_column(String, nullable=True)
    source_ref_id: Mapped[str | None] = mapped_column(String, nullable=True)
    fingerprint_key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class AgentContextSnapshot(Base):
    __tablename__ = "agent_context_snapshots"
    __table_args__ = (
        UniqueConstraint("run_id", "snapshot_key", name="uq_agent_context_run_snapshot"),
        UniqueConstraint("run_id", "model_call_id", name="uq_agent_context_run_model_call"),
        CheckConstraint(_sqlite_uuid_check("id"), name="ck_agent_context_id_uuid"),
        CheckConstraint(
            _sqlite_uuid_check("execution_segment_id"),
            name="ck_agent_context_segment_uuid",
        ),
        CheckConstraint(
            f"model_call_id IS NULL OR ({_sqlite_uuid_check('model_call_id')})",
            name="ck_agent_context_model_call_uuid",
        ),
        CheckConstraint(
            _sqlite_uuid_check("fingerprint_key_id"),
            name="ck_agent_context_key_uuid",
        ),
        CheckConstraint(
            "manifest_schema_version IN (1, 2)",
            name="ck_agent_context_manifest_schema",
        ),
        CheckConstraint(
            "(manifest_schema_version = 1 AND length(CAST(manifest_json AS BLOB)) <= 16384) "
            "OR (manifest_schema_version = 2 AND length(CAST(manifest_json AS BLOB)) <= 65536)",
            name="ck_agent_context_manifest_size",
        ),
        CheckConstraint(
            "estimated_token_count IS NULL OR estimated_token_count >= 0",
            name="ck_agent_context_token_count",
        ),
        CheckConstraint(
            "(token_estimator_name IS NULL AND token_estimator_version IS NULL) OR "
            "(token_estimator_name IS NOT NULL AND token_estimator_version IS NOT NULL)",
            name="ck_agent_context_token_estimator_pair",
        ),
        Index(
            "idx_agent_context_segment_step",
            "run_id",
            "execution_segment_id",
            "model_step",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    execution_segment_id: Mapped[str] = mapped_column(String(36), nullable=False)
    snapshot_key: Mapped[str] = mapped_column(String, nullable=False)
    manifest_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    snapshot_kind: Mapped[str] = mapped_column(String, nullable=False)
    model_step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_call_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    canonicalizer_version: Mapped[str] = mapped_column(String, nullable=False)
    logical_input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint_key_id: Mapped[str] = mapped_column(String(36), nullable=False)
    estimated_token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_estimator_name: Mapped[str | None] = mapped_column(String, nullable=True)
    token_estimator_version: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class KnowledgeSource(Base):
    """Knowledge Source 不可变原件 + lifecycle/extraction/brief 独立状态。"""

    __tablename__ = "knowledge_sources"
    __table_args__ = (
        Index("idx_knowledge_sources_hash", "source_hash"),
        Index("idx_knowledge_sources_lifecycle", "lifecycle"),
        Index("idx_knowledge_sources_extraction", "extraction_status"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    source_kind: Mapped[str] = mapped_column(
        String, nullable=False, default="markdown", server_default="markdown"
    )
    display_title: Mapped[str] = mapped_column(String, default="", server_default="")
    title_hint: Mapped[str] = mapped_column(String, default="", server_default="")
    # KBR-02：frontmatter 白名单 provenance 沿 Source 所有权持久化的文档来源字段。
    # display_title 承载 frontmatter title（可被用户 PATCH 覆盖）；author/published_at
    # 是从原文确定性提取的派生 provenance，非任意 metadata。
    author: Mapped[str] = mapped_column(String, default="", server_default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    main_filename: Mapped[str] = mapped_column(String, nullable=False)
    main_media_type: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="text/markdown",
        server_default="text/markdown",
    )
    main_relative_path: Mapped[str] = mapped_column(String, nullable=False)
    manifest_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    total_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lifecycle: Mapped[str] = mapped_column(
        String, nullable=False, default="active", server_default="active"
    )
    extraction_status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default="pending"
    )
    extraction_error_code: Mapped[str] = mapped_column(String, default="", server_default="")
    extraction_error_message: Mapped[str] = mapped_column(String, default="", server_default="")
    brief_status: Mapped[str] = mapped_column(
        String, nullable=False, default="not_started", server_default="not_started"
    )
    brief_block_reason: Mapped[str] = mapped_column(String, default="", server_default="")
    brief_error_code: Mapped[str] = mapped_column(String, default="", server_default="")
    brief_error_message: Mapped[str] = mapped_column(String, default="", server_default="")
    active_snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active_brief_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class KnowledgeSourceOrigin(Base):
    """每次导入追加一条 file/paste/bundle 来源记录。"""

    __tablename__ = "knowledge_source_origins"
    __table_args__ = (Index("idx_knowledge_source_origins_source", "source_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    import_method: Mapped[str] = mapped_column(String, nullable=False)
    original_filename: Mapped[str] = mapped_column(String, default="", server_default="")
    origin_url: Mapped[str] = mapped_column(String, default="", server_default="")
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class KnowledgeExtractionSnapshot(Base):
    """确定性 Extraction Snapshot：规范化文本 + 结构清单 + digest。"""

    __tablename__ = "knowledge_extraction_snapshots"
    __table_args__ = (
        Index("idx_knowledge_snapshots_source", "source_id"),
        UniqueConstraint(
            "source_id",
            "extractor_version",
            name="uq_knowledge_snapshots_source_version",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    extractor_version: Mapped[str] = mapped_column(String, nullable=False)
    parser_version: Mapped[str] = mapped_column(
        String, nullable=False, default="markdown-it-py-3", server_default="markdown-it-py-3"
    )
    normalization_version: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="nl-1",
        server_default="nl-1",
    )
    tokenizer_version: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="none-1",
        server_default="none-1",
    )
    encoding: Mapped[str] = mapped_column(
        String, nullable=False, default="utf-8", server_default="utf-8"
    )
    detection_method: Mapped[str] = mapped_column(String, default="", server_default="")
    canonical_text: Mapped[str] = mapped_column(Text, nullable=False)
    structure_manifest: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    # KBR-02：Snapshot 记录元数据提取版本，确定性重建可复现。空串表示旧 Snapshot
    # （由 _ensure_column 加列回填）。
    metadata_extraction_version: Mapped[str] = mapped_column(String, default="", server_default="")
    digest: Mapped[str] = mapped_column(String, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class KnowledgeEvidence(Base):
    """引用单位，stable ID + 结构位置 + 邻接关系。"""

    __tablename__ = "knowledge_evidence"
    __table_args__ = (
        Index("idx_knowledge_evidence_source", "source_id"),
        Index("idx_knowledge_evidence_snapshot", "snapshot_id"),
        UniqueConstraint(
            "snapshot_id",
            "ordinal",
            name="uq_knowledge_evidence_snapshot_ordinal",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_extraction_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    block_kind: Mapped[str] = mapped_column(String, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path_json: Mapped[str] = mapped_column(String, default="[]", server_default="[]")
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    line_start: Mapped[int] = mapped_column(Integer, nullable=False)
    line_end: Mapped[int] = mapped_column(Integer, nullable=False)
    canonical_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    search_text: Mapped[str] = mapped_column(String, nullable=False, default="", server_default="")
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    asset_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    previous_evidence_id: Mapped[str | None] = mapped_column(String, nullable=True)
    next_evidence_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )

    @property
    def heading_path(self) -> list[str]:
        if not self.heading_path_json:
            return []
        value = json.loads(self.heading_path_json)
        return value if isinstance(value, list) else []


class KnowledgeSourceAsset(Base):
    """Source Bundle 中的不可变图片附件。

    Spec §14.3：保存 source_id、逻辑名、媒体类型、相对路径、字节大小、sha256、宽、高。
    ``(source_id, logical_name)`` 唯一。原始字节保存于 ``knowledge/sources/<id>/assets/`` 下，
    不写入 SQLite BLOB。
    """

    __tablename__ = "knowledge_source_assets"
    __table_args__ = (
        Index("idx_knowledge_source_assets_source", "source_id"),
        UniqueConstraint(
            "source_id",
            "logical_name",
            name="uq_knowledge_source_assets_source_logical_name",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    logical_name: Mapped[str] = mapped_column(String, nullable=False)
    media_type: Mapped[str] = mapped_column(String, nullable=False)
    relative_path: Mapped[str] = mapped_column(String, nullable=False)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    height: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class KnowledgeJob(Base):
    """后台 Job：extract/brief/delete。KI-02 只产生 extract。"""

    __tablename__ = "knowledge_jobs"
    __table_args__ = (
        Index("idx_knowledge_jobs_source", "source_id"),
        Index("idx_knowledge_jobs_attempt", "attempt_id"),
        Index("idx_knowledge_jobs_status", "status"),
        Index("idx_knowledge_jobs_queue", "queue"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    queue: Mapped[str] = mapped_column(String, nullable=False)
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Brief Job 与具体 Attempt 一一关联；Extraction/Delete Job 保持 NULL。
    attempt_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_brief_attempts.id", ondelete="CASCADE"),
        nullable=True,
    )
    snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stage: Mapped[str] = mapped_column(String, default="", server_default="")
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default="pending"
    )
    progress: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    canceled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    lease_owner: Mapped[str] = mapped_column(String, default="", server_default="")
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # KI-07：每次 claim 生成新 attempt_token；complete/heartbeat 必须验证 token 匹配，
    # 防止迟到 lease 结果提交。Spec §12 "迟到的旧 lease 结果因 owner/Attempt 不匹配
    # 而拒绝提交"。
    attempt_token: Mapped[str] = mapped_column(String, default="", server_default="")
    error_code: Mapped[str] = mapped_column(String, default="", server_default="")
    error_message: Mapped[str] = mapped_column(String, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class KnowledgeLog(Base):
    """Knowledge 操作日志。

    Spec §5.4 / §18：删除日志只保留 Source ID、action、result 和时间,严禁保留标题、
    正文、URL、路径或 Provider 密钥。KI-06 仅使用 ``source_deleted`` action;后续 Ticket
    可在此基础上追加 Brief、Extraction 相关 action,但不得放宽数据最小化原则。
    """

    __tablename__ = "knowledge_logs"
    __table_args__ = (
        Index("idx_knowledge_logs_source", "source_id"),
        Index("idx_knowledge_logs_action", "action"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String, nullable=False)
    result: Mapped[str] = mapped_column(
        String, nullable=False, default="succeeded", server_default="succeeded"
    )
    error_code: Mapped[str] = mapped_column(String, default="", server_default="")
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class KnowledgeSourceBrief(Base):
    """Spec §10 / §14.7：每个 Source 的当前 Brief（单行）。

    一个 Source 至多一条当前 Brief 行；重建流程在新 Attempt 全部门禁通过后，
    于同一 SQLite 事务中以新 Brief 替换旧行并更新 Source.active_brief_id。
    payload_json 严格遵循 Brief Schema v1（Spec §10.1）。
    """

    __tablename__ = "knowledge_source_briefs"
    __table_args__ = (Index("idx_knowledge_source_briefs_source", "source_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_extraction_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    winning_attempt_id: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    language: Mapped[str] = mapped_column(
        String, nullable=False, default="zh-CN", server_default="zh-CN"
    )
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    outdated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class KnowledgeBriefAttempt(Base):
    """Spec §10 / §14.8 / §11.1：Brief Attempt 历史与诊断数据。

    Attempt 在创建时固定 Provider/Model/参数/Prompt 版本/Schema 版本/Snapshot；
    不保存 API Key、完整 Prompt 或不可解析原始响应（Spec §18 / §11.1）。
    候选 payload 与 validation 报告可持久化，便于排查与 KI-11 评估。
    """

    __tablename__ = "knowledge_brief_attempts"
    __table_args__ = (
        Index("idx_knowledge_brief_attempts_source", "source_id"),
        Index("idx_knowledge_brief_attempts_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_extraction_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default="pending"
    )
    provider_id: Mapped[str] = mapped_column(String, nullable=False)
    provider_model: Mapped[str] = mapped_column(String, nullable=False)
    provider_base_url: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    context_window: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    prompt_version: Mapped[str] = mapped_column(String, nullable=False)
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    language: Mapped[str] = mapped_column(
        String, nullable=False, default="zh-CN", server_default="zh-CN"
    )
    candidate_payload_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    validation_report_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )
    error_code: Mapped[str] = mapped_column(String, nullable=False, default="", server_default="")
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    repair_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # KI-10 / Spec §11.1 / §11.4：Attempt 固定 fallback 候选；actual_* 记录实际成功
    # Provider（可能为 fallback）；provider_retry_count 与 next_retry_at 持久化 Provider
    # 层重试进度，重启后保留。repair_count 仍是程序级 repair 次数，与之区分。
    fallback_provider_id: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    fallback_provider_model: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    actual_provider_id: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    actual_provider_model: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    provider_retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    token_input_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    token_output_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class KnowledgeBriefAttemptStep(Base):
    """Brief Attempt 的追加式过程记录。

    只保存结构化元数据、Evidence ID 和限长的模型响应摘要；原始响应按不可信内容
    隔离，禁止把 Evidence 正文或 Prompt 全量复制进常规日志。
    """

    __tablename__ = "knowledge_brief_attempt_steps"
    __table_args__ = (
        Index("idx_knowledge_brief_attempt_steps_attempt", "attempt_id", "sequence"),
        Index("idx_knowledge_brief_attempt_steps_phase", "phase"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_brief_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    iteration: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    phase: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="completed", server_default="completed"
    )
    block_path: Mapped[str] = mapped_column(String, nullable=False, default="", server_default="")
    provider_id: Mapped[str] = mapped_column(String, nullable=False, default="", server_default="")
    provider_model: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    prompt_version: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    evidence_ids_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]"
    )
    output_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )
    token_input_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    token_output_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_code: Mapped[str] = mapped_column(String, nullable=False, default="", server_default="")
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )


class KnowledgeRetrievalTrace(Base):
    """Spec §14.10 Retrieval Trace：本地评估数据，不参与召回。

    KI-08 验收点：每次搜索本地记录 query、filters、命中 ID/score、耗时和可选评估标签。
    Trace 只保存稳定标识符与元数据，禁止保留 Evidence 原文、prompt 或外部 trace。
    ``hits_json`` 结构：``[{"evidence_id": "ev_...", "source_id": int, "score": float}, ...]``。
    """

    __tablename__ = "knowledge_retrieval_traces"
    __table_args__ = (
        Index("idx_knowledge_retrieval_traces_created", "created_at"),
        Index("idx_knowledge_retrieval_traces_label", "evaluation_label"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    filters_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )
    hits_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    evaluation_label: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    error_code: Mapped[str] = mapped_column(String, nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )
