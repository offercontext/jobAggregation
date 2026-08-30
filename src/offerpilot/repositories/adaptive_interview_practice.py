from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from offerpilot.models import (
    AdaptivePracticePlan,
    Application,
    ApplicationEvent,
    InterviewNote,
)
from offerpilot.review_readiness.projection import project_practice_focus
from offerpilot.repositories.json_contract import canonical_json, sha256_text


class AdaptivePracticeNotFound(Exception):
    pass


class AdaptivePracticeConflict(ValueError):
    pass


class AdaptivePracticeValidationError(ValueError):
    pass


class AdaptivePracticeGone(Exception):
    """The caller attempted to create a retired legacy practice plan."""


class AdaptivePracticeUnavailable(Exception):
    """The canonical readiness projection could not be loaded safely."""


_PATH_TO_FIELD = {
    "/questions": "questions",
    "/self_reflection": "self_reflection",
    "/difficulty_points": "difficulty_points",
    "/mood": "mood",
}

_DRILLS = {
    "/difficulty_points": (
        "difficulty_breakdown",
        "拆解卡住的关键一步",
        "这个问题在复盘中被明确记录为卡点，先把关键一步拆开，比继续泛练更有效。",
        "写出当时卡住的具体节点，并用三步说明下一次如何推进。",
    ),
    "/self_reflection": (
        "answer_reframe",
        "重构一次更清晰的回答",
        "你的复盘已经指出表达结构问题，现在适合立刻重写一版可复用回答。",
        "重新组织一次回答：先结论，再给关键事实，最后说明影响。",
    ),
    "/questions": (
        "question_decode",
        "练习问题解码",
        "从真实被问问题出发，先识别考察意图，再组织回答。",
        "写出面试官可能在验证什么，再给出一版针对性的回答。",
    ),
    "/mood": (
        "pressure_rehearsal",
        "复盘压力情境",
        "情绪信号已经影响当时表达，先准备稳定节奏的过渡方式。",
        "写出压力出现的触发点，并准备一句稳定节奏的过渡表达。",
    ),
}

_ASSESSMENTS = {"needs_work", "clearer", "confident"}


def _require_positive_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1 or value > 2**63 - 1:
        raise AdaptivePracticeValidationError(f"adaptive practice {field} is invalid")
    return value


def _require_sha256(value: object, field: str) -> str:
    if (
        type(value) is not str
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise AdaptivePracticeValidationError(f"adaptive practice {field} is invalid")
    return value


class AdaptivePracticeRepository:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._session_factory = session_factory

    def list_recommendations(self) -> list[dict[str, Any]]:
        # New practice creation is exact Signal-Version + target only.  Scanning
        # unconfirmed Review Proposals here would recreate the retired V1 path.
        return []

    def list_plans(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            plans = list(
                session.scalars(
                    select(AdaptivePracticePlan)
                    .join(Application, Application.id == AdaptivePracticePlan.application_id)
                    .where(Application.deleted_at.is_(None))
                    .order_by(
                        AdaptivePracticePlan.created_at.desc(), AdaptivePracticePlan.id.desc()
                    )
                )
            )
            return [
                _plan_json(session, plan)
                for plan in plans
                if _plan_context_is_visible(session, plan)
            ]

    def get(self, plan_id: int) -> dict[str, Any]:
        with self._session_factory() as session:
            plan = _visible_plan(session, plan_id)
            if plan is None:
                raise AdaptivePracticeNotFound()
            return _plan_json(session, plan)

    def start(
        self,
        *,
        proposal_id: int,
        focus_id: str,
        expected_source_fingerprint: str,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        request_fingerprint = sha256_text(
            canonical_json(
                {
                    "proposal_id": proposal_id,
                    "focus_id": focus_id,
                    "expected_source_fingerprint": expected_source_fingerprint,
                }
            )
        )
        with self._session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            existing = session.scalar(
                select(AdaptivePracticePlan).where(
                    AdaptivePracticePlan.start_idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                if existing.start_input_fingerprint != request_fingerprint:
                    raise AdaptivePracticeConflict("adaptive practice idempotency input changed")
                visible = _visible_plan(session, existing.id)
                if visible is None:
                    raise AdaptivePracticeNotFound()
                return _plan_json(session, visible), False
            raise AdaptivePracticeGone("adaptive_practice_v1_retired")

    def start_v2(
        self,
        *,
        readiness_signal_version_id: int,
        target_application_event_id: int,
        expected_source_fingerprint: str,
        expected_target_fingerprint: str,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        _require_positive_int(readiness_signal_version_id, "readiness signal version")
        _require_positive_int(target_application_event_id, "target application event")
        _require_sha256(expected_source_fingerprint, "source fingerprint")
        _require_sha256(expected_target_fingerprint, "target fingerprint")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise AdaptivePracticeValidationError("adaptive practice idempotency key is invalid")
        try:
            parsed_key = UUID(idempotency_key)
        except (ValueError, AttributeError) as exc:
            raise AdaptivePracticeValidationError(
                "adaptive practice idempotency key is invalid"
            ) from exc
        if str(parsed_key) != idempotency_key:
            raise AdaptivePracticeValidationError("adaptive practice idempotency key is invalid")
        request_fingerprint = sha256_text(
            canonical_json(
                {
                    "idempotency_key": idempotency_key,
                    "readiness_signal_version_id": readiness_signal_version_id,
                    "expected_source_fingerprint": expected_source_fingerprint,
                    "target_application_event_id": target_application_event_id,
                    "expected_target_fingerprint": expected_target_fingerprint,
                }
            )
        )
        with self._session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            replay = _load_v2_start_replay(
                session,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
            if replay is not None:
                return replay

            projection = project_practice_focus(
                session,
                signal_version_id=readiness_signal_version_id,
                target_event_id=target_application_event_id,
            )
            if projection.state == "unavailable":
                raise AdaptivePracticeUnavailable("adaptive practice source is unavailable")
            if projection.state in {"source_missing", "target_missing"}:
                raise AdaptivePracticeNotFound()
            if projection.state != "ready":
                raise AdaptivePracticeConflict(
                    f"adaptive practice {projection.state.replace('_', ' ')}"
                )
            source = projection.source
            target = projection.target
            if source is None or target is None:
                raise AdaptivePracticeUnavailable("adaptive practice projection is incomplete")
            if source.practice_source_fingerprint != expected_source_fingerprint:
                raise AdaptivePracticeConflict("adaptive practice source changed")
            if target.practice_target_fingerprint != expected_target_fingerprint:
                raise AdaptivePracticeConflict("adaptive practice target changed")
            if (
                source.source_event_id is None
                or source.source_note_id is None
                or source.source_proposal_id is None
                or not source.evidence
            ):
                raise AdaptivePracticeUnavailable("adaptive practice source snapshot is incomplete")
            primary = source.evidence[0]
            drill = _DRILLS.get(primary.source_path)
            if drill is None:
                raise AdaptivePracticeUnavailable("adaptive practice source path is unsupported")
            drill_kind, title, reason, base_prompt = drill
            prompt = base_prompt
            if source.user_note:
                prompt = f"{base_prompt}\n用户补充：{source.user_note}"
            plan = AdaptivePracticePlan(
                application_id=source.application_id,
                application_event_id=source.source_event_id,
                interview_note_id=source.source_note_id,
                interview_review_proposal_id=source.source_proposal_id,
                focus_id=source.focus_id,
                start_idempotency_key=idempotency_key,
                start_input_fingerprint=request_fingerprint,
                source_fingerprint=source.practice_source_fingerprint,
                source_path=primary.source_path,
                source_excerpt=primary.excerpt,
                source_hash=primary.source_field_sha256,
                drill_kind=drill_kind,
                title=title,
                observation=source.statement_text,
                reason=reason,
                prompt=prompt,
                origin_contract="confirmed_readiness_signal_v1",
                readiness_signal_version_id=source.version_id,
                target_application_event_id=target.event_id,
                target_fingerprint=target.practice_target_fingerprint,
            )
            session.add(plan)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                replay = _load_v2_start_replay(
                    session,
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                )
                if replay is not None:
                    return replay
                duplicate = session.scalar(
                    select(AdaptivePracticePlan).where(
                        AdaptivePracticePlan.origin_contract == "confirmed_readiness_signal_v1",
                        AdaptivePracticePlan.readiness_signal_version_id
                        == readiness_signal_version_id,
                        AdaptivePracticePlan.target_application_event_id
                        == target_application_event_id,
                    )
                )
                if duplicate is not None:
                    raise AdaptivePracticeConflict("adaptive practice already started") from exc
                raise AdaptivePracticeUnavailable(
                    "adaptive practice could not be persisted"
                ) from exc
            session.refresh(plan)
            return _plan_json(session, plan, project_live=False), True

    def complete(
        self,
        *,
        plan_id: int,
        expected_revision: int,
        response_text: str,
        reflection_text: str,
        self_assessment: str,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        response = response_text.strip()
        reflection = reflection_text.strip()
        if not response or len(response) > 8000 or len(reflection) > 4000:
            raise AdaptivePracticeValidationError("adaptive practice response is invalid")
        if self_assessment not in _ASSESSMENTS:
            raise AdaptivePracticeValidationError("adaptive practice assessment is invalid")
        fingerprint = sha256_text(
            canonical_json(
                {
                    "plan_id": plan_id,
                    "expected_revision": expected_revision,
                    "response_text": response,
                    "reflection_text": reflection,
                    "self_assessment": self_assessment,
                }
            )
        )
        with self._session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            plan = _visible_plan(session, plan_id)
            if plan is None:
                raise AdaptivePracticeNotFound()
            if plan.completion_idempotency_key == idempotency_key:
                if plan.completion_fingerprint != fingerprint:
                    raise AdaptivePracticeConflict(
                        "adaptive practice completion idempotency input changed"
                    )
                return _plan_json(session, plan), False
            if plan.status != "in_progress" or plan.revision != expected_revision:
                raise AdaptivePracticeConflict("adaptive practice revision changed")
            other = session.scalar(
                select(AdaptivePracticePlan).where(
                    AdaptivePracticePlan.completion_idempotency_key == idempotency_key
                )
            )
            if other is not None:
                raise AdaptivePracticeConflict(
                    "adaptive practice completion idempotency input changed"
                )
            plan.response_text = response
            plan.reflection_text = reflection
            plan.self_assessment = self_assessment
            plan.completion_idempotency_key = idempotency_key
            plan.completion_fingerprint = fingerprint
            plan.status = "completed"
            plan.revision += 1
            plan.completed_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(plan)
            return _plan_json(session, plan), True


def _visible_plan(session: Session, plan_id: int) -> AdaptivePracticePlan | None:
    plan = session.get(AdaptivePracticePlan, plan_id)
    return plan if plan is not None and _plan_context_is_visible(session, plan) else None


def _load_v2_start_replay(
    session: Session,
    *,
    idempotency_key: str,
    request_fingerprint: str,
) -> tuple[dict[str, Any], bool] | None:
    existing = session.scalar(
        select(AdaptivePracticePlan).where(
            AdaptivePracticePlan.start_idempotency_key == idempotency_key
        )
    )
    if existing is None:
        return None
    if existing.start_input_fingerprint != request_fingerprint:
        raise AdaptivePracticeConflict("adaptive practice idempotency input changed")
    visible = _visible_plan(session, existing.id)
    if visible is None:
        raise AdaptivePracticeNotFound()
    return _plan_json(session, visible, project_live=False), False


def _plan_context_is_visible(session: Session, plan: AdaptivePracticePlan) -> bool:
    application = session.get(Application, plan.application_id)
    if application is None or application.deleted_at is not None:
        return False
    if plan.origin_contract == "confirmed_readiness_signal_v1":
        # The non-null snapshot columns are history, not live joins.  V2 remains
        # readable/completable after either locator is lowered to NULL.
        return True
    if plan.origin_contract != "legacy_review_focus_v1":
        return False
    note = session.get(InterviewNote, plan.interview_note_id)
    event = session.get(ApplicationEvent, plan.application_event_id)
    return bool(
        note is not None
        and event is not None
        and note.application_id == plan.application_id
        and note.application_event_id == plan.application_event_id
        and event.application_id == plan.application_id
        and event.event_type == "interview"
    )


def _legacy_source_status(session: Session, plan: AdaptivePracticePlan) -> str:
    note = session.get(InterviewNote, plan.interview_note_id)
    event = session.get(ApplicationEvent, plan.application_event_id)
    if note is None or event is None or note.application_id != plan.application_id:
        return "missing"
    field = _PATH_TO_FIELD.get(plan.source_path)
    if field is None:
        return "missing"
    current = str(getattr(note, field, ""))
    return "current" if sha256_text(current) == plan.source_hash else "changed"


def _practice_state(
    session: Session,
    plan: AdaptivePracticePlan,
    *,
    project_live: bool,
) -> str:
    if plan.origin_contract != "confirmed_readiness_signal_v1" or not project_live:
        return plan.status
    if plan.readiness_signal_version_id is None:
        return "source_missing"
    projection = project_practice_focus(
        session,
        signal_version_id=plan.readiness_signal_version_id,
        target_event_id=plan.target_application_event_id or 0,
    )
    return projection.state


def _plan_json(
    session: Session,
    plan: AdaptivePracticePlan,
    *,
    project_live: bool = True,
) -> dict[str, Any]:
    application = session.get(Application, plan.application_id)
    practice_state = _practice_state(session, plan, project_live=project_live)
    return {
        "id": plan.id,
        "origin_contract": plan.origin_contract,
        "application_id": plan.application_id,
        "application_event_id": plan.application_event_id,
        "target_application_event_id": plan.target_application_event_id,
        "readiness_signal_version_id": plan.readiness_signal_version_id,
        "interview_note_id": plan.interview_note_id,
        "proposal_id": plan.interview_review_proposal_id,
        "focus_id": plan.focus_id,
        "company_name": application.company_name if application is not None else "",
        "position_name": application.position_name if application is not None else "",
        "drill_kind": plan.drill_kind,
        "title": plan.title,
        "observation": plan.observation,
        "reason": plan.reason,
        "prompt": plan.prompt,
        "source_path": plan.source_path,
        "source_excerpt": plan.source_excerpt,
        "source_fingerprint": plan.source_fingerprint,
        "target_fingerprint": plan.target_fingerprint,
        "source_status": (
            _legacy_source_status(session, plan)
            if plan.origin_contract == "legacy_review_focus_v1"
            else None
        ),
        "practice_state": practice_state,
        "status": plan.status,
        "revision": plan.revision,
        "response_text": plan.response_text,
        "reflection_text": plan.reflection_text,
        "self_assessment": plan.self_assessment,
        "created_at": _utc(plan.created_at),
        "completed_at": _utc(plan.completed_at),
    }


def _utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
