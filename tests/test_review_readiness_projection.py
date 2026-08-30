from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from offerpilot.db import init_database
from offerpilot.models import (
    AdaptivePracticePlan,
    Application,
    ApplicationEvent,
    InterviewNote,
    InterviewReadinessSignal,
    InterviewReadinessSignalEvidence,
    InterviewReadinessSignalVersion,
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
    compute_practice_target_fingerprint_v1,
    load_canonical_readiness_signal,
    project_practice_focus,
)
from offerpilot.review_readiness.repository import ReadinessSignalRepository
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


def _commit_signal(
    session_factory,  # type: ignore[no-untyped-def]
    seeded: dict[str, object],
    *,
    focus_id: str,
    idempotency_key: str,
    user_note: str = "下次先说约束。",
):
    with session_factory() as session:
        resolved = resolve_readiness_candidates(
            int(seeded["note_id"]),
            int(seeded["proposal_id"]),
            session,
        )
        candidate = next(item for item in resolved.candidates if item.focus_id == focus_id)
    coordinator = _coordinator(session_factory)
    proposed = coordinator.propose_readiness_signal(
        note_id=int(seeded["note_id"]),
        request={
            "proposal_id": int(seeded["proposal_id"]),
            "focus_id": focus_id,
            "expected_note_revision": int(seeded["note_revision"]),
            "expected_candidate_fingerprint": candidate.candidate_fingerprint,
            "idempotency_key": idempotency_key,
            "user_note": user_note,
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
        signal = session.scalar(
            select(InterviewReadinessSignal).where(
                InterviewReadinessSignal.focus_id == focus_id
            )
        )
        assert signal is not None and signal.current_version_id is not None
        return signal.id, signal.current_version_id


def _reject_signal_operation(
    session_factory,  # type: ignore[no-untyped-def]
    seeded: dict[str, object],
    *,
    focus_id: str,
) -> str:
    with session_factory() as session:
        candidate = next(
            item
            for item in resolve_readiness_candidates(
                int(seeded["note_id"]),
                int(seeded["proposal_id"]),
                session,
            ).candidates
            if item.focus_id == focus_id
        )
    coordinator = _coordinator(session_factory)
    proposed = coordinator.propose_readiness_signal(
        note_id=int(seeded["note_id"]),
        request={
            "proposal_id": int(seeded["proposal_id"]),
            "focus_id": focus_id,
            "expected_note_revision": int(seeded["note_revision"]),
            "expected_candidate_fingerprint": candidate.candidate_fingerprint,
            "idempotency_key": str(uuid4()),
            "user_note": "",
        },
    )
    rejected = coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": proposed.confirmation_token,
            "decision": "reject",
        },
    )
    assert rejected.status == "rejected"
    return proposed.operation_id


def _seed_current_signal(tmp_path, *, practice_focuses=None):  # type: ignore[no-untyped-def]
    session_factory = init_database(tmp_path / f"projection-{uuid4()}.sqlite3")
    seeded = seed_review_candidate(session_factory, practice_focuses=practice_focuses)
    signal_id, version_id = _commit_signal(
        session_factory,
        seeded,
        focus_id=str(seeded["focus_id"]),
        idempotency_key=str(uuid4()),
    )
    return session_factory, seeded, signal_id, version_id


def _target(session_factory, application_id: int, *, status: str = "todo", **values):  # type: ignore[no-untyped-def]
    with session_factory() as session:
        event = ApplicationEvent(
            application_id=application_id,
            event_type=values.pop("event_type", "interview"),
            subtype=values.pop("subtype", "system_design"),
            round=values.pop("round", 3),
            scheduled_at=values.pop(
                "scheduled_at",
                datetime(2001, 1, 1, 9, tzinfo=timezone.utc),
            ),
            duration_minutes=values.pop("duration_minutes", 60),
            status=status,
        )
        event.tags = values.pop("tags", ["onsite", "architecture"])
        assert not values
        session.add(event)
        session.commit()
        return event.id


def _completed_plan(
    session,
    *,
    seeded: dict[str, object],
    version_id: int,
    target_id: int,
    source_fingerprint: str,
    target_fingerprint: str,
) -> AdaptivePracticePlan:  # type: ignore[no-untyped-def]
    plan = AdaptivePracticePlan(
        application_id=int(seeded["application_id"]),
        application_event_id=int(seeded["event_id"]),
        interview_note_id=int(seeded["note_id"]),
        interview_review_proposal_id=int(seeded["proposal_id"]),
        focus_id=str(seeded["focus_id"]),
        start_idempotency_key=str(uuid4()),
        start_input_fingerprint="sha256:" + "a" * 64,
        source_fingerprint=source_fingerprint,
        source_path="/difficulty_points",
        source_excerpt="safe excerpt",
        source_hash="sha256:" + "c" * 64,
        drill_kind="explain",
        title="Practice",
        observation="Observation",
        reason="Reason",
        prompt="Prompt",
        status="completed",
        revision=2,
        response_text="Response",
        reflection_text="Reflection",
        self_assessment="improved",
        completion_idempotency_key=str(uuid4()),
        completion_fingerprint="sha256:" + "d" * 64,
        completed_at=datetime.now(timezone.utc),
        origin_contract="confirmed_readiness_signal_v1",
        readiness_signal_version_id=version_id,
        target_application_event_id=target_id,
        target_fingerprint=target_fingerprint,
    )
    session.add(plan)
    session.flush()
    return plan


def test_current_aggregate_has_pinned_source_and_target_fingerprints(tmp_path) -> None:
    session_factory, seeded, signal_id, version_id = _seed_current_signal(tmp_path)
    target_id = _target(session_factory, int(seeded["application_id"]))

    with session_factory() as session:
        source = load_canonical_readiness_signal(session, signal_id=signal_id)
        focus = project_practice_focus(
            session,
            signal_version_id=version_id,
            target_event_id=target_id,
        )

    assert source.state == "current"
    assert source.aggregate is not None
    assert source.aggregate.practice_source_fingerprint == (
        "sha256:fe9e86890396731be34d319fecb60e64cd49e9dca3f737f2174f864119214833"
    )
    assert focus.state == "ready"
    assert focus.target is not None
    assert focus.target.practice_target_fingerprint == (
        "sha256:050edd5c8bcd8e82b6ab934c44f999970b724e93e60d8b87a2d36b66d0d472cb"
    )


def test_exact_candidate_resolver_keeps_confirmed_focus_for_source_validation(tmp_path) -> None:
    session_factory, seeded, _signal_id, _version_id = _seed_current_signal(tmp_path)

    with session_factory() as session:
        exact = resolve_readiness_candidates(
            int(seeded["note_id"]), int(seeded["proposal_id"]), session
        )
        public = project_readiness_candidates(
            int(seeded["note_id"]), int(seeded["proposal_id"]), session
        )

    assert exact.state == "ready"
    assert [item.focus_id for item in exact.candidates] == [seeded["focus_id"]]
    assert public.state == "already_confirmed"


def test_source_state_changed_missing_soft_deleted_and_hard_deleted(tmp_path) -> None:
    changed_factory, changed, changed_signal, _version = _seed_current_signal(tmp_path)
    with changed_factory() as session:
        note = session.get(InterviewNote, int(changed["note_id"]))
        assert note is not None
        note.content_revision += 1
        session.commit()
    with changed_factory() as session:
        assert load_canonical_readiness_signal(session, signal_id=changed_signal).state == "changed"

    missing_factory, _seeded, missing_signal, _version = _seed_current_signal(tmp_path)
    with missing_factory() as session:
        signal = session.get(InterviewReadinessSignal, missing_signal)
        assert signal is not None
        signal.source_event_id = None
        session.commit()
    with missing_factory() as session:
        assert load_canonical_readiness_signal(session, signal_id=missing_signal).state == "missing"

    soft_factory, soft, soft_signal, _version = _seed_current_signal(tmp_path)
    with soft_factory() as session:
        application = session.get(Application, int(soft["application_id"]))
        assert application is not None
        application.deleted_at = datetime.now(timezone.utc)
        session.commit()
    with soft_factory() as session:
        assert load_canonical_readiness_signal(session, signal_id=soft_signal).state == "unavailable"

    hard_factory, hard, hard_signal, _version = _seed_current_signal(tmp_path)
    with hard_factory() as session:
        application = session.get(Application, int(hard["application_id"]))
        assert application is not None
        session.delete(application)
        session.commit()
    with hard_factory() as session:
        assert load_canonical_readiness_signal(session, signal_id=hard_signal).state == "missing"


def test_valid_current_proposal_identity_drift_is_changed_not_corruption(tmp_path) -> None:
    session_factory, seeded, signal_id, _version_id = _seed_current_signal(tmp_path)
    with session_factory() as session:
        proposal = session.get(InterviewReviewProposal, int(seeded["proposal_id"]))
        assert proposal is not None
        payload = json.loads(proposal.proposal_json)
        payload["practice_focuses"][0]["text"] = "A newly reviewed exact focus."
        proposal.proposal_json = canonical_json(payload)
        proposal.proposal_hash = sha256_text(proposal.proposal_json)
        session.commit()

    with session_factory() as session:
        projection = load_canonical_readiness_signal(session, signal_id=signal_id)
    assert projection.state == "changed"
    assert projection.error_code is None


def test_retracted_current_version_has_priority_over_missing_sources(tmp_path) -> None:
    focuses = [
        {
            "id": focus_id,
            "text": f"Focus {focus_id}",
            "evidence_refs": [
                {
                    "source": "interview_note",
                    "path": "/difficulty_points",
                    "excerpt": "cache consistency tradeoffs",
                }
            ],
        }
        for focus_id in ("focus-1", "focus-2")
    ]
    session_factory, seeded, signal_id, version_id = _seed_current_signal(
        tmp_path,
        practice_focuses=focuses,
    )
    with session_factory() as session:
        second = resolve_readiness_candidates(
            int(seeded["note_id"]), int(seeded["proposal_id"]), session
        ).candidates[1]
    coordinator = _coordinator(session_factory)
    proposed = coordinator.propose_readiness_signal(
        note_id=int(seeded["note_id"]),
        request={
            "proposal_id": int(seeded["proposal_id"]),
            "focus_id": "focus-2",
            "expected_note_revision": int(seeded["note_revision"]),
            "expected_candidate_fingerprint": second.candidate_fingerprint,
            "idempotency_key": str(uuid4()),
            "user_note": "",
        },
    )
    rejected = coordinator.decide(
        operation_id=proposed.operation_id,
        request={
            "confirmation_token": proposed.confirmation_token,
            "decision": "reject",
        },
    )
    assert rejected.status == "rejected"
    with session_factory() as session:
        parent = session.get(InterviewReadinessSignalVersion, version_id)
        signal = session.get(InterviewReadinessSignal, signal_id)
        assert parent is not None and signal is not None
        rows = tuple(
            session.scalars(
                select(InterviewReadinessSignalEvidence).where(
                    InterviewReadinessSignalEvidence.signal_version_id == version_id
                )
            )
        )
        child = InterviewReadinessSignalVersion(
            signal_id=signal_id,
            version_number=2,
            parent_version_id=version_id,
            disposition="retracted",
            schema_version=parent.schema_version,
            statement_text=parent.statement_text,
            user_note=parent.user_note,
            source_note_revision=parent.source_note_revision,
            source_note_fingerprint=parent.source_note_fingerprint,
            source_proposal_hash=parent.source_proposal_hash,
            candidate_fingerprint=parent.candidate_fingerprint,
            domain_idempotency_key=str(uuid4()),
            write_operation_id=proposed.operation_id,
        )
        session.add(child)
        session.flush()
        session.add_all(
            InterviewReadinessSignalEvidence(
                signal_version_id=child.id,
                ordinal=row.ordinal,
                source_path=row.source_path,
                excerpt=row.excerpt,
                excerpt_sha256=row.excerpt_sha256,
                source_field_sha256=row.source_field_sha256,
            )
            for row in rows
        )
        signal.current_version_id = child.id
        signal.revision = 2
        signal.source_event_id = None
        session.commit()
        child_id = child.id
    with session_factory() as session:
        projection = load_canonical_readiness_signal(
            session,
            signal_version_id=child_id,
        )
    assert projection.state == "retracted"
    with session_factory() as session:
        historical = load_canonical_readiness_signal(
            session,
            signal_version_id=version_id,
        )
    assert historical.state == "retracted"
    assert historical.aggregate is not None
    assert historical.aggregate.version_id == child_id


def test_noncurrent_and_dangling_version_locators_are_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, _seeded, signal_id, version_id = _seed_current_signal(tmp_path)
    with session_factory() as session:
        original_get = session.get

        def unrelated_locator(model, ident, *args, **kwargs):  # type: ignore[no-untyped-def]
            if model is InterviewReadinessSignalVersion and ident == 999_991:
                return SimpleNamespace(id=ident, signal_id=signal_id)
            return original_get(model, ident, *args, **kwargs)

        monkeypatch.setattr(session, "get", unrelated_locator)
        noncurrent = load_canonical_readiness_signal(
            session,
            signal_version_id=999_991,
        )
    assert noncurrent.state == "unavailable"

    with session_factory() as session:
        original_get = session.get

        def dangling_locator(model, ident, *args, **kwargs):  # type: ignore[no-untyped-def]
            if model is InterviewReadinessSignalVersion and ident == 999_992:
                return SimpleNamespace(id=ident, signal_id=999_999)
            return original_get(model, ident, *args, **kwargs)

        monkeypatch.setattr(session, "get", dangling_locator)
        dangling = load_canonical_readiness_signal(
            session,
            signal_version_id=999_992,
        )
    assert dangling.state == "unavailable"

    with session_factory() as session:
        current = load_canonical_readiness_signal(session, signal_version_id=version_id)
    assert current.state == "current"


def test_active_v2_to_retracted_v3_lineage_is_unavailable(tmp_path) -> None:
    focuses = [
        {
            "id": focus_id,
            "text": f"Focus {focus_id}",
            "evidence_refs": [
                {
                    "source": "interview_note",
                    "path": "/difficulty_points",
                    "excerpt": "cache consistency tradeoffs",
                }
            ],
        }
        for focus_id in ("focus-1", "focus-2", "focus-3")
    ]
    session_factory, seeded, signal_id, version_id = _seed_current_signal(
        tmp_path,
        practice_focuses=focuses,
    )
    active_v2_operation = _reject_signal_operation(
        session_factory,
        seeded,
        focus_id="focus-2",
    )
    retracted_v3_operation = _reject_signal_operation(
        session_factory,
        seeded,
        focus_id="focus-3",
    )
    with session_factory() as session:
        v1 = session.get(InterviewReadinessSignalVersion, version_id)
        signal = session.get(InterviewReadinessSignal, signal_id)
        assert v1 is not None and signal is not None
        v1_evidence = tuple(
            session.scalars(
                select(InterviewReadinessSignalEvidence).where(
                    InterviewReadinessSignalEvidence.signal_version_id == version_id
                )
            )
        )

        def version(disposition, number, parent_id, operation_id):  # type: ignore[no-untyped-def]
            row = InterviewReadinessSignalVersion(
                signal_id=signal_id,
                version_number=number,
                parent_version_id=parent_id,
                disposition=disposition,
                schema_version=v1.schema_version,
                statement_text=v1.statement_text,
                user_note=v1.user_note,
                source_note_revision=v1.source_note_revision,
                source_note_fingerprint=v1.source_note_fingerprint,
                source_proposal_hash=v1.source_proposal_hash,
                candidate_fingerprint=v1.candidate_fingerprint,
                domain_idempotency_key=str(uuid4()),
                write_operation_id=operation_id,
            )
            session.add(row)
            session.flush()
            session.add_all(
                InterviewReadinessSignalEvidence(
                    signal_version_id=row.id,
                    ordinal=item.ordinal,
                    source_path=item.source_path,
                    excerpt=item.excerpt,
                    excerpt_sha256=item.excerpt_sha256,
                    source_field_sha256=item.source_field_sha256,
                )
                for item in v1_evidence
            )
            session.flush()
            return row

        active_v2 = version("active", 2, None, active_v2_operation)
        retracted_v3 = version(
            "retracted",
            3,
            active_v2.id,
            retracted_v3_operation,
        )
        signal.current_version_id = retracted_v3.id
        signal.revision = 3
        active_v2_id = active_v2.id
        session.commit()

    with session_factory() as session:
        projection = load_canonical_readiness_signal(session, signal_id=signal_id)
    assert projection.state == "unavailable"
    with session_factory() as session:
        noncurrent = load_canonical_readiness_signal(
            session,
            signal_version_id=active_v2_id,
        )
    assert noncurrent.state == "unavailable"
    assert noncurrent.error_code == "locator_not_current_lineage"


def test_evidence_prefix_and_hash_corruption_fail_closed(tmp_path) -> None:
    gap_factory, _seeded, gap_signal, gap_version = _seed_current_signal(tmp_path)
    with gap_factory() as session:
        session.execute(
            update(InterviewReadinessSignalEvidence)
            .where(InterviewReadinessSignalEvidence.signal_version_id == gap_version)
            .values(ordinal=1)
        )
        session.commit()
    with gap_factory() as session:
        projection = load_canonical_readiness_signal(session, signal_id=gap_signal)
    assert projection.state == "unavailable"
    assert projection.error_code == "aggregate_unreadable"

    focuses = [
        {
            "id": "focus-1",
            "text": "Gap check",
            "evidence_refs": [
                {
                    "source": "interview_note",
                    "path": "/difficulty_points",
                    "excerpt": "cache consistency tradeoffs",
                }
                for _index in range(5)
            ],
        }
    ]
    prefix_factory, _seeded, prefix_signal, prefix_version = _seed_current_signal(
        tmp_path,
        practice_focuses=focuses,
    )
    with prefix_factory() as session:
        gap = session.scalar(
            select(InterviewReadinessSignalEvidence).where(
                InterviewReadinessSignalEvidence.signal_version_id == prefix_version,
                InterviewReadinessSignalEvidence.ordinal == 3,
            )
        )
        assert gap is not None
        session.delete(gap)
        session.commit()
    with prefix_factory() as session:
        assert load_canonical_readiness_signal(
            session,
            signal_id=prefix_signal,
        ).state == "unavailable"

    hash_factory, _seeded, hash_signal, hash_version = _seed_current_signal(tmp_path)
    with hash_factory() as session:
        session.execute(
            update(InterviewReadinessSignalEvidence)
            .where(InterviewReadinessSignalEvidence.signal_version_id == hash_version)
            .values(excerpt_sha256="sha256:" + "0" * 64)
        )
        session.commit()
    with hash_factory() as session:
        assert load_canonical_readiness_signal(session, signal_id=hash_signal).state == "unavailable"


def test_zero_and_six_evidence_rows_fail_closed(tmp_path, monkeypatch) -> None:
    zero_factory, _seeded, zero_signal, zero_version = _seed_current_signal(tmp_path)
    with zero_factory() as session:
        rows = tuple(
            session.scalars(
                select(InterviewReadinessSignalEvidence).where(
                    InterviewReadinessSignalEvidence.signal_version_id == zero_version
                )
            )
        )
        for row in rows:
            session.delete(row)
        session.commit()
    with zero_factory() as session:
        assert load_canonical_readiness_signal(session, signal_id=zero_signal).state == "unavailable"

    six_factory, _seeded, six_signal, six_version = _seed_current_signal(tmp_path)
    with six_factory() as session:
        row = session.scalar(
            select(InterviewReadinessSignalEvidence).where(
                InterviewReadinessSignalEvidence.signal_version_id == six_version
            )
        )
        assert row is not None
        original_scalars = session.scalars

        def six_rows(statement, *args, **kwargs):  # type: ignore[no-untyped-def]
            if "interview_readiness_signal_evidence" in str(statement):
                return iter([row] * 6)
            return original_scalars(statement, *args, **kwargs)

        monkeypatch.setattr(session, "scalars", six_rows)
        projection = load_canonical_readiness_signal(session, signal_id=six_signal)
    assert projection.state == "unavailable"


def test_five_evidence_rows_are_ordered_and_all_affect_source_fingerprint(tmp_path) -> None:
    excerpts = ["cache consistency tradeoffs"] * 5
    focuses = [
        {
            "id": "focus-1",
            "text": "Five evidence rows",
            "evidence_refs": [
                {
                    "source": "interview_note",
                    "path": "/difficulty_points",
                    "excerpt": excerpt,
                }
                for excerpt in excerpts
            ],
        }
    ]
    session_factory, _seeded, signal_id, version_id = _seed_current_signal(
        tmp_path,
        practice_focuses=focuses,
    )
    with session_factory() as session:
        projection = load_canonical_readiness_signal(session, signal_id=signal_id)
    assert projection.state == "current"
    assert projection.aggregate is not None
    assert [item.ordinal for item in projection.aggregate.evidence] == list(range(5))
    assert projection.aggregate.practice_source_fingerprint == (
        "sha256:73d48507c2fa9ce0f51fc1e036f9fb482861a49c2674d4ab308dc9a58f5e69ab"
    )

    with session_factory() as session:
        session.execute(
            update(InterviewReadinessSignalEvidence)
            .where(
                InterviewReadinessSignalEvidence.signal_version_id == version_id,
                InterviewReadinessSignalEvidence.ordinal == 4,
            )
            .values(source_field_sha256="sha256:" + "1" * 64)
        )
        session.commit()
    with session_factory() as session:
        changed = load_canonical_readiness_signal(session, signal_id=signal_id)
    assert changed.state == "unavailable"
    assert changed.error_code == "aggregate_source_mismatch"
    assert changed.aggregate is None


def test_shuffled_evidence_materialization_is_normalized_by_ordinal(
    tmp_path,
    monkeypatch,
) -> None:
    focuses = [
        {
            "id": "focus-1",
            "text": "Ordered evidence",
            "evidence_refs": [
                {
                    "source": "interview_note",
                    "path": "/difficulty_points",
                    "excerpt": "cache consistency tradeoffs",
                }
                for _index in range(5)
            ],
        }
    ]
    session_factory, _seeded, signal_id, version_id = _seed_current_signal(
        tmp_path,
        practice_focuses=focuses,
    )
    with session_factory() as session:
        rows = list(
            session.scalars(
                select(InterviewReadinessSignalEvidence).where(
                    InterviewReadinessSignalEvidence.signal_version_id == version_id
                )
            )
        )
        original_scalars = session.scalars

        def shuffled(statement, *args, **kwargs):  # type: ignore[no-untyped-def]
            if "interview_readiness_signal_evidence" in str(statement):
                return iter(reversed(rows))
            return original_scalars(statement, *args, **kwargs)

        monkeypatch.setattr(session, "scalars", shuffled)
        projection = load_canonical_readiness_signal(session, signal_id=signal_id)

    assert projection.state == "current"
    assert projection.aggregate is not None
    assert [item.ordinal for item in projection.aggregate.evidence] == list(range(5))


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("todo", "ready"),
        ("pending", "ready"),
        ("scheduled", "ready"),
        ("in_progress", "ready"),
        ("done", "not_eligible"),
        ("completed", "not_eligible"),
        ("cancelled", "not_eligible"),
        ("deleted", "not_eligible"),
        ("soft_deleted", "not_eligible"),
        ("unexpected", "not_eligible"),
    ],
)
def test_target_eligibility_uses_only_the_shared_lifecycle_classifier(
    tmp_path,
    status,
    expected,
) -> None:
    session_factory, seeded, _signal_id, version_id = _seed_current_signal(tmp_path)
    target_id = _target(
        session_factory,
        int(seeded["application_id"]),
        status=status,
        scheduled_at=datetime(1999, 1, 1, tzinfo=timezone.utc),
    )
    with session_factory() as session:
        projection = project_practice_focus(
            session,
            signal_version_id=version_id,
            target_event_id=target_id,
        )
    assert projection.state == expected


def test_target_must_be_exact_distinct_interview_in_same_application(tmp_path) -> None:
    session_factory, seeded, _signal_id, version_id = _seed_current_signal(tmp_path)
    wrong_type = _target(
        session_factory,
        int(seeded["application_id"]),
        event_type="written_test",
    )
    with session_factory() as session:
        other = Application(company_name="Other", position_name="Role", source="web")
        session.add(other)
        session.commit()
        other_id = other.id
    cross_app = _target(session_factory, other_id)

    with session_factory() as session:
        assert project_practice_focus(
            session,
            signal_version_id=version_id,
            target_event_id=int(seeded["event_id"]),
        ).state == "not_eligible"
        assert project_practice_focus(
            session,
            signal_version_id=version_id,
            target_event_id=wrong_type,
        ).state == "not_eligible"
        assert project_practice_focus(
            session,
            signal_version_id=version_id,
            target_event_id=cross_app,
        ).state == "not_eligible"
        assert project_practice_focus(
            session,
            signal_version_id=version_id,
            target_event_id=999_999,
        ).state == "target_missing"


def test_practiced_requires_the_exact_completed_signal_target_pair(tmp_path) -> None:
    session_factory, seeded, _signal_id, version_id = _seed_current_signal(tmp_path)
    target_id = _target(session_factory, int(seeded["application_id"]))
    other_target_id = _target(session_factory, int(seeded["application_id"]))
    with session_factory() as session:
        source = load_canonical_readiness_signal(session, signal_version_id=version_id)
        assert source.aggregate is not None
        event = session.get(ApplicationEvent, target_id)
        assert event is not None
        target_fingerprint = compute_practice_target_fingerprint_v1(event)
        _completed_plan(
            session,
            seeded=seeded,
            version_id=version_id,
            target_id=target_id,
            source_fingerprint=source.aggregate.practice_source_fingerprint,
            target_fingerprint=target_fingerprint,
        )
        session.commit()
    with session_factory() as session:
        assert project_practice_focus(
            session,
            signal_version_id=version_id,
            target_event_id=target_id,
        ).state == "completed"
        assert project_practice_focus(
            session,
            signal_version_id=version_id,
            target_event_id=other_target_id,
        ).state == "ready"


def test_existing_plan_fingerprints_precede_terminal_status(tmp_path) -> None:
    source_factory, source_seeded, _signal_id, source_version = _seed_current_signal(tmp_path)
    source_target = _target(source_factory, int(source_seeded["application_id"]))
    with source_factory() as session:
        source = load_canonical_readiness_signal(session, signal_version_id=source_version)
        target = session.get(ApplicationEvent, source_target)
        assert source.aggregate is not None and target is not None
        _completed_plan(
            session,
            seeded=source_seeded,
            version_id=source_version,
            target_id=source_target,
            source_fingerprint="sha256:" + "f" * 64,
            target_fingerprint=compute_practice_target_fingerprint_v1(target),
        )
        session.commit()
    with source_factory() as session:
        assert project_practice_focus(
            session,
            signal_version_id=source_version,
            target_event_id=source_target,
        ).state == "source_changed"

    target_factory, target_seeded, _signal_id, target_version = _seed_current_signal(tmp_path)
    changed_target = _target(target_factory, int(target_seeded["application_id"]))
    with target_factory() as session:
        source = load_canonical_readiness_signal(session, signal_version_id=target_version)
        target = session.get(ApplicationEvent, changed_target)
        assert source.aggregate is not None and target is not None
        target_fingerprint = compute_practice_target_fingerprint_v1(target)
        _completed_plan(
            session,
            seeded=target_seeded,
            version_id=target_version,
            target_id=changed_target,
            source_fingerprint=source.aggregate.practice_source_fingerprint,
            target_fingerprint=target_fingerprint,
        )
        target.round += 1
        session.commit()
    with target_factory() as session:
        assert project_practice_focus(
            session,
            signal_version_id=target_version,
            target_event_id=changed_target,
        ).state == "target_changed"


def test_target_fingerprint_includes_only_authorization_fields(tmp_path) -> None:
    session_factory = init_database(tmp_path / "target-fields.sqlite3")
    with session_factory() as session:
        application = Application(company_name="Acme", position_name="Backend", source="web")
        session.add(application)
        session.flush()
        event = ApplicationEvent(
            application_id=application.id,
            event_type="interview",
            subtype="technical",
            round=2,
            scheduled_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            duration_minutes=45,
            location="Room A",
            notes="private",
            remind_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
            status="todo",
        )
        event.tags = ["backend"]
        session.add(event)
        session.commit()
        baseline = compute_practice_target_fingerprint_v1(event)
        event.location = "Room B"
        event.notes = "changed"
        event.remind_at = None
        event.created_at = datetime(1990, 1, 1, tzinfo=timezone.utc)
        session.flush()
        assert compute_practice_target_fingerprint_v1(event) == baseline
        included_mutations = (
            ("event_type", "written_test"),
            ("subtype", "behavioral"),
            ("round", 3),
            ("scheduled_at", None),
            ("duration_minutes", 46),
            ("status", "in_progress"),
        )
        for field, changed in included_mutations:
            original = getattr(event, field)
            setattr(event, field, changed)
            session.flush()
            assert compute_practice_target_fingerprint_v1(event) != baseline
            setattr(event, field, original)
            session.flush()
        original_tags = event.tags
        event.tags = ["backend", "system-design"]
        session.flush()
        assert compute_practice_target_fingerprint_v1(event) != baseline
        event.tags = original_tags


def test_target_read_failure_is_explicitly_unavailable(tmp_path, monkeypatch) -> None:
    session_factory, seeded, _signal_id, version_id = _seed_current_signal(tmp_path)
    target_id = _target(session_factory, int(seeded["application_id"]))
    with session_factory() as session:
        original_get = session.get

        def fail_target(model, ident, *args, **kwargs):  # type: ignore[no-untyped-def]
            if model is ApplicationEvent and ident == target_id:
                raise OperationalError("SELECT", {}, RuntimeError("offline"))
            return original_get(model, ident, *args, **kwargs)

        monkeypatch.setattr(session, "get", fail_target)
        projection = project_practice_focus(
            session,
            signal_version_id=version_id,
            target_event_id=target_id,
        )
    assert projection.state == "unavailable"


def test_database_read_failure_is_explicitly_unavailable(tmp_path, monkeypatch) -> None:
    session_factory, _seeded, signal_id, _version_id = _seed_current_signal(tmp_path)
    with session_factory() as session:
        monkeypatch.setattr(
            session,
            "get",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OperationalError("SELECT", {}, RuntimeError("offline"))
            ),
        )
        projection = load_canonical_readiness_signal(session, signal_id=signal_id)
    assert projection.state == "unavailable"
    assert projection.error_code == "database_unavailable"


def test_baseline_interview_readiness_module_has_no_signal_dependency() -> None:
    """The full API orthogonality matrix belongs to the later Task 7 API wiring."""

    from offerpilot.repositories import interview_index

    source = __import__("inspect").getsource(interview_index)
    assert "InterviewReadinessSignal" not in source
    assert "readiness_signal" not in source
