from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from offerpilot.db import init_database
from offerpilot.models import (
    AdaptivePracticePlan,
    Application,
    ApplicationEvent,
    InterviewNote,
    InterviewReadinessSignal,
    InterviewReadinessSignalEvidence,
    InterviewReviewProposal,
)
from offerpilot.product_actions.catalog import ProductActionCatalogV1
from offerpilot.product_actions.contracts import ProductActionProofRegistryV1
from offerpilot.product_actions.coordinator import ProductActionCoordinator
from offerpilot.product_actions.issuer import LedgerKeyProfileStoreV1, ReviewReadinessActionIssuer
from offerpilot.product_actions.repository import ProductActionProposalRepository
from offerpilot.review_readiness.candidates import (
    project_readiness_candidates,
    resolve_readiness_candidates,
)
from offerpilot.review_readiness.projection import (
    project_practice_focus,
)
from offerpilot.review_readiness.repository import ReadinessSignalRepository
from offerpilot.repositories.adaptive_interview_practice import (
    AdaptivePracticeConflict,
    AdaptivePracticeGone,
    AdaptivePracticeNotFound,
    AdaptivePracticeRepository,
    AdaptivePracticeUnavailable,
    AdaptivePracticeValidationError,
)
from offerpilot.repositories import adaptive_interview_practice as practice_repository_module
from offerpilot.repositories.json_contract import canonical_json, sha256_text

from tests.product_actions.conftest import KEY_ONE, KEY_TWO
from tests.review_readiness_support import seed_review_candidate


def _coordinator(session_factory):  # type: ignore[no-untyped-def]
    registry = ProductActionProofRegistryV1()
    catalog = ProductActionCatalogV1(registry)
    keys = LedgerKeyProfileStoreV1((KEY_ONE, KEY_TWO), active_key_id=KEY_ONE.key_id)
    return ProductActionCoordinator(
        session_factory,
        catalog=catalog,
        proposal_repository=ProductActionProposalRepository(
            session_factory,
            catalog=catalog,
            proof_registry=registry,
            key_profiles=keys,
        ),
        review_issuer=ReviewReadinessActionIssuer(catalog, registry, keys),
        proof_registry=registry,
        key_profiles=keys,
        readiness_repository=ReadinessSignalRepository(
            session_factory,
            proof_registry=registry,
        ),
        capability_check=lambda capability: (
            capability == "application.interview_readiness_feedback.write"
        ),
        candidate_projector=project_readiness_candidates,
    )


def _seed_v2(tmp_path):  # type: ignore[no-untyped-def]
    session_factory = init_database(tmp_path / "v2.db")
    difficulty = "I struggled to explain cache consistency tradeoffs."
    reflection = "I should clarify constraints before choosing a strategy."
    seeded = seed_review_candidate(
        session_factory,
        practice_focuses=[
            {
                "id": "focus-1",
                "text": "下次先澄清约束条件，再说明缓存一致性的取舍。",
                "evidence_refs": [
                    {
                        "source": "interview_note",
                        "path": "/difficulty_points",
                        "excerpt": difficulty,
                    },
                    {
                        "source": "interview_note",
                        "path": "/self_reflection",
                        "excerpt": reflection,
                    },
                ],
            }
        ],
    )
    with session_factory() as session:
        candidate = resolve_readiness_candidates(
            int(seeded["note_id"]),
            int(seeded["proposal_id"]),
            session,
        ).candidates[0]
    coordinator = _coordinator(session_factory)
    proposed = coordinator.propose_readiness_signal(
        note_id=int(seeded["note_id"]),
        request={
            "proposal_id": int(seeded["proposal_id"]),
            "focus_id": "focus-1",
            "expected_note_revision": int(seeded["note_revision"]),
            "expected_candidate_fingerprint": candidate.candidate_fingerprint,
            "idempotency_key": str(uuid4()),
            "user_note": "练习时先说明约束。",
        },
    )
    decided = coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": proposed.confirmation_token,
            "decision": "approve",
        },
    )
    assert decided.status == "committed"
    with session_factory() as session:
        signal = session.scalar(select(InterviewReadinessSignal))
        assert signal is not None and signal.current_version_id is not None
        source_event = session.get(ApplicationEvent, int(seeded["event_id"]))
        assert source_event is not None
        source_event.tags = ["source"]
        target = ApplicationEvent(
            application_id=int(seeded["application_id"]),
            event_type="interview",
            subtype="system_design",
            round=3,
            scheduled_at=datetime(2026, 9, 1, 10, tzinfo=timezone.utc),
            duration_minutes=60,
            status="scheduled",
        )
        target.tags = ["onsite", "architecture"]
        session.add(target)
        session.commit()
        version_id = signal.current_version_id
        target_id = target.id
    with session_factory() as session:
        focus = project_practice_focus(
            session,
            signal_version_id=version_id,
            target_event_id=target_id,
        )
        assert focus.state == "ready"
        assert focus.source is not None and focus.target is not None
        source_fingerprint = focus.source.practice_source_fingerprint
        target_fingerprint = focus.target.practice_target_fingerprint
    return (
        session_factory,
        seeded,
        version_id,
        target_id,
        source_fingerprint,
        target_fingerprint,
    )


def _add_target(
    session_factory,
    application_id: int,
    *,
    status: str = "scheduled",
    event_type: str = "interview",
) -> int:  # type: ignore[no-untyped-def]
    with session_factory() as session:
        target = ApplicationEvent(
            application_id=application_id,
            event_type=event_type,
            subtype="behavioral",
            round=4,
            scheduled_at=datetime(2026, 9, 2, 10, tzinfo=timezone.utc),
            duration_minutes=45,
            status=status,
        )
        target.tags = ["follow-up"]
        session.add(target)
        session.commit()
        return target.id


def _exact_pair(session_factory, version_id: int, target_id: int):  # type: ignore[no-untyped-def]
    with session_factory() as session:
        projection = project_practice_focus(
            session,
            signal_version_id=version_id,
            target_event_id=target_id,
        )
        assert projection.source is not None and projection.target is not None
        return (
            projection.source.practice_source_fingerprint,
            projection.target.practice_target_fingerprint,
            projection.state,
        )


def _setup(tmp_path):
    session_factory = init_database(tmp_path / "data.db")
    with session_factory() as session:
        application = Application(company_name="云栖智能", position_name="后端工程师", source="web")
        session.add(application)
        session.flush()
        event = ApplicationEvent(
            application_id=application.id,
            event_type="interview",
            scheduled_at=datetime(2026, 8, 12, 10, tzinfo=timezone.utc),
            duration_minutes=60,
            status="done",
        )
        session.add(event)
        session.flush()
        note = InterviewNote(
            application_id=application.id,
            application_event_id=event.id,
            company="云栖智能",
            position="后端工程师",
            questions="请说明一次线上延迟排查。",
            self_reflection="回答时先讲了过程，没有先给结论。",
            difficulty_points="被追问影响范围时卡住了。",
            mood="有些紧张",
        )
        session.add(note)
        session.flush()
        snapshot = {
            "note": {
                "questions": note.questions,
                "self_reflection": note.self_reflection,
                "difficulty_points": note.difficulty_points,
                "mood": note.mood,
            },
            "event": {"id": event.id},
        }
        proposal_payload = {
            "summary": {"text": "复盘包含可练习问题。", "evidence_refs": []},
            "observations": [],
            "clarifications": [],
            "practice_focuses": [
                {
                    "id": "focus-difficulty",
                    "text": "拆解影响范围追问。",
                    "evidence_refs": [
                        {
                            "source": "interview_note",
                            "path": "/difficulty_points",
                            "excerpt": note.difficulty_points,
                        }
                    ],
                },
                {
                    "id": "focus-reflection",
                    "text": "练习先结论后事实。",
                    "evidence_refs": [
                        {
                            "source": "interview_note",
                            "path": "/self_reflection",
                            "excerpt": note.self_reflection,
                        }
                    ],
                },
            ],
            "next_questions": [],
        }
        proposal = InterviewReviewProposal(
            note_id=note.id,
            application_event_id=event.id,
            idempotency_key="review-1",
            input_snapshot_json=canonical_json(snapshot),
            source_fingerprint=sha256_text(canonical_json(snapshot)),
            proposal_json=canonical_json(proposal_payload),
            proposal_hash=sha256_text(canonical_json(proposal_payload)),
        )
        session.add(proposal)
        session.commit()
        return session_factory, application.id, event.id, note.id, proposal.id


def _seed_legacy_plan(
    session_factory,
    *,
    proposal_id: int,
    idempotency_key: str = "practice-start-legacy",
):  # type: ignore[no-untyped-def]
    with session_factory() as session:
        proposal = session.get(InterviewReviewProposal, proposal_id)
        assert proposal is not None
        note = session.get(InterviewNote, proposal.note_id)
        event = session.get(ApplicationEvent, proposal.application_event_id)
        assert note is not None and event is not None and note.application_id is not None
        focus_id = "focus-difficulty"
        excerpt = "被追问影响范围时卡住了。"
        source_fingerprint = sha256_text(
            canonical_json(
                {
                    "proposal_id": proposal.id,
                    "proposal_hash": proposal.proposal_hash,
                    "focus_id": focus_id,
                    "source_path": "/difficulty_points",
                    "source_excerpt": excerpt,
                    "source_value_hash": sha256_text(note.difficulty_points),
                    "note_id": note.id,
                    "event_id": event.id,
                }
            )
        )
        input_fingerprint = sha256_text(
            canonical_json(
                {
                    "proposal_id": proposal.id,
                    "focus_id": focus_id,
                    "expected_source_fingerprint": source_fingerprint,
                }
            )
        )
        plan = AdaptivePracticePlan(
            application_id=note.application_id,
            application_event_id=event.id,
            interview_note_id=note.id,
            interview_review_proposal_id=proposal.id,
            focus_id=focus_id,
            start_idempotency_key=idempotency_key,
            start_input_fingerprint=input_fingerprint,
            source_fingerprint=source_fingerprint,
            source_path="/difficulty_points",
            source_excerpt=excerpt,
            source_hash=sha256_text(note.difficulty_points),
            drill_kind="difficulty_breakdown",
            title="拆解卡住的关键一步",
            observation="拆解影响范围追问。",
            reason="冻结的 legacy 理由",
            prompt="冻结的 legacy 练习提示",
        )
        session.add(plan)
        session.commit()
        return plan.id, focus_id, source_fingerprint, idempotency_key


def test_legacy_new_creation_is_retired_but_existing_replay_survives(tmp_path) -> None:
    session_factory, application_id, event_id, note_id, proposal_id = _setup(tmp_path)
    repository = AdaptivePracticeRepository(session_factory)
    plan_id, focus_id, source_fingerprint, idempotency_key = _seed_legacy_plan(
        session_factory,
        proposal_id=proposal_id,
    )
    replay, replay_created = repository.replay_legacy_start(
        proposal_id=proposal_id,
        focus_id=focus_id,
        expected_source_fingerprint=source_fingerprint,
        idempotency_key=idempotency_key,
    )

    assert replay_created is False
    assert replay["id"] == plan_id
    assert replay["application_id"] == application_id
    assert replay["application_event_id"] == event_id
    assert replay["interview_note_id"] == note_id
    assert repository.list_recommendations() == []
    with pytest.raises(AdaptivePracticeGone, match="retired"):
        repository.replay_legacy_start(
            proposal_id=proposal_id,
            focus_id="focus-reflection",
            expected_source_fingerprint="sha256:" + "a" * 64,
            idempotency_key="practice-start-new-legacy",
        )


def test_legacy_replay_rejects_changed_idempotent_input(tmp_path) -> None:
    session_factory, _, _, _, proposal_id = _setup(tmp_path)
    repository = AdaptivePracticeRepository(session_factory)
    _plan_id, focus_id, source_fingerprint, idempotency_key = _seed_legacy_plan(
        session_factory,
        proposal_id=proposal_id,
    )

    with pytest.raises(AdaptivePracticeConflict, match="idempotency"):
        repository.replay_legacy_start(
            proposal_id=proposal_id,
            focus_id=focus_id,
            expected_source_fingerprint=source_fingerprint + "-changed",
            idempotency_key=idempotency_key,
        )


def test_complete_uses_revision_cas_and_preserves_frozen_history(tmp_path) -> None:
    session_factory, _, _, note_id, proposal_id = _setup(tmp_path)
    repository = AdaptivePracticeRepository(session_factory)
    plan_id, _focus_id, _source_fingerprint, _start_key = _seed_legacy_plan(
        session_factory,
        proposal_id=proposal_id,
    )

    completed, created = repository.complete(
        plan_id=plan_id,
        expected_revision=1,
        response_text="先明确影响范围，再说明定位路径，最后给出恢复结果。",
        reflection_text="下一次先给结论。",
        self_assessment="clearer",
        idempotency_key="practice-complete-1",
    )
    replay, replay_created = repository.complete(
        plan_id=plan_id,
        expected_revision=1,
        response_text="先明确影响范围，再说明定位路径，最后给出恢复结果。",
        reflection_text="下一次先给结论。",
        self_assessment="clearer",
        idempotency_key="practice-complete-1",
    )

    assert created is True
    assert replay_created is False
    assert completed["status"] == "completed"
    assert completed["revision"] == 2
    assert replay["id"] == completed["id"]
    assert completed["source_status"] == "current"

    with session_factory() as session:
        note = session.get(InterviewNote, note_id)
        assert note is not None
        note.difficulty_points = "来源已经变化"
        session.commit()
    history = repository.list_plans()
    assert history[0]["source_status"] == "changed"
    assert history[0]["source_excerpt"] == "被追问影响范围时卡住了。"

    with pytest.raises(AdaptivePracticeConflict, match="idempotency"):
        repository.complete(
            plan_id=plan_id,
            expected_revision=1,
            response_text="改变后的回答",
            reflection_text="下一次先给结论。",
            self_assessment="clearer",
            idempotency_key="practice-complete-1",
        )


def test_deleted_application_hides_recommendations_and_plans(tmp_path) -> None:
    session_factory, application_id, _, _, proposal_id = _setup(tmp_path)
    repository = AdaptivePracticeRepository(session_factory)
    plan_id, focus_id, source_fingerprint, start_key = _seed_legacy_plan(
        session_factory,
        proposal_id=proposal_id,
    )
    with session_factory() as session:
        application = session.get(Application, application_id)
        assert application is not None
        application.deleted_at = datetime.now(timezone.utc)
        session.commit()

    assert repository.list_recommendations() == []
    assert repository.list_plans() == []
    with pytest.raises(AdaptivePracticeNotFound):
        repository.get(plan_id)
    with pytest.raises(AdaptivePracticeNotFound):
        repository.replay_legacy_start(
            proposal_id=proposal_id,
            focus_id=focus_id,
            expected_source_fingerprint=source_fingerprint,
            idempotency_key=start_key,
        )


def test_note_or_event_deletion_hides_frozen_plan(tmp_path) -> None:
    session_factory, _, event_id, note_id, proposal_id = _setup(tmp_path)
    repository = AdaptivePracticeRepository(session_factory)
    plan_id, _focus_id, _source_fingerprint, _start_key = _seed_legacy_plan(
        session_factory,
        proposal_id=proposal_id,
        idempotency_key="practice-start-hidden",
    )
    with session_factory() as session:
        note = session.get(InterviewNote, note_id)
        assert note is not None
        session.delete(note)
        session.commit()
    assert repository.list_plans() == []
    with pytest.raises(AdaptivePracticeNotFound):
        repository.get(plan_id)

    session_factory2, _, event_id2, _, proposal_id2 = _setup(tmp_path / "event")
    repository2 = AdaptivePracticeRepository(session_factory2)
    plan_id2, _focus_id2, _source_fingerprint2, _start_key2 = _seed_legacy_plan(
        session_factory2,
        proposal_id=proposal_id2,
        idempotency_key="practice-start-event-hidden",
    )
    with session_factory2() as session:
        event = session.get(ApplicationEvent, event_id2)
        assert event is not None
        session.delete(event)
        session.commit()
    assert repository2.list_plans() == []
    with pytest.raises(AdaptivePracticeNotFound):
        repository2.get(plan_id2)


def test_appending_to_legacy_source_marks_frozen_plan_changed(tmp_path) -> None:
    session_factory, _, _, note_id, proposal_id = _setup(tmp_path)
    repository = AdaptivePracticeRepository(session_factory)
    plan_id, _focus_id, _source_fingerprint, _start_key = _seed_legacy_plan(
        session_factory,
        proposal_id=proposal_id,
        idempotency_key="practice-start-source-hash",
    )
    with session_factory() as session:
        note = session.get(InterviewNote, note_id)
        assert note is not None
        note.difficulty_points = note.difficulty_points + " 后来补充了新的细节。"
        session.commit()

    assert repository.get(plan_id)["source_status"] == "changed"


def test_v2_start_binds_exact_signal_target_and_primary_evidence(tmp_path) -> None:
    (
        session_factory,
        seeded,
        version_id,
        target_id,
        source_fingerprint,
        target_fingerprint,
    ) = _seed_v2(tmp_path)
    repository = AdaptivePracticeRepository(session_factory)
    start_key = str(uuid4())

    plan, created = repository.start_v2(
        readiness_signal_version_id=version_id,
        target_application_event_id=target_id,
        expected_source_fingerprint=source_fingerprint,
        expected_target_fingerprint=target_fingerprint,
        idempotency_key=start_key,
    )

    assert created is True
    assert plan["origin_contract"] == "confirmed_readiness_signal_v1"
    assert plan["application_event_id"] == seeded["event_id"]
    assert plan["target_application_event_id"] == target_id
    assert plan["readiness_signal_version_id"] == version_id
    assert plan["source_fingerprint"] == source_fingerprint
    assert plan["target_fingerprint"] == target_fingerprint
    assert plan["source_path"] == "/difficulty_points"
    assert plan["source_excerpt"] == ("I struggled to explain cache consistency tradeoffs.")
    assert plan["observation"] == ("下次先澄清约束条件，再说明缓存一致性的取舍。")
    assert "练习时先说明约束。" in plan["prompt"]
    with session_factory() as session:
        persisted = session.get(AdaptivePracticePlan, plan["id"])
        assert persisted is not None
        assert persisted.application_event_id != persisted.target_application_event_id
        assert persisted.start_input_fingerprint == "sha256:" + sha256_text(
            canonical_json(
                {
                    "idempotency_key": start_key,
                    "readiness_signal_version_id": version_id,
                    "expected_source_fingerprint": source_fingerprint,
                    "target_application_event_id": target_id,
                    "expected_target_fingerprint": target_fingerprint,
                }
            )
        )


def test_v2_start_is_idempotency_first_and_replay_never_loads_live_pair(
    tmp_path,
    monkeypatch,
) -> None:
    (
        session_factory,
        seeded,
        version_id,
        target_id,
        source_fingerprint,
        target_fingerprint,
    ) = _seed_v2(tmp_path)
    repository = AdaptivePracticeRepository(session_factory)
    start_key = str(uuid4())
    first, created = repository.start_v2(
        readiness_signal_version_id=version_id,
        target_application_event_id=target_id,
        expected_source_fingerprint=source_fingerprint,
        expected_target_fingerprint=target_fingerprint,
        idempotency_key=start_key,
    )
    with session_factory() as session:
        note = session.get(InterviewNote, int(seeded["note_id"]))
        target = session.get(ApplicationEvent, target_id)
        assert note is not None and target is not None
        note.content_revision += 1
        target.status = "completed"
        session.commit()

    live_calls = 0

    def forbidden_live_loader(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal live_calls
        live_calls += 1
        raise AssertionError("idempotent replay must not load source or target")

    monkeypatch.setattr(
        practice_repository_module,
        "project_practice_focus",
        forbidden_live_loader,
    )
    replay, replay_created = repository.start_v2(
        readiness_signal_version_id=version_id,
        target_application_event_id=target_id,
        expected_source_fingerprint=source_fingerprint,
        expected_target_fingerprint=target_fingerprint,
        idempotency_key=start_key,
    )
    with pytest.raises(AdaptivePracticeConflict, match="idempotency"):
        repository.start_v2(
            readiness_signal_version_id=version_id,
            target_application_event_id=target_id,
            expected_source_fingerprint=source_fingerprint,
            expected_target_fingerprint="sha256:" + "f" * 64,
            idempotency_key=start_key,
        )

    assert created is True
    assert replay_created is False
    assert replay["id"] == first["id"]
    assert live_calls == 0

    with session_factory() as session:
        target = session.get(ApplicationEvent, target_id)
        assert target is not None
        session.delete(target)
        session.commit()
    missing_replay, missing_created = repository.start_v2(
        readiness_signal_version_id=version_id,
        target_application_event_id=target_id,
        expected_source_fingerprint=source_fingerprint,
        expected_target_fingerprint=target_fingerprint,
        idempotency_key=start_key,
    )
    assert missing_created is False
    assert missing_replay["id"] == first["id"]
    assert live_calls == 0


def test_v2_start_rejects_changed_fingerprints_and_ineligible_targets(tmp_path) -> None:
    (
        session_factory,
        seeded,
        version_id,
        target_id,
        source_fingerprint,
        target_fingerprint,
    ) = _seed_v2(tmp_path)
    repository = AdaptivePracticeRepository(session_factory)
    with pytest.raises(AdaptivePracticeConflict, match="source"):
        repository.start_v2(
            readiness_signal_version_id=version_id,
            target_application_event_id=target_id,
            expected_source_fingerprint="sha256:" + "a" * 64,
            expected_target_fingerprint=target_fingerprint,
            idempotency_key=str(uuid4()),
        )
    with pytest.raises(AdaptivePracticeConflict, match="target"):
        repository.start_v2(
            readiness_signal_version_id=version_id,
            target_application_event_id=target_id,
            expected_source_fingerprint=source_fingerprint,
            expected_target_fingerprint="sha256:" + "b" * 64,
            idempotency_key=str(uuid4()),
        )

    with pytest.raises(AdaptivePracticeConflict, match="not eligible"):
        repository.start_v2(
            readiness_signal_version_id=version_id,
            target_application_event_id=int(seeded["event_id"]),
            expected_source_fingerprint=source_fingerprint,
            expected_target_fingerprint="sha256:" + "c" * 64,
            idempotency_key=str(uuid4()),
        )

    with session_factory() as session:
        target = session.get(ApplicationEvent, target_id)
        assert target is not None
        target.status = "completed"
        session.commit()
    completed_target_fingerprint = _exact_pair(
        session_factory,
        version_id,
        target_id,
    )[1]
    with pytest.raises(AdaptivePracticeConflict, match="not eligible"):
        repository.start_v2(
            readiness_signal_version_id=version_id,
            target_application_event_id=target_id,
            expected_source_fingerprint=source_fingerprint,
            expected_target_fingerprint=completed_target_fingerprint,
            idempotency_key=str(uuid4()),
        )

    with session_factory() as session:
        other_application = Application(
            company_name="Other",
            position_name="Backend",
            source="web",
        )
        session.add(other_application)
        session.commit()
        other_application_id = other_application.id
    other_target_id = _add_target(session_factory, other_application_id)
    with pytest.raises(AdaptivePracticeNotFound):
        repository.start_v2(
            readiness_signal_version_id=version_id,
            target_application_event_id=other_target_id,
            expected_source_fingerprint=source_fingerprint,
            expected_target_fingerprint="sha256:" + "d" * 64,
            idempotency_key=str(uuid4()),
        )
    with session_factory() as session:
        assert session.scalar(select(AdaptivePracticePlan)) is None


def test_v2_new_key_rechecks_actual_source_target_drift_and_missing(tmp_path) -> None:
    (
        session_factory,
        seeded,
        version_id,
        target_id,
        source_fingerprint,
        target_fingerprint,
    ) = _seed_v2(tmp_path)
    repository = AdaptivePracticeRepository(session_factory)
    with pytest.raises(AdaptivePracticeNotFound):
        repository.start_v2(
            readiness_signal_version_id=version_id,
            target_application_event_id=2**31,
            expected_source_fingerprint=source_fingerprint,
            expected_target_fingerprint="sha256:" + "a" * 64,
            idempotency_key=str(uuid4()),
        )

    with session_factory() as session:
        target = session.get(ApplicationEvent, target_id)
        assert target is not None
        target.tags = ["changed"]
        session.commit()
    with pytest.raises(AdaptivePracticeConflict, match="target"):
        repository.start_v2(
            readiness_signal_version_id=version_id,
            target_application_event_id=target_id,
            expected_source_fingerprint=source_fingerprint,
            expected_target_fingerprint=target_fingerprint,
            idempotency_key=str(uuid4()),
        )

    with session_factory() as session:
        target = session.get(ApplicationEvent, target_id)
        note = session.get(InterviewNote, int(seeded["note_id"]))
        assert target is not None and note is not None
        target.tags = ["onsite", "architecture"]
        note.content_revision += 1
        session.commit()
    with pytest.raises(AdaptivePracticeConflict, match="source changed"):
        repository.start_v2(
            readiness_signal_version_id=version_id,
            target_application_event_id=target_id,
            expected_source_fingerprint=source_fingerprint,
            expected_target_fingerprint=target_fingerprint,
            idempotency_key=str(uuid4()),
        )
    with session_factory() as session:
        note = session.get(InterviewNote, int(seeded["note_id"]))
        assert note is not None
        session.delete(note)
        session.commit()
    with pytest.raises(AdaptivePracticeNotFound):
        repository.start_v2(
            readiness_signal_version_id=version_id,
            target_application_event_id=target_id,
            expected_source_fingerprint=source_fingerprint,
            expected_target_fingerprint=target_fingerprint,
            idempotency_key=str(uuid4()),
        )
    with session_factory() as session:
        assert session.scalar(select(AdaptivePracticePlan)) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("readiness_signal_version_id", True),
        ("target_application_event_id", 0),
        ("target_application_event_id", 2**63),
        ("expected_source_fingerprint", "SHA256:" + "a" * 64),
        ("expected_target_fingerprint", "sha256:" + "G" * 64),
        ("idempotency_key", ""),
        ("idempotency_key", "not-a-uuid"),
    ],
)
def test_v2_start_rejects_non_exact_trusted_input(tmp_path, field, value) -> None:
    session_factory = init_database(tmp_path / "invalid.db")
    request = {
        "readiness_signal_version_id": 1,
        "target_application_event_id": 2,
        "expected_source_fingerprint": "sha256:" + "a" * 64,
        "expected_target_fingerprint": "sha256:" + "b" * 64,
        "idempotency_key": str(uuid4()),
    }
    request[field] = value
    with pytest.raises(AdaptivePracticeValidationError):
        AdaptivePracticeRepository(session_factory).start_v2(**request)


def test_v2_same_signal_supports_distinct_targets_but_pair_has_one_winner(tmp_path) -> None:
    (
        session_factory,
        seeded,
        version_id,
        first_target_id,
        source_fingerprint,
        first_target_fingerprint,
    ) = _seed_v2(tmp_path)
    repository = AdaptivePracticeRepository(session_factory)
    first, _ = repository.start_v2(
        readiness_signal_version_id=version_id,
        target_application_event_id=first_target_id,
        expected_source_fingerprint=source_fingerprint,
        expected_target_fingerprint=first_target_fingerprint,
        idempotency_key=str(uuid4()),
    )
    with pytest.raises(AdaptivePracticeConflict, match="in progress"):
        repository.start_v2(
            readiness_signal_version_id=version_id,
            target_application_event_id=first_target_id,
            expected_source_fingerprint=source_fingerprint,
            expected_target_fingerprint=first_target_fingerprint,
            idempotency_key=str(uuid4()),
        )

    second_target_id = _add_target(
        session_factory,
        int(seeded["application_id"]),
        status="in_progress",
    )
    second_source_fingerprint, second_target_fingerprint, state = _exact_pair(
        session_factory,
        version_id,
        second_target_id,
    )
    assert state == "ready"
    second, created = repository.start_v2(
        readiness_signal_version_id=version_id,
        target_application_event_id=second_target_id,
        expected_source_fingerprint=second_source_fingerprint,
        expected_target_fingerprint=second_target_fingerprint,
        idempotency_key=str(uuid4()),
    )
    assert created is True
    assert first["id"] != second["id"]
    assert first["target_application_event_id"] != second["target_application_event_id"]


def test_v2_concurrent_same_pair_and_key_converges_to_one_plan(tmp_path) -> None:
    (
        session_factory,
        _seeded,
        version_id,
        target_id,
        source_fingerprint,
        target_fingerprint,
    ) = _seed_v2(tmp_path)
    repository = AdaptivePracticeRepository(session_factory)
    start_key = str(uuid4())

    def start():  # type: ignore[no-untyped-def]
        return repository.start_v2(
            readiness_signal_version_id=version_id,
            target_application_event_id=target_id,
            expected_source_fingerprint=source_fingerprint,
            expected_target_fingerprint=target_fingerprint,
            idempotency_key=start_key,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(start), pool.submit(start)]]

    assert {result[0]["id"] for result in results} == {results[0][0]["id"]}
    assert sorted(result[1] for result in results) == [False, True]
    with session_factory() as session:
        assert len(tuple(session.scalars(select(AdaptivePracticePlan)))) == 1


def test_v2_concurrent_distinct_keys_for_same_pair_have_one_winner(tmp_path) -> None:
    (
        session_factory,
        _seeded,
        version_id,
        target_id,
        source_fingerprint,
        target_fingerprint,
    ) = _seed_v2(tmp_path)
    repository = AdaptivePracticeRepository(session_factory)

    def start(start_key: str):  # type: ignore[no-untyped-def]
        try:
            return (
                "ok",
                repository.start_v2(
                    readiness_signal_version_id=version_id,
                    target_application_event_id=target_id,
                    expected_source_fingerprint=source_fingerprint,
                    expected_target_fingerprint=target_fingerprint,
                    idempotency_key=start_key,
                ),
            )
        except AdaptivePracticeConflict as exc:
            return "conflict", exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result()
            for future in [
                pool.submit(start, str(uuid4())),
                pool.submit(start, str(uuid4())),
            ]
        ]

    assert sorted(result[0] for result in results) == ["conflict", "ok"]
    winner = next(result[1] for result in results if result[0] == "ok")
    assert winner[1] is True
    with session_factory() as session:
        plans = tuple(session.scalars(select(AdaptivePracticePlan)))
        assert len(plans) == 1
        assert plans[0].id == winner[0]["id"]


def test_v2_integrity_failure_without_persisted_winner_is_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    (
        session_factory,
        _seeded,
        version_id,
        target_id,
        source_fingerprint,
        target_fingerprint,
    ) = _seed_v2(tmp_path)

    def fail_commit(_session):  # type: ignore[no-untyped-def]
        raise IntegrityError("forced insert failure", {}, RuntimeError("storage"))

    monkeypatch.setattr(Session, "commit", fail_commit)
    with pytest.raises(AdaptivePracticeUnavailable, match="persist"):
        AdaptivePracticeRepository(session_factory).start_v2(
            readiness_signal_version_id=version_id,
            target_application_event_id=target_id,
            expected_source_fingerprint=source_fingerprint,
            expected_target_fingerprint=target_fingerprint,
            idempotency_key=str(uuid4()),
        )


@pytest.mark.parametrize("operation", ["list", "get"])
def test_plan_read_storage_failures_are_unavailable(tmp_path, monkeypatch, operation: str) -> None:
    session_factory, _, _, _, proposal_id = _setup(tmp_path)
    plan_id, _focus_id, _source_fingerprint, _start_key = _seed_legacy_plan(
        session_factory,
        proposal_id=proposal_id,
    )
    repository = AdaptivePracticeRepository(session_factory)

    def fail_read(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OperationalError("SELECT", {}, RuntimeError("offline"))

    if operation == "list":
        monkeypatch.setattr(Session, "scalars", fail_read)
        with pytest.raises(AdaptivePracticeUnavailable):
            repository.list_plans()
    else:
        monkeypatch.setattr(Session, "get", fail_read)
        with pytest.raises(AdaptivePracticeUnavailable):
            repository.get(plan_id)


def test_begin_and_idempotency_lookup_storage_failures_are_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    (
        session_factory,
        _seeded,
        version_id,
        target_id,
        source_fingerprint,
        target_fingerprint,
    ) = _seed_v2(tmp_path)
    repository = AdaptivePracticeRepository(session_factory)
    request = {
        "readiness_signal_version_id": version_id,
        "target_application_event_id": target_id,
        "expected_source_fingerprint": source_fingerprint,
        "expected_target_fingerprint": target_fingerprint,
        "idempotency_key": str(uuid4()),
    }

    def fail_storage(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OperationalError("SELECT", {}, RuntimeError("offline"))

    with monkeypatch.context() as scoped:
        scoped.setattr(Session, "execute", fail_storage)
        with pytest.raises(AdaptivePracticeUnavailable):
            repository.start_v2(**request)
    with monkeypatch.context() as scoped:
        scoped.setattr(Session, "scalar", fail_storage)
        with pytest.raises(AdaptivePracticeUnavailable):
            repository.start_v2(**request)


def test_non_integrity_commit_failures_are_unavailable(tmp_path, monkeypatch) -> None:
    (
        session_factory,
        _seeded,
        version_id,
        target_id,
        source_fingerprint,
        target_fingerprint,
    ) = _seed_v2(tmp_path)
    repository = AdaptivePracticeRepository(session_factory)
    request = {
        "readiness_signal_version_id": version_id,
        "target_application_event_id": target_id,
        "expected_source_fingerprint": source_fingerprint,
        "expected_target_fingerprint": target_fingerprint,
        "idempotency_key": str(uuid4()),
    }

    def fail_commit(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OperationalError("COMMIT", {}, RuntimeError("offline"))

    monkeypatch.setattr(Session, "commit", fail_commit)
    with pytest.raises(AdaptivePracticeUnavailable):
        repository.start_v2(**request)


def test_completion_storage_failures_are_unavailable(tmp_path, monkeypatch) -> None:
    (
        session_factory,
        _seeded,
        version_id,
        target_id,
        source_fingerprint,
        target_fingerprint,
    ) = _seed_v2(tmp_path)
    repository = AdaptivePracticeRepository(session_factory)
    plan, _ = repository.start_v2(
        readiness_signal_version_id=version_id,
        target_application_event_id=target_id,
        expected_source_fingerprint=source_fingerprint,
        expected_target_fingerprint=target_fingerprint,
        idempotency_key=str(uuid4()),
    )

    def fail_storage(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OperationalError("COMMIT", {}, RuntimeError("offline"))

    completion = {
        "plan_id": plan["id"],
        "expected_revision": 1,
        "response_text": "回答",
        "reflection_text": "",
        "self_assessment": "clearer",
        "idempotency_key": str(uuid4()),
    }
    with monkeypatch.context() as scoped:
        scoped.setattr(Session, "execute", fail_storage)
        with pytest.raises(AdaptivePracticeUnavailable):
            repository.complete(**completion)
    with monkeypatch.context() as scoped:
        scoped.setattr(Session, "commit", fail_storage)
        with pytest.raises(AdaptivePracticeUnavailable):
            repository.complete(**completion)


def test_v2_commit_unknown_recovers_same_idempotency_winner_without_live_reload(
    tmp_path,
    monkeypatch,
) -> None:
    (
        session_factory,
        _seeded,
        version_id,
        target_id,
        source_fingerprint,
        target_fingerprint,
    ) = _seed_v2(tmp_path)
    original_commit = Session.commit

    def commit_then_raise(session):  # type: ignore[no-untyped-def]
        original_commit(session)
        raise IntegrityError("commit result unknown", {}, RuntimeError("lost ack"))

    monkeypatch.setattr(Session, "commit", commit_then_raise)
    recovered, created = AdaptivePracticeRepository(session_factory).start_v2(
        readiness_signal_version_id=version_id,
        target_application_event_id=target_id,
        expected_source_fingerprint=source_fingerprint,
        expected_target_fingerprint=target_fingerprint,
        idempotency_key=str(uuid4()),
    )

    assert created is False
    with session_factory() as session:
        assert session.get(AdaptivePracticePlan, recovered["id"]) is not None


def test_v2_corrupt_evidence_fails_closed_without_plan_write(tmp_path) -> None:
    (
        session_factory,
        seeded,
        version_id,
        target_id,
        source_fingerprint,
        target_fingerprint,
    ) = _seed_v2(tmp_path)
    repository = AdaptivePracticeRepository(session_factory)
    with session_factory() as session:
        evidence = list(
            session.scalars(
                select(InterviewReadinessSignalEvidence)
                .where(InterviewReadinessSignalEvidence.signal_version_id == version_id)
                .order_by(InterviewReadinessSignalEvidence.ordinal)
            )
        )
        assert [row.ordinal for row in evidence] == [0, 1]
        secondary = evidence[1]
        original_hash = secondary.source_field_sha256
        secondary.source_field_sha256 = "sha256:" + sha256_text("tampered-secondary-field")
        session.commit()
    with pytest.raises(AdaptivePracticeUnavailable):
        repository.start_v2(
            readiness_signal_version_id=version_id,
            target_application_event_id=target_id,
            expected_source_fingerprint=source_fingerprint,
            expected_target_fingerprint=target_fingerprint,
            idempotency_key=str(uuid4()),
        )
    with session_factory() as session:
        secondary = session.scalar(
            select(InterviewReadinessSignalEvidence).where(
                InterviewReadinessSignalEvidence.signal_version_id == version_id,
                InterviewReadinessSignalEvidence.ordinal == 1,
            )
        )
        assert secondary is not None
        secondary.source_field_sha256 = original_hash
        session.commit()

    with session_factory() as session:
        primary = session.scalar(
            select(InterviewReadinessSignalEvidence).where(
                InterviewReadinessSignalEvidence.signal_version_id == version_id,
                InterviewReadinessSignalEvidence.ordinal == 0,
            )
        )
        assert primary is not None
        primary.ordinal = 2
        session.commit()
    with pytest.raises(AdaptivePracticeUnavailable):
        repository.start_v2(
            readiness_signal_version_id=version_id,
            target_application_event_id=target_id,
            expected_source_fingerprint=source_fingerprint,
            expected_target_fingerprint=target_fingerprint,
            idempotency_key=str(uuid4()),
        )
    with session_factory() as session:
        assert session.scalar(select(AdaptivePracticePlan)) is None
        with pytest.raises(IntegrityError):
            session.execute(
                text(
                    "INSERT INTO interview_readiness_signal_evidence("
                    "signal_version_id,ordinal,source_path,excerpt,excerpt_sha256,"
                    "source_field_sha256) VALUES (:version_id,5,'/questions','x',"
                    ":excerpt_hash,:field_hash)"
                ),
                {
                    "version_id": version_id,
                    "excerpt_hash": "sha256:" + sha256_text("x"),
                    "field_hash": "sha256:" + sha256_text("x"),
                },
            )
            session.commit()


def test_v2_frozen_history_and_completion_survive_null_locators(tmp_path) -> None:
    (
        session_factory,
        seeded,
        version_id,
        target_id,
        source_fingerprint,
        target_fingerprint,
    ) = _seed_v2(tmp_path)
    repository = AdaptivePracticeRepository(session_factory)
    plan, _ = repository.start_v2(
        readiness_signal_version_id=version_id,
        target_application_event_id=target_id,
        expected_source_fingerprint=source_fingerprint,
        expected_target_fingerprint=target_fingerprint,
        idempotency_key=str(uuid4()),
    )
    with session_factory() as session:
        note = session.get(InterviewNote, int(seeded["note_id"]))
        target = session.get(ApplicationEvent, target_id)
        assert note is not None and target is not None
        session.delete(target)
        session.commit()
    with session_factory() as session:
        persisted = session.get(AdaptivePracticePlan, plan["id"])
        note = session.get(InterviewNote, int(seeded["note_id"]))
        assert persisted is not None and note is not None
        assert persisted.target_application_event_id is None
        persisted.readiness_signal_version_id = None
        session.delete(note)
        session.commit()

    frozen = repository.get(plan["id"])
    assert frozen["readiness_signal_version_id"] is None
    assert frozen["target_application_event_id"] is None
    assert frozen["source_fingerprint"] == source_fingerprint
    assert frozen["target_fingerprint"] == target_fingerprint
    assert repository.list_plans()[0]["id"] == plan["id"]
    completed, created = repository.complete(
        plan_id=plan["id"],
        expected_revision=1,
        response_text="先澄清容量约束，再说明一致性取舍。",
        reflection_text="回答结构更稳定。",
        self_assessment="clearer",
        idempotency_key=str(uuid4()),
    )
    assert created is True
    assert completed["status"] == "completed"
    assert completed["source_fingerprint"] == source_fingerprint
    assert completed["target_fingerprint"] == target_fingerprint


def test_v2_plan_exposes_every_exact_canonical_practice_state(
    tmp_path,
    monkeypatch,
) -> None:
    (
        session_factory,
        _seeded,
        version_id,
        target_id,
        source_fingerprint,
        target_fingerprint,
    ) = _seed_v2(tmp_path)
    repository = AdaptivePracticeRepository(session_factory)
    plan, _ = repository.start_v2(
        readiness_signal_version_id=version_id,
        target_application_event_id=target_id,
        expected_source_fingerprint=source_fingerprint,
        expected_target_fingerprint=target_fingerprint,
        idempotency_key=str(uuid4()),
    )
    projected_state = "ready"

    def exact_projection(*args, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(state=projected_state)

    monkeypatch.setattr(
        practice_repository_module,
        "project_practice_focus",
        exact_projection,
    )
    exact_states = (
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
    )
    for projected_state in exact_states:
        serialized = repository.get(plan["id"])
        assert serialized["practice_state"] == projected_state
        assert serialized["source_status"] is None


def test_v2_plan_distinguishes_independent_source_and_target_null_locators(
    tmp_path,
) -> None:
    (
        target_factory,
        _seeded,
        version_id,
        target_id,
        source_fingerprint,
        target_fingerprint,
    ) = _seed_v2(tmp_path / "target")
    target_repository = AdaptivePracticeRepository(target_factory)
    target_plan, _ = target_repository.start_v2(
        readiness_signal_version_id=version_id,
        target_application_event_id=target_id,
        expected_source_fingerprint=source_fingerprint,
        expected_target_fingerprint=target_fingerprint,
        idempotency_key=str(uuid4()),
    )
    with target_factory() as session:
        target = session.get(ApplicationEvent, target_id)
        assert target is not None
        session.delete(target)
        session.commit()
    assert target_repository.get(target_plan["id"])["practice_state"] == "target_missing"

    (
        source_factory,
        _seeded,
        source_version_id,
        source_target_id,
        source_source_fingerprint,
        source_target_fingerprint,
    ) = _seed_v2(tmp_path / "source")
    source_repository = AdaptivePracticeRepository(source_factory)
    source_plan, _ = source_repository.start_v2(
        readiness_signal_version_id=source_version_id,
        target_application_event_id=source_target_id,
        expected_source_fingerprint=source_source_fingerprint,
        expected_target_fingerprint=source_target_fingerprint,
        idempotency_key=str(uuid4()),
    )
    with source_factory() as session:
        persisted = session.get(AdaptivePracticePlan, source_plan["id"])
        assert persisted is not None
        persisted.readiness_signal_version_id = None
        session.commit()
    assert source_repository.get(source_plan["id"])["practice_state"] == "source_missing"
